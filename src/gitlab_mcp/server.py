"""GitLab MCP server — auto-discovery, grouping, and dispatch.

Adapted from komodo-mcp's server.py with two extensions:

1. Strict Literal validation in `_coerce_call` — invalid enum values raise
   ValueError before the function runs.
2. `_heptapod_only` filter in `_register_tools` — functions tagged with that
   attribute are skipped when the detected backend is not Heptapod.
"""

import inspect
import typing

from mcp.server.fastmcp import FastMCP

from . import tools as _tools_module
from .client import get_client
from .registry import ROOT

mcp = FastMCP("gitlab")


# ── Helpers ──────────────────────────────────────────────────────────────────


def _to_pascal(name: str) -> str:
    """get_server → GetServer"""
    return "".join(w.capitalize() for w in name.split("_"))


def _parse_bool(val, default: bool) -> bool:
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.lower() in ("1", "true", "yes")
    return bool(val)


def _is_bool_hint(hint) -> bool:
    """Check if a type hint is bool or Optional[bool]."""
    if hint is bool:
        return True
    args = typing.get_args(hint)
    return bool in args if args else False


def _get_literal_values(hint) -> tuple | None:
    """Extract Literal values from a type hint.

    Handles direct Literal[...] and union-wrapped variants like
    Optional[Literal[...]] / Literal[...] | None.
    """
    if hint is None:
        return None
    if typing.get_origin(hint) is typing.Literal:
        return typing.get_args(hint)
    for arg in typing.get_args(hint):
        if typing.get_origin(arg) is typing.Literal:
            return typing.get_args(arg)
    return None


def _coerce_call(fn, params: dict):
    """Validate and coerce JSON-parsed params to match function signature.

    Raises ValueError on:
      - Unknown parameter names (unless the function declares **kwargs,
        in which case unknown params are passed through as keyword args)
      - Invalid values for Literal[...] params
    Coerces:
      - Strings to bool for bool-typed params ("true"/"yes"/"1" → True).
    """
    sig = inspect.signature(fn)
    hints = typing.get_type_hints(fn)

    var_keyword_name: str | None = None
    for p in sig.parameters.values():
        if p.kind is inspect.Parameter.VAR_KEYWORD:
            var_keyword_name = p.name
            break
    has_var_keyword = var_keyword_name is not None
    named_params = {
        n: p for n, p in sig.parameters.items()
        if p.kind is not inspect.Parameter.VAR_KEYWORD
    }

    # Reject the common mistake of wrapping extra body fields under the
    # var-keyword name (e.g. options={"description": "..."}). Those are
    # meant to be passed flat as top-level params.
    if (
        has_var_keyword
        and var_keyword_name in params
        and isinstance(params[var_keyword_name], dict)
    ):
        raise ValueError(
            f"Do not nest body fields under {var_keyword_name!r}. "
            f"Pass additional body fields as top-level params instead "
            f"(e.g. description='text', labels='bug,ux'), "
            f"not {var_keyword_name}={{'description':'text'}}. "
            f"See this op's docstring for the supported body fields."
        )

    if not has_var_keyword:
        unknown = set(params.keys()) - set(named_params.keys())
        if unknown:
            valid = sorted(named_params.keys())
            raise ValueError(
                f"Unknown parameters: {sorted(unknown)}. Valid: {valid}"
            )

    kwargs = {}
    for key, val in params.items():
        param = named_params.get(key)
        hint = hints.get(key) if param is not None else None

        if param is not None and hint and _is_bool_hint(hint) and not isinstance(val, bool):
            default = param.default
            if default is inspect.Parameter.empty or default is None:
                default = False
            val = _parse_bool(val, default)

        if param is not None:
            lit_vals = _get_literal_values(hint)
            if lit_vals is not None and val is not None and val not in lit_vals:
                raise ValueError(
                    f"Invalid value {val!r} for {key}. Accepted: {list(lit_vals)}"
                )

        kwargs[key] = val
    return fn(**kwargs)


# ── Module-level state (populated by _register_tools) ────────────────────────

_group_ops: dict[str, dict] = {}    # {group_name: {PascalName: fn}}
_all_grouped: dict[str, str] = {}   # {PascalName: group_name}


# Common verb words used to detect the boundary between class and method in
# a snake_case op name. Walked from the start; the category is everything
# BEFORE the first verb. Used by `_category_for_fn` as a fallback when the
# function's docstring doesn't already start with `ClassName.method`.
_VERB_WORDS = frozenset({
    "all", "show", "create", "edit", "update", "remove", "delete", "add",
    "get", "set", "star", "unstar", "subscribe", "unsubscribe", "fork",
    "merge", "accept", "approve", "reject", "block", "unblock", "search",
    "render", "lint", "verify", "run", "play", "cancel", "retry", "erase",
    "keep", "sync", "promote", "restore", "archive", "unarchive", "transfer",
    "share", "pick", "revert", "activate", "deactivate", "ban", "unban",
    "follow", "unfollow", "test", "download", "upload", "import", "export",
    "schedule", "move", "clone", "reorder", "reset", "take", "trigger",
    "publish", "disable", "enable", "rotate", "generate", "send", "preview",
    "register", "unregister", "list",
})


def _category_from_snake(snake_name: str) -> str:
    """Heuristic: extract the resource-class category from a snake_case op name."""
    parts = snake_name.split("_")
    for i, part in enumerate(parts):
        if i > 0 and part in _VERB_WORDS:
            return "".join(p.title() for p in parts[:i])
    return "".join(p.title() for p in parts)


def _category_for_fn(fn) -> str:
    """Resolve a function's resource category.

    Codegen-emitted functions have docstrings starting with `ClassName.method`,
    so we extract the class name from there. Hand-written overrides fall back
    to a heuristic on the function name.
    """
    import re
    doc = fn.__doc__ or ""
    m = re.match(r"(\w+)\.\w+\s*\(", doc)
    if m:
        return m.group(1)
    return _category_from_snake(fn.__name__)


def _build_help(
    group_name: str,
    category: str | None = None,
    search: str | None = None,
) -> str:
    """Build help text for a group, with progressive disclosure.

    Default (no params): compact category index — one line per resource class
    with op count. Use this to discover what's available without dumping
    hundreds of operations.

    With `category="X"`: full signatures of all ops in that category.

    With `search="foo"`: full signatures of all ops whose name contains
    `foo` (case-insensitive).
    """
    ops = _group_ops[group_name]

    # ── Filtered detailed listings ──
    if search:
        s = search.lower()
        matched = {k: v for k, v in ops.items() if s in k.lower()}
        elsewhere: dict[str, list[str]] = {}
        for op_name, other_group in _all_grouped.items():
            if other_group == group_name:
                continue
            if s in op_name.lower():
                elsewhere.setdefault(other_group, []).append(op_name)
        if not matched:
            msg = f"No ops in {group_name} matching {search!r}."
            if elsewhere:
                msg += " Found in other groups: " + "; ".join(
                    f"{g}: {', '.join(sorted(names))}"
                    for g, names in sorted(elsewhere.items())
                )
            else:
                msg += " Use operation='help' (no params) for the category index."
            return msg
        out = _format_help_full(matched, group_name, f"matching {search!r}")
        if elsewhere:
            out += "\n\nAlso matching in other groups: " + "; ".join(
                f"{g}: {', '.join(sorted(names))}"
                for g, names in sorted(elsewhere.items())
            )
        return out

    if category:
        matched = {
            k: v for k, v in ops.items() if _category_for_fn(v) == category
        }
        if not matched:
            return (
                f"No category {category!r} in {group_name}. "
                f"Use operation='help' (no params) for the category index."
            )
        return _format_help_full(matched, group_name, f"in category {category!r}")

    # ── Compact category index (default) ──
    by_category: dict[str, int] = {}
    for fn in ops.values():
        cat = _category_for_fn(fn)
        by_category[cat] = by_category.get(cat, 0) + 1

    lines = [
        f"{len(ops)} operations in {group_name}, grouped by resource.",
        "Drill down with operation='help' params={'category': 'X'} for full signatures, "
        "or params={'search': 'foo'} to filter by name.",
        "",
    ]
    for cat in sorted(by_category):
        lines.append(f"  {cat}: {by_category[cat]} ops")
    return "\n".join(lines)


def _format_help_full(ops: dict, group_name: str, scope_desc: str) -> str:
    """Render a full signature listing for a filtered set of ops."""
    lines = [
        f"{len(ops)} operations in {group_name} {scope_desc}:",
        "",
        "NOTE: `**options` in a signature means the op accepts additional "
        "body fields (e.g. description, labels, assignee_ids). Pass them as "
        "TOP-LEVEL params; do NOT nest them under an 'options' key.",
        "",
    ]
    for pascal_name in sorted(ops):
        fn = ops[pascal_name]
        sig = inspect.signature(fn)
        parts = []
        for name, p in sig.parameters.items():
            if p.kind is inspect.Parameter.VAR_KEYWORD:
                parts.append(f"**{name}")
            else:
                parts.append(name)
        params = ", ".join(parts)
        doc = (fn.__doc__ or "").split("\n")[0]
        lines.append(f"  {pascal_name}({params}) — {doc}")
    return "\n".join(lines)


def _dispatch(operation: str, group_name: str, params: dict):
    """Dispatch an operation call to the right function."""
    ops = _group_ops[group_name]
    if operation not in ops:
        if operation in _all_grouped:
            correct = _all_grouped[operation]
            return {
                "error": f"{operation} belongs to {correct}. "
                         f"Use {correct}() instead."
            }
        return {
            "error": f"Unknown operation: {operation}. "
                     "Use operation=\"help\" to list available operations."
        }

    fn = ops[operation]
    return _coerce_call(fn, params)


# ── Registration ─────────────────────────────────────────────────────────


def _should_include(fn) -> bool:
    """Return False for Heptapod-only tools when the backend isn't Heptapod."""
    if not getattr(fn, "_heptapod_only", False):
        return True
    inst = get_client().instance
    if inst is None:
        # Pre-main() import (tests, introspection). Include by default.
        return True
    return inst.backend == "heptapod"


def _register_tools():
    """Discover @_op-decorated functions, filter, and register as MCP tools.

    Called explicitly by `main()` after `client.instance` is populated by
    `detect_instance()`. Not auto-called at import time — that would run
    the Heptapod filter before startup detection has finished.
    """
    # Reset module-level state to allow re-registration in tests.
    _group_ops.clear()
    _all_grouped.clear()

    groups: dict[str, tuple] = {}  # {group_name: (Group, {snake_name: fn})}

    for name, fn in inspect.getmembers(_tools_module, inspect.isfunction):
        if name.startswith("_"):
            continue
        if not hasattr(fn, "_mcp_group"):
            continue
        if not _should_include(fn):
            continue
        group = fn._mcp_group
        if group is ROOT:
            mcp.tool()(fn)
        else:
            if group.name not in groups:
                groups[group.name] = (group, {})
            groups[group.name][1][name] = fn

    for group_name, (group, fns) in groups.items():
        ops = {_to_pascal(n): fn for n, fn in fns.items()}
        _group_ops[group_name] = ops
        for pascal_name in ops:
            _all_grouped[pascal_name] = group_name

        def _make_tool(gname, gdoc):
            def tool_fn(operation: str, params: dict = {}):
                if operation == "help":
                    return _build_help(
                        gname,
                        category=params.get("category") if params else None,
                        search=params.get("search") if params else None,
                    )
                return _dispatch(operation, gname, params)
            tool_fn.__name__ = gname
            tool_fn.__qualname__ = gname
            tool_fn.__doc__ = gdoc
            return tool_fn

        mcp.tool()(_make_tool(group_name, group.doc))
