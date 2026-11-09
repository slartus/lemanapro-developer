#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
version=$(python3 -c 'import json; print(json.load(open("manifest.json"))["version"])')
archive="dist/gitlab-mr-radar-${version}.zip"

mkdir -p dist
rm -f "$archive"
zip -qrX "$archive" manifest.json icons src README.md LICENSE -x '*.DS_Store'
echo "$archive"
