from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, cast
from urllib.parse import quote

import httpx

if TYPE_CHECKING:
    from .client import GitLabClient

Backend = Literal["gitlab", "heptapod"]
VcsType = Literal["git", "hg", "hg_git"]

_VALID_VCS = {"git", "hg", "hg_git"}


@dataclass
class InstanceInfo:
    """What backend the MCP is talking to, populated once at startup by main()."""

    backend: Backend
    version: str
    enterprise: bool
    revision: str = ""
    vcs_types_supported: set[str] = field(default_factory=set)
    url: str = ""


def _parse_metadata(meta: object) -> tuple[str, str, bool]:
    """(version, revision, enterprise) from a /metadata response dict."""
    if not isinstance(meta, dict):
        raise TypeError(f"Unexpected /metadata response shape: {type(meta).__name__}")
    return (
        meta.get("version", "unknown"),
        meta.get("revision", ""),
        bool(meta.get("enterprise", False)),
    )


def probe_metadata(client: GitLabClient) -> tuple[str, str, bool]:
    """Best-effort /metadata probe for explicit-GITLAB_BACKEND startup.

    An explicit backend skips detection so startup can't be broken by an
    unreachable instance or a token without /metadata access. This probe
    keeps that guarantee: any failure degrades to ("unknown", "", False)
    instead of raising.
    """
    from .client import GitLabError

    try:
        return _parse_metadata(client.get("/metadata"))
    except (GitLabError, httpx.HTTPError, TypeError, ValueError):
        return "unknown", "", False


def detect_instance(client: GitLabClient) -> InstanceInfo:
    """Probe /metadata + /projects/vcs_type_stats to determine the backend.

    GitLab returns 404 on /vcs_type_stats; Heptapod returns a dict with
    {"git": N, "hg": M, "hg_git": K}. Any other probe error propagates as
    GitLabError and aborts startup (fail-fast).
    """
    from .client import GitLabError

    version, revision, enterprise = _parse_metadata(client.get("/metadata"))

    try:
        stats = client.get("/projects/vcs_type_stats")
        backend: Backend = "heptapod"
        # A fresh Heptapod with no projects returns {}; that doesn't mean it
        # lacks hg support, just that nothing's been created yet. Heptapod
        # always supports the full git/hg/hg_git matrix at the instance level.
        vcs_types: set[str] = {"git", "hg", "hg_git"}
        if isinstance(stats, dict) and stats:
            vcs_types = set(stats.keys()) | vcs_types
    except GitLabError as e:
        if e.status == 404:
            backend = "gitlab"
            vcs_types = {"git"}
        else:
            raise

    return InstanceInfo(
        backend=backend,
        version=version,
        revision=revision,
        enterprise=enterprise,
        vcs_types_supported=vcs_types,
        url=client._base,
    )


def project_vcs_type(client: GitLabClient, project_id: str | int) -> VcsType:
    """Determine the VCS type of a specific project on a Heptapod instance.

    Tries the `vcs_type` field on /projects/:id first; falls back to probing
    the /projects/:id/hg_heptapod_config endpoint (200 → hg, 404 → git).

    Should never be called against a vanilla GitLab instance — all GitLab
    projects are git, and the caller should know this from instance.backend.
    """
    from .client import GitLabError

    # URL-encode path-style project IDs ("group/project" -> "group%2Fproject").
    # Numeric IDs are unaffected.
    encoded = quote(str(project_id), safe="")

    proj = client.get(f"/projects/{encoded}")
    if isinstance(proj, dict):
        vcs = proj.get("vcs_type")
        if vcs in _VALID_VCS:
            return cast(VcsType, vcs)
        if vcs is not None:
            raise RuntimeError(
                f"Unknown vcs_type {vcs!r} returned by API for project {project_id}"
            )

    try:
        client.get(f"/projects/{encoded}/hg_heptapod_config")
    except GitLabError as e:
        if e.status == 404:
            return "git"
        raise
    return "hg"
