"""MIS search + convert: folder fallback, raw-export conversion, profile passthrough."""
import json

from accounts_pilot.mis import get_provider, normalize_to_profile, summarize
from accounts_pilot.mis.convert import is_raw_mis_record
from accounts_pilot.mis.provider import FolderMisProvider
from accounts_pilot.models.property_profile import PropertyProfile


# a complete raw MIS row (the DigiStay export shape: stringified sub-columns)
RAW = {
    "property_id": "TEST-001",
    "property_name": "HOTEL TEST PALACE",
    "property_type": "HOTEL",
    "owner_email": "owner@test.com",
    "currency": "INR",
    "checkin_time_ist": "12:00 PM",
    "checkout_time_ist": "11:00 AM",
    "billing_name": "HOTEL TEST PALACE",
    "service_gst": "22COIPS2490N1ZF",
    "terms_and_conditions": "Contact Number: +91 98765 43210 ...",
    "property_address": json.dumps({
        "street": "1 Test Road", "city": "Raipur", "state": "CHHATTISGARH",
        "pincode": "492001", "latitude": "21.2", "longitude": "81.6",
    }),
    "property_amenities": json.dumps([
        {"amenity_name": "WiFi"}, {"amenity_name": "Parking"}, {"amenity_name": "Restaurant"},
        {"amenity_name": "Room Service"}, {"amenity_name": "Elevator/Lift"},
    ]),
    "property_images": json.dumps([
        {"download_url": "https://x/img1.jpg", "tag": "Exterior"},
    ]),
    "room_types": json.dumps([
        {"room_type_name": "Deluxe", "size": "150 Sq. Ft", "base_price": 2000, "bed_count": 1,
         "bed_type": ["Double bed"], "base_occupancy": 2, "max_occupancy": 2,
         "max_child_occupancy": 0, "extra_adult_charge": 400,
         "amenities": [{"amenity_name": "Smart TV"}, {"amenity_name": "Geyser"}],
         "images": [{"download_url": "https://x/room1.jpg", "tag": "Room View"}]},
    ]),
}


def test_is_raw_detection():
    assert is_raw_mis_record(RAW) is True
    assert is_raw_mis_record({"display_name": "x", "room_types": []}) is False


def test_raw_to_profile_validates():
    prof = normalize_to_profile(RAW)               # validates internally
    PropertyProfile.model_validate(prof)
    assert prof["display_name"] == "HOTEL TEST PALACE"
    assert prof["address"]["city"] == "Raipur"
    assert prof["address"]["state"] == "Chhattisgarh"
    assert prof["contact"]["phone"] == "+919876543210"
    assert prof["compliance"]["gstin"] == "22COIPS2490N1ZF"
    assert prof["compliance"]["pan"] == "COIPS2490N"
    assert len(prof["room_types"]) == 1
    assert len(prof["amenities"]) == 5


def test_raw_list_unwrapped():
    prof = normalize_to_profile([RAW])             # MIS often returns an array of one
    assert prof["property_id"] == "TEST-001"


def test_profile_passthrough_adds_rate_plans():
    base = json.load(open("examples/booking_engine/manchester-royals.json", encoding="utf-8"))
    # strip rate_plans to prove they get re-added from base_rate
    for r in base["room_types"]:
        r.pop("rate_plans", None)
    prof = normalize_to_profile(base)
    assert all(rt["rate_plans"][0]["code"] == "EP" for rt in prof["room_types"])
    assert all(rt["rate_plans"][0]["price"] == rt["base_rate"] for rt in prof["room_types"])


def test_all_ota_fields_materialize():
    """Every schema field — including the OTA fields the MIS lacks — must appear in
    the loaded profile (empty), so the table editor can surface them."""
    prof = normalize_to_profile(RAW)
    # new top-level fields
    for k in ("property_email", "property_phone", "website", "year_built", "floors",
              "total_room_count", "nearby", "payout"):
        assert k in prof, f"missing top-level field {k}"
    # nested OTA fields present (empty is fine)
    assert "ifsc" in prof["payout"] and "account_number" not in prof["payout"]   # bank number never modeled
    assert "nearest_airport" in prof["nearby"]
    for k in ("owner_kind", "owner_dob", "fssai_license", "fire_safety_certificate", "gst_registered"):
        assert k in prof["compliance"]
    for k in ("couple_friendly", "accepted_ids", "early_checkin_fee", "checkin_until"):
        assert k in prof["policy"]
    for k in ("base_occupancy", "max_occupancy", "extra_adult_charge", "view", "description"):
        assert k in prof["room_types"][0]


def test_summary_shape():
    s = summarize(normalize_to_profile(RAW))
    assert s["name"] == "HOTEL TEST PALACE"
    assert s["total_rooms"] >= 1
    assert s["photos"] == 2
    assert isinstance(s["room_types"], list)


def test_folder_provider_search_and_load(tmp_path):
    # a folder with one already-converted profile is searchable + loadable
    prof = json.load(open("examples/booking_engine/manchester-royals.json", encoding="utf-8"))
    (tmp_path / "mr.json").write_text(json.dumps(prof), encoding="utf-8")
    fp = FolderMisProvider(tmp_path)
    hits = fp.search("manchester")
    assert len(hits) == 1 and hits[0]["city"] == "Raipur"
    loaded = fp.load_profile(hits[0]["id"])
    assert loaded["summary"]["amenities"] == len(prof["amenities"])
    PropertyProfile.model_validate(loaded["profile"])


def _clear_mis(monkeypatch):
    """Neutralize any real .env MIS config so provider-selection is tested in isolation."""
    from accounts_pilot.config import settings as s
    monkeypatch.setattr(s, "mis_metabase_url", "")
    monkeypatch.setattr(s, "mis_metabase_db_id", 0)
    monkeypatch.setattr(s, "mis_pg_dsn", "")
    monkeypatch.setattr(s, "mis_base_url", "")
    return s


def test_load_profile_is_lenient_and_reports_missing(tmp_path):
    """A record missing required fields must still load (so the operator can open
    the editor and fill them), with valid=False + the missing field paths."""
    prof = json.load(open("examples/booking_engine/manchester-royals.json", encoding="utf-8"))
    prof["address"]["city"] = None              # break a required field
    prof["contact"]["phone"] = None
    (tmp_path / "h.json").write_text(json.dumps(prof), encoding="utf-8")
    fp = FolderMisProvider(tmp_path)
    loaded = fp.load_profile(prof["property_id"])
    assert loaded["valid"] is False
    assert "address.city" in loaded["missing"]
    assert "contact.phone" in loaded["missing"]
    assert loaded["profile"]["display_name"]     # still returns a usable profile
    assert loaded["summary"]["name"]             # summary works on the partial


def test_default_provider_is_folder_without_config(monkeypatch):
    _clear_mis(monkeypatch)
    assert isinstance(get_provider(), FolderMisProvider)


def test_postgres_provider_selected(monkeypatch):
    from accounts_pilot.mis.provider import PostgresMisProvider
    s = _clear_mis(monkeypatch)
    monkeypatch.setattr(s, "mis_pg_dsn", "postgresql://u:p@h:5432/db")
    assert isinstance(get_provider(), PostgresMisProvider)


def test_pg_row_shape_converts():
    """A row as psycopg returns it: JSON columns already parsed (dict/list),
    and checkin/checkout as datetime.time — must still convert + validate."""
    import datetime
    pg_row = {
        "property_id": "PG-1",
        "property_name": "PG HOTEL",
        "property_type": "HOTEL",
        "owner_email": "o@x.com",
        "currency": "INR",
        "checkin_time_ist": datetime.time(12, 0),
        "checkout_time_ist": datetime.time(11, 0),
        "terms_and_conditions": "Phone +91 90000 00000",
        "billing_name": "PG HOTEL",
        "service_gst": "22COIPS2490N1ZF",
        "property_address": {"street": "Rd", "city": "Raipur", "state": "CHHATTISGARH",
                             "pincode": "492001", "latitude": 21.2, "longitude": 81.6},
        "property_amenities": [{"amenity_name": "WiFi"}, {"amenity_name": "Parking"}],
        "property_images": [{"download_url": "https://x/a.jpg", "tag": "Exterior"}],
        "room_types": [{"room_type_name": "Deluxe", "size": "150 Sq. Ft", "base_price": 2000,
                        "bed_count": 1, "bed_type": ["Double bed"], "max_occupancy": 2,
                        "max_child_occupancy": 0, "amenities": [{"amenity_name": "Smart TV"}],
                        "images": []}],
    }
    prof = normalize_to_profile(pg_row)
    PropertyProfile.model_validate(prof)
    assert prof["policy"]["checkin_from"] == "12:00"
    assert prof["policy"]["checkout_until"] == "11:00"
    assert prof["address"]["city"] == "Raipur"
    assert prof["room_types"][0]["base_rate"] == 2000


def test_sql_files_are_parameterized():
    from accounts_pilot.mis.provider import _QUERIES
    exp = (_QUERIES / "property_export.sql").read_text(encoding="utf-8")
    assert "%(property_id)s" in exp and "'0ac8abda" not in exp   # hardcoded UUID removed
    srch = (_QUERIES / "search.sql").read_text(encoding="utf-8")
    assert "%(like)s" in srch


# --- Metabase provider (the real DigiStay MIS) ---------------------------------
def test_inline_sql_escapes_quotes():
    from accounts_pilot.mis.provider import _inline_sql
    out = _inline_sql("WHERE id = %(property_id)s::text", {"property_id": "a'b"})
    assert out == "WHERE id = 'a''b'::text"          # single quote doubled
    assert _inline_sql("x = %(q)s", {"q": None}) == "x = NULL"


def test_metabase_provider_selected(monkeypatch):
    from accounts_pilot.config import settings as s
    from accounts_pilot.mis.provider import MetabaseMisProvider
    monkeypatch.setattr(s, "mis_metabase_url", "https://mis.digistay.co.in")
    monkeypatch.setattr(s, "mis_metabase_db_id", 2)
    assert isinstance(get_provider(), MetabaseMisProvider)


class _FakeResp:
    def __init__(self, payload): self._p = json.dumps(payload).encode()
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self): return self._p


def _mock_metabase(monkeypatch, payload, captured):
    import accounts_pilot.mis.provider as prov

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data.decode())
        return _FakeResp(payload)
    monkeypatch.setattr(prov.urllib.request, "urlopen", fake_urlopen)


def test_metabase_search_parses_rows(monkeypatch):
    from accounts_pilot.config import settings as s
    from accounts_pilot.mis.provider import MetabaseMisProvider
    monkeypatch.setattr(s, "mis_metabase_url", "https://mis.digistay.co.in")
    monkeypatch.setattr(s, "mis_metabase_db_id", 2)
    cap = {}
    payload = {"status": "completed", "data": {
        "cols": [{"name": "property_id"}, {"name": "property_name"}, {"name": "city"}, {"name": "state"}],
        "rows": [["b96-uuid", "Hotel Manchester Royals LLP", "Raipur", "CHHATTISGARH"]],
    }}
    _mock_metabase(monkeypatch, payload, cap)
    hits = MetabaseMisProvider().search("manchester")
    assert cap["url"].endswith("/api/dataset")
    assert cap["body"]["database"] == 2 and cap["body"]["type"] == "native"
    assert "manchester" in cap["body"]["native"]["query"]           # inlined search term
    assert hits == [{"id": "b96-uuid", "name": "Hotel Manchester Royals LLP",
                     "city": "Raipur", "state": "CHHATTISGARH"}]


def test_metabase_fetch_and_convert(monkeypatch):
    from accounts_pilot.config import settings as s
    from accounts_pilot.mis.provider import MetabaseMisProvider
    monkeypatch.setattr(s, "mis_metabase_url", "https://mis.digistay.co.in")
    monkeypatch.setattr(s, "mis_metabase_db_id", 2)
    cap = {}
    # Metabase returns json columns as strings; convert.py handles that
    cols = [{"name": k} for k in ("property_id", "property_name", "property_type", "owner_email",
            "currency", "checkin_time_ist", "checkout_time_ist", "terms_and_conditions",
            "billing_name", "service_gst", "property_address", "property_amenities",
            "property_images", "room_types")]
    row = ["MB-1", "MB HOTEL", "HOTEL", "o@x.com", "INR", "12:00:00", "11:00:00",
           "Phone +91 90000 00000", "MB HOTEL", "22COIPS2490N1ZF",
           json.dumps({"street": "Rd", "city": "Raipur", "state": "CHHATTISGARH", "pincode": "492001"}),
           json.dumps([{"amenity_name": "WiFi"}]),
           json.dumps([{"download_url": "https://x/a.jpg", "tag": "Exterior"}]),
           json.dumps([{"room_type_name": "Deluxe", "size": "150 Sq. Ft", "base_price": 2000,
                        "bed_count": 1, "bed_type": ["Double bed"], "max_occupancy": 2,
                        "max_child_occupancy": 0, "amenities": [], "images": []}])]
    _mock_metabase(monkeypatch, {"status": "completed", "data": {"cols": cols, "rows": [row]}}, cap)
    loaded = MetabaseMisProvider().load_profile("MB-1")
    PropertyProfile.model_validate(loaded["profile"])
    assert loaded["summary"]["name"] == "MB HOTEL"
    assert loaded["summary"]["city"] == "Raipur"
    assert "MB-1" in cap["body"]["native"]["query"]                 # property_id inlined


def test_validate_endpoint_accepts_and_rejects():
    from accounts_pilot.web.app import mis_validate, ProfileReq
    good = json.load(open("examples/booking_engine/manchester-royals.json", encoding="utf-8"))
    res = mis_validate(ProfileReq(profile=good))
    assert res["ok"] is True and res["summary"]["name"]
    # break it: room base_rate must be > 0
    bad = json.loads(json.dumps(good))
    bad["room_types"][0]["base_rate"] = 0
    res2 = mis_validate(ProfileReq(profile=bad))
    assert res2["ok"] is False and "base_rate" in res2["error"]


def test_normalize_endpoint_materializes_and_reports_missing():
    """Pasted/minimal JSON → full profile (all OTA fields) + valid/missing, so the
    Simulate 'paste → Table' path mirrors search."""
    from accounts_pilot.web.app import mis_normalize, ProfileReq
    res = mis_normalize(ProfileReq(profile={
        "property_id": "X1", "property_type": "hotel", "display_name": "My Test Hotel",
        "room_types": [{"name": "Std", "base_rate": 1500}],
    }))
    assert res["ok"] is True
    assert res["profile"]["nearby"] and res["profile"]["payout"]      # OTA fields materialized
    assert res["valid"] is False and "address.city" in res["missing"]  # gaps reported
    assert res["summary"]["name"] == "My Test Hotel"


def test_metabase_query_failure_raises(monkeypatch):
    from accounts_pilot.config import settings as s
    from accounts_pilot.mis.provider import MetabaseMisProvider
    monkeypatch.setattr(s, "mis_metabase_url", "https://mis.digistay.co.in")
    monkeypatch.setattr(s, "mis_metabase_db_id", 2)
    cap = {}
    _mock_metabase(monkeypatch, {"status": "failed", "error": "syntax error"}, cap)
    try:
        MetabaseMisProvider().search("x")
        assert False, "expected failure to raise"
    except RuntimeError as e:
        assert "failed" in str(e)
