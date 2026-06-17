"""Unit tests for the non-blocking wait tools (`*_wait` / `_poll` / `_cancel`).

These exercise the L1+L2 pattern: background-polling waits registered in
`wait_registry`, observed via dedicated tools and a `gitlab://waits/{id}`
MCP resource.

Each scenario runs inside a single `asyncio.run(flow())` so the background
poll task spawned by `*_wait` survives across the follow-up `*_wait_poll`
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
from typing import Any

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


def _handler(scripts: dict[str, Any]):
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


# ── pipelines_wait ──────────────────────────────────────────────────


class TestPipelinesWait:
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
        from gitlab_mcp.tools import pipelines_wait

        async def flow():
            return await pipelines_wait(
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
        from gitlab_mcp.tools import pipelines_wait

        async def flow():
            snap = await pipelines_wait(
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
        scripts: dict[str, Any] = {}  # any path → 404
        _seed(_handler(scripts))
        from gitlab_mcp.tools import pipelines_wait

        async def flow():
            return await pipelines_wait(
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
        from gitlab_mcp.tools import pipelines_wait

        with pytest.raises(ValueError, match="interval must be > 0"):
            asyncio.run(
                pipelines_wait(project_id=1, pipeline_id=42, interval=0)
            )

    def test_rejects_negative_log_tail(self):
        _seed(_handler({}))
        from gitlab_mcp.tools import pipelines_wait

        with pytest.raises(ValueError, match="log_tail must be >= 0"):
            asyncio.run(
                pipelines_wait(
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
        from gitlab_mcp.tools import pipelines_wait, pipelines_wait_poll

        async def flow():
            start_snap = await pipelines_wait(
                project_id=1, pipeline_id=42, interval=0.01,
                include_jobs=False, include_failed_logs=False,
            )
            poll_snap = await pipelines_wait_poll(start_snap["wait_id"], max_block=0)
            # cleanup background task
            handle = WAIT_REGISTRY.get(start_snap["wait_id"])
            assert handle is not None
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
        from gitlab_mcp.tools import pipelines_wait, pipelines_wait_poll

        async def flow():
            start_snap = await pipelines_wait(
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
        from gitlab_mcp.tools import pipelines_wait, pipelines_wait_poll

        # Patch sleep so the background task busy-polls 'running' forever.
        # max_block uses asyncio.wait_for which respects asyncio.sleep being
        # a no-op only if wait_for itself doesn't go through sleep — but it
        # uses asyncio.timeout under the hood; with no-op sleep wait_for will
        # still respect the timeout via the event-loop clock. To make this
        # deterministic without real wall-clock, we instead just yield once
        # and then cancel manually.

        async def flow():
            start_snap = await pipelines_wait(
                project_id=1, pipeline_id=42, interval=0.01,
                include_jobs=False, include_failed_logs=False,
            )
            # Tiny max_block, should time out near-immediately.
            poll_snap = await pipelines_wait_poll(
                start_snap["wait_id"], max_block=0.05,
            )
            handle = WAIT_REGISTRY.get(start_snap["wait_id"])
            assert handle is not None
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
        from gitlab_mcp.tools import jobs_wait, pipelines_wait_poll

        async def flow():
            j_snap = await jobs_wait(project_id=1, job_id=101, interval=0.01)
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
        from gitlab_mcp.tools import pipelines_wait, pipelines_wait_cancel

        async def flow():
            start_snap = await pipelines_wait(
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
        from gitlab_mcp.tools import pipelines_wait, pipelines_wait_cancel

        async def flow():
            start_snap = await pipelines_wait(
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


# ── jobs_wait / _poll / _cancel ───────────────────────────────────


class TestJobsWaitFlow:
    def test_terminal_on_first_poll_attaches_log(self):
        scripts = {
            "/api/v4/projects/1/jobs/101": [(200, _job(101, "success"))],
            "/api/v4/projects/1/jobs/101/trace": [(200, "step 1\nstep 2\ndone\n")],
        }
        _seed(_handler(scripts))
        from gitlab_mcp.tools import jobs_wait

        async def flow():
            return await jobs_wait(project_id=1, job_id=101, interval=0.01)

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
        from gitlab_mcp.tools import jobs_wait, jobs_wait_poll

        async def flow():
            s_snap = await jobs_wait(project_id=1, job_id=101, interval=0.01)
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
        from gitlab_mcp.tools import jobs_wait, jobs_wait_cancel

        async def flow():
            s = await jobs_wait(project_id=1, job_id=101, interval=0.01,
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
            pipelines_wait, jobs_wait, waits_list,
        )

        async def flow():
            p_snap = await pipelines_wait(project_id=1, pipeline_id=42, interval=0.01)
            j_snap = await jobs_wait(project_id=1, job_id=101, interval=0.01,
                                           include_log=False)
            handle = WAIT_REGISTRY.get(j_snap["wait_id"])
            assert handle is not None
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
        from gitlab_mcp.tools import pipelines_wait

        # Resource registration is part of _register_tools.
        server._register_tools()

        async def flow():
            snap = await pipelines_wait(project_id=1, pipeline_id=42, interval=0.01)
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
        wait operations into gitlab_read (waits only GET the service; see
        the group-assignment note in tools.py)."""
        scripts = {
            "/api/v4/projects/1/pipelines/42": [(200, _pipeline(42, "success"))],
            "/api/v4/projects/1/jobs": [(200, [])],
        }
        _seed(_handler(scripts))
        from gitlab_mcp import server
        server._register_tools()

        async def flow():
            start_coro = server._dispatch(
                "PipelinesWait",
                "gitlab_read",
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
        actually lives — including agents that look for waits in execute,
        where they lived before the move to gitlab_read."""
        _seed(_handler({}))
        from gitlab_mcp import server
        server._register_tools()
        with pytest.raises(ValueError, match="belongs to 'gitlab_read'"):
            server._dispatch(
                "PipelinesWait", "gitlab_execute",
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


# ── resilience: poll-failure budget and max_lifetime ──────────────────────


class TestWaitResilience:
    def test_transient_poll_failure_recovers(self):
        """A single 502 mid-wait counts against the budget but the wait
        carries on and still reaches the terminal status."""
        scripts = {
            "/api/v4/projects/1/pipelines/42": [
                (200, _pipeline(42, "running")),   # initial inline poll
                (502, {"message": "bad gateway"}),  # transient blip
                (200, _pipeline(42, "success")),
            ],
            "/api/v4/projects/1/jobs": [
                (200, [_job(101, "success")]),
            ],
        }
        _seed(_handler(scripts))
        from gitlab_mcp.tools import pipelines_wait, pipelines_wait_poll

        async def flow():
            snap = await pipelines_wait(
                project_id=1, pipeline_id=42, interval=0.01,
            )
            return await pipelines_wait_poll(snap["wait_id"], max_block=5.0)

        poll = asyncio.run(flow())
        assert poll["terminated"] is True
        assert poll["status"] == "success"
        assert poll["poll_failures"] == 1
        assert "502" in poll["last_poll_error"]

    def test_consecutive_failures_exhaust_budget(self):
        """max_poll_failures consecutive transient errors stop the wait with
        an error mentioning the failure count."""
        scripts = {
            "/api/v4/projects/1/pipelines/42": [
                (200, _pipeline(42, "running")),   # initial inline poll
                (502, {"message": "bad gateway"}),  # repeats forever
            ],
        }
        _seed(_handler(scripts))
        from gitlab_mcp.tools import pipelines_wait, pipelines_wait_poll

        async def flow():
            snap = await pipelines_wait(
                project_id=1, pipeline_id=42, interval=0.01,
                max_poll_failures=2, include_jobs=False,
            )
            return await pipelines_wait_poll(snap["wait_id"], max_block=5.0)

        poll = asyncio.run(flow())
        assert poll["terminated"] is False
        assert poll["timed_out"] is False
        assert "2 consecutive failures" in poll["error"]
        assert poll["poll_failures"] == 2

    def test_fatal_4xx_stops_wait_immediately(self):
        """A 404 mid-wait (pipeline deleted) must not burn through the
        budget - it fails on the first occurrence."""
        scripts = {
            "/api/v4/projects/1/pipelines/42": [
                (200, _pipeline(42, "running")),    # initial inline poll
                (404, {"message": "404 Not Found"}),  # repeats forever
            ],
        }
        _seed(_handler(scripts))
        from gitlab_mcp.tools import pipelines_wait, pipelines_wait_poll

        async def flow():
            snap = await pipelines_wait(
                project_id=1, pipeline_id=42, interval=0.01,
                max_poll_failures=5, include_jobs=False,
            )
            return await pipelines_wait_poll(snap["wait_id"], max_block=5.0)

        poll = asyncio.run(flow())
        assert poll["terminated"] is False
        assert poll["poll_failures"] == 1
        assert "404" in poll["error"]

    def test_max_lifetime_marks_timed_out(self):
        """A wait whose target never terminates gives up after max_lifetime
        with timed_out=True and a self-explanatory error."""
        scripts = {
            "/api/v4/projects/1/pipelines/42": [
                (200, _pipeline(42, "running")),
            ],
        }
        _seed(_handler(scripts))
        from gitlab_mcp.tools import pipelines_wait, pipelines_wait_poll

        async def flow():
            snap = await pipelines_wait(
                project_id=1, pipeline_id=42, interval=0.01,
                max_lifetime=0.05, include_jobs=False,
            )
            return await pipelines_wait_poll(snap["wait_id"], max_block=5.0)

        poll = asyncio.run(flow())
        assert poll["timed_out"] is True
        assert poll["terminated"] is False
        assert "max_lifetime" in poll["error"]
        assert poll["status"] == "running"

    def test_rejects_bad_resilience_params(self):
        _seed(_handler({}))
        from gitlab_mcp.tools import pipelines_wait, jobs_wait

        with pytest.raises(ValueError, match="max_poll_failures must be >= 1"):
            asyncio.run(pipelines_wait(
                project_id=1, pipeline_id=42, max_poll_failures=0,
            ))
        with pytest.raises(ValueError, match="max_lifetime must be >= 0"):
            asyncio.run(jobs_wait(
                project_id=1, job_id=7, max_lifetime=-1,
            ))


# ── Reverse-stream push (transitions, stages, terminal notification) ───────


class FakeSession:
    """Captures send_log_message calls; optionally fails to mimic a closed
    server->client channel."""

    def __init__(self, fail: bool = False):
        self.messages: list[dict] = []
        self.fail = fail

    async def send_log_message(self, level, data, logger=None):
        if self.fail:
            raise RuntimeError("stream closed")
        self.messages.append({"level": level, "data": data, "logger": logger})


class FakeContext:
    def __init__(self, session=None):
        self._session = session if session is not None else FakeSession()

    @property
    def session(self):
        return self._session


def _events(session):
    return [m["data"]["event"] for m in session.messages]


def _terminal(session):
    return [m for m in session.messages if m["data"]["event"] == "wait_terminal"][-1]


async def _drain(wait_id):
    """Await the background task so its terminal push completes before asserts.

    `*_wait_poll(max_block)` returns when done_event fires (set in mark_*),
    which is just before `_push_terminal` runs; awaiting the task closes that
    gap deterministically."""
    handle = WAIT_REGISTRY.get(wait_id)
    if handle is not None and handle.task is not None:
        try:
            await handle.task
        except asyncio.CancelledError:
            pass


class TestReverseStreamPipeline:
    def test_streams_transitions_stages_and_terminal(self):
        jobs = [
            {"id": 101, "status": "success", "stage": "build", "name": "compile"},
            {"id": 102, "status": "success", "stage": "test", "name": "unit"},
        ]
        scripts = {
            "/api/v4/projects/1/pipelines/42": [
                (200, _pipeline(42, "running")),
                (200, _pipeline(42, "success")),
            ],
            "/api/v4/projects/1/jobs": [(200, jobs)],
        }
        _seed(_handler(scripts))
        from gitlab_mcp.tools import pipelines_wait, pipelines_wait_poll

        session = FakeSession()
        ctx = FakeContext(session)

        async def flow():
            start = await pipelines_wait(
                project_id=1, pipeline_id=42, interval=0.01, ctx=ctx
            )
            poll = await pipelines_wait_poll(start["wait_id"], max_block=5.0)
            await _drain(start["wait_id"])
            return poll

        poll = asyncio.run(flow())
        events = _events(session)
        assert "wait_transition" in events
        assert "wait_stage_transition" in events
        assert events[-1] == "wait_terminal"
        term = _terminal(session)["data"]
        assert term["status"] == "success"
        assert term["terminated"] is True
        assert [s["name"] for s in term["stages"]] == ["build", "test"]
        # snapshot reports delivery + stages too (poll fallback)
        assert poll["notified"] is True
        assert poll["stages"]

    def test_terminal_push_on_error_budget(self):
        scripts = {
            "/api/v4/projects/1/pipelines/42": [
                (200, _pipeline(42, "running")),
                (502, {"message": "bad gateway"}),
            ],
        }
        _seed(_handler(scripts))
        from gitlab_mcp.tools import pipelines_wait, pipelines_wait_poll

        session = FakeSession()
        ctx = FakeContext(session)

        async def flow():
            start = await pipelines_wait(
                project_id=1, pipeline_id=42, interval=0.01,
                max_poll_failures=1, include_jobs=False, ctx=ctx,
            )
            poll = await pipelines_wait_poll(start["wait_id"], max_block=5.0)
            await _drain(start["wait_id"])
            return poll

        poll = asyncio.run(flow())
        term = _terminal(session)
        assert term["level"] == "error"
        assert term["data"]["terminated"] is False
        assert "poll failed" in (term["data"]["error"] or "")
        assert poll["notified"] is True

    def test_terminal_push_on_timeout(self):
        scripts = {
            "/api/v4/projects/1/pipelines/42": [(200, _pipeline(42, "running"))],
        }
        _seed(_handler(scripts))
        from gitlab_mcp.tools import pipelines_wait, pipelines_wait_poll

        session = FakeSession()
        ctx = FakeContext(session)

        async def flow():
            start = await pipelines_wait(
                project_id=1, pipeline_id=42, interval=0.01,
                max_lifetime=0.05, include_jobs=False, ctx=ctx,
            )
            poll = await pipelines_wait_poll(start["wait_id"], max_block=5.0)
            await _drain(start["wait_id"])
            return poll

        poll = asyncio.run(flow())
        term = _terminal(session)
        assert term["level"] == "warning"
        assert term["data"]["timed_out"] is True
        assert poll["timed_out"] is True

    def test_closed_channel_swallowed_and_recorded(self):
        scripts = {
            "/api/v4/projects/1/pipelines/42": [(200, _pipeline(42, "success"))],
            "/api/v4/projects/1/jobs": [(200, [])],
        }
        _seed(_handler(scripts))
        from gitlab_mcp.tools import pipelines_wait

        session = FakeSession(fail=True)
        ctx = FakeContext(session)

        # Inline-terminal path: must not raise even though every send fails.
        snap = asyncio.run(
            pipelines_wait(project_id=1, pipeline_id=42, interval=0.01, ctx=ctx)
        )
        assert snap["terminated"] is True
        assert snap.get("notified") is not True
        assert "stream closed" in (snap.get("notify_error") or "")

    def test_cancel_emits_no_terminal_push(self):
        scripts = {
            "/api/v4/projects/1/pipelines/42": [(200, _pipeline(42, "running"))],
        }
        _seed(_handler(scripts))
        from gitlab_mcp.tools import pipelines_wait, pipelines_wait_cancel

        session = FakeSession()
        ctx = FakeContext(session)

        async def flow():
            start = await pipelines_wait(
                project_id=1, pipeline_id=42, interval=0.01,
                include_jobs=False, ctx=ctx,
            )
            return await pipelines_wait_cancel(start["wait_id"])

        snap = asyncio.run(flow())
        assert snap["error"] == "cancelled"
        assert "wait_terminal" not in _events(session)

    def test_no_ctx_runs_push_free(self):
        scripts = {
            "/api/v4/projects/1/pipelines/42": [(200, _pipeline(42, "success"))],
            "/api/v4/projects/1/jobs": [(200, [])],
        }
        _seed(_handler(scripts))
        from gitlab_mcp.tools import pipelines_wait

        snap = asyncio.run(
            pipelines_wait(project_id=1, pipeline_id=42, interval=0.01)
        )
        assert snap["terminated"] is True
        assert "notified" not in snap
        assert "notify_error" not in snap


class TestReverseStreamJob:
    def test_job_streams_transition_and_terminal_without_stages(self):
        scripts = {
            "/api/v4/projects/1/jobs/101": [
                (200, _job(101, "running")),
                (200, _job(101, "success")),
            ],
            "/api/v4/projects/1/jobs/101/trace": [(200, "ok\n")],
        }
        _seed(_handler(scripts))
        from gitlab_mcp.tools import jobs_wait, jobs_wait_poll

        session = FakeSession()
        ctx = FakeContext(session)

        async def flow():
            start = await jobs_wait(project_id=1, job_id=101, interval=0.01, ctx=ctx)
            poll = await jobs_wait_poll(start["wait_id"], max_block=5.0)
            await _drain(start["wait_id"])
            return poll

        poll = asyncio.run(flow())
        events = _events(session)
        assert "wait_transition" in events
        assert "wait_stage_transition" not in events  # jobs have no stages
        assert events[-1] == "wait_terminal"
        term = _terminal(session)["data"]
        assert term["status"] == "success"
        assert "stages" not in term
        assert poll["notified"] is True


class TestStageStream:
    def test_stage_transitions_and_ordered_terminal_summary(self):
        jobs_running = [
            {"id": 101, "status": "running", "stage": "build", "name": "compile"},
            {"id": 102, "status": "created", "stage": "test", "name": "unit"},
        ]
        jobs_done = [
            {"id": 101, "status": "success", "stage": "build", "name": "compile"},
            {"id": 102, "status": "failed", "stage": "test", "name": "unit"},
        ]
        scripts = {
            "/api/v4/projects/1/pipelines/42": [
                (200, _pipeline(42, "running")),
                (200, _pipeline(42, "failed")),
            ],
            "/api/v4/projects/1/jobs": [(200, jobs_running), (200, jobs_done)],
            "/api/v4/projects/1/jobs/102/trace": [(200, "boom\n")],
        }
        _seed(_handler(scripts))
        from gitlab_mcp.tools import pipelines_wait, pipelines_wait_poll

        session = FakeSession()
        ctx = FakeContext(session)

        async def flow():
            start = await pipelines_wait(
                project_id=1, pipeline_id=42, interval=0.01, ctx=ctx
            )
            poll = await pipelines_wait_poll(start["wait_id"], max_block=5.0)
            await _drain(start["wait_id"])
            return poll

        poll = asyncio.run(flow())
        stage_events = [
            m["data"] for m in session.messages
            if m["data"]["event"] == "wait_stage_transition"
        ]
        assert any(e["stage"] == "build" for e in stage_events)
        assert any(e["stage"] == "test" for e in stage_events)
        term = _terminal(session)["data"]
        assert [s["name"] for s in term["stages"]] == ["build", "test"]
        statuses = {s["name"]: s["status"] for s in term["stages"]}
        assert statuses == {"build": "success", "test": "failed"}
        assert poll["stages"]


class TestWaitCtxContract:
    def test_ctx_absent_from_help(self):
        _seed(_handler({}))
        from gitlab_mcp import server
        server._register_tools()
        help_text = server._build_help("gitlab_read", search="pipelines_wait")
        assert "PipelinesWait(" in help_text
        assert "ctx" not in help_text

    def test_ctx_rejected_when_passed_via_params(self):
        _seed(_handler({}))
        from gitlab_mcp import server
        server._register_tools()
        # ctx is injected by the harness, never a caller param: extra=forbid.
        with pytest.raises(ValueError):
            server._dispatch(
                "PipelinesWait", "gitlab_read",
                {"project_id": 1, "pipeline_id": 42, "ctx": "nope"},
            )

    def test_ctx_threads_through_dispatch(self):
        scripts = {
            "/api/v4/projects/1/pipelines/42": [(200, _pipeline(42, "success"))],
            "/api/v4/projects/1/jobs": [(200, [])],
        }
        _seed(_handler(scripts))
        from gitlab_mcp import server
        server._register_tools()
        session = FakeSession()
        ctx = FakeContext(session)

        async def flow():
            coro = server._dispatch(
                "PipelinesWait", "gitlab_read",
                {"project_id": 1, "pipeline_id": 42, "interval": 0.01},
                ctx,  # type: ignore[arg-type]
            )
            return await coro

        snap = asyncio.run(flow())
        assert snap["terminated"] is True
        assert any(
            m["data"]["event"] == "wait_terminal" for m in session.messages
        )


class TestEnrichmentAndDiagnostics:
    def test_yaml_validation_diagnostic_warning(self):
        scripts = {
            "/api/v4/projects/1/pipelines/42": [(200, _pipeline(42, "failed"))],
            "/api/v4/projects/1/jobs": [(200, [])],
        }
        _seed(_handler(scripts))
        from gitlab_mcp.tools import pipelines_wait

        snap = asyncio.run(
            pipelines_wait(project_id=1, pipeline_id=42, interval=0.01)
        )
        assert snap["terminated"] is True and snap["status"] == "failed"
        assert any(".gitlab-ci.yml" in w for w in snap.get("warnings", []))

    def test_enrichment_error_surfaced(self):
        scripts = {
            "/api/v4/projects/1/pipelines/42": [(200, _pipeline(42, "success"))],
            "/api/v4/projects/1/jobs": [(500, {"message": "boom"})],
        }
        _seed(_handler(scripts))
        from gitlab_mcp.tools import pipelines_wait

        snap = asyncio.run(
            pipelines_wait(project_id=1, pipeline_id=42, interval=0.01)
        )
        assert snap["terminated"] is True
        assert "enrichment_error" in snap
