# gitlab-mcp

MCP server for **GitLab** and **Heptapod** (the Mercurial-friendly GitLab fork).
Full REST API coverage with VCS-aware helpers — one tool surface, two backends.

## Features

- **800+ tools** generated from `@gitbeaker/rest` TypeScript types covering the full GitLab v4 REST API
- **6 risk-graded groups** — `gitlab_read`, `gitlab_write`, `gitlab_execute`, `gitlab_delete`, `gitlab_admin_read`, `gitlab_admin_write`
- **Heptapod transparent** — auto-detects backend at startup, reveals 4 hg-specific tools (`hg_get_config`, `hg_set_config`, `hg_get_raw_hgrc`, `hg_create_topic_mr`) only when running against Heptapod
- **Mercurial refs preserved verbatim** — `branch/<name>` and `topic/<target>/<name>` pass through unchanged; commit IDs not assumed to be git SHAs
- **Pre-flight guards** — block `fork` on hg projects, validate hg topic naming on MR creation, detect silently-dropped fields in write responses
- **Visibility default-deny** — public/internal projects/snippets/groups blocked unless `--allow-public` is passed
- **Slim list views** — `brief=True` returns trimmed entries for projects/MRs/issues/branches/commits/etc. so the LLM doesn't drown in metadata
- **Long-running waiters** — `pipelines_wait` / `jobs_wait` block until a CI pipeline or job reaches a terminal status, streaming each transition via MCP `report_progress` + `log` notifications. Final summary (with failed-job log tails for pipelines) is returned even if the client doesn't render notifications
- Self-service helpers for SSH/GPG keys, emails, and notification settings (which gitbeaker hides behind URL helpers)
- Zero-config install via `uvx`

## Quick start

Add the following to your MCP client config (Claude Desktop, Cursor, Claude Code, etc.).
For Claude Code global config: `~/.claude.json` → `"mcpServers"`.

```json
{
  "mcpServers": {
    "gitlab": {
      "command": "uvx",
      "args": ["--refresh", "--extra-index-url", "https://nikitatsym.github.io/gitlab-mcp/simple", "gitlab-mcp"],
      "env": {
        "GITLAB_URL": "https://gitlab.example.com",
        "GITLAB_TOKEN": "glpat-..."
      }
    }
  }
}
```

Or use the interactive **[Setup Page](https://nikitatsym.github.io/gitlab-mcp/)** to generate the config.

The same config works against Heptapod — just point `GITLAB_URL` at your Heptapod instance.
The server probes `/api/v4/projects/vcs_type_stats` at startup to detect which backend it's
talking to and registers the right tool set.

## Configuration

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `GITLAB_URL` | yes | — | Instance base URL (`http://` or `https://`, no trailing slash needed) |
| `GITLAB_TOKEN` | yes | — | Any access token: PAT, project/group access token, OAuth2 bearer, or job token |
| `GITLAB_BACKEND` | no | `auto` | `auto` / `gitlab` / `heptapod` — skips the detection probe when set explicitly |
| `GITLAB_TIMEOUT` | no | `30.0` | httpx request timeout in seconds |
| `MCP_GITLAB_BRIEF_MAX` | no | `200` | Cap on slim note bodies; `0` disables truncation |

### CLI flags

| Flag | Default | Purpose |
|---|---|---|
| `--allow-public` | off | Permits creating public/internal projects, groups, and snippets. Without it, `visibility` must be `private`. |

## Heptapod handling

On a Heptapod instance, the MCP additionally registers four `hg_*` tools:

- `hg_get_config(project_id)` — read structured Mercurial settings
- `hg_set_config(project_id, inherit, allow_bookmarks, allow_multiple_heads, auto_publish)` — write settings (note: this is a PUT, not a PATCH — unsent fields reset to defaults)
- `hg_get_raw_hgrc(project_id)` — read the raw `hgrc` file (Maintainer required)
- `hg_create_topic_mr(project_id, target_hg_branch, topic_name, title)` — convenience wrapper that builds the `topic/<target>/<name>` source ref and the `branch/<target>` target ref

Pre-flight guards prevent common mistakes:
- `fork_project` is rejected on Mercurial projects (unsupported on Heptapod)
- `merge_requests_create` rejects equal source/target and validates that hg projects use `branch/...` target prefix
- `hg_create_topic_mr` is rejected on git-typed projects, even on Heptapod

## Development

The project uses npm scripts (per python-service.md spec) for the local lifecycle:

```bash
# unit tests (no docker, fast)
npm test

# bring up GitLab CE container + bootstrap a root PAT
npm run gitlab:up

# run integration tests against the live container
npm run test:integration

# tear down
npm run gitlab:down

# regenerate _generated.py from gitbeaker types
npm run codegen

# CI drift check (exit 1 if generated files diverge from source)
npm run codegen:check
```

### Waiter integration tests

`pipelines_wait` / `jobs_wait` need a real runner to exercise transitions, so
their integration test (`tests/test_integration_waiters.py`) is gated behind
an extra `gitlab-runner` container (shell executor, `--profile ci`):

```bash
npm run gitlab:up                 # GitLab CE + PAT bootstrap (~3-5 min first run)
npm run runner:up                 # register a fresh instance-scope runner
npm run test:integration:waiters  # creates a project, pushes CI config, waits

npm run runner:down               # remove the runner + wipe its config volume
```

Heptapod integration tests are gated behind `RUN_HEPTAPOD_TESTS=1`:

```bash
npm run heptapod:up
RUN_HEPTAPOD_TESTS=1 npm run test:integration:heptapod
npm run heptapod:down
```

The `tests/.env` file written by `bootstrap.py` is consumed both by pytest and by
interactive shells (`source tests/.env`).

## Creating a GitLab access token

1. Log in to your GitLab/Heptapod instance.
2. Go to **User Settings** → **Access Tokens**.
3. Name the token (e.g. `mcp-server`), set an expiration date.
4. Select scopes — `api` for full read/write access, or finer-grained scopes (`read_api`, `read_repository`, `write_repository`).
5. Click **Create personal access token** and copy the value immediately.

For project-scoped operations only, use a **project access token**: project **Settings** → **Access Tokens**.

## License

MIT
