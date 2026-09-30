#!/usr/bin/env bash
# Start the Trading Desk API + dashboard.
set -euo pipefail
cd "$(dirname "$0")"
exec .venv/bin/python -m uvicorn app.main:app --host "${TD_HOST:-127.0.0.1}" --port "${TD_PORT:-8770}" "$@"
