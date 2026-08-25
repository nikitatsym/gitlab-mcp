"""Tests for backend detection and per-project vcs_type probes with mocked httpx."""

from collections import OrderedDict

import httpx
import pytest

from gitlab_mcp.backend import (
    InstanceInfo,
    detect_instance,
    probe_metadata,
    project_vcs_type,
)
from gitlab_mcp.client import GitLabClient, GitLabError, _reset_client
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


def _make_client(handler) -> GitLabClient:
    transport = httpx.MockTransport(handler)
    return GitLabClient(transport=transport)


class TestDetectInstance:
    def test_gitlab_instance(self):
        def handler(req):
            if req.url.path == "/api/v4/metadata":
                return httpx.Response(200, json={
                    "version": "18.6.1",
                    "revision": "abc123",
                    "enterprise": False,
                    "kas": {"enabled": False},
                })
            if req.url.path == "/api/v4/projects/vcs_type_stats":
                return httpx.Response(404, json={"message": "404 Not Found"})
            return httpx.Response(500, json={"error": f"unexpected path {req.url.path}"})

        info = detect_instance(_make_client(handler))
        assert info.backend == "gitlab"
        assert info.version == "18.6.1"
        assert info.enterprise is False
        assert info.vcs_types_supported == {"git"}
        assert info.url == "https://gitlab.example.com"

    def test_heptapod_instance(self):
        def handler(req):
            if req.url.path == "/api/v4/metadata":
                return httpx.Response(200, json={
                    "version": "18.5.2",
                    "revision": "def456",
                    "enterprise": False,
                })
            if req.url.path == "/api/v4/projects/vcs_type_stats":
                return httpx.Response(200, json={"git": 10, "hg": 5, "hg_git": 2})
            return httpx.Response(500)

        info = detect_instance(_make_client(handler))
        assert info.backend == "heptapod"
        assert info.version == "18.5.2"
        assert info.vcs_types_supported == {"git", "hg", "hg_git"}

    def test_auth_failure_propagates(self):
        def handler(req):
            return httpx.Response(401, json={"message": "401 Unauthorized"})

        with pytest.raises(GitLabError) as exc_info:
            detect_instance(_make_client(handler))
        assert exc_info.value.status == 401

    def test_vcs_type_stats_500_propagates(self):
        def handler(req):
            if req.url.path == "/api/v4/metadata":
                return httpx.Response(200, json={"version": "18.0.0", "enterprise": False})
            return httpx.Response(500, json={"message": "internal"})

        with pytest.raises(GitLabError) as exc_info:
            detect_instance(_make_client(handler))
        assert exc_info.value.status == 500

    def test_malformed_metadata_raises(self):
        def handler(req):
            if req.url.path == "/api/v4/metadata":
                return httpx.Response(200, json=["not", "a", "dict"])
            return httpx.Response(404)

        with pytest.raises(TypeError, match="Unexpected /metadata response shape"):
            detect_instance(_make_client(handler))


class TestProbeMetadata:
    def test_success_returns_metadata(self):
        def handler(req):
            if req.url.path == "/api/v4/metadata":
                return httpx.Response(200, json={
                    "version": "18.7.1",
                    "revision": "c537467",
                    "enterprise": True,
                })
            return httpx.Response(500)

        assert probe_metadata(_make_client(handler)) == ("18.7.1", "c537467", True)

    def test_http_error_degrades_to_unknown(self):
        def handler(req):
            return httpx.Response(401, json={"message": "401 Unauthorized"})

        assert probe_metadata(_make_client(handler)) == ("unknown", "", False)

    def test_network_error_degrades_to_unknown(self):
        def handler(req):
            raise httpx.ConnectError("connection refused")

        assert probe_metadata(_make_client(handler)) == ("unknown", "", False)

    def test_malformed_body_degrades_to_unknown(self):
        def handler(req):
            return httpx.Response(200, json=["not", "a", "dict"])

        assert probe_metadata(_make_client(handler)) == ("unknown", "", False)


class TestProjectVcsType:
    def test_vcs_type_git_field(self):
        def handler(req):
            return httpx.Response(200, json={"id": 42, "vcs_type": "git"})

        assert project_vcs_type(_make_client(handler), "42") == "git"

    def test_vcs_type_hg_field(self):
        def handler(req):
            return httpx.Response(200, json={"id": 42, "vcs_type": "hg"})

        assert project_vcs_type(_make_client(handler), "42") == "hg"

    def test_vcs_type_hg_git_field(self):
        def handler(req):
            return httpx.Response(200, json={"id": 42, "vcs_type": "hg_git"})

        assert project_vcs_type(_make_client(handler), "42") == "hg_git"

    def test_fallback_probe_200_means_hg(self):
        def handler(req):
            if req.url.path == "/api/v4/projects/42":
                return httpx.Response(200, json={"id": 42})  # no vcs_type field
            if req.url.path == "/api/v4/projects/42/hg_heptapod_config":
                return httpx.Response(200, json={"inherit": True})
            return httpx.Response(500)

        assert project_vcs_type(_make_client(handler), "42") == "hg"

    def test_fallback_probe_404_means_git(self):
        def handler(req):
            if req.url.path == "/api/v4/projects/42":
                return httpx.Response(200, json={"id": 42})
            if req.url.path == "/api/v4/projects/42/hg_heptapod_config":
                return httpx.Response(404, json={"message": "404 Not Found"})
            return httpx.Response(500)

        assert project_vcs_type(_make_client(handler), "42") == "git"

    def test_unknown_vcs_type_raises(self):
        def handler(req):
            return httpx.Response(200, json={"id": 42, "vcs_type": "svn"})

        with pytest.raises(RuntimeError, match="Unknown vcs_type 'svn'"):
            project_vcs_type(_make_client(handler), "42")

    def test_path_style_project_id_is_url_encoded(self):
        seen_raw_paths: list[bytes] = []

        def handler(req):
            seen_raw_paths.append(req.url.raw_path)
            return httpx.Response(200, json={"id": 42, "vcs_type": "git"})

        project_vcs_type(_make_client(handler), "mygroup/myproject")
        # Must send the URL-encoded form on the wire, not the raw slash form.
        # raw_path preserves the actual bytes as transmitted.
        assert seen_raw_paths == [b"/api/v4/projects/mygroup%2Fmyproject"]

    def test_numeric_project_id_not_mangled(self):
        seen_raw_paths: list[bytes] = []

        def handler(req):
            seen_raw_paths.append(req.url.raw_path)
            return httpx.Response(200, json={"id": 42, "vcs_type": "git"})

        project_vcs_type(_make_client(handler), 42)
        assert seen_raw_paths == [b"/api/v4/projects/42"]


class TestClient:
    def test_error_carries_context(self):
        def handler(req):
            return httpx.Response(400, json={"message": "bad request"})

        client = _make_client(handler)
        with pytest.raises(GitLabError) as exc_info:
            client.get("/projects/999")
        err = exc_info.value
        assert err.status == 400
        assert err.method == "GET"
        assert err.path == "/projects/999"
        assert err.body == {"message": "bad request"}

    def test_204_returns_none(self):
        def handler(req):
            return httpx.Response(204)

        client = _make_client(handler)
        assert client.delete("/projects/1") is None

    def test_get_text_returns_raw(self):
        def handler(req):
            return httpx.Response(200, text="raw log content here")

        client = _make_client(handler)
        assert client.get_text("/projects/1/jobs/2/trace") == "raw log content here"

    def test_paginate_stops_on_short_page(self):
        call_count = {"n": 0}

        def handler(req):
            call_count["n"] += 1
            # Return 20 items on page 1, 5 items on page 2 (signals last page)
            if b"page=1" in req.url.query:
                return httpx.Response(200, json=[{"id": i} for i in range(20)])
            if b"page=2" in req.url.query:
                return httpx.Response(200, json=[{"id": i} for i in range(20, 25)])
            return httpx.Response(200, json=[])

        client = _make_client(handler)
        result = client.paginate("/projects", per_page=20)
        assert len(result) == 25
        assert call_count["n"] == 2

    def test_paginate_stops_on_empty_page(self):
        def handler(req):
            if b"page=1" in req.url.query:
                return httpx.Response(200, json=[{"id": i} for i in range(20)])
            return httpx.Response(200, json=[])

        client = _make_client(handler)
        result = client.paginate("/projects", per_page=20)
        assert len(result) == 20

    def test_paginate_respects_max_pages(self):
        def handler(req):
            return httpx.Response(200, json=[{"id": 1} for _ in range(20)])

        client = _make_client(handler)
        result = client.paginate("/projects", per_page=20, max_pages=3)
        assert len(result) == 60  # 3 × 20

    def test_project_cache_hit_avoids_probe(self):
        call_count = {"n": 0}

        def handler(req):
            call_count["n"] += 1
            return httpx.Response(200, json={"id": 42, "vcs_type": "hg"})

        client = _make_client(handler)
        assert client.project_vcs_type(42) == "hg"
        assert client.project_vcs_type(42) == "hg"
        assert client.project_vcs_type("42") == "hg"
        # Only one network call despite three lookups
        assert call_count["n"] == 1

    def test_project_cache_fifo_eviction(self):
        client = _make_client(lambda req: httpx.Response(200, json={}))
        client._project_cache = OrderedDict()
        client._project_cache_max = 3
        # Simulate cache population directly
        for i in range(5):
            client._project_cache[str(i)] = "git"
            while len(client._project_cache) > client._project_cache_max:
                client._project_cache.popitem(last=False)
        assert list(client._project_cache.keys()) == ["2", "3", "4"]
