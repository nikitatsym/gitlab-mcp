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
            del tools.synthetic_wrap_op

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
            del tools.synthetic_wrap_op2


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
