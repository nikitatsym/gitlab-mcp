"""MCP request-contract checks for upload-related documented spec gaps.

These checks distinguish endpoints whose Workhorse routes consume raw request
bytes from endpoints that require a named multipart file part. They exercise
the same validated meta-tool dispatch path as an MCP caller and capture the
outgoing request rather than inspecting generated source.
"""

from __future__ import annotations

import base64
import hashlib
import json
from email import policy
from email.parser import BytesParser

import httpx
import pytest

import gitlab_mcp.client as client_mod
from gitlab_mcp import server
from gitlab_mcp.backend import InstanceInfo
from gitlab_mcp.client import GitLabClient, _reset_client
from gitlab_mcp.config import _reset_settings, set_allow_public


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    monkeypatch.setenv("GITLAB_URL", "https://gitlab.example.com")
    monkeypatch.setenv("GITLAB_TOKEN", "test-token")
    set_allow_public(False)
    _reset_settings()
    _reset_client()
    yield
    _reset_settings()
    _reset_client()


def _capture_write_request(operation: str, params: dict) -> httpx.Request:
    """Dispatch one public write operation and return its sole HTTP request."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(201, json={})

    client = GitLabClient(transport=httpx.MockTransport(handler))
    client.instance = InstanceInfo(
        backend="gitlab",
        version="18.6.0",
        enterprise=False,
        vcs_types_supported={"git"},
        url="https://gitlab.example.com",
    )

    client_mod._client = client

    server._register_tools()
    result = server._dispatch(operation, "gitlab_write", params)
    assert not (isinstance(result, dict) and "error" in result), result

    assert len(captured) == 1
    return captured[0]


def _source_params(tmp_path, source, content, filename="payload.bin", prefix=""):
    if source == "file_path":
        local_file = tmp_path / filename
        local_file.write_bytes(content)
        return {f"{prefix}file_path": str(local_file)}
    return {
        f"{prefix}filename": filename,
        f"{prefix}content_base64": base64.b64encode(content).decode("ascii"),
    }


def _multipart_parts(request):
    message = BytesParser(policy=policy.default).parsebytes(
        b"Content-Type: " + request.headers["content-type"].encode() + b"\r\n\r\n" + request.content
    )
    return [
        (part.get_param("name", header="content-disposition"), part.get_filename(), part.get_payload(decode=True))
        for part in message.iter_parts()
    ]


@pytest.mark.parametrize("source", ["file_path", "content_base64"])
@pytest.mark.parametrize(
    ("operation", "method", "path", "params", "part", "fields", "prefix"),
    [
        ("ProjectsUploadForReference", "POST", "/projects/team%2Fproject/uploads",
         {"project_id": "team/project", "sudo": "maintainer"}, "file", {}, ""),
        ("ProjectsUploadAvatar", "PUT", "/projects/42", {"project_id": 42}, "avatar", {}, ""),
        ("GroupsUploadAvatar", "PUT", "/groups/42", {"group_id": 42}, "avatar", {}, ""),
        ("ProjectWikisUploadAttachment", "POST", "/projects/42/wikis/attachments",
         {"project_id": 42, "branch": "docs"}, "file", {"branch": "docs"}, ""),
        ("GroupWikisUploadAttachment", "POST", "/groups/42/wikis/attachments",
         {"group_id": 42, "branch": "docs"}, "file", {"branch": "docs"}, ""),
        ("IssuesUploadMetricImage", "POST", "/projects/42/issues/7/metric_images",
         {"project_id": 42, "issue_iid": 7, "url": "https://example.com", "url_text": "Metrics"},
         "file", {"url": "https://example.com", "url_text": "Metrics"}, ""),
        ("SecureFilesCreate", "POST", "/projects/42/secure_files",
         {"project_id": 42, "name": "certificate"}, "file", {"name": "certificate"}, ""),
        ("GroupImportExportsImport", "POST", "/groups/import",
         {"name": "Imported", "path": "imported", "parent_id": 7},
         "file", {"name": "Imported", "path": "imported", "parent_id": "7"}, ""),
        ("ProjectImportExportsImport", "POST", "/projects/import",
         {"path": "imported", "namespace_id": 7, "overwrite": False,
          "override_params": {"visibility": "private", "topics": ["first", "second"]}},
         "file", {"path": "imported", "namespace_id": "7", "overwrite": "false",
                  "override_params[visibility]": "private",
                  "override_params[topics][]": ["first", "second"]}, ""),
        ("NuGetUploadPackageFile", "PUT", "/projects/42/packages/nuget/",
         {"project_id": 42}, "package", {}, ""),
        ("NuGetUploadSymbolPackage", "PUT", "/projects/42/packages/nuget/symbolpackage",
         {"project_id": 42}, "package", {}, ""),
        ("PyPiUploadPackageFile", "POST", "/projects/42/packages/pypi",
         {"project_id": 42, "name": "example", "version": "1.0", "requires_python": ">=3.10"},
         "content", {"name": "example", "version": "1.0", "requires_python": ">=3.10"}, ""),
        ("ProjectsCreate", "POST", "/projects", {"name": "Example", "initialize_with_readme": True},
         "avatar", {"name": "Example", "initialize_with_readme": "true", "visibility": "private"}, "avatar_"),
        ("ProjectsEdit", "PUT", "/projects/42",
         {"project_id": 42, "description": "New description", "topics": []},
         "avatar", {"description": "New description", "topics": ""}, "avatar_"),
        ("GroupsCreate", "POST", "/groups", {"name": "Example", "path": "example"},
         "avatar", {"name": "Example", "path": "example", "visibility": "private"}, "avatar_"),
        ("GroupsEdit", "PUT", "/groups/42", {"group_id": 42, "description": "New description"},
         "avatar", {"description": "New description"}, "avatar_"),
        ("TopicsCreate", "POST", "/topics", {"name": "example", "title": "Example"},
         "avatar", {"name": "example", "title": "Example"}, "avatar_"),
        ("TopicsEdit", "PUT", "/topics/42", {"topic_id": 42, "title": "New title"},
         "avatar", {"title": "New title"}, "avatar_"),
        ("UsersCreate", "POST", "/users", {"email": "test@example.com", "name": "Test", "username": "test"},
         "avatar", {"email": "test@example.com", "name": "Test", "username": "test"}, "avatar_"),
        ("UsersEdit", "PUT", "/users/42", {"user_id": 42, "name": "New name"},
         "avatar", {"name": "New name"}, "avatar_"),
    ],
)
def test_uploads_send_exact_multipart_contract(tmp_path, source, operation, method, path, params, part, fields, prefix):
    content = b"\x89PNG\r\n\x1a\n\x00\xffbinary-data"
    request = _capture_write_request(
        operation, {**params, **_source_params(tmp_path, source, content, prefix=prefix)},
    )

    assert request.method == method
    assert request.url.raw_path == f"/api/v4{path}".encode()
    assert request.headers["content-type"].startswith("multipart/form-data")
    if "sudo" in params:
        assert request.headers["sudo"] == params["sudo"]
    parts = _multipart_parts(request)
    assert [(key, name, data) for key, name, data in parts if name is not None] == [
        (part, "payload.bin", content),
    ]
    expected_fields = dict(fields)
    if operation == "PyPiUploadPackageFile":
        expected_fields["sha256_digest"] = hashlib.sha256(content).hexdigest()
    expected_parts = sorted(
        (key, item.encode())
        for key, value in expected_fields.items()
        for item in (value if isinstance(value, list) else [value])
    )
    assert sorted((key, data) for key, name, data in parts if name is None) == expected_parts


@pytest.mark.parametrize("source", ["file_path", "content_base64"])
@pytest.mark.parametrize(
    ("operation", "method", "path", "params", "content_type", "content"),
    [
        ("NpmUploadPackageFile", "PUT", "/projects/42/packages/npm/%40acme%2Fwidget",
         {"project_id": 42, "package_name": "@acme/widget"}, "application/json",
         b'{ "name":"@acme/widget", "versions":{}, "_attachments":{} }\n'),
        ("ProjectTerraformStateCreateVersion", "POST", "/projects/42/terraform/state/production",
         {"project_id": 42, "name": "production", "sudo": "9001"}, "application/json",
         b'{ "version":4, "serial":1, "resources":[] }\n'),
        ("RubyGemsUploadGemFile", "POST", "/projects/42/packages/rubygems/api/v1/gems",
         {"project_id": 42}, "application/octet-stream", b"\x04\x08\xffgem-binary-bytes"),
    ],
)
def test_raw_uploads_preserve_bytes(tmp_path, source, operation, method, path, params, content_type, content):
    request = _capture_write_request(operation, {**params, **_source_params(tmp_path, source, content)})
    assert request.method == method
    assert request.url.raw_path == f"/api/v4{path}".encode()
    assert request.headers["content-type"] == content_type
    assert request.content == content
    if "sudo" in params:
        assert request.headers["sudo"] == params["sudo"]


@pytest.mark.parametrize(
    "source_params",
    [
        {},
        {"filename": "image.png"},
        {"content_base64": "AA=="},
        {"filename": "image.png", "content_base64": "not base64!"},
        {"filename": "../image.png", "content_base64": "AA=="},
        {"file_path": "unused.png", "filename": "image.png", "content_base64": "AA=="},
        {"file": {"filename": "image.png", "content": "old generated contract"}},
    ],
)
def test_invalid_upload_sources_fail_before_accessing_client(source_params, monkeypatch):
    def unexpected_client():
        pytest.fail("Invalid upload input must fail before accessing the client")

    monkeypatch.setattr("gitlab_mcp.tools.get_client", unexpected_client)
    server._register_tools()
    result = server._dispatch(
        "ProjectsUploadForReference", "gitlab_write", {"project_id": 42, **source_params},
    )
    assert "error" in result


@pytest.mark.parametrize("path_kind", ["missing", "directory"])
def test_upload_path_must_be_existing_regular_file(tmp_path, path_kind, monkeypatch):
    def unexpected_client():
        pytest.fail("Invalid file path must fail before accessing the client")

    monkeypatch.setattr("gitlab_mcp.tools.get_client", unexpected_client)
    server._register_tools()
    path = tmp_path / "missing" if path_kind == "missing" else tmp_path
    result = server._dispatch(
        "ProjectsUploadForReference", "gitlab_write", {"project_id": 42, "file_path": str(path)},
    )
    assert "error" in result


def test_avatar_updates_preserve_json_when_no_file_is_supplied():
    request = _capture_write_request("ProjectsEdit", {"project_id": 42, "description": "Updated"})
    assert request.headers["content-type"].startswith("application/json")
    assert json.loads(request.content) == {"description": "Updated"}


def test_incomplete_avatar_source_is_not_silently_ignored(monkeypatch):
    def unexpected_client():
        pytest.fail("Incomplete avatar must not modify the resource")

    monkeypatch.setattr("gitlab_mcp.tools.get_client", unexpected_client)
    server._register_tools()
    result = server._dispatch(
        "ProjectsEdit", "gitlab_write", {"project_id": 42, "avatar_filename": "image.png"},
    )
    assert "error" in result


def test_avatar_upload_cannot_bypass_private_visibility(monkeypatch):
    monkeypatch.setattr("gitlab_mcp.prepare.allow_public", lambda: False)
    server._register_tools()
    result = server._dispatch("ProjectsCreate", "gitlab_write", {
        "name": "example", "visibility": "public",
        "avatar_filename": "image.png", "avatar_content_base64": "AA==",
    })
    assert "error" in result


@pytest.mark.parametrize("source", ["file_path", "content_base64"])
def test_appearance_upload_preserves_multiple_named_images(tmp_path, source):
    params = {}
    expected = []
    for part in ("logo", "pwa_icon", "header_logo", "favicon"):
        content = b"\x00\xff" + part.encode()
        filename = f"{part}.png"
        params.update(_source_params(tmp_path, source, content, filename, prefix=f"{part}_"))
        expected.append((part, filename, content))
    request = _capture_write_request("ApplicationAppearanceEdit", params)
    assert request.method == "PUT"
    assert request.url.path == "/api/v4/application/appearance"
    assert sorted(_multipart_parts(request)) == sorted(expected)


def test_invalid_appearance_image_prevents_partial_upload(monkeypatch):
    def unexpected_client():
        pytest.fail("Invalid image must prevent the whole appearance update")

    monkeypatch.setattr("gitlab_mcp.tools.get_client", unexpected_client)
    server._register_tools()
    result = server._dispatch("ApplicationAppearanceEdit", "gitlab_write", {
        "logo_filename": "logo.png", "logo_content_base64": "AA==",
        "favicon_filename": "favicon.png", "favicon_content_base64": "invalid!",
    })
    assert "error" in result


@pytest.mark.parametrize(
    ("operation", "path", "params", "expected_body"),
    [
        pytest.param(
            "RepositoryFilesCreate",
            "/api/v4/projects/42/repository/files/README.md",
            {
                "project_id": 42,
                "file_path": "README.md",
                "branch": "main",
                "content": "created from JSON",
                "commit_message": "create README",
            },
            {
                "branch": "main",
                "content": "created from JSON",
                "commit_message": "create README",
            },
            id="RepositoryFiles.create",
        ),
        pytest.param(
            "RepositoryFilesEdit",
            "/api/v4/projects/42/repository/files/README.md",
            {
                "project_id": 42,
                "file_path": "README.md",
                "branch": "main",
                "content": "updated from JSON",
                "commit_message": "update README",
            },
            {
                "branch": "main",
                "content": "updated from JSON",
                "commit_message": "update README",
            },
            id="RepositoryFiles.edit",
        ),
        pytest.param(
            "CommitsCreate",
            "/api/v4/projects/42/repository/commits",
            {
                "project_id": 42,
                "branch": "main",
                "commit_message": "create README",
                "actions": [
                    {"action": "create", "file_path": "README.md", "content": "created from actions"},
                ],
            },
            {
                "branch": "main",
                "commit_message": "create README",
                "actions": [
                    {"action": "create", "file_path": "README.md", "content": "created from actions"},
                ],
            },
            id="Commits.create",
        ),
    ],
)
def test_json_content_operations_do_not_require_a_synthetic_file_field(
    operation: str,
    path: str,
    params: dict,
    expected_body: dict,
):
    """These source contracts remain functional despite OpenAPI's multipart ``file``."""
    request = _capture_write_request(operation, params)

    assert request.url.path == path, f"{operation} must dispatch to {path}"
    assert request.headers["content-type"].startswith("application/json")
    body = json.loads(request.content)
    assert body == expected_body
    assert "file" not in body, (
        f"{operation} {path} must use its JSON content contract, not synthetic multipart field 'file'"
    )
