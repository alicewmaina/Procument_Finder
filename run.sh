#!/usr/bin/env bash
# Start ProcureFind. Settings come from .env if present (see README).
set -euo pipefail
cd "$(dirname "$0")"
if [ -f .env ]; then set -a; . ./.env; set +a; fi
exec venv/bin/uvicorn app:app --host "${HOST:-127.0.0.1}" --port "${PORT:-8090}"
