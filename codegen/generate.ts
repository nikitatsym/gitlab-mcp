/**
 * Codegen: parse @gitbeaker/core implementation (dist/index.js) and emit
 * `_generated.py` + `_generated_groups.py` under src/gitlab_mcp/.
 *
 * Gitbeaker's classes are uniform enough that a regex walk captures ~75% of
 * methods cleanly. Each method is a flat:
 *
 *   methodName(arg1, arg2, options) {
 *     return RequestHelper.VERB()(
 *       this,
 *       endpoint`path/${arg1}/subpath`,
 *       options
 *     );
 *   }
 *
 * We extract:
 *   - HTTP verb (get/post/put/patch/del)
 *   - Path template with `${argName}` placeholders
 *   - Arg list (positional, pre-options)
 *
 * And emit a Python wrapper:
 *
 *   def class_name_method_name(arg1: str | int, **options):
 *       """ClassName.methodName."""
 *       return _ok(_get_client().request(
 *           "GET",
 *           f"/path/{_enc(arg1)}/subpath",
 *           params=options,
 *       ))
 *
 * Non-path positional args are intentionally collapsed into **options — the
 * body field names that gitbeaker uses aren't reliably derivable from JS.
 * The LLM passes GitLab REST field names directly through the meta-tool.
 *
 * Usage:
 *   npx tsx generate.ts           # write files
 *   npx tsx generate.ts --check   # exit 1 if committed files diverge
 */

import { readFileSync, writeFileSync } from "fs";
import { execSync } from "child_process";
import { join, dirname } from "path";
import { fileURLToPath } from "url";
import { EE_EXCLUDED_CLASSES } from "./ee_exclusions.ts";
import { loadChecker, resolveMethod } from "./typeResolver.ts";

const __dirname = dirname(fileURLToPath(import.meta.url));
const IMPL_PATH = join(
  __dirname,
  "node_modules/@gitbeaker/core/dist/index.js"
);
const TYPES_PATH = join(
  __dirname,
  "node_modules/@gitbeaker/core/dist/index.d.ts"
);
const OUT_PY = join(__dirname, "../src/gitlab_mcp/_generated.py");
const OUT_GROUPS = join(__dirname, "../src/gitlab_mcp/_generated_groups.py");

const CHECK_MODE = process.argv.includes("--check");

const IMPL = readFileSync(IMPL_PATH, "utf-8");

// Load the TS Compiler API once; resolveMethod() walks it per method.
const { checker, source: tsSource } = loadChecker(TYPES_PATH);

// ── Parsing ────────────────────────────────────────────────────────────────

interface BodyField {
  name: string; // GitLab field name (snake_case on the wire)
  variable: string; // gitbeaker's variable name (camelCase, matches a positional arg)
}

interface ParsedMethod {
  klass: string;
  name: string;
  positionalArgs: string[]; // arg names in order, last may be 'options'
  verb: string; // get/post/put/patch/del
  pathTpl: string; // raw template, e.g. "projects/${projectId}/foo"
  isDestructured: boolean; // true if args started with `{` (destructured options)
  bodyFields: BodyField[]; // explicit body fields from gitbeaker's object literal
  conditionalPath: boolean; // gitbeaker switches URLs based on options (selector fields)
  conditionalBranches: ConditionalBranch[] | null; // parsed branches for dispatch
  conditionalSuffix: string; // appended after each branch path (Search.all: 'search')
  conditionalBodyFields: BodyField[]; // explicit fields in the return-call body literal
}

interface ConditionalBranch {
  selectorVar: string | null; // camelCase JS name, null for `else` fallback
  pathTpl: string;            // raw template like "projects/${projectId}/deploy_keys"
}

const classRe =
  /var (\w+) = class extends requesterUtils\.BaseResource \{([\s\S]*?)^\};/gm;

interface Delegation {
  klass: string;
  name: string;
  target: string;
  args: string[];
}

const methods: ParsedMethod[] = [];
const delegations: Delegation[] = [];
const skippedMethods: { klass: string; name: string; argsRaw: string }[] = [];

/**
 * Parse a gitbeaker RequestHelper body object literal.
 *
 * Given `{ branch: branchName, ref, ...options }` or `{ ...options, targetProjectId }`,
 * extracts the named fields (shorthand and renamed) and reports whether an
 * `...options` spread is present.
 *
 * mBody is the method body text; startAfter is the position right after the
 * opening `(` of the third RequestHelper arg — i.e., typically right after
 * `endpoint\`...\``. Returns null if the third arg isn't an object literal
 * (e.g., it's just `options` or a variable).
 */
function parseBodyLiteral(
  mBody: string,
  startAfter: number,
): BodyField[] | null {
  let pos = startAfter;
  // Skip whitespace and a leading comma (separating endpoint and body arg).
  while (pos < mBody.length && /\s/.test(mBody[pos])) pos++;
  if (mBody[pos] !== ",") return null;
  pos++;
  while (pos < mBody.length && /\s/.test(mBody[pos])) pos++;
  if (mBody[pos] !== "{") return null;

  // Balanced-brace extraction of the object content.
  let depth = 1;
  const start = pos + 1;
  pos++;
  while (pos < mBody.length && depth > 0) {
    if (mBody[pos] === "{") depth++;
    else if (mBody[pos] === "}") depth--;
    if (depth > 0) pos++;
  }
  if (depth !== 0) return null;
  const content = mBody.slice(start, pos);

  const fields: BodyField[] = [];
  // Split by commas that aren't inside nested structures. Since we already
  // matched the outer braces, anything left is comma-separated entries.
  // Simple split works for the common shorthand/renamed patterns.
  const parts = content
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);

  for (const part of parts) {
    if (part.startsWith("...")) continue;  // spread — ignore
    const renameMatch = part.match(/^(\w+)\s*:\s*(\w+)$/);
    if (renameMatch) {
      fields.push({ name: renameMatch[1], variable: renameMatch[2] });
      continue;
    }
    const shorthandMatch = part.match(/^(\w+)$/);
    if (shorthandMatch) {
      fields.push({
        name: shorthandMatch[1],
        variable: shorthandMatch[1],
      });
      continue;
    }
    // Anything else (computed keys, method shorthand, nested objects) — skip.
  }

  return fields;
}
const stats = {
  classesFound: 0,
  classesExcluded: 0,
  methodsTotal: 0,
  methodsParsed: 0,
  methodsSkipped: 0,
  methodsTyped: 0,           // resolved via TS + emitted with typed params
  methodsKeptOptions: 0,     // resolved but options has [k:string]:any → kept **options
  methodsTypeFallback: 0,    // typeResolver returned null/!resolved → legacy shape
  methodsConditionalPath: 0, // JS impl selects URL based on selector field → forced **options
};

let m: RegExpExecArray | null;
while ((m = classRe.exec(IMPL)) !== null) {
  const klass = m[1];
  const body = m[2];
  stats.classesFound++;

  if (EE_EXCLUDED_CLASSES.has(klass)) {
    stats.classesExcluded++;
    continue;
  }

  // Find method heads: `  methodName(args) {`
  const headRe = /^\s{2}(\w+)\(([^)]*)\)\s*\{/gm;
  let hm: RegExpExecArray | null;
  while ((hm = headRe.exec(body)) !== null) {
    const name = hm[1];
    const argsRaw = hm[2];
    const bodyStart = hm.index + hm[0].length;
    stats.methodsTotal++;

    // Skip JS keywords caught by accident (constructor, etc.)
    if (name === "constructor") continue;

    // Balanced-brace search for method body end
    let depth = 1;
    let pos = bodyStart;
    while (depth > 0 && pos < body.length) {
      const ch = body[pos];
      if (ch === "{") depth++;
      else if (ch === "}") depth--;
      pos++;
    }
    const mBody = body.slice(bodyStart, pos - 1);

    // Extract positional identifier args from the method signature.
    // Handles mixed forms like `projectId, { nested, ...options } = {}` —
    // we take the leading sequence of plain identifiers and stop at the
    // first destructured/rest arg.
    const leadingSimple = argsRaw.match(/^((?:\s*\w+\s*,)*\s*\w+)/);
    const leadingPositional = leadingSimple
      ? leadingSimple[1]
          .split(",")
          .map((s) => s.trim())
          .filter(Boolean)
      : [];

    // Also extract destructured field names. Methods like
    // `all({ userId, ...options } = {})` expose `userId` as a usable var that
    // can appear in the path template, so we treat it as a positional arg too.
    const destructuredVars: string[] = [];
    const destructMatch = argsRaw.match(/\{([^{}]+)\}/);
    if (destructMatch) {
      for (const field of destructMatch[1].split(",")) {
        const trimmed = field.trim();
        if (
          trimmed &&
          !trimmed.startsWith("...") &&
          !trimmed.includes("=") &&
          /^\w+$/.test(trimmed)
        ) {
          destructuredVars.push(trimmed);
        }
      }
    }

    const positionalArgs = [...leadingPositional, ...destructuredVars];

    // Match any `return RequestHelper.VERB()(this, X, ...)` form.
    let verb: string | null = null;
    let pathTpl: string | null = null;

    // Pattern 1a: endpoint-tagged template literal (preferred, most common).
    let bodyFieldsFromEndpoint: BodyField[] | null = null;
    const pEndpoint = mBody.match(
      /return RequestHelper\.(\w+)\(\)\(\s*this,\s*endpoint`([^`]+)`/
    );
    if (pEndpoint && isSafeTemplate(pEndpoint[2])) {
      verb = pEndpoint[1];
      pathTpl = pEndpoint[2];
      // Try to parse the body object literal that follows the endpoint template.
      const afterBacktick =
        pEndpoint.index! + pEndpoint[0].length;
      bodyFieldsFromEndpoint = parseBodyLiteral(mBody, afterBacktick);
    }

    // Pattern 1b: plain template literal `...${var}...`. Use only when every
    // ${var} reference is a simple identifier AND a method positional arg.
    if (!verb || !pathTpl) {
      const pPlainTpl = mBody.match(
        /return RequestHelper\.(\w+)\(\)\(\s*this,\s*`([^`]+)`/
      );
      if (pPlainTpl && isSafeTemplate(pPlainTpl[2])) {
        const candidate = pPlainTpl[2];
        const varRefs = [...candidate.matchAll(/\$\{(\w+)\}/g)].map((m) => m[1]);
        if (varRefs.every((v) => positionalArgs.includes(v))) {
          verb = pPlainTpl[1];
          pathTpl = candidate;
        }
      }
    }

    // Pattern 2: plain string literal.
    if (!verb || !pathTpl) {
      const pString = mBody.match(
        /return RequestHelper\.(\w+)\(\)\(\s*this,\s*['"]([^'"]+)['"]/
      );
      if (pString) {
        verb = pString[1];
        pathTpl = pString[2];
      }
    }

    // Pattern 3: variable form. Trace varName back to its last assignment,
    // looking for endpoint-template, plain template with method-arg vars,
    // plain string, or ternary `VAR = cond ? A : B` (take B as the fallback).
    if (!verb || !pathTpl) {
      const pVar = mBody.match(
        /return RequestHelper\.(\w+)\(\)\(\s*this,\s*(\w+)\b/
      );
      if (pVar) {
        const varName = pVar[2];
        let lastPath: string | null = null;

        // 3a: `varName = endpoint\`...\`` or `varName = "literal"`
        const assignRe = new RegExp(
          `${varName}\\s*=\\s*(?:endpoint\`([^\`]+)\`|['"]([^'"]+)['"])`,
          "g"
        );
        let am: RegExpExecArray | null;
        while ((am = assignRe.exec(mBody)) !== null) {
          const cand = am[1] ?? am[2];
          if (isSafeTemplate(cand)) lastPath = cand;
        }

        // 3b: ternary `const varName = cond ? A : B` — take the else branch (B).
        // Handles templates (with or without `endpoint` tag) and plain strings
        // on either side.
        if (!lastPath) {
          const ternaryRe = new RegExp(
            `${varName}\\s*=\\s*\\w+\\s*\\?\\s*(?:(?:endpoint)?\`([^\`]+)\`|['"]([^'"]+)['"])\\s*:\\s*(?:(?:endpoint)?\`([^\`]+)\`|['"]([^'"]+)['"])`
          );
          const tm = mBody.match(ternaryRe);
          if (tm) {
            const cand = tm[3] ?? tm[4] ?? null;
            if (cand && isSafeTemplate(cand)) lastPath = cand;
          }
        }

        // 3c: plain template assignment `varName = \`...${var}...\``
        if (!lastPath) {
          const tplAssign = new RegExp(
            `${varName}\\s*=\\s*\`([^\`]+)\``,
            "g"
          );
          let tm: RegExpExecArray | null;
          while ((tm = tplAssign.exec(mBody)) !== null) {
            const candidate = tm[1];
            if (!isSafeTemplate(candidate)) continue;
            const varRefs = [...candidate.matchAll(/\$\{(\w+)\}/g)].map(
              (m2) => m2[1]
            );
            if (varRefs.every((v) => positionalArgs.includes(v))) {
              lastPath = candidate;
            }
          }
        }

        if (lastPath) {
          verb = pVar[1];
          pathTpl = lastPath;
        }
      }
    }

    // Pattern 4: inline template literal with leading local var (fallback).
    // `${localVar}rest_of_path` — drop the leading var, use the literal rest.
    if (!verb || !pathTpl) {
      const pLeadVar = mBody.match(
        /return RequestHelper\.(\w+)\(\)\(\s*this,\s*`\$\{\w+\}([^`]*)`/
      );
      if (pLeadVar && pLeadVar[2]) {
        verb = pLeadVar[1];
        pathTpl = pLeadVar[2];
      }
    }

    // Pattern 5: url4/url5 helpers (ResourceAwardEmojis / ResourceNoteAwardEmojis).
    // These compute paths like `${a}/${b}/${c}/award_emoji[/${d}]` and
    // `${a}/${b}/${c}/notes/${d}/award_emoji[/${e}]` but are opaque function
    // calls to our parser. Recognize them as specific patterns and expand
    // inline.
    if (!verb || !pathTpl) {
      const pUrl4 = mBody.match(
        /return RequestHelper\.(\w+)\(\)\(\s*this,\s*url4\(([^)]*)\)/
      );
      if (pUrl4) {
        const args = pUrl4[2].split(",").map((s) => s.trim());
        // url4(resourceId, resourceType2, resourceId2[, awardId])
        if (args.length >= 3) {
          let path =
            `\${${args[0]}}/${args[1].startsWith("this.") ? "${" + args[1] + "}" : args[1]}/\${${args[2]}}/award_emoji`;
          if (args.length >= 4) path += `/\${${args[3]}}`;
          verb = pUrl4[1];
          pathTpl = path;
        }
      }
    }

    if (!verb || !pathTpl) {
      const pUrl5 = mBody.match(
        /return RequestHelper\.(\w+)\(\)\(\s*this,\s*url5\(([^)]*)\)/
      );
      if (pUrl5) {
        const args = pUrl5[2].split(",").map((s) => s.trim());
        // url5(resourceId, resourceType2, resourceId2, noteId[, awardId])
        if (args.length >= 4) {
          let path =
            `\${${args[0]}}/${args[1].startsWith("this.") ? "${" + args[1] + "}" : args[1]}/\${${args[2]}}/notes/\${${args[3]}}/award_emoji`;
          if (args.length >= 5) path += `/\${${args[4]}}`;
          verb = pUrl5[1];
          pathTpl = path;
        }
      }
    }

    // (Legacy field kept only to minimize touching emitFn below.)
    const isDestructured = argsRaw.trim().startsWith("{");

    if (!verb || !pathTpl) {
      // Pattern 5: delegation to another method on the same class,
      // e.g. `return this.create(projectId, branchName, options);`.
      // Emit as an alias after all methods are collected.
      const pDelegate = mBody.match(/return this\.(\w+)\(([^)]*)\)/);
      if (pDelegate) {
        const targetArgs = pDelegate[2]
          .split(",")
          .map((s) => s.trim())
          .filter(Boolean);
        delegations.push({
          klass,
          name,
          target: pDelegate[1],
          args: targetArgs,
        });
        stats.methodsParsed++;
        continue;
      }
      stats.methodsSkipped++;
      skippedMethods.push({ klass, name, argsRaw });
      continue;
    }

    // Detect methods where gitbeaker conditionally selects between distinct
    // URLs based on an option/destructured arg (e.g. DeployKeys.all picks
    // /projects/{id}/deploy_keys vs /users/{id}/project_deploy_keys vs
    // /deploy_keys). For these, emit a Python dispatch chain so the right
    // URL is hit instead of forwarding the selector as a query param.
    const conditionalPath = hasConditionalUrl(mBody);
    let conditionalBranches: ConditionalBranch[] | null = null;
    let conditionalSuffix = "";
    let conditionalBodyFields: BodyField[] = [];
    if (conditionalPath) {
      const parsed = parseConditional(mBody);
      if (parsed) {
        conditionalBranches = parsed.branches;
        conditionalSuffix = parsed.suffix;
        conditionalBodyFields = parsed.bodyFields;
      }
    }

    methods.push({
      klass,
      name,
      positionalArgs,
      verb,
      pathTpl,
      isDestructured,
      bodyFields: bodyFieldsFromEndpoint ?? [],
      conditionalPath,
      conditionalBranches,
      conditionalSuffix,
      conditionalBodyFields,
    });
    stats.methodsParsed++;
  }
}

/**
 * Parse a gitbeaker conditional-URL method into branches.
 *
 * Returns the URL variable's assignment chain in declaration order and any
 * suffix concatenated to it in the RequestHelper call. Walks tokens left-
 * to-right tracking the most-recent `if (X)` / `else if (X)` / `else`, then
 * pairs each assignment with the condition immediately preceding it.
 *
 * Returns null if the body doesn't fit the pattern (no `let URL` decl, no
 * RequestHelper call referencing the var, or no branches).
 */
function parseConditional(mBody: string): {
  branches: ConditionalBranch[];
  suffix: string;
  bodyFields: BodyField[];
} | null {
  const letMatch = mBody.match(/\blet\s+(\w+)\b/);
  if (!letMatch) return null;
  const urlVar = letMatch[1];

  // `[^'"]*` (not `+`) so empty fallbacks like `url12 = ""` get captured —
  // they concatenate with the suffix in the return form.
  const tokenRe = new RegExp(
    `(?:\\b(if|else\\s+if|else)\\b\\s*(?:\\(\\s*(\\w+)\\s*\\))?|\\b${urlVar}\\s*=\\s*(?:endpoint\`([^\`]+)\`|\`([^\`]+)\`|['"]([^'"]*)['"]))`,
    "g",
  );

  const branches: ConditionalBranch[] = [];
  let pendingVar: string | null = null;
  let pendingIsElse = false;
  let m: RegExpExecArray | null;
  while ((m = tokenRe.exec(mBody)) !== null) {
    if (m[1]) {
      const kind = m[1].trim().replace(/\s+/g, " ");
      if (kind === "if" || kind === "else if") {
        pendingVar = m[2] ?? null;
        pendingIsElse = false;
      } else {
        pendingVar = null;
        pendingIsElse = true;
      }
    } else {
      const path = m[3] ?? m[4] ?? m[5] ?? "";
      if (pendingVar !== null || pendingIsElse) {
        branches.push({
          selectorVar: pendingVar,
          pathTpl: path,
        });
      }
      pendingVar = null;
      pendingIsElse = false;
    }
  }

  if (branches.length < 2) return null;

  // RequestHelper call form: direct `(this, urlVar, …)` OR concat
  // `(this, \`${urlVar}suffix\`, …)`. The suffix gets appended to every branch.
  // Also captures the end position of the URL arg so we can parse the body
  // literal that follows.
  let suffix = "";
  let urlArgEnd = -1;
  const directRe = new RegExp(
    `RequestHelper\\.\\w+\\(\\)\\(\\s*this,\\s*${urlVar}\\b`,
  );
  const concatRe = new RegExp(
    `RequestHelper\\.\\w+\\(\\)\\(\\s*this,\\s*\`\\$\\{${urlVar}\\}([^\`]*)\``,
  );
  const directMatch = mBody.match(directRe);
  if (directMatch && directMatch.index !== undefined) {
    suffix = "";
    urlArgEnd = directMatch.index + directMatch[0].length;
  } else {
    const c = mBody.match(concatRe);
    if (!c || c.index === undefined) return null;
    suffix = c[1];
    urlArgEnd = c.index + c[0].length;
  }

  for (const b of branches) {
    if (!isSafeTemplate(b.pathTpl + suffix)) return null;
  }

  // Body literal in the return call: `(this, url, {token, ...options})`.
  // Reuse parseBodyLiteral, which expects to start right after the URL arg
  // (so it sees the `, {…}`).
  const bodyFields = parseBodyLiteral(mBody, urlArgEnd) ?? [];
  return { branches, suffix, bodyFields };
}

/**
 * True if the method body assigns 2+ distinct URL strings to the same local
 * variable — the gitbeaker pattern for path-selector options.
 *
 *   let url12;
 *   if (projectId) url12 = endpoint`projects/${projectId}/deploy_keys`;
 *   else if (userId) url12 = endpoint`users/${userId}/project_deploy_keys`;
 *   else url12 = "deploy_keys";
 *
 * We don't try to recover the selector — the JS parser picks one path (the
 * unconditional fallback) and the caller-facing surface drops to **options
 * so we don't claim e.g. `project_id` is a query param when it's actually a
 * path selector.
 */
function hasConditionalUrl(mBody: string): boolean {
  // Identify the URL var (or vars) by walking RequestHelper.X()(this, V, …)
  // calls. Bare endpoint strings like `url = "runners"` are valid URLs but
  // wouldn't look like one to a naive content check — anchoring on
  // RequestHelper avoids false positives from unrelated local vars instead.
  const urlVars = new Set<string>();
  const urlVarRe = /RequestHelper\.\w+\(\)\(\s*this,\s*(?:(\w+)\b|`\$\{(\w+)\})/g;
  let um: RegExpExecArray | null;
  while ((um = urlVarRe.exec(mBody)) !== null) {
    if (um[1]) urlVars.add(um[1]);
    if (um[2]) urlVars.add(um[2]);
  }
  if (urlVars.size === 0) return false;

  // Count distinct assignments per URL var. `[^'"]*` (not `+`) captures empty
  // fallbacks like `url12 = ""` (Search.all's global path).
  const seenByVar = new Map<string, Set<string>>();
  const re = /\b(\w+)\s*=\s*(?:endpoint`([^`]+)`|`([^`]+)`|['"]([^'"]*)['"])/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(mBody)) !== null) {
    if (!urlVars.has(m[1])) continue;
    const val = m[2] ?? m[3] ?? m[4] ?? "";
    if (!seenByVar.has(m[1])) seenByVar.set(m[1], new Set());
    seenByVar.get(m[1])!.add(val);
  }
  for (const vals of seenByVar.values()) {
    if (vals.size >= 2) return true;
  }
  return false;
}

// ── Template safety check ─────────────────────────────────────────────────

/**
 * Return true iff every `${expr}` in the template has `expr` as a plain
 * identifier or a `this.field` access. Rejects function calls or other
 * complex expressions that would produce broken Python f-strings.
 * `this.X` placeholders are allowed because the resource-subclass expansion
 * step substitutes them with literal strings before emission.
 */
function isSafeTemplate(tpl: string): boolean {
  const refs = [...tpl.matchAll(/\$\{([^}]*)\}/g)];
  return refs.every(
    (m) => /^\w+$/.test(m[1]) || /^this\.\w+$/.test(m[1])
  );
}

// ── Resource base class expansion ─────────────────────────────────────────

/**
 * Gitbeaker's Resource* base classes (ResourceLabels, ResourceMembers,
 * ResourceNotes, …) hold the actual method implementations, while concrete
 * subclasses like `ProjectLabels` / `GroupMembers` / `IssueNotes` only set a
 * URL prefix via `super("projects", options)` or `super("projects", "issues",
 * options)`. This function walks those subclasses and expands each base
 * method into a concrete ParsedMethod with the prefix and (where applicable)
 * secondary resource type substituted in.
 */
function resourceArgCamel(resourceType: string, isSecond: boolean): string {
  const cleaned = resourceType.replace(/[^a-z0-9_]/gi, "");
  const singular = cleaned.endsWith("s") ? cleaned.slice(0, -1) : cleaned;
  // snake_case → camelCase
  const camel = singular
    .split("_")
    .map((w, i) => (i === 0 ? w : w.charAt(0).toUpperCase() + w.slice(1)))
    .join("");
  const iidTypes = ["issue", "mergeRequest", "mergerequest", "epic"];
  const suffix = isSecond && iidTypes.some((t) => camel.toLowerCase().startsWith(t.toLowerCase()))
    ? "Iid"
    : "Id";
  return camel + suffix;
}

const baseConstructorPrefix: Map<string, string> = new Map();

function expandResourceSubclasses(): number {
  // Partition parsed methods: base classes → map, others stay in `methods`.
  const resourceBaseMethods: Map<string, ParsedMethod[]> = new Map();
  const nonBaseMethods: ParsedMethod[] = [];
  for (const pm of methods) {
    if (pm.klass.startsWith("Resource")) {
      if (!resourceBaseMethods.has(pm.klass)) {
        resourceBaseMethods.set(pm.klass, []);
      }
      resourceBaseMethods.get(pm.klass)!.push(pm);
    } else {
      nonBaseMethods.push(pm);
    }
  }
  methods.length = 0;
  methods.push(...nonBaseMethods);

  // Pre-scan: which Resource* base classes hardcode prefixUrl in their own
  // constructor (so subclass super() args don't set the prefix)?
  const basePrefixRe =
    /var (Resource\w+) = class[\s\S]*?constructor\([^)]*\)\s*\{\s*super\(\s*\{\s*prefixUrl:\s*["']([^"']+)["']/g;
  let bpm: RegExpExecArray | null;
  while ((bpm = basePrefixRe.exec(IMPL)) !== null) {
    baseConstructorPrefix.set(bpm[1], bpm[2]);
  }

  // Second pass: find concrete subclasses `var X = class extends ResourceY { … super(args) }`.
  const subclassRe =
    /var (\w+) = class extends (Resource\w+) \{[\s\S]*?super\(([^)]*)\)/g;
  let expanded = 0;
  let sm: RegExpExecArray | null;

  while ((sm = subclassRe.exec(IMPL)) !== null) {
    const concreteName = sm[1];
    const baseName = sm[2];
    const superArgsRaw = sm[3];

    if (EE_EXCLUDED_CLASSES.has(concreteName)) continue;
    if (EE_EXCLUDED_CLASSES.has(baseName)) continue;

    const baseMethods = resourceBaseMethods.get(baseName);
    if (!baseMethods || baseMethods.length === 0) continue;

    const stringArgs = [...superArgsRaw.matchAll(/["']([^"']+)["']/g)].map(
      (m) => m[1]
    );
    if (stringArgs.length === 0) continue;

    const resourceType = stringArgs[0];
    const resource2Type: string | null = stringArgs[1] ?? null;

    const primaryArg = resourceArgCamel(resourceType, false);
    const secondaryArg: string | null = resource2Type
      ? resourceArgCamel(resource2Type, true)
      : null;

    for (const bm of baseMethods) {
      let newPath = bm.pathTpl;

      // 1. Substitute any `${this.resourceType*}` / `${this.resource2Type}`
      //    with the literal resource2Type from super(...). Gitbeaker uses
      //    several field name variants for the same concept.
      if (resource2Type) {
        newPath = newPath.replace(
          /\$\{this\.(resource2Type|resourceType2|resourceType)\}/g,
          resource2Type
        );
      } else {
        // Some Resource* bases hardcode prefixUrl and store the super arg
        // into `this.resourceType`. For those, primary is a type-placeholder
        // and we don't have a prefix from super args.
        newPath = newPath.replace(
          /\$\{this\.(resource2Type|resourceType2|resourceType)\}/g,
          resourceType
        );
      }

      // 2. Prepend the primary prefix.
      //    If the base class hardcodes prefixUrl (e.g., ResourceNoteAwardEmojis
      //    always uses "projects"), use that literal; else use the super() arg.
      const hardcoded = baseConstructorPrefix.get(baseName);
      const effectivePrefix = hardcoded ?? resourceType;
      newPath = `${effectivePrefix}/${newPath}`;

      // 3. Rename generic `${resourceId}` → `${primaryArg}` for clarity.
      newPath = newPath.replace(
        /\$\{resourceId\}/g,
        "${" + primaryArg + "}"
      );
      if (secondaryArg) {
        newPath = newPath.replace(
          /\$\{resource2Id\}/g,
          "${" + secondaryArg + "}"
        );
      }

      // 4. Any remaining `${this.X}` means we couldn't resolve a runtime attr; skip.
      if (/\$\{this\.\w+\}/.test(newPath)) continue;
      if (!isSafeTemplate(newPath)) continue;

      const newPositional = bm.positionalArgs.map((a) => {
        if (a === "resourceId") return primaryArg;
        if (a === "resource2Id") return secondaryArg ?? a;
        return a;
      });

      methods.push({
        klass: concreteName,
        name: bm.name,
        positionalArgs: newPositional,
        verb: bm.verb,
        pathTpl: newPath,
        isDestructured: bm.isDestructured,
        bodyFields: bm.bodyFields,
        conditionalPath: bm.conditionalPath,
        // Conditional branches were parsed against the BASE class's path
        // template, which the expansion above rewrites. Re-parsing per
        // subclass is overkill — drop the branches so emitFn falls back to
        // `**options`-only for any (rare) selector-driven Resource* method.
        conditionalBranches: null,
        conditionalSuffix: "",
        conditionalBodyFields: [],
      });
      expanded++;
      stats.methodsParsed++;
    }
  }

  return expanded;
}

const expansionCount = expandResourceSubclasses();

// ── Name conversions ───────────────────────────────────────────────────────

function toSnake(s: string): string {
  // Use {2,} instead of + so single-cap sequences like `IId` stay together
  // (becomes `iid` after lowercasing) rather than splitting into `i_id`.
  return s
    .replace(/([A-Z]{2,})([A-Z][a-z])/g, "$1_$2")
    .replace(/([a-z\d])([A-Z])/g, "$1_$2")
    .toLowerCase();
}

function toPascal(snake: string): string {
  return snake
    .split("_")
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join("");
}

// Python keywords that cannot be used as function parameter names.
const PY_KEYWORDS = new Set([
  "False", "None", "True", "and", "as", "assert", "async", "await",
  "break", "class", "continue", "def", "del", "elif", "else", "except",
  "finally", "for", "from", "global", "if", "import", "in", "is",
  "lambda", "nonlocal", "not", "or", "pass", "raise", "return", "try",
  "while", "with", "yield",
]);

// ── Path template → Python f-string ───────────────────────────────────────

function pyPath(tpl: string, argsInPath: Set<string>): string {
  // Replace ${arg} with {_enc(arg_snake)}, tracking which args are path vars.
  const out = tpl.replace(/\$\{(\w+)\}/g, (_, arg) => {
    argsInPath.add(arg);
    return `{_enc(${toSnake(arg)})}`;
  });
  return "/" + out.replace(/^\/+/, "");
}

/**
 * Split `path?key=${var}&key2=${var2}` into a clean path and a list of
 * (wire-key, JS-var) query pairs. Used by conditional-dispatch emission so
 * embedded query vars become payload entries instead of staying baked into
 * the URL (where httpx + `params=` would conflict).
 */
function splitPathQuery(tpl: string): {
  pathTpl: string;
  queryVars: { wire: string; jsVar: string }[];
} {
  const q = tpl.indexOf("?");
  if (q === -1) return { pathTpl: tpl, queryVars: [] };
  const pathTpl = tpl.slice(0, q);
  const queryPart = tpl.slice(q + 1);
  const queryVars: { wire: string; jsVar: string }[] = [];
  for (const part of queryPart.split("&")) {
    const m = part.match(/^(\w+)=\$\{(\w+)\}$/);
    if (m) queryVars.push({ wire: m[1], jsVar: m[2] });
  }
  return { pathTpl, queryVars };
}

// ── Generate Python wrapper function ──────────────────────────────────────

interface Emitted {
  snakeName: string;
  pascalName: string;
  klass: string;
  verb: string; // lowercase
  pyLines: string[];
}

interface BodyParam {
  pyName: string;
  wireName: string;
  pyType: string;       // already includes ` | None` when nullable
}

function emitConditionalDispatch(
  pm: ParsedMethod,
  typeInfo: ReturnType<typeof resolveMethod>,
): Emitted | null {
  if (!pm.conditionalBranches) return null;

  // 1. Per-branch: split `path?key=${var}` into path + query vars. The query
  //    part can't stay in the URL — httpx's `params=` doesn't reliably merge
  //    with an embedded query string (`keys?fingerprint=…` + params=… loses
  //    the embedded field). Move query vars into payload via a branch-local
  //    set so they only land on the right URL.
  const branchPaths: {
    selectorVar: string | null;
    pyPath: string;
    pathVars: Set<string>;
    queryVars: { wire: string; jsVar: string; pyName: string }[];
  }[] = [];
  for (const b of pm.conditionalBranches) {
    const fullTpl = b.pathTpl + pm.conditionalSuffix;
    const { pathTpl, queryVars: rawQv } = splitPathQuery(fullTpl);
    const argsSet = new Set<string>();
    const py = pyPath(pathTpl, argsSet);
    const queryVars = rawQv.map((qv) => {
      const snake = toSnake(qv.jsVar);
      return {
        wire: qv.wire,
        jsVar: qv.jsVar,
        pyName: PY_KEYWORDS.has(snake) ? snake + "_" : snake,
      };
    });
    branchPaths.push({
      selectorVar: b.selectorVar,
      pyPath: py,
      pathVars: argsSet,
      queryVars,
    });
  }

  // Union of every var that appears in ANY branch path — these go in the URL
  // and MUST NOT be re-sent as query/body, even when TS declares them as
  // positional args.
  const allPathVars = new Set<string>();
  for (const bp of branchPaths) {
    for (const v of bp.pathVars) allPathVars.add(v);
  }

  // 2. Unique selector vars, ordered by first appearance.
  const selectorVars: string[] = [];
  for (const b of pm.conditionalBranches) {
    if (b.selectorVar && !selectorVars.includes(b.selectorVar)) {
      selectorVars.push(b.selectorVar);
    }
  }

  // 3. Selectors that gitbeaker ALSO sends in the request body literal
  //    (e.g. Runners.resetRegistrationToken sends `{ token, ...options }`).
  //    Those selectors stay in payload even when they're a selector.
  const bodyFieldVars = new Set<string>(
    pm.conditionalBodyFields.map((bf) => bf.variable),
  );

  const seenPy = new Set<string>();
  const SKIP_PROPS = new Set([
    "options", "show_expanded", "as_admin", "as_stream", "is_form",
  ]);

  type SigArg = { pyName: string; pyType: string };

  // 4. Path-only typed positional args: in EVERY branch's URL, never in body.
  const pathOnlyArgs: SigArg[] = [];
  // 5. Required typed body args: typed positionals NOT in any branch path,
  //    NOT selectors (selectors are handled separately).
  const requiredBody: BodyParam[] = [];

  if (typeInfo) {
    for (const pa of typeInfo.positionalArgs) {
      if (SKIP_PROPS.has(pa.pyName)) continue;
      if (selectorVars.includes(pa.name)) continue;
      if (seenPy.has(pa.pyName)) continue;
      seenPy.add(pa.pyName);
      if (allPathVars.has(pa.name)) {
        pathOnlyArgs.push({ pyName: pa.pyName, pyType: pa.pyType });
      } else {
        requiredBody.push({
          pyName: pa.pyName,
          wireName: toSnake(pa.name),
          pyType: pa.pyType,
        });
      }
    }
  }

  // 6. Selectors as typed-optional params. Type pulled from TS options
  //    properties when available, else `str | int`.
  type SelectorArg = SigArg & { jsName: string; alsoBody: boolean };
  const selectors: SelectorArg[] = [];
  for (const sv of selectorVars) {
    const py = toSnake(sv);
    if (seenPy.has(py)) continue;
    seenPy.add(py);
    let pyType = "str | int";
    if (typeInfo) {
      const prop = typeInfo.options.properties.find(
        (p) => p.pyName === py || p.name === py,
      );
      if (prop) {
        pyType = prop.nullable ? `${prop.pyType} | None` : prop.pyType;
      }
    }
    selectors.push({
      pyName: py,
      pyType,
      jsName: sv,
      alsoBody: bodyFieldVars.has(sv),
    });
  }

  // 7. Query vars (from `?key=${var}` in any branch) not already in the
  //    signature → add as typed-optional. Type pulled from TS options when
  //    available, else `str | int`.
  const queryOnlyArgs: SigArg[] = [];
  for (const bp of branchPaths) {
    for (const qv of bp.queryVars) {
      if (seenPy.has(qv.pyName)) continue;
      seenPy.add(qv.pyName);
      let pyType = "str | int";
      if (typeInfo) {
        const prop = typeInfo.options.properties.find(
          (p) => p.pyName === qv.pyName || p.name === qv.pyName,
        );
        if (prop) {
          pyType = prop.nullable ? `${prop.pyType} | None` : prop.pyType;
        }
      }
      queryOnlyArgs.push({ pyName: qv.pyName, pyType });
    }
  }

  const fnSnake = `${toSnake(pm.klass)}_${toSnake(pm.name)}`;
  const fnPascal = toPascal(fnSnake);
  const httpMethod = pm.verb === "del" ? "DELETE" : pm.verb.toUpperCase();
  const payloadKwarg =
    httpMethod === "GET" || httpMethod === "DELETE" ? "params" : "json";

  // Signature: path-only → required body → selectors → query-only → **options.
  const sigParts: string[] = [];
  for (const a of pathOnlyArgs) sigParts.push(`${a.pyName}: ${a.pyType}`);
  for (const b of requiredBody) sigParts.push(`${b.pyName}: ${b.pyType}`);
  for (const s of selectors) sigParts.push(`${s.pyName}: ${s.pyType} = _UNSET`);
  for (const a of queryOnlyArgs) sigParts.push(`${a.pyName}: ${a.pyType} = _UNSET`);
  sigParts.push("**options");

  const docBranches = pm.conditionalBranches
    .map((b) => (b.selectorVar ? `if ${toSnake(b.selectorVar)}: ` : "else: ") + b.pathTpl + pm.conditionalSuffix)
    .join("; ");
  const lines: string[] = [];
  lines.push(`def ${fnSnake}(${sigParts.join(", ")}):`);
  lines.push(
    `    """${pm.klass}.${pm.name} (${httpMethod}; selector-driven path: ${docBranches})."""`,
  );

  // Payload: **options first; explicit typed body fields next; selectors
  // that ALSO appear in gitbeaker's body literal go in payload when set.
  lines.push(`    payload = {**options}`);
  for (const b of requiredBody) {
    lines.push(`    payload[${JSON.stringify(b.wireName)}] = ${b.pyName}`);
  }
  for (const s of selectors) {
    if (!s.alsoBody) continue;
    lines.push(`    if ${s.pyName} is not _UNSET:`);
    lines.push(`        payload[${JSON.stringify(toSnake(s.jsName))}] = ${s.pyName}`);
  }

  // Branch predicates mirror JS truthiness (`if (owned)`), not "was provided"
  // — `RunnersAll(owned=False)` must fall through to /runners/all the same
  // way JS `else if (owned)` does. _UNSET defines __bool__ False, so omitted
  // selectors also fall through. Body inclusion (above) is separate and
  // keeps `is not _UNSET` semantics.
  const fallback = branchPaths.find((bp) => bp.selectorVar === null);
  const emitBranchPayload = (bp: typeof branchPaths[number]): string[] => {
    const out: string[] = [];
    for (const qv of bp.queryVars) {
      out.push(`        if ${qv.pyName} is not _UNSET:`);
      out.push(`            payload[${JSON.stringify(qv.wire)}] = ${qv.pyName}`);
    }
    return out;
  };
  for (const bp of branchPaths) {
    if (bp.selectorVar === null) continue;
    const py = toSnake(bp.selectorVar);
    lines.push(`    if ${py}:`);
    for (const l of emitBranchPayload(bp)) lines.push(l);
    lines.push(
      `        return _ok(_get_client().request("${httpMethod}", f"${bp.pyPath}", ${payloadKwarg}=payload))`,
    );
  }
  if (fallback) {
    for (const qv of fallback.queryVars) {
      lines.push(`    if ${qv.pyName} is not _UNSET:`);
      lines.push(`        payload[${JSON.stringify(qv.wire)}] = ${qv.pyName}`);
    }
    lines.push(
      `    return _ok(_get_client().request("${httpMethod}", f"${fallback.pyPath}", ${payloadKwarg}=payload))`,
    );
  } else {
    const required = selectors.map((s) => s.pyName).join(" or ");
    lines.push(
      `    raise ValueError("${pm.klass}.${pm.name} requires one of: ${required}")`,
    );
  }

  return {
    snakeName: fnSnake,
    pascalName: fnPascal,
    klass: pm.klass,
    verb: pm.verb,
    pyLines: lines,
  };
}

function emitFn(pm: ParsedMethod): Emitted | null {
  const argsInPath = new Set<string>();
  const pathStr = pyPath(pm.pathTpl, argsInPath);

  // Path params come from positionalArgs that appear in the URL template.
  const pathArgs = pm.positionalArgs
    .filter((a) => argsInPath.has(a))
    .map(toSnake);

  // Try to resolve TS types for this method. Resource* base classes don't
  // appear under their concrete subclass names in the .d.ts, so for expanded
  // methods we look up the parsed klass name (e.g. ProjectLabels) directly —
  // when missing, we fall back gracefully.
  const typeInfo = resolveMethod(checker, tsSource, pm.klass, pm.name);

  // Conditional dispatch: gitbeaker picks URL based on which selector option
  // is set (e.g. DeployKeys.all → /projects/{id}/deploy_keys or
  // /users/{id}/project_deploy_keys or /deploy_keys). Emit a Python chain
  // that routes to the right path so callers can use the selector safely.
  if (pm.conditionalBranches) {
    const em = emitConditionalDispatch(pm, typeInfo);
    if (em) {
      stats.methodsConditionalPath++;
      return em;
    }
    // If emission failed (very rare — unsafe template after substitution),
    // fall through to the legacy `**options`-only path below.
  }

  const requiredBody: BodyParam[] = [];
  const optionalBody: BodyParam[] = [];
  const seenPy = new Set<string>(pathArgs);
  let useVarKwargs = true;
  let resolvedTyped = false;

  // Belt-and-suspenders skip list (snake-cased). Matches the gitbeaker-only
  // filter in typeResolver — `sudo` deliberately NOT here: it's a real
  // GitLab API param that we keep as a typed optional.
  const SKIP_PROPS = new Set(["options", "show_expanded", "as_admin", "as_stream", "is_form"]);

  if (typeInfo && typeInfo.options.resolved) {
    resolvedTyped = true;
    // Non-path positionals get their TS types. Path positionals stay
    // `str | int` (handled below in signature build).
    for (const pa of typeInfo.positionalArgs) {
      if (argsInPath.has(pa.name)) continue;
      if (SKIP_PROPS.has(pa.pyName)) continue;
      if (seenPy.has(pa.pyName)) continue;
      seenPy.add(pa.pyName);
      requiredBody.push({
        pyName: pa.pyName,
        wireName: toSnake(pa.name),
        pyType: pa.pyType,
      });
    }
    // Options properties — skipped entirely for conditional-path methods
    // (we can't distinguish path-selector options from legit body fields).
    if (!pm.conditionalPath) {
      for (const p of typeInfo.options.properties) {
        if (SKIP_PROPS.has(p.pyName)) continue;
        if (seenPy.has(p.pyName)) continue;
        seenPy.add(p.pyName);
        const pyType = p.nullable ? `${p.pyType} | None` : p.pyType;
        if (p.optional) {
          optionalBody.push({ pyName: p.pyName, wireName: p.name, pyType });
        } else {
          requiredBody.push({ pyName: p.pyName, wireName: p.name, pyType });
        }
      }
    }
    if (pm.conditionalPath) {
      stats.methodsConditionalPath++;
      useVarKwargs = true; // always keep **options for selector-driven methods
    } else {
      useVarKwargs = typeInfo.options.hasIndexSignature;
      if (useVarKwargs) {
        stats.methodsKeptOptions++;
      } else {
        stats.methodsTyped++;
      }
    }
  } else {
    // Legacy fallback: JS-parsed bodyFields as required `str | int` body params.
    if (pm.conditionalPath) {
      stats.methodsConditionalPath++;
    } else {
      stats.methodsTypeFallback++;
    }
    for (const bf of pm.bodyFields) {
      const wireName = toSnake(bf.name);
      let py = wireName;
      if (PY_KEYWORDS.has(py)) py = py + "_";
      if (SKIP_PROPS.has(py)) continue;
      if (seenPy.has(py)) continue;
      seenPy.add(py);
      requiredBody.push({ pyName: py, wireName, pyType: "str | int" });
    }
    useVarKwargs = true;
  }

  const snakeClass = toSnake(pm.klass);
  const snakeMethod = toSnake(pm.name);
  const fnSnake = `${snakeClass}_${snakeMethod}`;
  const fnPascal = toPascal(fnSnake);

  const httpMethod = pm.verb === "del" ? "DELETE" : pm.verb.toUpperCase();
  const payloadKwarg =
    httpMethod === "GET" || httpMethod === "DELETE" ? "params" : "json";

  // 4-tier signature: path → required body → optional (= _UNSET) → **options
  const sigParts: string[] = [];
  for (const a of pathArgs) sigParts.push(`${a}: str | int`);
  for (const b of requiredBody) sigParts.push(`${b.pyName}: ${b.pyType}`);
  for (const b of optionalBody) sigParts.push(`${b.pyName}: ${b.pyType} = _UNSET`);
  if (useVarKwargs) sigParts.push("**options");
  const sig = `def ${fnSnake}(${sigParts.join(", ")}):`;

  const lines: string[] = [];
  lines.push(sig);
  const docPath = pm.pathTpl;
  const allBody = [...requiredBody, ...optionalBody];
  if (allBody.length > 0) {
    const fieldList = allBody.map((b) => b.wireName).join(", ");
    lines.push(
      `    """${pm.klass}.${pm.name} (${httpMethod} ${docPath}). Body fields: ${fieldList}."""`
    );
  } else {
    lines.push(`    """${pm.klass}.${pm.name} (${httpMethod} ${docPath})."""`);
  }

  if (allBody.length === 0) {
    if (useVarKwargs) {
      lines.push(
        `    return _ok(_get_client().request("${httpMethod}", f"${pathStr}", ${payloadKwarg}=options))`
      );
    } else {
      lines.push(
        `    return _ok(_get_client().request("${httpMethod}", f"${pathStr}"))`
      );
    }
  } else {
    // Spread **options FIRST so explicitly typed params can't be silently
    // shadowed by an extra (matters for keyword-collision renames like
    // from_ → wire "from"). For closed types we start from an empty dict.
    if (useVarKwargs) {
      lines.push(`    payload = {**options}`);
    } else {
      lines.push(`    payload: dict = {}`);
    }
    for (const b of requiredBody) {
      lines.push(`    payload[${JSON.stringify(b.wireName)}] = ${b.pyName}`);
    }
    for (const b of optionalBody) {
      lines.push(`    if ${b.pyName} is not _UNSET:`);
      lines.push(`        payload[${JSON.stringify(b.wireName)}] = ${b.pyName}`);
    }
    lines.push(
      `    return _ok(_get_client().request("${httpMethod}", f"${pathStr}", ${payloadKwarg}=payload))`
    );
  }

  return {
    snakeName: fnSnake,
    pascalName: fnPascal,
    klass: pm.klass,
    verb: pm.verb,
    pyLines: lines,
  };
}

// ── Emission pipeline ─────────────────────────────────────────────────────

const emitted: Emitted[] = [];
const seenNames = new Set<string>();
const emittedByClassMethod = new Map<string, Emitted>();

for (const pm of methods) {
  const em = emitFn(pm);
  if (!em) continue;
  // Deduplicate by snake name (some classes may share a method name after
  // snake-casing; keep the first).
  if (seenNames.has(em.snakeName)) continue;
  seenNames.add(em.snakeName);
  emitted.push(em);
  emittedByClassMethod.set(`${pm.klass}.${pm.name}`, em);
}

// ── Resolve delegations ───────────────────────────────────────────────────

let aliasesEmitted = 0;
for (const d of delegations) {
  const targetKey = `${d.klass}.${d.target}`;
  const targetEm = emittedByClassMethod.get(targetKey);
  if (!targetEm) continue; // target was skipped too; give up on the alias

  const snakeClass = toSnake(d.klass);
  const snakeMethod = toSnake(d.name);
  const aliasSnake = `${snakeClass}_${snakeMethod}`;
  if (seenNames.has(aliasSnake)) continue;

  const aliasPascal = toPascal(aliasSnake);
  const pyArgs = d.args
    .filter((a) => /^\w+$/.test(a))
    .map(toSnake);
  const pyParams = [
    ...pyArgs.filter((a) => a !== "options").map((a) => `${a}`),
    "**options",
  ];
  const passArgs = [
    ...pyArgs.filter((a) => a !== "options").map((a) => `${a}`),
    "**options",
  ];

  const lines: string[] = [];
  lines.push(`def ${aliasSnake}(${pyParams.join(", ")}):`);
  lines.push(
    `    """${d.klass}.${d.name} (alias for ${d.klass}.${d.target})."""`
  );
  lines.push(`    return ${targetEm.snakeName}(${passArgs.join(", ")})`);

  emitted.push({
    snakeName: aliasSnake,
    pascalName: aliasPascal,
    klass: d.klass,
    verb: targetEm.verb,
    pyLines: lines,
  });
  seenNames.add(aliasSnake);
  aliasesEmitted++;
}

// Sort by class then name for readable output
emitted.sort((a, b) => {
  if (a.klass !== b.klass) return a.klass.localeCompare(b.klass);
  return a.snakeName.localeCompare(b.snakeName);
});

// ── Build _generated.py ───────────────────────────────────────────────────

const pyLines: string[] = [
  "# GENERATED by codegen/generate.ts — DO NOT EDIT",
  "from __future__ import annotations",
  "",
  "from typing import Any, Literal",
  "from urllib.parse import quote as _q",
  "",
  "from .client import get_client as _get_client",
  "from .registry import _UNSET",
  "",
  "",
  "def _enc(v) -> str:",
  '    """URL-encode a path segment. Slashes become %2F so path-style',
  '    project IDs ("group/project") survive.',
  '    """',
  '    return _q(str(v), safe="")',
  "",
  "",
  "def _ok(data):",
  '    """Replace None (e.g. 204 No Content) with a minimal success marker."""',
  "    if data is None:",
  '        return {"status": "ok"}',
  "    return data",
  "",
  "",
];

let currentClass = "";
for (const em of emitted) {
  if (em.klass !== currentClass) {
    if (currentClass) pyLines.push("");
    pyLines.push(
      `# ── ${em.klass} ${"─".repeat(Math.max(1, 70 - em.klass.length))}`
    );
    pyLines.push("");
    currentClass = em.klass;
  }
  for (const line of em.pyLines) pyLines.push(line);
  pyLines.push("");
  pyLines.push("");
}

const pyOut = pyLines.join("\n").replace(/\n{3,}/g, "\n\n\n").trimEnd() + "\n";

// ── Build _generated_groups.py ────────────────────────────────────────────

function defaultGroupFor(verb: string): string {
  switch (verb) {
    case "get":
      return "gitlab_read";
    case "post":
    case "put":
    case "patch":
      return "gitlab_write";
    case "del":
      return "gitlab_delete";
    default:
      return "gitlab_read";
  }
}

const groupMap: Record<string, string[]> = {
  gitlab_read: [],
  gitlab_write: [],
  gitlab_delete: [],
};
for (const em of emitted) {
  const g = defaultGroupFor(em.verb);
  groupMap[g].push(em.pascalName);
}

const groupLines: string[] = [
  "# GENERATED by codegen/generate.ts — DO NOT EDIT",
  "# Maps each generated op (PascalCase) to its default group.",
  "# tools.py merges this with manual overrides.",
  "from __future__ import annotations",
  "",
  "DEFAULT_GROUPS: dict[str, list[str]] = {",
];
for (const [g, ops] of Object.entries(groupMap)) {
  ops.sort();
  groupLines.push(`    "${g}": [`);
  for (const op of ops) groupLines.push(`        "${op}",`);
  groupLines.push("    ],");
}
groupLines.push("}");

const groupOut = groupLines.join("\n") + "\n";

// ── Write or --check ───────────────────────────────────────────────────────

function diffCheck(path: string, expected: string): boolean {
  let actual = "";
  try {
    actual = readFileSync(path, "utf-8");
  } catch {
    return false;
  }
  return actual === expected;
}

// Sanity check: the emitted Python must parse. Catches missing imports,
// duplicate defaults-after-non-defaults, etc. before we ship the file.
function astParseCheck(pySource: string): void {
  try {
    execSync(
      `python3 -c "import ast, sys; ast.parse(sys.stdin.read())"`,
      { input: pySource, stdio: ["pipe", "pipe", "inherit"] },
    );
  } catch (e) {
    console.error("AST parse FAILED on generated source:");
    throw e;
  }
}
astParseCheck(pyOut);

if (CHECK_MODE) {
  const okPy = diffCheck(OUT_PY, pyOut);
  const okGroups = diffCheck(OUT_GROUPS, groupOut);
  if (!okPy || !okGroups) {
    console.error("DRIFT: generated files differ from committed versions.");
    if (!okPy) console.error(`  ${OUT_PY} is stale`);
    if (!okGroups) console.error(`  ${OUT_GROUPS} is stale`);
    console.error("Run `npx tsx generate.ts` and commit the result.");
    process.exit(1);
  }
  console.log("Drift check OK.");
} else {
  writeFileSync(OUT_PY, pyOut);
  writeFileSync(OUT_GROUPS, groupOut);
  console.log(`Wrote ${OUT_PY}`);
  console.log(`Wrote ${OUT_GROUPS}`);
}

// ── Summary ────────────────────────────────────────────────────────────────

console.log("─".repeat(60));
console.log(`Classes found:     ${stats.classesFound}`);
console.log(`Classes excluded:  ${stats.classesExcluded} (EE/packages/mixins)`);
console.log(`Methods total:     ${stats.methodsTotal}`);
console.log(`Methods parsed:    ${stats.methodsParsed}`);
console.log(`  (of which ${delegations.length} are delegations, ${aliasesEmitted} emitted as aliases)`);
console.log(`  (of which ${expansionCount} are expanded from Resource* base classes)`);
console.log(`Methods skipped:   ${stats.methodsSkipped} (complex impl)`);
console.log(`Emitted ops:       ${emitted.length}`);
console.log(
  `  gitlab_read:    ${groupMap.gitlab_read.length}\n` +
    `  gitlab_write:   ${groupMap.gitlab_write.length}\n` +
    `  gitlab_delete:  ${groupMap.gitlab_delete.length}`
);
console.log("─".repeat(60));
console.log(`Methods with typed body params:  ${stats.methodsTyped}`);
console.log(`Methods kept **options (index sig): ${stats.methodsKeptOptions}`);
console.log(`Type-resolution fallback:        ${stats.methodsTypeFallback}`);
console.log(`Conditional-path dispatch:       ${stats.methodsConditionalPath}`);

if (process.argv.includes("--show-skipped") && skippedMethods.length > 0) {
  console.log("─".repeat(60));
  console.log("Skipped methods (grouped by class):");
  const byClass: Record<string, string[]> = {};
  for (const sm of skippedMethods) {
    (byClass[sm.klass] ??= []).push(`${sm.name}(${sm.argsRaw})`);
  }
  for (const [k, ms] of Object.entries(byClass).sort()) {
    console.log(`  ${k}:`);
    for (const m of ms) console.log(`    ${m}`);
  }
}
