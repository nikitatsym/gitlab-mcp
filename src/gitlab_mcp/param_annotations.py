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

PARAM_ANNOTATIONS: dict[str, dict[str, str]] = {
    "projects_upload_for_reference": {
        "file_path": "Path on the MCP server, not the caller's machine; exclusive with filename/content_base64.",
        "filename": "Attachment file name without directories; required with content_base64.",
        "content_base64": "Standard base64 file bytes, without a data-URL prefix; required with filename.",
    },
}
