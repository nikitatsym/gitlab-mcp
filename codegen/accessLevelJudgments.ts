/**
 * Exact access-level domains for generated public parameters.
 *
 * OpenAPI identifies the canonical operation, verb, raw path, and wire field.
 * Where its schema is a generic integer, the pinned GitBeaker surface supplies
 * the baseline role domain; GitBeaker 43.8.0 omits GitLab Planner (15), so the
 * selected operation domain restores that documented value. Do not broaden a
 * domain by field name: each entry is an independently audited wire contract.
 */
export interface AccessLevelJudgment {
  operation: string;
  verb: string;
  rawPath: string;
  field: string;
  values: readonly number[];
  /** The wire field is an array whose items carry the selected domain. */
  listValued?: true;
}

const BROAD_MEMBER_DOMAIN = [0, 5, 10, 15, 20, 30, 40, 50] as const;
const RESTRICTED_MEMBER_DOMAIN = [10, 15, 20, 30, 40, 50] as const;
const SAML_AND_INVITATION_DOMAIN = [5, 10, 15, 20, 30, 40, 50] as const;
const PROTECTED_PUSH_AND_MERGE_DOMAIN = [0, 30, 40, 60] as const;
const PROTECTED_UNPROTECT_DOMAIN = [30, 40, 60] as const;

export const ACCESS_LEVEL_JUDGMENTS: readonly AccessLevelJudgment[] = [
  { operation: "GroupMembers.add", verb: "POST", rawPath: "/api/v4/groups/{id}/members", field: "access_level", values: BROAD_MEMBER_DOMAIN },
  { operation: "GroupMembers.edit", verb: "PUT", rawPath: "/api/v4/groups/{id}/members/{user_id}", field: "access_level", values: BROAD_MEMBER_DOMAIN },
  { operation: "ProjectMembers.add", verb: "POST", rawPath: "/api/v4/projects/{id}/members", field: "access_level", values: BROAD_MEMBER_DOMAIN },
  { operation: "ProjectMembers.edit", verb: "PUT", rawPath: "/api/v4/projects/{id}/members/{user_id}", field: "access_level", values: BROAD_MEMBER_DOMAIN },
  { operation: "GroupAccessRequests.approve", verb: "PUT", rawPath: "/api/v4/groups/{id}/access_requests/{user_id}/approve", field: "access_level", values: BROAD_MEMBER_DOMAIN },
  { operation: "ProjectAccessRequests.approve", verb: "PUT", rawPath: "/api/v4/projects/{id}/access_requests/{user_id}/approve", field: "access_level", values: BROAD_MEMBER_DOMAIN },
  { operation: "GroupMemberRoles.add", verb: "POST", rawPath: "/api/v4/groups/{id}/members", field: "base_access_level", values: RESTRICTED_MEMBER_DOMAIN },
  { operation: "BroadcastMessages.create", verb: "POST", rawPath: "/api/v4/broadcast_messages", field: "target_access_levels", values: RESTRICTED_MEMBER_DOMAIN, listValued: true },
  { operation: "BroadcastMessages.edit", verb: "PUT", rawPath: "/api/v4/broadcast_messages/{id}", field: "target_access_levels", values: RESTRICTED_MEMBER_DOMAIN, listValued: true },
  { operation: "Projects.allInvitedGroups", verb: "GET", rawPath: "/api/v4/projects/{id}/invited_groups", field: "shared_min_access_level", values: BROAD_MEMBER_DOMAIN },
  { operation: "Users.allContributedProjects", verb: "GET", rawPath: "/api/v4/users/{user_id}/contributed_projects", field: "min_access_level", values: BROAD_MEMBER_DOMAIN },
  { operation: "GroupAccessTokens.create", verb: "POST", rawPath: "/api/v4/groups/{id}/access_tokens", field: "access_level", values: RESTRICTED_MEMBER_DOMAIN },
  { operation: "ProjectAccessTokens.create", verb: "POST", rawPath: "/api/v4/projects/{id}/access_tokens", field: "access_level", values: RESTRICTED_MEMBER_DOMAIN },
  { operation: "ProtectedBranches.create", verb: "POST", rawPath: "/api/v4/projects/{id}/protected_branches", field: "push_access_level", values: PROTECTED_PUSH_AND_MERGE_DOMAIN },
  { operation: "ProtectedBranches.create", verb: "POST", rawPath: "/api/v4/projects/{id}/protected_branches", field: "merge_access_level", values: PROTECTED_PUSH_AND_MERGE_DOMAIN },
  { operation: "ProtectedBranches.create", verb: "POST", rawPath: "/api/v4/projects/{id}/protected_branches", field: "unprotect_access_level", values: PROTECTED_UNPROTECT_DOMAIN },
  { operation: "ProtectedBranches.edit", verb: "PATCH", rawPath: "/api/v4/projects/{id}/protected_branches/{name}", field: "unprotect_access_level", values: PROTECTED_UNPROTECT_DOMAIN },
  { operation: "GroupSAMLLinks.create", verb: "POST", rawPath: "/api/v4/groups/{id}/saml_group_links", field: "access_level", values: SAML_AND_INVITATION_DOMAIN },
  { operation: "GroupInvitations.add", verb: "POST", rawPath: "/api/v4/groups/{id}/invitations", field: "access_level", values: SAML_AND_INVITATION_DOMAIN },
  { operation: "GroupInvitations.edit", verb: "PUT", rawPath: "/api/v4/groups/{id}/invitations/{email}", field: "access_level", values: RESTRICTED_MEMBER_DOMAIN },
  { operation: "ProjectInvitations.add", verb: "POST", rawPath: "/api/v4/projects/{id}/invitations", field: "access_level", values: SAML_AND_INVITATION_DOMAIN },
  { operation: "ProjectInvitations.edit", verb: "PUT", rawPath: "/api/v4/projects/{id}/invitations/{email}", field: "access_level", values: RESTRICTED_MEMBER_DOMAIN },
];

export function accessLevelJudgmentKey(
  operation: string,
  verb: string,
  rawPath: string,
  field: string,
): string {
  return `${operation}\u0000${verb.toUpperCase()}\u0000${rawPath}\u0000${field}`;
}

const accessLevelJudgmentsByKey: Record<string, AccessLevelJudgment> = {};
for (const judgment of ACCESS_LEVEL_JUDGMENTS) {
  const key = accessLevelJudgmentKey(
    judgment.operation,
    judgment.verb,
    judgment.rawPath,
    judgment.field,
  );
  if (accessLevelJudgmentsByKey[key] !== undefined) {
    throw new Error(`Duplicate access-level judgment: ${key}`);
  }
  accessLevelJudgmentsByKey[key] = judgment;
}

export function accessLevelJudgment(
  operation: string,
  verb: string,
  rawPath: string,
  field: string,
): AccessLevelJudgment | undefined {
  return accessLevelJudgmentsByKey[
    accessLevelJudgmentKey(operation, verb, rawPath, field)
  ];
}

export function accessLevelPythonType(
  judgment: AccessLevelJudgment,
  sourceType: string,
): string {
  const scalarSource = sourceType === "int" || /^Literal\[\d+(?:, \d+)*\]$/.test(sourceType);
  const listSource = sourceType === "list[int]" || /^list\[Literal\[\d+(?:, \d+)*\]\]$/.test(sourceType);
  const expectedSource = judgment.listValued ? listSource : scalarSource;
  if (!expectedSource) {
    throw new Error(
      `Access-level source type drift for ${judgment.operation}: ` +
      `${judgment.verb} ${judgment.rawPath} ${judgment.field} is ${sourceType}`,
    );
  }

  const literal = `Literal[${judgment.values.join(", ")}]`;
  return judgment.listValued ? `list[${literal}]` : literal;
}

export function assertAllAccessLevelJudgmentsApplied(
  appliedJudgmentKeys: ReadonlySet<string>,
): void {
  const stale = ACCESS_LEVEL_JUDGMENTS.filter(
    (judgment) => !appliedJudgmentKeys.has(
      accessLevelJudgmentKey(
        judgment.operation,
        judgment.verb,
        judgment.rawPath,
        judgment.field,
      ),
    ),
  );
  if (stale.length > 0) {
    throw new Error(
      `Stale access-level judgments:\n${stale
        .map(
          (judgment) =>
            `  ${judgment.operation}: ${judgment.verb} ${judgment.rawPath} ${judgment.field}`,
        )
        .join("\n")}`,
    );
  }
}
