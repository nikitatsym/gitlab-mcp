"""Unit tests for prepare.py — pure data transforms, no network."""

from __future__ import annotations

import pytest

from gitlab_mcp.prepare import (
    _brief,
    _categorize_branch,
    _categorize_branches,
    _first_line,
    _ok,
    _short,
    _slim_branch,
    _slim_commit,
    _slim_file_entry,
    _slim_issue,
    _slim_job,
    _slim_mr,
    _slim_note,
    _slim_pipeline,
    _slim_project,
    _slim_tag,
    _slim_user,
    _verify_response,
)


class TestOk:
    def test_none_becomes_status_ok(self):
        assert _ok(None) == {"status": "ok"}

    def test_pass_through_dict(self):
        assert _ok({"id": 1}) == {"id": 1}

    def test_pass_through_list(self):
        assert _ok([1, 2]) == [1, 2]


class TestShort:
    def test_default_12_chars(self):
        assert _short("abcdef1234567890") == "abcdef123456"

    def test_none_passthrough(self):
        assert _short(None) is None

    def test_shorter_than_limit(self):
        assert _short("abc") == "abc"


class TestFirstLine:
    def test_multiline(self):
        assert _first_line("hello\nworld\nfoo") == "hello"

    def test_single_line(self):
        assert _first_line("just one") == "just one"

    def test_empty(self):
        assert _first_line("") == ""
        assert _first_line(None) == ""


class TestBrief:
    def test_under_cap_unchanged(self):
        assert _brief("short text", cap=100) == "short text"

    def test_over_cap_truncated(self):
        result = _brief("a" * 200, cap=50)
        assert result is not None
        assert len(result) <= 51  # 50 + ellipsis (1 char)
        assert result.endswith("…")

    def test_none_passthrough(self):
        assert _brief(None) is None

    def test_zero_cap_no_truncation(self):
        long = "a" * 500
        assert _brief(long, cap=0) == long


class TestSlimProject:
    def test_extracts_expected_fields(self):
        p = {
            "id": 1,
            "name": "test",
            "path_with_namespace": "group/test",
            "description": "A project",
            "default_branch": "main",
            "visibility": "private",
            "archived": False,
            "last_activity_at": "2026-04-01T00:00:00Z",
            "web_url": "https://gitlab.example.com/group/test",
            "extra_field_ignored": "gone",
        }
        result = _slim_project(p)
        assert result["id"] == 1
        assert result["path_with_namespace"] == "group/test"
        assert result["default_branch"] == "main"
        assert "extra_field_ignored" not in result

    def test_missing_fields_become_none(self):
        result = _slim_project({})
        assert result["id"] is None
        assert result["name"] is None


class TestSlimMr:
    def test_author_username_extracted(self):
        mr = {
            "iid": 42,
            "title": "Fix bug",
            "state": "opened",
            "source_branch": "feat/x",
            "target_branch": "main",
            "draft": False,
            "author": {"username": "ari", "name": "Ari", "id": 1},
            "labels": ["bug", "urgent"],
            "updated_at": "2026-04-01T00:00:00Z",
            "web_url": "https://gitlab/mr/42",
            "has_conflicts": False,
        }
        result = _slim_mr(mr)
        assert result["author"] == "ari"
        assert result["labels"] == ["bug", "urgent"]
        assert result["iid"] == 42

    def test_hg_refs_preserved_verbatim(self):
        """Heptapod branch/topic names must pass through unchanged."""
        mr = {
            "iid": 1,
            "source_branch": "topic/default/my-feature",
            "target_branch": "branch/default",
        }
        result = _slim_mr(mr)
        assert result["source_branch"] == "topic/default/my-feature"
        assert result["target_branch"] == "branch/default"


class TestSlimIssue:
    def test_assignees_are_usernames(self):
        issue = {
            "iid": 10,
            "title": "Crash",
            "state": "opened",
            "labels": ["bug"],
            "assignees": [
                {"username": "ari", "id": 1},
                {"username": "bob", "id": 2},
            ],
            "updated_at": "2026-04-01T00:00:00Z",
            "web_url": "https://gitlab/issue/10",
        }
        result = _slim_issue(issue)
        assert result["assignees"] == ["ari", "bob"]

    def test_no_assignees(self):
        result = _slim_issue({"iid": 1, "assignees": None})
        assert result["assignees"] == []


class TestSlimCommit:
    def test_parent_count_from_parent_ids(self):
        c = {
            "id": "abc123def456" * 4,
            "short_id": "abc123def456",
            "title": "Fix stuff\n\nLong body here",
            "author_name": "Ari",
            "committed_date": "2026-04-01T00:00:00Z",
            "parent_ids": ["p1", "p2"],
        }
        result = _slim_commit(c)
        assert result["title"] == "Fix stuff"  # first line only
        assert result["parent_count"] == 2
        assert result["short_id"] == "abc123def456"

    def test_missing_short_id_derives_from_full(self):
        c = {"id": "0123456789abcdef" * 3, "parent_ids": []}
        result = _slim_commit(c)
        assert result["short_id"] == "0123456789ab"
        assert result["parent_count"] == 0


class TestSlimBranch:
    def test_hg_topic_name_preserved(self):
        b = {
            "name": "topic/default/my-feature",
            "merged": False,
            "protected": False,
            "default": False,
            "commit": {"id": "abc123def456xxxxxx"},
        }
        result = _slim_branch(b)
        assert result["name"] == "topic/default/my-feature"
        assert result["commit_id"] == "abc123def456"

    def test_git_branch(self):
        b = {
            "name": "main",
            "merged": False,
            "protected": True,
            "default": True,
            "commit": {"id": "1234567890abcdef"},
        }
        result = _slim_branch(b)
        assert result["name"] == "main"
        assert result["default"] is True


class TestSlimTag:
    def test_target_from_top_level(self):
        t = {"name": "v1.0", "target": "abcdef1234567890", "message": "Release"}
        assert _slim_tag(t)["target"] == "abcdef123456"

    def test_target_falls_back_to_commit(self):
        t = {"name": "v1.0", "commit": {"id": "abcdef1234567890"}, "message": None}
        assert _slim_tag(t)["target"] == "abcdef123456"


class TestSlimPipelineJobUser:
    def test_slim_pipeline(self):
        p = {
            "id": 1,
            "status": "success",
            "ref": "main",
            "sha": "1234567890abcdef",
            "source": "push",
            "created_at": "2026-04-01",
            "updated_at": "2026-04-01",
            "web_url": "https://gitlab/p/1",
        }
        result = _slim_pipeline(p)
        assert result["sha"] == "1234567890ab"

    def test_slim_job_with_runner(self):
        j = {
            "id": 1,
            "status": "success",
            "stage": "test",
            "name": "unit",
            "ref": "main",
            "created_at": "2026-04-01",
            "finished_at": "2026-04-01",
            "duration": 42.5,
            "runner": {"description": "my-runner", "id": 5},
        }
        assert _slim_job(j)["runner"] == "my-runner"

    def test_slim_user(self):
        u = {
            "id": 1,
            "username": "ari",
            "name": "Ari",
            "state": "active",
            "web_url": "https://gitlab/ari",
            "email": "ignored@example.com",
        }
        result = _slim_user(u)
        assert "email" not in result
        assert result["username"] == "ari"


class TestSlimNote:
    def test_long_body_truncated(self):
        n = {
            "id": 1,
            "body": "x" * 500,
            "author": {"username": "ari"},
            "created_at": "2026-04-01",
            "system": False,
        }
        result = _slim_note(n)
        assert result["body"] is not None
        assert len(result["body"]) <= 201  # brief cap (200) + ellipsis

    def test_author_is_username(self):
        n = {"id": 1, "body": "hi", "author": {"username": "ari"}}
        assert _slim_note(n)["author"] == "ari"


class TestSlimFileEntry:
    def test_basic_tree_entry(self):
        f = {
            "id": "abc",
            "name": "README.md",
            "type": "blob",
            "path": "docs/README.md",
            "mode": "100644",
        }
        assert _slim_file_entry(f) == f


class TestCategorizeBranch:
    def test_git(self):
        assert _categorize_branch("main") == "git"
        assert _categorize_branch("feature/x") == "git"
        assert _categorize_branch("release-1.0") == "git"

    def test_hg_named(self):
        assert _categorize_branch("branch/default") == "hg_named"
        assert _categorize_branch("branch/stable") == "hg_named"

    def test_hg_topic(self):
        assert _categorize_branch("topic/default/foo") == "hg_topic"
        assert _categorize_branch("topic/stable/bar-baz") == "hg_topic"


class TestCategorizeBranches:
    def test_mixed_summary(self):
        branches = [
            {"name": "branch/default"},
            {"name": "branch/stable"},
            {"name": "topic/default/foo"},
            {"name": "topic/default/bar"},
            {"name": "topic/stable/quux"},
        ]
        summary = _categorize_branches(branches)
        assert summary == {"git": 0, "hg_named": 2, "hg_topic": 3}

    def test_pure_git(self):
        branches = [{"name": "main"}, {"name": "develop"}, {"name": "feat/x"}]
        assert _categorize_branches(branches) == {"git": 3, "hg_named": 0, "hg_topic": 0}


class TestVerifyResponse:
    def test_matching_fields_ok(self):
        sent = {"title": "Hello", "description": "World"}
        received = {"id": 1, "title": "Hello", "description": "World"}
        _verify_response(sent, received)  # no raise

    def test_dropped_field_raises(self):
        sent = {"title": "Hello", "custom_field": "value"}
        received = {"id": 1, "title": "Hello"}
        with pytest.raises(ValueError, match="custom_field"):
            _verify_response(sent, received)

    def test_skip_verify_credentials(self):
        sent = {"name": "user", "password": "secret", "token": "abc"}
        received = {"id": 1, "name": "user"}  # password/token not echoed
        _verify_response(sent, received)  # no raise

    def test_none_values_ignored(self):
        sent = {"title": "Hello", "optional": None}
        received = {"id": 1, "title": "Hello"}
        _verify_response(sent, received)  # no raise — None isn't really sent

    def test_non_dict_response_ignored(self):
        _verify_response({"x": 1}, None)  # no raise
        _verify_response({"x": 1}, "text")  # no raise
        _verify_response({"x": 1}, [1, 2])  # no raise
