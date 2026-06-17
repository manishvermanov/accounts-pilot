from accounts_pilot.adapters.base import OTAAdapter, Step, StepKind, GateRequired
from accounts_pilot.adapters.booking_com import BookingComAdapter
from accounts_pilot.adapters.makemytrip import MakeMyTripAdapter
from accounts_pilot.adapters.agoda import AgodaAdapter
from accounts_pilot.adapters.expedia import ExpediaAdapter
from accounts_pilot.adapters.airbnb import AirbnbAdapter

REGISTRY: dict[str, type[OTAAdapter]] = {
    "booking_com": BookingComAdapter,
    "makemytrip": MakeMyTripAdapter,
    "agoda": AgodaAdapter,
    "expedia": ExpediaAdapter,
    "airbnb": AirbnbAdapter,
}

# human labels for the UI, one per OTA
OTA_LABELS: dict[str, str] = {
    "booking_com": "Booking.com",
    "makemytrip": "MakeMyTrip",
    "agoda": "Agoda",
    "expedia": "Expedia",
    "airbnb": "Airbnb",
}


def get_adapter(ota: str) -> OTAAdapter:
    if ota not in REGISTRY:
        raise KeyError(f"No adapter for OTA '{ota}'. Known: {list(REGISTRY)}")
    return REGISTRY[ota]()


__all__ = ["OTAAdapter", "Step", "StepKind", "GateRequired", "get_adapter",
           "REGISTRY", "OTA_LABELS"]
