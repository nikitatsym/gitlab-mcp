/**
 * Coverage probe: how well does GitLab's OpenAPI v3 spec cover the routes
 * gitbeaker exposes, and what would change if we switched body-schema source
 * of truth from gitbeaker `*Options` types to OpenAPI?
 *
 * Reads:
 *   node_modules/@gitbeaker/core/dist/index.js   — routes (verb + path)
 *   node_modules/@gitbeaker/core/dist/index.d.ts — *Options properties
 *   openapi/openapi_v3.yaml                      — OpenAPI v3 (vendored)
 *
 * Reports:
 *   - matched: gitbeaker routes that join with an OpenAPI operation
 *   - field deltas per matched route:
 *       openapi_only — fields OpenAPI describes that gitbeaker doesn't
 *                       (= the EditProject bug, scaled across all ops)
 *       gitbeaker_only — fields gitbeaker has that OpenAPI doesn't
 *                       (= regression risk if we drop gitbeaker types entirely)
 *   - needs_manual: gitbeaker routes with no OpenAPI match AND gitbeaker
 *                   *Options is either missing or open (index sig). These
 *                   require hand-written declarations under strict-only mode.
 *   - by-class breakdown so we can see which areas of the API are well-covered
 *
 * Runs: `cd codegen && npx tsx probe_openapi.ts`
 */
import { readFileSync } from "fs";
import { join, dirname } from "path";
import { fileURLToPath } from "url";
import * as ts from "typescript";
import { parse as yamlParse } from "yaml";
import { EE_EXCLUDED_CLASSES } from "./ee_exclusions.ts";
import { loadChecker, resolveMethod } from "./typeResolver.ts";

const __dirname = dirname(fileURLToPath(import.meta.url));
const IMPL_PATH = join(__dirname, "node_modules/@gitbeaker/core/dist/index.js");
const TYPES_PATH = join(__dirname, "node_modules/@gitbeaker/core/dist/index.d.ts");
const OPENAPI_PATH = join(__dirname, "openapi/openapi_v3.yaml");

const IMPL = readFileSync(IMPL_PATH, "utf-8");

// ── gitbeaker route extraction (subset of generate.ts) ────────────────────

interface Route {
  klass: string;     // concrete class (after Resource* expansion)
  baseKlass?: string;
  method: string;
  verb: string;      // get/post/put/patch/del
  pathTpl: string;   // gitbeaker template, e.g. "projects/${projectId}/foo"
  isConditional: boolean;
}

function isSafeTemplate(tpl: string): boolean {
  const refs = [...tpl.matchAll(/\$\{([^}]*)\}/g)];
  return refs.every((m) => /^\w+$/.test(m[1]) || /^this\.\w+$/.test(m[1]));
}

function parseRoutes(): { routes: Route[]; skipped: number } {
  const classRe =
    /var (\w+) = class extends requesterUtils\.BaseResource \{([\s\S]*?)^\};/gm;

  const baseRoutes: Map<string, Route[]> = new Map();
  const concreteRoutes: Route[] = [];
  let skipped = 0;

  let m: RegExpExecArray | null;
  while ((m = classRe.exec(IMPL)) !== null) {
    const klass = m[1];
    const body = m[2];
    if (EE_EXCLUDED_CLASSES.has(klass)) continue;

    const headRe = /^\s{2}(\w+)\(([^)]*)\)\s*\{/gm;
    let hm: RegExpExecArray | null;
    while ((hm = headRe.exec(body)) !== null) {
      const name = hm[1];
      if (name === "constructor") continue;
      const bodyStart = hm.index + hm[0].length;

      let depth = 1;
      let pos = bodyStart;
      while (depth > 0 && pos < body.length) {
        const ch = body[pos];
        if (ch === "{") depth++;
        else if (ch === "}") depth--;
        pos++;
      }
      const mBody = body.slice(bodyStart, pos - 1);

      // Pull first verb+template form. Matches generate.ts patterns 1a/1b/2/3.
      let verb: string | null = null;
      let pathTpl: string | null = null;
      const pEndpoint = mBody.match(
        /return RequestHelper\.(\w+)\(\)\(\s*this,\s*endpoint`([^`]+)`/,
      );
      if (pEndpoint && isSafeTemplate(pEndpoint[2])) {
        verb = pEndpoint[1];
        pathTpl = pEndpoint[2];
      }
      if (!verb || !pathTpl) {
        const pStr = mBody.match(
          /return RequestHelper\.(\w+)\(\)\(\s*this,\s*['"]([^'"]+)['"]/,
        );
        if (pStr) {
          verb = pStr[1];
          pathTpl = pStr[2];
        }
      }
      // Variable form: trace `varName = endpoint\`...\`` or string assignment.
      // For multi-branch methods (Projects.edit), take the LAST seen safe path.
      if (!verb || !pathTpl) {
        const pVar = mBody.match(
          /return RequestHelper\.(\w+)\(\)\(\s*this,\s*(\w+)\b/,
        );
        if (pVar) {
          const varName = pVar[2];
          let lastPath: string | null = null;
          const assignRe = new RegExp(
            `${varName}\\s*=\\s*(?:endpoint\`([^\`]+)\`|['"]([^'"]+)['"])`,
            "g",
          );
          let am: RegExpExecArray | null;
          while ((am = assignRe.exec(mBody)) !== null) {
            const cand = am[1] ?? am[2];
            if (isSafeTemplate(cand)) lastPath = cand;
          }
          if (lastPath) {
            verb = pVar[1];
            pathTpl = lastPath;
          }
        }
      }
      if (!verb || !pathTpl) {
        skipped++;
        continue;
      }

      // Crude conditional-URL detector: 2+ distinct path assignments to the
      // same URL var. Routes with this stay "conditional" so probe knows to
      // skip them in matching (gitbeaker picks the URL at runtime).
      const isConditional = /let\s+url\d*[\s\S]+url\d*\s*=[\s\S]+url\d*\s*=/.test(mBody);

      const route: Route = { klass, method: name, verb, pathTpl, isConditional };
      if (klass.startsWith("Resource")) {
        if (!baseRoutes.has(klass)) baseRoutes.set(klass, []);
        baseRoutes.get(klass)!.push(route);
      } else {
        concreteRoutes.push(route);
      }
    }
  }

  // Expand Resource* subclasses (minimal version of generate.ts logic).
  const subclassRe =
    /var (\w+) = class extends (Resource\w+) \{[\s\S]*?super\(([^)]*)\)/g;
  const baseConstructorPrefix = new Map<string, string>();
  const basePrefixRe =
    /var (Resource\w+) = class[\s\S]*?constructor\([^)]*\)\s*\{\s*super\(\s*\{\s*prefixUrl:\s*["']([^"']+)["']/g;
  let bpm: RegExpExecArray | null;
  while ((bpm = basePrefixRe.exec(IMPL)) !== null) {
    baseConstructorPrefix.set(bpm[1], bpm[2]);
  }

  let sm: RegExpExecArray | null;
  while ((sm = subclassRe.exec(IMPL)) !== null) {
    const concrete = sm[1];
    const base = sm[2];
    const superArgsRaw = sm[3];
    if (EE_EXCLUDED_CLASSES.has(concrete) || EE_EXCLUDED_CLASSES.has(base)) continue;
    const baseList = baseRoutes.get(base);
    if (!baseList) continue;
    const stringArgs = [...superArgsRaw.matchAll(/["']([^"']+)["']/g)].map((m) => m[1]);
    if (stringArgs.length === 0) continue;
    const resourceType = stringArgs[0];
    const resource2Type = stringArgs[1] ?? null;

    for (const bm of baseList) {
      let newPath = bm.pathTpl;
      if (resource2Type) {
        newPath = newPath.replace(
          /\$\{this\.(resource2Type|resourceType2|resourceType)\}/g,
          resource2Type,
        );
      } else {
        newPath = newPath.replace(
          /\$\{this\.(resource2Type|resourceType2|resourceType)\}/g,
          resourceType,
        );
      }
      const hardcoded = baseConstructorPrefix.get(base);
      const effectivePrefix = hardcoded ?? resourceType;
      newPath = `${effectivePrefix}/${newPath}`;
      if (/\$\{this\.\w+\}/.test(newPath)) continue;
      if (!isSafeTemplate(newPath)) continue;
      concreteRoutes.push({
        klass: concrete,
        baseKlass: base,
        method: bm.method,
        verb: bm.verb,
        pathTpl: newPath,
        isConditional: bm.isConditional,
      });
    }
  }

  return { routes: concreteRoutes, skipped };
}

// ── Path normalization (gitbeaker → OpenAPI key) ──────────────────────────

/**
 * Normalize a gitbeaker path template into the OpenAPI key form.
 *   gitbeaker: "projects/${projectId}/repository/branches"
 *   OpenAPI:   "/api/v4/projects/{id}/repository/branches"
 *
 * Strategy: replace every ${var} with a placeholder `{*}` and compare against
 * the OpenAPI path with its placeholders also normalized. OpenAPI uses {id}
 * for primary IDs and various names for nested IDs; we collapse all of them
 * to `{*}` for matching.
 */
function normalizeForJoin(pathTpl: string, addApiPrefix: boolean): string {
  let p = pathTpl.replace(/\$\{[^}]+\}/g, "{*}").replace(/\{[^}]+\}/g, "{*}");
  // Strip /api/v4 prefix if present.
  p = p.replace(/^\/api\/v4\//, "/");
  if (!p.startsWith("/")) p = "/" + p;
  // Strip trailing slash for stability.
  if (p.length > 1 && p.endsWith("/")) p = p.slice(0, -1);
  return p;
}

// ── OpenAPI parsing ────────────────────────────────────────────────────────

type JSONObj = Record<string, any>;

interface OpenApiOp {
  rawPath: string;
  verb: string;       // get/post/put/patch/delete
  pathParams: Set<string>;
  queryParams: Set<string>;
  bodyFields: Set<string>;
  bodySchemaRef?: string;
}

function loadOpenApi(): {
  ops: Map<string, OpenApiOp>;
  totalPaths: number;
  totalOps: number;
} {
  const text = readFileSync(OPENAPI_PATH, "utf-8");
  const spec = yamlParse(text) as JSONObj;
  const components = (spec.components?.schemas ?? {}) as JSONObj;

  function resolveRef(ref: string): JSONObj | null {
    const m = ref.match(/^#\/components\/schemas\/(.+)$/);
    if (!m) return null;
    const name = m[1];
    return components[name] ?? null;
  }

  function collectBodyProps(schema: JSONObj | null | undefined): Set<string> {
    const out = new Set<string>();
    if (!schema) return out;
    let resolved: JSONObj | null = schema;
    if (typeof schema.$ref === "string") {
      resolved = resolveRef(schema.$ref);
    }
    if (!resolved) return out;
    if (resolved.properties && typeof resolved.properties === "object") {
      for (const k of Object.keys(resolved.properties)) out.add(k);
    }
    // Some bodies have allOf/oneOf — flatten one level.
    for (const k of ["allOf", "oneOf", "anyOf"]) {
      const arr = resolved[k];
      if (Array.isArray(arr)) {
        for (const sub of arr) {
          for (const p of collectBodyProps(sub)) out.add(p);
        }
      }
    }
    return out;
  }

  const ops = new Map<string, OpenApiOp>();
  const paths = (spec.paths ?? {}) as JSONObj;
  let totalPaths = 0;
  let totalOps = 0;

  for (const [rawPath, pathItem] of Object.entries(paths)) {
    if (!pathItem || typeof pathItem !== "object") continue;
    totalPaths++;
    for (const verb of ["get", "post", "put", "patch", "delete"]) {
      const op = (pathItem as JSONObj)[verb];
      if (!op) continue;
      totalOps++;

      const pathParams = new Set<string>();
      const queryParams = new Set<string>();
      const params = Array.isArray(op.parameters) ? op.parameters : [];
      for (const p of params) {
        if (!p || typeof p !== "object") continue;
        const name = p.name;
        const where = p.in;
        if (typeof name !== "string") continue;
        if (where === "path") pathParams.add(name);
        else if (where === "query") queryParams.add(name);
      }

      const bodyFields = new Set<string>();
      let bodySchemaRef: string | undefined;
      const rb = op.requestBody;
      if (rb && typeof rb === "object" && rb.content && typeof rb.content === "object") {
        for (const media of Object.values(rb.content)) {
          const mediaObj = media as JSONObj;
          const schema = mediaObj.schema;
          if (schema && typeof schema === "object") {
            if (typeof schema.$ref === "string") bodySchemaRef = schema.$ref;
            for (const f of collectBodyProps(schema)) bodyFields.add(f);
          }
        }
      }

      const key = `${verb.toUpperCase()} ${normalizeForJoin(rawPath, true)}`;
      ops.set(key, {
        rawPath,
        verb,
        pathParams,
        queryParams,
        bodyFields,
        bodySchemaRef,
      });
    }
  }

  return { ops, totalPaths, totalOps };
}

// ── gitbeaker *Options field extraction ───────────────────────────────────

const { checker, source: tsSource } = loadChecker(TYPES_PATH);

interface GitbeakerOptions {
  fields: Set<string>;      // snake_case
  hasIndexSig: boolean;     // open (Record<string, any> shape)
  resolved: boolean;        // false = no TS info → treat as "missing"
}

function toSnake(s: string): string {
  return s
    .replace(/([A-Z]{2,})([A-Z][a-z])/g, "$1_$2")
    .replace(/([a-z\d])([A-Z])/g, "$1_$2")
    .toLowerCase();
}

function getGitbeakerOptions(klass: string, method: string): GitbeakerOptions {
  const info = resolveMethod(checker, tsSource, klass, method);
  if (!info) return { fields: new Set(), hasIndexSig: false, resolved: false };
  const fields = new Set<string>();
  for (const pa of info.positionalArgs) fields.add(toSnake(pa.name));
  for (const p of info.options.properties) fields.add(p.name);
  return {
    fields,
    hasIndexSig: info.options.hasIndexSignature,
    resolved: info.options.resolved || info.positionalArgs.length > 0,
  };
}

// ── Run probe ─────────────────────────────────────────────────────────────

const { routes, skipped: routeSkipped } = parseRoutes();
const { ops: openapiOps, totalPaths: oaPaths, totalOps: oaOps } = loadOpenApi();

interface Joined {
  route: Route;
  key: string;
  oa: OpenApiOp | null;
  gb: GitbeakerOptions;
}

const joined: Joined[] = [];
for (const r of routes) {
  const verbUpper = (r.verb === "del" ? "delete" : r.verb).toUpperCase();
  const key = `${verbUpper} ${normalizeForJoin(r.pathTpl, false)}`;
  const oa = openapiOps.get(key) ?? null;
  // For methods that look up the same gitbeaker base, the *Options type lives
  // on the base class, not the concrete one.
  const tsKlass = r.baseKlass ?? r.klass;
  const gb = getGitbeakerOptions(tsKlass, r.method);
  joined.push({ route: r, key, oa, gb });
}

// ── Aggregate ──────────────────────────────────────────────────────────────

let matched = 0;
let unmatched = 0;
let unmatchedConditional = 0;
let needsManual = 0;           // unmatched AND gitbeaker open/missing
let matchedClosedGitbeaker = 0; // matched AND gitbeaker closed (current behavior)
let openapiOnlyTotal = 0;
let gitbeakerOnlyTotal = 0;
let gitbeakerOnlySudoTotal = 0;

// Tail buckets for inspection
const openapiOnlyByMethod: { route: Route; extras: string[] }[] = [];
const gitbeakerOnlyByMethod: { route: Route; extras: string[] }[] = [];
const needsManualList: { route: Route; key: string; reason: string }[] = [];
const unmatchedExamples: { route: Route; key: string }[] = [];
const byClass = new Map<
  string,
  { matched: number; unmatched: number; needsManual: number; total: number }
>();

for (const j of joined) {
  const cls = j.route.klass;
  const cstat = byClass.get(cls) ?? { matched: 0, unmatched: 0, needsManual: 0, total: 0 };
  cstat.total++;
  byClass.set(cls, cstat);

  if (j.oa) {
    matched++;
    cstat.matched++;
    if (j.gb.resolved && !j.gb.hasIndexSig) matchedClosedGitbeaker++;

    // Path placeholders from gitbeaker template are NOT body fields. Without
    // this filter, every gitbeaker positional (project_id, branch, mergerequest_iid)
    // counts as "extra over OpenAPI" — false positive.
    const gbPathVars = new Set<string>();
    for (const m of j.route.pathTpl.matchAll(/\$\{(\w+)\}/g)) {
      gbPathVars.add(toSnake(m[1]));
    }
    // Pagination / client middleware fields that gitbeaker tracks but never go
    // on the wire as REST params. Also false positives.
    const GB_INTERNAL = new Set([
      "pagination", "per_page", "max_pages", "page", "order_by", "sort",
      "show_expanded", "as_admin", "as_stream", "is_form",
    ]);

    const openapiOnly = [...j.oa.bodyFields, ...j.oa.queryParams].filter(
      (f) => !j.gb.fields.has(f),
    );
    const gitbeakerOnly = [...j.gb.fields].filter(
      (f) =>
        !j.oa!.bodyFields.has(f) &&
        !j.oa!.queryParams.has(f) &&
        !j.oa!.pathParams.has(f) &&
        !gbPathVars.has(f) &&
        !GB_INTERNAL.has(f),
    );
    openapiOnlyTotal += openapiOnly.length;
    gitbeakerOnlyTotal += gitbeakerOnly.length;
    if (j.gb.fields.has("sudo")) gitbeakerOnlySudoTotal++;
    if (openapiOnly.length > 0)
      openapiOnlyByMethod.push({ route: j.route, extras: openapiOnly });
    if (gitbeakerOnly.length > 0)
      gitbeakerOnlyByMethod.push({ route: j.route, extras: gitbeakerOnly });
  } else {
    unmatched++;
    cstat.unmatched++;
    if (j.route.isConditional) unmatchedConditional++;
    const isOpen = !j.gb.resolved || j.gb.hasIndexSig;
    if (isOpen) {
      needsManual++;
      cstat.needsManual++;
      needsManualList.push({
        route: j.route,
        key: j.key,
        reason: !j.gb.resolved ? "no-TS-info" : "open-Record",
      });
    }
    if (unmatchedExamples.length < 20) unmatchedExamples.push({ route: j.route, key: j.key });
  }
}

// ── Report ────────────────────────────────────────────────────────────────

const line = (s: string) => process.stdout.write(s + "\n");

function pct(n: number, d: number) {
  if (d === 0) return "0%";
  return `${((n / d) * 100).toFixed(1)}%`;
}

line("─".repeat(72));
line("OpenAPI coverage probe");
line("─".repeat(72));
line(`gitbeaker routes parsed:      ${routes.length}`);
line(`gitbeaker routes skipped:     ${routeSkipped} (unrecognized impl shape)`);
line(`OpenAPI paths in spec:        ${oaPaths}`);
line(`OpenAPI ops (verb-paths):     ${oaOps}`);
line("");
line(`Matched gitbeaker -> OpenAPI: ${matched}  (${pct(matched, routes.length)})`);
line(`  of which gitbeaker today is closed (no varkwargs): ${matchedClosedGitbeaker}`);
line(`Unmatched:                    ${unmatched}  (${pct(unmatched, routes.length)})`);
line(`  of which conditional-URL:   ${unmatchedConditional}`);
line(`  of which need manual decl:  ${needsManual}`);
line(`     (gitbeaker open/missing, OpenAPI missing -> no source of truth)`);
line("");
line("Field-level deltas (matched routes only):");
line(`  Fields OpenAPI knows, gitbeaker doesn't: ${openapiOnlyTotal}`);
line(`     <- the EditProject class of bug; switching SOT fixes these`);
line(`  Fields gitbeaker has, OpenAPI doesn't:   ${gitbeakerOnlyTotal}`);
line(`     <- regression risk if gitbeaker dropped (path-vars excluded)`);
line(`     of which routes carrying 'sudo' (security, not REST):  ${gitbeakerOnlySudoTotal}`);
line("");

// Top classes by impact
line("By class (top 30 by total routes):");
const classRows = [...byClass.entries()].sort((a, b) => b[1].total - a[1].total).slice(0, 30);
const w = (s: string, n: number) => s.padEnd(n);
line(`  ${w("class", 36)} ${w("total", 6)} ${w("matched", 8)} ${w("unm.", 6)} ${w("manual", 7)}`);
for (const [cls, st] of classRows) {
  line(
    `  ${w(cls, 36)} ${w(st.total.toString(), 6)} ${w(st.matched + "", 8)} ${w(st.unmatched + "", 6)} ${w(st.needsManual + "", 7)}`,
  );
}
line("");

line("Top 15 matched routes where OpenAPI knows MORE than gitbeaker:");
const oaOnlySorted = openapiOnlyByMethod
  .sort((a, b) => b.extras.length - a.extras.length)
  .slice(0, 15);
for (const e of oaOnlySorted) {
  line(`  +${e.extras.length}  ${e.route.klass}.${e.route.method}`);
  line(`     ${e.extras.slice(0, 8).join(", ")}${e.extras.length > 8 ? ", …" : ""}`);
}
line("");

line("Top 15 matched routes where gitbeaker knows MORE than OpenAPI:");
const gbOnlySorted = gitbeakerOnlyByMethod
  .sort((a, b) => b.extras.length - a.extras.length)
  .slice(0, 15);
for (const e of gbOnlySorted) {
  line(`  -${e.extras.length}  ${e.route.klass}.${e.route.method}`);
  line(`     ${e.extras.slice(0, 8).join(", ")}${e.extras.length > 8 ? ", …" : ""}`);
}
line("");

line(`needs_manual tail (${needsManualList.length}):`);
const manualByClass = new Map<string, { route: Route; reason: string }[]>();
for (const m of needsManualList) {
  if (!manualByClass.has(m.route.klass)) manualByClass.set(m.route.klass, []);
  manualByClass.get(m.route.klass)!.push({ route: m.route, reason: m.reason });
}
for (const [cls, items] of [...manualByClass.entries()].sort((a, b) => b[1].length - a[1].length).slice(0, 20)) {
  line(`  ${cls}  (${items.length})`);
  for (const it of items.slice(0, 5)) {
    line(`    ${it.route.verb.toUpperCase()} ${it.route.pathTpl}  [${it.reason}]`);
  }
  if (items.length > 5) line(`    … +${items.length - 5} more`);
}
line("");

// EditProject focus: is the bug visible in this probe?
line("Focus: ProjectsEdit (the trigger case)");
const focus = joined.find((j) => j.route.klass === "Projects" && j.route.method === "edit");
if (focus) {
  line(`  gitbeaker route: ${focus.route.verb.toUpperCase()} ${focus.route.pathTpl}`);
  line(`  Matched OpenAPI: ${focus.oa ? "yes" : "NO"}  (key: ${focus.key})`);
  if (focus.oa) {
    line(`  gitbeaker fields: ${focus.gb.fields.size}, openapi fields: ${focus.oa.bodyFields.size}`);
    const oaOnly = [...focus.oa.bodyFields].filter((f) => !focus.gb.fields.has(f));
    const gbOnly = [...focus.gb.fields].filter(
      (f) =>
        !focus.oa!.bodyFields.has(f) &&
        !focus.oa!.queryParams.has(f) &&
        !focus.oa!.pathParams.has(f),
    );
    line(`  openapi_only (would be added): ${oaOnly.length}`);
    if (oaOnly.length > 0) line(`     ${oaOnly.slice(0, 15).join(", ")}${oaOnly.length > 15 ? ", …" : ""}`);
    line(`  gitbeaker_only (would be dropped): ${gbOnly.length}`);
    if (gbOnly.length > 0) line(`     ${gbOnly.slice(0, 15).join(", ")}${gbOnly.length > 15 ? ", …" : ""}`);
    line(`  package_registry_allow_anyone_to_pull_option in openapi? ${focus.oa.bodyFields.has("package_registry_allow_anyone_to_pull_option") ? "yes" : "NO"}`);
  }
}
