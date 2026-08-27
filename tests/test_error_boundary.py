"""Expected failures reach the MCP caller as result data, not as exceptions.

An exception crossing the tool boundary is reported by MCP clients as a
contextless execution failure, so the GitLab status, body, hint, and failing
request would all be lost. Param-validation coverage lives in
test_meta_tool_dispatch.py and test_server.py; this file pins the API,
transport, async-waiter, and programming-error edges.
"""

from __future__ import annotations

import asyncio
from typing import Any, cast

import httpx
import pytest

from gitlab_mcp.backend import InstanceInfo
from gitlab_mcp.client import GitLabClient, _reset_client
from gitlab_mcp.config import _reset_settings
from gitlab_mcp.wait_registry import WAIT_REGISTRY


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    monkeypatch.setenv("GITLAB_URL", "https://gitlab.example.com")
    monkeypatch.setenv("GITLAB_TOKEN", "test-token")
    _reset_settings()
    _reset_client()
    WAIT_REGISTRY.clear()
    yield
    WAIT_REGISTRY.clear()
    _reset_settings()
    _reset_client()


def _seed_and_register(handler):
    """Install a mock-transport client and register the full tool set."""
    client = GitLabClient(transport=httpx.MockTransport(handler))
    client.instance = InstanceInfo(
        backend="gitlab",
        version="18.6.0",
        enterprise=False,
        vcs_types_supported={"git"},
        url="https://gitlab.example.com",
    )
    import gitlab_mcp.client as client_mod
    client_mod._client = client

    from gitlab_mcp import server
    server._register_tools()
    return server


def _responding(status: int, body):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=body)

    return handler


def _refusing(exc: httpx.RequestError):
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    return handler


def _group_tool(server, name: str):
    """The registered group tool, i.e. exactly what an MCP client calls."""
    return server.mcp._tool_manager._tools[name].fn


def test_api_error_keeps_status_method_path_and_body():
    server = _seed_and_register(_responding(404, {"message": "404 Project Not Found"}))

    result = server._dispatch("ProjectsShow", "gitlab_read", {"project_id": 999})

    assert result == {
        "error": (
            "GitLab API 404 GET /projects/999: "
            "{'message': '404 Project Not Found'}"
        )
    }


def test_transport_error_names_request_without_query():
    server = _seed_and_register(_refusing(httpx.ConnectError("Connection refused")))

    result = server._dispatch(
        "ProjectsAll", "gitlab_read", {"search": "must-not-leak"},
    )

    assert result == {
        "error": (
            "GitLab request failed: GET /api/v4/projects: "
            "ConnectError: Connection refused"
        )
    }
    assert "must-not-leak" not in repr(result)


def test_missing_required_param_is_reported():
    server = _seed_and_register(_responding(200, {}))

    result = server._dispatch("ProjectsShow", "gitlab_read", {})

    assert "project_id" in result["error"]


def test_registered_group_reports_invalid_help_input():
    server = _seed_and_register(_responding(200, {}))

    result = asyncio.run(
        _group_tool(server, "gitlab_read")(operation="help", params={"search": 1})
    )

    assert result == {"error": "help parameter 'search' must be a string"}


def test_async_op_failure_maps_at_await_time():
    """An async op's body runs only once awaited - outside `_dispatch`'s own guard."""
    server = _seed_and_register(_responding(200, {}))

    async def flow():
        coro = server._dispatch(
            "PipelinesWaitPoll", "gitlab_read", {"wait_id": "wp-nope"},
        )
        assert asyncio.iscoroutine(coro)
        return await coro

    result = asyncio.run(flow())

    assert "Unknown wait_id" in result["error"]


def test_waiter_snapshot_contract_is_untouched():
    """`pipelines_wait` records an initial-poll failure in its own snapshot.

    That snapshot is a richer result than `{"error": ...}` and the boundary
    must leave it alone - it is a returned value, not a raised failure.
    """
    server = _seed_and_register(_responding(500, {"message": "500 Internal Error"}))

    async def flow():
        return await server._dispatch(
            "PipelinesWait", "gitlab_read",
            {"project_id": 1, "pipeline_id": 42, "interval": 0.01},
        )

    snapshot = asyncio.run(flow())

    assert snapshot["wait_id"].startswith("wp-")
    assert "polls" in snapshot  # a full snapshot, not the boundary's error dict
    assert "initial poll failed" in snapshot["error"]
    assert "GitLab API 500" in snapshot["error"]


def test_programming_error_still_propagates(monkeypatch):
    """A bug must stay a crash: only expected failures become result data."""
    server = _seed_and_register(_responding(200, {}))

    def boom(project_id: int):
        """Synthetic op that hits a bug instead of an expected failure."""
        raise AttributeError("'NoneType' object has no attribute 'get'")

    cast(Any, boom)._mcp_params_model = server._build_params_model(boom)
    monkeypatch.setitem(server._group_ops["gitlab_read"], "ProjectsShow", boom)

    with pytest.raises(AttributeError):
        server._dispatch("ProjectsShow", "gitlab_read", {"project_id": 1})


def test_cancellation_still_propagates_from_registered_group(monkeypatch):
    server = _seed_and_register(_responding(200, {}))

    async def cancelled(project_id: int):
        raise asyncio.CancelledError

    cast(Any, cancelled)._mcp_params_model = server._build_params_model(cancelled)
    monkeypatch.setitem(server._group_ops["gitlab_read"], "ProjectsShow", cancelled)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            _group_tool(server, "gitlab_read")(
                operation="ProjectsShow", params={"project_id": 1},
            )
        )
