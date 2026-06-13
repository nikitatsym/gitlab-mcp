"""Tests for Phase 7 overrides: pre-flight guards, hg tools, file/job helpers."""

from __future__ import annotations

import httpx
import pytest

from gitlab_mcp.backend import InstanceInfo
from gitlab_mcp.client import GitLabClient, _reset_client
from gitlab_mcp.config import _reset_settings


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    monkeypatch.setenv("GITLAB_URL", "https://gitlab.example.com")
    monkeypatch.setenv("GITLAB_TOKEN", "test-token")
    _reset_settings()
    _reset_client()
    yield
    _reset_settings()
    _reset_client()


def _seed(backend: str, handler=None) -> GitLabClient:
    """Install a seeded client as the process singleton."""
    transport = httpx.MockTransport(handler or (lambda req: httpx.Response(404)))
    client = GitLabClient(transport=transport)
    client.instance = InstanceInfo(
        backend=backend,  # type: ignore[arg-type]
        version="18.6.0",
        enterprise=False,
        vcs_types_supported=(
            {"git", "hg", "hg_git"} if backend == "heptapod" else {"git"}
        ),
        url="https://gitlab.example.com",
    )
    import gitlab_mcp.client as client_mod
    client_mod._client = client
    return client


# ── create_merge_request pre-flight ──────────────────────────────────────


class TestCreateMergeRequestPreflight:
    def test_rejects_equal_branches(self):
        _seed("gitlab")
        from gitlab_mcp.tools import merge_requests_create

        with pytest.raises(ValueError, match="must differ"):
            merge_requests_create(
                project_id=1,
                source_branch="main",
                target_branch="main",
                title="bad",
            )

    def test_rejects_empty_source(self):
        _seed("gitlab")
        from gitlab_mcp.tools import merge_requests_create

        with pytest.raises(ValueError, match="required"):
            merge_requests_create(
                project_id=1,
                source_branch="",
                target_branch="main",
                title="bad",
            )

    def test_gitlab_allows_any_target(self):
        calls: list[httpx.Request] = []

        def handler(req):
            calls.append(req)
            return httpx.Response(201, json={"iid": 1})

        _seed("gitlab", handler)
        from gitlab_mcp.tools import merge_requests_create

        result = merge_requests_create(
            project_id=1,
            source_branch="feat/x",
            target_branch="main",
            title="feature",
        )
        assert result == {"iid": 1}
        assert len(calls) == 1

    def test_heptapod_hg_project_requires_branch_prefix(self):
        """On a hg project, target_branch without `branch/` prefix → ValueError before network."""
        def handler(req):
            if req.url.path.endswith("/projects/1"):
                return httpx.Response(200, json={"id": 1, "vcs_type": "hg"})
            # Must not reach any other endpoint.
            raise AssertionError(f"unexpected request: {req.method} {req.url.path}")

        _seed("heptapod", handler)
        from gitlab_mcp.tools import merge_requests_create

        with pytest.raises(ValueError, match="target_branch to start with 'branch/'"):
            merge_requests_create(
                project_id=1,
                source_branch="topic/default/foo",
                target_branch="default",  # missing prefix
                title="feature",
            )

    def test_heptapod_hg_project_accepts_branch_prefix(self):
        sent_body: dict = {}

        def handler(req):
            if req.url.path.endswith("/projects/1") and req.method == "GET":
                return httpx.Response(200, json={"id": 1, "vcs_type": "hg"})
            if req.url.path.endswith("/projects/1/merge_requests") and req.method == "POST":
                import json
                sent_body.update(json.loads(req.content))
                return httpx.Response(201, json={"iid": 1, **sent_body})
            return httpx.Response(404)

        _seed("heptapod", handler)
        from gitlab_mcp.tools import merge_requests_create

        merge_requests_create(
            project_id=1,
            source_branch="topic/default/foo",
            target_branch="branch/default",
            title="feature",
        )
        assert sent_body["source_branch"] == "topic/default/foo"
        assert sent_body["target_branch"] == "branch/default"

    def test_heptapod_git_project_skips_prefix_check(self):
        """A git-typed project on Heptapod accepts any target_branch."""
        def handler(req):
            if req.url.path.endswith("/projects/1") and req.method == "GET":
                return httpx.Response(200, json={"id": 1, "vcs_type": "git"})
            if req.method == "POST":
                return httpx.Response(201, json={"iid": 1})
            return httpx.Response(404)

        _seed("heptapod", handler)
        from gitlab_mcp.tools import merge_requests_create

        # No exception — git project, any target_branch is fine
        merge_requests_create(
            project_id=1,
            source_branch="feat/x",
            target_branch="main",
            title="feature",
        )


# ── projects_fork pre-flight ──────────────────────────────────────────────


class TestForkGuard:
    def test_gitlab_allows_fork(self):
        def handler(req):
            if req.method == "POST":
                return httpx.Response(201, json={"id": 2})
            return httpx.Response(404)

        _seed("gitlab", handler)
        from gitlab_mcp.tools import projects_fork

        assert projects_fork(project_id=1) == {"id": 2}

    def test_heptapod_git_project_allows_fork(self):
        def handler(req):
            if req.url.path.endswith("/projects/1") and req.method == "GET":
                return httpx.Response(200, json={"id": 1, "vcs_type": "git"})
            if req.method == "POST":
                return httpx.Response(201, json={"id": 2})
            return httpx.Response(404)

        _seed("heptapod", handler)
        from gitlab_mcp.tools import projects_fork

        assert projects_fork(project_id=1) == {"id": 2}

    def test_heptapod_hg_project_refuses_fork(self):
        def handler(req):
            if req.url.path.endswith("/projects/1") and req.method == "GET":
                return httpx.Response(200, json={"id": 1, "vcs_type": "hg"})
            raise AssertionError(f"unexpected request: {req.method} {req.url.path}")

        _seed("heptapod", handler)
        from gitlab_mcp.tools import projects_fork

        with pytest.raises(ValueError, match="not supported for Mercurial projects"):
            projects_fork(project_id=1)


# ── jobs_show_log tail ────────────────────────────────────────────────────


class TestJobLogTail:
    def test_no_tail_returns_full_text(self):
        text = "line1\nline2\nline3\n"

        def handler(req):
            return httpx.Response(200, text=text, headers={"content-type": "text/plain"})

        _seed("gitlab", handler)
        from gitlab_mcp.tools import jobs_show_log

        result = jobs_show_log(project_id=1, job_id=42)
        assert result["text"] == text
        assert result["total_lines"] == 3
        assert result["truncated"] is False

    def test_tail_truncates(self):
        text = "\n".join(f"line{i}" for i in range(100))

        def handler(req):
            return httpx.Response(200, text=text, headers={"content-type": "text/plain"})

        _seed("gitlab", handler)
        from gitlab_mcp.tools import jobs_show_log

        result = jobs_show_log(project_id=1, job_id=42, tail=5)  # type: ignore[call-arg]
        assert result["truncated"] is True
        assert result["total_lines"] == 100
        assert result["tail"] == 5
        assert "95 lines truncated" in result["text"]
        assert "line99" in result["text"]
        assert "line94" not in result["text"]

    def test_negative_tail_raises(self):
        _seed("gitlab")
        from gitlab_mcp.tools import jobs_show_log

        with pytest.raises(ValueError, match="tail must be >= 0"):
            jobs_show_log(project_id=1, job_id=42, tail=-1)  # type: ignore[call-arg]

    def test_tail_larger_than_total_returns_full(self):
        text = "only\ntwo\n"

        def handler(req):
            return httpx.Response(200, text=text, headers={"content-type": "text/plain"})

        _seed("gitlab", handler)
        from gitlab_mcp.tools import jobs_show_log

        result = jobs_show_log(project_id=1, job_id=42, tail=100)  # type: ignore[call-arg]
        assert result["truncated"] is False
        assert result["total_lines"] == 2


# ── Heptapod hg_* tools ───────────────────────────────────────────────────


class TestHgConfig:
    def test_hg_get_config(self):
        def handler(req):
            # /hgrc is the structured-config endpoint that returns defaults.
            # /hg_heptapod_config (used by hg_get_raw_hgrc) returns only
            # explicit overrides.
            assert req.url.path == "/api/v4/projects/1/hgrc"
            return httpx.Response(200, json={
                "inherit": True,
                "allow_bookmarks": False,
                "allow_multiple_heads": False,
                "auto_publish": "nothing",
            })

        _seed("heptapod", handler)
        from gitlab_mcp.tools import hg_get_config

        result = hg_get_config(project_id=1)
        assert result["inherit"] is True
        assert result["auto_publish"] == "nothing"

    def test_hg_set_config_minimal(self):
        sent_body: dict = {}

        def handler(req):
            import json
            assert req.method == "PUT"
            assert req.url.path == "/api/v4/projects/1/hgrc"
            sent_body.update(json.loads(req.content))
            return httpx.Response(200, json={"status": "ok"})

        _seed("heptapod", handler)
        from gitlab_mcp.tools import hg_set_config

        hg_set_config(project_id=1, inherit=True)
        assert sent_body == {"inherit": True}

    def test_hg_set_config_full(self):
        sent_body: dict = {}

        def handler(req):
            import json
            sent_body.update(json.loads(req.content))
            return httpx.Response(200, json={"status": "ok"})

        _seed("heptapod", handler)
        from gitlab_mcp.tools import hg_set_config

        hg_set_config(
            project_id=1,
            inherit=False,
            allow_bookmarks=True,
            allow_multiple_heads=False,
            auto_publish="all",
        )
        assert sent_body == {
            "inherit": False,
            "allow_bookmarks": True,
            "allow_multiple_heads": False,
            "auto_publish": "all",
        }

    def test_hg_set_config_invalid_auto_publish(self):
        _seed("heptapod")
        from gitlab_mcp.tools import hg_set_config

        with pytest.raises(ValueError, match="auto_publish"):
            hg_set_config(project_id=1, inherit=True, auto_publish="bogus")

    def test_hg_get_raw_hgrc(self):
        def handler(req):
            # hg_get_raw_hgrc reads explicit overrides from /hg_heptapod_config
            # (dasherized keys). For the structured config with defaults use
            # hg_get_config -> /hgrc.
            assert req.url.path == "/api/v4/projects/1/hg_heptapod_config"
            return httpx.Response(200, json={"allow-bookmarks": True})

        _seed("heptapod", handler)
        from gitlab_mcp.tools import hg_get_raw_hgrc

        result = hg_get_raw_hgrc(project_id=1)
        assert "allow-bookmarks" in result


class TestHgCreateTopicMr:
    def test_builds_refs_and_posts(self):
        calls: list[dict] = []

        def handler(req):
            if req.url.path.endswith("/projects/1") and req.method == "GET":
                return httpx.Response(200, json={"id": 1, "vcs_type": "hg"})
            if req.url.path.endswith("/projects/1/merge_requests") and req.method == "POST":
                import json
                calls.append(json.loads(req.content))
                return httpx.Response(201, json={"iid": 1, **json.loads(req.content)})
            return httpx.Response(404)

        _seed("heptapod", handler)
        from gitlab_mcp.tools import hg_create_topic_mr

        hg_create_topic_mr(
            project_id=1,
            target_hg_branch="default",
            topic_name="my-feature",
            title="Add feature",
            description="Details here",
        )
        assert len(calls) == 1
        body = calls[0]
        assert body["source_branch"] == "topic/default/my-feature"
        assert body["target_branch"] == "branch/default"
        assert body["title"] == "Add feature"
        assert body["description"] == "Details here"

    def test_refuses_git_project(self):
        def handler(req):
            if req.url.path.endswith("/projects/1") and req.method == "GET":
                return httpx.Response(200, json={"id": 1, "vcs_type": "git"})
            raise AssertionError(f"unexpected: {req.method} {req.url.path}")

        _seed("heptapod", handler)
        from gitlab_mcp.tools import hg_create_topic_mr

        with pytest.raises(ValueError, match="requires a Mercurial project"):
            hg_create_topic_mr(
                project_id=1,
                target_hg_branch="default",
                topic_name="foo",
                title="Add",
            )


class TestHgOnlyAttr:
    def test_hg_tools_tagged_heptapod_only(self):
        from gitlab_mcp.tools import (
            hg_create_topic_mr,
            hg_get_config,
            hg_get_raw_hgrc,
            hg_set_config,
        )

        for fn in (hg_get_config, hg_get_raw_hgrc, hg_set_config, hg_create_topic_mr):
            assert getattr(fn, "_heptapod_only", False) is True, f"{fn.__name__} missing _heptapod_only"


# ── repository_files_show override ────────────────────────────────────────


class TestRepositoryFilesShow:
    def test_ref_passed_as_query_param(self):
        seen_queries: list[str] = []

        def handler(req):
            seen_queries.append(str(req.url.query))
            return httpx.Response(200, json={"file_path": "README.md", "ref": "branch/default"})

        _seed("heptapod", handler)
        from gitlab_mcp.tools import repository_files_show

        repository_files_show(
            project_id=1,
            file_path="README.md",
            ref="branch/default",
        )
        # ref should be in the query string, verbatim (URL-encoding for slash)
        assert any("ref=branch" in q for q in seen_queries)

    def test_topic_ref_passthrough(self):
        seen_queries: list[str] = []

        def handler(req):
            seen_queries.append(str(req.url.query))
            return httpx.Response(200, json={})

        _seed("heptapod", handler)
        from gitlab_mcp.tools import repository_files_show

        repository_files_show(
            project_id=1,
            file_path="src/main.py",
            ref="topic/default/my-feature",
        )
        # Topic ref passes through without stripping `topic/` prefix
        assert any("ref=topic" in q for q in seen_queries)


class TestRepositoryFilesShowRaw:
    def test_returns_raw_text(self):
        def handler(req):
            return httpx.Response(200, text="raw file body", headers={"content-type": "text/plain"})

        _seed("gitlab", handler)
        from gitlab_mcp.tools import repository_files_show_raw

        result = repository_files_show_raw(
            project_id=1, file_path="README.md", ref="main"
        )
        assert result == "raw file body"


# ── Visibility guard (--allow-public) ─────────────────────────────────────


class TestVisibilityGuard:
    def teardown_method(self):
        # Make sure we don't leak --allow-public state into other tests.
        from gitlab_mcp.config import set_allow_public
        set_allow_public(False)

    def test_default_blocks_public_project(self):
        _seed("gitlab")
        from gitlab_mcp.tools import projects_create

        with pytest.raises(ValueError, match="Public/internal projects not allowed"):
            projects_create(name="x", visibility="public")

    def test_default_blocks_internal_project(self):
        _seed("gitlab")
        from gitlab_mcp.tools import projects_create

        with pytest.raises(ValueError, match="Public/internal projects not allowed"):
            projects_create(name="x", visibility="internal")

    def test_default_allows_private(self):
        sent: dict = {}

        def handler(req):
            import json
            sent.update(json.loads(req.content))
            return httpx.Response(201, json={"id": 1, **sent})

        _seed("gitlab", handler)
        from gitlab_mcp.tools import projects_create

        result = projects_create(name="x")
        assert result["visibility"] == "private"
        assert sent["visibility"] == "private"

    def test_allow_public_permits_public(self):
        sent: dict = {}

        def handler(req):
            import json
            sent.update(json.loads(req.content))
            return httpx.Response(201, json={"id": 1, **sent})

        _seed("gitlab", handler)
        from gitlab_mcp.config import set_allow_public
        set_allow_public(True)
        from gitlab_mcp.tools import projects_create

        result = projects_create(name="x", visibility="public")
        assert result["visibility"] == "public"

    def test_groups_create_default_private(self):
        sent: dict = {}

        def handler(req):
            import json
            sent.update(json.loads(req.content))
            return httpx.Response(201, json={"id": 1, **sent})

        _seed("gitlab", handler)
        from gitlab_mcp.tools import groups_create

        groups_create(name="x", path="x")
        assert sent["visibility"] == "private"

    def test_groups_create_blocks_public(self):
        _seed("gitlab")
        from gitlab_mcp.tools import groups_create

        with pytest.raises(ValueError, match="Public/internal"):
            groups_create(name="x", path="x", visibility="public")

    def test_snippets_create_blocks_public(self):
        _seed("gitlab")
        from gitlab_mcp.tools import snippets_create

        with pytest.raises(ValueError, match="Public/internal"):
            snippets_create(title="x", visibility="public")

    def test_projects_edit_blocks_visibility_change_to_public(self):
        _seed("gitlab")
        from gitlab_mcp.tools import projects_edit

        with pytest.raises(ValueError, match="Public/internal"):
            projects_edit(project_id=1, visibility="public")

    def test_projects_edit_without_visibility_passes_through(self):
        sent: dict = {}

        def handler(req):
            import json
            sent.update(json.loads(req.content))
            return httpx.Response(200, json={"id": 1, "name": "renamed", **sent})

        _seed("gitlab", handler)
        from gitlab_mcp.tools import projects_edit

        # No visibility — guard not triggered, edit goes through.
        projects_edit(project_id=1, name="renamed")
        assert sent == {"name": "renamed"}

    def test_project_snippets_create_blocks_public(self):
        _seed("gitlab")
        from gitlab_mcp.tools import project_snippets_create

        with pytest.raises(ValueError, match="Public/internal"):
            project_snippets_create(project_id=1, title="x", visibility="public")
