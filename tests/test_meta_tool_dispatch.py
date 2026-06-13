"""End-to-end dispatch via the meta-tool layer.

Confirms that override pre-flight guards still fire when the operation comes
in through `server._dispatch(group, op, params)` (the path taken by an
MCP-tool call), and that fully-typed generated ops correctly reject extras.
"""

from __future__ import annotations

import httpx
import pytest

from gitlab_mcp.backend import InstanceInfo
from gitlab_mcp.client import GitLabClient, _reset_client
from gitlab_mcp.config import _reset_settings


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    monkeypatch.setenv("GITLAB_URL", "https://gitlab.example.com")
    monkeypatch.setenv("GITLAB_TOKEN", "test-token")
    _reset_settings()
    _reset_client()
    yield
    _reset_settings()
    _reset_client()


def _seed_and_register(backend: str, handler=None):
    """Install a seeded client and register the full tool set.

    Tests that go through `_dispatch` need `_register_tools()` to have built
    the per-op Pydantic models; otherwise the in-test fallback path would
    rebuild them on every call (correct, but masks the cached path).
    """
    transport = httpx.MockTransport(handler or (lambda req: httpx.Response(404)))
    client = GitLabClient(transport=transport)
    client.instance = InstanceInfo(
        backend=backend,  # type: ignore[arg-type]
        version="18.6.0",
        enterprise=False,
        vcs_types_supported=(
            {"git", "hg", "hg_git"} if backend == "heptapod" else {"git"}
        ),
        url="https://gitlab.example.com",
    )
    import gitlab_mcp.client as client_mod
    client_mod._client = client

    from gitlab_mcp import server
    server._register_tools()
    return client


# ── Shadowed overrides — pre-flight guards must trigger via _dispatch ──────


class TestOverridePreFlightGuardsViaDispatch:
    def test_projects_create_visibility_guard(self):
        _seed_and_register("gitlab")
        from gitlab_mcp import server

        with pytest.raises(ValueError, match="Public/internal projects not allowed"):
            server._dispatch(
                "ProjectsCreate", "gitlab_write",
                {"name": "x", "visibility": "public"},
            )

    def test_groups_create_visibility_guard(self):
        _seed_and_register("gitlab")
        from gitlab_mcp import server

        with pytest.raises(ValueError, match="Public/internal"):
            server._dispatch(
                "GroupsCreate", "gitlab_write",
                {"name": "x", "path": "x", "visibility": "public"},
            )

    def test_snippets_create_visibility_guard(self):
        _seed_and_register("gitlab")
        from gitlab_mcp import server

        with pytest.raises(ValueError, match="Public/internal"):
            server._dispatch(
                "SnippetsCreate", "gitlab_write",
                {"title": "x", "visibility": "public"},
            )

    def test_projects_edit_visibility_guard(self):
        _seed_and_register("gitlab")
        from gitlab_mcp import server

        with pytest.raises(ValueError, match="Public/internal"):
            server._dispatch(
                "ProjectsEdit", "gitlab_write",
                {"project_id": 1, "visibility": "public"},
            )

    def test_merge_requests_create_requires_git(self):
        """The MR-create override rejects hg projects with a hint about hg_create_topic_mr."""
        _seed_and_register("heptapod")
        from gitlab_mcp import server
        import gitlab_mcp.client as client_mod

        # The override looks up the project's vcs_type. Patch the lookup to return 'hg'.
        assert client_mod._client is not None
        client_mod._client.project_vcs_type = lambda project_id: "hg"  # type: ignore[method-assign]
        with pytest.raises(ValueError, match="Mercurial"):
            server._dispatch(
                "MergeRequestsCreate", "gitlab_write",
                {
                    "project_id": 1,
                    "source_branch": "x",
                    "target_branch": "y",
                    "title": "z",
                },
            )

    def test_projects_all_brief_default(self):
        """ProjectsAll override slims results when brief=True (the default)."""
        def handler(req):
            return httpx.Response(200, json=[
                {"id": 1, "name": "p1", "path_with_namespace": "g/p1", "junk": "x"},
            ])
        _seed_and_register("gitlab", handler)
        from gitlab_mcp import server

        out = server._dispatch("ProjectsAll", "gitlab_read", {})
        assert isinstance(out, list)
        assert "junk" not in out[0]  # slimmed
        assert out[0]["id"] == 1


# ── Hg-only overrides via dispatch (heptapod backend) ─────────────────────


class TestHgOpsViaDispatch:
    def test_hg_get_config(self):
        sent_paths: list[str] = []

        def handler(req):
            sent_paths.append(req.url.path)
            return httpx.Response(200, json={"vcs_type": "hg"})

        _seed_and_register("heptapod", handler)
        from gitlab_mcp import server

        result = server._dispatch(
            "HgGetConfig", "gitlab_read", {"project_id": 42},
        )
        assert result == {"vcs_type": "hg"}
        assert "/projects/42/hg_heptapod_config" in sent_paths[-1]


# ── Fully-typed generated ops reject extras ───────────────────────────────


class TestStrictRejectionOnTypedGeneratedOp:
    def test_branches_show_rejects_extra_kwarg(self):
        """`branches_show` has no override and no **options after codegen
        (TS Options resolves to Sudo+ShowExpanded only). Extra fields must
        be rejected by the per-op Pydantic model (extra='forbid').
        """
        _seed_and_register("gitlab")
        from gitlab_mcp import server

        with pytest.raises(ValueError, match="Extra inputs are not permitted"):
            server._dispatch(
                "BranchesShow", "gitlab_read",
                {"project_id": 1, "branch_name": "main", "foo": "bar"},
            )

    def test_merge_requests_create_typed_args_accepted(self):
        """Typed required body fields (source_branch, target_branch, title)
        flow through to the wire correctly when called via dispatch.
        """
        body: dict = {}

        def handler(req):
            import json
            body.update(json.loads(req.content))
            return httpx.Response(201, json={"id": 1})

        _seed_and_register("gitlab", handler)
        from gitlab_mcp import server

        server._dispatch(
            "MergeRequestsCreate", "gitlab_write",
            {
                "project_id": 1,
                "source_branch": "feat",
                "target_branch": "main",
                "title": "T",
            },
        )
        assert body["source_branch"] == "feat"
        assert body["target_branch"] == "main"
        assert body["title"] == "T"

    def test_optional_unset_not_in_payload(self):
        """Optional body fields (_UNSET default) must NOT appear in the wire
        payload when the caller omits them.
        """
        body: dict = {}

        def handler(req):
            import json
            body.update(json.loads(req.content))
            return httpx.Response(201, json={"id": 1})

        _seed_and_register("gitlab", handler)
        from gitlab_mcp import server

        server._dispatch(
            "MergeRequestsCreate", "gitlab_write",
            {
                "project_id": 1,
                "source_branch": "feat",
                "target_branch": "main",
                "title": "T",
            },
        )
        assert "description" not in body
        assert "assignee_id" not in body


# ── Codegen overload/selector regressions ─────────────────────────────────


class TestCodegenSignatureRegressions:
    def test_groups_all_projects_simple_is_optional(self):
        """`Groups.allProjects` has two TS overloads; the second (last) has
        `simple: true` as a required prop. Last-overload selection would make
        `simple` required, but `options?:` is optional in both overloads, so
        every property must be optional in the merged shape.
        """
        _seed_and_register("gitlab", lambda req: httpx.Response(200, json=[]))
        from gitlab_mcp import server

        # Must not raise "Field required" for `simple`.
        result = server._dispatch(
            "GroupsAllProjects", "gitlab_read", {"group_id": 1},
        )
        assert result == []

    def test_search_all_accepts_all_scopes(self):
        """`Search.all` has 10 overloads, one per scope value. Last-overload
        would narrow `scope` to a single Literal; we must merge across all
        overloads to recover the full SearchScopes set.
        """
        _seed_and_register("gitlab", lambda req: httpx.Response(200, json=[]))
        from gitlab_mcp import server

        for scope in (
            "projects", "issues", "merge_requests", "milestones",
            "snippet_titles", "wiki_blobs", "commits", "blobs",
            "notes", "users",
        ):
            result = server._dispatch(
                "SearchAll", "gitlab_read",
                {"scope": scope, "search": "x"},
            )
            assert result == [], f"scope={scope!r} rejected"

    def test_search_all_rejects_unknown_scope(self):
        _seed_and_register("gitlab", lambda req: httpx.Response(200, json=[]))
        from gitlab_mcp import server

        with pytest.raises(ValueError, match="Input should be"):
            server._dispatch(
                "SearchAll", "gitlab_read",
                {"scope": "bogus", "search": "x"},
            )

    def test_deploy_keys_all_routes_by_selector(self):
        """Gitbeaker's DeployKeys.all picks between /projects/{id}/deploy_keys,
        /users/{id}/project_deploy_keys, and /deploy_keys based on which
        option is set. Codegen emits a Python dispatch chain so each selector
        hits the right path (not query params on the wrong URL).
        """
        paths: list[str] = []

        def handler(req):
            paths.append(req.url.path)
            return httpx.Response(200, json=[])

        _seed_and_register("gitlab", handler)
        from gitlab_mcp import server

        server._dispatch(
            "DeployKeysAll", "gitlab_read", {"project_id": 42},
        )
        server._dispatch(
            "DeployKeysAll", "gitlab_read", {"user_id": 7},
        )
        server._dispatch("DeployKeysAll", "gitlab_read", {})

        assert paths[0] == "/api/v4/projects/42/deploy_keys"
        assert paths[1] == "/api/v4/users/7/project_deploy_keys"
        assert paths[2] == "/api/v4/deploy_keys"

    def test_users_show_status_routes_by_selector(self):
        """Same as DeployKeys.all: `iDOrUsername` selects between
        /users/{id}/status and /user/status.
        """
        paths: list[str] = []

        def handler(req):
            paths.append(req.url.path)
            return httpx.Response(200, json={})

        _seed_and_register("gitlab", handler)
        from gitlab_mcp import server

        server._dispatch(
            "UsersShowStatus", "gitlab_read", {"i_dor_username": "alice"},
        )
        server._dispatch("UsersShowStatus", "gitlab_read", {})

        assert paths[0] == "/api/v4/users/alice/status"
        assert paths[1] == "/api/v4/user/status"

    def test_search_all_routes_by_selector(self):
        """Search.all also has conditional path: /projects/{id}/search,
        /groups/{id}/search, or /search. scope/search are typed body fields
        across all three.
        """
        paths: list[str] = []
        qs: list[str] = []

        def handler(req):
            paths.append(req.url.path)
            qs.append(str(req.url.query))
            return httpx.Response(200, json=[])

        _seed_and_register("gitlab", handler)
        from gitlab_mcp import server

        server._dispatch(
            "SearchAll", "gitlab_read",
            {"scope": "projects", "search": "x", "project_id": 5},
        )
        server._dispatch(
            "SearchAll", "gitlab_read",
            {"scope": "issues", "search": "y", "group_id": 9},
        )
        server._dispatch(
            "SearchAll", "gitlab_read",
            {"scope": "users", "search": "z"},
        )

        assert paths[0] == "/api/v4/projects/5/search"
        assert paths[1] == "/api/v4/groups/9/search"
        assert paths[2] == "/api/v4/search"
        # scope/search go in query for GET, not in the path:
        for q in qs:
            assert b"scope" in q.encode() or "scope" in q

    def test_branches_show_accepts_sudo(self):
        """`sudo` is a real GitLab API param (Sudo header / query field).
        Methods with otherwise-closed Options types still accept it.
        """
        params_seen: list[bytes] = []

        def handler(req):
            params_seen.append(req.url.query)
            return httpx.Response(200, json={"name": "main"})

        _seed_and_register("gitlab", handler)
        from gitlab_mcp import server

        server._dispatch(
            "BranchesShow", "gitlab_read",
            {"project_id": 1, "branch_name": "main", "sudo": "alice"},
        )
        assert b"sudo=alice" in params_seen[0]


# ── Conditional-dispatch payload correctness ──────────────────────────────


class TestConditionalDispatchPayload:
    def test_path_only_args_not_in_query(self):
        """Path vars that appear in EVERY conditional branch must not also be
        sent as query/body params. Regression: project_id was being added to
        payload even though it's in the URL path for both branches.
        """
        sent: list[tuple[str, bytes]] = []

        def handler(req):
            sent.append((req.url.path, req.url.query))
            return httpx.Response(200, json={})

        _seed_and_register("gitlab", handler)
        from gitlab_mcp import server

        server._dispatch(
            "JobArtifactsRemove", "gitlab_delete",
            {"project_id": 7, "job_id": 42},
        )
        path, qs = sent[0]
        assert path == "/api/v4/projects/7/jobs/42/artifacts"
        assert b"project_id" not in qs

    def test_path_only_args_not_in_json_body(self):
        """Same as above but for a POST: project_id was being JSON-encoded
        into the request body alongside legit fields like `name`.
        """
        bodies: list[dict] = []

        def handler(req):
            import json
            bodies.append(json.loads(req.content))
            return httpx.Response(201, json={"id": 1})

        _seed_and_register("gitlab", handler)
        from gitlab_mcp import server

        server._dispatch(
            "MergeRequestApprovalsCreateApprovalRule", "gitlab_write",
            {
                "project_id": 5,
                "name": "rule-1",
                "approvals_required": 2,
                "mergerequest_iid": 11,
            },
        )
        body = bodies[0]
        assert body == {"name": "rule-1", "approvals_required": 2}

    def test_multi_path_var_positionals_stay_in_path(self):
        """sha + file_identifier appear in BOTH branches of
        PyPI.downloadPackageFile — neither should land in query.
        """
        sent: list[tuple[str, bytes]] = []

        def handler(req):
            sent.append((req.url.path, req.url.query))
            return httpx.Response(200, json={"ok": True})

        _seed_and_register("gitlab", handler)
        from gitlab_mcp import server

        server._dispatch(
            "PyPiDownloadPackageFile", "gitlab_read",
            {
                "sha": "abc123",
                "file_identifier": "pkg-1.0.tar.gz",
                "project_id": 9,
            },
        )
        path, qs = sent[0]
        assert path == "/api/v4/projects/9/packages/pypi/files/abc123/pkg-1.0.tar.gz"
        assert b"sha" not in qs
        assert b"file_identifier" not in qs

    def test_selector_also_in_body_kept(self):
        """Runners.resetRegistrationToken sends `{token, ...options}` in the
        body even though token is also a path-selector. Regression: dispatch
        had body `{}` because selectors were excluded from payload entirely.
        """
        bodies: list[dict] = []
        paths: list[str] = []

        def handler(req):
            import json
            paths.append(req.url.path)
            bodies.append(json.loads(req.content) if req.content else {})
            return httpx.Response(201, json={})

        _seed_and_register("gitlab", handler)
        from gitlab_mcp import server

        # Token-based reset: URL has no token, body MUST include it.
        server._dispatch(
            "RunnersResetRegistrationToken", "gitlab_execute",
            {"token": "tk-1"},
        )
        assert paths[0] == "/api/v4/runners/reset_registration_token"
        assert bodies[0] == {"token": "tk-1"}

        # Runner-id reset: URL has runner_id, body still includes whatever
        # token caller passed (matches gitbeaker's body literal). TS declares
        # runnerId as string only, hence the str value here.
        server._dispatch(
            "RunnersResetRegistrationToken", "gitlab_execute",
            {"runner_id": "9", "token": "tk-2"},
        )
        assert paths[1] == "/api/v4/runners/9/reset_registration_token"
        assert bodies[1] == {"token": "tk-2"}

    def test_runners_remove_detected_as_conditional(self):
        """Runners.remove's fallback URL is the bare endpoint "runners", which
        the old detector filtered as "non-URL". Regression: DELETE went to
        /runners?runner_id=N instead of /runners/N.
        """
        paths: list[str] = []

        def handler(req):
            paths.append(req.url.path)
            return httpx.Response(204)

        _seed_and_register("gitlab", handler)
        from gitlab_mcp import server

        server._dispatch(
            "RunnersRemove", "gitlab_delete", {"runner_id": 9},
        )
        server._dispatch(
            "RunnersRemove", "gitlab_delete", {"token": "tk-x"},
        )
        assert paths[0] == "/api/v4/runners/9"
        assert paths[1] == "/api/v4/runners"

    def test_query_in_branch_path_moves_to_payload(self):
        """`Keys.show` uses `keys?fingerprint=${fingerprint}` in one branch.
        Regression: embedded query was baked into the URL while httpx also
        got `params=payload`, so `KeysShow(fingerprint=..., sudo=root)`
        ended up at `/keys?sudo=root` and lost fingerprint. Codegen now
        splits the `?` out and emits the var into payload instead.
        """
        sent: list[tuple[str, bytes]] = []

        def handler(req):
            sent.append((req.url.path, req.url.query))
            return httpx.Response(200, json={})

        _seed_and_register("gitlab", handler)
        from gitlab_mcp import server

        server._dispatch(
            "KeysShow", "gitlab_read",
            {"fingerprint": "aa:bb", "sudo": "root"},
        )
        path, qs = sent[0]
        assert path == "/api/v4/keys"
        # Both fingerprint and sudo must reach the wire as query params.
        assert b"fingerprint=aa" in qs
        assert b"sudo=root" in qs

        # Keyed branch stays clean — no fingerprint anywhere.
        server._dispatch(
            "KeysShow", "gitlab_read", {"key_id": 7},
        )
        path2, qs2 = sent[1]
        assert path2 == "/api/v4/keys/7"
        assert b"fingerprint" not in qs2

    def test_branch_predicates_use_js_truthiness(self):
        """`Runners.all` JS: `if (scope) ... else if (owned) url = "runners"
        else url = "runners/all"`. Caller passing `owned=False` must route
        to /runners/all (matches falsy `if (owned)`), not /runners.
        Regression: predicates were `is not _UNSET`, so any False/0/"" still
        routed as if the selector were set.
        """
        paths: list[str] = []

        def handler(req):
            paths.append(req.url.path)
            return httpx.Response(200, json=[])

        _seed_and_register("gitlab", handler)
        from gitlab_mcp import server

        # owned=False is falsy → /runners/all (the catch-all admin route).
        server._dispatch(
            "RunnersAll", "gitlab_read", {"owned": False},
        )
        # owned=True is truthy → /runners (the "owned by current user" route).
        server._dispatch(
            "RunnersAll", "gitlab_read", {"owned": True},
        )
        # No selectors at all → fallback /runners/all.
        server._dispatch("RunnersAll", "gitlab_read", {})

        assert paths[0] == "/api/v4/runners/all"
        assert paths[1] == "/api/v4/runners"
        assert paths[2] == "/api/v4/runners/all"
