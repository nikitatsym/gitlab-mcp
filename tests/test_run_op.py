"""`_dispatch` runs a sync op in a worker thread, with the caller's context."""

from __future__ import annotations

import asyncio
import threading

from gitlab_mcp import client_var, server
from gitlab_mcp.client import get_client

GROUP = "run_op_group"


def _register(monkeypatch, fn):
    monkeypatch.setitem(server._group_ops, GROUP, {server._to_pascal(fn.__name__): fn})


def test_sync_op_does_not_block_event_loop(monkeypatch):
    started = threading.Event()
    release = threading.Event()

    def blocking_op() -> bool:
        """Block until the event loop releases it."""
        started.set()
        return release.wait(timeout=5)

    _register(monkeypatch, blocking_op)

    async def flow():
        task = asyncio.create_task(server._dispatch("BlockingOp", GROUP, {}))
        await asyncio.to_thread(started.wait, 5)
        release.set()
        return await task

    assert asyncio.run(flow()) is True


def test_sync_op_sees_bound_client(monkeypatch):
    sentinel = object()

    def client_op() -> object:
        """Return the client the op resolves."""
        return get_client()

    _register(monkeypatch, client_op)
    token = client_var.set(sentinel)  # type: ignore[arg-type]
    try:
        assert asyncio.run(server._dispatch("ClientOp", GROUP, {})) is sentinel
    finally:
        client_var.reset(token)
