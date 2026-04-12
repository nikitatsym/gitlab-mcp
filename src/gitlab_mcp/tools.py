"""GitLab / Heptapod tool operations grouped by risk level.

Layout:
  1. Group definitions
  2. ROOT tools (gitlab_version)
  3. Hand-written self-service ops that codegen missed (user keys, emails, etc.)
  4. Wire generated ops into groups via _SCOPE_GROUPS + _OVERRIDES
  5. (Phase 6) Business-logic overrides (create_merge_request, fork_project, …)
  6. (Phase 7) Heptapod-only ops (hg_*)
"""

import re
from importlib.metadata import version as _pkg_version
from urllib.parse import quote as _quote

from . import _generated
from ._generated import *  # noqa: F401,F403 — re-export all generated ops
from ._generated_groups import DEFAULT_GROUPS
from .client import get_client
from .prepare import (
    _categorize_branches,
    _enforce_visibility,
    _slim_branch,
    _slim_commit,
    _slim_issue,
    _slim_job,
    _slim_mr,
    _slim_pipeline,
    _slim_project,
    _slim_tag,
    _slim_user,
)
from .registry import Group, ROOT, _op


def _enc(v) -> str:
    return _quote(str(v), safe="")


def _ok(data):
    return {"status": "ok"} if data is None else data


# ── Groups ──────────────────────────────────────────────────────────────────

_HELP_HOWTO = (
    "Discovery uses progressive disclosure (mcp-abstract.md):\n"
    "  operation=\"help\"                              → compact category index\n"
    "  operation=\"help\" params={\"category\": \"X\"}    → full signatures in category X\n"
    "  operation=\"help\" params={\"search\": \"foo\"}    → ops whose name contains foo\n"
    "Otherwise call with operation=\"OpName\" params={...} to invoke an op."
)

gitlab_read = Group(
    "gitlab_read",
    "Query GitLab / Heptapod resources (safe, read-only).\n\n"
    + _HELP_HOWTO + "\n\n"
    "Example: gitlab_read(operation=\"ProjectsShow\", "
    "params={\"project_id\": \"mygroup/myproject\"})",
)

gitlab_write = Group(
    "gitlab_write",
    "Create, update, or modify GitLab / Heptapod resources.\n\n"
    + _HELP_HOWTO + "\n\n"
    "Example: gitlab_write(operation=\"IssuesCreate\", "
    "params={\"project_id\": \"mygroup/myproject\", \"title\": \"Bug X\"})",
)

gitlab_execute = Group(
    "gitlab_execute",
    "Trigger actions on GitLab / Heptapod: merge MRs, run pipelines, retry jobs.\n\n"
    + _HELP_HOWTO + "\n\n"
    "Example: gitlab_execute(operation=\"MergeRequestsAccept\", "
    "params={\"project_id\": \"mygroup/myproject\", \"mergerequest_iid\": 42})",
)

gitlab_delete = Group(
    "gitlab_delete",
    "Delete GitLab / Heptapod resources (destructive, irreversible).\n\n"
    + _HELP_HOWTO + "\n\n"
    "Example: gitlab_delete(operation=\"BranchesRemove\", "
    "params={\"project_id\": \"mygroup/myproject\", \"branch_name\": \"feature/old\"})",
)

gitlab_admin_read = Group(
    "gitlab_admin_read",
    "Query instance-level admin data: users, runners, hooks, settings.\n\n"
    + _HELP_HOWTO + "\n\n"
    "Example: gitlab_admin_read(operation=\"ApplicationSettingsShow\")",
)

gitlab_admin_write = Group(
    "gitlab_admin_write",
    "Manage users, runners, system hooks, and instance settings (admin).\n\n"
    + _HELP_HOWTO + "\n\n"
    "Example: gitlab_admin_write(operation=\"UsersBlock\", "
    "params={\"user_id\": 42})",
)


# ── ROOT tools ──────────────────────────────────────────────────────────────


@_op(ROOT)
def gitlab_version():
    """Get the MCP server version and the connected instance info."""
    inst = get_client().instance
    service: dict = {}
    if inst is not None:
        service = {
            "backend": inst.backend,
            "version": inst.version,
            "revision": inst.revision,
            "enterprise": inst.enterprise,
            "vcs_types": sorted(inst.vcs_types_supported),
            "url": inst.url,
        }
    return {
        "mcp": _pkg_version("gitlab-mcp"),
        "service": service,
    }


# ── Self-service user keys / emails / notification settings ────────────────
#
# Gitbeaker implements these via arrow-function URL builders
# (`(userId) => userId ? \`users/${userId}/keys\` : "user/keys"`) that the
# codegen parser can't safely unwind, so the list/create/show/remove methods
# for UserSSHKeys/UserGPGKeys/UserEmails/NotificationSettings are hand-written
# here. Each takes an optional `user_id` — omit it to operate on the
# authenticated user (self-service), pass it to operate on another user
# (requires admin rights on vanilla GitLab / Heptapod).


def _keys_path(user_id, segment: str) -> str:
    if user_id is None:
        return f"/user/{segment}"
    return f"/users/{_enc(user_id)}/{segment}"


@_op(gitlab_read)
def user_ssh_keys_all(user_id: str | int | None = None, **options):
    """List SSH keys. Without user_id: current user. With: target user (admin)."""
    return _ok(get_client().get(_keys_path(user_id, "keys"), params=options))


@_op(gitlab_write)
def user_ssh_keys_create(title: str, key: str, user_id: str | int | None = None, **options):
    """Add an SSH key. Without user_id: current user. With: target user (admin)."""
    body = {"title": title, "key": key, **options}
    return _ok(get_client().post(_keys_path(user_id, "keys"), json=body))


@_op(gitlab_read)
def user_ssh_keys_show(key_id: str | int, user_id: str | int | None = None, **options):
    """Get a specific SSH key by id."""
    base = _keys_path(user_id, "keys")
    return _ok(get_client().get(f"{base}/{_enc(key_id)}", params=options))


@_op(gitlab_delete)
def user_ssh_keys_remove(key_id: str | int, user_id: str | int | None = None, **options):
    """Delete an SSH key by id."""
    base = _keys_path(user_id, "keys")
    return _ok(get_client().delete(f"{base}/{_enc(key_id)}", params=options))


@_op(gitlab_read)
def user_gpg_keys_all(user_id: str | int | None = None, **options):
    """List GPG keys. Without user_id: current user. With: target user (admin)."""
    return _ok(get_client().get(_keys_path(user_id, "gpg_keys"), params=options))


@_op(gitlab_write)
def user_gpg_keys_create(key: str, user_id: str | int | None = None, **options):
    """Add a GPG key. Without user_id: current user. With: target user (admin)."""
    body = {"key": key, **options}
    return _ok(get_client().post(_keys_path(user_id, "gpg_keys"), json=body))


@_op(gitlab_read)
def user_gpg_keys_show(key_id: str | int, user_id: str | int | None = None, **options):
    """Get a specific GPG key by id."""
    base = _keys_path(user_id, "gpg_keys")
    return _ok(get_client().get(f"{base}/{_enc(key_id)}", params=options))


@_op(gitlab_delete)
def user_gpg_keys_remove(key_id: str | int, user_id: str | int | None = None, **options):
    """Delete a GPG key by id."""
    base = _keys_path(user_id, "gpg_keys")
    return _ok(get_client().delete(f"{base}/{_enc(key_id)}", params=options))


@_op(gitlab_read)
def user_emails_all(user_id: str | int | None = None, **options):
    """List email addresses. Without user_id: current user. With: target user (admin)."""
    return _ok(get_client().get(_keys_path(user_id, "emails"), params=options))


@_op(gitlab_write)
def user_emails_create(email: str, user_id: str | int | None = None, **options):
    """Add an email address. Without user_id: current user. With: target user (admin)."""
    body = {"email": email, **options}
    return _ok(get_client().post(_keys_path(user_id, "emails"), json=body))


@_op(gitlab_read)
def notification_settings_show(
    group_id: str | int | None = None,
    project_id: str | int | None = None,
    **options,
):
    """Read notification settings. Global by default; group_id or project_id scopes it."""
    if project_id is not None:
        path = f"/projects/{_enc(project_id)}/notification_settings"
    elif group_id is not None:
        path = f"/groups/{_enc(group_id)}/notification_settings"
    else:
        path = "/notification_settings"
    return _ok(get_client().get(path, params=options))


@_op(gitlab_write)
def notification_settings_edit(
    group_id: str | int | None = None,
    project_id: str | int | None = None,
    **options,
):
    """Update notification settings. Global by default; group_id or project_id scopes it."""
    if project_id is not None:
        path = f"/projects/{_enc(project_id)}/notification_settings"
    elif group_id is not None:
        path = f"/groups/{_enc(group_id)}/notification_settings"
    else:
        path = "/notification_settings"
    return _ok(get_client().put(path, json=options))


# ── Wire generated ops into groups ──────────────────────────────────────────


def _to_snake(name: str) -> str:
    """PascalCase → snake_case."""
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s).lower()


# Concrete Group objects keyed by string name, so we can resolve DEFAULT_GROUPS.
_GROUP_REGISTRY: dict[str, Group] = {
    "gitlab_read": gitlab_read,
    "gitlab_write": gitlab_write,
    "gitlab_execute": gitlab_execute,
    "gitlab_delete": gitlab_delete,
    "gitlab_admin_read": gitlab_admin_read,
    "gitlab_admin_write": gitlab_admin_write,
}


# Manual overrides: move specific ops out of their HTTP-verb-default group
# into a semantically better fit. Keyed by PascalCase op name.
_OVERRIDES: dict[str, Group] = {
    # ── gitlab_execute: state-changing actions ──────────────────────────
    "MergeRequestsAccept": gitlab_execute,
    "MergeRequestsMerge": gitlab_execute,
    "MergeRequestsRebase": gitlab_execute,
    "MergeRequestsApprove": gitlab_execute,
    "MergeRequestsUnapprove": gitlab_execute,
    "MergeRequestsCancelOnPipelineSuccess": gitlab_execute,
    "PipelinesRetry": gitlab_execute,
    "PipelinesCancel": gitlab_execute,
    "PipelineSchedulesPlay": gitlab_execute,
    "JobsPlay": gitlab_execute,
    "JobsRetry": gitlab_execute,
    "JobsCancel": gitlab_execute,
    "JobsErase": gitlab_execute,
    "JobsKeepArtifacts": gitlab_execute,
    "CommitsCherryPick": gitlab_execute,
    "CommitsRevert": gitlab_execute,
    "EnvironmentsStop": gitlab_execute,
    "DeploymentsApproveOrReject": gitlab_execute,
    "ProjectsHousekeeping": gitlab_execute,
    "ProjectsArchive": gitlab_execute,
    "ProjectsUnarchive": gitlab_execute,
    "ProjectsTransfer": gitlab_execute,
    "RunnersReset": gitlab_execute,
    "RunnersResetAuthenticationToken": gitlab_execute,
    # ── gitlab_admin_read ────────────────────────────────────────────────
    "ApplicationSettingsShow": gitlab_admin_read,
    "ApplicationStatisticsShow": gitlab_admin_read,
    "ApplicationPlanLimitsShow": gitlab_admin_read,
    "BroadcastMessagesAll": gitlab_admin_read,
    "BroadcastMessagesShow": gitlab_admin_read,
    "SystemHooksAll": gitlab_admin_read,
    "LicenseShow": gitlab_admin_read,
    "LicenseAll": gitlab_admin_read,
    "MigrationsAll": gitlab_admin_read,
    "MigrationsShow": gitlab_admin_read,
    # ── gitlab_admin_write ───────────────────────────────────────────────
    "ApplicationSettingsEdit": gitlab_admin_write,
    "ApplicationPlanLimitsEdit": gitlab_admin_write,
    "BroadcastMessagesCreate": gitlab_admin_write,
    "BroadcastMessagesEdit": gitlab_admin_write,
    "BroadcastMessagesRemove": gitlab_admin_write,
    "SystemHooksCreate": gitlab_admin_write,
    "SystemHooksRemove": gitlab_admin_write,
    "SystemHooksTest": gitlab_admin_write,
    "LicenseCreate": gitlab_admin_write,
    "LicenseRemove": gitlab_admin_write,
    "UsersBlock": gitlab_admin_write,
    "UsersUnblock": gitlab_admin_write,
    "UsersBan": gitlab_admin_write,
    "UsersUnban": gitlab_admin_write,
    "UsersActivate": gitlab_admin_write,
    "UsersDeactivate": gitlab_admin_write,
    "UsersApprove": gitlab_admin_write,
    "UsersReject": gitlab_admin_write,
    "UsersDisableTwoFactor": gitlab_admin_write,
    "UsersCreateCiRunner": gitlab_admin_write,
}


def _build_scope_groups() -> dict[Group, list[str]]:
    """Merge DEFAULT_GROUPS with _OVERRIDES into a Group-keyed map."""
    result: dict[Group, list[str]] = {g: [] for g in _GROUP_REGISTRY.values()}

    # Track overridden ops so we skip them when copying defaults.
    overridden = set(_OVERRIDES.keys())

    # 1. Copy defaults, skipping anything overridden.
    for group_name, op_names in DEFAULT_GROUPS.items():
        group = _GROUP_REGISTRY[group_name]
        for op_name in op_names:
            if op_name not in overridden:
                result[group].append(op_name)

    # 2. Apply overrides.
    for op_name, group in _OVERRIDES.items():
        result[group].append(op_name)

    # 3. Sort each group for deterministic registration order.
    for ops in result.values():
        ops.sort()

    return result


_SCOPE_GROUPS: dict[Group, list[str]] = _build_scope_groups()


_grouped: set[str] = set()


def _register_generated() -> None:
    """Retroactively decorate generated functions with their assigned groups."""
    for group, op_names in _SCOPE_GROUPS.items():
        for pascal in op_names:
            snake = _to_snake(pascal)
            fn = getattr(_generated, snake, None)
            if fn is None:
                continue
            _op(group)(fn)
            _grouped.add(snake)


_register_generated()


# ── List-view overrides with brief=True default ────────────────────────────
#
# These shadow the generated functions of the same name. When `brief=True`
# (the default), results are trimmed via `_slim_*` to the fields the LLM
# actually needs. `brief=False` returns the full API response.
#
# Each override is decorated here directly; `_register_generated()` already
# ran for these names but the decorators on the new module-level functions
# shadow those attributes — `server._register_tools()` picks up the shadowed
# (override) version because it walks the current tools module state.


def _maybe_slim(result, slim_fn, brief: bool):
    if brief and isinstance(result, list):
        return [slim_fn(x) for x in result]
    return result


@_op(gitlab_read)
def projects_all(brief: bool = True, **options):
    """List projects. brief=True returns slim entries (default)."""
    return _maybe_slim(_generated.projects_all(**options), _slim_project, brief)


@_op(gitlab_read)
def merge_requests_all(
    project_id: str | int | None = None,
    group_id: str | int | None = None,
    brief: bool = True,
    **options,
):
    """List merge requests.

    Without `project_id` or `group_id` this hits the global `/merge_requests`
    endpoint which returns MRs scoped to the current user. Pass `project_id=N`
    to list all MRs in a project, or `group_id=N` for a group.
    """
    if project_id is not None:
        path = f"/projects/{_enc(project_id)}/merge_requests"
    elif group_id is not None:
        path = f"/groups/{_enc(group_id)}/merge_requests"
    else:
        path = "/merge_requests"
    raw = get_client().get(path, params=options)
    return _maybe_slim(raw, _slim_mr, brief)


@_op(gitlab_read)
def issues_all(
    project_id: str | int | None = None,
    group_id: str | int | None = None,
    brief: bool = True,
    **options,
):
    """List issues.

    Without `project_id` or `group_id` this hits the global `/issues` endpoint
    which returns issues scoped to the current user. Pass `project_id=N` to
    list all issues in a project, or `group_id=N` for a group.
    """
    if project_id is not None:
        path = f"/projects/{_enc(project_id)}/issues"
    elif group_id is not None:
        path = f"/groups/{_enc(group_id)}/issues"
    else:
        path = "/issues"
    raw = get_client().get(path, params=options)
    return _maybe_slim(raw, _slim_issue, brief)


@_op(gitlab_read)
def users_all(brief: bool = True, **options):
    """List users. brief=True returns slim entries."""
    return _maybe_slim(_generated.users_all(**options), _slim_user, brief)


@_op(gitlab_read)
def commits_all(project_id: str | int, brief: bool = True, **options):
    """List commits for a project. brief=True returns slim entries."""
    return _maybe_slim(
        _generated.commits_all(project_id=project_id, **options),
        _slim_commit,
        brief,
    )


@_op(gitlab_read)
def tags_all(project_id: str | int, brief: bool = True, **options):
    """List tags for a project. brief=True returns slim entries."""
    return _maybe_slim(
        _generated.tags_all(project_id=project_id, **options),
        _slim_tag,
        brief,
    )


@_op(gitlab_read)
def pipelines_all(project_id: str | int, brief: bool = True, **options):
    """List pipelines for a project. brief=True returns slim entries."""
    return _maybe_slim(
        _generated.pipelines_all(project_id=project_id, **options),
        _slim_pipeline,
        brief,
    )


@_op(gitlab_read)
def jobs_all(project_id: str | int, brief: bool = True, **options):
    """List jobs for a project. brief=True returns slim entries."""
    return _maybe_slim(
        _generated.jobs_all(project_id=project_id, **options),
        _slim_job,
        brief,
    )


@_op(gitlab_read)
def branches_all(project_id: str | int, brief: bool = True, **options):
    """List branches for a project.

    Returns a dict with keys `branches` (slim entries when brief=True) and
    `categories` (counts by Heptapod naming: git / hg_named / hg_topic).
    On GitLab (non-Heptapod) projects, all entries are "git".
    Raw branch names are preserved verbatim — prefixes `branch/...` and
    `topic/.../...` are NOT stripped.
    """
    raw = _generated.branches_all(project_id=project_id, **options)
    if not isinstance(raw, list):
        return raw
    categories = _categorize_branches(raw)
    entries = [_slim_branch(b) for b in raw] if brief else raw
    return {"branches": entries, "categories": categories}


# Safety net: any generated op that somehow escaped _SCOPE_GROUPS gets routed
# to gitlab_read as a least-surprise fallback. This should stay empty — if it
# fires, it means DEFAULT_GROUPS and _generated.py are out of sync and codegen
# needs to re-run.
def _safety_net() -> None:
    import inspect as _inspect
    for _name, _fn in _inspect.getmembers(_generated, _inspect.isfunction):
        if _name.startswith("_"):
            continue
        if _name in _grouped:
            continue
        if hasattr(_fn, "_mcp_group"):
            continue
        _op(gitlab_read)(_fn)
        _grouped.add(_name)


_safety_net()


# ── Pre-flight guards and semantic overrides ───────────────────────────────


def _project_is_hg(project_id) -> bool:
    """Return True iff the current backend is Heptapod AND the given project
    is a Mercurial (hg/hg_git) project. Fast-paths GitLab instances without
    any per-project network call.
    """
    client = get_client()
    inst = client.instance
    if inst is None or inst.backend != "heptapod":
        return False
    vcs = client.project_vcs_type(project_id)
    return vcs in ("hg", "hg_git")


@_op(gitlab_read)
def repository_files_show(
    project_id: str | int,
    file_path: str,
    ref: str,
    **options,
):
    """Get a file's metadata and base64-encoded content.

    `ref` is passed through as an opaque identifier. It accepts a branch
    name, tag name, or commit SHA. On Heptapod Mercurial projects, refs
    have the form `branch/<name>` or `topic/<target>/<name>` — pass them
    verbatim, do not strip the prefix. Commit identifiers may be git SHAs
    or hg changeset hashes depending on the project's vcs_type.
    """
    return _generated.repository_files_show(
        project_id=project_id, file_path=file_path, ref=ref, **options
    )


@_op(gitlab_read)
def repository_files_show_raw(
    project_id: str | int,
    file_path: str,
    ref: str,
    **options,
):
    """Get a file's raw contents as text. See `repository_files_show` for ref semantics."""
    path = (
        f"/projects/{_enc(project_id)}/repository/files/{_enc(file_path)}/raw"
    )
    params = {"ref": ref, **options}
    return get_client().get_text(path, params=params)


@_op(gitlab_read)
def jobs_show_log(
    project_id: str | int,
    job_id: str | int,
    tail: int | None = None,
    **options,
):
    """Fetch a CI job's trace (raw log). When `tail` is set, returns only
    the last N lines plus a truncation marker; otherwise returns the full
    text.
    """
    if tail is not None and tail < 0:
        raise ValueError(f"tail must be >= 0, got {tail}")
    path = f"/projects/{_enc(project_id)}/jobs/{_enc(job_id)}/trace"
    text = get_client().get_text(path, params=options)
    lines = text.splitlines()
    if tail is None or tail == 0 or len(lines) <= tail:
        return {
            "text": text,
            "total_lines": len(lines),
            "truncated": False,
        }
    dropped = len(lines) - tail
    return {
        "text": f"... ({dropped} lines truncated)\n" + "\n".join(lines[-tail:]),
        "total_lines": len(lines),
        "tail": tail,
        "truncated": True,
    }


@_op(gitlab_write)
def merge_requests_create(
    project_id: str | int,
    source_branch: str,
    target_branch: str,
    title: str,
    **options,
):
    """Create a merge request.

    On Heptapod Mercurial projects, `source_branch` should look like
    `topic/<target>/<name>` and `target_branch` must start with `branch/`
    (e.g., `branch/default`). Use `hg_create_topic_mr` for a convenience
    wrapper that builds these strings from components.
    """
    if not source_branch or not target_branch:
        raise ValueError("source_branch and target_branch are required")
    if source_branch == target_branch:
        raise ValueError(
            f"source_branch and target_branch must differ (both are {source_branch!r})"
        )

    if _project_is_hg(project_id) and not target_branch.startswith("branch/"):
        raise ValueError(
            f"Mercurial projects on Heptapod require target_branch to start "
            f"with 'branch/' (e.g., 'branch/default'). Received: {target_branch!r}"
        )

    return _generated.merge_requests_create(
        project_id=project_id,
        source_branch=source_branch,
        target_branch=target_branch,
        title=title,
        **options,
    )


@_op(gitlab_write)
def projects_fork(project_id: str | int, **options):
    """Fork a project. Not supported on Mercurial projects in Heptapod (see RESEARCH §11)."""
    if _project_is_hg(project_id):
        raise ValueError(
            "Personal forks are not supported for Mercurial projects on Heptapod. "
            "See RESEARCH.md §11 (Other things that are limited or absent on Heptapod)."
        )
    return _generated.projects_fork(project_id=project_id, **options)


# ── Visibility guards (default: private only) ─────────────────────────────
#
# These overrides intercept create/edit ops that take a `visibility` field
# and enforce private-only unless the server was started with --allow-public.
# Pattern matches gitea-mcp's _enforce_visibility / _enforce_private style.


@_op(gitlab_write)
def projects_create(visibility: str = "private", **options):
    """Create a new project. Defaults to visibility='private'.

    Public/internal projects are blocked unless the server was started with
    --allow-public. Pass `visibility='private'` explicitly to be safe.
    """
    visibility = _enforce_visibility(visibility)
    return _generated.projects_create(visibility=visibility, **options)


@_op(gitlab_write)
def projects_edit(project_id: str | int, visibility: str | None = None, **options):
    """Edit a project. If `visibility` is given it must be 'private' unless --allow-public."""
    if visibility is not None:
        visibility = _enforce_visibility(visibility)
        options["visibility"] = visibility
    return _generated.projects_edit(project_id=project_id, **options)


@_op(gitlab_write)
def groups_create(visibility: str = "private", **options):
    """Create a new group. Defaults to visibility='private'."""
    visibility = _enforce_visibility(visibility)
    return _generated.groups_create(visibility=visibility, **options)


@_op(gitlab_write)
def groups_edit(group_id: str | int, visibility: str | None = None, **options):
    """Edit a group. If `visibility` is given it must be 'private' unless --allow-public."""
    if visibility is not None:
        visibility = _enforce_visibility(visibility)
        options["visibility"] = visibility
    return _generated.groups_edit(group_id=group_id, **options)


@_op(gitlab_write)
def snippets_create(visibility: str = "private", **options):
    """Create a personal snippet. Defaults to visibility='private'."""
    visibility = _enforce_visibility(visibility)
    return _generated.snippets_create(visibility=visibility, **options)


@_op(gitlab_write)
def snippets_edit(snippet_id: str | int, visibility: str | None = None, **options):
    """Edit a personal snippet. If `visibility` is given it must be 'private' unless --allow-public."""
    if visibility is not None:
        visibility = _enforce_visibility(visibility)
        options["visibility"] = visibility
    return _generated.snippets_edit(snippet_id=snippet_id, **options)


@_op(gitlab_write)
def project_snippets_create(
    project_id: str | int, visibility: str = "private", **options
):
    """Create a project-scoped snippet. Defaults to visibility='private'."""
    visibility = _enforce_visibility(visibility)
    return _generated.project_snippets_create(
        project_id=project_id, visibility=visibility, **options
    )


# ── Heptapod-only tools (always defined, registered only when backend is heptapod) ──


@_op(gitlab_read)
def hg_get_config(project_id: str | int):
    """Read the high-level Mercurial project settings (Heptapod only).

    Returns a structured view of allow_bookmarks, allow_multiple_heads,
    auto_publish, and inheritance flags. Requires Maintainer-or-higher on
    the project.
    """
    return get_client().get(
        f"/projects/{_enc(project_id)}/hg_heptapod_config"
    )


hg_get_config._heptapod_only = True


@_op(gitlab_read)
def hg_get_raw_hgrc(project_id: str | int):
    """Read the project's raw hgrc file (Heptapod only, Maintainer required)."""
    return get_client().get(f"/projects/{_enc(project_id)}/hgrc")


hg_get_raw_hgrc._heptapod_only = True


@_op(gitlab_write)
def hg_set_config(
    project_id: str | int,
    inherit: bool,
    allow_bookmarks: bool | None = None,
    allow_multiple_heads: bool | None = None,
    auto_publish: str | None = None,
):
    """Set high-level Mercurial project settings (Heptapod only).

    Warning: this endpoint is PUT, not PATCH — any field omitted from the
    call is reset to its default. Always pass the full intended state.

    `auto_publish` accepts "nothing", "non-topic", or "all".
    """
    if auto_publish is not None and auto_publish not in (
        "nothing",
        "non-topic",
        "all",
    ):
        raise ValueError(
            f"auto_publish must be 'nothing', 'non-topic', or 'all'; got {auto_publish!r}"
        )
    body: dict = {"inherit": inherit}
    if allow_bookmarks is not None:
        body["allow_bookmarks"] = allow_bookmarks
    if allow_multiple_heads is not None:
        body["allow_multiple_heads"] = allow_multiple_heads
    if auto_publish is not None:
        body["auto_publish"] = auto_publish
    return get_client().put(f"/projects/{_enc(project_id)}/hgrc", json=body)


hg_set_config._heptapod_only = True


@_op(gitlab_write)
def hg_create_topic_mr(
    project_id: str | int,
    target_hg_branch: str,
    topic_name: str,
    title: str,
    description: str | None = None,
    **options,
):
    """Create a merge request from a Mercurial topic (Heptapod only).

    Convenience wrapper: builds `source_branch=topic/{target_hg_branch}/{topic_name}`
    and `target_branch=branch/{target_hg_branch}`, then calls
    `merge_requests_create` under the hood.

    The topic must already exist on the server (pushed via `hg push`).
    Rejects the call if the target project is git-typed even on Heptapod.
    """
    client = get_client()
    vcs = client.project_vcs_type(project_id)
    if vcs == "git":
        raise ValueError(
            f"hg_create_topic_mr requires a Mercurial project; project "
            f"{project_id} is vcs_type=git on this Heptapod instance."
        )

    source_branch = f"topic/{target_hg_branch}/{topic_name}"
    target_branch = f"branch/{target_hg_branch}"
    body: dict = {
        "source_branch": source_branch,
        "target_branch": target_branch,
        "title": title,
    }
    if description is not None:
        body["description"] = description
    body.update(options)
    return _generated.merge_requests_create(project_id=project_id, **body)


hg_create_topic_mr._heptapod_only = True
