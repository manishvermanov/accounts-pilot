# Base = official Playwright image: Python + Chromium + all browser system deps preinstalled.
# Pinned to the playwright version in requirements (1.60) so the bundled browser matches.
FROM mcr.microsoft.com/playwright/python:v1.60.0-noble

# Extras so a HEADED Chromium can run on a virtual display and be watched in the browser:
#   xvfb      → virtual X display (Chromium has somewhere to draw)
#   fluxbox   → minimal window manager
#   x11vnc    → expose that display over VNC
#   novnc     → web (browser) front-end for VNC
#   websockify→ bridges noVNC ↔ VNC
RUN apt-get update && apt-get install -y --no-install-recommends \
        xvfb x11vnc fluxbox novnc websockify net-tools procps \
    && rm -rf /var/lib/apt/lists/*

ENV DISPLAY=:99 \
    AP_BROWSER_NO_SANDBOX=true
