"""Accounts Pilot CLI.

  validate <profile.json>                      — parse + validate a property profile
  plan     --profile <f> --ota booking_com     — dry run: print the step graph, no browser
  onboard  --profile <f> --ota booking_com     — run AUTO steps, park at first gate
  resume   <job_id> --profile <f>              — continue a parked job
  status   [job_id]                            — list jobs / show one job + audit trail
"""
from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from accounts_pilot.audit.log import AuditLog
from accounts_pilot.models.job import OnboardingJob
from accounts_pilot.models.property_profile import PropertyProfile
from accounts_pilot.state.machine import JobStore, run_job, inspect_fields

app = typer.Typer(add_completion=False, help="Automated OTA property-onboarding.")
console = Console()


def _load_profile(path: str) -> PropertyProfile:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return PropertyProfile.model_validate(data)


def _job_id(property_id: str, ota: str) -> str:
    return f"{ota}__{property_id}"


@app.command()
def validate(profile: str):
    """Validate a property profile JSON file."""
    p = _load_profile(profile)
    console.print(f"[green]OK[/] — {p.display_name} ({p.property_type.value}), "
                  f"{p.total_rooms} rooms, {len(p.amenities)} amenities, {len(p.photos)} photos")


@app.command()
def plan(profile: str = typer.Option(...), ota: str = typer.Option("booking_com")):
    """Dry run — print the onboarding step graph without touching a browser."""
    p = _load_profile(profile)
    store = JobStore()
    job = OnboardingJob(job_id=_job_id(p.property_id, ota), property_id=p.property_id, ota=ota)
    run_job(job, p, store, dry_run=True)


@app.command()
def capture(ota: str = typer.Option("booking_com")):
    """Log in with the configured creds and drill the wizard, dumping each page's DOM
    to data/artifacts/capture/ so the remaining selectors can be harvested.

    Requires BOOKING_PARTNER_EMAIL / BOOKING_PARTNER_PASSWORD in .env. Pauses at the
    CAPTCHA/OTP wall (Booking-side) for a one-time human tap, then re-run to continue."""
    from accounts_pilot.config import settings
    from accounts_pilot.adapters import get_adapter
    from accounts_pilot.runtime.browser import BrowserRuntime

    if not settings.booking_partner_email or not settings.booking_partner_password:
        console.print("[red]Missing creds[/] — set BOOKING_PARTNER_EMAIL / BOOKING_PARTNER_PASSWORD in .env")
        raise typer.Exit(1)

    from accounts_pilot.gates.captcha import CaptchaSolver

    adapter = get_adapter(ota)
    settings.ensure_dirs()
    solver = CaptchaSolver(provider=settings.captcha_provider, api_key=settings.captcha_api_key)

    with BrowserRuntime(stealth=True) as rt:
        result = adapter.login(rt, settings.booking_partner_email, settings.booking_partner_password)

        # Handle the live login gates in sequence — you watch it in the window.
        from accounts_pilot.gates.otp import OTPResolver
        for _ in range(6):
            if result == "captcha":
                ch = rt.find_captcha() or {"kind": "unknown", "site_key": ""}
                token = None
                try:
                    token = solver.try_solve(site_key=ch["site_key"], page_url=rt.page.url, kind=ch["kind"])
                except NotImplementedError:
                    console.print("[yellow]CAPTCHA solver hook not implemented (gates/captcha.py).[/]")
                if token:
                    rt.apply_captcha_token(token, kind=ch["kind"]); rt.try_advance()
                    console.print("[green]CAPTCHA solved + injected[/] — continuing.")
                elif not settings.headless:
                    input("  ↳ Solve the CAPTCHA in the browser window, then press Enter… ")
                    rt.try_advance()
                else:
                    console.print("[yellow]Paused at CAPTCHA[/] — wire a solver or run with HEADLESS=false.")
                    return
            elif result == "verification":
                console.print("[yellow]Booking sent a verification code.[/]")
                code = OTPResolver().try_resolve(channel="email")
                if not code:
                    console.print("[yellow]No code entered — stopping.[/]")
                    return
                for sel in ("input[autocomplete='one-time-code']", "input[name*='code']",
                            "input[name*='otp']", "input[name*='pin']"):
                    if rt.has(sel, timeout_ms=1500):
                        rt.fill(sel, code)
                        break
                rt.try_advance()
                console.print("[green]OTP entered into Booking.com[/] — continuing.")
            else:
                break
            rt.think()
            nxt = rt.detect_challenge()
            result = nxt if nxt else "ok"

        dumped = adapter.capture_walk(rt)
        console.print(f"[green]Captured {len(dumped)} wizard page(s)[/] → data/artifacts/capture/")


@app.command()
def serve(host: str = typer.Option("127.0.0.1"), port: int = typer.Option(8000)):
    """Launch the owner-facing web dashboard."""
    import uvicorn
    console.print(f"[green]Accounts Pilot dashboard[/] → http://{host}:{port}")
    uvicorn.run("accounts_pilot.web.app:app", host=host, port=port, log_level="warning")


@app.command()
def engine(property_id: str = typer.Argument(None, help="fetch one property; omit to list all")):
    """List properties in the booking engine, or fetch one (the source the service pulls from)."""
    from accounts_pilot.sources import BookingEngineSource
    src = BookingEngineSource()
    console.print(f"[dim]{src.describe()}[/]")
    if property_id is None:
        t = Table("property_id", "name", "type", "stars", "city", "rooms")
        for p in src.all():
            t.add_row(p.property_id, p.display_name, p.property_type.value,
                      str(p.star_rating or "—"), p.address.city, str(p.total_rooms))
        console.print(t)
        return
    p = src.get(property_id)
    console.print(f"[green]Fetched[/] {p.property_id}: {p.display_name} "
                  f"({p.star_rating or '—'}★ {p.property_type.value}, {p.total_rooms} rooms, {p.address.city})")


@app.command()
def demo(property_id: str = typer.Option("UDR-001", help="property to pull from the booking engine"),
         live: bool = typer.Option(True, help="run a real TinyFish fill on a public test form")):
    """End-to-end demo: fetch property from the booking engine → build Booking.com fill
    goals → (live) have TinyFish actually fill a public form with the fetched data."""
    from accounts_pilot.sources import BookingEngineSource
    from accounts_pilot.drivers import TinyFishDriver, booking_goals
    from accounts_pilot.config import settings

    console.rule("[bold]1. Fetch from booking engine")
    src = BookingEngineSource()
    console.print(f"[dim]source: {src.describe()}[/]")
    p = src.get(property_id)
    console.print(f"[green]✓ fetched[/] {p.property_id}: {p.display_name} — "
                  f"{p.star_rating}★ {p.property_type.value}, {p.total_rooms} rooms, {p.address.city}")

    console.rule("[bold]2. Build Booking.com fill goals")
    goals = booking_goals(p)
    for g in goals[:4]:
        console.print(f"[cyan]{g['step']}[/]: {g['goal'][:90]}…")
    console.print(f"[dim]… {len(goals)} goals total[/]")

    if not live:
        return
    console.rule("[bold]3. Live TinyFish fill (public test form)")
    driver = TinyFishDriver()
    if not driver.ready:
        console.print("[yellow]No TINYFISH_API_KEY — skipping live step.[/]")
        return
    form_url = "https://httpbin.org/forms/post"
    goal = (f"Fill this form using these details. Customer name: '{p.contact.full_name}'. "
            f"Telephone: '{p.contact.phone}'. Email: '{p.contact.email}'. "
            f"In the comments box write exactly: 'Onboarding {p.display_name}, "
            f"{p.address.city} — {p.star_rating}-star {p.property_type.value}, {p.total_rooms} rooms'. "
            f"Pick pizza size Medium. Then click the 'Submit order' button and return the JSON shown.")
    console.print(f"[dim]target: {form_url}[/]\n[dim]filling with fetched data…[/]")
    try:
        res = driver.run_goal(form_url, goal, timeout_s=180)
        inner = (res or {}).get("result", {})
        console.print(f"[green]✓ TinyFish run {res.get('status')}[/] in {res.get('num_of_steps')} steps")
        console.print(str(inner.get("result", res))[:900])
    except Exception as e:
        console.print(f"[red]live step failed:[/] {type(e).__name__}: {e}")


@app.command()
def tinyfish(profile: str = typer.Option(...), ota: str = typer.Option("booking_com"),
             run: bool = typer.Option(False, help="Actually send goals to TinyFish (needs TINYFISH_API_KEY)")):
    """Generate plain-English fill goals from the profile (AUTO steps only).
    With --run and a TINYFISH_API_KEY set, sends each goal to TinyFish."""
    from accounts_pilot.drivers import TinyFishDriver, booking_goals
    from accounts_pilot.config import settings

    p = _load_profile(profile)
    goals = booking_goals(p)
    console.print(f"[bold]{len(goals)} fill goals[/] for {ota} / {p.display_name}:\n")
    for g in goals:
        console.print(f"[cyan]{g['step']}[/]: {g['goal']}")

    if not run:
        console.print("\n[dim]Dry run — pass --run (with TINYFISH_API_KEY) to send these to TinyFish.[/]")
        return
    driver = TinyFishDriver()
    if not driver.ready:
        console.print("\n[red]TINYFISH_API_KEY not set[/] — add it to .env to --run.")
        raise typer.Exit(1)
    from accounts_pilot.drivers.tinyfish import ADMIN_URL
    for g in goals:
        console.print(f"\n[yellow]→ {g['step']}[/]")
        try:
            res = driver.run_goal(ADMIN_URL, g["goal"])
            console.print(f"  {res}")
        except Exception as e:
            console.print(f"  [red]failed:[/] {e}")
    console.print("\n[green]Done.[/] Gates (account/CAPTCHA/OTP/bank/contract) are owner-handled.")


@app.command(name="agentql-login")
def agentql_login(url: str = typer.Option("https://admin.booking.com/"),
                  user_data_dir: str = typer.Option("data/booking_profile")):
    """One-time: open a persistent browser, YOU log into Booking (email, password,
    CAPTCHA, OTP) in the window, then press Enter to save the session. AgentQL fills
    reuse it — so no CAPTCHA ever appears again."""
    from playwright.sync_api import sync_playwright
    Path(user_data_dir).mkdir(parents=True, exist_ok=True)
    console.print(f"[bold]Opening {url}[/] — log in (email + password + CAPTCHA + OTP) in the window.")
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(user_data_dir=user_data_dir, headless=False)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(url)
        input("\nWhen you're logged in and can see your property dashboard, press Enter to save & close… ")
        ctx.close()
    console.print(f"[green]Session saved to {user_data_dir}[/] — agentql-fill reuses it now.")


@app.command(name="agentql-fill")
def agentql_fill(config: str = typer.Option(..., help="path to a form-config JSON file"),
                 property_id: str = typer.Option(None, "--property", help="inject stored property data into @refs"),
                 dry_run: bool = typer.Option(False, "--dry-run", help="fill but don't submit")):
    """Fill a form locally (visible Chromium). AgentQL resolves fields via cloud on the
    EMPTY form; all typing/clicking/submitting runs locally. Config-driven, nothing hardcoded."""
    import os
    from accounts_pilot.config import settings
    from accounts_pilot.drivers.agentql_local import load_config, resolve_property_refs, fill_form

    if settings.agentql_api_key and not os.environ.get("AGENTQL_API_KEY"):
        os.environ["AGENTQL_API_KEY"] = settings.agentql_api_key
    if not os.environ.get("AGENTQL_API_KEY"):
        console.print("[red]AGENTQL_API_KEY not set[/] — add it to .env or the environment.")
        raise typer.Exit(1)

    cfg = load_config(json.loads(Path(config).read_text(encoding="utf-8")))
    if property_id:
        from accounts_pilot.sources import BookingEngineSource
        cfg = resolve_property_refs(cfg, BookingEngineSource().get(property_id))
        console.print(f"[dim]injected stored data from {property_id}[/]")
    console.print(f"[bold]Filling[/] {cfg.url}  (dry_run={dry_run})")
    ok = fill_form(cfg, dry_run=dry_run)
    console.print("[green]Done — success.[/]" if ok else "[red]Done — failed (see screenshot).[/]")
    raise typer.Exit(0 if ok else 1)


@app.command(name="tf-login")
def tf_login(name: str = typer.Option("booking", help="profile name"),
             url: str = typer.Option("https://admin.booking.com/")):
    """YOU log in once — TinyFish saves your Booking session so the fill runs authenticated.
    Opens a TinyFish setup browser; you handle email + password + CAPTCHA + OTP there."""
    from accounts_pilot.drivers import TinyFishDriver
    from accounts_pilot.config import settings
    d = TinyFishDriver()
    if not d.ready:
        console.print("[red]TINYFISH_API_KEY not set.[/]"); raise typer.Exit(1)
    pid = settings.tinyfish_profile_id
    if not pid:
        pid = d.create_profile(name)
        console.print(f"[green]Created profile[/] {pid}")
    s = d.setup_session(pid, url)
    console.print("[bold]A setup browser is starting.[/] Log into Booking.com there — "
                  "do the email + password + CAPTCHA + OTP yourself.")
    console.print(f"[dim]session info: {s}[/]")
    sid = s.get("session_id") or s.get("sessionId") or s.get("id")
    input("\nWhen you are fully logged in (you can see your property dashboard), press Enter to save… ")
    d.save_session(pid, sid)
    console.print(f"[green]Session saved.[/] Add this to .env →  TINYFISH_PROFILE_ID={pid}")


@app.command(name="tf-fill")
def tf_fill(property_id: str = typer.Option("UDR-001"),
            url: str = typer.Option("https://admin.booking.com/")):
    """Fill Booking.com with a property's data via TinyFish, using your saved login session.
    Run `tf-login` first. Logs each fill step so you can watch."""
    from accounts_pilot.sources import BookingEngineSource
    from accounts_pilot.drivers import TinyFishDriver, booking_goals
    from accounts_pilot.config import settings
    d = TinyFishDriver()
    if not d.ready:
        console.print("[red]TINYFISH_API_KEY not set.[/]"); raise typer.Exit(1)
    if not settings.tinyfish_profile_id:
        console.print("[yellow]No TINYFISH_PROFILE_ID[/] — run `tf-login` first so the fill is authenticated.")
    p = BookingEngineSource().get(property_id)
    goals = booking_goals(p)
    console.print(f"[bold]Filling {p.display_name} on Booking.com[/] via TinyFish "
                  f"(profile {settings.tinyfish_profile_id or 'default'})\n")
    for g in goals:
        console.print(f"[yellow]→ {g['step']}[/]: {g['goal'][:80]}…")
        try:
            res = d.run_goal(url, g["goal"], use_profile=True, timeout_s=240)
            r = (res.get("result") or {}).get("result")
            console.print(f"  [green]{res.get('status')}[/] · {res.get('num_of_steps')} steps"
                          + (f" · {str(r)[:160]}" if r else ""))
        except Exception as e:
            console.print(f"  [red]failed:[/] {type(e).__name__}: {e}")
    console.print("\n[green]Fill run complete.[/] Then YOU do bank + contract + submit on Booking.")


@app.command()
def fields(profile: str = typer.Option(...), ota: str = typer.Option("booking_com")):
    """Show every value the adapter will map from the profile, per step (no browser)."""
    p = _load_profile(profile)
    inspect_fields(p, ota)


@app.command()
def onboard(profile: str = typer.Option(...), ota: str = typer.Option("booking_com")):
    """Run the AUTO steps live; park at the first gate."""
    p = _load_profile(profile)
    store = JobStore()
    jid = _job_id(p.property_id, ota)
    job = store.get(jid) or OnboardingJob(job_id=jid, property_id=p.property_id, ota=ota)
    job = run_job(job, p, store)
    _print_job(job)


@app.command()
def resume(job_id: str, profile: str = typer.Option(...)):
    """Resume a parked job (after a human cleared the gate)."""
    p = _load_profile(profile)
    store = JobStore()
    job = store.get(job_id)
    if not job:
        console.print(f"[red]no such job:[/] {job_id}")
        raise typer.Exit(1)
    job = run_job(job, p, store)
    _print_job(job)


@app.command()
def status(job_id: str = typer.Argument(None)):
    """List all jobs, or show one job with its audit trail."""
    store = JobStore()
    if job_id is None:
        jobs = store.list()
        if not jobs:
            console.print("[dim]no jobs yet[/]")
            return
        t = Table("job_id", "ota", "state", "step", "waiting_on", "updated")
        for j in jobs:
            t.add_row(j.job_id, j.ota, j.state.value, j.current_step or "—",
                      j.waiting_on.value if j.waiting_on else "—", j.updated_at[:19])
        console.print(t)
        return

    job = store.get(job_id)
    if not job:
        console.print(f"[red]no such job:[/] {job_id}")
        raise typer.Exit(1)
    _print_job(job)
    audit = AuditLog(store.db_path)
    t = Table("at", "step", "action", "detail")
    for e in audit.for_job(job_id):
        t.add_row(e["at"][:19], e["step"] or "—", e["action"], e["detail"] or "")
    console.print(t)


def _print_job(job: OnboardingJob):
    color = "yellow" if job.waiting_on else "green"
    console.print(f"[{color}]{job.job_id}[/]  state=[bold]{job.state.value}[/]  "
                  f"step={job.current_step or '—'}  waiting_on={job.waiting_on.value if job.waiting_on else '—'}")


if __name__ == "__main__":
    app()
