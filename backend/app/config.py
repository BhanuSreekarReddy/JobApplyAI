import os
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = Path(os.environ.get("JOBAPPLY_DATA_DIR") or ROOT / "data")
UPLOAD_DIR = DATA_DIR / "uploads"
DB_PATH = DATA_DIR / "jobapply.db"
BROWSER_PROFILE_DIR = DATA_DIR / "browser-profile"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    adzuna_app_id: str = ""
    adzuna_app_key: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    usajobs_api_key: str = ""
    usajobs_user_agent: str = "JobApply/1.0"
    formfill_headless: bool = False
    # Human pacing for the form filler (delays + interaction pattern that
    # reads as a person typing rather than a script setting values).
    # FORMFILL_HUMAN_PACING=false is the kill switch: the filler reverts
    # to instant value-setting. FORMFILL_PACING_SCALE multiplies every
    # pacing delay (tests set it to 0 to stay fast; the interaction
    # pattern still runs, just without the wall-clock waits).
    formfill_human_pacing: bool = True
    formfill_pacing_scale: float = 1.0
    # Accounts / hosted mode. ADMIN_EMAILS is a comma-separated list; anyone
    # who registers with one of those addresses becomes an admin.
    admin_emails: str = ""
    # Optional one-time invite code created at startup so the very first
    # (admin) account can register while registration is invite-only.
    jobapply_bootstrap_invite: str = ""
    jobapply_hosted: bool = False
    max_runs_per_day: int = 20
    session_cookie_secure: bool = False
    # Public origin (scheme + host) the app is opened at. Account emails
    # build their verification and password-reset links from it, so hosted
    # deployments must set it to the public address.
    public_base_url: str = "http://localhost:5173"
    # Browser origins allowed to call the API, comma-separated. Defaults to
    # the local Vite dev server; hosted deployments set CORS_ORIGINS to the
    # public frontend origin. Never a wildcard: the API takes credentialed
    # requests, so an origin list is a real allowlist.
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"


settings = Settings()


def cors_origin_list() -> list[str]:
    """The configured CORS allowlist, parsed from the comma-separated
    CORS_ORIGINS setting."""
    return [origin.strip() for origin in settings.cors_origins.split(",") if origin.strip()]
DATA_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
