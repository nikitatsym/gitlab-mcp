"""Integration tests against a live GitLab CE container.

Covers the core agent workflow: connection, project CRUD, branches, files,
commits, merge requests, issues, notes, pipelines. Intended to run in CI.

Boot time: ~3-5 minutes on first run (GitLab CE is heavy). Subsequent runs
reuse the container through session-scoped fixtures.
"""

from __future__ import annotations

import base64
import hashlib
import io
import time
import uuid
import zipfile

import pytest

from gitlab_mcp.client import get_client

pytestmark = pytest.mark.integration

# Unique suffix per test session — prevents project-name collisions when the
# GitLab container is reused across runs (deletion-scheduled projects keep
# their names reserved).
_RUN_TAG = uuid.uuid4().hex[:8]
_PROJECT_NAME = f"integration-test-{_RUN_TAG}"
_FORK_PATH = f"integration-test-fork-{_RUN_TAG}"
_PNG_BASE64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNg6Pj/HwAEmgKHIN7SxQAAAABJRU5ErkJggg=="


def _wait_for_pipeline(agent, project_id: int, pipeline_id: int, timeout: int = 60):
    """Poll a pipeline until it leaves pending/created state."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        p = agent.call("pipelines_show", project_id=project_id, pipeline_id=pipeline_id)
        if p.get("status") not in ("pending", "created", "preparing"):
            return p
        time.sleep(2)
    return p


class TestAgentWorkflow:
    """One sequential scenario that exercises the core tool surface."""

    project_id: int = 0
    branch_name: str = "feature/integration"
    file_path: str = "README.md"
    mr_iid: int = 0
    issue_iid: int = 0

    def test_01_version(self, agent_gitlab):
        result = agent_gitlab.call("gitlab_version")
        assert "mcp" in result
        service = result["service"]
        assert service["backend"] == "gitlab"
        assert service["enterprise"] is False
        assert service["vcs_types"] == ["git"]

    def test_02_current_user(self, agent_gitlab):
        result = agent_gitlab.call("users_show_current_user")
        assert result["username"] == "root"
        assert result.get("is_admin") is True

    def test_10_create_project(self, agent_gitlab):
        result = agent_gitlab.call(
            "projects_create",
            name=_PROJECT_NAME,
            initialize_with_readme=True,
            visibility="private",
        )
        assert result["name"] == _PROJECT_NAME
        assert result["default_branch"] == "main"
        TestAgentWorkflow.project_id = result["id"]

    def test_11_list_projects_slim(self, agent_gitlab):
        result = agent_gitlab.call("projects_all", brief=True)
        assert isinstance(result, list)
        assert any(p["id"] == TestAgentWorkflow.project_id for p in result)
        # Slim entries should not include bulky fields like `container_registry_enabled`.
        first = result[0]
        assert "container_registry_enabled" not in first
        assert "id" in first and "path_with_namespace" in first

    def test_12_list_branches_categorized(self, agent_gitlab):
        result = agent_gitlab.call(
            "branches_all",
            project_id=TestAgentWorkflow.project_id,
        )
        assert "branches" in result
        assert "categories" in result
        # All branches on a GitLab-native project are git-typed.
        assert result["categories"]["hg_named"] == 0
        assert result["categories"]["hg_topic"] == 0
        assert result["categories"]["git"] >= 1

    def test_20_create_branch(self, agent_gitlab):
        result = agent_gitlab.call(
            "branches_create",
            project_id=TestAgentWorkflow.project_id,
            branch=TestAgentWorkflow.branch_name,
            ref="main",
        )
        # Verify via the single-branch GET (not affected by GitLab's branch-list cache).
        assert result.get("name") == TestAgentWorkflow.branch_name

    def test_21_get_file_on_new_branch(self, agent_gitlab):
        result = agent_gitlab.call(
            "repository_files_show",
            project_id=TestAgentWorkflow.project_id,
            file_path=TestAgentWorkflow.file_path,
            ref=TestAgentWorkflow.branch_name,
        )
        assert result["file_path"] == TestAgentWorkflow.file_path
        content = base64.b64decode(result["content"]).decode()
        assert "integration-test" in content.lower() or "readme" in content.lower()

    def test_22_edit_file(self, agent_gitlab):
        result = agent_gitlab.call(
            "repository_files_edit",
            project_id=TestAgentWorkflow.project_id,
            file_path=TestAgentWorkflow.file_path,
            branch=TestAgentWorkflow.branch_name,
            content="# Integration test\n\nEdited by MCP agent.\n",
            commit_message="Edit from agent",
        )
        assert result["file_path"] == TestAgentWorkflow.file_path

    def test_23_list_commits(self, agent_gitlab):
        result = agent_gitlab.call(
            "commits_all",
            project_id=TestAgentWorkflow.project_id,
            ref_name=TestAgentWorkflow.branch_name,
            brief=True,
        )
        assert isinstance(result, list)
        assert len(result) >= 2  # initial commit + our edit
        latest = result[0]
        assert latest["title"] == "Edit from agent"

    def test_30_create_merge_request(self, agent_gitlab):
        result = agent_gitlab.call(
            "merge_requests_create",
            project_id=TestAgentWorkflow.project_id,
            source_branch=TestAgentWorkflow.branch_name,
            target_branch="main",
            title="Integration MR",
        )
        TestAgentWorkflow.mr_iid = result["iid"]
        assert result["title"] == "Integration MR"
        assert result["source_branch"] == TestAgentWorkflow.branch_name
        assert result["target_branch"] == "main"

    def test_31_create_mr_rejects_equal_branches(self, agent_gitlab):
        with pytest.raises(ValueError, match="must differ"):
            agent_gitlab.call(
                "merge_requests_create",
                project_id=TestAgentWorkflow.project_id,
                source_branch="main",
                target_branch="main",
                title="bad",
            )

    def test_32_list_mrs_slim(self, agent_gitlab):
        result = agent_gitlab.call(
            "merge_requests_all",
            project_id=TestAgentWorkflow.project_id,
            brief=True,
        )
        assert isinstance(result, list)
        assert any(m["iid"] == TestAgentWorkflow.mr_iid for m in result)
        # Slim entries shouldn't include full description or rebase markers.
        first = next(m for m in result if m["iid"] == TestAgentWorkflow.mr_iid)
        assert "description" not in first

    def test_40_create_issue(self, agent_gitlab):
        result = agent_gitlab.call(
            "issues_create",
            project_id=TestAgentWorkflow.project_id,
            title="Integration issue",
            description="Filed by the MCP agent",
            labels="bug,automated",
        )
        TestAgentWorkflow.issue_iid = result["iid"]
        assert result["title"] == "Integration issue"

    def test_41_list_issues_slim(self, agent_gitlab):
        result = agent_gitlab.call(
            "issues_all",
            project_id=TestAgentWorkflow.project_id,
            brief=True,
        )
        ours = [i for i in result if i["iid"] == TestAgentWorkflow.issue_iid]
        assert len(ours) == 1
        assert "bug" in ours[0]["labels"]

    def test_50_fork_on_git_is_allowed(self, agent_gitlab):
        # Fork guard should only fire for hg projects — on plain GitLab it's a no-op.
        # Forking to a new path succeeds (or returns the existing fork).
        # Use the admin 'root' account so it's always allowed.
        try:
            agent_gitlab.call(
                "projects_fork",
                project_id=TestAgentWorkflow.project_id,
                path=_FORK_PATH,
                name=_FORK_PATH,
            )
        except Exception as e:
            # Some admins get 409 if fork exists; not a guard failure.
            if "409" not in str(e) and "already" not in str(e).lower():
                raise

    def test_60_branch_lookup_after_work(self, agent_gitlab):
        # Use the single-branch GET endpoint instead of listing — GitLab's
        # /repository/branches list response is cached and may serve a stale
        # view for several minutes after a write. The branch_show endpoint
        # bypasses that cache.
        result = agent_gitlab.call(
            "branches_show",
            project_id=TestAgentWorkflow.project_id,
            branch_name=TestAgentWorkflow.branch_name,
        )
        assert result["name"] == TestAgentWorkflow.branch_name
        # Should have the edit commit on top.
        assert result["commit"]["title"] == "Edit from agent"

    def test_70_close_issue(self, agent_gitlab):
        result = agent_gitlab.call(
            "issues_edit",
            project_id=TestAgentWorkflow.project_id,
            issue_iid=TestAgentWorkflow.issue_iid,
            state_event="close",
        )
        assert result["state"] == "closed"

    def test_71_delete_mr(self, agent_gitlab):
        # We don't DELETE the MR (admin-only destructive op); instead close it.
        result = agent_gitlab.call(
            "merge_requests_edit",
            project_id=TestAgentWorkflow.project_id,
            mergerequest_iid=TestAgentWorkflow.mr_iid,
            state_event="close",
        )
        assert result["state"] == "closed"

    def test_80_describe_instance_exposes_backend(self, agent_gitlab):
        """The LLM should be able to query the backend type via gitlab_version."""
        result = agent_gitlab.call("gitlab_version")
        assert result["service"]["backend"] == "gitlab"

    def test_81_upload_images_and_secure_file(self, agent_gitlab, tmp_path):
        project_id = self.project_id
        image = base64.b64decode(_PNG_BASE64)
        local_file = tmp_path / "local.png"
        local_file.write_bytes(image)
        for source in (
            {"file_path": str(local_file)},
            {"filename": "remote.png", "content_base64": _PNG_BASE64},
        ):
            uploaded = agent_gitlab.call("projects_upload_for_reference", project_id=project_id, **source)
            downloaded = get_client()._request("GET", f"/projects/{project_id}/uploads/{uploaded['id']}")
            assert downloaded.content == image

        agent_gitlab.call("project_wikis_create", project_id=project_id, title="home", content="Upload fixture")
        wiki = agent_gitlab.call(
            "project_wikis_upload_attachment", project_id=project_id,
            filename="wiki.png", content_base64=_PNG_BASE64,
        )
        agent_gitlab.call(
            "project_wikis_edit", project_id=project_id, slug="home", content=wiki["link"]["markdown"],
        )
        page = agent_gitlab.call("project_wikis_show", project_id=project_id, slug="home")
        assert wiki["file_path"] in page["content"]

        secure = agent_gitlab.call(
            "secure_files_create", project_id=project_id, name="fixture.png",
            filename="fixture.png", content_base64=_PNG_BASE64,
        )
        assert secure["checksum"] == hashlib.sha256(image).hexdigest()
        downloaded = get_client()._request("GET", f"/projects/{project_id}/secure_files/{secure['id']}/download")
        assert downloaded.content == image

        agent_gitlab.call(
            "projects_upload_avatar", project_id=project_id, filename="project.png", content_base64=_PNG_BASE64,
        )
        project = agent_gitlab.call("projects_show", project_id=project_id)
        assert project["avatar_url"].endswith("/project.png")

    def test_82_publish_pypi_distribution(self, agent_gitlab):
        package = io.BytesIO()
        with zipfile.ZipFile(package, "w") as archive:
            archive.writestr("upload_fixture/__init__.py", "")
            archive.writestr("upload_fixture-1.0.dist-info/METADATA", "Metadata-Version: 2.1\nName: upload-fixture\nVersion: 1.0\n")
            archive.writestr("upload_fixture-1.0.dist-info/WHEEL", "Wheel-Version: 1.0\nGenerator: fixture\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
        content = package.getvalue()
        filename = "upload_fixture-1.0-py3-none-any.whl"
        agent_gitlab.call(
            "py_pi_upload_package_file", project_id=self.project_id, name="upload-fixture", version="1.0",
            filename=filename, content_base64=base64.b64encode(content).decode("ascii"), requires_python=">=3.10",
        )
        digest = hashlib.sha256(content).hexdigest()
        downloaded = get_client()._request(
            "GET", f"/projects/{self.project_id}/packages/pypi/files/{digest}/{filename}",
        )
        assert downloaded.content == content

    def test_83_create_and_edit_avatars(self, agent_gitlab, tmp_path):
        tag = uuid.uuid4().hex[:8]
        local_file = tmp_path / "updated.png"
        local_file.write_bytes(base64.b64decode(_PNG_BASE64))
        cases: list[tuple[str, str, dict]] = [
            ("projects", "project_id", {"name": f"avatar-{tag}", "topics": ["upload-test"]}),
            ("groups", "group_id", {"name": f"avatar-{tag}", "path": f"avatar-{tag}"}),
            ("topics", "topic_id", {"name": f"avatar-{tag}", "title": "Avatar"}),
            ("users", "user_id", {"name": "Avatar", "username": f"avatar-{tag}",
                                  "email": f"avatar-{tag}@example.com", "password": uuid.uuid4().hex + "!aA8",
                                  "skip_confirmation": True}),
        ]
        for resource, id_key, fields in cases:
            created = agent_gitlab.call(
                f"{resource}_create", **fields,
                avatar_filename="created.png", avatar_content_base64=_PNG_BASE64,
            )
            identity = {id_key: created["id"]}
            try:
                assert created["avatar_url"].endswith("/created.png")
                edit_fields: dict[str, list[str]] = {"topics": []} if resource == "projects" else {}
                agent_gitlab.call(
                    f"{resource}_edit", **identity, **edit_fields, avatar_file_path=str(local_file),
                )
                updated = agent_gitlab.call(f"{resource}_show", **identity)
                assert updated["avatar_url"].endswith("/updated.png")
                if resource == "projects":
                    assert updated["topics"] == []
                if resource == "groups":
                    agent_gitlab.call(
                        "groups_upload_avatar", **identity, filename="standalone.png", content_base64=_PNG_BASE64,
                    )
                    group = agent_gitlab.call("groups_show", **identity)
                    assert group["avatar_url"].endswith("/standalone.png")
            finally:
                agent_gitlab.call(f"{resource}_remove", **identity)

    def test_84_upload_appearance_images(self, agent_gitlab):
        appearance = agent_gitlab.call(
            "application_appearance_edit",
            logo_filename="ci-logo.png", logo_content_base64=_PNG_BASE64,
            favicon_filename="ci-favicon.png", favicon_content_base64=_PNG_BASE64,
        )
        assert appearance["logo"].endswith("/ci-logo.png")
        assert appearance["favicon"].endswith("/ci-favicon.png")
        saved = get_client().get("/application/appearance")
        assert saved["logo"] == appearance["logo"]
        assert saved["favicon"] == appearance["favicon"]

    def test_99_delete_project(self, agent_gitlab):
        agent_gitlab.call("projects_remove", project_id=TestAgentWorkflow.project_id)


class TestHeptapodOnlyToolsGuarded:
    """hg_* operations are registered everywhere; on GitLab dispatch refuses them."""

    def test_hg_op_refused_on_gitlab(self, agent_gitlab):
        from gitlab_mcp.server import _dispatch

        result = _dispatch("HgGetConfig", "gitlab_read", {"project_id": 1})
        assert result == {"error": "HgGetConfig is Heptapod-only; this instance is gitlab"}

    def test_fork_guard_noop_on_gitlab(self, agent_gitlab):
        # The guard only fires on hg projects. A lookup should return fast
        # without the guard raising.
        from gitlab_mcp.tools import _project_is_hg

        assert _project_is_hg(1) is False  # shortcut: backend != heptapod
