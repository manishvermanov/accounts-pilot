"""Where hotel data comes from for the search box.

Two providers, same interface:
  • RestMisProvider   — live: calls the company MIS HTTP API (configured in .env).
  • FolderMisProvider — fallback: indexes a local folder of hotel JSON files so
                        search works offline with whatever exports you have on disk.

`get_provider()` picks REST when `settings.mis_base_url` is set, else the folder.
Both return search hits as {id, name, city, ...} and a full record for fetch();
the record is handed to `normalize_to_profile` to become the onboarding JSON.
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from accounts_pilot.config import settings
from accounts_pilot.mis.convert import normalize_to_profile, summarize

_QUERIES = Path(__file__).parent / "queries"


class MisProvider:
    name = "mis"

    def describe(self) -> str:
        return self.name

    def search(self, query: str) -> list[dict]:        # -> [{id,name,city,...}]
        raise NotImplementedError

    def fetch(self, hotel_id: str) -> dict:            # -> raw record (or profile)
        raise NotImplementedError

    # convenience: fetch + convert + summarize in one shot.
    # validate=False on purpose: MIS records are often missing required fields
    # (city, email, phone). We still build the profile so the operator can open
    # the editor, see the gaps flagged "not set", and fill them. Validation runs
    # at Save (/api/mis/validate). `valid`/`missing` tell the UI what to flag.
    def load_profile(self, hotel_id: str) -> dict:
        from accounts_pilot.models.property_profile import PropertyProfile
        record = self.fetch(hotel_id)
        profile = normalize_to_profile(record, validate=False)
        valid, missing = True, []
        try:
            PropertyProfile.model_validate(profile)
        except Exception as e:
            valid = False
            for err in getattr(e, "errors", lambda: [])():
                missing.append(".".join(str(p) for p in err.get("loc", ())))
        return {"profile": profile, "summary": summarize(profile),
                "valid": valid, "missing": missing}

    def health(self) -> dict:
        """Cheap reachability check — how many hotels this source can see."""
        return {"source": self.describe(), "properties": len(self.search(""))}


def _inline_sql(sql: str, params: dict) -> str:
    """Substitute psycopg-style %(name)s placeholders with safely-quoted SQL
    literals, so the same .sql files work when sent to Metabase as native SQL.
    Only used for our own fixed queries with validated inputs (UUID / hotel name)."""
    def lit(v) -> str:
        if v is None:
            return "NULL"
        return "'" + str(v).replace("'", "''") + "'"      # escape single quotes
    out = sql
    for name, val in params.items():
        out = out.replace(f"%({name})s", lit(val))
    return out


# --------------------------------------------------------------------------- #
# Metabase provider — runs native SQL through Metabase (which owns the prod DB).
# No prod DB credentials live here; auth is a Metabase API key.
# --------------------------------------------------------------------------- #
class MetabaseMisProvider(MisProvider):
    name = "mis-metabase"

    def __init__(self) -> None:
        self.url = settings.mis_metabase_url.rstrip("/")
        self.db_id = settings.mis_metabase_db_id
        self._search_sql = (_QUERIES / "search.sql").read_text(encoding="utf-8")
        self._export_sql = (_QUERIES / "property_export.sql").read_text(encoding="utf-8")

    def describe(self) -> str:
        return f"MIS Metabase · {self.url} (db {self.db_id})"

    def _dataset(self, sql: str) -> list[dict]:
        """POST a native query to Metabase /api/dataset → list of row dicts."""
        body = json.dumps({
            "database": self.db_id,
            "type": "native",
            "native": {"query": sql},
            "parameters": [],
        }).encode("utf-8")
        req = urllib.request.Request(f"{self.url}/api/dataset", data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")
        if settings.mis_metabase_api_key:
            req.add_header(settings.mis_metabase_auth_header, settings.mis_metabase_api_key)
        if settings.mis_edge_value:                 # Cloudflare edge-bypass secret header
            req.add_header(settings.mis_edge_header, settings.mis_edge_value)
        with urllib.request.urlopen(req, timeout=settings.mis_timeout_s) as r:
            data = json.loads(r.read().decode("utf-8"))
        if data.get("status") and data["status"] != "completed":
            raise RuntimeError(f"Metabase query {data.get('status')}: "
                               f"{(data.get('error') or '')[:300]}")
        inner = data.get("data") or {}
        cols = [c.get("name") for c in (inner.get("cols") or [])]
        rows = inner.get("rows") or []
        return [dict(zip(cols, row)) for row in rows]

    def search(self, query: str) -> list[dict]:
        q = (query or "").strip()
        sql = _inline_sql(self._search_sql, {"q": q, "like": f"%{q}%"})
        rows = self._dataset(sql)
        return [{"id": r.get("property_id"), "name": r.get("property_name"),
                 "city": r.get("city") or "", "state": r.get("state") or ""}
                for r in rows if r.get("property_id")]

    def fetch(self, hotel_id: str) -> dict:
        # property_id is a UUID; inline it as a quoted literal (no template tags needed)
        sql = _inline_sql(self._export_sql, {"property_id": str(hotel_id)})
        rows = self._dataset(sql)
        if not rows:
            raise KeyError(f"property not found in MIS: {hotel_id}")
        return rows[0]

    def health(self) -> dict:
        sql = "SELECT count(*) AS n FROM public.property WHERE is_active = TRUE AND is_deleted = FALSE"
        rows = self._dataset(sql)
        n = (rows[0].get("n") if rows else 0) or 0
        return {"source": self.describe(), "properties": int(n)}


# --------------------------------------------------------------------------- #
# live Postgres provider — runs the DigiStay "personal data collection" query
# --------------------------------------------------------------------------- #
class PostgresMisProvider(MisProvider):
    name = "mis-postgres"

    def __init__(self) -> None:
        self.dsn = settings.mis_pg_dsn
        self._search_sql = (_QUERIES / "search.sql").read_text(encoding="utf-8")
        self._export_sql = (_QUERIES / "property_export.sql").read_text(encoding="utf-8")

    def describe(self) -> str:
        # never leak credentials — show host/db only
        try:
            import psycopg
            info = psycopg.conninfo.conninfo_to_dict(self.dsn)
            return f"MIS Postgres · {info.get('host','?')}/{info.get('dbname','?')}"
        except Exception:
            return "MIS Postgres"

    def _connect(self):
        import psycopg
        # READ-ONLY + short timeout — this service must never write to the MIS.
        conn = psycopg.connect(self.dsn, autocommit=True,
                               connect_timeout=int(settings.mis_timeout_s))
        conn.read_only = True
        return conn

    def search(self, query: str) -> list[dict]:
        import psycopg.rows
        q = (query or "").strip()
        with self._connect() as conn:
            with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
                cur.execute(self._search_sql, {"q": q, "like": f"%{q}%"})
                rows = cur.fetchall()
        return [{"id": r["property_id"], "name": r["property_name"],
                 "city": r.get("city") or "", "state": r.get("state") or ""} for r in rows]

    def fetch(self, hotel_id: str) -> dict:
        import psycopg.rows
        with self._connect() as conn:
            with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
                cur.execute(self._export_sql, {"property_id": str(hotel_id)})
                row = cur.fetchone()
        if not row:
            raise KeyError(f"property not found in MIS: {hotel_id}")
        return row     # JSON columns come back already parsed; convert.py handles it

    def health(self) -> dict:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM public.property "
                            "WHERE is_active = TRUE AND is_deleted = FALSE")
                n = cur.fetchone()[0]
        return {"source": self.describe(), "properties": int(n)}


# --------------------------------------------------------------------------- #
# live REST provider
# --------------------------------------------------------------------------- #
class RestMisProvider(MisProvider):
    name = "mis-rest"

    def __init__(self) -> None:
        self.base = settings.mis_base_url.rstrip("/")

    def _get(self, url: str) -> Any:
        req = urllib.request.Request(url)
        if settings.mis_auth_value:
            req.add_header(settings.mis_auth_header, settings.mis_auth_value)
        req.add_header("Accept", "application/json")
        with urllib.request.urlopen(req, timeout=settings.mis_timeout_s) as r:
            return json.loads(r.read().decode("utf-8"))

    def describe(self) -> str:
        return f"MIS REST · {self.base}"

    def search(self, query: str) -> list[dict]:
        qs = urllib.parse.urlencode({settings.mis_search_param: query})
        data = self._get(f"{self.base}{settings.mis_search_path}?{qs}")
        rows = data if isinstance(data, list) else (data.get("results") or data.get("data") or [])
        out = []
        for row in rows:
            out.append({
                "id": row.get("property_id") or row.get("id"),
                "name": row.get("property_name") or row.get("display_name") or row.get("name"),
                "city": (_dig(row, "city") or ""),
                "state": (_dig(row, "state") or ""),
            })
        return [r for r in out if r["id"]]

    def fetch(self, hotel_id: str) -> dict:
        path = settings.mis_fetch_path.replace("{id}", urllib.parse.quote(str(hotel_id)))
        data = self._get(f"{self.base}{path}")
        if isinstance(data, list):
            return data[0] if data else {}
        # some APIs wrap the row: {"data": {...}}
        if isinstance(data, dict) and "data" in data and isinstance(data["data"], (dict, list)):
            d = data["data"]
            return (d[0] if isinstance(d, list) and d else d) if d else {}
        return data


# --------------------------------------------------------------------------- #
# offline folder provider
# --------------------------------------------------------------------------- #
class FolderMisProvider(MisProvider):
    name = "mis-folder"

    def __init__(self, folder: Path | None = None) -> None:
        self.folder = Path(folder or settings.mis_folder)

    def describe(self) -> str:
        return f"MIS folder · {self.folder}"

    def _records(self) -> list[tuple[str, dict]]:
        """(file_path, record) for every JSON in the folder (array-of-one unwrapped)."""
        recs: list[tuple[str, dict]] = []
        if not self.folder.exists():
            return recs
        for fp in sorted(self.folder.glob("*.json")):
            try:
                data = json.loads(fp.read_text(encoding="utf-8"))
            except Exception:
                continue
            if isinstance(data, list):
                data = data[0] if data else None
            if isinstance(data, dict):
                recs.append((str(fp), data))
        return recs

    @staticmethod
    def _hit(record: dict) -> dict:
        return {
            "id": record.get("property_id") or record.get("id"),
            "name": record.get("property_name") or record.get("display_name") or record.get("name"),
            "city": _dig(record, "city") or "",
            "state": _dig(record, "state") or "",
        }

    def search(self, query: str) -> list[dict]:
        q = (query or "").strip().lower()
        out = []
        for _, rec in self._records():
            hit = self._hit(rec)
            if not hit["id"] or not hit["name"]:
                continue
            hay = f"{hit['name']} {hit['city']} {hit['id']}".lower()
            if not q or q in hay:
                out.append(hit)
        return out

    def fetch(self, hotel_id: str) -> dict:
        for _, rec in self._records():
            rid = rec.get("property_id") or rec.get("id")
            if str(rid) == str(hotel_id):
                return rec
        raise KeyError(f"hotel not found in {self.folder}: {hotel_id}")


def _dig(record: dict, key: str) -> str | None:
    """Find a city/state whether the record is a raw MIS row (address stringified)
    or an already-built profile (nested address object)."""
    a = record.get("address")
    if isinstance(a, dict) and a.get(key):
        return a[key]
    pa = record.get("property_address")
    if isinstance(pa, str):
        try:
            pa = json.loads(pa)
        except Exception:
            pa = None
    if isinstance(pa, dict) and pa.get(key):
        return str(pa[key]).title() if key == "state" else pa[key]
    return None


def get_provider() -> MisProvider:
    if settings.mis_metabase_url.strip() and settings.mis_metabase_db_id:
        return MetabaseMisProvider()
    if settings.mis_pg_dsn.strip():
        return PostgresMisProvider()
    if settings.mis_base_url.strip():
        return RestMisProvider()
    return FolderMisProvider()
