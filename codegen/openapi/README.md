# GitLab OpenAPI v3 spec (vendored)

`openapi_v3.yaml` is fetched from `gitlab-org/gitlab` at a pinned tag and
consumed by `codegen/generate.ts` as the source of truth for request body
schemas and query/path parameter types.

## Current pin

`v18.11.5-ee`

## Update

```
./update-spec.sh v18.x.y-ee
```

Re-run `npx tsx generate.ts` afterwards and review the `_generated.py` diff
before committing.
