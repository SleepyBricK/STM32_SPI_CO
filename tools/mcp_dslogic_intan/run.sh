#!/usr/bin/env bash
# Launcher для Cursor MCP (stdio). cwd = корень репозитория.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
export PYTHONPATH="${ROOT}/tools:${ROOT}/tools/mcp_dslogic_intan${PYTHONPATH:+:$PYTHONPATH}"
exec python3 "${ROOT}/tools/mcp_dslogic_intan/server.py"
