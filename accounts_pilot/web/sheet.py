"""Flatten a PropertyProfile into a labeled, copy-ready registration sheet.

This is the reliable path: the stored data, shown field-by-field with copy
buttons, so the operator pastes each value into the OTA wizard. No AI, no
selectors, nothing to break — just the data on tap.
"""
from __future__ import annotations

from accounts_pilot.models.property_profile import PropertyProfile


def sheet_fields(p: PropertyProfile) -> list[dict]:
    """Return [{section, label, value}] — every value an operator types to register."""
    f: list[dict] = []

    def add(section, label, value):
        if value is None or value == "":
            return
        f.append({"section": section, "label": label, "value": str(value)})

    # identity
    add("Property", "Name", p.display_name)
    add("Property", "Type", p.property_type.value.replace("_", " ").title())
    add("Property", "Star rating", p.star_rating)
    add("Property", "Setting", p.location_type.value.replace("_", " ") if p.location_type else None)
    add("Property", "Description", p.description)
    add("Property", "Total rooms", p.total_rooms)

    # address
    a = p.address
    add("Address", "Street", a.line1)
    add("Address", "Line 2", a.line2)
    add("Address", "City", a.city)
    add("Address", "State/Region", a.state)
    add("Address", "Postal code", a.postal_code)
    add("Address", "Country", a.country)
    add("Address", "Latitude", a.latitude)
    add("Address", "Longitude", a.longitude)

    # rooms
    for r in p.room_types:
        beds = ", ".join(f"{b.count}x {b.bed_type.value}" for b in r.beds)
        add(f"Room: {r.name}", "Count", r.count)
        add(f"Room: {r.name}", "Max adults", r.max_adults)
        add(f"Room: {r.name}", "Max children", r.max_children)
        add(f"Room: {r.name}", "Beds", beds)
        add(f"Room: {r.name}", "Bathroom", r.bathroom.value)
        add(f"Room: {r.name}", "Size (sqm)", r.size_sqm)
        add(f"Room: {r.name}", "Rate", f"{r.base_rate:.0f} {r.currency}")

    # facilities
    fac = p.facilities
    on = [n for n, v in {
        "Free parking": fac.parking.available and fac.parking.type.value == "free",
        "Paid parking": fac.parking.available and fac.parking.type.value == "paid",
        "Breakfast": fac.breakfast.available, "Free WiFi": fac.internet.wifi and fac.internet.free,
        "Swimming pool": fac.swimming_pool, "Spa": fac.spa, "Fitness center": fac.fitness_center,
        "Restaurant": fac.restaurant, "Bar": fac.bar, "Room service": fac.room_service,
        "Airport shuttle": fac.airport_shuttle, "Laundry": fac.laundry,
        "Elevator": fac.elevator, "Family rooms": fac.family_rooms,
    }.items() if v]
    add("Facilities", "Enabled", ", ".join(on))
    add("Facilities", "Languages", ", ".join(fac.languages_spoken))

    # policies
    pol = p.policy
    add("Policies", "Check-in from", pol.checkin_from)
    add("Policies", "Check-out until", pol.checkout_until)
    add("Policies", "Cancellation", pol.cancellation_tier.value
        + (f" (free until {pol.free_cancellation_until_hours}h)" if pol.free_cancellation_until_hours else ""))
    add("Policies", "Prepayment", pol.prepayment.value)
    add("Policies", "Min check-in age", pol.min_checkin_age)
    add("Policies", "Smoking", "Allowed" if pol.smoking_allowed else "Not allowed")
    add("Policies", "Pets", "Allowed" if pol.pets_allowed else "Not allowed")
    add("Policies", "Payment methods", ", ".join(pol.payment_methods))

    # contact + tax
    add("Contact", "Name", p.contact.full_name)
    add("Contact", "Email", p.contact.email)
    add("Contact", "Phone", p.contact.phone)
    cm = p.compliance
    add("Tax / Legal", "Legal entity", cm.legal_entity_name)
    add("Tax / Legal", "Business type", cm.business_type.value)
    add("Tax / Legal", "GSTIN", cm.gstin)
    add("Tax / Legal", "PAN", cm.pan)

    return f
