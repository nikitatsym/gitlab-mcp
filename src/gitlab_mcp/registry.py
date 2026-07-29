"""Tool registration primitives."""

from typing import Any


class Group:
    """A named group of MCP tool operations exposed as a single meta-tool."""

    __slots__ = ("doc", "name")

    def __init__(self, name: str, doc: str):
        self.name = name
        self.doc = doc


ROOT = Group("root", "")


class _Unset:
    """Sentinel singleton: caller did not pass this field.

    Distinct from None, which means 'caller explicitly passed null'. Used as
    the function-signature default for optional body params in _generated.py,
    so payload construction can check `is not _UNSET` and forward explicit
    null values to GitLab (e.g. assignee_id=null to un-assign).
    """

    _instance: "_Unset | None" = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "_UNSET"

    def __bool__(self) -> bool:
        return False


# Annotated as Any so it can stand in as the default for any typed parameter
# without forcing every signature to include `| _Unset`. Runtime identity is
# unchanged; mypy stops flagging hand-written test fixtures or stubs that use
# `_UNSET` as a default. Generated signatures still include the explicit
# `| _Unset` union for documentation accuracy.
_UNSET: Any = _Unset()


def _op(group: Group):
    """Mark a function as an MCP tool in the given group."""
    def decorator(fn):
        if not fn.__doc__:
            raise RuntimeError(f"Tool function {fn.__name__!r} has no docstring")
        fn._mcp_group = group
        return fn
    return decorator
