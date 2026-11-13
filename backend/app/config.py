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


settings = Settings()
DATA_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
