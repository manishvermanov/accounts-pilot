# Accounts Pilot — Setup (clone → run)

Onboards hotels onto OTAs (Booking, Agoda, Expedia, MakeMyTrip, Airbnb) by driving a real
browser with Playwright, an LLM page-filler (Azure OpenAI) for unmapped pages, and a Metabase
MIS as the hotel-data source. FastAPI dashboard + a per-OTA live browser session.

> **For another agent / fresh machine:** run the steps in order. The two non-obvious gotchas are
> (1) you must install the Playwright **browser** separately from the pip packages, and
> (2) the app needs a `.env` (see [Environment](#3-environment)). Everything else is `pip install`.

---

## 1. Prerequisites
- **Python 3.11 – 3.12** (3.12 recommended; 3.13/3.14 may lack prebuilt wheels for some deps).
- **git**.
- ~1.5 GB free disk (Chromium download).

## 2. Install (one block)

**macOS / Linux:**
```bash
git clone https://github.com/manishvermanov/accounts-pilot.git
cd accounts-pilot
python3 -m venv .venv && source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m playwright install chromium          # the browser binary (NOT in requirements.txt)
```

**Windows (PowerShell):**
```powershell
git clone https://github.com/manishvermanov/accounts-pilot.git
cd accounts-pilot
python -m venv .venv ; .\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m playwright install chromium
```

Or just run the helper (does venv + pip + browser): `bash scripts/setup.sh`

## 3. Environment
Create a `.env` in the repo root. **Required** to do anything useful:

| Variable | What it is | Required for |
|---|---|---|
| `AZURE_OPENAI_ENDPOINT` | `https://<resource>.openai.azure.com` | Filling unmapped OTA pages |
| `AZURE_OPENAI_KEY` | Azure OpenAI API key | ″ |
| `AZURE_OPENAI_DEPLOYMENT` | your model deployment name | ″ |
| `MIS_METABASE_URL` | `https://mis.digistay.co.in` | Hotel search |
| `MIS_METABASE_API_KEY` | Metabase API key | ″ |
| `MIS_METABASE_DB_ID` | Metabase database id (e.g. `2`) | ″ |
| `MIS_EDGE_VALUE` | Cloudflare edge-bypass shared secret | ″ |

Optional: `AP_AUTH_USER` / `AP_AUTH_PASS` (cookie-login the dashboard when hosted publicly),
`BOOKING_PARTNER_EMAIL` / `BOOKING_PARTNER_PASSWORD` (pre-fill Booking login),
`HEADLESS=false`, and `AP_BROWSER_NO_SANDBOX=true` (containers only).
The full list with defaults lives in [`accounts_pilot/config.py`](accounts_pilot/config.py) — every
lowercase field there is settable via its `UPPER_CASE` env var.

> Never commit `.env` (it's gitignored).

## 4. Run
```bash
python -m uvicorn accounts_pilot.web.app:app --host 127.0.0.1 --port 8000 --reload --reload-dir accounts_pilot
```
Open **http://127.0.0.1:8000/**. (The dashboard also has a **↻ Restart server** button.)

## 5. Verify
```bash
python -m pytest -q          # expect: 125 passed
```

---

## How it avoids re-calling the LLM (page-map cache)
The first time the bot sees an OTA page it asks the LLM how to fill it, then **stores the result**
in `data/page_maps_<ota>.json` keyed by a stable, reload-proof descriptor (label / automation-id /
DOM path). Every later visit **replays that map with plain Playwright — no LLM call** (log:
`replayed learned map … no LLM`). These cache files **are committed**, so a fresh clone/deploy
starts with the already-learned maps and only calls the LLM for genuinely new pages.

- Per-OTA dedicated handlers (login, address, room-type, photos, property-type) are deterministic
  and never need the LLM at all.
- Photos: downloaded from the MIS S3 URLs into a temp cache, then EXIF-oriented → padded to
  landscape → resized into the OTA size window (needs **Pillow**, in requirements).

## Container / cloud notes
- A long-running **container or VM** (Fargate / EC2 / Fly.io / Render) — **not AWS Lambda**: the
  app holds a live browser session in memory across many requests and pauses for operator gates,
  which Lambda's 15-min stateless model can't host. See the in-app **Remote control** panel —
  it lets an operator handle login/OTP/address from the dashboard with no separate browser window.
- In a container set `AP_BROWSER_NO_SANDBOX=true` and install the apt deps Playwright lists
  (`python -m playwright install-deps chromium`).
