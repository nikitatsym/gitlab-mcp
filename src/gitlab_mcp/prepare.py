"""Data-shaping helpers: slim views, brief caps, response verification.

All functions here are pure — no httpx, no network. Covered by
tests/test_slim.py without any fixture server.
"""

from __future__ import annotations

import os
from typing import Any, Iterable

from .config import allow_public

_BRIEF_MAX = int(os.environ.get("MCP_GITLAB_BRIEF_MAX", "100"))
_DEFAULT_LIST_LIMIT = 20

# Fields we never complain about in _verify_response — the API legitimately
# reshapes or omits them from write responses.
_SKIP_VERIFY = frozenset({
    "password",
    "token",
    "private_token",
    "access_token",
    "secret",
    "secret_token",
    "avatar",
    "file",
    "content",
})


def _ok(data: Any) -> Any:
    """Wrap None (e.g. 204 No Content) as a minimal success marker."""
    if data is None:
        return {"status": "ok"}
    return data


def _enforce_visibility(visibility: str | None) -> str | None:
    """Block non-private visibility unless --allow-public was passed at startup.

    GitLab visibility values: 'private' | 'internal' | 'public'. Default-deny
    keeps an LLM from accidentally creating publicly-visible projects/snippets.
    """
    if not allow_public() and visibility != "private":
        raise ValueError(
            "Public/internal projects not allowed. "
            "Set visibility='private' explicitly, or start the server with --allow-public."
        )
    return visibility


def _short(s: str | None, n: int = 12) -> str | None:
    """Truncate a hash/hex string to its first N characters."""
    if s is None:
        return None
    return s[:n]


def _first_line(text: str | None) -> str:
    """Return only the first line of a multi-line string."""
    if not text:
        return ""
    return text.split("\n", 1)[0]


def _brief(text: str | None, cap: int | None = None) -> str | None:
    """Cap text to BRIEF_MAX chars (or explicit cap), preserving None."""
    if text is None:
        return None
    limit = cap if cap is not None else _BRIEF_MAX
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


# ── per-resource slim helpers ─────────────────────────────────────────────


def _slim_project(p: dict) -> dict:
    return {
        "id": p.get("id"),
        "name": p.get("name"),
        "path_with_namespace": p.get("path_with_namespace"),
        "description": _brief(p.get("description")),
        "default_branch": p.get("default_branch"),
        "visibility": p.get("visibility"),
        "archived": p.get("archived"),
        "last_activity_at": p.get("last_activity_at"),
        "web_url": p.get("web_url"),
    }


def _slim_mr(mr: dict) -> dict:
    author = mr.get("author") or {}
    return {
        "iid": mr.get("iid"),
        "title": mr.get("title"),
        "state": mr.get("state"),
        "source_branch": mr.get("source_branch"),
        "target_branch": mr.get("target_branch"),
        "draft": mr.get("draft"),
        "author": author.get("username"),
        "labels": mr.get("labels") or [],
        "updated_at": mr.get("updated_at"),
        "web_url": mr.get("web_url"),
        "has_conflicts": mr.get("has_conflicts"),
    }


def _slim_issue(i: dict) -> dict:
    return {
        "iid": i.get("iid"),
        "title": i.get("title"),
        "state": i.get("state"),
        "labels": i.get("labels") or [],
        "assignees": [a.get("username") for a in (i.get("assignees") or [])],
        "updated_at": i.get("updated_at"),
        "web_url": i.get("web_url"),
    }


def _slim_commit(c: dict) -> dict:
    return {
        "id": c.get("id"),
        "short_id": c.get("short_id") or _short(c.get("id")),
        "title": _first_line(c.get("title")),
        "author_name": c.get("author_name"),
        "committed_date": c.get("committed_date"),
        "parent_count": len(c.get("parent_ids") or []),
    }


def _slim_branch(b: dict) -> dict:
    commit = b.get("commit") or {}
    return {
        "name": b.get("name"),
        "merged": b.get("merged"),
        "protected": b.get("protected"),
        "default": b.get("default"),
        "commit_id": _short(commit.get("id")),
    }


def _slim_tag(t: dict) -> dict:
    commit = t.get("commit") or {}
    return {
        "name": t.get("name"),
        "target": _short(t.get("target") or commit.get("id")),
        "message": _brief(t.get("message")),
    }


def _slim_pipeline(p: dict) -> dict:
    return {
        "id": p.get("id"),
        "status": p.get("status"),
        "ref": p.get("ref"),
        "sha": _short(p.get("sha")),
        "source": p.get("source"),
        "created_at": p.get("created_at"),
        "updated_at": p.get("updated_at"),
        "web_url": p.get("web_url"),
    }


def _slim_job(j: dict) -> dict:
    runner = j.get("runner") or {}
    return {
        "id": j.get("id"),
        "status": j.get("status"),
        "stage": j.get("stage"),
        "name": j.get("name"),
        "ref": j.get("ref"),
        "created_at": j.get("created_at"),
        "finished_at": j.get("finished_at"),
        "duration": j.get("duration"),
        "runner": runner.get("description"),
    }


def _slim_user(u: dict) -> dict:
    return {
        "id": u.get("id"),
        "username": u.get("username"),
        "name": u.get("name"),
        "state": u.get("state"),
        "web_url": u.get("web_url"),
    }


def _slim_note(n: dict) -> dict:
    author = n.get("author") or {}
    return {
        "id": n.get("id"),
        "body": _brief(n.get("body")),
        "author": author.get("username"),
        "created_at": n.get("created_at"),
        "system": n.get("system"),
    }


def _slim_file_entry(f: dict) -> dict:
    return {
        "id": f.get("id"),
        "name": f.get("name"),
        "type": f.get("type"),
        "path": f.get("path"),
        "mode": f.get("mode"),
    }


# ── Branch categorization (Heptapod naming convention) ───────────────────


def _categorize_branch(name: str) -> str:
    """Classify a branch name: git / hg_named / hg_topic.

    Heptapod exposes hg named branches as `branch/<name>` and hg topics as
    `topic/<target>/<name>`. Everything else is treated as git.
    """
    if name.startswith("topic/"):
        return "hg_topic"
    if name.startswith("branch/"):
        return "hg_named"
    return "git"


def _categorize_branches(branches: Iterable[dict]) -> dict[str, int]:
    """Summarize a branch listing by Heptapod category. Never mutates input."""
    summary = {"git": 0, "hg_named": 0, "hg_topic": 0}
    for b in branches:
        summary[_categorize_branch(b.get("name", ""))] += 1
    return summary


# ── Response verification (silent-drop detection) ────────────────────────


def _verify_response(sent: dict, received: Any) -> None:
    """Raise if a sent field is missing from the response dict.

    Detects cases where the API silently accepts but ignores unknown fields
    (common when sending EE-only params against CE). Skip credentials and
    binary upload fields via `_SKIP_VERIFY`.
    """
    if not isinstance(received, dict):
        return
    for key, value in sent.items():
        if key in _SKIP_VERIFY:
            continue
        if value is None:
            continue  # nothing was actually sent
        if key not in received:
            raise ValueError(
                f"API silently dropped {key!r}. Resource was modified but "
                f"the field was ignored. Check value format or endpoint version."
            )
