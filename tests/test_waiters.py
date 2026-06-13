"""Unit tests for the long-running waiters (`pipelines_wait`, `jobs_wait`).

Polling logic is exercised with `httpx.MockTransport` returning a scripted
sequence of statuses. A fake Context records `report_progress` and `log`
calls so we can assert that notifications fire on status transitions
without depending on a real MCP transport.

`asyncio.sleep` is patched to a no-op so the tests don't actually sleep.
"""

from __future__ import annotations

import asyncio
from typing import Any

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
    # Make `asyncio.sleep(...)` a no-op so polling loops don't actually wait.
    async def _instant_sleep(_secs):
        return None
    monkeypatch.setattr(asyncio, "sleep", _instant_sleep)
    yield
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


class FakeContext:
    """Minimal stand-in for mcp.server.fastmcp.Context.

    Records every `report_progress` and `log` call so tests can assert the
    waiters fired notifications on each status transition without relying
    on a live MCP transport.
    """

    def __init__(self):
        self.progress: list[dict] = []
        self.logs: list[dict] = []

    async def report_progress(self, progress, total=None, message=None):
        self.progress.append(
            {"progress": progress, "total": total, "message": message}
        )

    async def log(self, level, message, logger_name=None):
        self.logs.append(
            {"level": level, "message": message, "logger_name": logger_name}
        )


# ── helpers ────────────────────────────────────────────────────────────────


def _pipeline_response(pipeline_id: int, status: str) -> dict:
    return {
        "id": pipeline_id,
        "status": status,
        "ref": "main",
        "sha": "deadbeef" * 5,
        "web_url": f"https://gitlab.example.com/test/-/pipelines/{pipeline_id}",
        "source": "push",
        "created_at": "2026-05-24T10:00:00Z",
        "updated_at": "2026-05-24T10:00:30Z",
    }


def _job_response(job_id: int, status: str, name: str = "build") -> dict:
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


def _make_handler(scripts: dict[str, Any]):
    """Build a handler that pops from a per-path script list each call.

    scripts: mapping path → list of (status_code, json_body | text_body)
    Path matching is exact on URL path; unmatched paths return 404.
    """
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


# ── pipelines_wait ────────────────────────────────────────────────────────


class TestPipelinesWaitSuccess:
    def test_reaches_success_after_polls(self):
        scripts = {
            "/api/v4/projects/1/pipelines/42": [
                (200, _pipeline_response(42, "pending")),
                (200, _pipeline_response(42, "running")),
                (200, _pipeline_response(42, "success")),
            ],
            "/api/v4/projects/1/jobs": [
                (200, [_job_response(101, "success"), _job_response(102, "success")]),
            ],
        }
        _seed(_make_handler(scripts))
        from gitlab_mcp.tools import pipelines_wait

        ctx = FakeContext()
        result = asyncio.run(
            pipelines_wait(
                project_id=1,
                pipeline_id=42,
                timeout=60.0,
                interval=0.01,
                include_jobs=True,
                include_failed_logs=True,
                ctx=ctx,
            )
        )

        assert result["terminated"] is True
        assert result["timed_out"] is False
        assert result["status"] == "success"
        assert result["polls"] == 3
        assert result["pipeline"]["status"] == "success"
        # All three statuses produced a progress + log entry; plus terminal log.
        statuses_seen = [
            p["message"].rsplit(": ", 1)[1] for p in ctx.progress
        ]
        assert statuses_seen == ["pending", "running", "success"]
        # First log is "starting wait", then two transitions, then "finished".
        levels = [entry["level"] for entry in ctx.logs]
        assert levels[0] == "info"  # starting
        assert levels[-1] == "info"  # terminal success
        assert any("→ success" in entry["message"] for entry in ctx.logs)
        # jobs are slimmed; no failed_logs because no failed jobs.
        assert isinstance(result["jobs"], list)
        assert len(result["jobs"]) == 2
        assert result["failed_logs"] == {}

    def test_works_without_ctx(self):
        """Result is the source of truth — None ctx must not break the call."""
        scripts = {
            "/api/v4/projects/1/pipelines/42": [
                (200, _pipeline_response(42, "success")),
            ],
        }
        _seed(_make_handler(scripts))
        from gitlab_mcp.tools import pipelines_wait

        result = asyncio.run(
            pipelines_wait(
                project_id=1, pipeline_id=42,
                timeout=10.0, interval=0.01,
                include_jobs=False, include_failed_logs=False,
                ctx=None,
            )
        )
        assert result["terminated"] is True
        assert result["status"] == "success"
        assert "jobs" not in result


class TestPipelinesWaitFailure:
    def test_failure_attaches_failed_job_logs(self):
        scripts = {
            "/api/v4/projects/1/pipelines/42": [
                (200, _pipeline_response(42, "running")),
                (200, _pipeline_response(42, "failed")),
            ],
            "/api/v4/projects/1/jobs": [
                (200, [
                    _job_response(101, "success", name="lint"),
                    _job_response(102, "failed", name="test"),
                ]),
            ],
            "/api/v4/projects/1/jobs/102/trace": [
                (200, "compiling...\nFAIL: assertion error on line 7\nexit code 1\n"),
            ],
        }
        _seed(_make_handler(scripts))
        from gitlab_mcp.tools import pipelines_wait

        ctx = FakeContext()
        result = asyncio.run(
            pipelines_wait(
                project_id=1, pipeline_id=42,
                timeout=60.0, interval=0.01,
                include_jobs=True, include_failed_logs=True,
                log_tail=50,
                ctx=ctx,
            )
        )

        assert result["terminated"] is True
        assert result["status"] == "failed"
        assert result["polls"] == 2

        # Only the failed job's log is attached.
        assert set(result["failed_logs"].keys()) == {102}
        log102 = result["failed_logs"][102]
        assert "FAIL" in log102["text"]
        assert log102["total_lines"] == 3
        assert log102["truncated"] is False

        # An "error"-level log entry was emitted for the failed pipeline.
        assert any(e["level"] == "error" and "failed" in e["message"].lower()
                   for e in ctx.logs)
        # Plus a second error log about the failed-job trace attachment.
        assert any("1 failed job(s)" in e["message"] for e in ctx.logs)


class TestPipelinesWaitDiagnostics:
    """Soft hint for `failed + no jobs + no yaml_errors` — typical CI YAML
    validation signature. We do NOT auto-lint; just point at the lint tool."""

    def test_failed_with_zero_jobs_emits_lint_hint(self):
        """Pipeline reaches `failed` before any job appears → diagnostic warning
        pointing at LintCheck / LintLint."""
        pipe = _pipeline_response(42, "failed")
        # yaml_errors is null in this exact GitLab bug signature (the parser
        # rejected the config silently).
        pipe["yaml_errors"] = None
        scripts = {
            "/api/v4/projects/1/pipelines/42": [(200, pipe)],
            "/api/v4/projects/1/jobs": [(200, [])],
        }
        _seed(_make_handler(scripts))
        from gitlab_mcp.tools import pipelines_wait

        ctx = FakeContext()
        result = asyncio.run(
            pipelines_wait(
                project_id=1, pipeline_id=42,
                timeout=10.0, interval=0.01,
                include_jobs=True, include_failed_logs=True,
                ctx=ctx,
            )
        )

        assert result["status"] == "failed"
        assert result["jobs"] == []
        assert result["failed_logs"] == {}
        warnings = result.get("warnings") or []
        assert len(warnings) == 1, f"expected one warning, got {warnings}"
        msg = warnings[0]
        assert "before any jobs were materialized" in msg
        assert "LintCheck" in msg and "LintLint" in msg
        # The same warning surfaces via ctx.log at warning level.
        assert any(
            e["level"] == "warning"
            and "before any jobs were materialized" in e["message"]
            for e in ctx.logs
        ), f"expected diagnostic in ctx.logs; got {ctx.logs}"

    def test_failed_with_jobs_no_diagnostic(self):
        """Normal failed pipeline (jobs exist, at least one failed) → no hint;
        failed_logs carry the real diagnostic."""
        scripts = {
            "/api/v4/projects/1/pipelines/42": [
                (200, _pipeline_response(42, "failed")),
            ],
            "/api/v4/projects/1/jobs": [
                (200, [_job_response(101, "failed")]),
            ],
            "/api/v4/projects/1/jobs/101/trace": [
                (200, "real failure here\n"),
            ],
        }
        _seed(_make_handler(scripts))
        from gitlab_mcp.tools import pipelines_wait

        result = asyncio.run(
            pipelines_wait(
                project_id=1, pipeline_id=42,
                timeout=10.0, interval=0.01,
                include_jobs=True, include_failed_logs=True,
            )
        )
        assert result["status"] == "failed"
        assert len(result["jobs"]) == 1
        # No diagnostic because jobs are non-empty.
        assert "warnings" not in result

    def test_failed_with_yaml_errors_no_diagnostic(self):
        """If GitLab already attached yaml_errors, the caller has the answer;
        no extra hint needed."""
        pipe = _pipeline_response(42, "failed")
        pipe["yaml_errors"] = "jobs:build:script config should be a string..."
        scripts = {
            "/api/v4/projects/1/pipelines/42": [(200, pipe)],
            "/api/v4/projects/1/jobs": [(200, [])],
        }
        _seed(_make_handler(scripts))
        from gitlab_mcp.tools import pipelines_wait

        result = asyncio.run(
            pipelines_wait(
                project_id=1, pipeline_id=42,
                timeout=10.0, interval=0.01,
                include_jobs=True, include_failed_logs=True,
            )
        )
        assert result["status"] == "failed"
        assert result["jobs"] == []
        # GitLab already explained itself; no synthetic warning.
        assert "warnings" not in result

    def test_success_with_zero_jobs_no_diagnostic(self):
        """Edge case: skipped/empty-but-successful pipeline → no hint, the
        diagnostic is specific to `failed`."""
        scripts = {
            "/api/v4/projects/1/pipelines/42": [
                (200, _pipeline_response(42, "success")),
            ],
            "/api/v4/projects/1/jobs": [(200, [])],
        }
        _seed(_make_handler(scripts))
        from gitlab_mcp.tools import pipelines_wait

        result = asyncio.run(
            pipelines_wait(
                project_id=1, pipeline_id=42,
                timeout=10.0, interval=0.01,
                include_jobs=True, include_failed_logs=True,
            )
        )
        assert result["status"] == "success"
        assert "warnings" not in result

    def test_diagnostic_only_when_include_jobs_true(self):
        """Without include_jobs we don't know if there were any jobs, so we
        can't make the inference. Skip the hint."""
        pipe = _pipeline_response(42, "failed")
        pipe["yaml_errors"] = None
        scripts = {
            "/api/v4/projects/1/pipelines/42": [(200, pipe)],
        }
        _seed(_make_handler(scripts))
        from gitlab_mcp.tools import pipelines_wait

        result = asyncio.run(
            pipelines_wait(
                project_id=1, pipeline_id=42,
                timeout=10.0, interval=0.01,
                include_jobs=False, include_failed_logs=False,
            )
        )
        assert result["status"] == "failed"
        assert "warnings" not in result


class TestPipelinesWaitTimeout:
    def test_timeout_returns_summary(self):
        scripts = {
            "/api/v4/projects/1/pipelines/42": [
                (200, _pipeline_response(42, "running")),
            ],
        }
        _seed(_make_handler(scripts))
        from gitlab_mcp.tools import pipelines_wait

        ctx = FakeContext()
        # timeout=0.05 + interval=0.04 → first poll ok, second pass triggers
        # the elapsed+interval >= timeout branch.
        result = asyncio.run(
            pipelines_wait(
                project_id=1, pipeline_id=42,
                timeout=0.05, interval=0.04,
                include_jobs=False, include_failed_logs=False,
                ctx=ctx,
            )
        )

        assert result["terminated"] is False
        assert result["timed_out"] is True
        assert result["status"] == "running"
        assert result["polls"] >= 1
        # A "warning"-level log mentions the failure to reach terminal.
        assert any(e["level"] == "warning" and "terminal" in e["message"]
                   for e in ctx.logs)


class TestPipelinesWaitNotificationFiring:
    def test_progress_called_once_per_status_change(self):
        # Same "running" status across the first two polls — exactly one
        # progress notification, then a second one when status changes.
        scripts = {
            "/api/v4/projects/1/pipelines/42": [
                (200, _pipeline_response(42, "running")),
                (200, _pipeline_response(42, "running")),
                (200, _pipeline_response(42, "success")),
            ],
            "/api/v4/projects/1/jobs": [
                (200, []),
            ],
        }
        _seed(_make_handler(scripts))
        from gitlab_mcp.tools import pipelines_wait

        ctx = FakeContext()
        asyncio.run(
            pipelines_wait(
                project_id=1, pipeline_id=42,
                timeout=60.0, interval=0.01,
                include_jobs=True, include_failed_logs=False,
                ctx=ctx,
            )
        )

        # Two distinct statuses across three polls → two progress events.
        assert len(ctx.progress) == 2
        msgs = [p["message"] for p in ctx.progress]
        assert msgs[0].endswith("running")
        assert msgs[1].endswith("success")


class TestPipelinesWaitParamValidation:
    def test_rejects_zero_timeout(self):
        _seed(_make_handler({}))
        from gitlab_mcp.tools import pipelines_wait

        with pytest.raises(ValueError, match="timeout must be > 0"):
            asyncio.run(pipelines_wait(project_id=1, pipeline_id=42, timeout=0))

    def test_rejects_zero_interval(self):
        _seed(_make_handler({}))
        from gitlab_mcp.tools import pipelines_wait

        with pytest.raises(ValueError, match="interval must be > 0"):
            asyncio.run(
                pipelines_wait(project_id=1, pipeline_id=42, interval=0)
            )

    def test_rejects_negative_log_tail(self):
        _seed(_make_handler({}))
        from gitlab_mcp.tools import pipelines_wait

        with pytest.raises(ValueError, match="log_tail must be >= 0"):
            asyncio.run(
                pipelines_wait(project_id=1, pipeline_id=42, log_tail=-1)
            )


# ── jobs_wait ─────────────────────────────────────────────────────────────


class TestJobsWaitSuccess:
    def test_reaches_success(self):
        scripts = {
            "/api/v4/projects/1/jobs/101": [
                (200, _job_response(101, "pending")),
                (200, _job_response(101, "running")),
                (200, _job_response(101, "success")),
            ],
            "/api/v4/projects/1/jobs/101/trace": [
                (200, "step 1\nstep 2\ndone\n"),
            ],
        }
        _seed(_make_handler(scripts))
        from gitlab_mcp.tools import jobs_wait

        ctx = FakeContext()
        result = asyncio.run(
            jobs_wait(
                project_id=1, job_id=101,
                timeout=60.0, interval=0.01,
                include_log=True, log_tail=100,
                ctx=ctx,
            )
        )
        assert result["terminated"] is True
        assert result["status"] == "success"
        assert result["polls"] == 3
        assert result["log"]["text"] == "step 1\nstep 2\ndone\n"

        statuses = [p["message"].rsplit(": ", 1)[1] for p in ctx.progress]
        assert statuses == ["pending", "running", "success"]
        levels = [e["level"] for e in ctx.logs]
        assert levels[-1] == "info"  # terminal: success


class TestJobsWaitFailure:
    def test_failure_logs_error(self):
        scripts = {
            "/api/v4/projects/1/jobs/101": [
                (200, _job_response(101, "running")),
                (200, _job_response(101, "failed")),
            ],
            "/api/v4/projects/1/jobs/101/trace": [
                (200, "compile error\n"),
            ],
        }
        _seed(_make_handler(scripts))
        from gitlab_mcp.tools import jobs_wait

        ctx = FakeContext()
        result = asyncio.run(
            jobs_wait(
                project_id=1, job_id=101,
                timeout=60.0, interval=0.01,
                include_log=True,
                ctx=ctx,
            )
        )

        assert result["terminated"] is True
        assert result["status"] == "failed"
        assert "compile error" in result["log"]["text"]
        # Terminal log level escalates to "error" on failure.
        assert any(e["level"] == "error" and "failed" in e["message"]
                   for e in ctx.logs)


class TestJobsWaitTimeout:
    def test_timeout_returns_partial(self):
        scripts = {
            "/api/v4/projects/1/jobs/101": [
                (200, _job_response(101, "running")),
            ],
            "/api/v4/projects/1/jobs/101/trace": [
                (200, "in progress\n"),
            ],
        }
        _seed(_make_handler(scripts))
        from gitlab_mcp.tools import jobs_wait

        ctx = FakeContext()
        result = asyncio.run(
            jobs_wait(
                project_id=1, job_id=101,
                timeout=0.05, interval=0.04,
                include_log=True,
                ctx=ctx,
            )
        )
        assert result["terminated"] is False
        assert result["timed_out"] is True
        assert result["status"] == "running"
        assert any(e["level"] == "warning" and "terminal" in e["message"]
                   for e in ctx.logs)


class TestJobsWaitParamValidation:
    def test_rejects_zero_timeout(self):
        _seed(_make_handler({}))
        from gitlab_mcp.tools import jobs_wait

        with pytest.raises(ValueError, match="timeout must be > 0"):
            asyncio.run(jobs_wait(project_id=1, job_id=101, timeout=-1))


# ── Dispatch path: meta-tool + Context injection ──────────────────────────


class TestWaitViaDispatch:
    def test_ctx_threads_through_dispatch(self):
        """`server._dispatch` returns the coroutine; awaiting it must use the
        injected Context for progress/log."""
        scripts = {
            "/api/v4/projects/1/pipelines/42": [
                (200, _pipeline_response(42, "success")),
            ],
        }
        _seed(_make_handler(scripts))
        from gitlab_mcp import server

        server._register_tools()
        ctx = FakeContext()
        coro = server._dispatch(
            "PipelinesWait",
            "gitlab_read",
            {
                "project_id": 1,
                "pipeline_id": 42,
                "timeout": 10.0,
                "interval": 0.01,
                "include_jobs": False,
                "include_failed_logs": False,
            },
            ctx=ctx,  # type: ignore[arg-type]
        )
        assert asyncio.iscoroutine(coro)
        result = asyncio.run(coro)
        assert result["status"] == "success"
        # Exactly one transition (None → success) → one progress + two logs
        # (starting + terminal).
        assert len(ctx.progress) == 1
        assert any("success" in e["message"].lower() for e in ctx.logs)

    def test_ctx_not_in_help_signature(self):
        _seed(_make_handler({}))
        from gitlab_mcp import server

        server._register_tools()
        help_text = server._build_help("gitlab_read", search="PipelinesWait")
        assert "PipelinesWait" in help_text
        # `ctx` is an internal injection point, never shown to callers. Search
        # for the parameter-syntax form (`ctx:` or `, ctx,`) rather than the
        # bare word, since the rendered docstring body itself can mention
        # `ctx.report_progress(...)` etc.
        assert "ctx:" not in help_text
        assert ", ctx," not in help_text
        assert "(ctx," not in help_text
        assert "ctx)" not in help_text

    def test_ctx_param_rejected_when_passed_via_params(self):
        """Callers must not be able to inject `ctx` themselves through params."""
        _seed(_make_handler({}))
        from gitlab_mcp import server
        server._register_tools()
        with pytest.raises(ValueError, match="Extra inputs are not permitted"):
            coro = server._dispatch(
                "PipelinesWait",
                "gitlab_read",
                {"project_id": 1, "pipeline_id": 42, "ctx": "evil"},
            )
            # If coro slipped through, drain it to surface the error.
            if asyncio.iscoroutine(coro):
                asyncio.run(coro)


# ── poll-failure budget (blocking waiters) ─────────────────────────────────


class TestPollFailureBudget:
    def test_transient_blip_is_tolerated(self):
        """One 502 mid-wait: the waiter logs a warning, keeps polling, and
        the final result reports the blip without failing the call."""
        scripts = {
            "/api/v4/projects/1/pipelines/42": [
                (200, _pipeline_response(42, "running")),
                (502, {"message": "bad gateway"}),
                (200, _pipeline_response(42, "success")),
            ],
        }
        _seed(_make_handler(scripts))
        from gitlab_mcp.tools import pipelines_wait

        ctx = FakeContext()
        result = asyncio.run(
            pipelines_wait(
                project_id=1, pipeline_id=42,
                timeout=60.0, interval=0.01,
                include_jobs=False, include_failed_logs=False,
                ctx=ctx,
            )
        )
        assert result["terminated"] is True
        assert result["status"] == "success"
        assert result["polls"] == 3  # failed call counts as a poll
        assert result["poll_failures"] == 1
        assert "502" in result["last_poll_error"]
        assert any(
            "poll failed (1/3 consecutive)" in entry["message"]
            for entry in ctx.logs
        )

    def test_budget_exhaustion_raises(self):
        """max_poll_failures consecutive transient errors re-raise the last
        underlying API error."""
        from gitlab_mcp.client import GitLabError

        scripts = {
            "/api/v4/projects/1/pipelines/42": [
                (200, _pipeline_response(42, "running")),
                (502, {"message": "bad gateway"}),  # repeats forever
            ],
        }
        _seed(_make_handler(scripts))
        from gitlab_mcp.tools import pipelines_wait

        with pytest.raises(GitLabError, match="502"):
            asyncio.run(
                pipelines_wait(
                    project_id=1, pipeline_id=42,
                    timeout=60.0, interval=0.01, max_poll_failures=2,
                    include_jobs=False, include_failed_logs=False,
                )
            )

    def test_fatal_4xx_raises_immediately(self):
        """A 404 (job/pipeline gone) must not be retried even with budget
        to spare."""
        from gitlab_mcp.client import GitLabError

        calls = {"n": 0}

        def handler(req):
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(200, json=_job_response(7, "running"))
            return httpx.Response(404, json={"message": "404 Not Found"})

        _seed(handler)
        from gitlab_mcp.tools import jobs_wait

        with pytest.raises(GitLabError, match="404"):
            asyncio.run(
                jobs_wait(
                    project_id=1, job_id=7,
                    timeout=60.0, interval=0.01, max_poll_failures=5,
                    include_log=False,
                )
            )
        assert calls["n"] == 2  # exactly one failed poll, no retries

    def test_enrichment_failure_does_not_discard_result(self):
        """The wait reached terminal status; a failing jobs fetch afterwards
        must surface as enrichment_error, not destroy the result."""
        scripts = {
            "/api/v4/projects/1/pipelines/42": [
                (200, _pipeline_response(42, "success")),
            ],
            # no /jobs entry -> 404 on enrichment
        }
        _seed(_make_handler(scripts))
        from gitlab_mcp.tools import pipelines_wait

        result = asyncio.run(
            pipelines_wait(
                project_id=1, pipeline_id=42,
                timeout=10.0, interval=0.01,
                include_jobs=True, include_failed_logs=True,
            )
        )
        assert result["terminated"] is True
        assert result["status"] == "success"
        assert "failed to fetch jobs" in result["enrichment_error"]
        assert "jobs" not in result

    def test_rejects_bad_max_poll_failures(self):
        _seed(_make_handler({}))
        from gitlab_mcp.tools import pipelines_wait

        with pytest.raises(ValueError, match="max_poll_failures must be >= 1"):
            asyncio.run(
                pipelines_wait(project_id=1, pipeline_id=42, max_poll_failures=0)
            )
