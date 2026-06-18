#!/usr/bin/env bash
# One command to run Accounts Pilot in a GitHub Codespace and WATCH the browser.
#  - starts a virtual display (Xvfb) so the headed Chromium has somewhere to draw
#  - exposes that display over noVNC (port 6080) so you watch it in a browser tab
#  - starts the app (port 8000)
# Both ports are forwarded by Codespaces as PRIVATE (only your GitHub account opens them).
set -euo pipefail
cd "$(dirname "$0")/.."

export DISPLAY="${DISPLAY:-:99}"
export AP_BROWSER_NO_SANDBOX=true

# self-heal: if the deps aren't in THIS python (e.g. the build step didn't finish),
# install them now so `python -m uvicorn` works.
if ! python -m uvicorn --version >/dev/null 2>&1; then
  echo "▸ installing Python deps (first run / build didn't finish) …"
  python -m pip install -r requirements.txt
  python -m playwright install chromium >/dev/null 2>&1 || true
fi

# clean any previous run (idempotent)
pkill -f "Xvfb $DISPLAY" 2>/dev/null || true
pkill -f "x11vnc"        2>/dev/null || true
pkill -f "websockify"    2>/dev/null || true
sleep 0.5

echo "▸ starting virtual display $DISPLAY …"
Xvfb "$DISPLAY" -screen 0 1440x900x24 -nolisten tcp >/tmp/xvfb.log 2>&1 &
sleep 1
fluxbox >/tmp/fluxbox.log 2>&1 &

echo "▸ exposing it over noVNC (port 6080) …"
x11vnc -display "$DISPLAY" -rfbport 5900 -forever -shared -nopw -quiet -bg >/tmp/x11vnc.log 2>&1
# novnc web assets live in /usr/share/novnc on the Playwright/Ubuntu image
NOVNC_DIR=/usr/share/novnc
[ -d "$NOVNC_DIR" ] || NOVNC_DIR=/usr/share/webapps/novnc
websockify --web="$NOVNC_DIR" 6080 localhost:5900 >/tmp/novnc.log 2>&1 &
sleep 1

echo ""
echo "  ✅ Watch the browser → open the forwarded port 6080, then visit  /vnc.html?autoconnect=1&resize=remote"
echo "  ✅ App UI            → open the forwarded port 8000"
echo "  (both are PRIVATE — only your GitHub account can open them)"
echo ""

# the app. Headed Chromium will appear on $DISPLAY and stream through noVNC.
exec python -m uvicorn accounts_pilot.web.app:app --host 0.0.0.0 --port 8000
