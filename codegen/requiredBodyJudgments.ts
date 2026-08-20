/**
 * Exact, evidence-backed judgments consumed by codegen and its independent
 * required-body conformance gate. OpenAPI supplies the default wire names,
 * requiredness, types, and nullability.
 *
 * BODY_FIELD_OVERRIDES correct an under-marked required field or map a
 * Gitbeaker positional to its canonical body wire key. GITBEAKER_SOURCE_WIRE_NAME_JUDGMENTS
 * cover the exact pinned implementation mappings absent from OpenAPI. allowNull
 * is permitted only where evidence proves a required field can be null.
 * DOCUMENTED_SPEC_GAPS identify a spec-required field the pinned Gitbeaker JSON
 * contract cannot represent. CONCRETE_DEFAULT_OVERRIDES identify a hand-written
 * wrapper that always supplies a documented concrete default.
 * CONDITIONAL_BRANCH_FIELD_JUDGMENTS identify an optional field that belongs
 * only to one selector path.
 *
 * Every entry must be exact by operation, verb, raw path, and property or source
 * parameter and have direct source evidence. Entries that document a semantic
 * divergence carry a rationale. The gate stale-checks every list so a judgment
 * cannot outlive its source condition.
 */
export interface BodyFieldOverride {
  operation: string;
  verb: string;
  rawPath: string;
  property: string;
  /** Python argument name when the body wire key collides with another argument. */
  pyName?: string;
  /** Gitbeaker positional argument that supplies this body property. */
  sourceParameter?: string;
  /** An evidence-backed exception to the default non-null required-body contract. */
  allowNull?: true;
}

/**
 * Exact GitBeaker implementation mapping for a positional whose wire key is
 * absent from the corresponding OpenAPI request body schema.
 */
export interface GitbeakerSourceWireNameJudgment {
  operation: string;
  verb: string;
  rawPath: string;
  /** Canonical positional declared in GitBeaker's TypeScript surface. */
  sourceParameter: string;
  /** Canonical snake_case HTTP key emitted by this generator. */
  wireName: string;
  /** Property spelling in the pinned GitBeaker implementation literal. */
  sourceWireName: string;
  /** Bundled implementation variable assigned to sourceWireName. */
  sourceVariable: string;
  rationale: string;
}

export interface ConditionalBranchFieldJudgment {
  operation: string;
  verb: string;
  rawPath: string;
  property: string;
  /** Gitbeaker selector that routes requests to this exact OpenAPI branch. */
  selectorParameter: string;
  rationale: string;
}

export interface ConcreteDefaultOverride {
  operation: string;
  verb: string;
  rawPath: string;
  functionName: string;
  property: string;
}

export interface DocumentedSpecGap {
  operation: string;
  verb: string;
  rawPath: string;
  property: string;
  rationale: string;
}

export const BODY_FIELD_OVERRIDES: readonly BodyFieldOverride[] = [
  { operation: "BroadcastMessages.create", verb: "POST", rawPath: "/api/v4/broadcast_messages", property: "message" },
  { operation: "Deployments.create", verb: "POST", rawPath: "/api/v4/projects/{id}/deployments", property: "status" },
  { operation: "GroupImportExports.import", verb: "POST", rawPath: "/api/v4/groups/import", property: "name" },
  { operation: "GroupMemberRoles.add", verb: "POST", rawPath: "/api/v4/groups/{id}/members", property: "access_level" },
  { operation: "GroupSAMLIdentities.edit", verb: "PATCH", rawPath: "/api/v4/groups/{id}/saml/{uid}", property: "extern_uid" },
  { operation: "GroupSCIMIdentities.edit", verb: "PATCH", rawPath: "/api/v4/groups/{id}/scim/{uid}", property: "extern_uid" },
  {
    operation: "Groups.share",
    verb: "POST",
    rawPath: "/api/v4/groups/{id}/share",
    property: "group_id",
    pyName: "shared_group_id",
  },
  {
    operation: "Groups.transfer",
    verb: "POST",
    rawPath: "/api/v4/groups/{id}/transfer",
    property: "group_id",
    pyName: "parent_group_id",
  },
  {
    operation: "Import.importBitbucketServerRepository",
    verb: "POST",
    rawPath: "/api/v4/import/bitbucket_server",
    property: "bitbucket_server_repo",
    sourceParameter: "bitbucketServerRepository",
  },
  {
    operation: "Import.importGithubRepository",
    verb: "POST",
    rawPath: "/api/v4/import/github",
    property: "repo_id",
    sourceParameter: "repositoryId",
  },
  { operation: "ProductAnalytics.dryRun", verb: "POST", rawPath: "/api/v4/projects/{project_id}/product_analytics/request/dry-run", property: "query" },
  { operation: "ProductAnalytics.load", verb: "POST", rawPath: "/api/v4/projects/{project_id}/product_analytics/request/load", property: "query" },
  { operation: "ProjectReleases.create", verb: "POST", rawPath: "/api/v4/projects/{id}/releases", property: "tag_name" },
  {
    operation: "ProtectedBranches.create",
    verb: "POST",
    rawPath: "/api/v4/projects/{id}/protected_branches",
    property: "name",
    sourceParameter: "branchName",
  },
  {
    operation: "ProtectedTags.create",
    verb: "POST",
    rawPath: "/api/v4/projects/{id}/protected_tags",
    property: "name",
    sourceParameter: "tagName",
  },
  { operation: "Runners.verify", verb: "POST", rawPath: "/api/v4/runners/verify", property: "token" },
  {
    operation: "Suggestions.editBatch",
    verb: "PUT",
    rawPath: "/api/v4/suggestions/batch_apply",
    property: "ids",
    sourceParameter: "suggestionIds",
  },
  { operation: "Topics.create", verb: "POST", rawPath: "/api/v4/topics", property: "title" },
  { operation: "Users.create", verb: "POST", rawPath: "/api/v4/users", property: "email" },
  { operation: "Users.create", verb: "POST", rawPath: "/api/v4/users", property: "name" },
  { operation: "Users.create", verb: "POST", rawPath: "/api/v4/users", property: "username" },
];

export const GITBEAKER_SOURCE_WIRE_NAME_JUDGMENTS: readonly GitbeakerSourceWireNameJudgment[] = [
  {
    operation: "ProjectRemoteMirrors.createPullMirror",
    verb: "POST",
    rawPath: "/api/v4/projects/{id}/mirror/pull",
    sourceParameter: "url",
    wireName: "import_url",
    sourceWireName: "importUrl",
    sourceVariable: "url12",
    rationale: "Pinned core/dist/index.js maps the declared url positional as importUrl: url12 while OpenAPI does not declare import_url.",
  },
  {
    operation: "Projects.createPullMirror",
    verb: "POST",
    rawPath: "/api/v4/projects/{id}/mirror/pull",
    sourceParameter: "url",
    wireName: "import_url",
    sourceWireName: "importUrl",
    sourceVariable: "url12",
    rationale: "Pinned core/dist/index.js maps the declared url positional as importUrl: url12 while OpenAPI does not declare import_url.",
  },
];

export const DOCUMENTED_SPEC_GAPS: readonly DocumentedSpecGap[] = [
  {
    operation: "RepositoryFiles.create",
    verb: "POST",
    rawPath: "/api/v4/projects/{id}/repository/files/{file_path}",
    property: "file",
    rationale: "The multipart file requirement does not describe this wrapper's JSON content contract.",
  },
  {
    operation: "RepositoryFiles.edit",
    verb: "PUT",
    rawPath: "/api/v4/projects/{id}/repository/files/{file_path}",
    property: "file",
    rationale: "The multipart file requirement does not describe this wrapper's JSON content contract.",
  },
  {
    operation: "Commits.create",
    verb: "POST",
    rawPath: "/api/v4/projects/{id}/repository/commits",
    property: "file",
    rationale: "Gitbeaker constructs a commit from required branch, commit_message, and actions; this endpoint has no standalone file property.",
  },
  {
    operation: "Commits.createComment",
    verb: "POST",
    rawPath: "/api/v4/projects/{id}/repository/commits/{sha}/comments",
    property: "line",
    rationale: "line is only meaningful for an inline comment; Gitbeaker correctly permits a general note without it.",
  },
  {
    operation: "Commits.createComment",
    verb: "POST",
    rawPath: "/api/v4/projects/{id}/repository/commits/{sha}/comments",
    property: "line_type",
    rationale: "line_type is only meaningful for an inline comment; Gitbeaker correctly permits a general note without it.",
  },
  {
    operation: "Issues.uploadMetricImage",
    verb: "POST",
    rawPath: "/api/v4/projects/{id}/issues/{issue_iid}/metric_images",
    property: "file",
    rationale: "Gitbeaker requires metricImage.content and metricImage.filename, then sends a multipart file; the JSON wrapper cannot represent that file contract.",
  },
  {
    operation: "NPM.uploadPackageFile",
    verb: "PUT",
    rawPath: "/api/v4/projects/{id}/packages/npm/{package_name}",
    property: "file",
    rationale: "Gitbeaker publishes NPM metadata by spreading the required metadata object and versions; this operation has no standalone JSON file property.",
  },
  {
    operation: "NuGet.uploadPackageFile",
    verb: "PUT",
    rawPath: "/api/v4/projects/{id}/packages/nuget",
    property: "package",
    rationale: "Gitbeaker requires packageFile.content and packageFile.filename, then sends multipart file under file; the JSON wrapper cannot represent that file contract.",
  },
  {
    operation: "NuGet.uploadSymbolPackage",
    verb: "PUT",
    rawPath: "/api/v4/projects/{id}/packages/nuget/symbolpackage",
    property: "package",
    rationale: "Gitbeaker requires packageFile.content and packageFile.filename, then sends multipart file under file; the JSON wrapper cannot represent that file contract.",
  },
  {
    operation: "ProjectImportExports.importRemoteS3",
    verb: "POST",
    rawPath: "/api/v4/projects/remote-import",
    property: "url",
    rationale: "The shared OpenAPI operation requires url for generic remote import, while Gitbeaker's S3 variant sends access_key_id, bucket_name, file_key, path, region, and secret_access_key instead.",
  },
  {
    operation: "ProjectSnippets.create",
    verb: "POST",
    rawPath: "/api/v4/projects/{id}/snippets",
    property: "file_name",
    rationale: "The schema marks file_name required while also admitting the alternative files array; Gitbeaker exposes that files contract without a file_name.",
  },
  {
    operation: "ProjectTerraformState.createVersion",
    verb: "POST",
    rawPath: "/api/v4/projects/{id}/terraform/state/{name}",
    property: "file",
    rationale: "The OpenAPI operation is multipart/form-data, whereas Gitbeaker forwards an untyped options object; the JSON wrapper cannot represent the required upload file.",
  },
  {
    operation: "RubyGems.uploadGemFile",
    verb: "POST",
    rawPath: "/api/v4/projects/{id}/packages/rubygems/api/v1/gems",
    property: "file",
    rationale: "Gitbeaker requires packageFile.content and packageFile.filename, then sends multipart file under file; the JSON wrapper cannot represent that file contract.",
  },
  {
    operation: "Snippets.create",
    verb: "POST",
    rawPath: "/api/v4/snippets",
    property: "file_name",
    rationale: "The schema marks file_name required while also admitting the alternative files array; Gitbeaker exposes that files contract without a file_name.",
  },
  {
    operation: "Users.createCIRunner",
    verb: "POST",
    rawPath: "/api/v4/user/runners",
    property: "group_id",
    rationale: "The required list unconditionally includes group_id despite nullable group_id/project_id fields and runner_type selecting the scope; it cannot require both scopes together.",
  },
  {
    operation: "Users.createCIRunner",
    verb: "POST",
    rawPath: "/api/v4/user/runners",
    property: "project_id",
    rationale: "The required list unconditionally includes project_id despite nullable group_id/project_id fields and runner_type selecting the scope; it cannot require both scopes together.",
  },
];

export const CONCRETE_DEFAULT_OVERRIDES: readonly ConcreteDefaultOverride[] = [
  {
    operation: "ProjectSnippets.create",
    verb: "POST",
    rawPath: "/api/v4/projects/{id}/snippets",
    functionName: "project_snippets_create",
    property: "visibility",
  },
];

/**
 * Optional body fields that exist only on one selector-driven OpenAPI branch.
 * This list is deliberately exact; it is not an optional-field coverage claim.
 */
export const CONDITIONAL_BRANCH_FIELD_JUDGMENTS: readonly ConditionalBranchFieldJudgment[] = [
  {
    operation: "MergeRequestApprovals.createApprovalRule",
    verb: "POST",
    rawPath: "/api/v4/projects/{id}/merge_requests/{merge_request_iid}/approval_rules",
    property: "approval_project_rule_id",
    selectorParameter: "mergerequestIId",
    rationale: "The field creates an MR-level rule from a project-level rule and is absent from the project-level endpoint.",
  },
];

function judgmentKey(
  operation: string,
  verb: string,
  rawPath: string,
  property: string,
): string {
  return `${operation} ${verb.toUpperCase()} ${rawPath} ${property}`;
}

export function sourceWireNameJudgmentKey(
  operation: string,
  verb: string,
  rawPath: string,
  sourceParameter: string,
): string {
  return `${operation} ${verb.toUpperCase()} ${rawPath} ${sourceParameter}`;
}

function indexSourceWireNameJudgments(
  judgments: readonly GitbeakerSourceWireNameJudgment[],
): Record<string, GitbeakerSourceWireNameJudgment> {
  const indexed: Record<string, GitbeakerSourceWireNameJudgment> = {};
  for (const judgment of judgments) {
    const key = sourceWireNameJudgmentKey(
      judgment.operation,
      judgment.verb,
      judgment.rawPath,
      judgment.sourceParameter,
    );
    if (indexed[key]) throw new Error(`Duplicate GitBeaker source wire-name judgment: ${key}`);
    indexed[key] = judgment;
  }
  return indexed;
}

function indexJudgments<
  T extends
    | BodyFieldOverride
    | ConditionalBranchFieldJudgment
    | DocumentedSpecGap
    | ConcreteDefaultOverride,
>(
  judgments: readonly T[],
  kind: string,
): Record<string, T> {
  const indexed: Record<string, T> = {};
  for (const judgment of judgments) {
    const key = judgmentKey(
      judgment.operation,
      judgment.verb,
      judgment.rawPath,
      judgment.property,
    );
    if (indexed[key]) throw new Error(`Duplicate ${kind}: ${key}`);
    indexed[key] = judgment;
  }
  return indexed;
}

const bodyFieldOverridesByKey = indexJudgments(
  BODY_FIELD_OVERRIDES,
  "body-field override",
);
const documentedGapsByKey = indexJudgments(
  DOCUMENTED_SPEC_GAPS,
  "documented required-body spec gap",
);
const concreteDefaultOverridesByKey = indexJudgments(
  CONCRETE_DEFAULT_OVERRIDES,
  "concrete-default override",
);
const conditionalBranchFieldsByKey = indexJudgments(
  CONDITIONAL_BRANCH_FIELD_JUDGMENTS,
  "conditional branch field judgment",
);
const gitbeakerSourceWireNameJudgmentsByKey = indexSourceWireNameJudgments(
  GITBEAKER_SOURCE_WIRE_NAME_JUDGMENTS,
);

for (const key of Object.keys(bodyFieldOverridesByKey)) {
  if (documentedGapsByKey[key]) {
    throw new Error(`Body-field override and spec gap overlap: ${key}`);
  }
}
for (const key of Object.keys(conditionalBranchFieldsByKey)) {
  if (bodyFieldOverridesByKey[key] || documentedGapsByKey[key]) {
    throw new Error(`Conditional branch field judgment overlaps another body judgment: ${key}`);
  }
}

export function bodyFieldOverride(
  operation: string,
  verb: string,
  rawPath: string,
  property: string,
): BodyFieldOverride | undefined {
  return bodyFieldOverridesByKey[judgmentKey(operation, verb, rawPath, property)];
}

export function gitbeakerSourceWireNameJudgment(
  operation: string,
  verb: string,
  rawPath: string,
  sourceParameter: string,
): GitbeakerSourceWireNameJudgment | undefined {
  return gitbeakerSourceWireNameJudgmentsByKey[
    sourceWireNameJudgmentKey(operation, verb, rawPath, sourceParameter)
  ];
}

export function conditionalBranchFieldJudgment(
  operation: string,
  verb: string,
  rawPath: string,
  property: string,
): ConditionalBranchFieldJudgment | undefined {
  return conditionalBranchFieldsByKey[
    judgmentKey(operation, verb, rawPath, property)
  ];
}

export function documentedSpecGap(
  operation: string,
  verb: string,
  rawPath: string,
  property: string,
): DocumentedSpecGap | undefined {
  return documentedGapsByKey[judgmentKey(operation, verb, rawPath, property)];
}

export function concreteDefaultOverride(
  operation: string,
  verb: string,
  rawPath: string,
  property: string,
): ConcreteDefaultOverride | undefined {
  return concreteDefaultOverridesByKey[judgmentKey(operation, verb, rawPath, property)];
}

export function bodyFieldJudgmentKey(
  judgment:
    | BodyFieldOverride
    | ConditionalBranchFieldJudgment
    | DocumentedSpecGap
    | ConcreteDefaultOverride,
): string {
  return judgmentKey(
    judgment.operation,
    judgment.verb,
    judgment.rawPath,
    judgment.property,
  );
}
