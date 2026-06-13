"""Integration tests against a live Heptapod container.

Heptapod boots slow (5-8 minutes). Run via `npm run test:integration:heptapod`
which bootstraps the container and points tests/.env at it. The fixture
HARD-FAILS if the env points elsewhere (or nothing) — silence is a bug.
"""

from __future__ import annotations

import pytest


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
            auto_publish="non-topic",
        )
        result = agent_heptapod.call(
            "hg_get_config",
            project_id=TestHeptapodHgConfig.project_id,
        )
        assert result["allow_bookmarks"] is True
        assert result["auto_publish"] == "non-topic"

    def test_21_set_config_partial_erases_unsent(self, agent_heptapod):
        """Document PUT-not-PATCH: unsent fields reset to defaults."""
        agent_heptapod.call(
            "hg_set_config",
            project_id=TestHeptapodHgConfig.project_id,
            inherit=False,
            # Deliberately omit allow_bookmarks — should reset to default.
        )
        result = agent_heptapod.call(
            "hg_get_config",
            project_id=TestHeptapodHgConfig.project_id,
        )
        # The previously-set allow_bookmarks=true has been erased back to false.
        assert result["allow_bookmarks"] is False

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
        )
