# Accounts Pilot — Context Handoff

Paste this whole file into the new chat. It captures the project, architecture, what's done, and the one open issue.

## What this is
A standalone autonomous Python service at `C:\Users\manis\Desktop\Accounts Pilot` that onboards a hotel property onto multiple OTAs from a **single property JSON**, each OTA running **independently** in its own browser/thread/session. The assistant builds/maintains the service; the operator runs it via a local web UI.

- Python 3.14, venv `.venv`, FastAPI + uvicorn on **http://127.0.0.1:8000**, Pydantic v2, Playwright (sync) local browser automation.
- LLM page-mapper: **Azure OpenAI GPT‑5.4** (`/openai/v1` endpoint, deployment `gpt-5.4`, a reasoning model — no temperature/max_tokens).
- Test contact (dummy): Manish Verma / testmanish8070@gmail.com (MMT real OTP runs use manishvermanov1911@gmail.com) / +918076707050.

## Run it
```cmd
cd "C:\Users\manis\Desktop\Accounts Pilot"
python -m uvicorn accounts_pilot.web.app:app --host 127.0.0.1 --port 8000
```
Kill a stuck port 8000 (PowerShell): `Get-NetTCPConnection -LocalPort 8000 -State Listen | %{ Stop-Process -Id $_.OwningProcess -Force }`
Tests: `.venv/Scripts/python.exe -m pytest -q` (currently **53 passing**). Always run tests + restart the server after code changes.

## Architecture
- **Adapters** `accounts_pilot/adapters/` — one per OTA: `booking_com.py`, `makemytrip.py`, `agoda.py`, `expedia.py`, `airbnb.py`. Registered in `adapters/__init__.py` (REGISTRY + OTA_LABELS). Each has `ota`, `display_name`, `CONNECT_URL`, `login()`, `steps()`, `run_step()` (no-op; the generic walker does the work).
- **Walker** `accounts_pilot/web/live.py` — `LiveSession` per OTA via `get_session(ota)`; per-OTA browser + thread + `storage_state_<ota>.json` + `page_maps_<ota>.json`. The big `_do_fill()` loop: scrape → LLM map → execute → advance, with per-OTA deterministic handlers gated by `self.ota`.
- **LLM mapper** `accounts_pilot/web/llm_fill.py` — `_SCRAPE_JS` (DOM-agnostic scrape: tags every interactive element with `data-ap-id`, captures label/group/stable selector), `_SYSTEM` prompt, `map_actions(fields, profile, autopilot)`.
- **Schema** `accounts_pilot/models/property_profile.py` — `PropertyProfile` (the JSON shape). **No rate-plan field exists.**
- **Web** `accounts_pilot/web/app.py` (endpoints) + `accounts_pilot/web/static/index.html` (UI).
- **Logs persist** to `data/logs/<ota>.log` (survive restarts — read these to debug).
- **Photos**: generator `scripts/gen_photos.py` (12 dummy landscape JPEGs ~480KB each in `data/photos/DEMO-HOTEL-01/`). Converters: `scripts/convert_tally_export.py`, `scripts/download_photos.py`.

## Simulate (offline dry-run — test any JSON, no login)
A **Simulate** button sits next to the sidebar Search bar (a global action, not per-channel), so the operator can paste their OWN property JSON and dry-run it without a live OTA session. It runs **globally across all channels** in one pass (`POST /api/simulate?ota=all` → `simulate_all`): validates + reports coverage once, then dry-runs the offline handlers for every supported channel. The modal's **Use for live Fill →** button loads the pasted JSON as the active property so it can then drive a real Connect + Fill:
- Engine: `accounts_pilot/web/simulate.py` → `simulate(ota, profile)`. Validates against `PropertyProfile`, reports field coverage + photo pre-flight (OTA-agnostic), and for supported OTAs drives the deterministic handlers against synthetic OTA-shaped pages in a **headless Chromium**, returning exactly what got filled. It uses a **throwaway `LiveSession` with a silenced `_say`**, so a connected live session is never touched and `data/logs/<ota>.log` stays clean.
- Endpoints: `GET /api/simulate/otas` (which OTAs have an offline flow — currently **expedia**), `POST /api/simulate?ota=<ota>` body `{profile}`.
- Fixtures are profile-driven (the page's option lists always include the profile's own country/state/room/bed values), answering "given a page that offers your value, does the handler pick it?". Add a new OTA by adding an entry to `_FLOWS` in `simulate.py`.
- Tests: `tests/test_simulate.py` (validation, coverage, unsupported-OTA note, full headless Expedia fill, and a guard that the live singleton is never mutated). It's the UI sibling of `scripts/expedia_smoke.py`.

## Generic capabilities (work for every OTA)
- DOM-agnostic scraper + `data-ap-id` execution + `stable` (id/name/`data-testid`/`data-test-id`) selectors for the cache.
- Custom dropdowns via `_open_and_pick` (snapshot→open→diff newly-appeared options→pick; types into a search box if present).
- `_set_counter` (+/- steppers), card pickers, checkboxes/radios.
- **Autopilot** (`"autopilot": true` in JSON OR the UI toggle): fills every field, invents dummy for gaps. Off = fill only what's given (does NOT invent). Autopilot runs do NOT persist maps.
- **Guardrails**: page-**fingerprint** progress detection (re-filling same values ≠ progress), 5× auto-retry on slow pages, hard cap (sig_streak>14), per-OTA **Stop** button (`/api/fill/stop`, `self._stop`).
- **Two-tier map cache**: in-memory → `page_maps_<ota>.json` file → LLM.
- Genuine human gates pause (don't fail): login, OTP/email verify, CAPTCHA, bank/payout.
- Photo pre-flight warns (<100KB / <10 / portrait) before upload; uploads **chunk at 20** for MMT (`_upload_photos_chunked` + `_click_add_more_photos`).

## Per-OTA status
- **Booking.com** — fully working end-to-end (the original).
- **MakeMyTrip** — working through most steps via handlers: `_fill_mmt_property_type`, `_fill_mmt_basic_info` (years default **2026**), `_fill_mmt_location`, `_fill_mmt_amenities` (clicks through ALL left category tabs; strict/conservative Yes only for given amenities via `_mmt_present_set`/`_amenity_present`), `_fill_mmt_occupancy` (clamps max children < max occupancy), `_fill_mmt_rooms_overview` (multi-room). Verify-gate pause for email+mobile OTP.
- **Agoda** — handlers: `_fill_agoda_location` (search + structured + state/city dropdowns), `_fill_agoda_rooms` ("Set rate manually" + room-type dropdowns; LLM does size/rate), `_fill_agoda_times` (24h↔12h), `_fill_agoda_legal` (Country/Nationality/State/City dropdowns; DOB best-effort). Connect URL `agoda.com/en-us/list-my-property`.
- **Expedia** — first-pass handlers added (gated on `self.ota == "expedia"`): `_fill_expedia_location` (address autocomplete + structured fields + Country/State/City dropdowns, blocks the LLM for that page), `_fill_expedia_rooms` (room-type / bed-type dropdowns; LLM does size/rate), `_fill_expedia_times` (check-in/out pickers, 24h↔12h), plus a shared `_dropdown_already_set` "don't re-pick" guard. Selectors are resilient (label/placeholder/role + DOM-agnostic `_open_and_pick`/`_select_robust`) and body-text gated, so they degrade to the LLM walker on any unrecognised page. **Selectors still need one live run to tighten** — run to each step, then read `data/logs/expedia.log` + `data/training/expedia/` and paste any page that stalls so the exact `data-*` ids get locked in (same loop used for MMT/Agoda). 6 unit tests in `tests/test_expedia.py`.
- **Airbnb** — registered, generic walker only, **no handlers yet** (first runs will be rocky; map pages as they stall). Airbnb is heavily bot-protected.

## OPEN ISSUE (where we are right now)
**MMT creates only 1 room (Standard), not all 3.** Added `_fill_mmt_rooms_overview` to click "Create New Room" until `created == len(room_types)`, tracking the current room via `self._mmt_room_idx` (used in the per-room LLM narrowing). Added diagnostic logging + the persistent log file to debug.

**Next step:** run MMT to the Rooms step, then read `data/logs/makemytrip.log` for the line `MMT rooms overview — N created / 3 wanted`:
- If it appears → the handler sees the overview; fix the "Create New Room" click or the room-2 accordion fill.
- If it never appears → the walker **skips the overview** (MMT advances straight to Photos after room 1); change approach to add all rooms *before* leaving the Rooms step (e.g. don't click the final Continue until `created == target`).

The MMT "Create Room" overview page shows `CREATED (N)`, a `+ Create New Room` button, one `Edit Room` link per created room, and a bottom `Continue`.

## Working rules
- After ANY code change: run pytest, then stop+restart uvicorn (restart wipes in-memory session + live browser login, but `data/logs/<ota>.log` persists).
- Assistant hard line: never auto-type a bank/card/IBAN account NUMBER (pauses for the operator). Everything else (GST/PAN/consent) the service fills since it's the operator's own autonomous tool.
- When a page stalls (not a human gate), the operator pastes that page's HTML/log and a per-OTA deterministic handler gets added — same pattern used for every MMT/Agoda handler.
