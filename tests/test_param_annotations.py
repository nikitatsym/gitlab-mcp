"""Tests for PARAM_ANNOTATIONS wrapping, type rendering, and help output."""

from typing import Annotated, Literal

import pytest
from pydantic import Field

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

    def test_planner_is_accepted_for_member_and_list_access_levels(self):
        from gitlab_mcp import _generated
        from gitlab_mcp.server import _build_params_model

        cases = (
            (_generated.group_members_add, {"group_id": 1, "access_level": 15}),
            (
                _generated.group_members_edit,
                {"group_id": 1, "user_id": 2, "access_level": 15},
            ),
            (_generated.project_members_add, {"project_id": 1, "access_level": 15}),
            (
                _generated.project_members_edit,
                {"project_id": 1, "user_id": 2, "access_level": 15},
            ),
            (
                _generated.group_access_requests_approve,
                {"group_id": 1, "user_id": 2, "access_level": 15},
            ),
            (
                _generated.project_access_requests_approve,
                {"project_id": 1, "user_id": 2, "access_level": 15},
            ),
            (
                _generated.group_member_roles_add,
                {"group_id": 1, "base_access_level": 15, "access_level": 15},
            ),
            (
                _generated.broadcast_messages_create,
                {"message": "notice", "target_access_levels": [15]},
            ),
            (
                _generated.broadcast_messages_edit,
                {"broadcast_message_id": 1, "target_access_levels": [15]},
            ),
            (
                _generated.projects_all_invited_groups,
                {"project_id": 1, "shared_min_access_level": 15},
            ),
            (
                _generated.users_all_contributed_projects,
                {"user_id": 1, "min_access_level": 15},
            ),
        )
        for operation, params in cases:
            _build_params_model(operation).model_validate(params)

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
