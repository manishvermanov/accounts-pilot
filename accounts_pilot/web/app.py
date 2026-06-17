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

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
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
    """Fetch one hotel from MIS and convert it to a validated PropertyProfile.
    Returns a compact summary + the full profile (the UI keeps the profile hidden
    behind a 'View JSON' toggle and feeds it to the OTA fill)."""
    from accounts_pilot.mis import get_provider
    p = get_provider()
    try:
        loaded = p.load_profile(hotel_id)
    except KeyError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise HTTPException(502, f"MIS convert failed: {type(e).__name__}: {e}")
    return loaded     # {"profile": {...}, "summary": {...}}


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


# ---- static SPA -----------------------------------------------------------
@app.get("/")
def index():
    # no-store so the dashboard always loads the latest HTML/JS (no stale cached UI)
    return FileResponse(STATIC / "index.html",
                        headers={"Cache-Control": "no-store, must-revalidate"})


app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")
