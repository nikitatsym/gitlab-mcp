"""GitLab / Heptapod tool operations grouped by risk level.

Layout:
  1. Group definitions
  2. ROOT tools (gitlab_version)
  3. Hand-written self-service ops that codegen missed (user keys, emails, etc.)
  4. Wire generated ops into groups via _SCOPE_GROUPS + _OVERRIDES
  5. (Phase 6) Business-logic overrides (create_merge_request, fork_project, …)
  6. (Phase 7) Heptapod-only ops (hg_*)
  7. Long-running waiters (pipelines_wait, jobs_wait)
"""

import asyncio
import inspect
import logging
import re
import time
import typing
from importlib.metadata import version as _pkg_version
from pathlib import Path as _Path
from typing import Annotated, Any
from urllib.parse import quote as _quote

from mcp.server.fastmcp import Context
from pydantic import Field

from . import _generated
from ._generated import *  # noqa: F401,F403 — re-export all generated ops
from ._generated_groups import DEFAULT_GROUPS
from .annotations import ANNOTATIONS
from .client import GitLabError, get_client
from .param_annotations import PARAM_ANNOTATIONS
from .prepare import (
    Visibility,
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
from .registry import Group, ROOT, _UNSET, _Unset, _op  # noqa: F401 — `_Unset` resolves annotations of `_strict_proxy`-decorated overrides at typing.get_type_hints() time


def _enc(v) -> str:
    return _quote(str(v), safe="")


def _ok(data):
    return {"status": "ok"} if data is None else data


def _strict_proxy(
    generated_fn,
    *,
    add_params: list | None = None,
    drop_params: set[str] | None = None,
):
    """Decorator that closes an override's tool-visible surface by mirroring
    `generated_fn`'s strict-closed signature.

    The override body may still use `**options` to collect arbitrary kwargs —
    that's only what the meta-tool decided to forward after validating against
    the synthetic signature. Since the synthetic signature mirrors
    `generated_fn` (which is itself strict-closed by codegen), unknown fields
    are rejected at the meta-tool layer before the body even runs.

    add_params: extra `inspect.Parameter` entries appended after the mirrored
      ones (e.g. `brief: bool = True` for slim wrappers).
    drop_params: names of generated_fn params to omit from the override sig
      (e.g. when the override replaces a field with something else).
    """
    import inspect

    add_params = add_params or []
    drop_params = drop_params or set()
    gen_sig = inspect.signature(generated_fn)
    new_params: list[inspect.Parameter] = []
    for name, p in gen_sig.parameters.items():
        if p.kind is inspect.Parameter.VAR_KEYWORD:
            continue
        if name in drop_params:
            continue
        new_params.append(p)
    new_params.extend(add_params)
    new_sig = inspect.Signature(parameters=new_params)

    gen_anns = dict(getattr(generated_fn, "__annotations__", {}))
    for p in add_params:
        if p.annotation is not inspect.Parameter.empty:
            gen_anns[p.name] = p.annotation
    for name in drop_params:
        gen_anns.pop(name, None)

    def decorator(fn):
        fn.__signature__ = new_sig
        fn.__annotations__ = gen_anns
        return fn

    return decorator


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


# Self-service ops are hand-written with explicit typed surfaces (per the
# strict-closed rule). gitbeaker doesn't expose the /user/keys, /user/gpg_keys,
# /user/emails, or /notification_settings endpoints, so codegen can't generate
# them — we declare typed params drawn from the GitLab REST docs.


@_op(gitlab_read)
def user_ssh_keys_all(
    user_id: str | int | None = None,
    sudo: str | int | None = None,
):
    """List SSH keys. Without user_id: current user. With: target user (admin)."""
    return _ok(get_client().get(
        _keys_path(user_id, "keys"),
        params={"sudo": sudo} if sudo is not None else {},
    ))


@_op(gitlab_write)
def user_ssh_keys_create(
    title: str,
    key: str,
    user_id: str | int | None = None,
    expires_at: str | None = None,
    usage_type: typing.Literal["auth", "signing", "auth_and_signing"] | None = None,
    sudo: str | int | None = None,
):
    """Add an SSH key. Without user_id: current user. With: target user (admin)."""
    body: dict = {"title": title, "key": key}
    if expires_at is not None:
        body["expires_at"] = expires_at
    if usage_type is not None:
        body["usage_type"] = usage_type
    if sudo is not None:
        body["sudo"] = sudo
    return _ok(get_client().post(_keys_path(user_id, "keys"), json=body))


@_op(gitlab_read)
def user_ssh_keys_show(
    key_id: str | int,
    user_id: str | int | None = None,
    sudo: str | int | None = None,
):
    """Get a specific SSH key by id."""
    base = _keys_path(user_id, "keys")
    return _ok(get_client().get(
        f"{base}/{_enc(key_id)}",
        params={"sudo": sudo} if sudo is not None else {},
    ))


@_op(gitlab_delete)
def user_ssh_keys_remove(
    key_id: str | int,
    user_id: str | int | None = None,
    sudo: str | int | None = None,
):
    """Delete an SSH key by id."""
    base = _keys_path(user_id, "keys")
    return _ok(get_client().delete(
        f"{base}/{_enc(key_id)}",
        params={"sudo": sudo} if sudo is not None else {},
    ))


@_op(gitlab_read)
def user_gpg_keys_all(
    user_id: str | int | None = None,
    sudo: str | int | None = None,
):
    """List GPG keys. Without user_id: current user. With: target user (admin)."""
    return _ok(get_client().get(
        _keys_path(user_id, "gpg_keys"),
        params={"sudo": sudo} if sudo is not None else {},
    ))


@_op(gitlab_write)
def user_gpg_keys_create(
    key: str,
    user_id: str | int | None = None,
    sudo: str | int | None = None,
):
    """Add a GPG key. Without user_id: current user. With: target user (admin)."""
    body: dict = {"key": key}
    if sudo is not None:
        body["sudo"] = sudo
    return _ok(get_client().post(_keys_path(user_id, "gpg_keys"), json=body))


@_op(gitlab_read)
def user_gpg_keys_show(
    key_id: str | int,
    user_id: str | int | None = None,
    sudo: str | int | None = None,
):
    """Get a specific GPG key by id."""
    base = _keys_path(user_id, "gpg_keys")
    return _ok(get_client().get(
        f"{base}/{_enc(key_id)}",
        params={"sudo": sudo} if sudo is not None else {},
    ))


@_op(gitlab_delete)
def user_gpg_keys_remove(
    key_id: str | int,
    user_id: str | int | None = None,
    sudo: str | int | None = None,
):
    """Delete a GPG key by id."""
    base = _keys_path(user_id, "gpg_keys")
    return _ok(get_client().delete(
        f"{base}/{_enc(key_id)}",
        params={"sudo": sudo} if sudo is not None else {},
    ))


@_op(gitlab_read)
def user_emails_all(
    user_id: str | int | None = None,
    sudo: str | int | None = None,
):
    """List email addresses. Without user_id: current user. With: target user (admin)."""
    return _ok(get_client().get(
        _keys_path(user_id, "emails"),
        params={"sudo": sudo} if sudo is not None else {},
    ))


@_op(gitlab_write)
def user_emails_create(
    email: str,
    user_id: str | int | None = None,
    confirmation_required: bool | None = None,
    sudo: str | int | None = None,
):
    """Add an email address. Without user_id: current user. With: target user (admin)."""
    body: dict = {"email": email}
    if confirmation_required is not None:
        body["confirmation_required"] = confirmation_required
    if sudo is not None:
        body["sudo"] = sudo
    return _ok(get_client().post(_keys_path(user_id, "emails"), json=body))


# Notification settings: levels + 12 per-event booleans, per
# https://docs.gitlab.com/api/notification_settings.
_NS_LEVEL = typing.Literal[
    "disabled", "participating", "watch", "global", "mention", "custom",
]


@_op(gitlab_read)
def notification_settings_show(
    group_id: str | int | None = None,
    project_id: str | int | None = None,
    sudo: str | int | None = None,
):
    """Read notification settings. Global by default; group_id or project_id scopes it."""
    if project_id is not None:
        path = f"/projects/{_enc(project_id)}/notification_settings"
    elif group_id is not None:
        path = f"/groups/{_enc(group_id)}/notification_settings"
    else:
        path = "/notification_settings"
    return _ok(get_client().get(
        path,
        params={"sudo": sudo} if sudo is not None else {},
    ))


@_op(gitlab_write)
def notification_settings_edit(
    group_id: str | int | None = None,
    project_id: str | int | None = None,
    level: _NS_LEVEL | None = None,
    notification_email: str | None = None,
    new_note: bool | None = None,
    new_issue: bool | None = None,
    reopen_issue: bool | None = None,
    close_issue: bool | None = None,
    reassign_issue: bool | None = None,
    issue_due: bool | None = None,
    new_merge_request: bool | None = None,
    push_to_merge_request: bool | None = None,
    reopen_merge_request: bool | None = None,
    close_merge_request: bool | None = None,
    reassign_merge_request: bool | None = None,
    merge_merge_request: bool | None = None,
    failed_pipeline: bool | None = None,
    fixed_pipeline: bool | None = None,
    success_pipeline: bool | None = None,
    moved_project: bool | None = None,
    merge_when_pipeline_succeeds: bool | None = None,
    new_epic: bool | None = None,
    sudo: str | int | None = None,
):
    """Update notification settings. Global by default; group_id or project_id scopes it."""
    if project_id is not None:
        path = f"/projects/{_enc(project_id)}/notification_settings"
    elif group_id is not None:
        path = f"/groups/{_enc(group_id)}/notification_settings"
    else:
        path = "/notification_settings"
    body: dict = {}
    for k, v in {
        "level": level,
        "notification_email": notification_email,
        "new_note": new_note,
        "new_issue": new_issue,
        "reopen_issue": reopen_issue,
        "close_issue": close_issue,
        "reassign_issue": reassign_issue,
        "issue_due": issue_due,
        "new_merge_request": new_merge_request,
        "push_to_merge_request": push_to_merge_request,
        "reopen_merge_request": reopen_merge_request,
        "close_merge_request": close_merge_request,
        "reassign_merge_request": reassign_merge_request,
        "merge_merge_request": merge_merge_request,
        "failed_pipeline": failed_pipeline,
        "fixed_pipeline": fixed_pipeline,
        "success_pipeline": success_pipeline,
        "moved_project": moved_project,
        "merge_when_pipeline_succeeds": merge_when_pipeline_succeeds,
        "new_epic": new_epic,
        "sudo": sudo,
    }.items():
        if v is not None:
            body[k] = v
    return _ok(get_client().put(path, json=body))


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
    "MergeRequestApprovalsApprove": gitlab_execute,
    "MergeRequestApprovalsUnapprove": gitlab_execute,
    "MergeRequestsCancelOnPipelineSuccess": gitlab_execute,
    "PipelinesRetry": gitlab_execute,
    "PipelinesCancel": gitlab_execute,
    "PipelineSchedulesRun": gitlab_execute,
    "JobsPlay": gitlab_execute,
    "JobsRetry": gitlab_execute,
    "JobsCancel": gitlab_execute,
    "JobsErase": gitlab_execute,
    "JobArtifactsKeep": gitlab_execute,
    "CommitsCherryPick": gitlab_execute,
    "CommitsRevert": gitlab_execute,
    "EnvironmentsStop": gitlab_execute,
    "DeploymentsSetApproval": gitlab_execute,
    "ProjectsHousekeeping": gitlab_execute,
    "ProjectsArchive": gitlab_execute,
    "ProjectsUnarchive": gitlab_execute,
    "ProjectsTransfer": gitlab_execute,
    "RunnersResetRegistrationToken": gitlab_execute,
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
    "LicenseAdd": gitlab_admin_write,
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


# ── Apply manual annotations to docstrings ─────────────────────────────────
# Annotations override the codegen-emitted first line ("ClassName.method (VERB
# path).") with a human-written description. This runs AFTER registration so
# the docstring is visible in help output.
for _ann_name, _ann_doc in ANNOTATIONS.items():
    _fn = getattr(_generated, _ann_name, None)
    if _fn is not None and callable(_fn):
        _fn.__doc__ = _ann_doc


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


_BRIEF_PARAM = inspect.Parameter(
    "brief",
    inspect.Parameter.KEYWORD_ONLY,
    default=True,
    annotation=bool,
)


@_op(gitlab_read)
@_strict_proxy(_generated.projects_all, add_params=[_BRIEF_PARAM])
def projects_all(brief: bool = True, **options):
    """List projects. brief=True returns slim entries (default)."""
    return _maybe_slim(_generated.projects_all(**options), _slim_project, brief)


@_op(gitlab_read)
@_strict_proxy(_generated.merge_requests_all, add_params=[_BRIEF_PARAM])
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
@_strict_proxy(_generated.issues_all, add_params=[_BRIEF_PARAM])
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
@_strict_proxy(_generated.users_all, add_params=[_BRIEF_PARAM])
def users_all(brief: bool = True, **options):
    """List users. brief=True returns slim entries."""
    return _maybe_slim(_generated.users_all(**options), _slim_user, brief)


@_op(gitlab_read)
@_strict_proxy(_generated.commits_all, add_params=[_BRIEF_PARAM])
def commits_all(project_id: str | int, brief: bool = True, **options):
    """List commits for a project. brief=True returns slim entries."""
    return _maybe_slim(
        _generated.commits_all(project_id=project_id, **options),
        _slim_commit,
        brief,
    )


@_op(gitlab_read)
@_strict_proxy(_generated.tags_all, add_params=[_BRIEF_PARAM])
def tags_all(project_id: str | int, brief: bool = True, **options):
    """List tags for a project. brief=True returns slim entries."""
    return _maybe_slim(
        _generated.tags_all(project_id=project_id, **options),
        _slim_tag,
        brief,
    )


@_op(gitlab_read)
@_strict_proxy(_generated.pipelines_all, add_params=[_BRIEF_PARAM])
def pipelines_all(project_id: str | int, brief: bool = True, **options):
    """List pipelines for a project. brief=True returns slim entries."""
    return _maybe_slim(
        _generated.pipelines_all(project_id=project_id, **options),
        _slim_pipeline,
        brief,
    )


@_op(gitlab_read)
@_strict_proxy(_generated.jobs_all, add_params=[_BRIEF_PARAM])
def jobs_all(project_id: str | int, brief: bool = True, **options):
    """List jobs for a project. brief=True returns slim entries."""
    return _maybe_slim(
        _generated.jobs_all(project_id=project_id, **options),
        _slim_job,
        brief,
    )


@_op(gitlab_read)
@_strict_proxy(_generated.branches_all, add_params=[_BRIEF_PARAM])
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


def _resolve_file_content(
    content: str | None, local_path: str | None
) -> tuple[str, dict]:
    """Return (content_str, extra_opts) from either content or local_path.

    Exactly one must be provided. local_path reads the file and base64-encodes it.
    """
    import base64

    if content is not None and local_path is not None:
        raise ValueError("Pass content OR local_path, not both.")
    if content is None and local_path is None:
        raise ValueError("Either content (text) or local_path (file on disk) is required.")
    if local_path is not None:
        p = _Path(local_path).expanduser()
        if not p.exists():
            raise ValueError(f"File not found: {local_path}")
        return base64.b64encode(p.read_bytes()).decode("ascii"), {"encoding": "base64"}
    return content, {}  # type: ignore[return-value]


_CONTENT_OPT_PARAM = inspect.Parameter(
    "content",
    inspect.Parameter.KEYWORD_ONLY,
    default=None,
    annotation=str | None,
)
_LOCAL_PATH_PARAM = inspect.Parameter(
    "local_path",
    inspect.Parameter.KEYWORD_ONLY,
    default=None,
    annotation=str | None,
)


@_op(gitlab_write)
@_strict_proxy(
    _generated.repository_files_create,
    drop_params={"content"},
    add_params=[_CONTENT_OPT_PARAM, _LOCAL_PATH_PARAM],
)
def repository_files_create(
    project_id: str | int,
    file_path: str,
    branch: str,
    commit_message: str,
    content: str | None = None,
    local_path: str | None = None,
    **options,
):
    """Create a new file in a branch.

    Pass `content` for text, or `local_path` for a file on disk (binary or text).
    local_path auto-reads and base64-encodes. Exactly one of the two is required.
    """
    resolved, extra = _resolve_file_content(content, local_path)
    return _generated.repository_files_create(
        project_id=project_id,
        file_path=file_path,
        branch=branch,
        content=resolved,
        commit_message=commit_message,
        **extra,
        **options,
    )


@_op(gitlab_write)
@_strict_proxy(
    _generated.repository_files_edit,
    drop_params={"content"},
    add_params=[_CONTENT_OPT_PARAM, _LOCAL_PATH_PARAM],
)
def repository_files_edit(
    project_id: str | int,
    file_path: str,
    branch: str,
    commit_message: str,
    content: str | None = None,
    local_path: str | None = None,
    **options,
):
    """Update an existing file in a branch.

    Pass `content` for text, or `local_path` for a file on disk (binary or text).
    local_path auto-reads and base64-encodes. Exactly one of the two is required.
    """
    resolved, extra = _resolve_file_content(content, local_path)
    return _generated.repository_files_edit(
        project_id=project_id,
        file_path=file_path,
        branch=branch,
        content=resolved,
        commit_message=commit_message,
        **extra,
        **options,
    )


@_op(gitlab_read)
@_strict_proxy(_generated.repository_files_show)
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
@_strict_proxy(_generated.repository_files_show)
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


_TAIL_PARAM = inspect.Parameter(
    "tail",
    inspect.Parameter.KEYWORD_ONLY,
    default=None,
    annotation=int | None,
)


@_op(gitlab_read)
@_strict_proxy(_generated.jobs_show_log, add_params=[_TAIL_PARAM])
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
@_strict_proxy(_generated.merge_requests_create)
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
@_strict_proxy(_generated.projects_fork)
def projects_fork(project_id: str | int, **options):
    """Fork a project. Not supported on Mercurial projects in Heptapod (see RESEARCH §11)."""
    if _project_is_hg(project_id):
        raise ValueError(
            "Personal forks are not supported for Mercurial projects on Heptapod. "
            "See RESEARCH.md §11 (Other things that are limited or absent on Heptapod)."
        )
    return _generated.projects_fork(project_id=project_id, **options)


# ── File upload overrides (multipart/form-data) ────────────────────────────
#
# MCP tools communicate via JSON — no binary file transfer. For endpoints
# that require multipart upload (avatars, attachments), we accept a LOCAL
# FILE PATH from the agent, read it server-side, and upload via httpx's
# `files=` parameter. This works when the MCP server runs on the same
# machine as the files (Claude Code, local dev). For remote MCP setups
# (Claude Desktop), use curl or the GitLab web UI.

@_op(gitlab_write)
def projects_upload_avatar(project_id: str | int, file_path: str):
    """Upload an avatar image for a project from a local file path.

    Accepts PNG, JPG, or GIF. Max 200KB per GitLab defaults.
    The file is read from the local filesystem and uploaded as multipart form data.
    """
    p = _Path(file_path).expanduser()
    if not p.exists():
        raise ValueError(f"File not found: {file_path}")
    client = get_client()
    files = {"avatar": (p.name, p.read_bytes(), "application/octet-stream")}
    r = client._request("PUT", f"/projects/{_enc(project_id)}", files=files)
    if r.status_code == 204 or not r.content:
        return {"status": "ok"}
    return r.json()


@_op(gitlab_write)
def groups_upload_avatar(group_id: str | int, file_path: str):
    """Upload an avatar image for a group from a local file path."""
    p = _Path(file_path).expanduser()
    if not p.exists():
        raise ValueError(f"File not found: {file_path}")
    client = get_client()
    files = {"avatar": (p.name, p.read_bytes(), "application/octet-stream")}
    r = client._request("PUT", f"/groups/{_enc(group_id)}", files=files)
    if r.status_code == 204 or not r.content:
        return {"status": "ok"}
    return r.json()


# ── Visibility guards (default: private only) ─────────────────────────────
#
# These overrides intercept create/edit ops that take a `visibility` field
# and enforce private-only unless the server was started with --allow-public.
# Pattern matches gitea-mcp's _enforce_visibility / _enforce_private style.


@_op(gitlab_write)
@_strict_proxy(_generated.projects_create)
def projects_create(visibility: Visibility = "private", **options):
    """Create a new project. Defaults to visibility='private'.

    Public/internal projects are blocked unless the server was started with
    --allow-public. Pass `visibility='private'` explicitly to be safe.
    """
    options["visibility"] = _enforce_visibility(visibility)
    return _generated.projects_create(**options)


@_op(gitlab_write)
@_strict_proxy(_generated.projects_edit)
def projects_edit(project_id: str | int, visibility: Visibility | None = None, **options):
    """Edit a project. If `visibility` is given it must be 'private' unless --allow-public."""
    if visibility is not None:
        options["visibility"] = _enforce_visibility(visibility)
    return _generated.projects_edit(project_id=project_id, **options)


@_op(gitlab_write)
@_strict_proxy(_generated.groups_create)
def groups_create(visibility: Visibility = "private", **options):
    """Create a new group. Defaults to visibility='private'."""
    options["visibility"] = _enforce_visibility(visibility)
    return _generated.groups_create(**options)


@_op(gitlab_write)
@_strict_proxy(_generated.groups_edit)
def groups_edit(group_id: str | int, visibility: Visibility | None = None, **options):
    """Edit a group. If `visibility` is given it must be 'private' unless --allow-public."""
    if visibility is not None:
        options["visibility"] = _enforce_visibility(visibility)
    return _generated.groups_edit(group_id=group_id, **options)


@_op(gitlab_write)
@_strict_proxy(_generated.snippets_create)
def snippets_create(visibility: Visibility = "private", **options):
    """Create a personal snippet. Defaults to visibility='private'."""
    options["visibility"] = _enforce_visibility(visibility)
    return _generated.snippets_create(**options)


@_op(gitlab_write)
@_strict_proxy(_generated.snippets_edit)
def snippets_edit(snippet_id: str | int, visibility: Visibility | None = None, **options):
    """Edit a personal snippet. If `visibility` is given it must be 'private' unless --allow-public."""
    if visibility is not None:
        options["visibility"] = _enforce_visibility(visibility)
    return _generated.snippets_edit(snippet_id=snippet_id, **options)


@_op(gitlab_write)
@_strict_proxy(_generated.project_snippets_create)
def project_snippets_create(
    project_id: str | int, visibility: Visibility = "private", **options
):
    """Create a project-scoped snippet. Defaults to visibility='private'."""
    options["visibility"] = _enforce_visibility(visibility)
    return _generated.project_snippets_create(
        project_id=project_id, **options
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


# ── Long-running waiters ──────────────────────────────────────────────────
#
# Hand-written async tools that poll a pipeline / job until it reaches a
# terminal status, streaming progress via `ctx.report_progress(message=…)`
# and `ctx.log(level, message)`. Both return a complete summary even when
# the MCP client doesn't render notifications — the result is the source
# of truth, progress / log are best-effort.
#
# Group assignment: every wait op lives in gitlab_read. Against the service
# a wait only ever GETs (pipelines_show / jobs_show / jobs_all / traces);
# even *_wait_cancel stops the local polling task, never the pipeline. The
# groups grade risk to the service, so a read-only agent may watch CI to
# completion. The only cost of a wait is API load, not mutation.


_log_wait = logging.getLogger("gitlab_mcp.wait")

# Per GitLab pipeline / job state machine:
#   non-terminal: created, waiting_for_resource, preparing, pending, running
#   terminal:     success, failed, canceled, skipped, manual, scheduled
# `manual` / `scheduled` are terminal in the polling sense — they will not
# change without an external trigger (user click, schedule fire), so further
# polling is pointless. Callers that want to wait through a manual gate
# should explicitly play the job and call wait again.
_PIPELINE_TERMINAL = frozenset(
    {"success", "failed", "canceled", "skipped", "manual", "scheduled"}
)
_JOB_TERMINAL = _PIPELINE_TERMINAL

# Status → log level for terminal outcomes. `info` for the green path,
# `warning` for non-failure stops, `error` only for actual failures.
_TERMINAL_LOG_LEVEL: dict[str, str] = {
    "success": "info",
    "failed": "error",
    "canceled": "warning",
    "skipped": "warning",
    "manual": "info",
    "scheduled": "info",
}

# Transient infrastructure blips (proxy 502s, connection resets, read
# timeouts) are routine over a CI wait that spans many minutes; a single one
# must not kill the wait. Non-transient API errors will not heal on retry
# and fail immediately - see _poll_error_is_fatal.
_MAX_POLL_FAILURES_DEFAULT = 3
# Background waits poll until terminal; a pipeline stuck in `pending` with no
# runner would otherwise keep a task polling forever. 2h covers any sane CI
# run while still bounding the orphan case. 0 disables the cap.
_MAX_LIFETIME_DEFAULT = 7200.0


def _poll_error_is_fatal(e: Exception) -> bool:
    """True for poll errors that retrying cannot fix (4xx other than 429).

    Everything else - connect/read timeouts, connection resets, 5xx, 429 -
    counts against the consecutive-failure budget instead of failing the
    wait outright.
    """
    return (
        isinstance(e, GitLabError)
        and 400 <= e.status < 500
        and e.status != 429
    )


async def _emit_progress(
    ctx: Context | None, progress: float, total: float | None, message: str
) -> None:
    """Best-effort progress emit — never breaks polling on transport errors."""
    if ctx is None:
        return
    try:
        await ctx.report_progress(progress=progress, total=total, message=message)
    except Exception:  # noqa: BLE001 — progress is best-effort, never fatal
        _log_wait.debug("report_progress failed", exc_info=True)


async def _emit_log(ctx: Context | None, level: str, message: str) -> None:
    """Best-effort log emit — never breaks polling on transport errors."""
    if ctx is None:
        return
    try:
        await ctx.log(level=level, message=message)  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001 — log notifications are best-effort
        _log_wait.debug("ctx.log failed", exc_info=True)


def _fetch_failed_logs(
    project_id: str | int, jobs: list[dict], log_tail: int
) -> dict[int, dict]:
    """Pull trace tails for each failed job. Errors per-job are absorbed so
    one unreadable trace doesn't poison the whole summary."""
    out: dict[int, dict] = {}
    for j in jobs:
        if not isinstance(j, dict) or j.get("status") != "failed":
            continue
        jid = j.get("id")
        if jid is None:
            continue
        try:
            out[jid] = jobs_show_log(  # type: ignore[call-arg]
                project_id=project_id, job_id=jid, tail=log_tail
            )
        except Exception as e:  # noqa: BLE001 — surface as content, not abort
            out[jid] = {"error": f"failed to fetch log: {e}"}
    return out


@_op(gitlab_read)
async def pipelines_wait(
    project_id: str | int,
    pipeline_id: str | int,
    timeout: Annotated[
        float,
        Field(description="Max seconds to wait for a terminal status."),
    ] = 600.0,
    interval: Annotated[
        float,
        Field(description="Seconds between polls. Lower = faster reaction, more API calls."),
    ] = 5.0,
    max_poll_failures: Annotated[
        int,
        Field(description="Consecutive transient poll failures (network errors, 5xx, 429) tolerated before the wait fails. Other 4xx errors fail immediately."),
    ] = _MAX_POLL_FAILURES_DEFAULT,
    include_jobs: Annotated[
        bool,
        Field(description="When terminated, include the pipeline's jobs in the response."),
    ] = True,
    include_failed_logs: Annotated[
        bool,
        Field(description="When include_jobs is true, also attach the trailing log of every failed job."),
    ] = True,
    log_tail: Annotated[
        int,
        Field(description="Number of trailing log lines to attach per failed job."),
    ] = 100,
    ctx: Context | None = None,
):
    """Block until a pipeline reaches a terminal status.

    Holds the MCP tool call open for the whole wait (up to `timeout`
    seconds). If the agent should stay free to do other work during a long
    CI run, use `pipelines_wait_start` + `pipelines_wait_poll(max_block=...)`
    instead - same data, no long-held call.

    Polls `pipelines_show` every `interval` seconds for up to `timeout`
    seconds. Each status transition is streamed to the client via
    `ctx.report_progress(progress, total, message=...)` and `ctx.log(level,
    message)`. Clients without notification UI still get the full picture
    from the return value — progress and log are best-effort.

    Transient poll failures (network errors, 5xx, 429) are tolerated up to
    `max_poll_failures` consecutive misses; other 4xx errors raise
    immediately. The HTTP calls run in a worker thread, so concurrent tool
    calls are not stalled by a slow GitLab response.

    Terminal statuses: success, failed, canceled, skipped, manual, scheduled.
    `manual` and `scheduled` are terminal in the polling sense (they won't
    change without an external trigger).

    Returns a dict:
      pipeline          full pipelines_show payload at the last poll
      status            terminal status string (or last seen on timeout)
      terminated        True if a terminal status was reached
      timed_out         True if `timeout` expired first
      elapsed_seconds   wall-clock duration of the wait
      polls             number of pipelines_show calls made (incl. failed)
      poll_failures     present when > 0: count of failed polls
      last_poll_error   present alongside poll_failures: last failure text
      jobs              list (when include_jobs=True) of jobs in the pipeline
      failed_logs       dict[job_id, log] (when include_failed_logs=True)
      enrichment_error  present if the post-wait jobs/log fetch failed
    """
    if timeout <= 0:
        raise ValueError(f"timeout must be > 0, got {timeout}")
    if interval <= 0:
        raise ValueError(f"interval must be > 0, got {interval}")
    if max_poll_failures < 1:
        raise ValueError(f"max_poll_failures must be >= 1, got {max_poll_failures}")
    if log_tail < 0:
        raise ValueError(f"log_tail must be >= 0, got {log_tail}")

    start = time.monotonic()
    previous_status: str | None = None
    pipeline: Any = {}
    status: str | None = None
    polls = 0
    poll_failures = 0
    consecutive_failures = 0
    last_poll_error: str | None = None
    terminated = False

    while True:
        elapsed = time.monotonic() - start
        try:
            pipeline = await asyncio.to_thread(
                _generated.pipelines_show,
                project_id=project_id,
                pipeline_id=pipeline_id,
            )
        except Exception as e:  # noqa: BLE001 - classified below
            polls += 1
            poll_failures += 1
            consecutive_failures += 1
            last_poll_error = str(e)
            if _poll_error_is_fatal(e) or consecutive_failures >= max_poll_failures:
                raise
            await _emit_log(
                ctx, "warning",
                f"pipeline #{pipeline_id}: poll failed "
                f"({consecutive_failures}/{max_poll_failures} consecutive), retrying: {e}",
            )
            if elapsed + interval >= timeout:
                break
            await asyncio.sleep(interval)
            continue
        polls += 1
        consecutive_failures = 0
        status = pipeline.get("status") if isinstance(pipeline, dict) else None

        if status != previous_status:
            await _emit_progress(
                ctx,
                progress=elapsed,
                total=timeout,
                message=f"pipeline #{pipeline_id} status: {status}",
            )
            if previous_status is None:
                await _emit_log(
                    ctx, "info",
                    f"pipeline #{pipeline_id}: starting wait (status={status})",
                )
            else:
                await _emit_log(
                    ctx, "info",
                    f"pipeline #{pipeline_id}: {previous_status} → {status}",
                )
            previous_status = status

        if status in _PIPELINE_TERMINAL:
            terminated = True
            break

        if elapsed + interval >= timeout:
            break

        await asyncio.sleep(interval)

    elapsed_final = time.monotonic() - start
    result: dict[str, Any] = {
        "pipeline": pipeline,
        "status": status,
        "terminated": terminated,
        "timed_out": not terminated,
        "elapsed_seconds": round(elapsed_final, 2),
        "polls": polls,
    }
    if poll_failures:
        result["poll_failures"] = poll_failures
        result["last_poll_error"] = last_poll_error

    if terminated:
        level = _TERMINAL_LOG_LEVEL.get(status or "", "info")
        await _emit_log(
            ctx, level,
            f"pipeline #{pipeline_id} finished with status={status} "
            f"after {polls} polls in {elapsed_final:.1f}s",
        )
    else:
        await _emit_log(
            ctx, "warning",
            f"pipeline #{pipeline_id} did not reach a terminal status "
            f"in {timeout}s (last status={status}, polls={polls})",
        )

    if include_jobs:
        # A blip here must not discard a wait that already completed -
        # absorb and report instead of raising away minutes of progress.
        try:
            jobs_raw = await asyncio.to_thread(
                _generated.jobs_all,
                project_id=project_id,
                pipeline_id=int(pipeline_id),
            )
        except Exception as e:  # noqa: BLE001 - surface as content, not abort
            result["enrichment_error"] = f"failed to fetch jobs: {e}"
            jobs_raw = None
        if jobs_raw is not None:
            jobs = jobs_raw if isinstance(jobs_raw, list) else []
            result["jobs"] = [_slim_job(j) for j in jobs]
            if include_failed_logs:
                failed_logs = await asyncio.to_thread(
                    _fetch_failed_logs, project_id, jobs, log_tail
                )
                result["failed_logs"] = failed_logs
                if failed_logs:
                    await _emit_log(
                        ctx, "error",
                        f"pipeline #{pipeline_id}: {len(failed_logs)} failed job(s); "
                        f"trailing log attached (tail={log_tail})",
                    )

            # Soft diagnostic for "failed before any job materialized" - a
            # common but not exclusive signature of `.gitlab-ci.yml`
            # validation failure. We don't auto-lint here: the waiter
            # shouldn't read files or guess ref/content. Just point at the
            # right tool so the caller can act.
            yaml_errors = (
                pipeline.get("yaml_errors") if isinstance(pipeline, dict) else None
            )
            if (
                terminated
                and status == "failed"
                and not jobs
                and not yaml_errors
            ):
                warning = (
                    f"pipeline #{pipeline_id} reached terminal status 'failed' "
                    "before any jobs were materialized. This often indicates a "
                    "`.gitlab-ci.yml` validation failure, but no `yaml_errors` "
                    "were attached to the pipeline. To get the parser error, "
                    "call gitlab_read(operation='LintCheck', params={'project_id': "
                    f"{project_id!r}}}) for the committed config, or "
                    "gitlab_read(operation='LintLint', params={'project_id': "
                    f"{project_id!r}, 'content': '<yaml>'}}) with explicit content."
                )
                result.setdefault("warnings", []).append(warning)
                await _emit_log(ctx, "warning", warning)

    return result


@_op(gitlab_read)
async def jobs_wait(
    project_id: str | int,
    job_id: str | int,
    timeout: Annotated[
        float,
        Field(description="Max seconds to wait for a terminal status."),
    ] = 600.0,
    interval: Annotated[
        float,
        Field(description="Seconds between polls. Lower = faster reaction, more API calls."),
    ] = 5.0,
    max_poll_failures: Annotated[
        int,
        Field(description="Consecutive transient poll failures (network errors, 5xx, 429) tolerated before the wait fails. Other 4xx errors fail immediately."),
    ] = _MAX_POLL_FAILURES_DEFAULT,
    include_log: Annotated[
        bool,
        Field(description="Include the job's trailing log in the response when terminated."),
    ] = True,
    log_tail: Annotated[
        int,
        Field(description="Number of trailing log lines to attach (used when include_log is true)."),
    ] = 100,
    ctx: Context | None = None,
):
    """Block until a job reaches a terminal status.

    Holds the MCP tool call open for the whole wait (up to `timeout`
    seconds). If the agent should stay free to do other work during a long
    job, use `jobs_wait_start` + `jobs_wait_poll(max_block=...)` instead.

    Polls `jobs_show` every `interval` seconds for up to `timeout` seconds.
    Each status transition is streamed via `ctx.report_progress(message=…)`
    and `ctx.log(level, message)`. The final result dict is the source of
    truth — clients without notification UI still get everything from the
    return value; progress / log are best-effort.

    Transient poll failures (network errors, 5xx, 429) are tolerated up to
    `max_poll_failures` consecutive misses; other 4xx errors raise
    immediately.

    Terminal statuses: success, failed, canceled, skipped, manual, scheduled.

    Returns a dict:
      job               full jobs_show payload at the last poll
      status            terminal status string (or last seen on timeout)
      terminated        True if a terminal status was reached
      timed_out         True if `timeout` expired first
      elapsed_seconds   wall-clock duration of the wait
      polls             number of jobs_show calls made (incl. failed)
      poll_failures     present when > 0: count of failed polls
      last_poll_error   present alongside poll_failures: last failure text
      log               trailing log (when include_log=True), structured like
                        jobs_show_log: {text, total_lines, tail, truncated}
    """
    if timeout <= 0:
        raise ValueError(f"timeout must be > 0, got {timeout}")
    if interval <= 0:
        raise ValueError(f"interval must be > 0, got {interval}")
    if max_poll_failures < 1:
        raise ValueError(f"max_poll_failures must be >= 1, got {max_poll_failures}")
    if log_tail < 0:
        raise ValueError(f"log_tail must be >= 0, got {log_tail}")

    start = time.monotonic()
    previous_status: str | None = None
    job: Any = {}
    status: str | None = None
    polls = 0
    poll_failures = 0
    consecutive_failures = 0
    last_poll_error: str | None = None
    terminated = False

    while True:
        elapsed = time.monotonic() - start
        try:
            job = await asyncio.to_thread(
                _generated.jobs_show, project_id=project_id, job_id=job_id
            )
        except Exception as e:  # noqa: BLE001 - classified below
            polls += 1
            poll_failures += 1
            consecutive_failures += 1
            last_poll_error = str(e)
            if _poll_error_is_fatal(e) or consecutive_failures >= max_poll_failures:
                raise
            await _emit_log(
                ctx, "warning",
                f"job #{job_id}: poll failed "
                f"({consecutive_failures}/{max_poll_failures} consecutive), retrying: {e}",
            )
            if elapsed + interval >= timeout:
                break
            await asyncio.sleep(interval)
            continue
        polls += 1
        consecutive_failures = 0
        status = job.get("status") if isinstance(job, dict) else None

        if status != previous_status:
            await _emit_progress(
                ctx,
                progress=elapsed,
                total=timeout,
                message=f"job #{job_id} status: {status}",
            )
            if previous_status is None:
                await _emit_log(
                    ctx, "info",
                    f"job #{job_id}: starting wait (status={status})",
                )
            else:
                await _emit_log(
                    ctx, "info",
                    f"job #{job_id}: {previous_status} → {status}",
                )
            previous_status = status

        if status in _JOB_TERMINAL:
            terminated = True
            break

        if elapsed + interval >= timeout:
            break

        await asyncio.sleep(interval)

    elapsed_final = time.monotonic() - start
    result: dict[str, Any] = {
        "job": job,
        "status": status,
        "terminated": terminated,
        "timed_out": not terminated,
        "elapsed_seconds": round(elapsed_final, 2),
        "polls": polls,
    }
    if poll_failures:
        result["poll_failures"] = poll_failures
        result["last_poll_error"] = last_poll_error

    if terminated:
        level = _TERMINAL_LOG_LEVEL.get(status or "", "info")
        await _emit_log(
            ctx, level,
            f"job #{job_id} finished with status={status} "
            f"after {polls} polls in {elapsed_final:.1f}s",
        )
    else:
        await _emit_log(
            ctx, "warning",
            f"job #{job_id} did not reach a terminal status "
            f"in {timeout}s (last status={status}, polls={polls})",
        )

    if include_log:
        try:
            result["log"] = await asyncio.to_thread(  # type: ignore[call-arg]
                jobs_show_log, project_id=project_id, job_id=job_id, tail=log_tail
            )
        except Exception as e:  # noqa: BLE001 — surface as content, not abort
            result["log"] = {"error": f"failed to fetch log: {e}"}

    return result


# ── Non-blocking wait tools (start / poll / cancel) ───────────────────────
#
# Pattern: the blocking `pipelines_wait` / `jobs_wait` above hold the MCP
# tool call open for the entire wait, which means the agent can't do
# anything else for up to `timeout` seconds. The trio below splits the
# operation into:
#
#   start  - one initial inline poll, spawns a background task for the rest,
#            returns a `wait_id` + snapshot immediately.
#   poll   — read current snapshot. `max_block > 0` waits efficiently on
#            the handle's done_event so a poll can sleep up to N seconds
#            without busy-looping.
#   cancel — cancel the underlying task, leaves the snapshot in the
#            registry (so the caller can still read the partial state).
#
# The background loop tolerates transient poll failures (budgeted via
# `max_poll_failures`) and self-terminates after `max_lifetime` seconds so
# an orphaned target can't keep a task polling forever.
#
# Each wait is also exposed as an MCP Resource at `gitlab://waits/{wait_id}`
# (registered in server.py) so clients that support resource reads can
# observe state without going through a tool call.


from .wait_registry import (  # noqa: E402 — defined late so registry is optional
    TERMINAL_STATUSES as _TERMINAL_STATUSES,
    WAIT_REGISTRY as _WAIT_REGISTRY,
    WaitHandle as _WaitHandle,
)


async def _do_pipeline_poll(handle: _WaitHandle) -> bool:
    """One pipeline poll. Updates handle, returns True if terminal.

    The HTTP call runs in a worker thread (`asyncio.to_thread`) so a slow
    GitLab response never stalls the event loop. The handle is mutated only
    after the await, back on the loop - preserving the single-writer
    invariant documented in wait_registry. Wrap in a try/except by the
    caller if it should not propagate.
    """
    payload = await asyncio.to_thread(
        _generated.pipelines_show,
        project_id=handle.project_id,
        pipeline_id=handle.target_id,
    )
    handle.polls += 1
    handle.last_payload = payload
    status = payload.get("status") if isinstance(payload, dict) else None
    handle.record_transition(status)
    return status in _TERMINAL_STATUSES


async def _do_job_poll(handle: _WaitHandle) -> bool:
    """One job poll. Updates handle, returns True if terminal."""
    payload = await asyncio.to_thread(
        _generated.jobs_show,
        project_id=handle.project_id,
        job_id=handle.target_id,
    )
    handle.polls += 1
    handle.last_payload = payload
    status = payload.get("status") if isinstance(payload, dict) else None
    handle.record_transition(status)
    return status in _TERMINAL_STATUSES


async def _enrich_pipeline_final(handle: _WaitHandle) -> None:
    """Populate handle.final_extras (jobs, failed_logs, warnings) after terminal.

    Mirrors the blocking `pipelines_wait` finalisation: optional jobs list,
    optional failed-job logs, and the soft `.gitlab-ci.yml` diagnostic
    when a pipeline reaches `failed` before any job materialised.
    HTTP runs in worker threads; final_extras is written on the loop.
    """
    opts = handle.options
    if not opts.get("include_jobs", True):
        return
    jobs_raw = await asyncio.to_thread(
        _generated.jobs_all,
        project_id=handle.project_id,
        pipeline_id=int(handle.target_id),
    )
    jobs = jobs_raw if isinstance(jobs_raw, list) else []
    handle.final_extras["jobs"] = [_slim_job(j) for j in jobs]
    if opts.get("include_failed_logs", True):
        handle.final_extras["failed_logs"] = await asyncio.to_thread(
            _fetch_failed_logs, handle.project_id, jobs, opts.get("log_tail", 100)
        )

    yaml_errors = (
        handle.last_payload.get("yaml_errors")
        if isinstance(handle.last_payload, dict)
        else None
    )
    if (
        handle.status == "failed"
        and not jobs
        and not yaml_errors
    ):
        warning = (
            f"pipeline #{handle.target_id} reached terminal status 'failed' "
            "before any jobs were materialized. This often indicates a "
            "`.gitlab-ci.yml` validation failure, but no `yaml_errors` "
            "were attached to the pipeline. To get the parser error, "
            "call gitlab_read(operation='LintCheck', params={'project_id': "
            f"{handle.project_id!r}}}) for the committed config, or "
            "gitlab_read(operation='LintLint', params={'project_id': "
            f"{handle.project_id!r}, 'content': '<yaml>'}}) with explicit content."
        )
        handle.final_extras.setdefault("warnings", []).append(warning)


async def _enrich_job_final(handle: _WaitHandle) -> None:
    """Attach trailing job log to final_extras when include_log is set."""
    opts = handle.options
    if not opts.get("include_log", True):
        return
    try:
        handle.final_extras["log"] = await asyncio.to_thread(  # type: ignore[call-arg]
            jobs_show_log,
            project_id=handle.project_id,
            job_id=handle.target_id,
            tail=opts.get("log_tail", 100),
        )
    except Exception as e:  # noqa: BLE001 — surface as content, not abort
        handle.final_extras["log"] = {"error": f"failed to fetch log: {e}"}


async def _wait_loop(handle: _WaitHandle, do_poll, enrich_final) -> None:
    """Background task body: sleep, poll, repeat until terminal.

    Shared by pipeline and job waits - `do_poll` / `enrich_final` are the
    kind-specific async callables. Tolerates `max_poll_failures` consecutive
    transient poll errors (fatal 4xx fail immediately) and gives up with
    `timed_out=True` once `max_lifetime` seconds have passed without a
    terminal status.
    """
    interval = handle.options["interval"]
    max_failures = handle.options.get("max_poll_failures", _MAX_POLL_FAILURES_DEFAULT)
    max_lifetime = handle.options.get("max_lifetime", _MAX_LIFETIME_DEFAULT)
    consecutive_failures = 0
    try:
        while True:
            await asyncio.sleep(interval)
            if max_lifetime > 0 and (time.time() - handle.started_at) >= max_lifetime:
                handle.mark_timed_out(
                    f"exceeded max_lifetime {max_lifetime:g}s without reaching "
                    f"a terminal status (last status={handle.status})"
                )
                return
            try:
                terminal = await do_poll(handle)
            except Exception as e:  # noqa: BLE001 - classified below
                consecutive_failures += 1
                handle.record_poll_failure(str(e))
                if _poll_error_is_fatal(e) or consecutive_failures >= max_failures:
                    suffix = (
                        f" ({consecutive_failures} consecutive failures)"
                        if consecutive_failures > 1 else ""
                    )
                    handle.mark_terminated(error=f"poll failed: {e}{suffix}")
                    return
                continue
            consecutive_failures = 0
            if terminal:
                try:
                    await enrich_final(handle)
                except Exception as e:  # noqa: BLE001 — enrichment is best-effort
                    handle.final_extras["enrichment_error"] = str(e)
                handle.mark_terminated()
                return
    except asyncio.CancelledError:
        handle.mark_terminated(error="cancelled")
        raise


async def _pipeline_loop(handle: _WaitHandle) -> None:
    await _wait_loop(handle, _do_pipeline_poll, _enrich_pipeline_final)


async def _job_loop(handle: _WaitHandle) -> None:
    await _wait_loop(handle, _do_job_poll, _enrich_job_final)


async def _cancel_handle(handle: _WaitHandle) -> None:
    """Cancel a handle's background task and ensure the handle is marked
    terminated with `error="cancelled"`.

    Why we don't rely on the loop's `except asyncio.CancelledError` handler
    alone: a task whose CancelledError arrives before the coroutine has
    executed past its first await point may finish without running the
    handler. We await the task to let any handler that does run set state,
    then defensively mark the handle if the done_event still isn't set.
    """
    task = handle.task
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001 — defensive
            pass
    if not handle.done_event.is_set():
        handle.mark_terminated(error="cancelled")


def _require_handle(wait_id: str, expected_kind: str | None = None) -> _WaitHandle:
    handle = _WAIT_REGISTRY.get(wait_id)
    if handle is None:
        raise ValueError(
            f"Unknown wait_id: {wait_id!r}. Use waits_list to enumerate "
            "active or recently-finished waits."
        )
    if expected_kind is not None and handle.kind != expected_kind:
        raise ValueError(
            f"wait_id {wait_id!r} is a {handle.kind} wait, not {expected_kind}. "
            f"Use {expected_kind}s_wait_poll for {handle.kind} waits or fix the call."
        )
    return handle


@_op(gitlab_read)
async def pipelines_wait_start(
    project_id: str | int,
    pipeline_id: str | int,
    interval: Annotated[
        float,
        Field(description="Seconds between background polls. Lower = faster reaction, more API calls."),
    ] = 5.0,
    max_poll_failures: Annotated[
        int,
        Field(description="Consecutive transient poll failures (network errors, 5xx, 429) tolerated by the background loop before the wait errors out. Other 4xx errors fail immediately."),
    ] = _MAX_POLL_FAILURES_DEFAULT,
    max_lifetime: Annotated[
        float,
        Field(description="Hard cap in seconds on the background wait's total runtime; when exceeded the wait stops with timed_out=True. 0 disables the cap."),
    ] = _MAX_LIFETIME_DEFAULT,
    include_jobs: Annotated[
        bool,
        Field(description="When the pipeline terminates, attach the slim jobs list to the final snapshot."),
    ] = True,
    include_failed_logs: Annotated[
        bool,
        Field(description="When include_jobs is true, also attach trailing logs of failed jobs."),
    ] = True,
    log_tail: Annotated[
        int,
        Field(description="Number of trailing log lines to attach per failed job."),
    ] = 100,
):
    """Start a non-blocking wait for a pipeline to reach a terminal status.

    Returns a `wait_id` and `resource_uri` immediately so the agent stays
    unblocked. The first poll runs synchronously so the returned snapshot
    already carries real status; if the pipeline is already terminal, no
    background task is spawned and the snapshot includes full enrichment
    (jobs, failed_logs).

    Observe with `pipelines_wait_poll(wait_id, max_block=...)` or by reading
    the resource at `gitlab://waits/{wait_id}`. Stop with
    `pipelines_wait_cancel(wait_id)`.

    The background loop tolerates `max_poll_failures` consecutive transient
    poll failures (network errors, 5xx, 429); other 4xx errors stop the wait
    immediately. After `max_lifetime` seconds without a terminal status the
    wait gives up with `timed_out=True` so an orphaned pipeline (e.g. stuck
    `pending` with no runner) can't keep a polling task alive forever.

    Returns the same snapshot shape as `pipelines_wait_poll`. See its
    docstring for field semantics.
    """
    if interval <= 0:
        raise ValueError(f"interval must be > 0, got {interval}")
    if max_poll_failures < 1:
        raise ValueError(f"max_poll_failures must be >= 1, got {max_poll_failures}")
    if max_lifetime < 0:
        raise ValueError(f"max_lifetime must be >= 0, got {max_lifetime}")
    if log_tail < 0:
        raise ValueError(f"log_tail must be >= 0, got {log_tail}")

    _WAIT_REGISTRY.reap_old()

    options = {
        "interval": interval,
        "max_poll_failures": max_poll_failures,
        "max_lifetime": max_lifetime,
        "include_jobs": include_jobs,
        "include_failed_logs": include_failed_logs,
        "log_tail": log_tail,
    }
    handle = _WAIT_REGISTRY.new_handle(
        "pipeline", project_id, pipeline_id, options
    )

    # First poll inline so the returned snapshot carries real status. It
    # fails fast (no budget): an immediate error here is feedback the agent
    # can act on right away - wrong ID, no access, instance down.
    try:
        terminal = await _do_pipeline_poll(handle)
    except Exception as e:  # noqa: BLE001
        handle.mark_terminated(error=f"initial poll failed: {e}")
        return handle.snapshot()

    if terminal:
        try:
            await _enrich_pipeline_final(handle)
        except Exception as e:  # noqa: BLE001
            handle.final_extras["enrichment_error"] = str(e)
        handle.mark_terminated()
        return handle.snapshot()

    handle.task = asyncio.create_task(_pipeline_loop(handle))
    return handle.snapshot()


@_op(gitlab_read)
async def pipelines_wait_poll(
    wait_id: str,
    max_block: Annotated[
        float,
        Field(description="If > 0 and the wait is still in flight, block up to this many seconds waiting for the next terminal event. 0 (default) returns the current snapshot immediately."),
    ] = 0.0,
):
    """Read the current snapshot of a pipeline wait.

    With `max_block=0` (default) this is non-blocking — returns whatever
    the background poll task has observed so far. With `max_block > 0` it
    waits up to that many seconds for the wait to terminate, using an
    asyncio.Event under the hood so the caller doesn't spin.

    Snapshot fields:
      wait_id           identifier registered by pipelines_wait_start
      resource_uri      gitlab://waits/<wait_id> (for clients that read resources)
      kind              "pipeline"
      project_id, pipeline_id
      status            latest observed status
      terminated        True once a terminal status was reached
      timed_out         True if this poll's max_block elapsed before terminal,
                        or if the wait gave up after max_lifetime (then `error`
                        is set too and the wait is over)
      polls             number of pipelines_show calls made (incl. failed)
      poll_failures     present when > 0: count of failed polls so far
      last_poll_error   present alongside poll_failures: last failure text
      transitions       list of {from, to, elapsed_seconds} entries
      pipeline          full pipelines_show payload from the latest poll
      started_at, ended_at, elapsed_seconds
      jobs, failed_logs, warnings   only when terminated (and include_* set on start)
      error             set if the wait failed, timed out, or was cancelled
    """
    if max_block < 0:
        raise ValueError(f"max_block must be >= 0, got {max_block}")
    handle = _require_handle(wait_id, expected_kind="pipeline")
    if max_block > 0 and not handle.done_event.is_set():
        try:
            await asyncio.wait_for(handle.done_event.wait(), timeout=max_block)
        except asyncio.TimeoutError:
            snap = handle.snapshot()
            snap["timed_out"] = True
            return snap
    return handle.snapshot()


@_op(gitlab_read)
async def pipelines_wait_cancel(wait_id: str):
    """Cancel a pipeline wait. The snapshot remains readable; error="cancelled".

    Idempotent on an already-terminal or already-errored wait — returns the
    snapshot unchanged. Cancellation only stops the background polling task;
    it does NOT cancel the underlying GitLab pipeline. Use `pipelines_cancel`
    for that.
    """
    handle = _require_handle(wait_id, expected_kind="pipeline")
    if handle.done_event.is_set():
        return handle.snapshot()
    await _cancel_handle(handle)
    return handle.snapshot()


@_op(gitlab_read)
async def jobs_wait_start(
    project_id: str | int,
    job_id: str | int,
    interval: Annotated[
        float,
        Field(description="Seconds between background polls."),
    ] = 5.0,
    max_poll_failures: Annotated[
        int,
        Field(description="Consecutive transient poll failures (network errors, 5xx, 429) tolerated by the background loop before the wait errors out. Other 4xx errors fail immediately."),
    ] = _MAX_POLL_FAILURES_DEFAULT,
    max_lifetime: Annotated[
        float,
        Field(description="Hard cap in seconds on the background wait's total runtime; when exceeded the wait stops with timed_out=True. 0 disables the cap."),
    ] = _MAX_LIFETIME_DEFAULT,
    include_log: Annotated[
        bool,
        Field(description="On termination, attach the job's trailing log to the final snapshot."),
    ] = True,
    log_tail: Annotated[
        int,
        Field(description="Number of trailing log lines to attach."),
    ] = 100,
):
    """Start a non-blocking wait for a job to reach a terminal status.

    Returns a handle immediately. See `pipelines_wait_start` for the same
    pattern (including `max_poll_failures` / `max_lifetime` semantics);
    observe with `jobs_wait_poll(wait_id, max_block=...)` or read the
    resource at `gitlab://waits/{wait_id}`.
    """
    if interval <= 0:
        raise ValueError(f"interval must be > 0, got {interval}")
    if max_poll_failures < 1:
        raise ValueError(f"max_poll_failures must be >= 1, got {max_poll_failures}")
    if max_lifetime < 0:
        raise ValueError(f"max_lifetime must be >= 0, got {max_lifetime}")
    if log_tail < 0:
        raise ValueError(f"log_tail must be >= 0, got {log_tail}")

    _WAIT_REGISTRY.reap_old()

    options = {
        "interval": interval,
        "max_poll_failures": max_poll_failures,
        "max_lifetime": max_lifetime,
        "include_log": include_log,
        "log_tail": log_tail,
    }
    handle = _WAIT_REGISTRY.new_handle("job", project_id, job_id, options)

    try:
        terminal = await _do_job_poll(handle)
    except Exception as e:  # noqa: BLE001
        handle.mark_terminated(error=f"initial poll failed: {e}")
        return handle.snapshot()

    if terminal:
        try:
            await _enrich_job_final(handle)
        except Exception as e:  # noqa: BLE001
            handle.final_extras["enrichment_error"] = str(e)
        handle.mark_terminated()
        return handle.snapshot()

    handle.task = asyncio.create_task(_job_loop(handle))
    return handle.snapshot()


@_op(gitlab_read)
async def jobs_wait_poll(
    wait_id: str,
    max_block: Annotated[
        float,
        Field(description="If > 0, block up to this many seconds waiting for terminal. 0 returns the current snapshot immediately."),
    ] = 0.0,
):
    """Read the current snapshot of a job wait. Mirrors `pipelines_wait_poll`."""
    if max_block < 0:
        raise ValueError(f"max_block must be >= 0, got {max_block}")
    handle = _require_handle(wait_id, expected_kind="job")
    if max_block > 0 and not handle.done_event.is_set():
        try:
            await asyncio.wait_for(handle.done_event.wait(), timeout=max_block)
        except asyncio.TimeoutError:
            snap = handle.snapshot()
            snap["timed_out"] = True
            return snap
    return handle.snapshot()


@_op(gitlab_read)
async def jobs_wait_cancel(wait_id: str):
    """Cancel a job wait. Mirrors `pipelines_wait_cancel`."""
    handle = _require_handle(wait_id, expected_kind="job")
    if handle.done_event.is_set():
        return handle.snapshot()
    await _cancel_handle(handle)
    return handle.snapshot()


@_op(gitlab_read)
def waits_list(
    kind: Annotated[
        str | None,
        Field(description="Filter by kind: 'pipeline' or 'job'. None lists both."),
    ] = None,
    terminated: Annotated[
        bool | None,
        Field(description="Filter by termination state. None lists all."),
    ] = None,
):
    """List active and recently-terminal waits known to this server.

    Returns a list of compact dicts (no payload, no jobs, no logs) suitable
    for letting the agent recover after losing a wait_id. Each entry has:
      wait_id, resource_uri, kind, project_id, target_id, status,
      terminated, timed_out, polls, elapsed_seconds, started_at, ended_at,
      error.

    The wait registry has a TTL (default 1 hour after termination); after
    that, entries are reaped and no longer listed.
    """
    if kind is not None and kind not in ("pipeline", "job"):
        raise ValueError(f"kind must be 'pipeline' or 'job' or None, got {kind!r}")
    out: list[dict] = []
    for handle in _WAIT_REGISTRY.all_handles():
        if kind is not None and handle.kind != kind:
            continue
        if terminated is not None and handle.terminated != terminated:
            continue
        target_key = "pipeline_id" if handle.kind == "pipeline" else "job_id"
        out.append({
            "wait_id": handle.wait_id,
            "resource_uri": f"gitlab://waits/{handle.wait_id}",
            "kind": handle.kind,
            "project_id": handle.project_id,
            target_key: handle.target_id,
            "status": handle.status,
            "terminated": handle.terminated,
            "timed_out": handle.timed_out,
            "polls": handle.polls,
            "elapsed_seconds": handle.elapsed_seconds,
            "started_at": handle.started_at,
            "ended_at": handle.ended_at,
            "error": handle.error,
        })
    return out


# ── Per-param descriptions ─────────────────────────────────────────────────
# Run at the very bottom so every override (generated wrapper or hand-written
# module-level fn) is in `globals()` before we walk PARAM_ANNOTATIONS.


def _wrap_param_annotations() -> None:
    """Attach Annotated[T, Field(description=…)] to params listed in PARAM_ANNOTATIONS.

    Idempotent: if a hint is already Annotated, the underlying type is unwrapped
    before re-wrapping so descriptions can be edited without doubling up.
    """
    for fn_name, params in PARAM_ANNOTATIONS.items():
        fn = globals().get(fn_name) or getattr(_generated, fn_name, None)
        if fn is None or not callable(fn):
            continue
        hints = typing.get_type_hints(fn, include_extras=True)
        for param_name, description in params.items():
            if param_name not in hints:
                continue
            original = hints[param_name]
            if typing.get_origin(original) is Annotated:
                original = typing.get_args(original)[0]
            fn.__annotations__[param_name] = Annotated[
                original, Field(description=description)
            ]


_wrap_param_annotations()
