"""Manual operation descriptions that override codegen-emitted docstrings.

Keyed by snake_case function name. When present, the annotation replaces
the first line of the generated docstring — which is typically just
"ClassName.method (VERB path)." — with a human-written description that
tells the LLM what the operation actually *does*, not just which endpoint
it hits.

Codegen never touches this file. Add entries as you discover ops that
confuse the LLM or that have non-obvious semantics.
"""

ANNOTATIONS: dict[str, str] = {
    # ── Projects ────────────────────────────────────────────────────────
    "projects_all": "List projects visible to the current user. Pass brief=True (default) for slim entries.",
    "projects_show": "Get full details of a single project by numeric ID or URL-encoded path.",
    "projects_create": "Create a new project. Defaults to visibility='private'.",
    "projects_edit": "Update project settings. Only pass fields you want to change.",
    "projects_fork": "Fork a project. Blocked on Mercurial projects in Heptapod.",
    "projects_remove": "Delete a project permanently. Requires Owner or Admin.",
    "projects_search": "Search projects by name. Returns matching projects globally.",
    "projects_archive": "Archive a project (read-only mode). Reversible with unarchive.",
    "projects_unarchive": "Unarchive a previously archived project.",
    "projects_transfer": "Transfer a project to a different namespace. Requires Owner.",

    # ── Merge Requests ──────────────────────────────────────────────────
    "merge_requests_all": "List merge requests. Pass project_id for project-scoped, group_id for group, or neither for global (current user).",
    "merge_requests_show": "Get full details of a single merge request by project_id and mergerequest_iid.",
    "merge_requests_create": "Create a merge request. Required: project_id, source_branch, target_branch, title. Optional body fields (pass as top-level params): description, assignee_id, assignee_ids, reviewer_ids, labels, milestone_id, remove_source_branch, squash, target_project_id. On Heptapod hg projects target_branch must start with 'branch/'.",
    "merge_requests_edit": "Update a merge request. Optional body fields (pass as top-level params, not nested): title, description, labels, add_labels, remove_labels, assignee_id, assignee_ids, reviewer_ids, milestone_id, target_branch, state_event ('close'/'reopen'), remove_source_branch, squash, discussion_locked.",
    "merge_requests_merge": "Merge (accept) the merge request into its target branch. Requires the MR to be mergeable (no conflicts, required approvals met, pipeline green). This is the real 'merge' action — not to be confused with merge_requests_merge_to_default.",
    "merge_requests_accept": "Accept (merge) the merge request. Alias for merge_requests_merge. In gitlab_execute group.",
    "merge_requests_merge_to_default": "Create a merge commit on a throwaway ref (PUT /merge_ref). Does NOT merge into the target branch; just returns the would-be merge SHA. Rarely needed. For the real merge use merge_requests_merge.",
    "merge_requests_rebase": "Rebase the source branch onto the target. Returns rebase status.",
    "merge_request_approvals_approve": "Approve the merge request.",
    "merge_request_approvals_unapprove": "Revoke your approval of the merge request.",
    "merge_requests_all_commits": "List commits included in a merge request.",
    "merge_requests_all_diffs": "List file diffs in a merge request.",
    "merge_requests_show_changes": "Show the full changes (diff) of a merge request.",
    "merge_requests_all_pipelines": "List CI pipelines triggered for a merge request.",

    # ── Issues ──────────────────────────────────────────────────────────
    "issues_all": "List issues. Pass project_id for project-scoped, group_id for group, or neither for global (current user).",
    "issues_show": "Get full details of a single issue by global issue_id.",
    "issues_create": "Create a new issue. Required: project_id, title. Optional body fields (pass as top-level params): description, assignee_ids, confidential, labels, milestone_id, due_date, issue_type ('issue'/'incident'/'test_case'/'task'), weight, discussion_to_resolve, merge_request_to_resolve_discussions_of.",
    "issues_edit": "Update an issue. Optional body fields (pass as top-level params, not nested): title, description, labels, add_labels, remove_labels, assignee_ids, milestone_id, state_event ('close'/'reopen'), due_date, confidential, discussion_locked, issue_type, weight.",

    # ── Branches / Tags / Commits ───────────────────────────────────────
    "branches_all": "List branches. Returns {branches, categories} where categories counts git/hg_named/hg_topic.",
    "branches_show": "Get a single branch by name. Use URL-encoded name for slashes (branch%2Fdefault).",
    "branches_create": "Create a new branch from a ref. Required: branch (name), ref (source).",
    "branches_remove": "Delete a branch by name.",
    "tags_all": "List tags in a project.",
    "tags_create": "Create a new tag from a ref.",
    "commits_all": "List commits in a project. Pass ref_name to filter by branch/tag.",
    "commits_show": "Get a single commit by SHA (or hg changeset hash on Heptapod).",
    "commits_cherry_pick": "Cherry-pick a commit to a target branch.",
    "commits_revert": "Revert a commit on a target branch.",

    # ── Files ───────────────────────────────────────────────────────────
    "repository_files_show": "Get a file's metadata + base64-encoded content. Pass ref (branch/tag/SHA) as opaque string.",
    "repository_files_show_raw": "Get a file's raw content as plain text.",
    "repository_files_create": "Create a file. Pass content='text' for text OR local_path='/path/to/file' for binary (auto base64). branch and commit_message required.",
    "repository_files_edit": "Update a file. Pass content='text' for text OR local_path='/path/to/file' for binary (auto base64). branch and commit_message required.",
    "repository_files_remove": "Delete a file from a branch. Required: branch, commit_message.",
    "repositories_all_repository_trees": "List the file/directory tree of a repository. Pass ref and path for filtering.",
    "repositories_compare": "Compare two refs (branches, tags, SHAs). Returns commits and diffs between from_ and to.",

    # ── Pipelines / Jobs ────────────────────────────────────────────────
    "pipelines_all": "List CI/CD pipelines for a project.",
    "pipelines_show": "Get details of a single pipeline.",
    "pipelines_create": "Trigger a new pipeline run on a ref (branch/tag).",
    "pipelines_retry": "Retry all failed jobs in a pipeline.",
    "pipelines_cancel": "Cancel a running pipeline.",
    "jobs_all": "List CI jobs for a project.",
    "jobs_show": "Get details of a single job.",
    "jobs_show_log": "Fetch a job's raw trace (log). Use tail=N to get only the last N lines.",
    "jobs_play": "Trigger a manual job (one that's in 'manual' state).",
    "jobs_retry": "Retry a failed job.",
    "jobs_cancel": "Cancel a running job.",

    # ── Labels / Milestones ─────────────────────────────────────────────
    "project_labels_all": "List labels in a project.",
    "project_labels_create": "Create a label. Required: name, color (hex like '#FF0000').",
    "project_milestones_all": "List milestones in a project.",
    "project_milestones_create": "Create a milestone. Required: title.",

    # ── Notes (comments) ────────────────────────────────────────────────
    "issue_notes_all": "List comments on an issue.",
    "issue_notes_create": "Add a comment to an issue. Required: body (the comment text).",
    "merge_request_notes_all": "List comments on a merge request.",
    "merge_request_notes_create": "Add a comment to a merge request. Required: body.",

    # ── Members / Access ────────────────────────────────────────────────
    "project_members_all": "List project members.",
    "project_members_add": "Add a user to a project. Required: access_level (10=Guest, 20=Reporter, 30=Developer, 40=Maintainer, 50=Owner).",
    "group_members_all": "List group members.",
    "group_members_add": "Add a user to a group. Required: access_level.",

    # ── Heptapod ────────────────────────────────────────────────────────
    "hg_get_config": "Read structured Mercurial project settings (Heptapod only). Shows allow_bookmarks, auto_publish, etc.",
    "hg_set_config": "Write Mercurial project settings (Heptapod only). PUT not PATCH — unsent fields reset to defaults!",
    "hg_get_raw_hgrc": "Read the raw hgrc file of a project (Heptapod only, Maintainer required).",
    "hg_create_topic_mr": "Create an MR from a Mercurial topic. Builds source_branch=topic/{target}/{name} and target_branch=branch/{target} automatically.",

    # ── User self-service ───────────────────────────────────────────────
    "user_ssh_keys_all": "List SSH keys. Without user_id: current user's keys. With user_id: another user (admin).",
    "user_ssh_keys_create": "Add an SSH key. Required: title, key (the public key string).",
    "user_gpg_keys_all": "List GPG keys for the current user (or another via user_id).",
    "user_emails_all": "List email addresses for the current user (or another via user_id).",
    "notification_settings_show": "Read notification settings. Pass project_id or group_id to scope, or neither for global.",
    "notification_settings_edit": "Update notification settings.",

    # ── File uploads ─────────────────────────────────────────────────────
    "projects_upload_avatar": "Upload a project avatar from a LOCAL file path (PNG/JPG/GIF). Agent passes the path, server reads and uploads via multipart.",
    "groups_upload_avatar": "Upload a group avatar from a LOCAL file path (PNG/JPG/GIF). Agent passes the path, server reads and uploads via multipart.",

    # ── Job Token Scopes ────────────────────────────────────────────────
    "project_job_token_scopes_show": "Show the CI_JOB_TOKEN access settings for a project.",
    "project_job_token_scopes_edit": "Enable/disable CI_JOB_TOKEN inbound access for a project. Body field: enabled.",
    "project_job_token_scopes_add_to_inbound_allow_list": "Allow another project to access this project via CI_JOB_TOKEN. Required: target_project_id (integer).",
    "project_job_token_scopes_add_to_groups_allow_list": "Allow a group's projects to access this project via CI_JOB_TOKEN. Required: target_group_id.",
    "project_job_token_scopes_show_inbound_allow_list": "List projects allowed to access this project via CI_JOB_TOKEN.",
    "project_job_token_scopes_show_groups_allow_list": "List groups allowed to access this project via CI_JOB_TOKEN.",
}
