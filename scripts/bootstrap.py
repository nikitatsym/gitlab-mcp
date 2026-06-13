#!/usr/bin/env python3
"""Wait for a GitLab/Heptapod container to be ready and seed a root PAT.

Usage: `uv run python scripts/bootstrap.py [gitlab|heptapod]`

Exports:
  - Writes GITLAB_URL and GITLAB_TOKEN to `tests/.env` for pytest + shells
  - Prints the token to stdout on success

Idempotent: safe to re-run against an already-bootstrapped instance.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import typing
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILE = ROOT / "tests" / "docker-compose.yml"
ENV_FILE = ROOT / "tests" / ".env"

class _Instance(typing.TypedDict):
    url: str
    service: str
    compose_profile: str | None
    readiness_timeout: int


INSTANCES: dict[str, _Instance] = {
    "gitlab": {
        "url": "http://localhost:8929",
        "service": "gitlab",
        "compose_profile": None,
        "readiness_timeout": 600,
    },
    "heptapod": {
        "url": "http://localhost:8930",
        "service": "heptapod",
        "compose_profile": "heptapod",
        "readiness_timeout": 900,
    },
}

ROOT_PASSWORD = "X9!Zq-Integration-Tkn42"
TEST_PAT = "glpat-integration-test-token"


def _compose_exec(service: str, profile: str | None, *args: str) -> subprocess.CompletedProcess:
    cmd = ["docker", "compose", "-f", str(COMPOSE_FILE)]
    if profile:
        cmd += ["--profile", profile]
    cmd += ["exec", "-T", service, *args]
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def wait_for_ready(url: str, timeout: int) -> None:
    """Poll the API until the instance accepts requests.

    Probes `/api/v4/version` rather than `/-/readiness`: GitLab CE serves both
    publicly, but Heptapod's nginx (since 17-0-latest) only serves `/-/...`
    from inside the container. A 401 here is just as good as a 200 — Rails is
    up enough to authenticate, which is all bootstrap needs.
    """
    deadline = time.time() + timeout
    last_error: str = "(no attempt yet)"
    attempts = 0
    while time.time() < deadline:
        attempts += 1
        try:
            r = httpx.get(f"{url}/api/v4/version", timeout=5, follow_redirects=False)
            if r.status_code in (200, 401):
                elapsed = int(timeout - (deadline - time.time()))
                print(f"[bootstrap] ready after {elapsed}s ({attempts} attempts)")
                # Extra grace period — API may answer before rails fully accepts writes.
                time.sleep(5)
                return
            last_error = f"HTTP {r.status_code}"
        except Exception as e:
            last_error = type(e).__name__
        if attempts % 6 == 0:
            elapsed = int(timeout - (deadline - time.time()))
            print(f"[bootstrap] waiting... {elapsed}s elapsed, last: {last_error}")
        time.sleep(5)
    raise TimeoutError(f"{url} did not become ready in {timeout}s (last: {last_error})")


def bootstrap_pat(service: str, profile: str | None, retries: int = 5) -> str:
    """Run `gitlab-rails runner` inside the container to create a PAT."""
    script = f"""
user = User.find_by(username: 'root')
user.password = '{ROOT_PASSWORD}'
user.password_confirmation = '{ROOT_PASSWORD}'
user.password_automatically_set = false
user.save!

existing = user.personal_access_tokens.find_by(name: 'integration-test')
existing&.destroy!

token = user.personal_access_tokens.create(
  scopes: ['api', 'read_user', 'read_repository', 'write_repository', 'sudo'],
  name: 'integration-test',
  expires_at: 365.days.from_now
)
token.set_token('{TEST_PAT}')
token.save!
puts 'OK'
"""
    last_result: subprocess.CompletedProcess | None = None
    for attempt in range(1, retries + 1):
        last_result = _compose_exec(service, profile, "gitlab-rails", "runner", script)
        if "OK" in last_result.stdout:
            return TEST_PAT
        print(f"[bootstrap] PAT seed attempt {attempt}/{retries} failed, retrying in 10s...")
        if last_result.stderr:
            print(f"[bootstrap]   stderr tail: {last_result.stderr.splitlines()[-1] if last_result.stderr.strip() else '(empty)'}")
        time.sleep(10)
    raise RuntimeError(
        f"PAT bootstrap failed after {retries} attempts.\n"
        f"STDOUT:\n{last_result.stdout if last_result else ''}\n"
        f"STDERR:\n{last_result.stderr if last_result else ''}"
    )


def write_env_file(url: str, token: str) -> None:
    ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    ENV_FILE.write_text(
        f"# Written by scripts/bootstrap.py — consumed by tests and local shells\n"
        f"GITLAB_URL={url}\n"
        f"GITLAB_TOKEN={token}\n"
        f"GITLAB_BACKEND=auto\n"
    )
    print(f"[bootstrap] wrote {ENV_FILE}")


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in INSTANCES:
        print(f"usage: {sys.argv[0]} [{'|'.join(INSTANCES)}]", file=sys.stderr)
        return 2

    inst = INSTANCES[sys.argv[1]]
    url = inst["url"]
    print(f"[bootstrap] target: {url}")

    try:
        wait_for_ready(url, inst["readiness_timeout"])
    except TimeoutError as e:
        print(f"[bootstrap] FAILED: {e}", file=sys.stderr)
        return 1

    token = bootstrap_pat(inst["service"], inst["compose_profile"])
    write_env_file(url, token)
    print(f"[bootstrap] OK — token written to {ENV_FILE}")
    print(f"[bootstrap] For interactive shell: `source {ENV_FILE.relative_to(ROOT)}`")
    return 0


if __name__ == "__main__":
    sys.exit(main())
