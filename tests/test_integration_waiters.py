"""Integration tests for `pipelines_wait` and `jobs_wait` against a live
GitLab CE + gitlab-runner.

Prerequisites:
  npm run gitlab:up        # GitLab CE + bootstrap PAT (~3-5 min first run)
  npm run runner:up        # shell-executor gitlab-runner registered for the instance

The runner is shell-executor so jobs run directly inside the runner container.
Pipelines should reach `success`/`failed` within ~30s once the runner picks
them up; the test budgets up to 5 minutes per wait to accommodate cold-start.
"""

from __future__ import annotations

import asyncio
import time
import uuid

import pytest

pytestmark = pytest.mark.integration

_RUN_TAG = uuid.uuid4().hex[:8]
_PROJECT_NAME = f"waiter-test-{_RUN_TAG}"

# Plain YAML quirk (not GitLab-specific): inside a `- ...` list item, the
# scalar that follows the dash is parsed as a plain scalar — and `: ` (colon
# + space) anywhere in a plain scalar is the YAML mapping marker. So
# `- echo "foo: bar"` parses as `[{'echo "foo': 'bar"'}, ...]`, not as a
# string. Wrapping the whole item in single quotes (or using a `|` block
# scalar) makes the item a properly quoted scalar with no re-interpretation.
# We use single-quote wrapping for the failing-job marker so the test exercises
# the real (colon-containing) text agents would actually emit.
_CI_CONFIG = """\
stages:
  - test

passing:
  stage: test
  script:
    - echo "step 1"
    - echo "step 2"
    - echo "step 3 success"

failing:
  stage: test
  script:
    - echo "starting failing job"
    - 'echo "FAIL_MARKER: simulated failure on purpose"'
    - exit 7
"""


class FakeContext:
    """Capture progress / log notifications during waits."""

    def __init__(self):
        self.progress: list[dict] = []
        self.logs: list[dict] = []

    async def report_progress(self, progress, total=None, message=None):
        self.progress.append({
            "progress": progress, "total": total, "message": message,
        })

    async def log(self, level, message, logger_name=None):
        self.logs.append({
            "level": level, "message": message, "logger_name": logger_name,
        })


def _await_pipeline_id(agent, project_id: int, timeout: int = 60) -> int:
    """The .gitlab-ci.yml push triggers a pipeline asynchronously; poll until
    one shows up so the wait test starts from a known state."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        pipes = agent.call(
            "pipelines_all", project_id=project_id, brief=True,
        )
        if isinstance(pipes, list) and pipes:
            return pipes[0]["id"]
        time.sleep(1)
    raise AssertionError("no pipeline created within 60s of push")


@pytest.fixture(scope="module")
def project_with_pipeline(agent_gitlab):
    """Create a project, push a CI config that has one passing + one failing
    job, return (project_id, pipeline_id, jobs_by_name). Cleanup deletes
    the project at module-teardown.
    """
    result = agent_gitlab.call(
        "projects_create",
        name=_PROJECT_NAME,
        initialize_with_readme=True,
        visibility="private",
    )
    project_id = result["id"]

    agent_gitlab.call(
        "repository_files_create",
        project_id=project_id,
        file_path=".gitlab-ci.yml",
        branch="main",
        content=_CI_CONFIG,
        commit_message="Add CI config for waiter integration test",
    )

    pipeline_id = _await_pipeline_id(agent_gitlab, project_id)

    # Wait for jobs to be enumerable (gitlab needs a beat to materialise them).
    jobs_by_name: dict[str, dict] = {}
    deadline = time.time() + 30
    while time.time() < deadline:
        jobs = agent_gitlab.call(
            "jobs_all", project_id=project_id, pipeline_id=pipeline_id,
            brief=False,
        )
        if isinstance(jobs, list) and len(jobs) >= 2:
            jobs_by_name = {j["name"]: j for j in jobs}
            if "passing" in jobs_by_name and "failing" in jobs_by_name:
                break
        time.sleep(1)
    assert "passing" in jobs_by_name and "failing" in jobs_by_name, (
        f"expected passing+failing jobs in pipeline {pipeline_id}, got {list(jobs_by_name)}"
    )

    yield project_id, pipeline_id, jobs_by_name

    try:
        agent_gitlab.call("projects_remove", project_id=project_id)
    except Exception:
        pass


def test_pipelines_wait_failure_attaches_failed_logs(agent_gitlab, project_with_pipeline):
    project_id, pipeline_id, _jobs = project_with_pipeline
    ctx = FakeContext()
    wait_tool = agent_gitlab._tools["pipelines_wait"]

    result = asyncio.run(
        wait_tool(
            project_id=project_id,
            pipeline_id=pipeline_id,
            timeout=300.0,
            interval=2.0,
            include_jobs=True,
            include_failed_logs=True,
            log_tail=200,
            ctx=ctx,
        )
    )

    assert result["terminated"] is True, (
        f"pipeline did not terminate within 300s; final status={result['status']}, "
        f"polls={result['polls']}"
    )
    assert result["timed_out"] is False
    # One job failed (`exit 7`) → pipeline overall is `failed`.
    assert result["status"] == "failed", (
        f"expected pipeline status=failed; got {result['status']}"
    )
    assert result["polls"] >= 1

    # Failed jobs surface in the summary with their trailing log.
    assert isinstance(result["jobs"], list)
    failed_jobs = [j for j in result["jobs"] if j["status"] == "failed"]
    assert len(failed_jobs) == 1
    failed_job = failed_jobs[0]
    failed_log = result["failed_logs"].get(failed_job["id"])
    assert failed_log is not None, (
        f"no log attached for failed job id={failed_job['id']}; "
        f"failed_logs={result['failed_logs']}"
    )
    assert "FAIL_MARKER" in failed_log["text"], (
        f"expected failure marker in log text; got: {failed_log['text']!r}"
    )

    # Progress fired at least twice (some non-terminal status + terminal).
    # And at least one terminal-status log entry at error level.
    statuses_emitted = {
        (p.get("message") or "").rsplit(": ", 1)[-1] for p in ctx.progress
    }
    assert "failed" in statuses_emitted, (
        f"expected a 'failed' progress message; got {statuses_emitted}"
    )
    assert any(
        e["level"] == "error" and "failed" in e["message"].lower()
        for e in ctx.logs
    ), f"expected error-level log on failure; got {ctx.logs}"


def test_jobs_wait_success_streams_progress(agent_gitlab, project_with_pipeline):
    project_id, _pipeline_id, jobs_by_name = project_with_pipeline
    passing_job_id = jobs_by_name["passing"]["id"]

    ctx = FakeContext()
    wait_tool = agent_gitlab._tools["jobs_wait"]

    result = asyncio.run(
        wait_tool(
            project_id=project_id,
            job_id=passing_job_id,
            timeout=300.0,
            interval=2.0,
            include_log=True,
            log_tail=100,
            ctx=ctx,
        )
    )

    assert result["terminated"] is True, (
        f"passing job did not finish within 300s; status={result['status']}"
    )
    assert result["status"] == "success", (
        f"expected success; got {result['status']}"
    )
    assert "step 3 success" in result["log"]["text"], (
        f"expected step3 marker in log; got {result['log']['text']!r}"
    )
    # At minimum the terminal status fired a progress event.
    assert any(
        "success" in (p.get("message") or "") for p in ctx.progress
    ), f"expected success progress; got {ctx.progress}"


# ── Non-blocking start + poll against live GitLab ─────────────────────────


def test_pipelines_wait_start_and_poll_full_flow(agent_gitlab):
    """End-to-end of the L1+L2 pattern against a live GitLab.

    Creates its own project (separate from the shared `project_with_pipeline`
    fixture, which has already terminated by the time other tests touch it)
    so we exercise the genuine non-terminal start → background polling →
    terminal-via-poll path. Also reads the resource URI returned by start
    to verify the resource handler is wired through `_register_tools`.
    """
    import json
    import uuid

    from gitlab_mcp import server
    from gitlab_mcp.wait_registry import WAIT_REGISTRY

    proj_name = f"waiter-async-{uuid.uuid4().hex[:8]}"
    project = agent_gitlab.call(
        "projects_create",
        name=proj_name,
        initialize_with_readme=True,
        visibility="private",
    )
    project_id = project["id"]

    try:
        agent_gitlab.call(
            "repository_files_create",
            project_id=project_id,
            file_path=".gitlab-ci.yml",
            branch="main",
            content=_CI_CONFIG,
            commit_message="Add CI config for async waiter integration test",
        )
        pipeline_id = _await_pipeline_id(agent_gitlab, project_id)

        async def flow():
            # start: returns immediately with a wait_id + initial snapshot.
            start_snap = await agent_gitlab._tools["pipelines_wait_start"](
                project_id=project_id,
                pipeline_id=pipeline_id,
                interval=2.0,
                include_jobs=True,
                include_failed_logs=True,
                log_tail=200,
            )
            assert start_snap["wait_id"].startswith("wp-")
            assert start_snap["resource_uri"] == f"gitlab://waits/{start_snap['wait_id']}"
            assert start_snap["kind"] == "pipeline"
            # Right after start, pipeline is almost certainly still
            # pending/running (CI runners take some seconds to pick it up).
            # We don't assert on .terminated here — if GitLab raced us to a
            # terminal state on the very first poll, that's a valid outcome
            # for this test too.

            # Poll once with max_block=0 — should be near-instant.
            mid_snap = await agent_gitlab._tools["pipelines_wait_poll"](
                wait_id=start_snap["wait_id"],
                max_block=0.0,
            )
            assert mid_snap["wait_id"] == start_snap["wait_id"]

            # Now poll with a generous max_block; the background task will
            # set the done_event when it observes a terminal status.
            final_snap = await agent_gitlab._tools["pipelines_wait_poll"](
                wait_id=start_snap["wait_id"],
                max_block=300.0,
            )
            return start_snap, mid_snap, final_snap

        start_snap, mid_snap, final_snap = asyncio.run(flow())

        assert final_snap["terminated"] is True, (
            f"pipeline did not terminate within 300s; final={final_snap}"
        )
        # The failing job fixture pipeline ends with status='failed'.
        assert final_snap["status"] == "failed", (
            f"expected status=failed; got {final_snap['status']}"
        )
        assert final_snap["polls"] >= 1
        assert isinstance(final_snap["transitions"], list) and final_snap["transitions"]

        # Enrichment runs once on terminal — slim jobs + failed log.
        failed_jobs = [j for j in final_snap["jobs"] if j["status"] == "failed"]
        assert len(failed_jobs) == 1
        failed_log = final_snap["failed_logs"].get(failed_jobs[0]["id"])
        assert failed_log is not None
        assert "FAIL_MARKER" in failed_log["text"]

        # Resource read path: the wait should be readable as a resource.
        async def read_resource():
            return list(await server.mcp.read_resource(start_snap["resource_uri"]))

        chunks = asyncio.run(read_resource())
        parsed = json.loads(chunks[0].content)
        assert parsed["wait_id"] == start_snap["wait_id"]
        assert parsed["terminated"] is True
        assert parsed["status"] == "failed"

        # waits_list should enumerate this wait.
        listings = agent_gitlab.call("waits_list")
        ids = {entry["wait_id"] for entry in listings}
        assert start_snap["wait_id"] in ids

        # Cancel after terminal is a no-op (idempotent).
        async def cancel_after_terminal():
            return await agent_gitlab._tools["pipelines_wait_cancel"](
                wait_id=start_snap["wait_id"],
            )
        cancel_snap = asyncio.run(cancel_after_terminal())
        assert cancel_snap["terminated"] is True
        assert cancel_snap["status"] == "failed"
        assert "error" not in cancel_snap
    finally:
        WAIT_REGISTRY.clear()
        try:
            agent_gitlab.call("projects_remove", project_id=project_id)
        except Exception:
            pass
