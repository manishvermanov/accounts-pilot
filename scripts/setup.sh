#!/usr/bin/env bash
# Accounts Pilot — one-command setup. Idempotent: safe to re-run.
#   bash scripts/setup.sh
set -euo pipefail
cd "$(dirname "$0")/.."

echo "▸ Python: $(python3 --version 2>&1 || python --version)"
PY="$(command -v python3 || command -v python)"

# 1) virtualenv
if [ ! -d .venv ]; then
  echo "▸ creating .venv"
  "$PY" -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate 2>/dev/null || source .venv/Scripts/activate

# 2) python deps
echo "▸ installing requirements"
python -m pip install --upgrade pip >/dev/null
python -m pip install -r requirements.txt

# 3) the Playwright browser binary (separate from pip)
echo "▸ installing Chromium for Playwright"
python -m playwright install chromium
# in a container you also want the OS deps (ignored if not root / not Linux):
python -m playwright install-deps chromium 2>/dev/null || true

# 4) data dir + .env reminder
mkdir -p data
if [ ! -f .env ]; then
  echo "⚠ no .env found — create one (see SETUP.md §3). The app needs AZURE_OPENAI_* and MIS_* to do real work."
fi

echo ""
echo "✅ setup done. Run it with:"
echo "   python -m uvicorn accounts_pilot.web.app:app --host 127.0.0.1 --port 8000 --reload --reload-dir accounts_pilot"
echo "   then open http://127.0.0.1:8000/"
echo "   tests:  python -m pytest -q"
