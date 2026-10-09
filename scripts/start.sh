#!/usr/bin/env bash
set -euo pipefail

sentinel_script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
sentinel_project_dir="$(cd -- "${sentinel_script_dir}/.." && pwd)"
cd -- "$sentinel_project_dir"

if ! command -v uv >/dev/null 2>&1; then
  echo '需要 uv。也可先安装 requirements.txt，再用 python -m uvicorn backend.app.main:app 启动。' >&2
  exit 1
fi

# Settings loads .env without overwriting existing environment variables.
# Defaults: simulated data, persistent data/sentinel.sqlite3, actuation locked.
exec uv run uvicorn backend.app.main:app \
  --host "${SENTINEL_HOST:-127.0.0.1}" \
  --port "${SENTINEL_PORT:-8000}"
