"""Unit tests for pydantic-settings config."""

import pytest

from gitlab_mcp.config import Settings, _reset_settings, get_settings


@pytest.fixture(autouse=True)
def _clean_settings(monkeypatch):
    """Reset settings before and after each test, with no gitlab-related env leakage."""
    for var in ("GITLAB_URL", "GITLAB_TOKEN", "GITLAB_BACKEND", "GITLAB_TIMEOUT", "MCP_GITLAB_BRIEF_MAX"):
        monkeypatch.delenv(var, raising=False)
    _reset_settings()
    yield
    _reset_settings()


class TestSettings:
    def test_defaults(self):
        s = Settings()
        assert s.gitlab_url == ""
        assert s.gitlab_token == ""
        assert s.gitlab_backend == "auto"
        assert s.gitlab_timeout == 30.0
        assert s.mcp_gitlab_brief_max == 100

    def test_from_env(self, monkeypatch):
        monkeypatch.setenv("GITLAB_URL", "https://gitlab.example.com")
        monkeypatch.setenv("GITLAB_TOKEN", "glpat-abc123")
        monkeypatch.setenv("GITLAB_BACKEND", "heptapod")
        monkeypatch.setenv("GITLAB_TIMEOUT", "60.0")
        monkeypatch.setenv("MCP_GITLAB_BRIEF_MAX", "200")
        s = Settings()
        assert s.gitlab_url == "https://gitlab.example.com"
        assert s.gitlab_token == "glpat-abc123"
        assert s.gitlab_backend == "heptapod"
        assert s.gitlab_timeout == 60.0
        assert s.mcp_gitlab_brief_max == 200

    def test_invalid_backend_rejected(self, monkeypatch):
        monkeypatch.setenv("GITLAB_BACKEND", "forgejo")
        with pytest.raises(Exception):
            Settings()

    def test_invalid_timeout_rejected(self, monkeypatch):
        monkeypatch.setenv("GITLAB_TIMEOUT", "not-a-number")
        with pytest.raises(Exception):
            Settings()

    def test_url_must_have_scheme(self, monkeypatch):
        monkeypatch.setenv("GITLAB_URL", "gitlab.example.com")
        with pytest.raises(Exception, match="must start with http"):
            Settings()

    def test_url_http_scheme_accepted(self, monkeypatch):
        monkeypatch.setenv("GITLAB_URL", "http://gitlab.local")
        assert Settings().gitlab_url == "http://gitlab.local"

    def test_url_https_scheme_accepted(self, monkeypatch):
        monkeypatch.setenv("GITLAB_URL", "https://gitlab.com")
        assert Settings().gitlab_url == "https://gitlab.com"

    def test_empty_url_still_allowed(self):
        # Empty URL is validated later in main(), not in Settings.
        # This permits test fixtures to construct Settings with no env.
        assert Settings().gitlab_url == ""


class TestGetSettings:
    def test_lazy_singleton(self, monkeypatch):
        monkeypatch.setenv("GITLAB_URL", "https://first.com")
        s1 = get_settings()
        assert s1.gitlab_url == "https://first.com"

        monkeypatch.setenv("GITLAB_URL", "https://second.com")
        s2 = get_settings()
        assert s2 is s1  # same instance, not re-read

    def test_reset_forces_reread(self, monkeypatch):
        monkeypatch.setenv("GITLAB_URL", "https://first.com")
        s1 = get_settings()

        monkeypatch.setenv("GITLAB_URL", "https://second.com")
        _reset_settings()
        s2 = get_settings()
        assert s2.gitlab_url == "https://second.com"
        assert s2 is not s1


class TestMain:
    def test_main_raises_on_missing_url(self):
        from gitlab_mcp import main
        with pytest.raises(ValueError, match="GITLAB_URL and GITLAB_TOKEN must be set"):
            main()

    def test_main_raises_on_missing_token(self, monkeypatch):
        monkeypatch.setenv("GITLAB_URL", "https://gitlab.example.com")
        from gitlab_mcp import main
        with pytest.raises(ValueError, match="GITLAB_URL and GITLAB_TOKEN must be set"):
            main()
