#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
exec .venv/bin/python -m uvicorn chess_coach.api:app --host 127.0.0.1 --port 8787 --reload
