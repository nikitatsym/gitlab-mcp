"""In-memory registry of long-running wait operations.

Backs the `pipelines_wait` / `pipelines_wait_poll` and
`jobs_wait` / `jobs_wait_poll` tools. Each registered wait is a
`WaitHandle` carrying:

- identity (wait_id, kind, project_id, target_id)
- current observed state (status, terminated, polls, transitions)
- the latest API payload (`pipelines_show` / `jobs_show`)
- final enrichment (`jobs`, `failed_logs`, `log`, `warnings`) once terminal
- a background `asyncio.Task` polling on a schedule
- an `asyncio.Event` that fires when terminal so `poll(max_block=...)`
  can wait efficiently instead of busy-checking

The registry is a module-level singleton: handles live for the lifetime
of the server process. `reap_old()` opportunistically drops handles that
have been terminal for more than `_DEFAULT_TTL_SECONDS` so the dict
doesn't grow without bound in long-lived servers.

Concurrency note: snapshot read and field updates happen on the same
asyncio loop. The HTTP request inside a poll runs in a worker thread
(`asyncio.to_thread`), but its result is applied to the handle back on
the loop, and the poll task is the sole writer to mutable state - so no
lock is needed; reads from other coroutines see a consistent snapshot
at every await point.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from typing import Any


# Pipeline / job terminal statuses (see tools.py for the full rationale).
TERMINAL_STATUSES = frozenset(
    {"success", "failed", "canceled", "skipped", "manual", "scheduled"}
)


_DEFAULT_TTL_SECONDS = 3600  # 1 hour after termination — long enough for any
                             # reasonable agent flow to re-fetch the result.


class WaitHandle:
    """One long-running wait operation."""

    __slots__ = (
        "wait_id",
        "kind",
        "project_id",
        "target_id",
        "options",
        "status",
        "terminated",
        "timed_out",
        "polls",
        "poll_failures",
        "last_poll_error",
        "started_at",
        "ended_at",
        "last_payload",
        "transitions",
        "final_extras",
        "error",
        "task",
        "done_event",
        "notify_session",
        "notified",
        "notify_error",
        "stages",
    )

    def __init__(
        self,
        wait_id: str,
        kind: str,
        project_id: str | int,
        target_id: str | int,
        options: dict[str, Any],
    ):
        self.wait_id = wait_id
        self.kind = kind  # "pipeline" | "job"
        self.project_id = project_id
        self.target_id = target_id
        self.options = options

        self.status: str | None = None
        self.terminated: bool = False
        self.timed_out: bool = False
        self.polls: int = 0
        self.poll_failures: int = 0
        self.last_poll_error: str | None = None
        self.started_at: float = time.time()
        self.ended_at: float | None = None
        self.last_payload: Any = None
        self.transitions: list[dict[str, Any]] = []
        self.final_extras: dict[str, Any] = {}
        self.error: str | None = None

        self.task: asyncio.Task | None = None
        self.done_event: asyncio.Event = asyncio.Event()

        # Reverse-stream push: the connection-scoped MCP ServerSession captured
        # at *_wait time (typed Any so this module stays MCP-free). The
        # background task streams transition logs + a terminal notification
        # through it. notified/notify_error record best-effort delivery.
        self.notify_session: Any = None
        self.notified: bool = False
        self.notify_error: str | None = None

        # Per-stage status view (pipeline waits only): ordered list of
        # {name, status, jobs}, refreshed each poll for the live stage stream
        # and the terminal summary.
        self.stages: list[dict[str, Any]] = []

    @property
    def elapsed_seconds(self) -> float:
        end = self.ended_at if self.ended_at is not None else time.time()
        return round(end - self.started_at, 2)

    def record_transition(self, new_status: str | None) -> bool:
        """If `new_status` differs from current, log a transition. Returns True
        when a transition was recorded."""
        if new_status == self.status:
            return False
        self.transitions.append({
            "from": self.status,
            "to": new_status,
            "elapsed_seconds": round(time.time() - self.started_at, 2),
        })
        self.status = new_status
        return True

    def record_poll_failure(self, message: str) -> None:
        """Count a failed poll attempt. A failed HTTP call is still a call,
        so `polls` advances too. Kept on the handle (not loop-local) so a
        snapshot shows flakiness even after the wait recovers."""
        self.polls += 1
        self.poll_failures += 1
        self.last_poll_error = message

    def mark_terminated(self, *, error: str | None = None) -> None:
        self.terminated = error is None
        self.error = error
        self.ended_at = time.time()
        self.done_event.set()

    def mark_timed_out(self, message: str) -> None:
        """The wait gave up (max_lifetime exceeded) without a terminal status."""
        self.timed_out = True
        self.error = message
        self.ended_at = time.time()
        self.done_event.set()

    def snapshot(self) -> dict[str, Any]:
        """Return a JSON-serializable snapshot of the current state.

        Always includes the latest payload (`pipeline` or `job` key) so a
        caller polling mid-flight sees what the wait sees. When terminal,
        also includes enrichment (`jobs`, `failed_logs`, `log`, `warnings`).
        """
        payload_key = "pipeline" if self.kind == "pipeline" else "job"
        target_key = "pipeline_id" if self.kind == "pipeline" else "job_id"

        snap: dict[str, Any] = {
            "wait_id": self.wait_id,
            "resource_uri": f"gitlab://waits/{self.wait_id}",
            "kind": self.kind,
            "project_id": self.project_id,
            target_key: self.target_id,
            "status": self.status,
            "terminated": self.terminated,
            "timed_out": self.timed_out,
            "polls": self.polls,
            "elapsed_seconds": self.elapsed_seconds,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "transitions": list(self.transitions),
        }
        if self.poll_failures:
            snap["poll_failures"] = self.poll_failures
            snap["last_poll_error"] = self.last_poll_error
        # Surfaced even mid-flight: a transition push that failed is a signal
        # the agent should fall back to polling, not something to hide until
        # the wait ends.
        if self.notified:
            snap["notified"] = self.notified
        if self.notify_error is not None:
            snap["notify_error"] = self.notify_error
        if self.stages:
            snap["stages"] = self.stages
        if self.last_payload is not None:
            snap[payload_key] = self.last_payload
        if self.error is not None:
            snap["error"] = self.error
        if self.terminated:
            for k, v in self.final_extras.items():
                snap[k] = v
        return snap

    def push_payload(self) -> dict[str, Any]:
        """Compact terminal-notification payload for the reverse stream.

        Omits heavy fields (last_payload, jobs, logs) and the non-serializable
        notify_session - it is a completion signal, not the full snapshot. The
        consumer fetches detail via *_wait_poll or the resource using wait_id.
        """
        target_key = "pipeline_id" if self.kind == "pipeline" else "job_id"
        payload: dict[str, Any] = {
            "event": "wait_terminal",
            "wait_id": self.wait_id,
            "resource_uri": f"gitlab://waits/{self.wait_id}",
            "kind": self.kind,
            "project_id": self.project_id,
            target_key: self.target_id,
            "status": self.status,
            "terminated": self.terminated,
            "timed_out": self.timed_out,
            "error": self.error,
            "elapsed_seconds": self.elapsed_seconds,
        }
        if self.stages:
            payload["stages"] = self.stages
        return payload


class WaitRegistry:
    """Module-level singleton holding all in-flight and recently-terminal waits."""

    def __init__(self, ttl_seconds: int = _DEFAULT_TTL_SECONDS):
        self._waits: dict[str, WaitHandle] = {}
        self._ttl = ttl_seconds

    def new_handle(
        self,
        kind: str,
        project_id: str | int,
        target_id: str | int,
        options: dict[str, Any],
    ) -> WaitHandle:
        prefix = "wp" if kind == "pipeline" else "wj"
        wait_id = f"{prefix}-{secrets.token_hex(4)}"
        handle = WaitHandle(wait_id, kind, project_id, target_id, options)
        self._waits[wait_id] = handle
        return handle

    def get(self, wait_id: str) -> WaitHandle | None:
        return self._waits.get(wait_id)

    def all_handles(self) -> list[WaitHandle]:
        return list(self._waits.values())

    def cancel(self, wait_id: str) -> WaitHandle | None:
        """Cancel the underlying task if still running. Returns the handle
        (which may then be inspected for the final state), or None if unknown."""
        handle = self._waits.get(wait_id)
        if handle is None:
            return None
        if handle.task is not None and not handle.task.done():
            handle.task.cancel()
        return handle

    def reap_old(self, *, now: float | None = None) -> int:
        """Drop terminal handles older than TTL. Returns count removed."""
        now = now if now is not None else time.time()
        stale = [
            wid for wid, h in self._waits.items()
            if h.ended_at is not None and (now - h.ended_at) > self._ttl
        ]
        for wid in stale:
            del self._waits[wid]
        return len(stale)

    def clear(self) -> None:
        """Drop all handles. Used by tests to reset state between cases.

        Cancels any still-running tasks first so they don't keep mutating
        a handle that's already been removed.
        """
        for h in self._waits.values():
            if h.task is not None and not h.task.done():
                h.task.cancel()
        self._waits.clear()


# Module-level singleton — instantiated lazily so import-time consumers
# (e.g. tests that reset state) get the same object the tools use.
WAIT_REGISTRY = WaitRegistry()
