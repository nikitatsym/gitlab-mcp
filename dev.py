#!/usr/bin/env python3
"""Repo entry point per specs/general/dev-script.md.

`test` is the unit suite (pytest's own `-m 'not integration'` default from
pyproject.toml); `e2e` is the integration suite, selected by marker so new
test files join it automatically. Both are auto-discovered - no file lists.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

CMDS: dict[str, list[list[str]]] = {
    "lint": [
        ["uv", "run", "ruff", "check", "."],
        ["uv", "run", "mypy", "src/gitlab_mcp", "tests", "scripts"],
    ],
    "test": [
        ["uv", "run", "pytest", "-m", "not integration"],
    ],
    "e2e": [
        ["uv", "run", "pytest", "-m", "integration"],
    ],
}


def codegen_drift() -> int:
    """Fail when generated output drifts or required body fields lose their contract."""
    npx = shutil.which("npx")
    if npx is None:
        print("codegen drift: npx not found, install Node 22+", file=sys.stderr)
        return 1
    if not (ROOT / "codegen" / "node_modules").is_dir():
        print(
            "codegen drift: codegen/node_modules missing, run `npm ci` in codegen/",
            file=sys.stderr,
        )
        return 1
    for command in (
        [npx, "tsx", "generate.ts", "--check"],
        [npx, "tsx", "required_body_conformance.ts"],
    ):
        rc = subprocess.run(
            command,
            cwd=ROOT / "codegen",
            check=False,
        ).returncode
        if rc:
            return rc
    return 0


def install_hook() -> int:
    """Point git at the repo's tracked pre-commit hook. Idempotent."""
    if (ROOT / ".githooks" / "pre-commit").exists():
        return subprocess.run(
            ["git", "config", "core.hooksPath", ".githooks"], check=False
        ).returncode
    print("no tracked hook: expected .githooks/pre-commit", file=sys.stderr)
    return 1


def _hook_ready() -> bool:
    if (ROOT / ".git" / "hooks" / "pre-commit").exists():
        return True
    configured = subprocess.run(
        ["git", "config", "--get", "core.hooksPath"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    return bool(configured) and (ROOT / configured / "pre-commit").exists()


def _hook_hint() -> None:
    # A fresh clone gates nothing until asked; CI has no use for a hook.
    if not os.environ.get("CI") and not _hook_ready():
        print("hint: `python dev.py hook` installs the pre-commit gate", file=sys.stderr)


def run(name: str) -> int:
    if name == "hook":
        return install_hook()
    if name == "codegen-drift":
        return codegen_drift()
    if name == "check":
        _hook_hint()
        # Drift before the slow suite: 6s of feedback should not wait on 60s.
        return run("lint") or run("codegen-drift") or run("test")
    if name not in CMDS:
        extra = ["check", "codegen-drift", "hook"]
        print(f"unknown: {name}. available: {list(CMDS) + extra}", file=sys.stderr)
        return 2
    for cmd in CMDS[name]:
        rc = subprocess.run(cmd, cwd=ROOT, check=False).returncode
        if rc:
            return rc
    return 0


if __name__ == "__main__":
    sys.exit(run(sys.argv[1] if len(sys.argv) > 1 else "check"))
