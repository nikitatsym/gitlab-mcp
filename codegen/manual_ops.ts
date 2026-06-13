/**
 * Hand-written supplements to codegen output.
 *
 * Codegen pipeline order, per emitted op:
 *   1. OpenAPI v3 (primary source — typed body/query/path schemas)
 *   2. gitbeaker TypeScript types (secondary — fills overflow for fields
 *      OpenAPI doesn't describe; sudo, vendor-specific extras)
 *   3. MANUAL_PARAMS[OpName]  (this file — wins on conflict)
 *
 * No `**options` anywhere. Every parameter is explicitly declared with a
 * mapped Python type. If neither OpenAPI, gitbeaker TS, nor this file
 * describes an op, codegen skips it and lists it under `needs_manual` in
 * the run summary so it's an explicit decision, not silent loss.
 *
 * MANUAL_OPS declares ops that gitbeaker doesn't expose at all — the small
 * tail that exists only in OpenAPI or in vendor docs.
 */

export interface ManualParam {
  name: string;            // wire name (snake_case)
  pyType: string;          // mapped Python type, no optional/nullable decoration
  required?: boolean;      // default false (optional, default _UNSET)
  nullable?: boolean;      // default false (no `| None`)
  location?: "path" | "query" | "body"; // default body
}

export interface ManualOp {
  klass: string;           // grouping for output (PascalCase)
  method: string;          // camelCase
  verb: "get" | "post" | "put" | "patch" | "delete";
  path: string;            // OpenAPI-style template, e.g. "projects/{id}/foo"
  params: ManualParam[];
  group?: "gitlab_read" | "gitlab_write" | "gitlab_delete";
}

/**
 * Per-op additions. Keyed by PascalCase op name (klass + method joined),
 * matching the `pascalName` codegen emits. Entries here are MERGED into
 * the codegen-derived signature; on field-name conflict, manual wins.
 *
 * Use when a real-world GitLab/Heptapod field is on the wire but missing
 * from both OpenAPI and gitbeaker TS at the current pin.
 */
export const MANUAL_PARAMS: Record<string, ManualParam[]> = {
  // Heptapod-specific project create field: lets callers create hg / hg_git
  // projects, not just git. The gitbeaker TS types don't know about it
  // (Heptapod is a fork) and GitLab's OpenAPI doesn't describe it either.
  ProjectsCreate: [
    { name: "vcs_type", pyType: `Literal["git", "hg", "hg_git"]` },
  ],
};

/**
 * Whole ops declared by hand. Codegen emits these alongside the gitbeaker-
 * derived ops. The `needs_manual` tail from the probe (currently 2 ops)
 * lives here.
 */
export const MANUAL_OPS: ManualOp[] = [];

/**
 * Ops to drop from emission entirely. Empty by default — codegen prefers
 * emitting whatever typed surface it can build.
 */
export const MANUAL_SKIP: Set<string> = new Set();
