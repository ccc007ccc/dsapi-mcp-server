#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="dsapi-mcp-server"
REPO_URL="https://github.com/ccc007ccc/DirectScreenAPI.git"

mkdir -p "${PROJECT_ROOT}"
cd "${PROJECT_ROOT}"

if [ ! -d ".git" ]; then
  git init
fi

if [ ! -d "DirectScreenAPI" ]; then
  git clone "${REPO_URL}" DirectScreenAPI
fi

cat > requirements.txt <<'REQ'
mcp
Pillow
pyyaml
REQ

echo "bootstrap_done project=$(pwd)"
