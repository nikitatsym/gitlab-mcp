/**
 * OpenAPI v3 resolver: given a (verb, gitbeaker-pathTemplate), look up the
 * matching GitLab OpenAPI operation and return a typed parameter list.
 *
 * Used by `generate.ts` as the primary source of truth for body schemas and
 * query/path parameters. gitbeaker TypeScript types are a secondary source
 * (filled in for fields OpenAPI doesn't describe), and `manual_ops.ts`
 * supplements both. NO `**options` fallback at any layer — every parameter
 * is explicitly declared with its mapped Python type.
 *
 * The lookup key is the gitbeaker path template normalized to OpenAPI form:
 *   gitbeaker: "projects/${projectId}/repository/branches"
 *   OpenAPI:   "/api/v4/projects/{id}/repository/branches"
 *   key:       "PUT /projects/{*}/repository/branches"
 * Every {var} placeholder collapses to {*} since the variable names differ
 * between sources (gitbeaker camelCase vs OpenAPI snake_case / `{id}`).
 */
import { readFileSync } from "fs";
import { parse as yamlParse } from "yaml";

type JSONObj = Record<string, any>;

export interface OpenApiParam {
  name: string;          // wire name (snake_case)
  pyName: string;        // python identifier (snake_case, suffixed _ for keywords)
  pyType: string;        // mapped Python type, WITHOUT optional/nullable decoration
  required: boolean;     // false → default _UNSET
  nullable: boolean;     // → append " | None" in type decoration at emit time
  location: "path" | "query" | "body";
}

export interface OpenApiOp {
  rawPath: string;       // e.g. "/api/v4/projects/{id}"
  verb: string;          // lowercase
  params: OpenApiParam[];
}

export interface OpenApiLookup {
  byKey: Map<string, OpenApiOp>; // key = `${VERB} ${normalized_path}`
  totalOps: number;
  pinTag: string;                // best-effort version tag for diagnostics
}

const PY_KEYWORDS = new Set([
  "False", "None", "True", "and", "as", "assert", "async", "await",
  "break", "class", "continue", "def", "del", "elif", "else", "except",
  "finally", "for", "from", "global", "if", "import", "in", "is",
  "lambda", "nonlocal", "not", "or", "pass", "raise", "return", "try",
  "while", "with", "yield",
]);

/**
 * OpenAPI parameter names can contain characters that aren't valid Python
 * identifiers — GitLab uses `not[author_id]`, `or[labels]`, etc. for nested
 * filters. Sanitize to a Python-safe form for the kwarg name; the original
 * wire key is preserved separately in OpenApiParam.name so codegen emits the
 * correct payload key.
 */
function pyName(wire: string): string {
  // Collapse any non-identifier run into a single underscore. Covers GitLab
  // bracket filters (`not[author_id]`), dotted nested fields (`file.path`),
  // dashes, slashes, anything else.
  let s = wire.replace(/[^A-Za-z0-9_]+/g, "_");
  s = s.replace(/^_+/, "").replace(/_+$/, "");
  if (!s) s = "_";
  if (!/^[A-Za-z_]/.test(s)) s = "_" + s;
  return PY_KEYWORDS.has(s) ? s + "_" : s;
}

/**
 * Normalize a path template (either gitbeaker or OpenAPI form) into a shared
 * lookup key. Every `${var}` or `{var}` becomes `{*}`; the `/api/v4` prefix
 * is stripped; leading slash is preserved; trailing slash removed.
 */
export function normalizePath(pathTpl: string): string {
  let p = pathTpl.replace(/\$\{[^}]+\}/g, "{*}").replace(/\{[^}]+\}/g, "{*}");
  p = p.replace(/^\/api\/v4\//, "/");
  if (!p.startsWith("/")) p = "/" + p;
  if (p.length > 1 && p.endsWith("/")) p = p.slice(0, -1);
  return p;
}

export function lookupKey(verb: string, pathTpl: string): string {
  const v = (verb === "del" ? "delete" : verb).toUpperCase();
  return `${v} ${normalizePath(pathTpl)}`;
}

// ── Schema → Python type mapping ──────────────────────────────────────────

interface MappedType {
  pyType: string;
  nullable: boolean;
}

/**
 * Map a JSON Schema fragment to a Python type. Resolves `$ref` recursively
 * against `components/schemas`. Returns "Any" for shapes we don't recognize
 * (better to be permissive than to drop the parameter — the resolver's
 * mandate is "every param declared", not "perfectly typed").
 */
function mapSchema(schema: JSONObj | undefined | null, components: JSONObj, depth = 0): MappedType {
  if (!schema || depth > 8) return { pyType: "Any", nullable: false };

  if (typeof schema.$ref === "string") {
    const m = schema.$ref.match(/^#\/components\/schemas\/(.+)$/);
    if (m) {
      const target = components[m[1]];
      if (target) return mapSchema(target, components, depth + 1);
    }
    return { pyType: "Any", nullable: false };
  }

  const nullable = schema.nullable === true;

  if (Array.isArray(schema.enum) && schema.enum.length > 0) {
    const allStrings = schema.enum.every((e: unknown) => typeof e === "string");
    const allNumbers = schema.enum.every((e: unknown) => typeof e === "number");
    if (allStrings) {
      const vals = [...new Set(schema.enum as string[])]
        .map((v) => `"${v.replace(/"/g, '\\"')}"`)
        .join(", ");
      return { pyType: `Literal[${vals}]`, nullable };
    }
    if (allNumbers) {
      const vals = [...new Set(schema.enum as number[])].join(", ");
      return { pyType: `Literal[${vals}]`, nullable };
    }
    // fall through to type-based mapping
  }

  // oneOf / anyOf → union
  for (const k of ["oneOf", "anyOf"] as const) {
    if (Array.isArray(schema[k]) && schema[k].length > 0) {
      const parts = (schema[k] as JSONObj[]).map((s) => mapSchema(s, components, depth + 1));
      const types = [...new Set(parts.map((p) => p.pyType))].filter((t) => t !== "None");
      const anyNullable = parts.some((p) => p.nullable);
      if (types.length === 0) return { pyType: "Any", nullable: anyNullable || nullable };
      if (types.length === 1) return { pyType: types[0], nullable: anyNullable || nullable };
      return { pyType: types.join(" | "), nullable: anyNullable || nullable };
    }
  }

  // allOf → take the first object-like member that has type info
  if (Array.isArray(schema.allOf) && schema.allOf.length > 0) {
    for (const sub of schema.allOf) {
      const r = mapSchema(sub, components, depth + 1);
      if (r.pyType !== "Any") return { pyType: r.pyType, nullable: nullable || r.nullable };
    }
  }

  switch (schema.type) {
    case "string":
      return { pyType: "str", nullable };
    case "integer":
      return { pyType: "int", nullable };
    case "number":
      return { pyType: "float | int", nullable };
    case "boolean":
      return { pyType: "bool", nullable };
    case "array": {
      const inner = mapSchema(schema.items, components, depth + 1);
      const t = inner.nullable ? `${inner.pyType} | None` : inner.pyType;
      return { pyType: `list[${t}]`, nullable };
    }
    case "object":
      return { pyType: "dict", nullable };
  }

  return { pyType: "Any", nullable };
}

// ── Body schema → property list ───────────────────────────────────────────

interface FlatProp {
  name: string;
  type: MappedType;
  required: boolean;
}

/**
 * Walk a body schema (possibly $ref'd) and produce a flat list of
 * (name, type, required) tuples. Handles `properties + required` at top
 * level and recurses one level into `allOf` for combined schemas.
 */
function flattenBodySchema(schema: JSONObj, components: JSONObj, depth = 0): FlatProp[] {
  const out: FlatProp[] = [];
  if (!schema || depth > 4) return out;

  let resolved: JSONObj = schema;
  if (typeof schema.$ref === "string") {
    const m = schema.$ref.match(/^#\/components\/schemas\/(.+)$/);
    if (m && components[m[1]]) {
      resolved = components[m[1]];
    } else {
      return out;
    }
  }

  const requiredList = new Set<string>(
    Array.isArray(resolved.required) ? resolved.required : [],
  );

  if (resolved.properties && typeof resolved.properties === "object") {
    for (const [name, propSchema] of Object.entries(resolved.properties)) {
      const t = mapSchema(propSchema as JSONObj, components);
      out.push({ name, type: t, required: requiredList.has(name) });
    }
  }

  for (const k of ["allOf"] as const) {
    if (Array.isArray(resolved[k])) {
      for (const sub of resolved[k] as JSONObj[]) {
        for (const p of flattenBodySchema(sub, components, depth + 1)) {
          out.push(p);
        }
      }
    }
  }

  return out;
}

// ── Top-level: parse spec, build lookup ──────────────────────────────────

export function loadOpenApi(path: string, pinTag = "(unset)"): OpenApiLookup {
  const text = readFileSync(path, "utf-8");
  const spec = yamlParse(text) as JSONObj;
  const components = (spec.components?.schemas ?? {}) as JSONObj;

  const byKey = new Map<string, OpenApiOp>();
  const paths = (spec.paths ?? {}) as JSONObj;
  let totalOps = 0;

  for (const [rawPath, pathItem] of Object.entries(paths)) {
    if (!pathItem || typeof pathItem !== "object") continue;
    for (const verb of ["get", "post", "put", "patch", "delete"]) {
      const op = (pathItem as JSONObj)[verb];
      if (!op) continue;
      totalOps++;

      const params: OpenApiParam[] = [];
      const seen = new Set<string>();

      // Parameters: path / query (header is rare for GitLab; skip)
      const opParams = Array.isArray(op.parameters) ? op.parameters : [];
      for (const p of opParams) {
        if (!p || typeof p !== "object") continue;
        if (typeof p.name !== "string") continue;
        const where = p.in;
        if (where !== "path" && where !== "query") continue;
        if (seen.has(p.name)) continue;
        seen.add(p.name);
        const t = mapSchema(p.schema as JSONObj, components);
        params.push({
          name: p.name,
          pyName: pyName(p.name),
          pyType: t.pyType,
          required: p.required === true,
          nullable: t.nullable,
          location: where,
        });
      }

      // requestBody: prefer application/json, then multipart/form-data
      const rb = op.requestBody as JSONObj | undefined;
      if (rb && rb.content && typeof rb.content === "object") {
        const ct = rb.content as JSONObj;
        const media = ct["application/json"] ?? ct["multipart/form-data"] ?? Object.values(ct)[0];
        if (media && typeof media === "object" && media.schema) {
          for (const f of flattenBodySchema(media.schema as JSONObj, components)) {
            if (seen.has(f.name)) continue;
            seen.add(f.name);
            params.push({
              name: f.name,
              pyName: pyName(f.name),
              pyType: f.type.pyType,
              required: f.required,
              nullable: f.type.nullable,
              location: "body",
            });
          }
        }
      }

      byKey.set(lookupKey(verb, rawPath), { rawPath, verb, params });
    }
  }

  return { byKey, totalOps, pinTag };
}

export function resolveOpenApi(
  lookup: OpenApiLookup,
  verb: string,
  pathTpl: string,
): OpenApiOp | null {
  return lookup.byKey.get(lookupKey(verb, pathTpl)) ?? null;
}
