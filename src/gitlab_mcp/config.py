from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    gitlab_url: str = ""
    gitlab_token: str = ""
    gitlab_backend: Literal["auto", "gitlab", "heptapod"] = "auto"
    gitlab_timeout: float = 30.0
    mcp_gitlab_brief_max: int = 100

    @field_validator("gitlab_url")
    @classmethod
    def _validate_url_scheme(cls, v: str) -> str:
        if v and not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError(
                f"gitlab_url must start with http:// or https://, got {v!r}"
            )
        return v


_settings: Settings | None = None

# Not an env var — only controllable via the --allow-public CLI flag,
# parsed in __init__.main() before FastMCP sees the args. Default-deny
# means agents can't accidentally create public projects/snippets.
_allow_public: bool = False


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def allow_public() -> bool:
    return _allow_public


def set_allow_public(value: bool) -> None:
    global _allow_public
    _allow_public = value


def _reset_settings() -> None:
    """Force re-read from env. Used by tests."""
    global _settings
    _settings = None
