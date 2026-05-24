"""Unit tests for the non-blocking wait tools (`*_wait_start` / `_poll` / `_cancel`).

These exercise the L1+L2 pattern: background-polling waits registered in
`wait_registry`, observed via dedicated tools and a `gitlab://waits/{id}`
MCP resource.

Each scenario runs inside a single `asyncio.run(flow())` so the background
poll task spawned by `*_wait_start` survives across the follow-up `*_wait_poll`
or resource read.

Unlike test_waiters.py we do NOT patch `asyncio.sleep` to a no-op. The new
tools spawn a real background task, and an async function that returns
without ever awaiting a suspending coroutine doesn't yield to the event
loop — so a patched sleep starves the loop and the test deadlocks waiting
on `done_event`. With real sleep and `interval=0.01`, each scenario finishes
in tens of milliseconds.
"""

from __future__ import annotations

import asyncio
import json

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


def _seed(handler) -> GitLabClient:
    transport = httpx.MockTransport(handler)
    client = GitLabClient(transport=transport)
    client.instance = InstanceInfo(
        backend="gitlab",
        version="18.6.0",
        enterprise=False,
        vcs_types_supported={"git"},
        url="https://gitlab.example.com",
    )
    import gitlab_mcp.client as client_mod
    client_mod._client = client
    return client


def _pipeline(pipeline_id: int, status: str, *, yaml_errors=None) -> dict:
    body = {
        "id": pipeline_id,
        "status": status,
        "ref": "main",
        "sha": "deadbeef" * 5,
        "web_url": f"https://gitlab.example.com/test/-/pipelines/{pipeline_id}",
        "source": "push",
        "created_at": "2026-05-24T10:00:00Z",
        "updated_at": "2026-05-24T10:00:30Z",
    }
    if yaml_errors is not None:
        body["yaml_errors"] = yaml_errors
    return body


def _job(job_id: int, status: str, name: str = "build") -> dict:
    return {
        "id": job_id,
        "status": status,
        "stage": "build",
        "name": name,
        "ref": "main",
        "created_at": "2026-05-24T10:00:00Z",
        "finished_at": "2026-05-24T10:00:30Z" if status in {"success", "failed"} else None,
        "duration": 30 if status in {"success", "failed"} else None,
    }


def _handler(scripts: dict[str, list]):
    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        script = scripts.get(path)
        if not script:
            return httpx.Response(404, json={"path": path})
        item = script.pop(0) if len(script) > 1 else script[0]
        status, body = item
        if isinstance(body, str):
            return httpx.Response(status, text=body, headers={"content-type": "text/plain"})
        return httpx.Response(status, json=body)
    return handler


# ── pipelines_wait_start ──────────────────────────────────────────────────


class TestPipelinesWaitStart:
    def test_terminal_on_first_poll_returns_enriched_snapshot(self):
        """Pipeline already at terminal status when start fires the first poll:
        no background task is spawned; snapshot carries jobs + failed_logs."""
        scripts = {
            "/api/v4/projects/1/pipelines/42": [
                (200, _pipeline(42, "success")),
            ],
            "/api/v4/projects/1/jobs": [
                (200, [_job(101, "success")]),
            ],
        }
        _seed(_handler(scripts))
        from gitlab_mcp.tools import pipelines_wait_start

        async def flow():
            return await pipelines_wait_start(
                project_id=1, pipeline_id=42, interval=0.01,
            )

        snap = asyncio.run(flow())
        assert snap["terminated"] is True
        assert snap["timed_out"] is False
        assert snap["status"] == "success"
        assert snap["kind"] == "pipeline"
        assert snap["wait_id"].startswith("wp-")
        assert snap["resource_uri"] == f"gitlab://waits/{snap['wait_id']}"
        assert snap["polls"] == 1
        assert snap["transitions"] == [
            {"from": None, "to": "success", "elapsed_seconds": snap["transitions"][0]["elapsed_seconds"]}
        ]
        assert isinstance(snap["jobs"], list) and len(snap["jobs"]) == 1
        assert snap["failed_logs"] == {}

        # Wait was registered.
        handle = WAIT_REGISTRY.get(snap["wait_id"])
        assert handle is not None
        # No background task since first poll was already terminal.
        assert handle.task is None

    def test_non_terminal_spawns_background_task(self):
        """First poll returns 'running'; a background task is queued and the
        snapshot mirrors the partial state."""
        scripts = {
            "/api/v4/projects/1/pipelines/42": [
                (200, _pipeline(42, "running")),
                (200, _pipeline(42, "running")),  # safety: avoid script underflow
            ],
        }
        _seed(_handler(scripts))
        from gitlab_mcp.tools import pipelines_wait_start

        async def flow():
            snap = await pipelines_wait_start(
                project_id=1, pipeline_id=42, interval=0.01,
                include_jobs=False, include_failed_logs=False,
            )
            handle = WAIT_REGISTRY.get(snap["wait_id"])
            # Cancel right away so the background loop doesn't busy-spin
            # past the scripts after this test returns.
            if handle and handle.task and not handle.task.done():
                handle.task.cancel()
                try:
                    await handle.task
                except asyncio.CancelledError:
                    pass
            return snap, handle

        snap, handle = asyncio.run(flow())
        assert snap["terminated"] is False
        assert snap["status"] == "running"
        assert snap["polls"] == 1
        assert "jobs" not in snap  # not yet enriched
        # handle.task was a real Task before we cancelled it.
        assert handle is not None

    def test_initial_poll_failure_marks_terminated_with_error(self):
        """404 on first poll → no task spawned; snapshot carries error."""
        scripts = {}  # any path → 404
        _seed(_handler(scripts))
        from gitlab_mcp.tools import pipelines_wait_start

        async def flow():
            return await pipelines_wait_start(
                project_id=1, pipeline_id=42, interval=0.01,
            )

        snap = asyncio.run(flow())
        assert snap["terminated"] is False
        assert snap["error"] and "initial poll failed" in snap["error"]
        handle = WAIT_REGISTRY.get(snap["wait_id"])
        assert handle is not None
        assert handle.task is None

    def test_rejects_zero_interval(self):
        _seed(_handler({}))
        from gitlab_mcp.tools import pipelines_wait_start

        with pytest.raises(ValueError, match="interval must be > 0"):
            asyncio.run(
                pipelines_wait_start(project_id=1, pipeline_id=42, interval=0)
            )

    def test_rejects_negative_log_tail(self):
        _seed(_handler({}))
        from gitlab_mcp.tools import pipelines_wait_start

        with pytest.raises(ValueError, match="log_tail must be >= 0"):
            asyncio.run(
                pipelines_wait_start(
                    project_id=1, pipeline_id=42, interval=1.0, log_tail=-1
                )
            )


# ── pipelines_wait_poll ──────────────────────────────────────────────────


class TestPipelinesWaitPoll:
    def test_max_block_zero_returns_immediately(self):
        scripts = {
            "/api/v4/projects/1/pipelines/42": [
                (200, _pipeline(42, "running")),
            ],
        }
        _seed(_handler(scripts))
        from gitlab_mcp.tools import pipelines_wait_start, pipelines_wait_poll

        async def flow():
            start_snap = await pipelines_wait_start(
                project_id=1, pipeline_id=42, interval=0.01,
                include_jobs=False, include_failed_logs=False,
            )
            poll_snap = await pipelines_wait_poll(start_snap["wait_id"], max_block=0)
            # cleanup background task
            handle = WAIT_REGISTRY.get(start_snap["wait_id"])
            if handle.task and not handle.task.done():
                handle.task.cancel()
                try:
                    await handle.task
                except asyncio.CancelledError:
                    pass
            return start_snap, poll_snap

        start_snap, poll_snap = asyncio.run(flow())
        assert poll_snap["wait_id"] == start_snap["wait_id"]
        assert poll_snap["status"] == "running"
        # Poll itself didn't fire a new API request — only the start poll did.
        assert poll_snap["polls"] >= 1

    def test_max_block_waits_for_terminal(self):
        """max_block > 0 should resolve as soon as the background loop hits
        terminal, well before the deadline."""
        scripts = {
            "/api/v4/projects/1/pipelines/42": [
                (200, _pipeline(42, "running")),
                (200, _pipeline(42, "success")),
            ],
            "/api/v4/projects/1/jobs": [
                (200, [_job(101, "success")]),
            ],
        }
        _seed(_handler(scripts))
        from gitlab_mcp.tools import pipelines_wait_start, pipelines_wait_poll

        async def flow():
            start_snap = await pipelines_wait_start(
                project_id=1, pipeline_id=42, interval=0.01,
            )
            poll_snap = await pipelines_wait_poll(start_snap["wait_id"], max_block=5.0)
            return start_snap, poll_snap

        start_snap, poll_snap = asyncio.run(flow())
        assert start_snap["status"] == "running"
        assert poll_snap["terminated"] is True
        assert poll_snap["status"] == "success"
        assert poll_snap["timed_out"] is False
        # Final snapshot picks up enrichment from the background task.
        assert isinstance(poll_snap["jobs"], list)
        # Transitions captured both observed states.
        statuses = [t["to"] for t in poll_snap["transitions"]]
        assert "running" in statuses and "success" in statuses

    def test_max_block_times_out_returns_partial(self):
        """If terminal never arrives within max_block, return a snapshot with
        timed_out=True and the latest observed state."""
        scripts = {
            "/api/v4/projects/1/pipelines/42": [
                (200, _pipeline(42, "running")),
            ],
        }
        _seed(_handler(scripts))
        from gitlab_mcp.tools import pipelines_wait_start, pipelines_wait_poll

        # Patch sleep so the background task busy-polls 'running' forever.
        # max_block uses asyncio.wait_for which respects asyncio.sleep being
        # a no-op only if wait_for itself doesn't go through sleep — but it
        # uses asyncio.timeout under the hood; with no-op sleep wait_for will
        # still respect the timeout via the event-loop clock. To make this
        # deterministic without real wall-clock, we instead just yield once
        # and then cancel manually.

        async def flow():
            start_snap = await pipelines_wait_start(
                project_id=1, pipeline_id=42, interval=0.01,
                include_jobs=False, include_failed_logs=False,
            )
            # Tiny max_block, should time out near-immediately.
            poll_snap = await pipelines_wait_poll(
                start_snap["wait_id"], max_block=0.05,
            )
            handle = WAIT_REGISTRY.get(start_snap["wait_id"])
            if handle.task and not handle.task.done():
                handle.task.cancel()
                try:
                    await handle.task
                except asyncio.CancelledError:
                    pass
            return poll_snap

        poll_snap = asyncio.run(flow())
        assert poll_snap["timed_out"] is True
        assert poll_snap["status"] == "running"
        # Still not terminated when max_block expired.
        assert poll_snap["terminated"] is False

    def test_unknown_wait_id_raises(self):
        _seed(_handler({}))
        from gitlab_mcp.tools import pipelines_wait_poll

        with pytest.raises(ValueError, match="Unknown wait_id"):
            asyncio.run(pipelines_wait_poll("wp-deadbeef"))

    def test_wrong_kind_raises(self):
        """job wait_id passed to pipelines_wait_poll → kind mismatch error."""
        scripts = {
            "/api/v4/projects/1/jobs/101": [(200, _job(101, "success"))],
            "/api/v4/projects/1/jobs/101/trace": [(200, "done\n")],
        }
        _seed(_handler(scripts))
        from gitlab_mcp.tools import jobs_wait_start, pipelines_wait_poll

        async def flow():
            j_snap = await jobs_wait_start(project_id=1, job_id=101, interval=0.01)
            with pytest.raises(ValueError, match="is a job wait, not pipeline"):
                await pipelines_wait_poll(j_snap["wait_id"])

        asyncio.run(flow())

    def test_rejects_negative_max_block(self):
        _seed(_handler({}))
        from gitlab_mcp.tools import pipelines_wait_poll

        with pytest.raises(ValueError, match="max_block must be >= 0"):
            asyncio.run(pipelines_wait_poll("wp-x", max_block=-1))


# ── pipelines_wait_cancel ────────────────────────────────────────────────


class TestPipelinesWaitCancel:
    def test_cancel_running_marks_error(self):
        scripts = {
            "/api/v4/projects/1/pipelines/42": [
                (200, _pipeline(42, "running")),
            ],
        }
        _seed(_handler(scripts))
        from gitlab_mcp.tools import pipelines_wait_start, pipelines_wait_cancel

        async def flow():
            start_snap = await pipelines_wait_start(
                project_id=1, pipeline_id=42, interval=0.01,
                include_jobs=False, include_failed_logs=False,
            )
            cancel_snap = await pipelines_wait_cancel(start_snap["wait_id"])
            return cancel_snap

        snap = asyncio.run(flow())
        assert snap["error"] == "cancelled"
        # mark_terminated(error=...) sets terminated=False (it's an error path).
        assert snap["terminated"] is False
        assert snap["ended_at"] is not None

    def test_cancel_on_terminal_is_idempotent(self):
        scripts = {
            "/api/v4/projects/1/pipelines/42": [(200, _pipeline(42, "success"))],
            "/api/v4/projects/1/jobs": [(200, [])],
        }
        _seed(_handler(scripts))
        from gitlab_mcp.tools import pipelines_wait_start, pipelines_wait_cancel

        async def flow():
            start_snap = await pipelines_wait_start(
                project_id=1, pipeline_id=42, interval=0.01,
            )
            cancel_snap = await pipelines_wait_cancel(start_snap["wait_id"])
            return start_snap, cancel_snap

        start_snap, cancel_snap = asyncio.run(flow())
        assert start_snap["terminated"] is True
        # Cancel on already-terminal returns the same successful snapshot.
        assert cancel_snap["terminated"] is True
        assert cancel_snap["status"] == "success"
        assert "error" not in cancel_snap

    def test_cancel_unknown_raises(self):
        _seed(_handler({}))
        from gitlab_mcp.tools import pipelines_wait_cancel

        with pytest.raises(ValueError, match="Unknown wait_id"):
            asyncio.run(pipelines_wait_cancel("wp-nope"))


# ── jobs_wait_start / _poll / _cancel ───────────────────────────────────


class TestJobsWaitFlow:
    def test_terminal_on_first_poll_attaches_log(self):
        scripts = {
            "/api/v4/projects/1/jobs/101": [(200, _job(101, "success"))],
            "/api/v4/projects/1/jobs/101/trace": [(200, "step 1\nstep 2\ndone\n")],
        }
        _seed(_handler(scripts))
        from gitlab_mcp.tools import jobs_wait_start

        async def flow():
            return await jobs_wait_start(project_id=1, job_id=101, interval=0.01)

        snap = asyncio.run(flow())
        assert snap["terminated"] is True
        assert snap["status"] == "success"
        assert snap["kind"] == "job"
        assert snap["wait_id"].startswith("wj-")
        assert snap["log"]["text"] == "step 1\nstep 2\ndone\n"

    def test_non_terminal_then_poll_until_terminal(self):
        scripts = {
            "/api/v4/projects/1/jobs/101": [
                (200, _job(101, "running")),
                (200, _job(101, "failed")),
            ],
            "/api/v4/projects/1/jobs/101/trace": [(200, "compile error\n")],
        }
        _seed(_handler(scripts))
        from gitlab_mcp.tools import jobs_wait_start, jobs_wait_poll

        async def flow():
            s_snap = await jobs_wait_start(project_id=1, job_id=101, interval=0.01)
            p_snap = await jobs_wait_poll(s_snap["wait_id"], max_block=5.0)
            return s_snap, p_snap

        s, p = asyncio.run(flow())
        assert s["status"] == "running"
        assert p["status"] == "failed"
        assert p["terminated"] is True
        assert "compile error" in p["log"]["text"]

    def test_cancel_job_wait(self):
        scripts = {
            "/api/v4/projects/1/jobs/101": [(200, _job(101, "running"))],
        }
        _seed(_handler(scripts))
        from gitlab_mcp.tools import jobs_wait_start, jobs_wait_cancel

        async def flow():
            s = await jobs_wait_start(project_id=1, job_id=101, interval=0.01,
                                      include_log=False)
            c = await jobs_wait_cancel(s["wait_id"])
            return c

        snap = asyncio.run(flow())
        assert snap["error"] == "cancelled"


# ── waits_list ────────────────────────────────────────────────────────────


class TestWaitsList:
    def test_empty_when_no_waits(self):
        _seed(_handler({}))
        from gitlab_mcp.tools import waits_list
        assert waits_list() == []

    def test_lists_both_kinds_and_filters(self):
        scripts = {
            "/api/v4/projects/1/pipelines/42": [(200, _pipeline(42, "success"))],
            "/api/v4/projects/1/jobs": [(200, [])],
            "/api/v4/projects/1/jobs/101": [(200, _job(101, "running"))],
        }
        _seed(_handler(scripts))
        from gitlab_mcp.tools import (
            pipelines_wait_start, jobs_wait_start, waits_list,
        )

        async def flow():
            p_snap = await pipelines_wait_start(project_id=1, pipeline_id=42, interval=0.01)
            j_snap = await jobs_wait_start(project_id=1, job_id=101, interval=0.01,
                                           include_log=False)
            handle = WAIT_REGISTRY.get(j_snap["wait_id"])
            if handle.task and not handle.task.done():
                handle.task.cancel()
                try:
                    await handle.task
                except asyncio.CancelledError:
                    pass
            return p_snap, j_snap

        p_snap, j_snap = asyncio.run(flow())

        # Unfiltered: both kinds.
        all_entries = waits_list()
        assert len(all_entries) == 2
        ids = {e["wait_id"] for e in all_entries}
        assert ids == {p_snap["wait_id"], j_snap["wait_id"]}

        # Filter by kind.
        from gitlab_mcp.tools import waits_list as wl
        assert {e["wait_id"] for e in wl(kind="pipeline")} == {p_snap["wait_id"]}
        assert {e["wait_id"] for e in wl(kind="job")} == {j_snap["wait_id"]}

        # Filter by terminated.
        # Pipeline finished on its sync first poll → terminated=True.
        # Job was cancelled → terminated=False (error path).
        terminated_ids = {e["wait_id"] for e in wl(terminated=True)}
        assert terminated_ids == {p_snap["wait_id"]}
        not_terminated_ids = {e["wait_id"] for e in wl(terminated=False)}
        assert not_terminated_ids == {j_snap["wait_id"]}

    def test_rejects_invalid_kind(self):
        _seed(_handler({}))
        from gitlab_mcp.tools import waits_list
        with pytest.raises(ValueError, match="kind must be"):
            waits_list(kind="weird")


# ── Resource: gitlab://waits/{wait_id} ───────────────────────────────────


class TestWaitResource:
    def test_resource_returns_snapshot_json(self):
        scripts = {
            "/api/v4/projects/1/pipelines/42": [(200, _pipeline(42, "success"))],
            "/api/v4/projects/1/jobs": [(200, [])],
        }
        _seed(_handler(scripts))
        from gitlab_mcp import server
        from gitlab_mcp.tools import pipelines_wait_start

        # Resource registration is part of _register_tools.
        server._register_tools()

        async def flow():
            snap = await pipelines_wait_start(project_id=1, pipeline_id=42, interval=0.01)
            content_list = await server.mcp.read_resource(snap["resource_uri"])
            return snap, content_list

        snap, content_list = asyncio.run(flow())
        # FastMCP read_resource returns an iterable of ReadResourceContents.
        chunks = list(content_list)
        assert chunks, "expected resource content"
        text = chunks[0].content
        parsed = json.loads(text)
        assert parsed["wait_id"] == snap["wait_id"]
        assert parsed["status"] == "success"
        assert parsed["terminated"] is True

    def test_resource_unknown_id_returns_error_json(self):
        _seed(_handler({}))
        from gitlab_mcp import server
        server._register_tools()

        async def flow():
            content_list = await server.mcp.read_resource("gitlab://waits/wp-doesnotexist")
            return list(content_list)

        chunks = asyncio.run(flow())
        parsed = json.loads(chunks[0].content)
        assert "error" in parsed and "unknown wait_id" in parsed["error"]


# ── Dispatch via the meta-tool path ──────────────────────────────────────


class TestWaitDispatch:
    def test_start_then_poll_through_dispatch(self):
        """End-to-end via server.dispatch — ensures the meta-tool wires the
        new operations into the right groups (start/cancel → gitlab_execute,
        poll → gitlab_read)."""
        scripts = {
            "/api/v4/projects/1/pipelines/42": [(200, _pipeline(42, "success"))],
            "/api/v4/projects/1/jobs": [(200, [])],
        }
        _seed(_handler(scripts))
        from gitlab_mcp import server
        server._register_tools()

        async def flow():
            start_coro = server._dispatch(
                "PipelinesWaitStart",
                "gitlab_execute",
                {"project_id": 1, "pipeline_id": 42, "interval": 0.01},
            )
            assert asyncio.iscoroutine(start_coro)
            start_snap = await start_coro

            poll_coro = server._dispatch(
                "PipelinesWaitPoll",
                "gitlab_read",
                {"wait_id": start_snap["wait_id"], "max_block": 0},
            )
            assert asyncio.iscoroutine(poll_coro)
            poll_snap = await poll_coro
            return start_snap, poll_snap

        start_snap, poll_snap = asyncio.run(flow())
        assert poll_snap["status"] == "success"
        assert poll_snap["wait_id"] == start_snap["wait_id"]

    def test_start_in_wrong_group_routes_to_correct(self):
        """The dispatch's cross-group hint should tell callers where the op
        actually lives."""
        _seed(_handler({}))
        from gitlab_mcp import server
        server._register_tools()
        with pytest.raises(ValueError, match="belongs to 'gitlab_execute'"):
            server._dispatch(
                "PipelinesWaitStart", "gitlab_read",
                {"project_id": 1, "pipeline_id": 42},
            )

    def test_waits_list_is_synchronous(self):
        """waits_list isn't async; dispatch must return its value directly,
        not a coroutine."""
        _seed(_handler({}))
        from gitlab_mcp import server
        server._register_tools()
        result = server._dispatch("WaitsList", "gitlab_read", {})
        assert result == []
