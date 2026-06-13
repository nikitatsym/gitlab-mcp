"""Integration tests against a live Heptapod container.

Heptapod boots slow (5-8 minutes). Run via `npm run test:integration:heptapod`
which bootstraps the container and points tests/.env at it. The fixture
HARD-FAILS if the env points elsewhere (or nothing) — silence is a bug.
"""

from __future__ import annotations

import time

import pytest


def _await_project_ready(agent, project_id, timeout=30):
    """Wait until the project's `initialize_with_readme` seed commit lands.

    `default_branch` is the moment the repo has its first commit, regardless of
    backend (`main` on git, `branch/default` on hg). Tests that read branches
    or files immediately after `projects_create` race the seed commit and get
    404 (`Commit Not Found`) or empty branch list otherwise.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            proj = agent.call("projects_show", project_id=project_id)
            if isinstance(proj, dict) and proj.get("default_branch"):
                return
        except Exception:
            pass
        time.sleep(0.5)
    raise AssertionError(
        f"project {project_id} did not finish seed-commit within {timeout}s"
    )


pytestmark = [pytest.mark.integration]


class TestHeptapodBackendDetection:
    def test_detect_heptapod(self, agent_heptapod):
        result = agent_heptapod.call("gitlab_version")
        service = result["service"]
        assert service["backend"] == "heptapod"
        assert "hg" in service["vcs_types"] or "hg_git" in service["vcs_types"]

    def test_hg_tools_registered(self, agent_heptapod):
        available = set(agent_heptapod._tools.keys())
        hg_tools = {
            "hg_get_config",
            "hg_get_raw_hgrc",
            "hg_set_config",
            "hg_create_topic_mr",
        }
        assert hg_tools.issubset(available), (
            f"Expected all hg_* tools registered on heptapod; missing: {hg_tools - available}"
        )


class TestHeptapodGitProject:
    """A git-typed project on Heptapod behaves exactly like a GitLab project."""

    project_id: int = 0

    def test_01_create_git_project(self, agent_heptapod):
        # Heptapod (17-0 and later) defaults new projects to vcs_type=hg, so
        # we MUST opt into git explicitly — otherwise the "git project" suite
        # silently runs against an hg project.
        result = agent_heptapod.call(
            "projects_create",
            name="integration-git",
            vcs_type="git",
            initialize_with_readme=True,
            visibility="private",
        )
        TestHeptapodGitProject.project_id = result["id"]

    def test_02_hg_create_topic_mr_refuses_git_project(self, agent_heptapod):
        with pytest.raises(ValueError, match="requires a Mercurial project"):
            agent_heptapod.call(
                "hg_create_topic_mr",
                project_id=TestHeptapodGitProject.project_id,
                target_hg_branch="main",
                topic_name="foo",
                title="bad",
            )

    def test_99_cleanup(self, agent_heptapod):
        agent_heptapod.call(
            "projects_remove",
            project_id=TestHeptapodGitProject.project_id,
            permanent=True,
        )


class TestHeptapodHgConfig:
    """hg_get_config / hg_set_config round-trip (documents PUT-not-PATCH semantics)."""

    project_id: int = 0

    def test_01_create_hg_project(self, agent_heptapod):
        result = agent_heptapod.call(
            "projects_create",
            name="integration-hg",
            vcs_type="hg",  # Heptapod-specific field
            initialize_with_readme=True,
            visibility="private",
        )
        TestHeptapodHgConfig.project_id = result["id"]

    def test_10_get_initial_config(self, agent_heptapod):
        result = agent_heptapod.call(
            "hg_get_config",
            project_id=TestHeptapodHgConfig.project_id,
        )
        assert "inherit" in result

    def test_20_set_config_full(self, agent_heptapod):
        agent_heptapod.call(
            "hg_set_config",
            project_id=TestHeptapodHgConfig.project_id,
            inherit=False,
            allow_bookmarks=True,
            allow_multiple_heads=False,
            auto_publish="all",
        )
        result = agent_heptapod.call(
            "hg_get_config",
            project_id=TestHeptapodHgConfig.project_id,
        )
        assert result["allow_bookmarks"] is True
        assert result["auto_publish"] == "all"

    def test_21_set_config_partial_preserves_unsent(self, agent_heptapod):
        """Document PATCH-semantic: omitted fields keep their previous value.

        Heptapod 1.x reset omitted fields to defaults (PUT). 17+ preserves
        them (PATCH). hg_set_config's docstring spells this out.
        """
        agent_heptapod.call(
            "hg_set_config",
            project_id=TestHeptapodHgConfig.project_id,
            inherit=False,
            # Deliberately omit allow_bookmarks; previously set to True in
            # test_20 — should NOT reset.
        )
        result = agent_heptapod.call(
            "hg_get_config",
            project_id=TestHeptapodHgConfig.project_id,
        )
        assert result["allow_bookmarks"] is True

    def test_30_invalid_auto_publish_rejected(self, agent_heptapod):
        with pytest.raises(ValueError, match="auto_publish"):
            agent_heptapod.call(
                "hg_set_config",
                project_id=TestHeptapodHgConfig.project_id,
                inherit=False,
                auto_publish="bogus",
            )

    def test_99_cleanup(self, agent_heptapod):
        agent_heptapod.call(
            "projects_remove",
            project_id=TestHeptapodHgConfig.project_id,
            permanent=True,
        )


class TestHeptapodBranchConvention:
    """List branches on an hg project shows `branch/...` and `topic/...` prefixes verbatim."""

    project_id: int = 0

    def test_01_create_hg_project(self, agent_heptapod):
        result = agent_heptapod.call(
            "projects_create",
            name="integration-hg-branches",
            vcs_type="hg",
            initialize_with_readme=True,
            visibility="private",
        )
        TestHeptapodBranchConvention.project_id = result["id"]
        _await_project_ready(agent_heptapod, result["id"])

    def test_10_list_branches_shows_hg_prefix(self, agent_heptapod):
        result = agent_heptapod.call(
            "branches_all",
            project_id=TestHeptapodBranchConvention.project_id,
        )
        # On hg projects, the default branch is exposed as `branch/default`
        # (not `main` or `master`).
        names = {b["name"] for b in result["branches"]}
        assert any(n.startswith("branch/") for n in names), (
            f"Expected at least one `branch/...` entry, got: {names}"
        )
        assert result["categories"]["hg_named"] >= 1

    def test_20_get_file_with_hg_ref(self, agent_heptapod):
        # Get README.md at the hg default branch — ref passes through verbatim.
        result = agent_heptapod.call(
            "repository_files_show",
            project_id=TestHeptapodBranchConvention.project_id,
            file_path="README.md",
            ref="branch/default",
        )
        assert result.get("file_path") == "README.md"

    def test_30_create_mr_without_branch_prefix_rejected(self, agent_heptapod):
        """Pre-flight guard fires on mismatched target_branch."""
        with pytest.raises(ValueError, match="target_branch to start with 'branch/'"):
            agent_heptapod.call(
                "merge_requests_create",
                project_id=TestHeptapodBranchConvention.project_id,
                source_branch="topic/default/foo",
                target_branch="default",  # missing `branch/` prefix
                title="bad",
            )

    def test_99_cleanup(self, agent_heptapod):
        agent_heptapod.call(
            "projects_remove",
            project_id=TestHeptapodBranchConvention.project_id,
            permanent=True,
        )
