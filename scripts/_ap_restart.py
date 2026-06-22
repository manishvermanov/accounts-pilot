"""Detached restarter for the Accounts Pilot dev server.

Spawned (fully detached) by POST /api/restart. After a short delay — so the HTTP response
has flushed — it kills the running uvicorn server (and its --reload workers) and starts a
fresh one. Because it's detached, it outlives the server it kills, so the server ALWAYS
comes back, regardless of whether --reload is healthy.

NOTE: this script's own command line is '.../Accounts Pilot/scripts/_ap_restart.py' — it
contains 'Accounts Pilot' (a space), NOT 'accounts_pilot', so the kill filter below never
matches (and never kills) this helper.
"""
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG = ROOT / "data" / "logs" / "restart.log"


def log(msg: str) -> None:
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except Exception:
        pass


def main() -> None:
    time.sleep(1.0)                                    # let the /api/restart response flush
    py = ROOT / ".venv" / "Scripts" / "python.exe"
    py = str(py) if py.exists() else sys.executable
    log(f"[restart] killing existing server… (py={py})")

    if os.name == "nt":
        kill = (
            "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
            "Where-Object { $_.CommandLine -match 'accounts_pilot' -or $_.CommandLine -match 'spawn_main' } | "
            "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
        )
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", kill],
                       cwd=str(ROOT), stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        subprocess.run(["pkill", "-f", "uvicorn accounts_pilot"], stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    time.sleep(1.0)
    args = [py, "-m", "uvicorn", "accounts_pilot.web.app:app",
            "--host", "127.0.0.1" if os.name == "nt" else "0.0.0.0",
            "--port", "8000", "--reload", "--reload-dir", "accounts_pilot"]
    log(f"[restart] starting fresh server: {' '.join(args)}")
    if os.name == "nt":
        DETACHED = 0x00000008 | 0x00000200             # DETACHED_PROCESS | NEW_PROCESS_GROUP
        subprocess.Popen(args, cwd=str(ROOT), creationflags=DETACHED, close_fds=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        subprocess.Popen(args, cwd=str(ROOT), start_new_session=True, stdin=subprocess.DEVNULL,
                         stdout=open("/tmp/accounts-pilot.log", "ab"), stderr=subprocess.STDOUT)
    log("[restart] new server launched.")


if __name__ == "__main__":
    main()
