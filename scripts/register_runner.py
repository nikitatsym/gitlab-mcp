#!/usr/bin/env python3
"""Register a shell-executor gitlab-runner against the local GitLab CE.

Used by the waiter integration tests (`tests/test_integration_waiters.py`)
to ensure pipelines can actually transition pending → running → success/failed,
not just sit pending forever waiting for a runner.

Steps:
  1. Reads GITLAB_URL / GITLAB_TOKEN from tests/.env (written by bootstrap.py).
  2. Creates an instance-scope runner via POST /api/v4/user/runners with the
     admin PAT, captures the runner authentication token.
  3. Brings up the `gitlab-runner` container (compose profile=ci) if it isn't
     already running.
  4. Inside that container, runs `gitlab-runner register` with the token,
     shell executor, untagged + run-untagged so any pipeline picks it up.
  5. Restarts the runner so it loads the new config.

Idempotent for our purposes: if a runner with the same description already
exists, we still create a fresh one and register it (cheap, and avoids the
race of stale tokens).
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILE = ROOT / "tests" / "docker-compose.yml"
ENV_FILE = ROOT / "tests" / ".env"

RUNNER_DESC = "waiter-integration-shell"
RUNNER_SERVICE = "gitlab-runner"
# GitLab inside the container talks to itself by container name, not localhost.
INTERNAL_GITLAB_URL = "http://gitlab:8929"


def _read_env() -> dict[str, str]:
    if not ENV_FILE.exists():
        sys.exit(
            f"{ENV_FILE} not found. Run `npm run gitlab:up` first."
        )
    env: dict[str, str] = {}
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
    if "GITLAB_URL" not in env or "GITLAB_TOKEN" not in env:
        sys.exit("tests/.env is missing GITLAB_URL or GITLAB_TOKEN")
    return env


def _cleanup_stale_runners(url: str, pat: str) -> None:
    """Delete any prior `waiter-integration-shell` runners on the server so we
    don't accumulate ghost entries that the local runner can't pick up.

    Without this, repeated `runner:up` calls leave registered-but-offline
    runners that confuse the test harness on subsequent runs.
    """
    r = httpx.get(
        f"{url}/api/v4/runners/all",
        headers={"PRIVATE-TOKEN": pat},
        timeout=20.0,
    )
    if r.status_code >= 400:
        return  # best-effort cleanup
    for runner in r.json():
        if runner.get("description") == RUNNER_DESC:
            httpx.delete(
                f"{url}/api/v4/runners/{runner['id']}",
                headers={"PRIVATE-TOKEN": pat},
                timeout=20.0,
            )
            print(f"[runner] cleaned up stale runner id={runner['id']}")


def _create_runner_token(url: str, pat: str) -> str:
    """Provision an instance-scope runner and return its auth token."""
    r = httpx.post(
        f"{url}/api/v4/user/runners",
        headers={"PRIVATE-TOKEN": pat},
        json={
            "runner_type": "instance_type",
            "description": RUNNER_DESC,
            "tag_list": [],
            "run_untagged": True,
            "locked": False,
            "access_level": "not_protected",
        },
        timeout=20.0,
    )
    if r.status_code >= 400:
        sys.exit(
            f"POST /api/v4/user/runners failed: {r.status_code} {r.text}"
        )
    body = r.json()
    token = body.get("token")
    if not isinstance(token, str) or not token:
        sys.exit(f"runner create response missing token: {body}")
    print(f"[runner] created runner id={body.get('id')} desc={RUNNER_DESC!r}")
    return token


def _compose(*args: str, capture: bool = False) -> subprocess.CompletedProcess:
    cmd = ["docker", "compose", "-f", str(COMPOSE_FILE), "--profile", "ci", *args]
    return subprocess.run(
        cmd, capture_output=capture, text=True, check=False,
    )


def _wait_for_runner_container(timeout: int = 60) -> None:
    """gitlab-runner has no healthcheck; poll docker ps until it's running."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = _compose("ps", "--format", "json", RUNNER_SERVICE, capture=True)
        if r.returncode == 0 and '"State":"running"' in r.stdout:
            return
        time.sleep(1)
    raise TimeoutError(f"{RUNNER_SERVICE} did not enter the running state")


def _register(token: str) -> None:
    """Run gitlab-runner register inside the runner container."""
    cmd = [
        "docker", "compose", "-f", str(COMPOSE_FILE), "--profile", "ci",
        "exec", "-T", RUNNER_SERVICE,
        "gitlab-runner", "register",
        "--non-interactive",
        "--url", INTERNAL_GITLAB_URL,
        # GitLab returns its external_url ("http://localhost:8929") to runners
        # for clone — but `localhost` from inside the runner container is the
        # runner itself, not GitLab. clone-url overrides it so git fetch works.
        "--clone-url", INTERNAL_GITLAB_URL,
        "--token", token,
        "--executor", "shell",
        "--shell", "sh",
        "--description", RUNNER_DESC,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if r.returncode != 0:
        sys.exit(
            f"gitlab-runner register failed:\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
        )
    print(f"[runner] registered against {INTERNAL_GITLAB_URL}")


def main() -> int:
    env = _read_env()
    pat = env["GITLAB_TOKEN"]
    public_url = env["GITLAB_URL"].rstrip("/")
    _cleanup_stale_runners(public_url, pat)
    token = _create_runner_token(public_url, pat)

    print(f"[runner] bringing up {RUNNER_SERVICE} container...")
    _compose("up", "-d", RUNNER_SERVICE)
    _wait_for_runner_container()

    _register(token)

    # Restart to pick up the new config.toml entry.
    _compose("restart", RUNNER_SERVICE)
    print("[runner] OK — registered and restarted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
