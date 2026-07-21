#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
export PYTHONPATH="${ROOT}/tools/mcp_rigol_scope${PYTHONPATH:+:$PYTHONPATH}"
exec python3 "${ROOT}/tools/mcp_rigol_scope/server.py"
