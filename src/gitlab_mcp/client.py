from __future__ import annotations

import logging
import time
from collections import OrderedDict
from typing import TYPE_CHECKING, Any

import httpx

from .config import get_settings

if TYPE_CHECKING:
    from .backend import InstanceInfo

_log = logging.getLogger("gitlab_mcp.client")

_DEFAULT_PER_PAGE = 20
_MAX_PAGINATE_PAGES = 50


class GitLabError(Exception):
    """GitLab/Heptapod API error with full context."""

    def __init__(self, status: int, method: str, path: str, body: Any):
        self.status = status
        self.method = method
        self.path = path
        self.body = body
        super().__init__(f"GitLab API {status} {method} {path}: {body}")


class GitLabClient:
    """REST client for GitLab v4 / Heptapod.

    Sends `PRIVATE-TOKEN` header which covers PATs, project/group access tokens,
    OAuth2 bearer tokens, and job tokens uniformly.
    """

    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        timeout: float | None = None,
        transport: httpx.BaseTransport | None = None,
    ):
        s = get_settings()
        self._base = (base_url or s.gitlab_url).rstrip("/")
        self._token = token or s.gitlab_token
        self._http = httpx.Client(
            base_url=f"{self._base}/api/v4",
            headers={"PRIVATE-TOKEN": self._token},
            timeout=timeout if timeout is not None else s.gitlab_timeout,
            transport=transport,
        )
        # Populated by main() via detect_instance() or explicit backend seed.
        # Using Any to avoid circular import; real type is InstanceInfo.
        self.instance: "InstanceInfo | None" = None
        # Per-project vcs_type FIFO cache, bounded.
        self._project_cache: OrderedDict[str, str] = OrderedDict()
        self._project_cache_max = 256

    # ── low-level ──────────────────────────────────────────────

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        start = time.perf_counter()
        r = self._http.request(method, path, **kwargs)
        duration_ms = int((time.perf_counter() - start) * 1000)
        status = r.status_code
        if status >= 500:
            level = logging.ERROR
        elif status >= 400:
            level = logging.WARNING
        else:
            level = logging.INFO
        _log.log(level, "%s %s %d %dms", method, path, status, duration_ms)
        if status >= 400:
            try:
                body = r.json()
            except Exception:
                body = r.text
            raise GitLabError(status, method, path, body)
        return r

    def _json(self, method: str, path: str, **kwargs):
        r = self._request(method, path, **kwargs)
        if r.status_code == 204 or not r.content:
            return None
        return r.json()

    def _text(self, method: str, path: str, **kwargs) -> str:
        r = self._request(method, path, **kwargs)
        return r.text

    # ── HTTP verbs ─────────────────────────────────────────────

    def get(self, path: str, params: dict | None = None):
        return self._json("GET", path, params=params)

    def post(self, path: str, json=None, **kwargs):
        return self._json("POST", path, json=json, **kwargs)

    def put(self, path: str, json=None, **kwargs):
        return self._json("PUT", path, json=json, **kwargs)

    def patch(self, path: str, json=None, **kwargs):
        return self._json("PATCH", path, json=json, **kwargs)

    def delete(self, path: str, params: dict | None = None):
        return self._json("DELETE", path, params=params)

    def get_text(self, path: str, params: dict | None = None) -> str:
        return self._text("GET", path, params=params)

    def request(self, method: str, path: str, **kwargs):
        """Generic entry used by generated wrappers."""
        return self._json(method, path, **kwargs)

    def paginate(
        self,
        path: str,
        params: dict | None = None,
        per_page: int = _DEFAULT_PER_PAGE,
        max_pages: int = _MAX_PAGINATE_PAGES,
    ) -> list:
        """Walk GitLab pagination; capped at max_pages to avoid runaway."""
        params = dict(params or {})
        params["per_page"] = per_page
        result: list = []
        for page in range(1, max_pages + 1):
            params["page"] = page
            data = self._json("GET", path, params=params)
            if not data:
                break
            result.extend(data)
            if len(data) < per_page:
                break
        return result

    # ── per-project vcs_type cache (FIFO, bounded) ─────────────

    def project_vcs_type(self, project_id: str | int) -> str:
        key = str(project_id)
        if key in self._project_cache:
            return self._project_cache[key]
        # Lazy import to avoid circular dependency.
        from .backend import project_vcs_type as _probe

        vcs = _probe(self, key)
        self._project_cache[key] = vcs
        while len(self._project_cache) > self._project_cache_max:
            self._project_cache.popitem(last=False)
        return vcs


# ── module singleton accessor ─────────────────────────────────

_client: GitLabClient | None = None


def get_client() -> GitLabClient:
    global _client
    if _client is None:
        _client = GitLabClient()
    return _client


def _reset_client() -> None:
    """Force re-creation on next get_client(). Used by tests."""
    global _client
    _client = None


# Underscore alias used by tools.py / _generated.py.
_get_client = get_client
