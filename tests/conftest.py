"""Integration test fixtures.

This conftest does NOT manage the Docker lifecycle. It assumes that a
GitLab/Heptapod instance is already running at `GITLAB_URL` with a valid
`GITLAB_TOKEN`. Use the npm scripts (`npm run gitlab:up` / `npm run gitlab:down`)
or the bootstrap script (`scripts/bootstrap.py`) to manage containers.

Tests marked `@pytest.mark.integration` are skipped automatically if the env
vars are not present.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

TESTS_DIR = Path(__file__).parent
ENV_FILE = TESTS_DIR / ".env"


def _load_env_file() -> None:
    """Load GITLAB_URL / GITLAB_TOKEN from tests/.env if present.

    Called at collect-time so the fixture skips cleanly when nothing's set up.
    """
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


_load_env_file()


# ── Agent simulator ───────────────────────────────────────────────────────


class AgentSimulator:
    """Simulate an MCP agent calling tools by snake_case name.

    Only exposes tools that are actually registered in `server._group_ops`
    after `_register_tools()` has filtered out Heptapod-only tools on a
    non-Heptapod backend.
    """

    def __init__(self):
        from gitlab_mcp.server import _group_ops, mcp as _mcp

        self.call_log: list[dict] = []
        self._tools: dict[str, Any] = {}

        # Meta-tools (gitlab_read, gitlab_write, …) + ROOT standalone tools.
        for tool in _mcp._tool_manager._tools.values():
            self._tools[tool.name] = tool.fn

        # Individual ops by snake name, pulled from the filtered registry.
        for _group_name, ops in _group_ops.items():
            for _pascal, fn in ops.items():
                snake = fn.__name__
                if snake not in self._tools:
                    self._tools[snake] = fn

    def call(self, tool_name: str, **kwargs) -> Any:
        fn = self._tools.get(tool_name)
        if fn is None:
            raise ValueError(
                f"Unknown tool: {tool_name}. "
                f"Known sample: {sorted(self._tools.keys())[:20]}..."
            )
        result = fn(**kwargs)
        self.call_log.append({"tool": tool_name, "kwargs": kwargs, "result": result})
        return result

    @property
    def total_calls(self) -> int:
        return len(self.call_log)


# ── Fixtures ──────────────────────────────────────────────────────────────


def _require_env(*names: str) -> dict[str, str]:
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        pytest.skip(
            f"Integration test requires env vars {missing}. "
            f"Start GitLab with `npm run gitlab:up` or set them manually."
        )
    return {n: os.environ[n] for n in names}


def _build_agent() -> AgentSimulator:
    """Fresh client + server registration, then return an AgentSimulator."""
    from gitlab_mcp.backend import detect_instance
    from gitlab_mcp.client import _reset_client, get_client
    from gitlab_mcp.config import _reset_settings
    from gitlab_mcp.server import _register_tools

    _reset_settings()
    _reset_client()

    client = get_client()
    client.instance = detect_instance(client)
    _register_tools()
    return AgentSimulator()


@pytest.fixture(scope="session")
def gitlab_instance():
    """URL/token for a running GitLab. Skips the test if env vars aren't set."""
    env = _require_env("GITLAB_URL", "GITLAB_TOKEN")
    return {"url": env["GITLAB_URL"], "token": env["GITLAB_TOKEN"]}


@pytest.fixture(scope="session")
def heptapod_instance():
    """URL/token for a running Heptapod. Gated on RUN_HEPTAPOD_TESTS=1."""
    if not os.environ.get("RUN_HEPTAPOD_TESTS"):
        pytest.skip("Heptapod integration gated: set RUN_HEPTAPOD_TESTS=1 to run")
    env = _require_env("GITLAB_URL", "GITLAB_TOKEN")
    return {"url": env["GITLAB_URL"], "token": env["GITLAB_TOKEN"]}


@pytest.fixture(scope="session")
def agent_gitlab(gitlab_instance) -> AgentSimulator:
    """AgentSimulator wired to the live GitLab instance."""
    return _build_agent()


@pytest.fixture(scope="session")
def agent_heptapod(heptapod_instance) -> AgentSimulator:
    """AgentSimulator wired to the live Heptapod instance."""
    return _build_agent()
