"""MCP request-contract checks for upload-related documented spec gaps.

These checks distinguish endpoints whose Workhorse routes consume raw request
bytes from endpoints that require a named multipart file part. They exercise
the same validated meta-tool dispatch path as an MCP caller and capture the
outgoing request rather than inspecting generated source.
"""

from __future__ import annotations

import inspect
import json
import re

import httpx
import pytest

import gitlab_mcp.client as client_mod
from gitlab_mcp import server
from gitlab_mcp.backend import InstanceInfo
from gitlab_mcp.client import GitLabClient, _reset_client
from gitlab_mcp.config import _reset_settings


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    monkeypatch.setenv("GITLAB_URL", "https://gitlab.example.com")
    monkeypatch.setenv("GITLAB_TOKEN", "test-token")
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
    server._dispatch(operation, "gitlab_write", params)

    assert len(captured) == 1
    return captured[0]


def _assert_public_file_path_contract(
    tmp_path,
    operation: str,
    base_params: dict,
    params: dict,
    public_params: tuple[str, ...],
    obsolete_params: tuple[str, ...],
):
    """Assert the closed public file-path contract shared by all upload transports."""
    public_fn = server._group_ops["gitlab_write"][operation]
    signature = inspect.signature(public_fn)
    assert tuple(signature.parameters) == public_params
    assert signature.parameters["file_path"].annotation is str
    schema = public_fn._mcp_params_model.model_json_schema()
    assert set(schema["properties"]) == set(public_params)
    assert set(schema["required"]) == {
        name for name in public_params if signature.parameters[name].default is inspect.Parameter.empty
    }
    help_line = next(
        line
        for line in server._build_help("gitlab_write", search=operation).splitlines()
        if line.startswith(f"  {operation}(")
    )
    assert "file_path: str" in help_line
    for obsolete_param in obsolete_params:
        assert f"{obsolete_param}: " not in help_line
        assert f"{obsolete_param}?: " not in help_line
        with pytest.raises(ValueError) as exc_info:
            server._dispatch(
                operation,
                "gitlab_write",
                {**params, obsolete_param: "obsolete generated input"},
            )
        assert obsolete_param in str(exc_info.value)

    with pytest.raises(ValueError, match="File not found"):
        server._dispatch(
            operation,
            "gitlab_write",
            {**base_params, "file_path": str(tmp_path / "missing")},
        )
    with pytest.raises(ValueError, match="Not a file"):
        server._dispatch(
            operation,
            "gitlab_write",
            {**base_params, "file_path": str(tmp_path)},
        )


@pytest.mark.parametrize(
    (
        "operation",
        "path",
        "base_params",
        "filename",
        "content",
        "multipart_part",
        "public_params",
        "obsolete_params",
        "form_fields",
    ),
    [
        pytest.param(
            "IssuesUploadMetricImage",
            "/api/v4/projects/42/issues/7/metric_images",
            {
                "project_id": 42,
                "issue_iid": 7,
                "url": "https://metrics.example.com/chart",
                "url_text": "Latency chart",
            },
            "chart.png",
            b"chart-png-bytes",
            "file",
            ("project_id", "issue_iid", "file_path", "url", "url_text", "sudo"),
            ("metric_image", "file"),
            {
                "url": "https://metrics.example.com/chart",
                "url_text": "Latency chart",
            },
            id="Issues.uploadMetricImage",
        ),
        pytest.param(
            "NuGetUploadPackageFile",
            "/api/v4/projects/42/packages/nuget/",
            {"project_id": 42},
            "Acme.Widget.1.0.0.nupkg",
            b"nupkg-bytes",
            "package",
            ("project_id", "file_path"),
            ("package_name", "package_version", "package_file", "package"),
            {},
            id="NuGet.uploadPackageFile",
        ),
        pytest.param(
            "NuGetUploadSymbolPackage",
            "/api/v4/projects/42/packages/nuget/symbolpackage",
            {"project_id": 42},
            "Acme.Widget.1.0.0.snupkg",
            b"snupkg-bytes",
            "package",
            ("project_id", "file_path"),
            ("package_name", "package_version", "package_file", "package"),
            {},
            id="NuGet.uploadSymbolPackage",
        ),
    ],
)
def test_multipart_upload_operations_emit_exact_named_file(
    tmp_path,
    operation: str,
    path: str,
    base_params: dict,
    filename: str,
    content: bytes,
    multipart_part: str,
    public_params: tuple[str, ...],
    obsolete_params: tuple[str, ...],
    form_fields: dict[str, str],
):
    """Multipart public operations use exactly the documented multipart fields."""
    local_file = tmp_path / filename
    local_file.write_bytes(content)
    params = {**base_params, "file_path": str(local_file)}

    request = _capture_write_request(operation, params)

    assert request.url.path == path, f"{operation} must dispatch to {path}"
    content_type = request.headers["content-type"]
    assert content_type.startswith("multipart/form-data"), (
        f"{operation} {path} requires multipart field {multipart_part!r}; got {content_type!r}"
    )
    multipart_fields = re.findall(
        br'Content-Disposition: form-data; name="([^"]+)"',
        request.content,
    )
    actual_multipart_fields = {field.decode() for field in multipart_fields}
    expected_multipart_fields = {multipart_part, *form_fields}
    assert actual_multipart_fields == expected_multipart_fields, (
        f"{operation} {path} must serialize only documented multipart fields "
        f"{sorted(expected_multipart_fields)!r}, got {sorted(actual_multipart_fields)!r}"
    )
    assert len(multipart_fields) == len(expected_multipart_fields), (
        f"{operation} {path} must not serialize duplicate multipart fields"
    )
    assert f'name="{multipart_part}"'.encode() in request.content, (
        f"{operation} {path} must serialize the upload under multipart field "
        f"{multipart_part!r}"
    )
    assert f'filename="{filename}"'.encode() in request.content, (
        f"{operation} {path} must preserve the local filename {filename!r}"
    )
    assert content in request.content, (
        f"{operation} {path} must carry the local file bytes in multipart"
    )
    for name, value in form_fields.items():
        assert f'name="{name}"'.encode() in request.content
        assert value.encode() in request.content

    _assert_public_file_path_contract(
        tmp_path,
        operation,
        base_params,
        params,
        public_params,
        obsolete_params,
    )


@pytest.mark.parametrize(
    (
        "operation",
        "path",
        "base_params",
        "filename",
        "content",
        "content_type",
        "public_params",
        "obsolete_params",
        "expected_headers",
    ),
    [
        pytest.param(
            "NpmUploadPackageFile",
            "/api/v4/projects/42/packages/npm/@acme/widget",
            {"project_id": 42, "package_name": "@acme/widget"},
            "packument.json",
            b'{"_id":"@acme/widget","name":"@acme/widget","versions":{},"_attachments":{}}',
            "application/json",
            ("project_id", "package_name", "file_path"),
            ("versions", "metadata", "file"),
            {},
            id="NPM.uploadPackageFile",
        ),
        pytest.param(
            "ProjectTerraformStateCreateVersion",
            "/api/v4/projects/42/terraform/state/production",
            {"project_id": 42, "name": "production", "sudo": "9001"},
            "production.tfstate",
            b'{"version":4,"terraform_version":"1.7.0","serial":1,"lineage":"abc","resources":[]}',
            "application/json",
            ("project_id", "name", "file_path", "sudo"),
            ("file",),
            {"sudo": "9001"},
            id="ProjectTerraformState.createVersion",
        ),
        pytest.param(
            "RubyGemsUploadGemFile",
            "/api/v4/projects/42/packages/rubygems/api/v1/gems",
            {"project_id": 42},
            "acme-1.0.0.gem",
            b"\x04\x08gem-binary-bytes",
            "application/octet-stream",
            ("project_id", "file_path"),
            ("package_file", "file"),
            {},
            id="RubyGems.uploadGemFile",
        ),
    ],
)
def test_raw_file_upload_operations_emit_exact_request_body(
    tmp_path,
    operation: str,
    path: str,
    base_params: dict,
    filename: str,
    content: bytes,
    content_type: str,
    public_params: tuple[str, ...],
    obsolete_params: tuple[str, ...],
    expected_headers: dict[str, str],
):
    """Raw Workhorse routes receive the exact local file bytes, never multipart."""
    local_file = tmp_path / filename
    local_file.write_bytes(content)
    params = {**base_params, "file_path": str(local_file)}

    request = _capture_write_request(operation, params)

    assert request.url.path == path, f"{operation} must dispatch to {path}"
    assert request.headers["content-type"] == content_type
    assert not request.headers["content-type"].startswith("multipart/form-data")
    assert b"Content-Disposition: form-data" not in request.content
    assert request.content == content
    assert request.headers["private-token"] == "test-token"
    for header, value in expected_headers.items():
        assert request.headers[header] == value

    _assert_public_file_path_contract(
        tmp_path,
        operation,
        base_params,
        params,
        public_params,
        obsolete_params,
    )


def test_npm_upload_package_file_help_describes_packument_contract():
    server._register_tools()
    fn = server._group_ops["gitlab_write"]["NpmUploadPackageFile"]
    schema = fn._mcp_params_model.model_json_schema()

    assert set(schema["properties"]) == {"project_id", "package_name", "file_path"}
    assert set(schema["required"]) == {"project_id", "package_name", "file_path"}

    help_text = server._format_help_full({"NpmUploadPackageFile": fn}, "gitlab_write", "")
    assert (
        "NpmUploadPackageFile(project_id: str | int, package_name: str, file_path: str)"
    ) in help_text
    assert "packument JSON document" in help_text
    assert "not a .tgz archive" in help_text
    for obsolete_param in ("versions", "metadata", "file"):
        assert f"{obsolete_param}: " not in help_text
        assert f"{obsolete_param}?: " not in help_text


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
