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
import { join, dirname } from "path";
import { fileURLToPath } from "url";
import { EE_EXCLUDED_CLASSES } from "./ee_exclusions.ts";

const __dirname = dirname(fileURLToPath(import.meta.url));
const IMPL_PATH = join(
  __dirname,
  "node_modules/@gitbeaker/core/dist/index.js"
);
const OUT_PY = join(__dirname, "../src/gitlab_mcp/_generated.py");
const OUT_GROUPS = join(__dirname, "../src/gitlab_mcp/_generated_groups.py");

const CHECK_MODE = process.argv.includes("--check");

const IMPL = readFileSync(IMPL_PATH, "utf-8");

// ── Parsing ────────────────────────────────────────────────────────────────

interface ParsedMethod {
  klass: string;
  name: string;
  positionalArgs: string[]; // arg names in order, last may be 'options'
  verb: string; // get/post/put/patch/del
  pathTpl: string; // raw template, e.g. "projects/${projectId}/foo"
  isDestructured: boolean; // true if args started with `{` (destructured options)
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
const stats = {
  classesFound: 0,
  classesExcluded: 0,
  methodsTotal: 0,
  methodsParsed: 0,
  methodsSkipped: 0,
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
    const pEndpoint = mBody.match(
      /return RequestHelper\.(\w+)\(\)\(\s*this,\s*endpoint`([^`]+)`/
    );
    if (pEndpoint && isSafeTemplate(pEndpoint[2])) {
      verb = pEndpoint[1];
      pathTpl = pEndpoint[2];
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

    methods.push({
      klass,
      name,
      positionalArgs,
      verb,
      pathTpl,
      isDestructured,
    });
    stats.methodsParsed++;
  }
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

// ── Path template → Python f-string ───────────────────────────────────────

function pyPath(tpl: string, argsInPath: Set<string>): string {
  // Replace ${arg} with {_enc(arg_snake)}, tracking which args are path vars.
  const out = tpl.replace(/\$\{(\w+)\}/g, (_, arg) => {
    argsInPath.add(arg);
    return `{_enc(${toSnake(arg)})}`;
  });
  return "/" + out.replace(/^\/+/, "");
}

// ── Generate Python wrapper function ──────────────────────────────────────

interface Emitted {
  snakeName: string;
  pascalName: string;
  klass: string;
  verb: string; // lowercase
  pyLines: string[];
}

function emitFn(pm: ParsedMethod): Emitted | null {
  const argsInPath = new Set<string>();
  const pathStr = pyPath(pm.pathTpl, argsInPath);

  // Positional Python params = path vars (in the order they appear in the original args).
  const pathArgs = pm.positionalArgs
    .filter((a) => argsInPath.has(a))
    .map(toSnake);

  // If positional args reference vars that aren't in pathArgs (because the
  // method has non-path positional args like `branchName, ref`), we lose
  // their names. They become **options body fields instead.

  const snakeClass = toSnake(pm.klass);
  const snakeMethod = toSnake(pm.name);
  const fnSnake = `${snakeClass}_${snakeMethod}`;
  const fnPascal = toPascal(fnSnake);

  const httpMethod =
    pm.verb === "del" ? "DELETE" : pm.verb.toUpperCase();

  const bodyArg =
    httpMethod === "GET" || httpMethod === "DELETE"
      ? "params=options"
      : "json=options";

  const sigParams = [...pathArgs.map((a) => `${a}: str | int`), "**options"];
  const sig = `def ${fnSnake}(${sigParams.join(", ")}):`;

  const lines: string[] = [];
  lines.push(sig);
  lines.push(`    """${pm.klass}.${pm.name} (${httpMethod} ${pm.pathTpl})."""`);
  lines.push(
    `    return _ok(_get_client().request("${httpMethod}", f"${pathStr}", ${bodyArg}))`
  );

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
  "from urllib.parse import quote as _q",
  "",
  "from .client import get_client as _get_client",
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
