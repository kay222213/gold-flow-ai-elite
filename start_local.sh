#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
if ! command -v python3 >/dev/null 2>&1; then echo "Python 3 is required." >&2; exit 1; fi
cd "$ROOT/backend"
if [ ! -d .venv ]; then python3 -m venv .venv; fi
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
if [ ! -f .env ]; then cp .env.example .env; echo "Created backend/.env. Add provider keys there, then run this script again."; exit 0; fi
python -m uvicorn app:app --host 127.0.0.1 --port 8000
