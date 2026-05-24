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
    # Start empty. Populate as LLM confusion surfaces in real use.
    # Example shape:
    # "issues_create": {
    #     "labels": "Comma-separated label names or list of names.",
    #     "due_date": "YYYY-MM-DD.",
    # },
}
