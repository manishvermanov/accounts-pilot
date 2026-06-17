# Accounts Pilot — Architecture

> Automated OTA **property-onboarding**. Drives an OTA's "List your property" wizard
> from one canonical profile, auto-filling every data field and pausing at the steps
> that require a human (account, OTP, bank, contract).

---

## 1. What this is (and isn't)

| It IS | It is NOT |
|---|---|
| A robot that **lists your properties onto** OTAs | A channel manager |
| Driven by a single canonical **Property Profile** | A guest-data scraper |
| **Human-in-the-loop** at credential/financial/legal steps | A fully hands-off bot |
| **OTA-agnostic core**, one adapter per OTA | Booking.com-only by design |

The inventory / rate / booking-engine sync (the "channel manager" part) is a deliberate
**later** phase. v1 stops at "property is listed and live on the OTA."

---

## 2. Design principles

1. **OTA-agnostic core.** Everything except the adapter is shared. Adding an OTA = one new adapter class.
2. **AUTO vs GATE separation.** Every wizard step is classified. The engine owns AUTO; the gate handler owns GATE.
3. **Never auto-submit financials or credentials.** Bank/payout and account passwords are *human-only* gates — they aren't even modeled in the Profile.
4. **Resumable by default.** A job survives human pauses and the OTA's multi-day review. The browser is not held open.
5. **Audit everything.** Append-only log of every field, gate, and screenshot. The filesystem tells the truth.
6. **Two-layer anti-bot evasion.** Fingerprint layer (CloakBrowser, per-step) *and* behaviour layer (humanised mouse paths + typing cadence, always on). OTA risk engines score both — one without the other still gets flagged.

---

## 3. Component diagram

```mermaid
graph TD
    CLI["CLI / (future) API + Dashboard"] --> SM

    subgraph Core["OTA-agnostic core"]
        SM["⑤ Job State Machine<br/>run / resume loop"]
        PP["① Property Profile<br/>canonical data model"]
        GH["④ Gate Handler<br/>auto-resolve vs park"]
        AL["⑥ Audit Log<br/>append-only"]
        RT["③ Browser Runtime<br/>Playwright / CloakBrowser"]
    end

    subgraph Adapters["OTA-specific (one per OTA)"]
        BC["② BookingComAdapter<br/>step graph + selectors + mapping"]
        AG["AgodaAdapter (v2)"]
        MMT["MMTAdapter (v2)"]
    end

    subgraph Resolvers["Gate auto-resolvers"]
        OTP["OTP Resolver<br/>email / SMS"]
        CAP["CAPTCHA Solver<br/>2Captcha / CapSolver"]
    end

    SM --> PP
    SM --> GH
    SM --> AL
    SM --> RT
    SM --> Adapters
    RT --> BC
    GH --> OTP
    GH --> CAP
    BC -. drives .-> OTA[("Booking.com<br/>join wizard")]

    style Core fill:#eef6ff,stroke:#3b82f6
    style Adapters fill:#f0fdf4,stroke:#22c55e
    style Resolvers fill:#fef9c3,stroke:#eab308
```

### Responsibilities

| # | Component | Module | Responsibility | Knows about Booking.com? |
|---|---|---|---|---|
| ① | Property Profile | `models/property_profile.py` | Canonical hotel data; validation | ❌ |
| ② | OTA Adapter | `adapters/booking_com.py` | Step graph, selectors, Profile→OTA mapping, gate declarations | ✅ (the only one) |
| ③ | Browser Runtime | `runtime/browser.py` + `runtime/human.py` | Drive pages; Playwright/CloakBrowser; **humanised** clicks/typing; session reuse | ❌ |
| ④ | Gate Handler | `gates/handler.py` | Route each gate: auto-resolve (OTP/CAPTCHA) or park | ❌ |
| ⑤ | Job State Machine | `state/machine.py` | Persist + run/resume the step walk | ❌ |
| ⑥ | Audit Log | `audit/log.py` | Append-only evidence | ❌ |

The **single ✅** is the whole point: OTA knowledge is quarantined in the adapter.

---

## 4. Data model

```mermaid
classDiagram
    class PropertyProfile {
        +str property_id
        +PropertyType property_type
        +str display_name
        +str description
        +int total_rooms()
    }
    class Address {
        +str line1, city, state, country, postal_code
        +float latitude, longitude
    }
    class Contact {
        +str full_name
        +EmailStr email
        +str phone
    }
    class Compliance {
        +str legal_entity_name
        +str gstin
    }
    class RoomType {
        +str name
        +int count
        +int max_adults, max_children
        +float base_rate
        +str currency
    }
    class BedConfig {
        +BedType bed_type
        +int count
    }
    class Photo {
        +str path
        +str url
        +str caption
    }
    class Policy {
        +str checkin_from, checkout_until
        +str cancellation
        +list house_rules
    }

    PropertyProfile "1" --> "1" Address
    PropertyProfile "1" --> "1" Contact
    PropertyProfile "1" --> "1" Compliance
    PropertyProfile "1" --> "*" RoomType
    PropertyProfile "1" --> "*" Photo
    PropertyProfile "1" --> "1" Policy
    RoomType "1" --> "*" BedConfig

    note for PropertyProfile "Bank / payout details are\nDELIBERATELY NOT modeled here.\nHuman-only GATE step."
```

See [DATA-MODEL.md](DATA-MODEL.md) for the field-by-field reference.

---

## 5. Job state machine

```mermaid
stateDiagram-v2
    [*] --> draft
    draft --> filling : run_job()
    filling --> filling : AUTO step done → next

    filling --> awaiting_account : GATE account
    filling --> awaiting_otp : GATE otp (no resolver)
    filling --> awaiting_bank : GATE bank
    filling --> awaiting_contract : GATE contract
    filling --> awaiting_captcha : GATE captcha (no solver)

    awaiting_account --> filling : resume (human set login)
    awaiting_otp --> filling : resume (code entered / auto-read)
    awaiting_bank --> filling : resume (payout entered)
    awaiting_contract --> filling : resume (terms accepted)
    awaiting_captcha --> filling : resume (token injected)

    filling --> submitted : submit step
    submitted --> under_review : OTA reviewing
    under_review --> live : approved
    under_review --> needs_fix : changes requested
    needs_fix --> filling : fix + resubmit
    filling --> failed : unrecoverable
    live --> [*]
```

**Why it must be persistent:** the `awaiting_*` states can last minutes (OTP) to days
(`under_review`). You cannot hold a Chromium process open across that. The job is saved
to SQLite at every transition and resumed from `current_step`.

---

## 6. Gate handling

```mermaid
flowchart TD
    G[Adapter raises GateRequired] --> Q{Which gate?}
    Q -->|account / bank / contract| P[PARK<br/>human-only, always]
    Q -->|otp| O{OTP resolver<br/>configured?}
    Q -->|captcha| C{CAPTCHA solver<br/>configured?}
    O -->|yes| OR[auto-read code] --> R[RESOLVED → continue]
    O -->|no| P2[PARK → manual entry]
    C -->|yes| CR[solve → inject token] --> R
    C -->|no| P3[PARK → manual solve]
    P --> N[notify operator<br/>+ screenshot + audit]
    P2 --> N
    P3 --> N
    R --> NEXT[engine continues to next step]

    style P fill:#fee2e2,stroke:#ef4444
    style R fill:#dcfce7,stroke:#22c55e
```

**Hard rule:** `account`, `bank`, `contract` are in `HUMAN_ONLY` and can never be
auto-resolved, regardless of configuration. OTP and CAPTCHA *can* be auto-resolved when
a resolver/solver is configured (v1.1); until then they park like any other gate.

---

## 7. End-to-end sequence (one Booking.com onboarding)

```mermaid
sequenceDiagram
    actor Op as Operator
    participant CLI
    participant SM as State Machine
    participant AD as BookingComAdapter
    participant RT as Browser Runtime
    participant GH as Gate Handler
    participant AL as Audit Log
    participant BK as Booking.com

    Op->>CLI: onboard --profile X --ota booking_com
    CLI->>SM: run_job(job, profile)
    SM->>RT: launch (stealth if any step needs it)

    loop each step from current_step
        SM->>AD: run_step(step, rt, profile)
        AD->>RT: goto / fill / click (AUTO)
        RT->>BK: drive wizard page
        AD-->>SM: GateRequired(account)
        SM->>GH: handle(account)
        GH-->>SM: PARKED (human-only)
        SM->>AL: record(parked) + screenshot
        SM-->>CLI: job parked @ awaiting_account
    end

    Note over Op,BK: Human creates login on Booking.com
    Op->>CLI: resume <job_id> --profile X
    CLI->>SM: run_job(job)  %% picks up at next step
    SM->>AD: run_step(prop_type … tax)  %% AUTO core
    AD->>RT: fill all data fields
    AD-->>SM: GateRequired(bank) → PARK
    Note over Op,BK: Human enters payout + accepts contract
    Op->>CLI: resume <job_id>
    SM->>AD: run_step(submit)
    AD->>BK: submit listing
    SM->>SM: state = submitted → under_review → live
```

---

## 8. Tech stack

| Layer | Choice | Why (short) — full rationale in [DECISIONS.md](DECISIONS.md) |
|---|---|---|
| Language | Python 3.11+ | CloakBrowser is Python-only |
| Browser driver | Playwright + CloakBrowser | Auto-wait + stealth drop-in; **not** Selenium |
| Behaviour layer | HumanActor (Bézier mouse + typing cadence) | Defeats behavioural risk scoring; always on |
| Job engine (v1) | SQLite + framework-free loop | Zero infra; wrap in Celery/Temporal later |
| Job engine (scale) | Temporal (or Celery+Redis) | Durable workflows + human-in-loop signals |
| Data model | Pydantic v2 | Validation = the Profile schema |
| CLI | Typer + Rich | Fast, readable |
| OTP | IMAP / SMS provider (v1.1) | Auto-read verification codes |
| CAPTCHA | 2Captcha / CapSolver (v1.1) | Accuracy over cost |
| Dashboard (v1.2) | Retool / Streamlit → Next.js | Fast ops UI first, custom later |
| Deploy | Docker on Fargate/EC2 | Browser needs a real Chromium — never Lambda |

---

## 9. Deployment view (target, v1.2+)

```mermaid
graph LR
    subgraph AWS
        API["FastAPI<br/>(trigger + dashboard API)"]
        W["Worker container<br/>Python + Chromium + Xvfb"]
        DB[("PostgreSQL<br/>profiles · jobs · audit")]
        RDS[("Redis<br/>queue / locks")]
        S3[("S3<br/>photos · screenshots")]
    end
    DASH["Dashboard (Retool/Next.js)"] --> API
    API --> DB
    API --> RDS
    W --> RDS
    W --> DB
    W --> S3
    W -->|proxy| PX["Residential proxy"]
    W -->|stealth| OTA[("OTA signup wizards")]
    W --> SOLV["CAPTCHA / OTP services"]
```

> **Infra gotcha:** the worker ships a ~200–300 MB Chromium and runs it under Xvfb/headless.
> That's a long-running **container task**, not a Lambda — browser size/time limits fight serverless.

---

## 9b. Anti-bot evasion — the two layers

OTA signup pages run layered bot defences. Beating them needs **both** of these, because
each catches what the other misses:

```mermaid
graph LR
    subgraph L1["Layer 1 — FINGERPRINT (who you appear to be)"]
        F["CloakBrowser<br/>navigator.webdriver, canvas,<br/>WebGL, audio, fonts, TLS"]
    end
    subgraph L2["Layer 2 — BEHAVIOUR (how you act)"]
        B["HumanActor<br/>curved Bézier mouse paths,<br/>dwell before click,<br/>char-by-char typing,<br/>think() pauses"]
    end
    F --> OK{Pass the<br/>risk score?}
    B --> OK
    OK -->|both clean| GO[low bot score → proceed]
    OK -->|either off| FLAG[flagged → CAPTCHA / block]

    style L1 fill:#eef6ff,stroke:#3b82f6
    style L2 fill:#fef9c3,stroke:#eab308
    style FLAG fill:#fee2e2,stroke:#ef4444
```

| | Layer 1 — Fingerprint | Layer 2 — Behaviour |
|---|---|---|
| **Owned by** | CloakBrowser (`runtime/browser.py`) | HumanActor (`runtime/human.py`) |
| **Hides** | That it's automation/headless | That a *robot* is driving |
| **Defeats** | FingerprintJS, `navigator.webdriver`, canvas/TLS checks | DataDome / PerimeterX / reCAPTCHA *risk score* (mouse, timing, cadence) |
| **When active** | Per-step (`needs_stealth`) | **Always on** (`humanize=true`) |

**How the behaviour layer works:**
- **Mouse:** moves along a cubic-Bézier arc with two random control points, eased speed
  (slow-fast-slow), per-hop jitter, and aims at a *random point inside* the target — never the
  exact centre. Virtual cursor position persists between actions so paths are continuous.
- **Click:** move → short dwell → `mouse.down` → micro-delay → `mouse.up` (not an instant click).
- **Typing:** field is focused via a human click, then text is entered **character-by-character**
  with randomised inter-key delays and an occasional longer pause (a "typo-think").
- **Pacing:** `think()` pauses between fields and between wizard steps.

All timing ranges are tunable in `.env` (`KEY_DELAY_*`, `THINK_*`). `humanize=false` exists only
for fast local tests against non-hostile pages.

> **Why this matters here specifically:** CloakBrowser makes the *browser* look real, but a
> teleporting cursor and instant form-fill make the *driver* look robotic. On hostile OTA
> signups that behavioural tell alone is enough to trip the wall — so the behaviour layer is
> on by default, not opt-in.

---

## 10. Security & compliance

- **Bank/payout + OTA passwords never enter the Profile or the repo.** Human-only gates; secrets live in an encrypted store (`.env`/Vault), gitignored.
- **PII** (guest-free here — only the hotel's own contact + GSTIN) handled under DPDP; audit log is the access record.
- **Self-hosted stealth** chosen partly *because* a managed browser service would see credential/bank keystrokes (see [DECISIONS.md ADR-003](DECISIONS.md)).
- **OTA ToS:** automating signup is operator-owned risk; the human-in-loop gates keep a person on every contractual/financial action.

---

## 11. Adding a new OTA (extension guide)

1. Create `adapters/<ota>.py` with a `class XAdapter(OTAAdapter)`.
2. Implement `steps()` — the ordered step graph, each tagged AUTO / GATE / SYSTEM.
3. Implement `run_step()` — AUTO steps map the Profile to that OTA's fields; GATE steps `raise GateRequired(...)`.
4. Register it in `adapters/__init__.py:REGISTRY`.
5. Nothing else changes — runtime, gates, state machine, audit, CLI are all shared.

That single-file extension cost is the payoff of the OTA-agnostic core.
