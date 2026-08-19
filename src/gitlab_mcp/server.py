"""GitLab MCP server — auto-discovery, grouping, and dispatch.

Adapted from komodo-mcp's server.py with two extensions:

1. Strict Literal validation in `_coerce_call` — invalid enum values raise
   ValueError before the function runs.
2. `_heptapod_only` filter in `_register_tools` — functions tagged with that
   attribute are skipped when the detected backend is not Heptapod.
"""

import inspect
import re
import string
import types as _types
import typing
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import Context, MCPServer
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    create_model,
    field_validator,
)

from . import tools as _tools_module
from .client import get_client
from .registry import _UNSET, ROOT, _Unset

mcp = MCPServer("gitlab")


# Parameter name reserved for MCPServer's Context injection. Tools that declare
# this param receive the live MCP request context (for report_progress / log)
# but it's never exposed to callers — the Pydantic params model skips it and
# `_coerce_call` injects it after validation.
_CTX_PARAM = "ctx"


# ── Helpers ──────────────────────────────────────────────────────────────────


def _to_pascal(name: str) -> str:
    """get_server → GetServer"""
    return "".join(w.capitalize() for w in name.split("_"))


def _build_params_model(fn) -> type[BaseModel]:
    """Build a Pydantic model that validates the params for one tool function.

    - Required signature args → required fields.
    - Args defaulting to `_UNSET` → optional via `default_factory` (Pydantic v2
      omits `default` from JSON schema in this case, so introspection doesn't
      lie about field defaults).
    - Other defaults are reused as-is.
    - `**kwargs` in the signature → `extra='allow'`; otherwise `extra='forbid'`.
    - Loose string→bool coercion ("True"/"yes"/"0") preserved via a
      before-validator on every field.
    """
    hints = typing.get_type_hints(fn, include_extras=True)
    sig = inspect.signature(fn)
    fields: dict[str, tuple] = {}
    has_var_keyword = False
    for name, p in sig.parameters.items():
        if p.kind is inspect.Parameter.VAR_KEYWORD:
            has_var_keyword = True
            continue
        if name == _CTX_PARAM:
            continue
        ann = hints.get(name, Any)
        if p.default is inspect.Parameter.empty:
            field_spec: Any = ...
        elif isinstance(p.default, _Unset):
            field_spec = Field(default_factory=lambda: _UNSET)
        else:
            field_spec = p.default
        fields[name] = (ann, field_spec)
    extra: Literal["allow", "forbid"] = "allow" if has_var_keyword else "forbid"

    @field_validator("*", mode="before")  # type: ignore[misc]
    @classmethod
    def _coerce_string_bool(cls, v, info):
        if not isinstance(v, str):
            return v
        ann = cls.model_fields[info.field_name].annotation
        types_in_ann = (ann,) + typing.get_args(ann)
        if bool not in types_in_ann:
            return v
        lower = v.lower()
        if lower in ("true", "1", "yes"):
            return True
        if lower in ("false", "0", "no"):
            return False
        return v

    model: type[BaseModel] = create_model(  # type: ignore[call-overload]
        f"{_to_pascal(fn.__name__)}Params",
        __config__=ConfigDict(extra=extra, arbitrary_types_allowed=True),
        __validators__={"_coerce_string_bool": _coerce_string_bool},
        **fields,
    )
    return model


def _format_validation_error(err: ValidationError, fn) -> str:
    """Pydantic ValidationError → readable multi-line message.

    Ends with the accepted parameter list (from the op's signature) and a
    pointer at `operation='help'` search, so a caller that passed a wrong
    field can self-correct without a second failed call. The op name is
    derived from the function name so callers of `_coerce_call` that don't
    know the PascalCase op (tests, direct dispatch) still get a real name.
    """
    op_name = _to_pascal(fn.__name__)
    lines = [f"Invalid params for {op_name}:"]
    for e in err.errors():
        loc = ".".join(str(x) for x in e["loc"]) or "<root>"
        msg = e["msg"]
        got = repr(e.get("input"))
        if len(got) > 80:
            got = got[:77] + "..."
        lines.append(f"  - {loc}: {msg} (got {got})")
    accepted = []
    for name, p in inspect.signature(fn).parameters.items():
        if name == _CTX_PARAM:
            continue
        if p.kind is inspect.Parameter.VAR_KEYWORD:
            accepted.append(f"plus arbitrary top-level body fields (**{name})")
        elif p.default is inspect.Parameter.empty:
            accepted.append(name)
        else:
            accepted.append(f"{name}?")
    lines.append(f"Accepted params ('?' = optional): {', '.join(accepted)}")
    # There is no 'schema' operation; 'help' search is the full-spec path.
    lines.append(
        f"Call operation='help', params={{'search': {op_name!r}}} for the full parameter spec."
    )
    return "\n".join(lines)


def _coerce_call(fn, params: dict, ctx: Context | None = None):
    """Validate `params` via the cached Pydantic model and invoke `fn`.

    - Caller-omitted fields (sentinel `_UNSET` defaults) are filtered via
      `exclude_unset=True` so the function still sees its own default.
    - Extras land in `model_extra` and are forwarded as **kwargs.
    - Pre-flight check still rejects body-fields-nested-under-`**options`
      with a friendly hint (Pydantic's "extra not permitted" error wouldn't
      flag this since the var-keyword name IS a valid key).
    - When the target function declares a `ctx` parameter, the live MCP
      Context (when present) is injected after validation. `ctx` is never
      part of the Pydantic params model, so callers can't pass it themselves.
    """
    sig_params = inspect.signature(fn).parameters
    var_kw = next(
        (p.name for p in sig_params.values()
         if p.kind is inspect.Parameter.VAR_KEYWORD),
        None,
    )
    if var_kw and var_kw in params and isinstance(params[var_kw], dict):
        raise ValueError(
            f"Do not nest body fields under {var_kw!r}. "
            f"Pass additional body fields as top-level params instead "
            f"(e.g. description='text', labels='bug,ux'), "
            f"not {var_kw}={{'description':'text'}}. "
            f"See this op's docstring for the supported body fields."
        )

    model = getattr(fn, "_mcp_params_model", None) or _build_params_model(fn)
    try:
        validated = model.model_validate(params)
    except ValidationError as e:
        raise ValueError(_format_validation_error(e, fn)) from None
    kwargs = validated.model_dump(exclude_unset=True)
    if validated.model_extra:
        kwargs.update(validated.model_extra)
    if _CTX_PARAM in sig_params:
        kwargs[_CTX_PARAM] = ctx
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

        def _hit(name: str, fn) -> bool:
            # Match op name AND docstring (ANNOTATIONS overrides land in
            # __doc__), so intent words find ops named differently.
            return s in name.lower() or s in (inspect.getdoc(fn) or "").lower()

        matched = {k: v for k, v in ops.items() if _hit(k, v)}
        elsewhere: dict[str, list[str]] = {}
        for op_name, other_group in _all_grouped.items():
            if other_group == group_name:
                continue
            other_fn = _group_ops.get(other_group, {}).get(op_name)
            if other_fn is not None and _hit(op_name, other_fn):
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
        (
            "Drill down with operation='help' params={'category': 'X'} for full signatures, "
            "or params={'search': 'foo'} to filter by name."
        ),
        "",
    ]
    for cat in sorted(by_category):
        lines.append(f"  {cat}: {by_category[cat]} ops")
    return "\n".join(lines)


def _render_type(hint) -> str:
    """Render a type hint for help display.

    - Strips Annotated wrappers.
    - Formats Literal as 'a|b|c' (no quotes for str literals).
    - Unions render as 'T | None', list[T] / dict canonical.
    """
    if hint is None or hint is type(None):
        return "None"
    if typing.get_origin(hint) is Annotated:
        hint = typing.get_args(hint)[0]
    origin = typing.get_origin(hint)
    args = typing.get_args(hint)
    if origin in (typing.Union, _types.UnionType):
        return " | ".join(_render_type(a) for a in args)
    if origin is typing.Literal:
        return "|".join(a if isinstance(a, str) else repr(a) for a in args)
    if origin in (list, tuple, set):
        inner = ", ".join(_render_type(a) for a in args) or "Any"
        return f"{origin.__name__}[{inner}]"
    if origin is dict:
        return "dict"
    return getattr(hint, "__name__", repr(hint))


def _format_help_full(ops: dict, group_name: str, scope_desc: str) -> str:
    """Render a full signature listing for a filtered set of ops.

    Signature conventions:
      - `name: T`        — required.
      - `name?: T`       — optional (caller may omit). Signalled by _UNSET default.
      - `name: T | None` — nullable (caller MUST pass; may pass null).
      - `name?: T | None`— both.

    Per-param descriptions from PARAM_ANNOTATIONS render as indented bullets
    under the signature.
    """
    has_variadic = any(
        p.kind is inspect.Parameter.VAR_KEYWORD
        for fn in ops.values()
        for p in inspect.signature(fn).parameters.values()
    )
    note = (
        "NOTE: every signature is closed - only the listed fields are accepted. "
        "Unknown kwargs are rejected before any HTTP call. If a field you need "
        "is missing, the codegen source (OpenAPI / gitbeaker TS) didn't list it "
        "at the current pin; raise an issue or add it to MANUAL_PARAMS."
    )
    if has_variadic:
        note += (
            " Exception: ops whose signature shows **options accept arbitrary "
            "extra top-level params, forwarded as request body fields."
        )
    lines = [
        f"{len(ops)} operations in {group_name} {scope_desc}:",
        "",
        note,
        "",
    ]
    for pascal_name in sorted(ops):
        fn = ops[pascal_name]
        sig = inspect.signature(fn)
        hints = getattr(fn, "_mcp_hints", None) or typing.get_type_hints(
            fn, include_extras=True
        )
        parts: list[str] = []
        descs: list[tuple[str, str]] = []
        for name, p in sig.parameters.items():
            if p.kind is inspect.Parameter.VAR_KEYWORD:
                parts.append(f"**{name}")
                continue
            if name == _CTX_PARAM:
                continue
            hint = hints.get(name)
            type_str = _render_type(hint) if hint is not None else "Any"
            if p.default is inspect.Parameter.empty:
                parts.append(f"{name}: {type_str}")
            elif isinstance(p.default, _Unset):
                parts.append(f"{name}?: {type_str}")
            elif p.default is None:
                parts.append(f"{name}: {type_str} = None")
            else:
                parts.append(f"{name}: {type_str} = {p.default!r}")
            if typing.get_origin(hint) is Annotated:
                for meta in typing.get_args(hint)[1:]:
                    desc = getattr(meta, "description", None)
                    if desc:
                        descs.append((name, desc))
        params = ", ".join(parts)
        doc = inspect.getdoc(fn) or ""
        head, _, body = doc.partition("\n\n")
        head = " ".join(head.split())  # collapse any wrapping in the head line
        lines.append(f"  {pascal_name}({params}) — {head}")
        for body_line in body.rstrip().splitlines():
            lines.append(f"      {body_line}" if body_line else "")
        for name, desc in descs:
            lines.append(f"      {name}: {desc}")
    return "\n".join(lines)


def _dispatch(operation: str, group_name: str, params: dict, ctx: Context | None = None):
    """Dispatch an operation call to the right function.

    Synchronous ops are called directly. Async ops (e.g. pipelines_wait,
    jobs_wait) return a coroutine which is returned as-is — the meta-tool
    `tool_fn` awaits it. Calling `_dispatch` directly from a sync caller
    against an async op therefore yields an awaitable, not a value;
    callers in that situation should `asyncio.run(...)` the result.
    """
    ops = _group_ops[group_name]
    if operation not in ops:
        if operation in _all_grouped:
            correct = _all_grouped[operation]
            raise ValueError(
                f"{operation!r} belongs to {correct!r}, not {group_name!r}. "
                f"Call {correct}(operation={operation!r}, ...) instead."
            )
        raise ValueError(
            f"Unknown operation {operation!r} in {group_name!r}. "
            "Use operation='help' to list available operations."
        )

    fn = ops[operation]
    return _coerce_call(fn, params, ctx)


# ── Registration ─────────────────────────────────────────────────────────


def _make_tool(group_name: str, group_doc: str):
    """Build the meta-tool function for a group.

    Async by design so tools that need the MCP Context (progress / log)
    can `await ctx.report_progress(...)` from inside their dispatch path.
    Sync ops still work — `_coerce_call` returns whatever the op returns,
    and we only `await` when the result is actually a coroutine.

    The `ctx` parameter is typed `Context` so MCPServer injects the live
    request context; it never appears in the tool's JSON schema for callers.
    """
    async def tool_fn(
        operation: str,
        params: dict | None = None,
        ctx: Context | None = None,
    ):
        params = params or {}
        if operation == "help":
            return _build_help(
                group_name,
                category=params.get("category"),
                search=params.get("search"),
            )
        result = _dispatch(operation, group_name, params, ctx)
        if inspect.iscoroutine(result):
            result = await result
        return result
    tool_fn.__name__ = group_name
    tool_fn.__qualname__ = group_name
    tool_fn.__doc__ = group_doc
    return tool_fn


_HARDCODED_OPERATION = re.compile(r"""\boperation\s*=\s*["'](?![$<])""")


def _render_group_doc(group_name: str, doc: str, ops: dict) -> str:
    """Resolve $OpName placeholders in a group doc against the registered operations.

    Examples are hand-written while operation names come from OpenAPI operationIds;
    rendering the names from the registry keeps the two from drifting apart, and an
    unresolved placeholder aborts startup. A hardcoded operation name is rejected
    outright; angle-bracket placeholders (`operation="<OpName>"`) stay available
    for deliberately generic usage patterns.
    """
    if _HARDCODED_OPERATION.search(doc):
        raise RuntimeError(
            f"{group_name} doc hardcodes an operation name; use the $OpName form"
        )
    names = {name: name for name in ops} | {"help": "help"}
    try:
        return string.Template(doc).substitute(names)
    except (KeyError, ValueError) as exc:
        raise RuntimeError(
            f"{group_name} doc references an unknown operation placeholder: {exc}"
        ) from exc


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
        doc = _render_group_doc(group_name, group.doc, ops)
        for pascal_name in ops:
            _all_grouped[pascal_name] = group_name

        mcp.tool()(_make_tool(group_name, doc))

    # Eager: build params models + cache type hints on each op function. Done
    # once at startup so dispatch is hint/model-free. ~100-150ms for ~800 ops.
    for ops in _group_ops.values():
        for fn in ops.values():
            fn._mcp_hints = typing.get_type_hints(fn, include_extras=True)
            fn._mcp_params_model = _build_params_model(fn)

    _register_wait_resource()


# ── Resources ────────────────────────────────────────────────────────────


_WAIT_RESOURCE_REGISTERED = False


def _register_wait_resource() -> None:
    """Register `gitlab://waits/{wait_id}` once per process.

    The resource returns the JSON snapshot of an in-flight or recently-terminal
    wait operation. Clients that support resource reads can observe wait state
    without going through a tool call — useful as a side-channel for agents
    that want to peek between other operations.

    MCPServer's resource manager rejects duplicate registration, so we gate
    on a module-level flag. Tests that call `_register_tools()` multiple
    times therefore see exactly one registration.
    """
    global _WAIT_RESOURCE_REGISTERED
    if _WAIT_RESOURCE_REGISTERED:
        return
    import json as _json

    from .wait_registry import WAIT_REGISTRY as _WAIT_REGISTRY

    @mcp.resource(
        "gitlab://waits/{wait_id}",
        name="GitLab wait snapshot",
        description=(
            "JSON snapshot of a long-running wait operation registered by "
            "pipelines_wait or jobs_wait. Same shape as the "
            "return value of the corresponding *_wait_poll tool."
        ),
        mime_type="application/json",
    )
    def _read_wait(wait_id: str) -> str:
        handle = _WAIT_REGISTRY.get(wait_id)
        if handle is None:
            return _json.dumps({
                "error": f"unknown wait_id: {wait_id!r}",
                "hint": (
                    "Use waits_list to enumerate currently-known waits. "
                    "Terminated waits are reaped after ~1 hour."
                ),
            })
        return _json.dumps(handle.snapshot(), default=str)

    _WAIT_RESOURCE_REGISTERED = True
