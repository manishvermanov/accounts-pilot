"""TinyFish driver — fill an OTA wizard with a cloud AI web-agent.

Instead of capturing selectors per OTA, we turn the PropertyProfile into a list of
plain-English *goals* and hand each to TinyFish, which drives the browser on its
cloud and adapts when the OTA reskins its pages.

What this driver does:  the AUTO steps (the property data — type, name, stars,
rooms, rates, facilities, photos, policies, contact, tax).
What it does NOT do:     the gates (account creation, CAPTCHA, OTP, bank, contract).
Those are owner-handled — TinyFish's stealth only *reduces* CAPTCHA appearances; by
its own docs it pairs with a solver and cannot reliably solve hard challenges.

API shape (verify against current docs at https://docs.tinyfish.ai):
  POST {base_url}             Authorization: Bearer <key>
  body {url, goal, browser_profile}
  → returns the agent run result.
"""
from __future__ import annotations

from typing import Optional

from accounts_pilot.config import settings
from accounts_pilot.models.property_profile import PropertyProfile

ADMIN_URL = "https://admin.booking.com/"


class TinyFishDriver:
    def __init__(self, *, api_key: Optional[str] = None, base_url: Optional[str] = None,
                 browser_profile: Optional[str] = None):
        self.api_key = api_key or settings.tinyfish_api_key
        self.base_url = base_url or settings.tinyfish_base_url
        self.browser_profile = browser_profile or settings.tinyfish_browser_profile
        self.ready = bool(self.api_key)

    @property
    def _headers(self) -> dict:
        return {"X-API-Key": self.api_key, "Content-Type": "application/json"}

    def _api_base(self) -> str:
        # base_url is …/v1/automation/run → strip to …/v1
        return self.base_url.rsplit("/automation/", 1)[0]

    def run_goal(self, url: str, goal: str, *, timeout_s: int = 180,
                 use_profile: bool = False, profile_id: str = "") -> dict:
        """Send one goal to TinyFish. With use_profile, runs inside your saved login session."""
        if not self.ready:
            raise RuntimeError("TINYFISH_API_KEY not set — cannot run goals.")
        import httpx
        payload = {"url": url, "goal": goal, "browser_profile": self.browser_profile}
        if use_profile:
            payload["use_profile"] = True
            pid = profile_id or settings.tinyfish_profile_id
            if pid:
                payload["profile_id"] = pid
        with httpx.Client(timeout=timeout_s) as client:
            resp = client.post(self.base_url, json=payload, headers=self._headers)
            resp.raise_for_status()
            return resp.json()

    # ---- BBU profiles (your saved Booking login session) ------------------
    def create_profile(self, name: str, *, proxy_country: str = "", set_default: bool = True) -> str:
        import httpx
        body = {"name": name, "set_as_default": set_default}
        if proxy_country:
            body["proxy_country_code"] = proxy_country
        with httpx.Client(timeout=60) as c:
            r = c.post(f"{self._api_base()}/profiles", json=body, headers=self._headers)
            r.raise_for_status()
            data = r.json()
        return data.get("profile_id") or data.get("id") or data.get("profileId")

    def setup_session(self, profile_id: str, url: str, *, timeout_seconds: int = 900) -> dict:
        """Start a setup browser. Returns {cdp_url, session_id} — YOU log in there."""
        import httpx
        with httpx.Client(timeout=120) as c:
            r = c.post(f"{self._api_base()}/profiles/{profile_id}/setup-session",
                       json={"url": url, "timeout_seconds": timeout_seconds}, headers=self._headers)
            r.raise_for_status()
            return r.json()

    def save_session(self, profile_id: str, session_id: str) -> dict:
        """Capture cookies/storage from the logged-in setup session into the profile."""
        import httpx
        with httpx.Client(timeout=60) as c:
            r = c.post(f"{self._api_base()}/profiles/{profile_id}/save",
                       json={"session_id": session_id}, headers=self._headers)
            r.raise_for_status()
            return r.json()


# --------------------------------------------------------------------------- #
#  Profile -> plain-English goals (driver-agnostic; reusable for any NL agent)
# --------------------------------------------------------------------------- #
def booking_goals(p: PropertyProfile) -> list[dict]:
    """Turn a PropertyProfile into ordered (step, goal) instructions for Booking.com.
    These are the AUTO steps only — gates are excluded by design."""
    f = p.facilities
    pol = p.policy

    def yes(names: dict[str, bool]) -> str:
        on = [k.replace("_", " ") for k, v in names.items() if v]
        return ", ".join(on) if on else "none"

    rooms = "; ".join(
        f"'{r.name}' x{r.count} (max {r.max_adults} adults/{r.max_children} children, "
        f"{', '.join(f'{b.count} {b.bed_type.value}' for b in r.beds)} bed, "
        f"{r.bathroom.value} bathroom, {r.base_rate:.0f} {r.currency}/night)"
        for r in p.room_types
    )
    facilities = yes({
        "free parking" if f.parking.type.value == "free" else "paid parking": f.parking.available,
        "breakfast": f.breakfast.available, "free wifi": (f.internet.wifi and f.internet.free),
        "swimming pool": f.swimming_pool, "fitness center": f.fitness_center,
        "restaurant": f.restaurant, "bar": f.bar, "room service": f.room_service,
        "airport shuttle": f.airport_shuttle, "laundry": f.laundry,
        "elevator": f.elevator, "family rooms": f.family_rooms,
    })

    steps = [
        ("scope", f"On the property-setup wizard, choose to list {'a group of properties' if p.listing_scope.value=='property_group' else 'a single property'}."),
        ("prop_type", f"Select the property category: {p.property_type.value.replace('_',' ').title()}."),
        ("details", f"Set the property name to '{p.display_name}'"
                    + (f", star rating to {p.star_rating}" if p.star_rating else ", no star rating")
                    + (f", and setting to {p.location_type.value.replace('_',' ')}." if p.location_type else ".")),
        ("location", f"Enter the address: {p.address.line1}, "
                     + (f"{p.address.line2}, " if p.address.line2 else "")
                     + f"{p.address.city}, {p.address.state} {p.address.postal_code}, {p.address.country}. "
                     + (f"Place the map pin at latitude {p.address.latitude}, longitude {p.address.longitude}."
                        if p.address.latitude else "Confirm the map location.")),
        ("rooms", f"Add these room types: {rooms}."),
        ("rates", "Set each room type's nightly rate as specified above."),
        ("facilities", f"Enable these facilities: {facilities}. "
                       + (f"Breakfast type: {f.breakfast.type.value}, {f.breakfast.price_per_person:.0f} {f.breakfast.currency} per person. " if f.breakfast.available else "")
                       + f"Languages spoken: {', '.join(f.languages_spoken) or 'English'}."),
        ("photos", f"Upload the {len([x for x in p.photos if x.path or x.url])} property photos provided."),
        ("policies", f"Set check-in from {pol.checkin_from}, check-out until {pol.checkout_until}. "
                     f"Cancellation: {pol.cancellation_tier.value}"
                     + (f" (free until {pol.free_cancellation_until_hours}h before)" if pol.free_cancellation_until_hours else "")
                     + f". Prepayment: {pol.prepayment.value}. "
                     + (f"Minimum check-in age {pol.min_checkin_age}. " if pol.min_checkin_age else "")
                     + f"Smoking: {'allowed' if pol.smoking_allowed else 'not allowed'}. "
                     + f"Pets: {'allowed' if pol.pets_allowed else 'not allowed'}. "
                     + f"Payment methods accepted: {', '.join(pol.payment_methods) or 'cash'}."),
        ("contact", f"Set the contact person to {p.contact.full_name}, email {p.contact.email}, phone {p.contact.phone}."),
        ("tax", f"Set the legal entity to '{p.compliance.legal_entity_name}' ({p.compliance.business_type.value})"
                + (f", GSTIN {p.compliance.gstin}" if p.compliance.gstin else "")
                + (f", PAN {p.compliance.pan}." if p.compliance.pan else ".")),
    ]
    return [{"step": k, "goal": g} for k, g in steps]
