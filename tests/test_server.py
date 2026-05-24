"""Unit tests for server.py: _coerce_call validation and _register_tools filter."""

import inspect
from typing import Literal

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


# ── _coerce_call ──────────────────────────────────────────────────────────


class TestCoerceCall:
    def test_rejects_unknown_param(self):
        from gitlab_mcp.server import _coerce_call

        def fn(a: int):
            """Test."""
            return a

        with pytest.raises(ValueError, match="Extra inputs are not permitted"):
            _coerce_call(fn, {"a": 1, "z": 99})

    def test_rejects_invalid_literal_value(self):
        from gitlab_mcp.server import _coerce_call

        def fn(mode: Literal["active", "archived", "all"]):
            """Test."""
            return mode

        with pytest.raises(ValueError, match="Input should be 'active', 'archived' or 'all'"):
            _coerce_call(fn, {"mode": "pending"})

    def test_accepts_valid_literal_value(self):
        from gitlab_mcp.server import _coerce_call

        def fn(mode: Literal["active", "archived"]):
            """Test."""
            return mode

        assert _coerce_call(fn, {"mode": "active"}) == "active"
        assert _coerce_call(fn, {"mode": "archived"}) == "archived"

    def test_optional_literal_accepts_none(self):
        from gitlab_mcp.server import _coerce_call

        def fn(mode: Literal["a", "b"] | None = None):
            """Test."""
            return mode

        assert _coerce_call(fn, {"mode": None}) is None
        assert _coerce_call(fn, {}) is None
        assert _coerce_call(fn, {"mode": "a"}) == "a"

    def test_optional_literal_rejects_invalid(self):
        from gitlab_mcp.server import _coerce_call

        def fn(mode: Literal["a", "b"] | None = None):
            """Test."""
            return mode

        with pytest.raises(ValueError, match="Input should be 'a' or 'b'"):
            _coerce_call(fn, {"mode": "c"})

    def test_bool_coercion_from_string(self):
        from gitlab_mcp.server import _coerce_call

        def fn(flag: bool = False):
            """Test."""
            return flag

        assert _coerce_call(fn, {"flag": "true"}) is True
        assert _coerce_call(fn, {"flag": "yes"}) is True
        assert _coerce_call(fn, {"flag": "1"}) is True
        assert _coerce_call(fn, {"flag": "false"}) is False
        assert _coerce_call(fn, {"flag": "no"}) is False
        # Case-insensitive
        assert _coerce_call(fn, {"flag": "True"}) is True
        assert _coerce_call(fn, {"flag": "FALSE"}) is False

    def test_bool_pass_through(self):
        from gitlab_mcp.server import _coerce_call

        def fn(flag: bool = False):
            """Test."""
            return flag

        assert _coerce_call(fn, {"flag": True}) is True
        assert _coerce_call(fn, {"flag": False}) is False

    def test_empty_params_uses_defaults(self):
        from gitlab_mcp.server import _coerce_call

        def fn(a: int = 5, b: str = "hi"):
            """Test."""
            return (a, b)

        assert _coerce_call(fn, {}) == (5, "hi")

    def test_field_level_validation_error_includes_path(self):
        """Pydantic surfaces the offending field name in the error."""
        from gitlab_mcp.server import _coerce_call

        def fn(labels: list[str]):
            """Test."""
            return labels

        with pytest.raises(ValueError, match="labels.1"):
            _coerce_call(fn, {"labels": ["bug", 42]})

    def test_validation_error_is_multi_line_with_schema_hint(self):
        """`_format_validation_error` turns the ValidationError into a readable
        multi-line message and points at `operation='schema'` so the agent
        knows where to find the full parameter spec."""
        from gitlab_mcp.server import _coerce_call

        def my_op(name: str, count: int):
            """Test."""
            return (name, count)

        with pytest.raises(ValueError) as exc:
            _coerce_call(my_op, {"count": "not-an-int"})
        msg = str(exc.value)
        # Header names the op (derived from fn.__name__ → PascalCase).
        assert msg.startswith("Invalid params for MyOp:")
        # Field-level lines start with `  - <loc>:` so multiple errors are
        # each surfaced separately.
        assert "  - name:" in msg
        assert "  - count:" in msg
        # Closes with a schema hint that uses the same PascalCase op name.
        assert "operation='schema'" in msg
        assert "'op': 'MyOp'" in msg

    def test_unset_default_preserved_when_caller_omits(self):
        """Sentinel-default params: omitted by caller → fn sees its own default."""
        from gitlab_mcp.registry import _UNSET
        from gitlab_mcp.server import _coerce_call

        def fn(name: str = _UNSET, description: str = _UNSET):
            """Test."""
            return (name, description)

        # Neither passed → both _UNSET preserved
        assert _coerce_call(fn, {}) == (_UNSET, _UNSET)
        # One passed → other still _UNSET
        assert _coerce_call(fn, {"name": "x"}) == ("x", _UNSET)

    def test_var_keyword_accepts_unknown(self):
        from gitlab_mcp.server import _coerce_call

        def fn(project_id: str, **options):
            """Test."""
            return {"project_id": project_id, "options": options}

        # Extra fields should be passed through to **options, not rejected.
        result = _coerce_call(fn, {"project_id": "myproj", "branch": "feat", "ref": "main"})
        assert result["project_id"] == "myproj"
        assert result["options"] == {"branch": "feat", "ref": "main"}

    def test_var_keyword_still_validates_known_literal(self):
        from gitlab_mcp.server import _coerce_call

        def fn(mode: Literal["a", "b"], **options):
            """Test."""
            return (mode, options)

        # Named param with Literal still validated
        with pytest.raises(ValueError, match="Input should be 'a' or 'b'"):
            _coerce_call(fn, {"mode": "c", "extra": 1})

        # Valid call still works
        assert _coerce_call(fn, {"mode": "a", "extra": 1}) == ("a", {"extra": 1})

    def test_rejects_dict_nested_under_var_keyword_name(self):
        """LLMs sometimes pass body fields wrapped under 'options': reject loudly."""
        from gitlab_mcp.server import _coerce_call

        def fn(project_id: str, **options):
            """Test."""
            return (project_id, options)

        with pytest.raises(ValueError, match="Do not nest body fields under 'options'"):
            _coerce_call(fn, {
                "project_id": "p",
                "options": {"description": "text"},
            })

    def test_scalar_value_for_var_keyword_name_passes_through(self):
        """Only dict-valued 'options' is the footgun; scalar 'options' is fine."""
        from gitlab_mcp.server import _coerce_call

        def fn(project_id: str, **options):
            """Test."""
            return (project_id, options)

        # e.g. a literal field named 'options' that's a string — not our concern
        result = _coerce_call(fn, {"project_id": "p", "options": "some-string"})
        assert result == ("p", {"options": "some-string"})


# ── _make_tool: mutable-default regression ────────────────────────────────


class TestMakeTool:
    def test_params_default_is_none(self):
        from gitlab_mcp.server import _make_tool

        tool_fn = _make_tool("test_group", "doc")
        assert inspect.signature(tool_fn).parameters["params"].default is None

    def test_no_dict_leak_across_calls(self, monkeypatch):
        """Each call without a `params` arg must receive a fresh dict.

        Pre-fix bug: `params: dict = {}` shared one dict across all calls,
        so mutations from earlier calls leaked into later ones.

        Meta-tools are async (so progress / log can be awaited from inside
        ops), so we drive them with `asyncio.run`.
        """
        import asyncio

        from gitlab_mcp import server

        captured: list[int] = []

        def fake_dispatch(operation, group_name, params, ctx=None):
            captured.append(len(params))
            params["leaked"] = "mutation"
            return None

        monkeypatch.setattr(server, "_dispatch", fake_dispatch)
        tool_fn = server._make_tool("test_group", "doc")
        asyncio.run(tool_fn(operation="MyOp"))
        asyncio.run(tool_fn(operation="MyOp"))
        assert captured == [0, 0]


# ── _register_tools filter ────────────────────────────────────────────────


def _seed_client(backend: str) -> GitLabClient:
    """Create a GitLabClient with instance pre-populated for the given backend."""
    transport = httpx.MockTransport(lambda req: httpx.Response(404))
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
    return client


class TestRegisterToolsFilter:
    def test_heptapod_only_excluded_on_gitlab(self, monkeypatch):
        import gitlab_mcp.client as client_mod
        client_mod._client = _seed_client("gitlab")

        from gitlab_mcp import server, tools
        from gitlab_mcp.registry import _op

        @_op(tools.gitlab_read)
        def hg_probe_synthetic():
            """Synthetic hg-only tool."""
            return "hg"

        hg_probe_synthetic._heptapod_only = True
        monkeypatch.setattr(tools, "hg_probe_synthetic", hg_probe_synthetic, raising=False)

        server._register_tools()
        gitlab_read_ops = server._group_ops.get("gitlab_read", {})
        assert "HgProbeSynthetic" not in gitlab_read_ops

    def test_heptapod_only_included_on_heptapod(self, monkeypatch):
        import gitlab_mcp.client as client_mod
        client_mod._client = _seed_client("heptapod")

        from gitlab_mcp import server, tools
        from gitlab_mcp.registry import _op

        @_op(tools.gitlab_read)
        def hg_probe_synthetic():
            """Synthetic hg-only tool."""
            return "hg"

        hg_probe_synthetic._heptapod_only = True
        monkeypatch.setattr(tools, "hg_probe_synthetic", hg_probe_synthetic, raising=False)

        server._register_tools()
        gitlab_read_ops = server._group_ops.get("gitlab_read", {})
        assert "HgProbeSynthetic" in gitlab_read_ops

    def test_non_heptapod_tools_always_registered(self, monkeypatch):
        import gitlab_mcp.client as client_mod
        client_mod._client = _seed_client("gitlab")

        from gitlab_mcp import server, tools
        from gitlab_mcp.registry import _op

        @_op(tools.gitlab_read)
        def synthetic_read():
            """Synthetic regular tool."""
            return "ok"

        monkeypatch.setattr(tools, "synthetic_read", synthetic_read, raising=False)

        server._register_tools()
        assert "SyntheticRead" in server._group_ops.get("gitlab_read", {})


# ── gitlab_version ROOT tool ──────────────────────────────────────────────


class TestBuildHelp:
    """Progressive disclosure help: index → category → search."""

    def setup_method(self):
        # Seed a synthetic group_ops dict so we don't depend on actual generated ops.
        from gitlab_mcp import server

        def fake_projects_show():
            """Projects.show (GET projects/${projectId})."""
        def fake_projects_create():
            """Projects.create (POST projects)."""
        def fake_branches_all():
            """Branches.all (GET projects/${projectId}/repository/branches)."""
        def fake_hg_get_config():
            """Read the high-level Mercurial project settings (Heptapod only)."""

        server._group_ops.clear()
        server._group_ops["fake_read"] = {
            "ProjectsShow": fake_projects_show,
            "ProjectsCreate": fake_projects_create,
            "BranchesAll": fake_branches_all,
            "HgGetConfig": fake_hg_get_config,
        }

    def teardown_method(self):
        from gitlab_mcp import server
        server._group_ops.clear()

    def test_default_returns_compact_index(self):
        from gitlab_mcp.server import _build_help

        result = _build_help("fake_read")
        assert "4 operations in fake_read" in result
        assert "Projects: 2 ops" in result
        assert "Branches: 1 ops" in result
        assert "Hg: 1 ops" in result
        # Compact form: no signatures
        assert "(project_id)" not in result
        assert "GET projects" not in result

    def test_filter_by_category(self):
        from gitlab_mcp.server import _build_help

        result = _build_help("fake_read", category="Projects")
        assert "ProjectsShow" in result
        assert "ProjectsCreate" in result
        assert "BranchesAll" not in result
        assert "HgGetConfig" not in result

    def test_filter_by_unknown_category(self):
        from gitlab_mcp.server import _build_help

        result = _build_help("fake_read", category="Nonexistent")
        assert "No category 'Nonexistent'" in result

    def test_search_by_name(self):
        from gitlab_mcp.server import _build_help

        result = _build_help("fake_read", search="hg")
        assert "HgGetConfig" in result
        assert "ProjectsShow" not in result

    def test_search_no_match(self):
        from gitlab_mcp.server import _build_help

        result = _build_help("fake_read", search="qwerty")
        assert "No ops" in result
        assert "qwerty" in result

    def test_help_includes_docstring_body(self):
        """The detail view shows the head on the signature line AND the
        docstring body indented underneath — agents shouldn't need a
        separate `schema` call for body-level guidance like format hints."""
        from gitlab_mcp import server

        def fake_with_body():
            """Head line summarising what this op does.

            Longer body with context: pass labels as IDs from list_repo_labels,
            not strings. The body is what carries this kind of caller-facing
            constraint, so it needs to land in help.
            """

        server._group_ops["fake_read"] = {"FakeWithBody": fake_with_body}
        try:
            result = server._build_help("fake_read", search="FakeWithBody")
            # Head present on the signature line.
            assert "FakeWithBody() — Head line summarising" in result
            # Body lines appear, indented (four spaces under the signature).
            assert "    Longer body with context" in result
            assert "    not strings" in result
        finally:
            server._group_ops.clear()


# ── _dispatch wrong-group error path ───────────────────────────────────────


class TestDispatchWrongGroup:
    """Per v2 spec: surface errors as exceptions, not `{"error": ...}` dicts."""

    def test_op_in_wrong_group_raises_value_error(self, monkeypatch):
        from gitlab_mcp import server

        server._group_ops.clear()
        server._all_grouped.clear()
        server._group_ops["fake_read"] = {"Existing": lambda: None}
        server._group_ops["fake_write"] = {"OnlyHere": lambda: None}
        server._all_grouped["Existing"] = "fake_read"
        server._all_grouped["OnlyHere"] = "fake_write"
        try:
            with pytest.raises(ValueError, match="belongs to 'fake_write'"):
                server._dispatch("OnlyHere", "fake_read", {})
        finally:
            server._group_ops.clear()
            server._all_grouped.clear()

    def test_unknown_op_raises_value_error(self):
        from gitlab_mcp import server

        server._group_ops.clear()
        server._group_ops["fake_read"] = {"Existing": lambda: None}
        try:
            with pytest.raises(ValueError, match="Unknown operation 'Mystery'"):
                server._dispatch("Mystery", "fake_read", {})
        finally:
            server._group_ops.clear()

    def test_category_from_snake_heuristic(self):
        from gitlab_mcp.server import _category_from_snake

        assert _category_from_snake("projects_show") == "Projects"
        assert _category_from_snake("merge_requests_create") == "MergeRequests"
        assert _category_from_snake("hg_get_config") == "Hg"
        assert _category_from_snake("user_ssh_keys_all") == "UserSshKeys"
        assert _category_from_snake("notification_settings_show") == "NotificationSettings"

    def test_category_from_docstring(self):
        from gitlab_mcp.server import _category_for_fn

        def fn():
            """MergeRequests.create (POST projects/${projectId}/merge_requests)."""

        assert _category_for_fn(fn) == "MergeRequests"


class TestGitlabVersion:
    def test_shape_with_instance(self):
        import gitlab_mcp.client as client_mod
        client_mod._client = _seed_client("heptapod")

        from gitlab_mcp.tools import gitlab_version

        result = gitlab_version()
        assert "mcp" in result
        assert "service" in result
        svc = result["service"]
        assert svc["backend"] == "heptapod"
        assert svc["version"] == "18.6.0"
        assert svc["enterprise"] is False
        assert svc["vcs_types"] == ["git", "hg", "hg_git"]
        assert svc["url"] == "https://gitlab.example.com"

    def test_shape_without_instance(self):
        # If called before main() sets up instance, returns empty service dict.
        import gitlab_mcp.client as client_mod
        transport = httpx.MockTransport(lambda req: httpx.Response(404))
        client = GitLabClient(transport=transport)
        # Don't populate client.instance
        client_mod._client = client

        from gitlab_mcp.tools import gitlab_version

        result = gitlab_version()
        assert result["service"] == {}
        assert "mcp" in result  # version string still populated from package metadata

    def test_is_root_tool(self):
        from gitlab_mcp.registry import ROOT
        from gitlab_mcp.tools import gitlab_version

        assert gitlab_version._mcp_group is ROOT
