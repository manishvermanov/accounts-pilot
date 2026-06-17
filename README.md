# Accounts Pilot

Automated OTA **property-onboarding** service. Drives an OTA's "List your property"
wizard from a single canonical property profile, auto-filling every data field and
pausing at the steps that need a human (account creation, OTP, bank/payout, contract).

**v1 target:** Booking.com. The engine is OTA-agnostic — adding a new OTA = writing one
adapter, nothing else.

> This is **not** a channel manager and it does **not** pull guest data out of OTAs.
> It puts *your* properties *onto* OTAs. The inventory/booking-engine integration is a
> deliberate later step.

## Documentation

| Doc | What's in it |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Components, diagrams (component / data-model / state machine / gate flow / sequence / deployment) |
| [docs/PLAN.md](docs/PLAN.md) | Phased build plan (v0→v3), milestones, acceptance criteria, risks |
| [docs/DECISIONS.md](docs/DECISIONS.md) | Stack decision records (why Python / Playwright / CloakBrowser / SQLite-then-Temporal …) |
| [docs/DATA-MODEL.md](docs/DATA-MODEL.md) | Field-by-field Property Profile + Job reference |
| [docs/BOOKING-FIELDS.md](docs/BOOKING-FIELDS.md) | **Every Booking.com onboarding field**, by wizard segment, mapped to the model + AUTO/GATE |
| [docs/RUNBOOK.md](docs/RUNBOOK.md) | How to run, operate, and troubleshoot an onboarding |

---

## Architecture — the six components

| Component | Module | Role |
|---|---|---|
| **Property Profile** | `accounts_pilot/models/property_profile.py` | Canonical, OTA-agnostic data model for one hotel. Fill once, feed every OTA. |
| **OTA Adapter** | `accounts_pilot/adapters/booking_com.py` | The OTA-specific brain: wizard step graph, selectors, Profile→OTA field mapping, gate declarations. |
| **Browser runtime** | `accounts_pilot/runtime/browser.py` + `runtime/human.py` | Drives the wizard. Playwright + CloakBrowser (fingerprint stealth) **and** a HumanActor (curved mouse paths + human typing — behavioural stealth, always on). |
| **Gate handler** | `accounts_pilot/gates/` | Manages the human/credential/verification steps: park-and-notify or auto-resolve (OTP, CAPTCHA). |
| **Job state machine** | `accounts_pilot/state/machine.py` | Resumable jobs that survive the human pauses + the OTA's own review delay. |
| **Audit log** | `accounts_pilot/audit/log.py` | Append-only record of every field, gate, and screenshot. |

### The AUTO vs GATE split (Booking.com)

| Step | Type |
|---|---|
| Create account (email + password) | **GATE** — credential |
| Verify email / phone (OTP) | **GATE** — verification (auto-resolvable) |
| Property type | AUTO |
| Name + address + map pin | AUTO |
| Room types, counts, occupancy | AUTO |
| Base rates + currency | AUTO |
| Amenities | AUTO |
| Photos | AUTO |
| Policies | AUTO |
| Contact person | AUTO |
| Payout / bank account | **GATE** — financial (human only) |
| Tax / GST | AUTO (GSTIN) |
| Partner contract | **GATE** — contract (human only) |
| Submit → review → live | system |
| CAPTCHA (any step) | **GATE** — solver/stealth |

The engine owns the AUTO steps. The gate handler manages the GATE steps.

---

## Quickstart (local)

```bash
# 1. create a virtualenv
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # macOS/Linux

# 2. install
pip install -r requirements.txt
playwright install chromium

# 3. validate the sample property
python -m accounts_pilot.cli validate examples/sample_property.json

# 4. dry-run the Booking.com onboarding (no real submission — prints the plan)
python -m accounts_pilot.cli plan --profile examples/sample_property.json --ota booking_com

# 5. open the live wizard and walk the AUTO steps (parks at the first GATE)
python -m accounts_pilot.cli onboard --profile examples/sample_property.json --ota booking_com

# inspect job state
python -m accounts_pilot.cli status
```

### Web dashboard (the owner-facing product)

```bash
python -m accounts_pilot.cli serve          # → http://127.0.0.1:8000
```
Owner picks a property → sees the 16-step Booking.com flow → the service fills the
AUTO steps, the owner clears their gates (account, OTP, bank, contract) with one click
each. "Run live fill" drives TinyFish against a test form with the property's data.

### Booking-engine source + live demo

```bash
# the service fetches property data FROM the booking engine (examples/booking_engine/)
python -m accounts_pilot.cli engine                 # list properties
python -m accounts_pilot.cli engine UDR-001         # fetch one

# end-to-end: fetch from engine → build Booking.com goals → TinyFish fills a live form
python -m accounts_pilot.cli demo --property-id UDR-001
```

Point the engine at a real HTTP backend instead of the local folder by setting
`BOOKING_ENGINE_URL` in `.env` (expects `GET /properties` and `GET /properties/{id}`).

> CloakBrowser is **optional** and installed on demand (`pip install cloakbrowser`).
> Without it, the runtime falls back to plain Playwright. The adapter declares which
> steps need stealth.

---

## Status

**v1 scaffold.** The structure, models, state machine, gate routing, audit log, the
behavioural humaniser (mouse paths + typing cadence), and the Booking.com step graph are in
place. The Booking.com selectors are **placeholders** — they get filled in once we walk the
live wizard and capture the real DOM. That is the next task.

> **Anti-bot is two layers.** Fingerprint stealth (CloakBrowser) **and** behaviour stealth
> (humanised mouse + typing, on by default) — OTA risk engines score both. See
> [docs/ARCHITECTURE.md §9b](docs/ARCHITECTURE.md).

## Roadmap

- **v1**   AUTO steps automated, all gates as manual park-and-notify. CLI trigger.
- **v1.1** OTP auto-resolve (email/SMS), CAPTCHA auto-solve.
- **v1.2** Operator dashboard (gate completion UI).
- **v2**   Second OTA adapter (Agoda / MMT) — proves the engine is truly OTA-agnostic.
