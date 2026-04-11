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

    has_var_keyword = any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
    )
    named_params = {
        n: p for n, p in sig.parameters.items()
        if p.kind is not inspect.Parameter.VAR_KEYWORD
    }

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


def _build_help(group_name: str) -> str:
    """Build help text from operation functions in a group."""
    ops = _group_ops[group_name]
    lines = []
    for pascal_name, fn in ops.items():
        sig = inspect.signature(fn)
        params = ", ".join(sig.parameters.keys())
        doc = (fn.__doc__ or "").split("\n")[0]
        lines.append(f"  {pascal_name}({params}) — {doc}")
    return f"{len(lines)} operations available:\n" + "\n".join(lines)


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
                    return _build_help(gname)
                return _dispatch(operation, gname, params)
            tool_fn.__name__ = gname
            tool_fn.__qualname__ = gname
            tool_fn.__doc__ = gdoc
            return tool_fn

        mcp.tool()(_make_tool(group_name, group.doc))
