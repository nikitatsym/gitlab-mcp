# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "mcp>=1.27.0",
#   "pydantic>=2.0",
# ]
# ///
"""Probe MCP for observing what Claude Code does with the three server->client
notification primitives MCP defines.

One tool (`start_long_op`) returns immediately with an `op_id` and a
`testmcp://ops/{op_id}` resource URI, then a background task emits each
enabled notification kind on every tick:

  - notifications/message       via session.send_log_message
  - notifications/progress      via session.send_progress_notification
                                (only if the client passed _meta.progressToken)
  - notifications/resources/updated via session.send_resource_updated
                                (only useful if the client called
                                resources/subscribe on the URI)

Each flag is per-call so we can isolate which type Claude Code actually
surfaces to the model and whether subscription -> auto re-read works.

Run via:
    uv run /Users/ari/src/mcps/gitlab-mcp/scripts/notif_probe.py
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import sys
import time
from typing import Any

from mcp.server.fastmcp import Context, FastMCP
from pydantic import AnyUrl

# stdout is a pipe when this runs under an MCP client, which means Python
# defaults to block-buffering it. JSON-RPC notifications (~200 B) then sit
# in the buffer until it fills, so the client only sees them in bursts.
# Force line-buffered writes so each notifications/... line is flushed
# before any output is produced.
sys.stdout.reconfigure(line_buffering=True)  # type: ignore[union-attr]

mcp = FastMCP("notif-probe")
log = logging.getLogger("notif_probe")

_OPS: dict[str, dict[str, Any]] = {}


@mcp.tool()
async def start_long_op(
    ticks: int = 3,
    interval: float = 1.0,
    emit_log: bool = True,
    emit_progress: bool = True,
    emit_resource_updated: bool = True,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Start a fake long-running op. Returns immediately; the background task
    emits the selected notification types on each tick until done.

    Inspect after by calling `read_op(op_id)` or by reading the resource
    `testmcp://ops/{op_id}`. `send_errors` on the state captures any push
    failures (e.g. resource_updated without a prior subscribe is silent on
    some clients but errors on strict ones)."""
    op_id = secrets.token_hex(3)
    uri = f"testmcp://ops/{op_id}"
    session = ctx.session if ctx else None
    progress_token: str | int | None = None
    request_id: str | int | None = None
    if ctx is not None:
        try:
            request_id = ctx.request_id
        except Exception:  # noqa: BLE001 — no active request context
            request_id = None
        rc = ctx.request_context
        if rc is not None and rc.meta is not None:
            progress_token = rc.meta.progressToken

    state: dict[str, Any] = {
        "op_id": op_id,
        "resource_uri": uri,
        "status": "running",
        "tick": 0,
        "ticks_total": ticks,
        "started_at": time.time(),
        "flags": {
            "emit_log": emit_log,
            "emit_progress": emit_progress,
            "emit_resource_updated": emit_resource_updated,
        },
        "progress_token_observed": progress_token,
        "request_id_observed": request_id,
        "session_captured": session is not None,
        "sent_log": 0,
        "sent_progress": 0,
        "sent_resource_updated": 0,
        "send_errors": [],
    }
    _OPS[op_id] = state

    asyncio.create_task(
        _run(
            op_id, ticks, interval,
            emit_log, emit_progress, emit_resource_updated,
            session, progress_token, request_id, uri,
        )
    )

    return {
        "op_id": op_id,
        "resource_uri": uri,
        "status": "started",
        "ticks": ticks,
        "interval": interval,
        "progress_token_observed": progress_token,
        "request_id_observed": request_id,
        "hint": (
            "Background task will set related_request_id on log/progress "
            "notifications so streamable-http transports can route them back "
            "to the originating request (per modelcontextprotocol/"
            "python-sdk#953). For stdio routing isn't request-bound."
        ),
    }


@mcp.tool()
def read_op(op_id: str) -> dict[str, Any]:
    """Read the current state of an op (same payload the resource returns)."""
    return _OPS.get(op_id) or {"error": f"unknown op {op_id!r}"}


@mcp.tool()
def list_ops() -> list[dict[str, Any]]:
    """List every op the probe has registered so far."""
    return list(_OPS.values())


@mcp.resource(
    "testmcp://ops/{op_id}",
    name="probe op",
    description="Live state of a probe op started by start_long_op.",
    mime_type="application/json",
)
def _read_op_resource(op_id: str) -> str:
    import json
    return json.dumps(_OPS.get(op_id) or {"error": f"unknown op {op_id!r}"})


async def _run(
    op_id: str,
    ticks: int,
    interval: float,
    emit_log: bool,
    emit_progress: bool,
    emit_resource_updated: bool,
    session: Any,
    progress_token: str | int | None,
    request_id: str | int | None,
    uri: str,
) -> None:
    state = _OPS[op_id]
    uri_obj = AnyUrl(uri)
    for tick in range(1, ticks + 1):
        await asyncio.sleep(interval)
        state["tick"] = tick
        state["status"] = "running" if tick < ticks else "done"
        msg = f"op {op_id} tick {tick}/{ticks} status={state['status']}"
        if session is None:
            state["send_errors"].append(f"tick {tick}: session is None")
            continue
        if emit_log:
            try:
                await session.send_log_message(
                    level="info",
                    data={
                        "event": "tick",
                        "op_id": op_id,
                        "tick": tick,
                        "total": ticks,
                        "status": state["status"],
                    },
                    logger="notif_probe",
                    related_request_id=request_id,
                )
                state["sent_log"] += 1
            except Exception as e:  # noqa: BLE001
                state["send_errors"].append(f"log: {e!r}")
        if emit_progress and progress_token is not None:
            try:
                await session.send_progress_notification(
                    progress_token=progress_token,
                    progress=float(tick),
                    total=float(ticks),
                    message=msg,
                    related_request_id=request_id,
                )
                state["sent_progress"] += 1
            except Exception as e:  # noqa: BLE001
                state["send_errors"].append(f"progress: {e!r}")
        if emit_resource_updated:
            try:
                await session.send_resource_updated(uri_obj)
                state["sent_resource_updated"] += 1
            except Exception as e:  # noqa: BLE001
                state["send_errors"].append(f"resource_updated: {e!r}")
    state["ended_at"] = time.time()


if __name__ == "__main__":
    mcp.run()
