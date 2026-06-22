"""FastAPI app — owner-facing assisted-onboarding dashboard.

Endpoints:
  GET  /                          → the dashboard SPA
  GET  /api/properties            → properties from the booking engine
  GET  /api/jobs                  → all onboarding jobs
  POST /api/jobs                  → start onboarding {property_id, ota}
  GET  /api/jobs/{id}             → job + step plan + audit
  POST /api/jobs/{id}/gate        → owner cleared a gate {gate, value}
  POST /api/jobs/{id}/fill        → run a live TinyFish fill (demo target)
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from accounts_pilot.audit.log import AuditLog
from accounts_pilot.drivers import TinyFishDriver, booking_goals
from accounts_pilot.models.job import GateKind, OnboardingJob
from accounts_pilot.sources import BookingEngineSource
from accounts_pilot.state.machine import JobStore
from accounts_pilot.web import flow

app = FastAPI(title="Accounts Pilot")
STATIC = Path(__file__).parent / "static"
store = JobStore()
engine = BookingEngineSource()


# ---- optional shared login (active only when AP_AUTH_USER + AP_AUTH_PASS are set) ----
# Cookie-based (NOT HTTP Basic): the github.dev / app.github.dev proxy drops the
# Authorization header, so Basic-auth loops forever there. A login form + signed cookie
# passes through cleanly. No env set → no auth (local dev / GitHub-private port).
import hashlib as _hashlib


def _auth_token() -> str:
    from accounts_pilot.config import settings as _s
    return _hashlib.sha256(f"{_s.ap_auth_user}:{_s.ap_auth_pass}".encode()).hexdigest()


_LOGIN_PAGE = """<!doctype html><html><head><meta charset=utf-8><title>Sign in — Accounts Pilot</title>
<meta name=viewport content="width=device-width,initial-scale=1">
<style>body{font-family:Inter,system-ui,Segoe UI,sans-serif;background:#0f1729;color:#e7ecf4;display:grid;place-items:center;height:100vh;margin:0}
.box{background:#1a2235;border:1px solid #2a3550;border-radius:16px;padding:28px 26px;width:300px;box-shadow:0 24px 64px -24px #000}
h1{font-size:19px;margin:0 0 3px}p{color:#94a1b6;font-size:12.5px;margin:0 0 16px}
input{width:100%;box-sizing:border-box;margin:6px 0;padding:11px 12px;border-radius:9px;border:1px solid #2a3550;background:#0f1729;color:#fff;font-size:14px;outline:none}
input:focus{border-color:#4f46e5}
button{width:100%;margin-top:12px;padding:11px;border:0;border-radius:9px;background:linear-gradient(135deg,#4f46e5,#7c5cff);color:#fff;font-weight:700;font-size:14px;cursor:pointer}
.err{color:#ff7a7a;font-size:12px;margin-top:10px;min-height:14px}</style></head>
<body><form class=box method=post action="/login">
<h1>Accounts&nbsp;Pilot</h1><p>Sign in to continue</p>
<input name=username placeholder=Username autofocus autocomplete=username>
<input name=password type=password placeholder=Password autocomplete=current-password>
<button>Sign in</button><div class=err>%ERR%</div></form></body></html>"""


@app.get("/login")
def login_page():
    return HTMLResponse(_LOGIN_PAGE.replace("%ERR%", ""))


@app.post("/login")
async def login_submit(request: Request):
    import secrets as _secrets
    from urllib.parse import parse_qs
    from accounts_pilot.config import settings as _s
    data = parse_qs((await request.body()).decode("utf-8", "ignore"))
    user = (data.get("username") or [""])[0]
    pw = (data.get("password") or [""])[0]
    if (_s.ap_auth_user and _secrets.compare_digest(user, _s.ap_auth_user)
            and _secrets.compare_digest(pw, _s.ap_auth_pass)):
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie("ap_session", _auth_token(), httponly=True, secure=True,
                        samesite="lax", max_age=60 * 60 * 24 * 30)
        return resp
    return HTMLResponse(_LOGIN_PAGE.replace("%ERR%", "Wrong username or password"), status_code=401)


@app.middleware("http")
async def _cookie_auth(request, call_next):
    """Require the session cookie when a shared login is configured. /login is open."""
    from accounts_pilot.config import settings as _s
    if _s.ap_auth_user and _s.ap_auth_pass and request.url.path != "/login":
        if request.cookies.get("ap_session") != _auth_token():
            return RedirectResponse("/login", status_code=303)
    return await call_next(request)


def _jid(property_id: str, ota: str) -> str:
    return f"{ota}__{property_id}"


def _job_payload(job: OnboardingJob) -> dict:
    profile = engine.get(job.property_id)
    audit = AuditLog(store.db_path).for_job(job.job_id)
    return {
        "job": job.model_dump(),
        "property": {
            "id": profile.property_id, "name": profile.display_name,
            "type": profile.property_type.value, "stars": profile.star_rating,
            "city": profile.address.city, "rooms": profile.total_rooms,
        },
        "plan": flow.status_plan(job, profile),
        "audit": audit,
    }


# ---- API ------------------------------------------------------------------
@app.get("/api/properties")
def list_properties():
    out = []
    for p in engine.all():
        out.append({"id": p.property_id, "name": p.display_name, "type": p.property_type.value,
                    "stars": p.star_rating, "city": p.address.city, "rooms": p.total_rooms,
                    "goals": len(booking_goals(p))})
    return {"source": engine.describe(), "properties": out}


@app.get("/api/properties/{property_id}/sheet")
def property_sheet(property_id: str):
    from accounts_pilot.web.sheet import sheet_fields
    try:
        p = engine.get(property_id)
    except Exception as e:
        raise HTTPException(404, f"property not found: {e}")
    return {"property_id": p.property_id, "name": p.display_name, "fields": sheet_fields(p)}


@app.get("/api/jobs")
def list_jobs():
    return {"jobs": [j.model_dump() for j in store.list()]}


class StartReq(BaseModel):
    property_id: str
    ota: str = "booking_com"


@app.post("/api/jobs")
def start_job(req: StartReq):
    try:
        engine.get(req.property_id)
    except Exception as e:
        raise HTTPException(404, f"property not found: {e}")
    jid = _jid(req.property_id, req.ota)
    job = store.get(jid) or OnboardingJob(job_id=jid, property_id=req.property_id, ota=req.ota)
    flow.progress(job)                        # advance to the first gate
    store.save(job)
    AuditLog(store.db_path).record(jid, job.current_step, "start", "onboarding started")
    return _job_payload(job)


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    job = store.get(job_id)
    if not job:
        raise HTTPException(404, "no such job")
    return _job_payload(job)


class GateReq(BaseModel):
    gate: str
    value: str = ""


@app.post("/api/jobs/{job_id}/gate")
def clear_gate(job_id: str, req: GateReq):
    job = store.get(job_id)
    if not job:
        raise HTTPException(404, "no such job")
    try:
        gate = GateKind(req.gate)
    except ValueError:
        raise HTTPException(400, f"bad gate: {req.gate}")
    if not flow.clear_gate(job, gate, req.value):
        raise HTTPException(409, f"job is not waiting on {req.gate}")
    store.save(job)
    note = f"otp={req.value}" if gate is GateKind.OTP and req.value else "done"
    AuditLog(store.db_path).record(job_id, gate.value, "gate_cleared", note)
    return _job_payload(job)


@app.post("/api/jobs/{job_id}/fill-step")
def fill_step(job_id: str, step: int = 0):
    """Fill ONE Booking.com wizard step via TinyFish using your saved login session.
    The UI calls this in sequence so you watch it fill step by step."""
    job = store.get(job_id)
    if not job:
        raise HTTPException(404, "no such job")
    p = engine.get(job.property_id)
    goals = booking_goals(p)
    total = len(goals)
    if step >= total:
        return {"done": True, "total": total}
    driver = TinyFishDriver()
    if not driver.ready:
        raise HTTPException(400, "TINYFISH_API_KEY not set")
    g = goals[step]
    out = {"step": g["step"], "goal": g["goal"], "index": step, "total": total,
           "next": step + 1, "done": step + 1 >= total}
    try:
        res = driver.run_goal("https://admin.booking.com/", g["goal"], use_profile=True, timeout_s=240)
        out["status"] = res.get("status")
        out["agent_steps"] = res.get("num_of_steps")
        out["result"] = (res.get("result") or {}).get("result")
    except Exception as e:
        out["status"] = "error"
        out["result"] = f"{type(e).__name__}: {e}"
    AuditLog(store.db_path).record(job_id, f"fill:{g['step']}", "tinyfish", str(out.get("status")))
    return out


@app.post("/api/jobs/{job_id}/fill")
def live_fill(job_id: str):
    """Run a real TinyFish fill (against a public test form) with this job's property data."""
    job = store.get(job_id)
    if not job:
        raise HTTPException(404, "no such job")
    p = engine.get(job.property_id)
    driver = TinyFishDriver()
    if not driver.ready:
        raise HTTPException(400, "TINYFISH_API_KEY not set")
    goal = (f"Fill this form. Customer name: '{p.contact.full_name}'. Telephone: '{p.contact.phone}'. "
            f"Email: '{p.contact.email}'. In comments write: 'Onboarding {p.display_name}, "
            f"{p.address.city} — {p.star_rating}-star {p.property_type.value}, {p.total_rooms} rooms'. "
            f"Pick size Medium, click 'Submit order', return the JSON shown.")
    try:
        res = driver.run_goal("https://httpbin.org/forms/post", goal, timeout_s=180)
    except Exception as e:
        raise HTTPException(502, f"TinyFish error: {e}")
    AuditLog(store.db_path).record(job_id, "fill", "tinyfish", str(res.get("status")))
    return {"status": res.get("status"), "steps": res.get("num_of_steps"),
            "result": (res.get("result") or {}).get("result")}


# ---- live browser session (paste JSON → connect → fill) -------------------
class FillJsonReq(BaseModel):
    profile: dict


@app.get("/api/otas")
def list_otas():
    """OTAs the dashboard can drive (one independent session each)."""
    from accounts_pilot.adapters import REGISTRY, OTA_LABELS
    return {"otas": [{"id": k, "label": OTA_LABELS.get(k, k)} for k in REGISTRY]}


# ---- MIS: search a hotel → convert its record to a profile (no JSON pasting) ----
@app.get("/api/mis/search")
def mis_search(q: str = ""):
    """Search the company MIS by hotel name. Returns a small list — the raw data
    is never exposed; the operator just picks a hotel."""
    from accounts_pilot.mis import get_provider
    p = get_provider()
    try:
        results = p.search(q)
    except Exception as e:
        raise HTTPException(502, f"MIS search failed: {type(e).__name__}: {e}")
    return {"source": p.describe(), "count": len(results), "results": results[:50]}


@app.get("/api/mis/health")
def mis_health():
    """Verify the MIS is reachable (DB connects / folder readable) and how many
    hotels it can see — use this to confirm the DSN before searching."""
    from accounts_pilot.mis import get_provider
    p = get_provider()
    try:
        return {"ok": True, **p.health()}
    except Exception as e:
        return {"ok": False, "source": p.describe(), "error": f"{type(e).__name__}: {e}"}


class ProfileReq(BaseModel):
    profile: dict


def _validity(profile: dict) -> tuple[bool, list[str]]:
    """(valid, [missing field paths]) — lenient check used by the editors."""
    from accounts_pilot.models.property_profile import PropertyProfile
    try:
        PropertyProfile.model_validate(profile)
        return True, []
    except Exception as e:
        missing = []
        for err in getattr(e, "errors", lambda: [])():
            missing.append(".".join(str(p) for p in err.get("loc", ())))
        return False, missing


@app.post("/api/mis/validate")
def mis_validate(req: ProfileReq):
    """Re-validate an operator-edited profile against the schema and return a fresh
    summary. Used by the editable JSON viewer so manual fixes (room counts, star
    rating, extra rate plans the DB lacks) can't ship a broken profile to the OTAs."""
    from accounts_pilot.mis import summarize
    valid, _ = _validity(req.profile)
    if not valid:
        from accounts_pilot.models.property_profile import PropertyProfile
        try:
            PropertyProfile.model_validate(req.profile)
        except Exception as e:
            return {"ok": False, "error": str(e)}
    return {"ok": True, "summary": summarize(req.profile)}


@app.post("/api/mis/normalize")
def mis_normalize(req: ProfileReq):
    """Convert a hand-entered/pasted property JSON into the full profile (every OTA
    field materialized, empty where unknown) — the same conversion search uses, so
    the table editor shows the complete field set. Lenient: returns valid/missing."""
    from accounts_pilot.mis import summarize
    from accounts_pilot.mis.convert import normalize_to_profile
    try:
        profile = normalize_to_profile(req.profile, validate=False)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    valid, missing = _validity(profile)
    return {"ok": True, "profile": profile, "summary": summarize(profile),
            "valid": valid, "missing": missing}


@app.get("/api/mis/hotel/{hotel_id}")
def mis_hotel(hotel_id: str):
    """Fetch one hotel from MIS and convert it to a PropertyProfile. If the operator
    has saved edits for this hotel (MIS is read-only — edits live in the local DB),
    merge them back on top (their edits win), so prior changes reload automatically."""
    from accounts_pilot.mis import get_provider, summarize
    from accounts_pilot.mis.convert import _deep_merge
    from accounts_pilot.mis.overrides import get_override
    p = get_provider()
    try:
        loaded = p.load_profile(hotel_id)
    except KeyError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise HTTPException(502, f"MIS convert failed: {type(e).__name__}: {e}")

    override = get_override(hotel_id)
    if override:
        merged = _deep_merge(loaded["profile"], override)   # operator's saved edits win
        valid, missing = _validity(merged)
        loaded.update(profile=merged, summary=summarize(merged),
                      valid=valid, missing=missing, from_override=True)
    else:
        loaded["from_override"] = False
    return loaded     # {"profile", "summary", "valid", "missing", "from_override"}


@app.post("/api/mis/save")
def mis_save(req: ProfileReq):
    """Persist the operator's edited profile to the local DB (keyed by property_id),
    so it survives restarts and reloads next time the hotel is opened. Saves even if a
    few required fields are still blank (progress isn't lost); reports valid/missing."""
    from accounts_pilot.mis import summarize
    from accounts_pilot.mis.overrides import save_override
    pid = req.profile.get("property_id")
    if not pid:
        return {"ok": False, "error": "profile has no property_id — cannot save"}
    save_override(pid, req.profile)
    valid, missing = _validity(req.profile)
    return {"ok": True, "saved": True, "summary": summarize(req.profile),
            "valid": valid, "missing": missing}


@app.delete("/api/mis/override/{hotel_id}")
def mis_clear_override(hotel_id: str):
    """Forget the saved edits for this hotel — next open re-pulls fresh from the MIS."""
    from accounts_pilot.mis.overrides import delete_override
    delete_override(hotel_id)
    return {"ok": True}


@app.post("/api/record-page")
def record_page(ota: str = "booking_com"):
    """Capture the current page's fields to build a deterministic field map for this OTA."""
    from accounts_pilot.web.live import get_session
    s = get_session(ota)
    if s.state not in ("connected", "filling"):
        raise HTTPException(409, f"not connected — Connect to {ota} first")
    s.record()
    return s.status()


@app.post("/api/fill-json")
def fill_json(req: FillJsonReq, ota: str = "booking_com"):
    """Fill the CURRENT page of the OTA's connected browser with the pasted property JSON."""
    from accounts_pilot.web.live import get_session
    s = get_session(ota)
    if s.state not in ("connected", "filling"):
        raise HTTPException(409, f"not connected — click Connect to {ota} first")
    s.fill(req.profile)
    return s.status()


@app.get("/api/simulate/otas")
def simulate_otas():
    """OTAs that have an OFFLINE handler simulation (others get validation + coverage)."""
    from accounts_pilot.web.simulate import available_otas
    return {"otas": available_otas()}


@app.post("/api/simulate")
def simulate_profile(req: ProfileReq, ota: str = "all"):
    """Dry-run the pasted property JSON: validate it, report field coverage + photo
    pre-flight, and drive the deterministic handlers against synthetic pages in a headless
    browser — NO live OTA login, never touches the live session. Defaults to a GLOBAL run
    across every supported channel; pass ota=<id> to target one. Returns a structured report."""
    from accounts_pilot.web.simulate import simulate, simulate_all
    try:
        return simulate_all(req.profile) if ota in ("all", "*", "") else simulate(ota, req.profile)
    except Exception as e:
        raise HTTPException(500, f"simulate failed: {type(e).__name__}: {e}")


@app.get("/api/properties/{property_id}/json")
def property_json(property_id: str):
    """Full property profile as JSON (for the 'load example' button)."""
    try:
        return engine.get(property_id).model_dump()
    except Exception as e:
        raise HTTPException(404, f"property not found: {e}")


@app.post("/api/connect")
def connect_start(ota: str = "booking_com"):
    from accounts_pilot.web.live import get_session
    s = get_session(ota)
    s.start(ota)
    return s.status()


@app.get("/api/connect/status")
def connect_status(ota: str = "booking_com"):
    from accounts_pilot.web.live import get_session
    return get_session(ota).status()


@app.post("/api/connect/captcha-done")
def connect_captcha(ota: str = "booking_com"):
    from accounts_pilot.web.live import get_session
    s = get_session(ota)
    s.captcha_done()
    return s.status()


class OtpReq(BaseModel):
    code: str


@app.post("/api/connect/otp")
def connect_otp(req: OtpReq, ota: str = "booking_com"):
    from accounts_pilot.web.live import get_session
    s = get_session(ota)
    s.submit_otp(req.code)
    return s.status()


@app.post("/api/fill/stop")
def fill_stop(ota: str = "booking_com"):
    """Operator Kill — halt the fill currently running for this OTA (browser stays open)."""
    from accounts_pilot.web.live import get_session
    s = get_session(ota)
    s.stop()
    return s.status()


@app.get("/api/ping")
def ping():
    """Liveness check — the Restart button polls this until the server is back up."""
    return {"ok": True}


@app.post("/api/restart")
def restart_server():
    """Restart the dev server RELIABLY. Touching a --reload watched file is too fragile (if
    the reload watcher hiccups the server just dies and never comes back). Instead we spawn a
    fully DETACHED helper process that — after this response is sent — kills the current
    server and starts a fresh one. The helper outlives the server, so it always comes back.
    The UI then polls /api/ping until the new worker answers."""
    import os
    import subprocess
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]                 # repo root (has .venv/)
    venv_py = root / ".venv" / "Scripts" / "python.exe"
    py = str(venv_py) if venv_py.exists() else sys.executable
    helper = root / "scripts" / "_ap_restart.py"
    try:
        kwargs = dict(cwd=str(root), stdin=subprocess.DEVNULL,
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if os.name == "nt":
            kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | NEW_PROCESS_GROUP
            kwargs["close_fds"] = True
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen([py, str(helper)], **kwargs)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ---- static SPA -----------------------------------------------------------
@app.get("/")
def index():
    # no-store so the dashboard always loads the latest HTML/JS (no stale cached UI)
    return FileResponse(STATIC / "index.html",
                        headers={"Cache-Control": "no-store, must-revalidate"})


app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")
