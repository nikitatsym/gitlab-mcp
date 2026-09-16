"""Per-parameter descriptions wrapped onto generated functions post-codegen.

Codegen never touches this file. Add entries for params whose name+type
isn't self-documenting (formats, conditional rules, embedded-tag requirements).

Shape: PARAM_ANNOTATIONS[fn_name][param_name] = description (str).

Descriptions surface in:
  - `gitlab_read(operation="help", params={"category": "X"})` — indented
    bullet under each op signature.
  - `model_json_schema()` of the per-op Pydantic params model.

Edit and reload — no codegen re-run needed.
"""

_UPLOAD_SOURCE = {
    "file_path": "Path on the MCP server; supply this OR filename and content_base64, never both.",
    "filename": "File name without directories; required with content_base64 instead of file_path.",
    "content_base64": "Standard base64 file bytes without a data-URL prefix; requires filename.",
}

PARAM_ANNOTATIONS: dict[str, dict[str, str]] = {
    name: dict(_UPLOAD_SOURCE)
    for name in (
        "projects_upload_for_reference", "projects_upload_avatar", "groups_upload_avatar",
        "project_wikis_upload_attachment", "group_wikis_upload_attachment",
        "issues_upload_metric_image", "secure_files_create",
        "project_import_exports_import", "group_import_exports_import",
        "nu_get_upload_package_file", "nu_get_upload_symbol_package",
        "npm_upload_package_file", "py_pi_upload_package_file",
        "ruby_gems_upload_gem_file", "project_terraform_state_create_version",
    )
}

PARAM_ANNOTATIONS.update({
    name: {
        "avatar_file_path": "Avatar path on the MCP server; exclusive with avatar_filename/avatar_content_base64.",
        "avatar_filename": "Avatar file name without directories; required with avatar_content_base64.",
        "avatar_content_base64": "Standard base64 avatar bytes; requires avatar_filename. Omit all avatar_* fields to leave the avatar unchanged.",
    }
    for name in (
        "projects_create", "projects_edit", "groups_create", "groups_edit",
        "topics_create", "topics_edit", "users_create", "users_edit",
    )
})

PARAM_ANNOTATIONS["application_appearance_edit"] = {
    key: description
    for part in ("logo", "pwa_icon", "header_logo", "favicon")
    for key, description in {
        f"{part}_file_path": f"Image path on the MCP server; exclusive with {part}_filename/{part}_content_base64.",
        f"{part}_filename": f"File name without directories; required with {part}_content_base64.",
        f"{part}_content_base64": f"Standard base64 image bytes; requires {part}_filename.",
    }.items()
}
