"""Tests for PARAM_ANNOTATIONS wrapping, type rendering, and help output."""

from typing import Annotated, Literal

import httpx
import pytest
from pydantic import Field

import gitlab_mcp.client as client_mod
from gitlab_mcp import _generated
from gitlab_mcp.client import GitLabClient
from gitlab_mcp.config import _reset_settings
from gitlab_mcp.registry import _UNSET

# ── _render_type ──────────────────────────────────────────────────────────


class TestRenderType:
    def test_simple_builtins(self):
        from gitlab_mcp.server import _render_type

        assert _render_type(str) == "str"
        assert _render_type(int) == "int"
        assert _render_type(bool) == "bool"

    def test_none_type(self):
        from gitlab_mcp.server import _render_type

        assert _render_type(None) == "None"
        assert _render_type(type(None)) == "None"

    def test_optional(self):
        from gitlab_mcp.server import _render_type

        assert _render_type(str | None) == "str | None"

    def test_union(self):
        from gitlab_mcp.server import _render_type

        assert _render_type(str | int) == "str | int"

    def test_literal_strings(self):
        from gitlab_mcp.server import _render_type

        assert _render_type(Literal["a", "b", "c"]) == "a|b|c"

    def test_literal_mixed(self):
        from gitlab_mcp.server import _render_type

        # Non-string literals keep repr quoting.
        assert _render_type(Literal[1, 2, 3]) == "1|2|3"

    def test_list(self):
        from gitlab_mcp.server import _render_type

        assert _render_type(list[str]) == "list[str]"
        assert _render_type(list[int]) == "list[int]"

    def test_dict(self):
        from gitlab_mcp.server import _render_type

        assert _render_type(dict) == "dict"

    def test_strips_annotated(self):
        from gitlab_mcp.server import _render_type

        wrapped = Annotated[str, Field(description="hi")]
        assert _render_type(wrapped) == "str"


# ── _wrap_param_annotations idempotency ────────────────────────────────────


class TestWrapParamAnnotations:
    def test_wrap_attaches_description_to_hint(self):
        import typing

        from gitlab_mcp import tools

        def fn(name: str = _UNSET):
            """Test."""
            return name

        # Inject a synthetic op into the tools module so _wrap can find it.
        tools.synthetic_wrap_op = fn  # type: ignore[attr-defined]
        try:
            tools.PARAM_ANNOTATIONS["synthetic_wrap_op"] = {
                "name": "A test description."
            }
            tools._wrap_param_annotations()

            hints = typing.get_type_hints(fn, include_extras=True)
            assert typing.get_origin(hints["name"]) is Annotated
            meta = typing.get_args(hints["name"])[1]
            assert meta.description == "A test description."
        finally:
            tools.PARAM_ANNOTATIONS.pop("synthetic_wrap_op", None)
            del tools.synthetic_wrap_op  # type: ignore[attr-defined]

    def test_wrap_is_idempotent(self):
        """Running twice → single Annotated layer, description preserved."""
        import typing

        from gitlab_mcp import tools

        def fn(name: str = _UNSET):
            """Test."""
            return name

        tools.synthetic_wrap_op2 = fn  # type: ignore[attr-defined]
        try:
            tools.PARAM_ANNOTATIONS["synthetic_wrap_op2"] = {
                "name": "First."
            }
            tools._wrap_param_annotations()
            tools.PARAM_ANNOTATIONS["synthetic_wrap_op2"] = {
                "name": "Second."
            }
            tools._wrap_param_annotations()

            hints = typing.get_type_hints(fn, include_extras=True)
            assert typing.get_origin(hints["name"]) is Annotated
            args = typing.get_args(hints["name"])
            # Underlying type is still bare str (not Annotated[Annotated[str,…],…]).
            assert args[0] is str
            assert args[1].description == "Second."
        finally:
            tools.PARAM_ANNOTATIONS.pop("synthetic_wrap_op2", None)
            del tools.synthetic_wrap_op2  # type: ignore[attr-defined]


# ── Pydantic model surfaces description ────────────────────────────────────


class TestBuildParamsModelDescription:
    def test_description_in_json_schema(self):
        from gitlab_mcp.server import _build_params_model

        def fn(name: Annotated[str, Field(description="A name.")]):
            """Test."""
            return name

        model = _build_params_model(fn)
        schema = model.model_json_schema()
        assert schema["properties"]["name"]["description"] == "A name."


# ── _format_help_full renders typed signatures + bullets ──────────────────


class TestHelpFormatting:
    def test_required_param_no_marker(self):
        from gitlab_mcp.server import _format_help_full

        def fn(project_id: str):
            """Op.method (POST projects)."""

        result = _format_help_full({"Op": fn}, "g", "")
        assert "Op(project_id: str)" in result

    def test_optional_uses_question_mark(self):
        """_UNSET default → `name?: T` (NOT `name: T = _UNSET`)."""
        from gitlab_mcp.server import _format_help_full

        def fn(name: str = _UNSET):
            """Op.method."""

        result = _format_help_full({"Op": fn}, "g", "")
        assert "name?: str" in result
        assert "_UNSET" not in result  # critical: sentinel never in help

    def test_nullable_renders_union(self):
        from gitlab_mcp.server import _format_help_full

        def fn(assignee_id: int | None):
            """Op."""

        result = _format_help_full({"Op": fn}, "g", "")
        assert "assignee_id: int | None" in result

    def test_optional_and_nullable(self):
        from gitlab_mcp.server import _format_help_full

        def fn(assignee_id: int | None = _UNSET):
            """Op."""

        result = _format_help_full({"Op": fn}, "g", "")
        assert "assignee_id?: int | None" in result

    def test_var_keyword_rendered(self):
        from gitlab_mcp.server import _format_help_full

        def fn(project_id: str, **options):
            """Op."""

        result = _format_help_full({"Op": fn}, "g", "")
        assert "**options" in result

    def test_annotated_description_renders_as_bullet(self):
        from gitlab_mcp.server import _format_help_full

        def fn(
            labels: Annotated[
                list[str], Field(description="Comma-separated label names.")
            ] = _UNSET,
        ):
            """Op."""

        result = _format_help_full({"Op": fn}, "g", "")
        assert "labels?: list[str]" in result
        assert "      labels: Comma-separated label names." in result

    def test_literal_renders_pipe_separated(self):
        from gitlab_mcp.server import _format_help_full

        def fn(mode: Literal["active", "archived"] = _UNSET):
            """Op."""

        result = _format_help_full({"Op": fn}, "g", "")
        assert "mode?: active|archived" in result


def _schema_contains_type(schema: object, expected: str) -> bool:
    if isinstance(schema, dict):
        if schema.get("type") == expected:
            return True
        return any(_schema_contains_type(value, expected) for value in schema.values())
    if isinstance(schema, list):
        return any(_schema_contains_type(value, expected) for value in schema)
    return False

def _schema_enum_values(schema: object) -> set[int]:
    if isinstance(schema, dict):
        values = schema.get("enum")
        if isinstance(values, list) and all(isinstance(value, int) for value in values):
            return set(values)
        return {
            value
            for child in schema.values()
            for value in _schema_enum_values(child)
        }
    if isinstance(schema, list):
        return {
            value
            for child in schema
            for value in _schema_enum_values(child)
        }
    return set()


class TestGeneratedOpenApiSurface:
    def test_arrays_and_booleans_remain_visible_in_schema_and_help(self):
        from gitlab_mcp import _generated
        from gitlab_mcp.server import _build_params_model, _format_help_full

        feature_flags = _build_params_model(_generated.feature_flags_create)
        pages_domains = _build_params_model(_generated.pages_domains_create)
        markdown = _build_params_model(_generated.markdown_render)

        assert _schema_contains_type(
            feature_flags.model_json_schema()["properties"]["strategies"], "array"
        )
        assert _schema_contains_type(
            pages_domains.model_json_schema()["properties"]["auto_ssl_enabled"], "boolean"
        )
        assert _schema_contains_type(
            markdown.model_json_schema()["properties"]["gfm"], "boolean"
        )
        feature_flags.model_validate(
            {
                "project_id": 1,
                "name": "flag",
                "version": "new_version_flag",
                "strategies": [{"name": "default", "scopes": []}],
            }
        )
        pages_domains.model_validate(
            {"project_id": 1, "domain": "example.test", "auto_ssl_enabled": True}
        )
        markdown.model_validate({"text": "body", "gfm": True})

        help_text = _format_help_full(
            {
                "FeatureFlagsCreate": _generated.feature_flags_create,
                "PagesDomainsCreate": _generated.pages_domains_create,
                "MarkdownRender": _generated.markdown_render,
            },
            "gitlab_write",
            "",
        )
        assert "strategies?: list[dict]" in help_text
        assert "auto_ssl_enabled?: bool" in help_text
        assert "gfm?: bool" in help_text

    def test_gitbeaker_any_cannot_erase_openapi_concrete_types(self):
        from gitlab_mcp import _generated
        from gitlab_mcp.server import _build_params_model

        repositories = _build_params_model(
            _generated.container_registry_all_repositories
        )
        protected_branches = _build_params_model(_generated.protected_branches_edit)

        assert _schema_contains_type(
            repositories.model_json_schema()["properties"]["page"], "integer"
        )
        assert _schema_contains_type(
            protected_branches.model_json_schema()["properties"]["allowed_to_merge"],
            "array",
        )
        assert _schema_contains_type(
            protected_branches.model_json_schema()["properties"]["allowed_to_merge"],
            "object",
        )
        repositories.model_validate({"page": 2})
        protected_branches.model_validate(
            {
                "project_id": 1,
                "branch_name": "main",
                "allowed_to_merge": [{"access_level": 40}],
            }
        )

    def test_one_of_sentinels_and_null_unassignment_remain_accepted(self):
        from gitlab_mcp import _generated
        from gitlab_mcp.server import _build_params_model

        issues_all = _build_params_model(_generated.issues_all)
        merge_requests_all = _build_params_model(_generated.merge_requests_all)
        issues_edit = _build_params_model(_generated.issues_edit)

        issues_all.model_validate({"assignee_id": "None"})
        merge_requests_all.model_validate({"approved_by_ids": "None"})
        merge_requests_all.model_validate({"approved_by_ids": [1, 2]})
        unassignment = issues_edit.model_validate(
            {"project_id": 1, "issue_iid": 2, "assignee_id": None}
        )
        assert unassignment.model_dump()["assignee_id"] is None

    def test_upload_dictionary_remains_an_object(self):
        from gitlab_mcp import _generated
        from gitlab_mcp.server import _build_params_model

        cases = (
            (
                _generated.group_import_exports_import,
                "file",
                {
                    "file": {
                        "filename": "group-export.tar.gz",
                        "content": "data",
                    },
                    "path": "group",
                    "name": "group",
                },
            ),
            (
                _generated.group_wikis_upload_attachment,
                "file",
                {
                    "group_id": 1,
                    "file": {"filename": "attachment.txt", "content": "data"},
                },
            ),
            (
                _generated.project_import_exports_import,
                "file",
                {
                    "file": {
                        "filename": "project-export.tar.gz",
                        "content": "data",
                    },
                    "path": "project",
                },
            ),
            (
                _generated.projects_upload_avatar,
                "avatar",
                {
                    "project_id": 1,
                    "avatar": {"filename": "avatar.png", "content": "data"},
                },
            ),
            (
                _generated.projects_upload_for_reference,
                "file",
                {
                    "project_id": 1,
                    "file": {"filename": "reference.txt", "content": "data"},
                },
            ),
            (
                _generated.project_wikis_upload_attachment,
                "file",
                {
                    "project_id": 1,
                    "file": {"filename": "attachment.txt", "content": "data"},
                },
            ),
            (
                _generated.secure_files_create,
                "file",
                {
                    "project_id": 1,
                    "name": "secret",
                    "file": {"filename": "secret.txt", "content": "data"},
                },
            ),
        )
        for operation, parameter, params in cases:
            model = _build_params_model(operation)
            assert _schema_contains_type(
                model.model_json_schema()["properties"][parameter], "object"
            )
            model.model_validate(params)

    def test_renamed_positionals_are_required_under_only_wire_names(self):
        from gitlab_mcp import _generated
        from gitlab_mcp.server import _build_params_model

        cases = (
            (
                _generated.import_import_github_repository,
                "repo_id",
                "repository_id",
            ),
            (
                _generated.import_import_bitbucket_server_repository,
                "bitbucket_server_repo",
                "bitbucket_server_repository",
            ),
            (_generated.suggestions_edit_batch, "ids", "suggestion_ids"),
        )
        for operation, wire_name, positional_name in cases:
            schema = _build_params_model(operation).model_json_schema()
            assert wire_name in schema["required"]
            assert positional_name not in schema["properties"]

    @pytest.mark.parametrize(
        (
            "operation_name",
            "path",
            "field",
            "base_params",
            "valid_values",
            "invalid_values",
            "list_valued",
        ),
        (
            pytest.param(
                "group_members_add",
                "/api/v4/groups/{id}/members",
                "access_level",
                {"group_id": 1},
                (0, 5, 10, 15, 20, 30, 40, 50),
                (60,),
                False,
                id="GroupMembers.add POST /groups/{id}/members access_level",
            ),
            pytest.param(
                "group_members_edit",
                "/api/v4/groups/{id}/members/{user_id}",
                "access_level",
                {"group_id": 1, "user_id": 2},
                (0, 5, 10, 15, 20, 30, 40, 50),
                (60,),
                False,
                id="GroupMembers.edit PUT /groups/{id}/members/{user_id} access_level",
            ),
            pytest.param(
                "project_members_add",
                "/api/v4/projects/{id}/members",
                "access_level",
                {"project_id": 1},
                (0, 5, 10, 15, 20, 30, 40, 50),
                (60,),
                False,
                id="ProjectMembers.add POST /projects/{id}/members access_level",
            ),
            pytest.param(
                "project_members_edit",
                "/api/v4/projects/{id}/members/{user_id}",
                "access_level",
                {"project_id": 1, "user_id": 2},
                (0, 5, 10, 15, 20, 30, 40, 50),
                (60,),
                False,
                id="ProjectMembers.edit PUT /projects/{id}/members/{user_id} access_level",
            ),
            pytest.param(
                "group_access_requests_approve",
                "/api/v4/groups/{id}/access_requests/{user_id}/approve",
                "access_level",
                {"group_id": 1, "user_id": 2},
                (0, 5, 10, 15, 20, 30, 40, 50),
                (60,),
                False,
                id="GroupAccessRequests.approve PUT /groups/{id}/access_requests/{user_id}/approve access_level",
            ),
            pytest.param(
                "project_access_requests_approve",
                "/api/v4/projects/{id}/access_requests/{user_id}/approve",
                "access_level",
                {"project_id": 1, "user_id": 2},
                (0, 5, 10, 15, 20, 30, 40, 50),
                (60,),
                False,
                id="ProjectAccessRequests.approve PUT /projects/{id}/access_requests/{user_id}/approve access_level",
            ),
            pytest.param(
                "group_member_roles_add",
                "/api/v4/groups/{id}/members",
                "base_access_level",
                {"group_id": 1, "access_level": 10},
                (10, 15, 20, 30, 40, 50),
                (0, 5, 60),
                False,
                id="GroupMemberRoles.add POST /groups/{id}/members base_access_level",
            ),
            pytest.param(
                "broadcast_messages_create",
                "/api/v4/broadcast_messages",
                "target_access_levels",
                {"message": "notice"},
                (10, 15, 20, 30, 40, 50),
                (0, 5, 60),
                True,
                id="BroadcastMessages.create POST /broadcast_messages target_access_levels",
            ),
            pytest.param(
                "broadcast_messages_edit",
                "/api/v4/broadcast_messages/{id}",
                "target_access_levels",
                {"broadcast_message_id": 1},
                (10, 15, 20, 30, 40, 50),
                (0, 5, 60),
                True,
                id="BroadcastMessages.edit PUT /broadcast_messages/{id} target_access_levels",
            ),
            pytest.param(
                "projects_all_invited_groups",
                "/api/v4/projects/{id}/invited_groups",
                "shared_min_access_level",
                {"project_id": 1},
                (0, 5, 10, 15, 20, 30, 40, 50),
                (60,),
                False,
                id="Projects.allInvitedGroups GET /projects/{id}/invited_groups shared_min_access_level",
            ),
            pytest.param(
                "users_all_contributed_projects",
                "/api/v4/users/{user_id}/contributed_projects",
                "min_access_level",
                {"user_id": 1},
                (0, 5, 10, 15, 20, 30, 40, 50),
                (60,),
                False,
                id="Users.allContributedProjects GET /users/{user_id}/contributed_projects min_access_level",
            ),
            pytest.param(
                "group_access_tokens_create",
                "/api/v4/groups/{id}/access_tokens",
                "access_level",
                {
                    "group_id": 1,
                    "name": "token",
                    "scopes": ["api"],
                    "expires_at": "2030-01-01",
                },
                (10, 15, 20, 30, 40, 50),
                (0, 5, 60),
                False,
                id="GroupAccessTokens.create POST /groups/{id}/access_tokens access_level",
            ),
            pytest.param(
                "project_access_tokens_create",
                "/api/v4/projects/{id}/access_tokens",
                "access_level",
                {
                    "project_id": 1,
                    "name": "token",
                    "scopes": ["api"],
                    "expires_at": "2030-01-01",
                },
                (10, 15, 20, 30, 40, 50),
                (0, 5, 60),
                False,
                id="ProjectAccessTokens.create POST /projects/{id}/access_tokens access_level",
            ),
            pytest.param(
                "protected_branches_create",
                "/api/v4/projects/{id}/protected_branches",
                "push_access_level",
                {"project_id": 1, "name": "main"},
                (0, 30, 40, 60),
                (5, 15, 20, 50),
                False,
                id="ProtectedBranches.create POST /projects/{id}/protected_branches push_access_level",
            ),
            pytest.param(
                "protected_branches_create",
                "/api/v4/projects/{id}/protected_branches",
                "merge_access_level",
                {"project_id": 1, "name": "main"},
                (0, 30, 40, 60),
                (5, 15, 20, 50),
                False,
                id="ProtectedBranches.create POST /projects/{id}/protected_branches merge_access_level",
            ),
            pytest.param(
                "protected_branches_create",
                "/api/v4/projects/{id}/protected_branches",
                "unprotect_access_level",
                {"project_id": 1, "name": "main"},
                (30, 40, 60),
                (0, 5, 15, 20, 50),
                False,
                id="ProtectedBranches.create POST /projects/{id}/protected_branches unprotect_access_level",
            ),
            pytest.param(
                "protected_branches_edit",
                "/api/v4/projects/{id}/protected_branches/{name}",
                "unprotect_access_level",
                {"project_id": 1, "branch_name": "main"},
                (30, 40, 60),
                (0, 5, 15, 20, 50),
                False,
                id="ProtectedBranches.edit PATCH /projects/{id}/protected_branches/{name} unprotect_access_level",
            ),
            pytest.param(
                "group_saml_links_create",
                "/api/v4/groups/{id}/saml_group_links",
                "access_level",
                {"group_id": 1, "saml_group_name": "engineering"},
                (5, 10, 15, 20, 30, 40, 50),
                (0, 60),
                False,
                id="GroupSAMLLinks.create POST /groups/{id}/saml_group_links access_level",
            ),
            pytest.param(
                "group_invitations_add",
                "/api/v4/groups/{id}/invitations",
                "access_level",
                {"group_id": 1, "email": ["member@example.test"]},
                (5, 10, 15, 20, 30, 40, 50),
                (0, 60),
                False,
                id="GroupInvitations.add POST /groups/{id}/invitations access_level",
            ),
            pytest.param(
                "group_invitations_edit",
                "/api/v4/groups/{id}/invitations/{email}",
                "access_level",
                {"group_id": 1, "email": "member@example.test"},
                (10, 15, 20, 30, 40, 50),
                (0, 5, 60),
                False,
                id="GroupInvitations.edit PUT /groups/{id}/invitations/{email} access_level",
            ),
            pytest.param(
                "project_invitations_add",
                "/api/v4/projects/{id}/invitations",
                "access_level",
                {"project_id": 1, "email": ["member@example.test"]},
                (5, 10, 15, 20, 30, 40, 50),
                (0, 60),
                False,
                id="ProjectInvitations.add POST /projects/{id}/invitations access_level",
            ),
            pytest.param(
                "project_invitations_edit",
                "/api/v4/projects/{id}/invitations/{email}",
                "access_level",
                {"project_id": 1, "email": "member@example.test"},
                (10, 15, 20, 30, 40, 50),
                (0, 5, 60),
                False,
                id="ProjectInvitations.edit PUT /projects/{id}/invitations/{email} access_level",
            ),
        ),
    )
    def test_access_level_domains_match_operation_contract(
        self,
        operation_name,
        path,
        field,
        base_params,
        valid_values,
        invalid_values,
        list_valued,
    ):
        from gitlab_mcp import _generated
        from gitlab_mcp.server import _build_params_model

        operation = getattr(_generated, operation_name)
        model = _build_params_model(operation)
        schema = model.model_json_schema()["properties"][field]
        expected_values = set(valid_values)
        actual_values = _schema_enum_values(schema)
        assert actual_values == expected_values, (
            f"{operation.__name__} {path} {field}: "
            f"schema advertises {sorted(actual_values)}, expected {sorted(expected_values)}"
        )

        for value in valid_values:
            expected = [value] if list_valued else value
            validated = model.model_validate({**base_params, field: expected})
            assert validated.model_dump()[field] == expected, (
                f"{operation.__name__} {path} {field}: "
                f"rejected documented value {value}"
            )

        for value in invalid_values:
            candidate = [value] if list_valued else value
            try:
                model.model_validate({**base_params, field: candidate})
            except ValueError:
                continue
            pytest.fail(
                f"{operation.__name__} {path} {field}: "
                f"accepted excluded value {value}"
            )

    @pytest.mark.parametrize(
        ("operation_name", "params", "method", "path"),
        (
            pytest.param(
                "group_access_requests_all",
                {"group_id": 12},
                "GET",
                "/api/v4/groups/12/access_requests",
                id="GroupAccessRequests.all GET /groups/{id}/access_requests",
            ),
            pytest.param(
                "group_access_requests_request",
                {"group_id": 12},
                "POST",
                "/api/v4/groups/12/access_requests",
                id="GroupAccessRequests.request POST /groups/{id}/access_requests",
            ),
            pytest.param(
                "group_access_requests_approve",
                {"group_id": 12, "user_id": 34},
                "PUT",
                "/api/v4/groups/12/access_requests/34/approve",
                id="GroupAccessRequests.approve PUT /groups/{id}/access_requests/{user_id}/approve",
            ),
            pytest.param(
                "group_access_requests_deny",
                {"group_id": 12, "user_id": 34},
                "DELETE",
                "/api/v4/groups/12/access_requests/34",
                id="GroupAccessRequests.deny DELETE /groups/{id}/access_requests/{user_id}",
            ),
        ),
    )
    def test_group_access_request_operations_use_group_paths(
        self, monkeypatch, operation_name, params, method, path
    ):
        """All inherited GroupAccessRequests methods retain the groups prefix."""

        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json={})

        monkeypatch.setenv("GITLAB_URL", "https://gitlab.example.test")
        monkeypatch.setenv("GITLAB_TOKEN", "test-token")
        _reset_settings()
        try:
            client = GitLabClient(transport=httpx.MockTransport(handler))
            monkeypatch.setattr(client_mod, "_client", client)

            getattr(_generated, operation_name)(**params)
        finally:
            _reset_settings()

        assert [(request.method, request.url.path) for request in captured] == [
            (method, path)
        ]

    @pytest.mark.parametrize(
        ("operation_name", "path", "base_params"),
        (
            pytest.param(
                "personal_access_tokens_create",
                "/api/v4/users/{user_id}/personal_access_tokens",
                {"user_id": 1, "name": "token", "scopes": ["api"]},
                id="PersonalAccessTokens.create POST /users/{user_id}/personal_access_tokens",
            ),
            pytest.param(
                "user_impersonation_tokens_create",
                "/api/v4/users/{user_id}/impersonation_tokens",
                {"user_id": 1, "name": "token", "scopes": ["api"]},
                id="UserImpersonationTokens.create POST /users/{user_id}/impersonation_tokens",
            ),
            pytest.param(
                "deploy_tokens_create",
                "/api/v4/projects/{id}/deploy_tokens or /api/v4/groups/{id}/deploy_tokens",
                {
                    "project_id": 1,
                    "name": "token",
                    "scopes": ["read_repository"],
                },
                id="DeployTokens.create POST /projects/{id}/deploy_tokens",
            ),
        ),
    )
    def test_token_creators_without_access_levels_forbid_access_level(
        self, operation_name, path, base_params
    ):
        from gitlab_mcp import _generated
        from gitlab_mcp.server import _build_params_model

        operation = getattr(_generated, operation_name)
        model = _build_params_model(operation)
        assert "access_level" not in model.model_json_schema()["properties"], (
            f"{operation.__name__} {path} access_level must not be advertised"
        )
        with pytest.raises(ValueError):
            model.model_validate({**base_params, "access_level": 0})

    def test_required_id_or_path_positionals_preserve_the_gitbeaker_union(self):
        from gitlab_mcp import _generated
        from gitlab_mcp.server import _build_params_model

        cases = (
            (
                _generated.project_aliases_create,
                "project_id",
                {"name": "project-alias"},
                "group/project",
            ),
            (
                _generated.projects_transfer,
                "namespace",
                {"project_id": 1},
                "parent/child",
            ),
            (
                _generated.linked_epics_create,
                "target_group_id",
                {"group_id": 1, "epic_iid": 2, "target_epic_iid": 3},
                "group/subgroup",
            ),
        )
        for operation, field, required_params, path in cases:
            model = _build_params_model(operation)
            schema = model.model_json_schema()["properties"][field]
            assert _schema_contains_type(schema, "integer")
            assert _schema_contains_type(schema, "string")
            assert model.model_validate(
                {**required_params, field: 42}
            ).model_dump()[field] == 42
            assert model.model_validate(
                {**required_params, field: path}
            ).model_dump()[field] == path

    def test_true_integer_required_positionals_remain_integer_schema_fields(self):
        from gitlab_mcp import _generated
        from gitlab_mcp.server import _build_params_model

        cases = (
            (_generated.issues_clone, "to_project_id"),
            (_generated.issues_move, "to_project_id"),
            (_generated.projects_share, "group_id"),
            (
                _generated.project_job_token_scopes_add_to_inbound_allow_list,
                "target_project_id",
            ),
            (_generated.linked_epics_create, "target_epic_iid"),
        )
        for operation, field in cases:
            schema = _build_params_model(operation).model_json_schema()[
                "properties"
            ][field]
            assert _schema_contains_type(schema, "integer")
            assert not _schema_contains_type(schema, "string")

    def test_body_field_help_maps_python_argument_to_wire_key(self):
        from gitlab_mcp import _generated
        from gitlab_mcp.server import _format_help_full

        pull_mirror_operations = (
            _generated.project_remote_mirrors_create_pull_mirror,
            _generated.projects_create_pull_mirror,
        )
        help_text = _format_help_full(
            {
                "GroupsShare": _generated.groups_share,
                "GroupsTransfer": _generated.groups_transfer,
                "ProjectRemoteMirrorsCreatePullMirror": pull_mirror_operations[0],
                "ProjectsCreatePullMirror": pull_mirror_operations[1],
                "RepositoriesEditChangelog": _generated.repositories_edit_changelog,
            },
            "gitlab_write",
            "",
        )

        assert "Body fields: shared_group_id -> group_id, group_access" in help_text
        assert "Body fields: parent_group_id -> group_id, sudo." in help_text
        assert help_text.count("Body fields: url -> import_url, mirror") == 2
        assert "pull_request_number -> pull_request.number" in help_text
        assert "Body fields: version, from_ -> from, to" in help_text
        assert "Body fields: group_id, group_access" not in help_text

        for operation in pull_mirror_operations:
            with pytest.raises(ValueError, match=r"body field: url -> import_url"):
                operation(1, None, True)  # type: ignore[arg-type]
