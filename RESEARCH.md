# GitLab vs Heptapod API Research

**Target audience:** Developers building an MCP server that must speak to both GitLab and Heptapod.

**Date of research:** April 2026.

**Scope:** Factual, concrete API differences relevant to an MCP tool surface. Not a general Heptapod overview.

---

## TL;DR

Heptapod is a "friendly fork" of GitLab CE maintained by Octobus (now Cloudcrane SAS) that adds first-class Mercurial support. Since Heptapod 17.0 (May 2024), its version numbers track GitLab's exactly: Heptapod `x.y.*` is based on GitLab `x.y.*`. Current releases in 2025/2026 are 17.x and 18.x (latest announced: 18.6 in December 2025). Heptapod is **CE-only** — no EE/Premium/Ultimate features are available.

**The single most important fact for MCP design:** On both the web UI and the REST API, Mercurial content is exposed *as if it were Git*. Mercurial named branches appear as GitLab branches named `branch/<hg-branch>` (e.g., `branch/default`). Mercurial topics appear as GitLab branches named `topic/<target-hg-branch>/<topic-name>` (e.g., `topic/default/my-feature`). This means the standard `/repository/branches`, `/merge_requests`, `/repository/tree`, `/repository/files/...` endpoints all Just Work against Heptapod — you just have to know the naming convention when constructing refs.

The main divergences a client library hits are: (1) the `branch/`/`topic/` prefix convention, (2) a couple of Heptapod-exclusive endpoints for hg-specific settings, (3) an extra `hgid`/`hg_after`/`hg_before`/`checkout_hgsha` set of fields in webhooks/payloads for Mercurial projects, (4) absence of every EE-only endpoint, and (5) a few GitLab EE/CE features that require explicit Git mode (personal forks, `.gitlab-ci.yml` with certain merge methods, etc.).

---

## 1. Version tracking and what that implies

Source: [Upgrading Heptapod from an old version](https://heptapod.net/pages/upgrade), [Heptapod 1.0 and a roadmap for 2024](https://heptapod.net/heptapod-1-0-2024-roadmap).

- **From 17.0 onwards** Heptapod `x.y` is always based on GitLab `x.y`, with the patch number usually differing (e.g., Heptapod 17.6.1 is built on GitLab 17.6.3).
- The latest release announced on the public blog is **Heptapod 18.6** (December 2025), tracking GitLab 18.6. Earlier in 2025: 18.5 (November), 18.4, 18.3, 17.x through 17.11.
- GitLab 17.0 shipped May 2024; GitLab 17.11 was the last 17.x (April 2025); GitLab 18.x began shipping monthly in 2025, 18.6 was released by GitLab in November 2025. Sources: [endoflife.date/gitlab](https://endoflife.date/gitlab), [GitLab releases](https://about.gitlab.com/releases/).
- **Heptapod is always CE-based.** The FAQ explicitly answers "no" to EE features: "Currently no, but if you're interested, tell us" ([Heptapod FAQ](https://heptapod.net/pages/faq.html)).
- Historical lag: pre-17.0 Heptapod could lag several GitLab minors behind (e.g., Heptapod 0.40 = GitLab 16.2, Heptapod 1.1 = GitLab 16.7). If an MCP user is on an old Heptapod (< 17.0), assume they're missing any GitLab feature newer than their base version.

**Implication for MCP:** If you support "recent" GitLab, targeting the GitLab 17.x REST API surface is a safe baseline for both products. Anything introduced in GitLab 18+ should be treated as "present on modern GitLab, present on Heptapod 18.x+, absent on older Heptapod."

---

## 2. The big idea: Heptapod exposes hg as fake-git

Sources: [Heptapod FAQ](https://heptapod.net/pages/faq.html), [Heptapod workflow (py-edu-fr)](https://py-edu-fr.pages.heptapod.net/contribute/mercurial-heptapod.html), [readthedocs issue #6747](https://github.com/readthedocs/readthedocs.org/issues/6747), [The road to fully native Mercurial in Heptapod](https://heptapod.net/the-road-to-fully-native-mercurial-in-heptapod).

Heptapod's web application is still GitLab CE Rails at heart. It addresses repository content through the Gitaly gRPC protocol. For Mercurial projects, a component called **HGitaly** (plus a newer high-performance Rust variant **RHGitaly**) implements the Gitaly protocol for Mercurial. From the Rails app's point of view — and therefore from the REST/GraphQL API's point of view — a Mercurial repository looks like a Git repository, just with funny branch names.

### 2.1 Branch naming convention

| Mercurial concept | GitLab branch name | Git ref seen on the wire |
|---|---|---|
| Named branch `default` | `branch/default` | `refs/heads/branch/default` |
| Named branch `stable` | `branch/stable` | `refs/heads/branch/stable` |
| Topic `foo` targeting `default` | `topic/default/foo` | `refs/heads/topic/default/foo` |
| Topic `foo` targeting `stable` | `topic/stable/foo` | `refs/heads/topic/stable/foo` |
| Bookmarks | **not exposed** (bookmarks on topics are forbidden; instance-wide bookmarks on non-topic changesets are optional and rarely used) |

This is confirmed by actual project URLs (`/-/tree/branch/default`, `/-/tree/topic/default/replacements`), by the Heptapod FAQ, and by a [public webhook payload](https://github.com/readthedocs/readthedocs.org/issues/6747) where `"default_branch": "branch/default"` and `"ref": "refs/heads/branch/py3.6"` show up literally.

**Characters allowed in topic names:** topic names must conform to Git's ref format. Topics ending in `.lock` are rejected (Git reserves that suffix). See [heptapod#347](https://foss.heptapod.net/heptapod/heptapod/-/issues/347).

### 2.2 Commit identifiers

There are two regimes:

1. **Legacy `hg_git` projects** (created before Heptapod 0.25, ~Sept 2021). These ran Mercurial content through an internal Mercurial→Git conversion. The GitLab API returns **Git SHA-1 commit hashes** — the hg user cannot easily reconcile them with their local `hg log` output.
2. **Native `hg` projects** (default since Heptapod 0.25). The Rails app sees Mercurial changeset hashes directly. `id` fields in API responses are Mercurial node hashes (40 hex chars, same format as Git SHA-1, but an entirely different hash space).

Consequence: **you cannot assume a commit `id` in a Heptapod API response is a Git hash.** For a given commit, the identifier returned depends on `vcs_type` of the project. A hash from one project is meaningless against another.

### 2.3 Webhook payloads carry both identifiers

Confirmed against a [real read-the-docs integration report](https://github.com/readthedocs/readthedocs.org/issues/6747) and [heptapod#196](https://foss.heptapod.net/heptapod/heptapod/-/issues/196):

Push events from Mercurial projects include Heptapod-only fields alongside the GitLab-standard ones:

| Field | Standard GitLab? | Heptapod extra? | Meaning |
|---|---|---|---|
| `before`, `after` | yes | — | Git SHAs (may be Mercurial hashes on native projects) |
| `checkout_sha` | yes | — | same |
| `hg_before`, `hg_after` | no | **yes** | Mercurial changeset hashes |
| `checkout_hgsha` | no | **yes** | Mercurial changeset hash of HEAD |
| `commits[*].id` | yes | — | Git SHA or hg hash (depends on vcs_type) |
| `commits[*].hgid` | no | **yes** | Mercurial changeset hash for that commit |
| `ref` | yes | — | `refs/heads/branch/<name>` or `refs/heads/topic/<target>/<name>` |
| `project.default_branch` | yes | — | literal string `"branch/default"` for most hg projects |

MCP consumers that care about commit identifiers should prefer `hgid`/`hg_after` when present, and fall back to `id`/`after` otherwise.

---

## 3. REST API endpoint-by-endpoint comparison

Unless stated otherwise, Heptapod supports the standard GitLab v4 REST API (`/api/v4/...`). The only Heptapod-*exclusive* endpoints documented are the HGRC project-settings endpoints and a VCS-type statistics endpoint. Everything else is either "works identically, subject to the branch naming convention" or "GitLab EE-only and therefore 404/403 on Heptapod."

### 3.1 Heptapod-exclusive endpoints

Sources: [Heptapod FAQ §hgrc](https://heptapod.net/pages/faq.html), [Heptapod 0.29](https://heptapod.net/heptapod-0.29.html), [Help us polishing the native migration](https://heptapod.net/help-us-polishing-the-native-migration.html).

| Method | Path | Since | Purpose |
|---|---|---|---|
| `PUT` | `/api/v4/projects/:id/hgrc` | 0.8+ | Set high-level Mercurial settings for a project. Body (JSON): `inherit` (bool, required), `allow_bookmarks` (bool), `allow_multiple_heads` (bool), `auto_publish` ("nothing" \| "non-topic" \| "all"). **Fields not sent are erased** — the API is not a PATCH. |
| `GET` | `/api/v4/projects/:id/hg_heptapod_config` | 0.8+ | Read the high-level (structured) Mercurial settings. |
| `GET` | `/api/v4/projects/:id/hgrc` | later | Read the full raw HGRC with audit log (Maintainer-level). |
| `GET` | `/api/v4/projects/vcs_type_stats` | 0.17+ | Instance-wide project counts per VCS type. Response includes keys like `hg`, `hg_git`, `git`. |

The hgrc endpoint requires Maintainer-or-higher on the project. On a GitLab instance these paths return 404. On Heptapod they return JSON for Mercurial projects and (for Git projects) a 400/404-style error depending on route.

### 3.2 Standard GitLab endpoints: behavior on Heptapod

The following all work on Heptapod against Mercurial projects, with the caveat that refs follow the `branch/...` and `topic/.../...` convention:

| Endpoint | Works on hg? | Notes |
|---|---|---|
| `GET /api/v4/projects` | yes | Instance lists both hg and git projects. See §3.3 for response schema changes. |
| `GET /api/v4/projects/:id` | yes | Response includes the `default_branch` field which will be `"branch/default"` for most hg projects. |
| `GET /api/v4/projects/:id/repository/branches` | yes | Returns entries like `{"name": "branch/default", ...}` and `{"name": "topic/default/my-feature", ...}`. |
| `GET /api/v4/projects/:id/repository/branches/:branch` | yes | `:branch` must be **URL-encoded**, including the slashes inside `branch/default` or `topic/default/foo` (`branch%2Fdefault`, `topic%2Fdefault%2Ffoo`). |
| `POST /api/v4/projects/:id/repository/branches` | **partial** | Server-side branch/topic creation is limited because Heptapod requires a Mercurial push to create a real topic with a draft changeset. Creating arbitrary "branches" from the web UI is restricted for hg projects. Treat as unreliable. |
| `DELETE /api/v4/projects/:id/repository/branches/:branch` | partial | Similar limitations; topics are normally retired by landing or obsoleting changesets, not by REST delete. |
| `GET /api/v4/projects/:id/repository/tags` | yes | Mercurial tags are exposed as git-style tags. |
| `GET /api/v4/projects/:id/repository/tree?ref=...` | yes | `ref` accepts `branch/default`, `topic/default/foo`, or a commit id (hg node hash or git SHA depending on vcs_type). **Does NOT accept hg revsets.** |
| `GET /api/v4/projects/:id/repository/files/:file_path?ref=...` | yes | Same ref rules as tree. |
| `GET /api/v4/projects/:id/repository/commits?ref_name=...` | yes | Same ref rules. |
| `GET /api/v4/projects/:id/repository/commits/:sha` | yes | `:sha` is the identifier the project advertises (hg node for native, git SHA for legacy). |
| `GET /api/v4/projects/:id/repository/commits/:sha/diff` | yes | ditto. |
| `GET /api/v4/projects/:id/merge_requests` | yes | MR `source_branch` / `target_branch` will contain strings like `topic/default/foo` and `branch/default`. |
| `POST /api/v4/projects/:id/merge_requests` | yes, with caveats | You must pass `source_branch=topic/<target_hg_branch>/<topic_name>` and `target_branch=branch/<target_hg_branch>`. The topic must already exist (i.e., have been pushed via `hg push`). MR auto-creation on push is *not* done — user gets a link in the hg push output. |
| `PUT /api/v4/projects/:id/merge_requests/:iid/merge` | partial | See §4 on merge methods and subrepository restrictions. |
| `GET /api/v4/projects/:id/issues` | yes | Generic GitLab issues. No hg-specific fields. |
| `GET /api/v4/projects/:id/pipelines` | yes | Standard GitLab CI. Pipelines run against the git-ish view, so `.gitlab-ci.yml` works as usual. |
| `GET /api/v4/projects/:id/jobs` | yes | Identical. |
| `GET /api/v4/version`, `GET /api/v4/metadata` | yes | Returns `version`, `revision`, `enterprise` fields. **`enterprise` will always be `false` on Heptapod.** See §9 for detection strategy. |
| `POST /api/v4/projects/import` | partial | Full imports work for Git; Mercurial import is partial. Bitbucket import of hg repos is supported in a minimal form (FAQ). |

### 3.3 Project response schema differences

Sources: [Heptapod 0.17 blog](https://heptapod.net/heptapod-0170rc1-released-with-3-tech-previews.html), [Heptapod 0.29 blog](https://heptapod.net/heptapod-0.29.html), [VCS type stats endpoint discussion](https://heptapod.net/help-us-polishing-the-native-migration.html).

Heptapod added a `vcs_type` column to projects in its 0.17 cycle (early 2021). The [VCS type statistics endpoint](https://heptapod.net/help-us-polishing-the-native-migration.html) confirms the possible values:

- `git` — a normal Git project. GitLab behavior, no Mercurial anywhere.
- `hg` — a native Mercurial project (default since 0.25).
- `hg_git` — a legacy Mercurial project with Git conversion (projects created before 0.25, or old imports).

Whether a single-project `GET /projects/:id` response exposes `vcs_type` as a top-level field is not explicitly documented in the Heptapod release notes I could find (the 0.29 post says only "the migration to native Mercurial is now exposed in the REST API"). In practice, community reports and the [VCS type stats endpoint](https://heptapod.net/help-us-polishing-the-native-migration.html) indicate `vcs_type` is addressable through the API. **The safe assumption for MCP code: treat the field as optional; when present, use it to branch behavior; when absent, infer from `default_branch` matching `branch/...` or from a presence-probe of the hgrc endpoint.**

Standard GitLab `GET /projects/:id` does **not** include any `vcs_type` field — so unconditionally reading it is a detection signal in itself.

### 3.4 Endpoints that are GitLab EE/Premium/Ultimate only

Heptapod is CE-only. Anything marked "Premium" or "Ultimate" in the [GitLab REST API docs](https://docs.gitlab.com/api/) is **not available** on Heptapod:

- Epics (`/groups/:id/epics`, `/groups/:id/epics/:epic_iid/...`) — EE Premium
- Iterations (`/projects/:id/iterations`) — EE Premium
- Push rules (`/projects/:id/push_rule`) — EE Premium
- Merge request approvals configuration (`/projects/:id/approvals`, approval rules) — EE Premium
- Code owners enforcement — EE Premium
- Protected environments (`/projects/:id/protected_environments`) — EE Premium
- Vulnerabilities, security dashboards — EE Ultimate
- Dependency scanning / SAST / DAST report endpoints — EE Ultimate
- Requirements management — EE Ultimate
- GitLab Duo / AI features — EE Premium/Ultimate
- Value Stream Analytics — EE Premium/Ultimate
- License compliance — EE Ultimate

On Heptapod these will return 403 (with a license message) or 404. The metadata `enterprise: false` flag is the authoritative way to know in advance.

---

## 4. Merge Requests in detail

Sources: [Heptapod Merge Request Quick Start Guide](https://heptapod.net/pages/quick-start-guide.html), [Heptapod's default workflow blog](https://octobus.net/blog/2019-09-04-heptapod-workflow.html), [heptapod#670](https://foss.heptapod.net/heptapod/heptapod/-/issues/670), [heptapod#311](https://foss.heptapod.net/heptapod/heptapod/-/issues/311).

### 4.1 Creating an MR

1. A developer runs `hg topic my-feature` locally, commits draft changesets, and runs `hg push`.
2. The server accepts the topic. It does *not* create an MR automatically; it returns a link to create one.
3. The MR is created through the Web UI **or** via the REST `POST /projects/:id/merge_requests` endpoint, with `source_branch = topic/<target-hg-branch>/my-feature` and `target_branch = branch/<target-hg-branch>`.
4. The topic must already exist on the server. Unlike GitHub-style flows, you cannot create a PR for a branch that hasn't been pushed yet.

**MCP tool implication:** a `create_merge_request` tool should, when talking to an hg project, require the caller to specify the hg target branch explicitly, and construct `source_branch`/`target_branch` from it. Or require the caller to pass fully-formed GitLab branch strings. Avoid any "auto-detect source branch from current git HEAD" logic.

### 4.2 Merge methods

- **Merge commits are forbidden for hg projects.** Heptapod only allows fast-forward-style merges. This is a design choice: in Mercurial's topic workflow, landing a topic means publishing its changesets onto the target named branch, not creating a merge commit. Merge commits via web UI are explicitly disallowed when there's no fast-forward path.
- **Squash-and-merge** is supported in principle (the underlying HGitaly `UserCommitFiles`/`Rebase` support landed in 18.x), but the default workflow is "evolve your topic locally so it fast-forwards cleanly."
- **Projects with Mercurial subrepositories** cannot be merged server-side at all. Also, online file editing via the API is disabled for these projects. See [heptapod#311](https://foss.heptapod.net/heptapod/heptapod/-/issues/311).
- **Changing source/target branch of an open MR** is reportedly buggy on hg projects (see [heptapod#670](https://foss.heptapod.net/heptapod/heptapod/-/issues/670)) — pipelines may get stuck in "Checking pipeline status". Treat the `PUT /merge_requests/:iid` endpoint as "don't change source/target branch at runtime" for Mercurial projects.

### 4.3 Diff format

Diffs are served through Gitaly/HGitaly and come back as unified-diff text like on GitLab. There is **no hg-format diff** in the REST API. Clients that want `hg log --patch`-style output must fetch via a Mercurial client directly.

### 4.4 Commit identifiers in MR responses

`sha`, `merge_commit_sha`, `squash_commit_sha`, and the entries in `/merge_requests/:iid/commits` will contain whatever identifier format the project uses (hg node for native, git SHA for legacy). They match the format seen elsewhere in the same project — they do **not** flip between modes within a single MR.

---

## 5. Repository browsing details

For all of the following, `:ref` accepts any of:

- A full commit identifier (40 hex chars — hg node for native, git SHA for legacy/git)
- A short commit identifier
- A branch name in the Heptapod exposition: `branch/default`, `branch/stable`, `topic/default/foo`, `topic/stable/bar`
- A tag name

It does **not** accept:

- Mercurial revsets (e.g., `::tip`, `tip~1`, `children(abc)`)
- Mercurial bookmark names (Heptapod generally doesn't expose bookmarks)
- Bare hg branch names without the `branch/` prefix (e.g., `default` alone — must be `branch/default`)

**URL encoding is mandatory.** `branch/default` in a path parameter must be `branch%2Fdefault`. Query parameters may or may not require encoding depending on the client; python-gitlab handles this correctly.

### 5.1 `/projects/:id/repository/tree`

Standard GitLab behavior. Recursive listing works. Pagination works. On native hg projects, `commit_id` fields in tree-entry responses are hg node hashes.

### 5.2 `/projects/:id/repository/files/:file_path`

Standard. `file_path` is URL-encoded. `ref` follows §5 rules. The `blob_id`, `commit_id`, `last_commit_id` fields follow the same identifier-format rule as §2.2.

### 5.3 `/projects/:id/repository/commits`

Standard. Parameters like `since`, `until`, `path`, `all`, `ref_name` work. The `id`, `short_id`, `parent_ids` are hg nodes on native projects.

### 5.4 `/projects/:id/repository/branches`

Returns `name` fields prefixed with `branch/` or `topic/...`. **Always sort client-side if you want the "main" branch first** — Heptapod does not guarantee the named branches will come before topics in the listing. The project's actual default is in `project.default_branch`.

### 5.5 `/projects/:id/repository/tags`

Works. Mercurial tags (local and global) are exposed. Because Mercurial tag semantics differ (tags in hg are stored in `.hgtags`, not refs), some edge cases may differ in creation/deletion behavior. Read-only access is reliable.

---

## 6. Authentication

Sources: [Pulling and pushing over HTTP using a Personal Access Token](https://heptapod.net/pages/tuto-repo-http-access-token.html), [Heptapod FAQ](https://heptapod.net/pages/faq.html).

- **Personal Access Tokens (PATs):** identical to GitLab. Scopes `api`, `read_api`, `read_user`, `read_repository`, `write_repository`, `read_registry`, `write_registry` all work. Use them as `PRIVATE-TOKEN` header or `Authorization: Bearer ...`.
- **OAuth2:** the standard GitLab OAuth endpoints (`/oauth/authorize`, `/oauth/token`) work. Heptapod has not published any OAuth-specific divergences.
- **Project access tokens:** these are a standard CE feature and have been in GitLab since 14.7 (2022). They work on Heptapod versions based on GitLab 14.7+ (i.e., Heptapod 0.35 / 1.x / 17.x / 18.x). On older Heptapod they may be absent.
- **Group access tokens:** were made available in GitLab CE at some point and are present in recent Heptapod.
- **Job tokens:** standard CI feature, works.
- **Deploy tokens / deploy keys:** work.
- **2FA:** supported. PATs are the way to authenticate Mercurial HTTP clones/pushes when 2FA is on.
- **SSO:** Heptapod Hosting specifically advertises Clever Cloud SSO; self-hosted supports the usual LDAP/SAML/OmniAuth providers present in CE. EE-only SSO providers (SCIM, Kerberos restrictions, etc.) are not available.

There is no evidence of Heptapod-specific scopes or token types in any documentation I could find.

---

## 7. CI/CD and pipelines

Sources: [Heptapod FAQ](https://heptapod.net/pages/faq.html), [heptapod/heptapod CI files](https://foss.heptapod.net/heptapod/heptapod/-/blob/aba57ef42b93b7ceb25de4a294b0bb5d20c556c7/.gitlab-ci.yml).

- **`.gitlab-ci.yml` works identically.** CI jobs run against the git-ish view of the Mercurial repository (for native projects, this view is produced on demand by HGitaly; for legacy projects, it's the auxiliary git repo).
- **CI runners:** standard GitLab runner. Heptapod maintains a fork called `heptapod-runner` that adds Mercurial clone support so jobs can clone the original hg repository; the standard runner falls back to cloning a Git representation.
- **Pipelines API:** unchanged. `GET /projects/:id/pipelines`, `POST /projects/:id/pipeline`, jobs API, artifacts API — all identical.
- **`CI_COMMIT_SHA`** in job environments is a Git-style SHA (the one Gitaly/HGitaly exposes to the runner), not a Mercurial node hash. If your pipeline needs the hg hash, consult `CI_COMMIT_HG_SHA` if the project is configured to expose it, or use `hg id` inside the job.
- **Auto-DevOps** features that depend on EE (like vulnerability scanning) won't run; the CE Auto-DevOps templates do run.
- **.gitlab-ci.yml validation endpoint** (`POST /projects/:id/ci/lint`) works.

---

## 8. GraphQL API

Sources: [Heptapod 17.1.1 rubocop GraphQL enum values](https://foss.heptapod.net/heptapod/heptapod/-/blob/heptapod-17.1.1/rubocop/cop/graphql/enum_values.rb), source tree of `spec/graphql/types`.

- Heptapod **does expose `/api/graphql`** — the upstream GitLab GraphQL API is inherited verbatim from the CE base.
- **Coverage gaps are the same as REST:** EE/Premium/Ultimate types (epics, iterations, vulnerabilities, requirements, value stream, etc.) are simply absent. Querying them returns a schema error.
- **No Heptapod-specific GraphQL types** are documented. There is no `Mercurial`-prefixed resolver, no `HgTopic` GraphQL type — hg topics appear as the same `Branch` GraphQL type, with the name being `topic/default/foo`.
- **Branch/commit fields** return the same values as the REST API (hg hashes for native projects, git SHAs otherwise).

For an MCP server, the practical upshot: **if you use REST, keep using REST; GraphQL will not give you more information about hg-specific things** and will have the same EE gaps.

---

## 9. Detecting Heptapod at runtime

Sources: [Metadata API docs](https://docs.gitlab.com/api/metadata/), heptapod-tests conventions, field evidence from §3.3 and §2.3.

There is **no official Heptapod discriminator header or metadata field.** The Heptapod `/api/v4/version` and `/api/v4/metadata` return the same schema as GitLab: `{version, revision, enterprise, kas}`. The `version` field will look like `"18.6.0"` — the same format a GitLab 18.6 instance would return — so you cannot distinguish on `version` alone.

Practical detection strategies, in order of preference:

1. **Probe `GET /api/v4/projects/vcs_type_stats`** (unauthenticated or with a read token). On Heptapod this returns a JSON body with keys `hg`, `hg_git`, `git`. On GitLab this returns 404. This is the cleanest positive detection signal I could find, because the endpoint is documented in Heptapod release notes and has no equivalent path on GitLab. [Source](https://heptapod.net/help-us-polishing-the-native-migration.html).
2. **Check `enterprise == false` in `/api/v4/metadata`.** This alone is weak (it also matches GitLab CE), but combined with (1) or the server URL, it's a useful prior.
3. **Probe an `hgrc` endpoint for any known project**, e.g., `GET /api/v4/projects/:id/hg_heptapod_config`. Returns JSON on Heptapod hg projects, 404 on GitLab or Heptapod git projects.
4. **Inspect the instance hostname.** `foss.heptapod.net`, `heptapod.host`, `*.pages.heptapod.net` are obvious. This should only be used as a hint, never as the sole signal.
5. **Look at a project's `default_branch`.** If it's literally the string `"branch/default"`, the project is almost certainly hg-on-Heptapod. A GitLab project can in theory use the same name, but no sane team does.
6. **Look at `vcs_type` on a project**, if present. Absent on GitLab; present and one of `git`/`hg`/`hg_git` on Heptapod.

Recommended order in code: **try (1) once per instance**, cache the result, fall back to the other signals if the probe fails due to permissions. Don't run detection on every request.

There is no evidence of a custom HTTP response header like `X-Heptapod-Version`. The only Heptapod-specific header documented (`X-Heptapod-Permission-User`) is internal traffic between GitLab workhorse and hgweb — it is not exposed to clients.

---

## 10. Known client library compatibility

- **python-gitlab**: I could not find any reports of python-gitlab breaking against Heptapod. The primary risk is code that assumes `default_branch == "main"` or `"master"`. python-gitlab itself does not make such assumptions; your MCP server must not either.
- **go-gitlab**: same status — no public bug reports against Heptapod.
- **ruby `gitlab` gem**: same.
- **Renovate bot**: has a long-running [discussion #38564](https://github.com/renovatebot/renovate/discussions/38564) about adding first-class Hg support for Heptapod; as of research date it's community-maintained, not officially supported.
- **Read the Docs**: [known bug](https://github.com/readthedocs/readthedocs.org/issues/6747) where RTD's branch-matching logic didn't handle `refs/heads/branch/<name>` correctly. This is the clearest real-world example of a client that broke because of the `branch/` prefix convention.

**MCP server implication:** assume any hardcoded branch-name matching (regex `^(main|master|develop)$`, string equality to `"main"`, etc.) will silently misbehave on Heptapod hg projects. Always resolve via `project.default_branch` and quote the full returned string.

---

## 11. Other things that are limited or absent on Heptapod

From the [Heptapod FAQ](https://heptapod.net/pages/faq.html) and [Heptapod Hosting features page](https://about.heptapod.host/features.html):

- **Personal forks of Mercurial projects:** not supported. (Forks of Git projects are fine.)
- **Narrow clones:** not supported.
- **Git LFS / Mercurial LFS:** work in progress, patchy support.
- **Clone bundles:** not supported.
- **Cherry-pick, revert, squash (server-side)** for hg projects: limited. The "right" workflow is to evolve the topic locally.
- **Subrepository-using projects:** server-side commits, online file edits, and web-UI-triggered merges are all disabled to avoid corrupting `.hgsubstate`.
- **Online file editor:** works for git and native hg projects, blocked for hg projects with subrepositories or certain legacy configurations.
- **Bitbucket import:** minimal — can import hg repos but metadata support is limited.
- **Web IDE:** works on a per-project basis depending on VCS type; treat as unreliable for hg.
- **External issue trackers and some CI/bot integrations:** "Some work only for Git projects" per the FAQ.
- **`.hgsub` / `.hgsubstate` files:** hidden from the web UI deliberately.

---

## 12. Open questions / where information was scarce

- **Exact response schema of `/projects/:id`** on Heptapod — specifically whether `vcs_type` is always present as a top-level field. The 0.29 release notes say "exposed in the REST API" but I could not find a markdown API doc confirming the exact field name on the project entity. My recommendation is to probe defensively.
- **Does `/projects/:id/repository/branches` always return entries with `branch/`/`topic/` prefixes**, or are there configurations where it returns bare `default`? All evidence points to "always prefixed for hg projects" but I did not find a source explicitly stating "never strips the prefix."
- **GraphQL Branch type behavior** for hg topics — inferred from REST parity but not directly tested in published docs.
- **Whether `/api/v4/metadata` has any Heptapod-added fields.** Nothing in my sources suggests it does, but I cannot prove a negative.
- **Exact EE feature matrix per Heptapod version.** Heptapod is CE-based, so the rule is "no EE features," but some features have moved between tiers in GitLab history. Cross-reference against the specific GitLab version your Heptapod is tracking.

---

## 13. Recommendations for MCP design

Concrete guidance for building the MCP server at `/home/ari/src/mcps/gitlab-mcp`.

### 13.1 Target API surface for maximum compatibility

Build primarily against the **GitLab 17.x CE REST API**. That surface is present on:
- GitLab 17.x and newer CE/EE (EE just adds more)
- GitLab 18.x CE/EE
- Heptapod 17.x (GitLab 17.x CE)
- Heptapod 18.x (GitLab 18.x CE)

Avoid EE-only endpoints in the core tool set. If you want to support them, put them behind an `enterprise=true` guard from the metadata probe.

**Core endpoints the MCP should expose as tools:**

- `/projects` — list, get, create, update (skip EE-only fields like approvals)
- `/projects/:id/repository/branches` — list, get (accept full ref name as-is; never strip `branch/` or `topic/` prefixes)
- `/projects/:id/repository/tags` — list, get
- `/projects/:id/repository/tree` — list with `ref`
- `/projects/:id/repository/files/:path` — get with `ref`, get raw
- `/projects/:id/repository/commits` — list, get, get diff, get refs containing a commit
- `/projects/:id/repository/compare` — git-style compare (works on both)
- `/projects/:id/merge_requests` — list, get, create, update, merge, get commits, get diffs, get approvals only if EE
- `/projects/:id/issues` — list, get, create, update, notes
- `/projects/:id/pipelines` — list, get, create, retry, cancel
- `/projects/:id/jobs` — list, get, trace, artifacts
- `/users/:id`, `/groups/:id` — read-only is safe

### 13.2 Where to add Heptapod-specific branches in code

Keep it minimal. Put a thin abstraction at these points:

1. **A `Backend` struct or dataclass** that holds `{kind: "gitlab" | "heptapod", version: str, vcs_types_supported: set[str]}`. Populate it on first contact via the detection strategy in §9.
2. **A `resolve_default_branch(project)` helper** that returns `project["default_branch"]` literally. No fallback to `"main"` or `"master"`. Document that callers must use the returned string verbatim.
3. **A `ref_from_hg_topic(target_branch: str, topic_name: str) -> str` helper** that returns `f"topic/{target_branch}/{topic_name}"`. Only used when the caller has an hg context (i.e., calling `create_merge_request` and knows the target is hg).
4. **A `parse_ref(ref: str) -> RefKind` helper** that can categorize any branch name returned by the API: `"branch/foo"` → `HgNamedBranch("foo")`, `"topic/foo/bar"` → `HgTopic(target="foo", name="bar")`, anything else → `Opaque(ref)`. This lets tool responses be richer (`"this is an hg topic called 'bar' targeting the 'foo' named branch"`) without changing the underlying API call.
5. **A commit-identifier normalizer** that exposes both `id` (as returned) and `hgid` (if present in webhooks or if the MCP later adds a lookup tool) rather than assuming one format.
6. **A conditional `hgrc` tool** — expose `get_project_hg_config` and `update_project_hg_config` tools **only when** the detected backend is Heptapod. These map to `GET /projects/:id/hg_heptapod_config` and `PUT /projects/:id/hgrc` respectively. Include the warning that `PUT` overwrites unsent fields.
7. **Webhook-parsing tool** (if the MCP exposes one) should surface `hgid`, `hg_before`, `hg_after`, `checkout_hgsha` as first-class fields when present, not drop them.

### 13.3 GitLab-only features: skip or mark unsupported

When the backend is detected as Heptapod OR when `enterprise == false` in metadata, mark these tools as unavailable (or return a clean "not supported on this backend" error):

- Epics and their notes
- Iterations and iteration cadences
- Push rules
- MR approval rules (not approvals — approvals work on CE; approval *rules* are EE)
- Protected environments
- Vulnerabilities, security dashboards, dependency scanning reports
- License compliance
- Requirements
- Value Stream Analytics (Premium)
- GitLab Duo/AI features
- Code owners *enforcement* (pattern parsing is CE, enforcement is EE)
- Audit events API (partial — some endpoints are EE)

Do not blacklist them at compile time — discover at runtime via the metadata probe, because a GitLab CE instance has the same gap.

### 13.4 Backend detection at runtime

Recommended implementation:

```
on first API call to a new instance:
  1. GET /api/v4/metadata (cache version, revision, enterprise)
  2. GET /api/v4/projects/vcs_type_stats
     - 200 with {hg, hg_git, git} keys -> backend = "heptapod"
     - 404 or 501 -> backend = "gitlab"
  3. cache { backend, version, enterprise } keyed by base_url
  4. expose the cached value through an internal accessor; do not re-probe on every tool call
```

Make the detection result visible to LLM callers through a tool like `describe_instance` so the agent can reason about what's available. This is cheap and very helpful for debugging.

For **per-project** behavior differences, prefer a second lazy probe: the first time a project is touched, inspect `default_branch`, and optionally call the VCS-type endpoint if you want to be certain. Cache per-project.

### 13.5 Documentation and test fixtures

- Write integration tests against **both** a `gitlab/gitlab-ce` Docker image and an `octobus/heptapod` Docker image ([octobus/heptapod on Docker Hub](https://hub.docker.com/r/octobus/heptapod) — note: recent production images may require the free account setup described in the [2025 commercial policy](https://heptapod.net/2025-commercial-policy.html); release candidates remain freely available).
- Include test fixtures that cover: a git project on Heptapod, a native hg project, and a legacy `hg_git` project if feasible.
- Explicitly test: branch listing returns `branch/default`, MR creation with `source_branch=topic/default/foo`, file fetch with `ref=branch/default`, commit lookup by hg node hash.
- Document in the MCP README that Heptapod users should expect branch names like `branch/default` in tool output and that this is not a bug.

### 13.6 What to absolutely avoid

- Hardcoding `"main"` or `"master"` anywhere.
- String-matching commit hashes as "git-looking SHA-1s" — they can be hg node hashes, which look identical.
- Stripping or rewriting the `branch/`/`topic/` prefix before passing refs to the server.
- Assuming bookmarks exist on hg projects.
- Assuming server-side cherry-pick, revert, squash work on hg projects.
- Auto-detecting source branch from a local git clone — the user may be working in hg.
- Treating `enterprise: false` as "definitely GitLab CE, not Heptapod" — it can be either.

---

## 14. Sources cited

Primary Heptapod sources:

- [Heptapod FAQ](https://heptapod.net/pages/faq.html)
- [Heptapod Merge Request Quick Start Guide](https://heptapod.net/pages/quick-start-guide.html)
- [Heptapod project landing page](https://heptapod.net/heptapod/)
- [Upgrading Heptapod from an old version](https://heptapod.net/pages/upgrade)
- [Heptapod 0.17.0rc1 released with 3 tech previews](https://heptapod.net/heptapod-0170rc1-released-with-3-tech-previews)
- [Heptapod 0.25.0 released, featuring GitLab 14.2](https://heptapod.net/heptapod-0.25)
- [Heptapod 0.26.0rc1 released, 1.0 now in sight](https://heptapod.net/heptapod-0.26)
- [Heptapod 0.29 released and general development news](https://heptapod.net/heptapod-0.29)
- [Heptapod 0.40.0 released](https://heptapod.net/heptapod-0.40)
- [Heptapod 1.0 and a roadmap for 2024](https://heptapod.net/heptapod-1-0-2024-roadmap)
- [Help us polishing the native migration](https://heptapod.net/help-us-polishing-the-native-migration.html)
- [The road to fully native Mercurial in Heptapod](https://heptapod.net/the-road-to-fully-native-mercurial-in-heptapod)
- [A new commercial policy in 2025 for Heptapod](https://heptapod.net/2025-commercial-policy.html)
- [Heptapod 18.5 retrospective](https://heptapod.net/18-5-retrospective.html)
- [Heptapod announcements category](https://heptapod.net/category/announcements)
- [Heptapod Hosting features page](https://about.heptapod.host/features.html)
- [Heptapod's default workflow (Octobus blog)](https://octobus.net/blog/2019-09-04-heptapod-workflow.html)

Related community sources:

- [Heptapod workflow — py-edu-fr contributor guide](https://py-edu-fr.pages.heptapod.net/contribute/mercurial-heptapod.html)
- [Fluiddyn Heptapod workflow docs](https://fluidhowto.readthedocs.io/en/latest/mercurial/heptapod-workflow.html)
- [Heptapod wiki page on mercurial-scm.org](https://wiki.mercurial-scm.org/Heptapod)

Issue tracker evidence (foss.heptapod.net):

- [heptapod#196 — webhooks do not include the hg revision](https://foss.heptapod.net/heptapod/heptapod/-/issues/196)
- [heptapod#311 — subrepositories and server-side operations](https://foss.heptapod.net/heptapod/heptapod/-/issues/311)
- [heptapod#347 — topic names need to conform to git's refs format](https://foss.heptapod.net/heptapod/heptapod/-/issues/347)
- [heptapod#670 — changing source/target branch breaks the merge button](https://foss.heptapod.net/heptapod/heptapod/-/issues/670)
- [heptapod#755 — cannot find merge request linked to a topic](https://foss.heptapod.net/heptapod/heptapod/-/issues/755)

Real-world third-party integration evidence:

- [Read the Docs issue #6747 — heptapod webhook not triggering build](https://github.com/readthedocs/readthedocs.org/issues/6747) — contains a full Heptapod push webhook payload example showing `hg_after`, `hg_before`, `checkout_hgsha`, `hgid`, and `ref: "refs/heads/branch/default"`.
- [Renovate discussion #38564 — Add Mercurial (Hg) VCS Support for Heptapod Integration](https://github.com/renovatebot/renovate/discussions/38564)

GitLab reference docs used for comparison:

- [GitLab REST API index](https://docs.gitlab.com/api/rest/)
- [GitLab Metadata API](https://docs.gitlab.com/api/metadata/)
- [GitLab Projects API](https://docs.gitlab.com/api/projects/)
- [GitLab Merge Requests API](https://docs.gitlab.com/api/merge_requests/)
- [GitLab Commits API](https://docs.gitlab.com/api/commits/)
- [endoflife.date/gitlab](https://endoflife.date/gitlab) for GitLab release dates
