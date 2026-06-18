"""Runtime configuration, loaded from environment / .env."""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # runtime
    headless: bool = False
    slow_mo_ms: int = 0
    use_stealth: Literal["auto", "always", "never"] = "auto"

    # humanisation (behavioural anti-bot evasion — mouse paths + typing cadence)
    humanize: bool = True
    key_delay_min_s: float = 0.04
    key_delay_max_s: float = 0.17
    think_min_s: float = 0.3
    think_max_s: float = 1.1

    # proxy
    cloak_proxy: str = ""

    # OTA partner credentials (the service logs in with these; never committed)
    booking_partner_email: str = ""
    booking_partner_password: str = ""

    # TinyFish (cloud AI web-agent) — optional alternative fill driver
    tinyfish_api_key: str = ""
    tinyfish_base_url: str = "https://agent.tinyfish.ai/v1/automation/run"
    tinyfish_browser_profile: str = "lite"   # "lite" | "stealth"
    tinyfish_profile_id: str = ""            # BBU profile (your saved Booking login session)

    # AgentQL (local Playwright element-resolution; actions stay local)
    agentql_api_key: str = ""

    # Azure OpenAI (LLM page-filler — reads any page, maps the JSON, fills all 20 pages)
    azure_openai_endpoint: str = ""        # https://<resource>.openai.azure.com
    azure_openai_key: str = ""
    azure_openai_deployment: str = ""      # your model deployment name
    azure_openai_api_version: str = "2024-08-01-preview"

    # captcha
    captcha_provider: Literal["twocaptcha", "capsolver", "azcaptcha", "none"] = "none"
    captcha_api_key: str = ""

    # otp (v1.1)
    otp_email_imap_host: str = ""
    otp_email_imap_user: str = ""
    otp_email_imap_pass: str = ""
    otp_sms_provider: str = ""
    otp_sms_api_key: str = ""

    # booking engine (source of property data the service fetches from)
    booking_engine_dir: Path = Path("examples/booking_engine")
    booking_engine_url: str = ""        # if set, fetch over HTTP instead of the local dir

    # MIS (company hotel-data source the dashboard searches; secrets live in .env, never chat)
    # Provider priority:  Metabase → Postgres → REST → folder fallback.
    # The DigiStay MIS is METABASE — it already holds the prod DB connection, so Accounts
    # Pilot routes through it (no prod DB creds here). Run native SQL via POST /api/dataset.
    mis_metabase_url: str = ""         # e.g. https://mis.digistay.co.in
    mis_metabase_api_key: str = ""     # Metabase API key (Admin → Settings → API Keys)
    mis_metabase_db_id: int = 0        # the Metabase database id (the payload showed database: 2)
    mis_metabase_auth_header: str = "x-api-key"   # or "X-Metabase-Session" if using a session token

    # Cloudflare edge bypass: the MIS sits behind Cloudflare's Browser Integrity Check,
    # which 403s non-browser clients. Infra added a WAF rule that skips BIC for /api/*
    # requests carrying a shared-secret header. We send it IN ADDITION TO the Metabase
    # api-key; it only clears Cloudflare's edge, it does not touch Metabase auth.
    mis_edge_header: str = "X-AP-Auth"
    mis_edge_value: str = ""           # the shared secret (kept in .env, never committed)

    mis_pg_dsn: str = ""               # direct Postgres (fallback): postgresql://u:p@host:5432/db
    mis_base_url: str = ""             # generic REST (fallback)
    mis_auth_header: str = "Authorization"
    mis_auth_value: str = ""           # e.g. "Bearer <token>"  (kept out of the repo)
    mis_search_path: str = "/hotels"   # GET {base}{search_path}?{search_param}=<name>
    mis_search_param: str = "search"
    mis_fetch_path: str = "/hotels/{id}"   # GET full record for one hotel; {id} substituted
    mis_folder: Path = Path("examples/booking_engine")   # offline fallback search corpus
    mis_timeout_s: float = 20.0

    # optional shared login (for when the server is exposed publicly, e.g. a Public
    # Codespaces port). When BOTH are set, every route requires HTTP Basic auth with
    # these. Leave blank → no auth (local dev / GitHub-private port).
    ap_auth_user: str = ""
    ap_auth_pass: str = ""

    # storage
    db_path: Path = Path("data/accounts_pilot.db")
    artifacts_dir: Path = Path("data/artifacts")

    def ensure_dirs(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)


settings = Settings()
