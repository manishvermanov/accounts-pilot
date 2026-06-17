"""Booking.com adapter — the v1 target.

This encodes Booking.com's "List your property" wizard as a step graph. The
AUTO steps map the PropertyProfile into Booking.com's fields; the GATE steps
raise GateRequired so the state machine parks the job for a human (or an
auto-resolver) to pass.

The step graph mirrors the real Booking.com segments: listing scope (single vs
group) → property category → property details (name, star rating, setting) →
location → rooms → rates → facilities → photos → policies → contact → tax →
[bank] → [contract] → submit.

⚠️  SELECTORS ARE PLACEHOLDERS. They get filled in by walking the live wizard
and capturing the real DOM (the next task). Every `# TODO(selector)` marks one.
The mapping LOGIC is real and shows intent (visible in the dry-run plan).
See docs/BOOKING-FIELDS.md for the full field inventory.
"""
from __future__ import annotations

from accounts_pilot.adapters.base import GateRequired, OTAAdapter, Step, StepKind
from accounts_pilot.models.job import GateKind
from accounts_pilot.models.property_profile import PropertyProfile
from accounts_pilot.runtime.browser import BrowserRuntime

JOIN_URL = "https://join.booking.com/"


class BookingComAdapter(OTAAdapter):
    ota = "booking_com"
    display_name = "Booking.com"

    def steps(self) -> list[Step]:
        return [
            Step("account",   "Create partner account",          StepKind.GATE,
                 gate=GateKind.ACCOUNT, needs_stealth=True, url=JOIN_URL),
            Step("verify",    "Verify email / phone (OTP)",       StepKind.GATE,
                 gate=GateKind.OTP),
            Step("scope",     "Single property or group",         StepKind.AUTO),
            Step("prop_type", "Property category",                StepKind.AUTO),
            Step("details",   "Name, star rating, setting",       StepKind.AUTO),
            Step("location",  "Address + map pin",                StepKind.AUTO),
            Step("rooms",     "Room types, beds, occupancy",      StepKind.AUTO),
            Step("rates",     "Base rates & currency",            StepKind.AUTO),
            Step("facilities", "Facilities & amenities",          StepKind.AUTO),
            Step("photos",    "Photos",                           StepKind.AUTO),
            Step("policies",  "Policies (check-in, cxl, child)",  StepKind.AUTO),
            Step("contact",   "Contact person",                   StepKind.AUTO),
            Step("tax",       "Tax / GST / legal entity",         StepKind.AUTO),
            Step("bank",      "Payout / bank account",            StepKind.GATE,
                 gate=GateKind.BANK),
            Step("contract",  "Partner contract",                 StepKind.GATE,
                 gate=GateKind.CONTRACT),
            Step("submit",    "Submit for review",                StepKind.SYSTEM),
        ]

    # ---- login + capture walk (the service drives this with creds) ---- #
    def login(self, rt: BrowserRuntime, email: str, password: str) -> str:
        """Drive join → register → email → password. Returns 'ok' | 'captcha' | 'verification'.

        The service fills credentials and proceeds. It stops only where Booking.com
        forces a human/solver (CAPTCHA or OTP) — that is Booking's wall, handled by the
        gate handler (2Captcha solver or a one-time human tap), not a code gap.
        """
        rt.goto(JOIN_URL)
        rt.dump_capture("00_landing")
        rt.click_text("Get started now") or rt.click_text("Get started today")
        rt.think()
        rt.dump_capture("01_after_get_started")

        # Already logged in (persisted session)? Then there's no email field —
        # skip the whole login and go straight to capturing the wizard.
        if not rt.has("#login_name_register", timeout_ms=5000):
            rt.dump_capture("01_already_logged_in")
            ch = rt.detect_challenge()
            return ch if ch else "ok"

        # email (selector CAPTURED live)
        rt.fill("#login_name_register", email)
        rt.try_advance() or rt.click("form button[type='submit']")
        rt.think()

        ch = rt.detect_challenge()
        if ch:
            rt.dump_capture(f"02_{ch}")
            return ch

        # password page (generic — refined from the capture dump)
        rt.dump_capture("02_password")
        if rt.has("input[type='password']", timeout_ms=4000):
            rt.fill("input[type='password']", password)
            rt.try_advance() or rt.click("form button[type='submit']")
            rt.think()

        ch = rt.detect_challenge()
        if ch:
            rt.dump_capture(f"03_{ch}")
            return ch
        rt.dump_capture("03_post_login")
        return "ok"

    def capture_walk(self, rt: BrowserRuntime, max_pages: int = 25) -> list[str]:
        """After login, drill page-by-page through the wizard, dumping each page's DOM so
        the remaining selectors can be harvested. Does NOT fill property data (no selectors
        yet) and NEVER clicks a final submit. Stops at a challenge or when no 'next' exists."""
        dumped: list[str] = []
        for i in range(max_pages):
            label = f"wiz_{i:02d}"
            dumped.append(rt.dump_capture(label))
            ch = rt.detect_challenge()
            if ch:
                print(f"  [capture] challenge '{ch}' — hand off to operator, then re-run capture")
                break
            # never auto-advance past the money/legal gates
            body = (rt.page.inner_text("body") if rt.page else "") or ""
            low = body.lower()
            if any(k in low for k in ("bank", "payout", "iban", "agreement", "commission", "contract")):
                print("  [capture] reached a money/legal gate — stopping (human-only).")
                break
            if not rt.try_advance():
                print("  [capture] no further 'next' button — wizard end or manual step.")
                break
        return dumped

    # ------------------------------------------------------------------ #
    def run_step(self, step: Step, rt: BrowserRuntime, profile: PropertyProfile) -> None:
        handler = getattr(self, f"_step_{step.key}", None)
        if handler is None:
            raise NotImplementedError(f"booking_com: no handler for step '{step.key}'")
        handler(rt, profile)

    # ---- GATE steps: raise so the engine parks the job ---------------- #
    def _step_account(self, rt: BrowserRuntime, profile: PropertyProfile) -> None:
        rt.goto(JOIN_URL)
        raise GateRequired(GateKind.ACCOUNT, "Create the Booking.com partner login.")

    def _step_verify(self, rt: BrowserRuntime, profile: PropertyProfile) -> None:
        raise GateRequired(GateKind.OTP, "Enter the verification code Booking.com sent.")

    def _step_bank(self, rt: BrowserRuntime, profile: PropertyProfile) -> None:
        raise GateRequired(GateKind.BANK, "Enter payout/bank details (human-only).")

    def _step_contract(self, rt: BrowserRuntime, profile: PropertyProfile) -> None:
        raise GateRequired(GateKind.CONTRACT, "Accept the partner agreement (human-only).")

    # ---- AUTO steps: fill from the profile ---------------------------- #
    # Selectors are placeholders. Mapping logic is real and shows intent.
    def _step_scope(self, rt: BrowserRuntime, profile: PropertyProfile) -> None:
        scope = "a group of properties" if profile.listing_scope.value == "property_group" else "a single property"
        _log("scope", f"listing {scope}")
        # rt.click(f'[data-scope="{profile.listing_scope.value}"]')  # TODO(selector)

    def _step_prop_type(self, rt: BrowserRuntime, profile: PropertyProfile) -> None:
        bc_type = _PROP_TYPE_MAP.get(profile.property_type.value, "hotel")
        _log("prop_type", bc_type)
        # rt.click(f'[data-property-type="{bc_type}"]')  # TODO(selector)

    def _step_details(self, rt: BrowserRuntime, profile: PropertyProfile) -> None:
        stars = f"{profile.star_rating}★" if profile.star_rating else "unrated"
        setting = _LOCATION_TYPE_MAP.get(
            profile.location_type.value if profile.location_type else "", "—")
        _log("details", f"name='{profile.display_name}'  stars={stars}  setting={setting}")
        # rt.fill('#property-name', profile.display_name)          # TODO(selector)
        # rt.select('#star-rating', str(profile.star_rating or 0)) # TODO(selector)
        # rt.select('#setting', setting)                           # TODO(selector)

    def _step_location(self, rt: BrowserRuntime, profile: PropertyProfile) -> None:
        a = profile.address
        _log("location", f"{a.line1}, {a.city} {a.postal_code}  pin=({a.latitude},{a.longitude})")
        # fill address fields; drop map pin from lat/long, else geocode  # TODO(selector)

    def _step_rooms(self, rt: BrowserRuntime, profile: PropertyProfile) -> None:
        for r in profile.room_types:
            beds = ", ".join(f"{b.count}x{b.bed_type.value}" for b in r.beds)
            _log("rooms", f"{r.name} x{r.count}  ≤{r.max_adults}ad/{r.max_children}ch  "
                          f"{r.bathroom.value} bath  beds[{beds}]")
            # add room → name/count/occupancy/bed config/bathroom  # TODO(selector)

    def _step_rates(self, rt: BrowserRuntime, profile: PropertyProfile) -> None:
        for r in profile.room_types:
            _log("rates", f"{r.name}: {r.base_rate} {r.currency}/night")

    def _step_facilities(self, rt: BrowserRuntime, profile: PropertyProfile) -> None:
        f = profile.facilities
        flags = [name for name, on in {
            "swimming_pool": f.swimming_pool, "spa": f.spa, "fitness_center": f.fitness_center,
            "restaurant": f.restaurant, "bar": f.bar, "room_service": f.room_service,
            "airport_shuttle": f.airport_shuttle, "laundry": f.laundry,
            "business_center": f.business_center, "ev_charging": f.ev_charging,
            "elevator": f.elevator, "family_rooms": f.family_rooms,
        }.items() if on]
        park = f"parking:{f.parking.type.value}" if f.parking.available else "parking:none"
        bfast = f"breakfast:{f.breakfast.type.value}" if f.breakfast.available else "breakfast:none"
        wifi = f"wifi:{'free' if f.internet.free else 'paid'}/{f.internet.coverage.value}" if f.internet.wifi else "wifi:none"
        _log("facilities", f"{park}  {bfast}  {wifi}  flags[{', '.join(flags)}]  "
                           f"langs[{', '.join(f.languages_spoken)}]")
        # check each facility code  # TODO(selector)

    def _step_photos(self, rt: BrowserRuntime, profile: PropertyProfile) -> None:
        files = [p.path for p in profile.photos if p.path]
        _log("photos", f"{len(files)} file(s)")
        # if files: rt.upload('input[type=file]', files)  # TODO(selector)

    def _step_policies(self, rt: BrowserRuntime, profile: PropertyProfile) -> None:
        p = profile.policy
        cxl = p.cancellation_tier.value
        if p.free_cancellation_until_hours:
            cxl += f" (free until {p.free_cancellation_until_hours}h)"
        child = "kids ok" if p.child_policy.children_allowed else "no kids"
        _log("policies", f"in {p.checkin_from}/out {p.checkout_until}  cxl={cxl}  "
                         f"prepay={p.prepayment.value}  {child}  pay[{', '.join(p.payment_methods)}]")

    def _step_contact(self, rt: BrowserRuntime, profile: PropertyProfile) -> None:
        c = profile.contact
        _log("contact", f"{c.full_name} / {c.email} / {c.phone}")

    def _step_tax(self, rt: BrowserRuntime, profile: PropertyProfile) -> None:
        cm = profile.compliance
        _log("tax", f"{cm.legal_entity_name} [{cm.business_type.value}]  "
                    f"GSTIN={cm.gstin or '—'}  PAN={cm.pan or '—'}")

    def _step_submit(self, rt: BrowserRuntime, profile: PropertyProfile) -> None:
        _log("submit", "submitting listing for Booking.com review")
        # rt.click('#submit-listing')  # TODO(selector)


# --- Profile -> Booking.com value maps (extend as we learn the real enums) --- #
_PROP_TYPE_MAP = {
    "hotel": "hotel", "apartment": "apartment", "aparthotel": "aparthotel",
    "guesthouse": "guest_house", "homestay": "homestay", "bnb": "bed_and_breakfast",
    "hostel": "hostel", "resort": "resort", "villa": "villa", "holiday_home": "holiday_home",
}

_LOCATION_TYPE_MAP = {
    "city_centre": "City centre", "suburb": "Suburb", "beach": "Beachfront",
    "lakeside": "Lakeside", "countryside": "Countryside", "mountain": "Mountain",
    "near_airport": "Near airport", "near_station": "Near station",
}


def _log(step: str, detail: str) -> None:
    print(f"  [booking_com:{step}] {detail}")
