"""Booking-engine source — where the service fetches property data FROM.

This stands in for your booking engine / PMS: the system of record that holds each
hotel's property data. The onboarding service pulls a PropertyProfile from here and
uses it to register on the OTAs.

Two backends, same interface:
  - local dir  (default): reads `*.json` from settings.booking_engine_dir
  - HTTP       : if settings.booking_engine_url is set, GET {url}/properties[/{id}]

Swap the backend by setting BOOKING_ENGINE_URL — no code change for callers.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from accounts_pilot.config import settings
from accounts_pilot.models.property_profile import PropertyProfile


class BookingEngineSource:
    def __init__(self, *, dir_path: Optional[str] = None, url: Optional[str] = None):
        self.dir = Path(dir_path or settings.booking_engine_dir)
        self.url = (url if url is not None else settings.booking_engine_url) or ""

    # ---- listing ----------------------------------------------------------
    def list_ids(self) -> list[str]:
        if self.url:
            import httpx
            with httpx.Client(timeout=30) as c:
                r = c.get(f"{self.url.rstrip('/')}/properties")
                r.raise_for_status()
                data = r.json()
            return [p["property_id"] if isinstance(p, dict) else p for p in data]
        return sorted(p.stem for p in self.dir.glob("*.json"))

    # ---- fetch one --------------------------------------------------------
    def get(self, property_id: str) -> PropertyProfile:
        if self.url:
            import httpx
            with httpx.Client(timeout=30) as c:
                r = c.get(f"{self.url.rstrip('/')}/properties/{property_id}")
                r.raise_for_status()
                payload = r.json()
        else:
            path = self.dir / f"{property_id}.json"
            if not path.exists():
                raise FileNotFoundError(f"booking engine has no property '{property_id}' ({path})")
            payload = json.loads(path.read_text(encoding="utf-8"))
        return PropertyProfile.model_validate(payload)

    # ---- fetch all --------------------------------------------------------
    def all(self) -> list[PropertyProfile]:
        return [self.get(i) for i in self.list_ids()]

    def describe(self) -> str:
        where = self.url or str(self.dir)
        return f"BookingEngine({'http' if self.url else 'local'} @ {where})"
