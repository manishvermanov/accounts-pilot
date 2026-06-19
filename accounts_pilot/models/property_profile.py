"""The canonical, OTA-agnostic property data model.

Expanded to cover Booking.com's full "List your property" field set:
property category + single-vs-group, star rating, location/setting type, a deep
facilities taxonomy, granular room/bed/bathroom config, and a full policy block
(prepayment, cancellation tier, child policy, payment methods, etc.).

Fill this once per hotel. Every OTA adapter reads from it and maps it into that
OTA's specific wizard fields. OTA-specific shapes (Booking.com's facility codes,
their bed-type enums) live in the adapter, NOT here.

New fields are optional with sensible defaults so older/simpler profiles still
validate. The provenance of the Booking.com field list is documented in
docs/BOOKING-FIELDS.md — it is reconciled against the live wizard during the
selector-capture pass.
"""
from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, EmailStr, Field, field_validator


# --------------------------------------------------------------------------- #
# enums
# --------------------------------------------------------------------------- #
class ListingScope(str, Enum):
    SINGLE_PROPERTY = "single_property"     # one hotel with multiple rooms
    PROPERTY_GROUP = "property_group"       # multiple distinct hotels (chain/group)


class PropertyType(str, Enum):
    HOTEL = "hotel"
    APARTMENT = "apartment"
    APARTHOTEL = "aparthotel"
    GUESTHOUSE = "guesthouse"
    HOMESTAY = "homestay"
    BNB = "bnb"
    HOSTEL = "hostel"
    RESORT = "resort"
    VILLA = "villa"
    HOLIDAY_HOME = "holiday_home"


class LocationType(str, Enum):
    CITY_CENTRE = "city_centre"
    SUBURB = "suburb"
    BEACH = "beach"
    LAKESIDE = "lakeside"
    COUNTRYSIDE = "countryside"
    MOUNTAIN = "mountain"
    NEAR_AIRPORT = "near_airport"
    NEAR_STATION = "near_station"


class BusinessType(str, Enum):
    BUSINESS = "business"
    INDIVIDUAL = "individual"


class BedType(str, Enum):
    SINGLE = "single"
    DOUBLE = "double"
    QUEEN = "queen"
    KING = "king"
    TWIN = "twin"
    BUNK = "bunk"
    SOFA_BED = "sofa_bed"


class Bathroom(str, Enum):
    PRIVATE = "private"
    SHARED = "shared"


class ParkingType(str, Enum):
    NONE = "none"
    FREE = "free"
    PAID = "paid"


class BreakfastType(str, Enum):
    NONE = "none"
    CONTINENTAL = "continental"
    BUFFET = "buffet"
    A_LA_CARTE = "a_la_carte"
    ASIAN = "asian"
    VEGETARIAN = "vegetarian"
    FULL_ENGLISH = "full_english"


class InternetCoverage(str, Enum):
    NONE = "none"
    ROOMS = "rooms"
    PUBLIC_AREAS = "public_areas"
    ALL_AREAS = "all_areas"


class CancellationTier(str, Enum):
    FLEXIBLE = "flexible"
    MODERATE = "moderate"
    STRICT = "strict"
    NON_REFUNDABLE = "non_refundable"


class Prepayment(str, Enum):
    NONE = "none"
    PARTIAL = "partial"
    FULL = "full"


# --------------------------------------------------------------------------- #
# sub-models
# --------------------------------------------------------------------------- #
class Address(BaseModel):
    line1: str
    line2: Optional[str] = None
    city: str
    state: str
    country: str = "IN"
    postal_code: str
    latitude: Optional[float] = None        # drives the map pin without geocoding
    longitude: Optional[float] = None


class Contact(BaseModel):
    full_name: str
    email: EmailStr
    phone: str                              # E.164 preferred, e.g. +919812345678


class Compliance(BaseModel):
    legal_entity_name: str
    business_type: BusinessType = BusinessType.BUSINESS
    gstin: Optional[str] = None             # 15-char GST id (India)
    pan: Optional[str] = None               # 10-char PAN (India)
    business_registration_number: Optional[str] = None
    gst_registered: bool = False
    # owner / authorised-signatory KYC — OTAs collect this; the MIS usually doesn't
    owner_kind: Optional[str] = None        # individual | individual_running_a_business | company
    owner_first_name: Optional[str] = None
    owner_last_name: Optional[str] = None
    owner_dob: Optional[str] = None         # DD-MM-YYYY
    owner_nationality: Optional[str] = None
    # licenses OTAs may request
    tan: Optional[str] = None
    fssai_license: Optional[str] = None     # food-serving properties
    fire_safety_certificate: Optional[str] = None
    trade_license: Optional[str] = None
    msme_udyam: Optional[str] = None

    @field_validator("gstin")
    @classmethod
    def _gstin_len(cls, v):
        if v and len(v) != 15:
            raise ValueError("GSTIN must be 15 characters")
        return v

    @field_validator("pan")
    @classmethod
    def _pan_len(cls, v):
        if v and len(v) != 10:
            raise ValueError("PAN must be 10 characters")
        return v


class BedConfig(BaseModel):
    bed_type: BedType
    count: int = Field(ge=1)


class RatePlan(BaseModel):
    """A sellable meal plan for a room. code: EP=Room Only, CP=Breakfast,
    MAP=Breakfast+1 meal, AP=All meals. price is the per-night total for this plan."""
    code: str                               # "EP" | "CP" | "MAP" | "AP"
    name: str                               # display name, e.g. "Room Only", "Breakfast"
    price: float = Field(gt=0)              # per-night total, in the room's currency


class RoomType(BaseModel):
    name: str                               # e.g. "Deluxe Double"
    count: int = Field(ge=1)                # how many of this room exist
    max_adults: int = Field(ge=1)
    max_children: int = Field(ge=0, default=0)
    base_occupancy: Optional[int] = None    # min/standard adults the base_rate covers
    base_child_occupancy: Optional[int] = None
    max_occupancy: Optional[int] = None     # total adults+children cap (OTAs ask explicitly)
    beds: list[BedConfig] = Field(default_factory=list)
    bathroom: Bathroom = Bathroom.PRIVATE
    size_sqm: Optional[float] = None
    view: Optional[str] = None              # sea | city | garden | pool | mountain
    description: Optional[str] = None
    base_rate: float = Field(gt=0)          # per-night, in `currency` (the EP / Room-Only rate)
    currency: str = "INR"
    extra_adult_charge: Optional[float] = None
    extra_child_charge: Optional[float] = None
    smoking: bool = False
    extra_bed_available: bool = False
    room_amenities: list[str] = Field(default_factory=list)  # ac, tv, safe, minibar, balcony…
    rate_plans: list[RatePlan] = Field(default_factory=list)  # EP/CP/MAP/AP meal plans + prices


class Reception(BaseModel):
    is_24h: bool = True
    hours_from: Optional[str] = None        # e.g. "07:00" when not 24h
    hours_until: Optional[str] = None


class Parking(BaseModel):
    available: bool = False
    type: ParkingType = ParkingType.NONE
    on_site: bool = True
    reservation_needed: bool = False


class Breakfast(BaseModel):
    available: bool = False
    included_in_rate: bool = False
    type: BreakfastType = BreakfastType.NONE
    price_per_person: Optional[float] = None
    currency: str = "INR"


class Internet(BaseModel):
    wifi: bool = True
    free: bool = True
    coverage: InternetCoverage = InternetCoverage.ALL_AREAS


class Facilities(BaseModel):
    """Booking.com's facilities/amenities taxonomy, structured."""
    parking: Parking = Field(default_factory=Parking)
    breakfast: Breakfast = Field(default_factory=Breakfast)
    internet: Internet = Field(default_factory=Internet)
    # property-level booleans
    swimming_pool: bool = False
    spa: bool = False
    fitness_center: bool = False
    restaurant: bool = False
    bar: bool = False
    room_service: bool = False
    airport_shuttle: bool = False
    laundry: bool = False
    business_center: bool = False
    ev_charging: bool = False
    elevator: bool = False
    family_rooms: bool = False
    air_conditioning: bool = False
    power_backup: bool = False
    check_in_method: Optional[str] = None      # reception_24h | self_check_in | lockbox | host
    # lists
    languages_spoken: list[str] = Field(default_factory=list)      # English, Hindi…
    accessibility: list[str] = Field(default_factory=list)         # wheelchair_accessible…
    other: list[str] = Field(default_factory=list)                 # long-tail / unmapped


class Photo(BaseModel):
    path: Optional[str] = None              # local file path
    url: Optional[str] = None               # remote url
    caption: Optional[str] = None
    room_type: Optional[str] = None         # link a photo to a specific room type

    @field_validator("url")
    @classmethod
    def _one_source(cls, v, info):
        if not v and not info.data.get("path"):
            raise ValueError("Photo needs either `path` or `url`")
        return v


class ChildPolicy(BaseModel):
    children_allowed: bool = True
    min_age: Optional[int] = None
    free_stay_under_age: Optional[int] = None
    extra_bed_fee: Optional[float] = None
    crib_available: bool = False
    crib_fee: Optional[float] = None


class Policy(BaseModel):
    checkin_from: str = "14:00"
    checkin_until: Optional[str] = None      # latest check-in time, e.g. "23:00"
    checkout_from: Optional[str] = None      # earliest check-out time, e.g. "00:00"
    checkout_until: str = "11:00"
    early_checkin_fee: Optional[float] = None
    late_checkout_fee: Optional[float] = None
    cancellation_tier: CancellationTier = CancellationTier.FLEXIBLE
    free_cancellation_until_hours: Optional[int] = None   # e.g. 24
    prepayment: Prepayment = Prepayment.NONE
    min_checkin_age: Optional[int] = None
    couple_friendly: bool = False
    unmarried_couples_allowed: bool = False
    local_ids_accepted: bool = True
    accepted_ids: list[str] = Field(default_factory=list)  # Aadhaar, Passport, Driving License, Voter ID
    smoking_allowed: bool = False
    quiet_hours_from: Optional[str] = None
    quiet_hours_until: Optional[str] = None
    pets_allowed: bool = False
    pet_fee: Optional[float] = None
    payment_methods: list[str] = Field(default_factory=list)   # cash, visa, mastercard, upi…
    deposit_required: bool = False           # OTA 'Do you require any deposits?' (Expedia, etc.)
    cancellation_cutoff_time: Optional[str] = None   # local cutoff on check-in day, e.g. "18:00"
    cancellation_fee_type: Optional[str] = None      # first_night | fifty_percent | full_stay
    house_rules: list[str] = Field(default_factory=list)
    child_policy: ChildPolicy = Field(default_factory=ChildPolicy)


class Nearby(BaseModel):
    """Surroundings OTAs ask about (distances drive search relevance)."""
    nearest_airport: Optional[str] = None
    distance_to_airport_km: Optional[float] = None
    nearest_railway_station: Optional[str] = None
    distance_to_railway_km: Optional[float] = None
    nearest_bus_stand: Optional[str] = None
    distance_to_bus_km: Optional[float] = None
    points_of_interest: list[str] = Field(default_factory=list)


class Payout(BaseModel):
    """Reference payout details for the operator's records / the gated bank step.

    SECURITY: the bank ACCOUNT NUMBER is deliberately NOT modeled. Entering it is a
    human-only gate — the engine never auto-submits a bank/card account number.
    These routing fields just let the operator note where money should land.
    """
    account_holder_name: Optional[str] = None
    bank_name: Optional[str] = None
    branch: Optional[str] = None
    ifsc: Optional[str] = None
    account_type: Optional[str] = None      # savings | current
    upi_id: Optional[str] = None


class TaxLine(BaseModel):
    """A tax/fee the property includes in its rates (Expedia: City/Federal/Occupancy/District/
    Hotel/GST/HST/VAT; India is typically GST). Leave the `taxes` list EMPTY to include no
    taxes in the rate (the OTA tax switches stay off)."""
    name: str                                # "GST", "City tax", "VAT", "Occupancy tax", …
    basis: str = "percent_per_stay"          # percent_per_stay | amount_per_stay | amount_per_night
    rate: float = 0                          # percent (0.001–100) or a fixed amount


# --------------------------------------------------------------------------- #
# root
# --------------------------------------------------------------------------- #
class PropertyProfile(BaseModel):
    """One hotel's complete onboarding profile."""

    property_id: str
    listing_scope: ListingScope = ListingScope.SINGLE_PROPERTY
    property_type: PropertyType
    display_name: str
    star_rating: Optional[int] = Field(default=None, ge=0, le=5)   # 0/None = unrated
    location_type: Optional[LocationType] = None
    description: Optional[str] = None
    property_email: Optional[str] = None         # public-facing property contact
    property_phone: Optional[str] = None
    website: Optional[str] = None
    chain_name: Optional[str] = None             # brand/chain affiliation, if any
    year_built: Optional[str] = None
    year_renovated: Optional[str] = None
    floors: Optional[int] = None
    total_room_count: Optional[int] = None       # operator-set physical inventory (MIS lacks it)

    address: Address
    contact: Contact
    compliance: Compliance
    nearby: Nearby = Field(default_factory=Nearby)
    payout: Payout = Field(default_factory=Payout)

    room_types: list[RoomType] = Field(min_length=1)

    facilities: Facilities = Field(default_factory=Facilities)
    amenities: list[str] = Field(default_factory=list)   # legacy/simple long-tail (optional)
    photos: list[Photo] = Field(default_factory=list)
    policy: Policy = Field(default_factory=Policy)

    reception: Reception = Field(default_factory=Reception)
    currency: str = "INR"
    timezone: Optional[str] = None               # IANA tz, e.g. "Asia/Kolkata" — drives OTA tz pickers
    billing_currency: Optional[str] = None       # OTA payout/billing currency; falls back to `currency`
    taxes: list[TaxLine] = Field(default_factory=list)   # taxes included in rates (empty = none)
    opening_date: Optional[str] = None           # ISO date the property opened / will open
    is_currently_open: bool = True

    @property
    def total_rooms(self) -> int:
        return sum(rt.count for rt in self.room_types)
