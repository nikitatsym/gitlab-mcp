"""In-memory registry of long-running wait operations.

Backs the `pipelines_wait_start` / `pipelines_wait_poll` and
`jobs_wait_start` / `jobs_wait_poll` tools. Each registered wait is a
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
asyncio loop. The poll task is the sole writer to mutable state on the
handle, so no lock is needed — reads from other coroutines see a
consistent snapshot at every await point.
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
        "polls",
        "started_at",
        "ended_at",
        "last_payload",
        "transitions",
        "final_extras",
        "error",
        "task",
        "done_event",
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
        self.polls: int = 0
        self.started_at: float = time.time()
        self.ended_at: float | None = None
        self.last_payload: Any = None
        self.transitions: list[dict[str, Any]] = []
        self.final_extras: dict[str, Any] = {}
        self.error: str | None = None

        self.task: asyncio.Task | None = None
        self.done_event: asyncio.Event = asyncio.Event()

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

    def mark_terminated(self, *, error: str | None = None) -> None:
        self.terminated = error is None
        self.error = error
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
            "timed_out": False,  # waits don't time out themselves; see options
            "polls": self.polls,
            "elapsed_seconds": self.elapsed_seconds,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "transitions": list(self.transitions),
        }
        if self.last_payload is not None:
            snap[payload_key] = self.last_payload
        if self.error is not None:
            snap["error"] = self.error
        if self.terminated:
            for k, v in self.final_extras.items():
                snap[k] = v
        return snap


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
