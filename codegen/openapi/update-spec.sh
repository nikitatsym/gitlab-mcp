#!/usr/bin/env bash
# Fetch openapi_v3.yaml from gitlab-org/gitlab at a pinned tag.
# Usage: ./update-spec.sh [tag]    (default: current pin from README.md)
set -euo pipefail

cd "$(dirname "$0")"

if [ -n "${1:-}" ]; then
  TAG="$1"
else
  TAG="$(grep -oE 'v[0-9]+\.[0-9]+\.[0-9]+-ee' README.md | head -1)"
fi

if [ -z "$TAG" ]; then
  echo "could not determine tag" >&2
  exit 1
fi

URL="https://gitlab.com/gitlab-org/gitlab/-/raw/${TAG}/doc/api/openapi/openapi_v3.yaml"
echo "fetching ${URL}"
curl -fsSL -o openapi_v3.yaml "$URL"
echo "wrote $(pwd)/openapi_v3.yaml ($(wc -c < openapi_v3.yaml) bytes)"

# Update pin in README if a tag arg was given
if [ -n "${1:-}" ]; then
  sed -i "s/v[0-9]\+\.[0-9]\+\.[0-9]\+-ee/${TAG}/" README.md
  echo "pinned ${TAG} in README.md"
fi
