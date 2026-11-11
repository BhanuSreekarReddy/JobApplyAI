from __future__ import annotations

import json
import re
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

import httpx
from datetime import datetime
from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from . import answers as profile_answers
from . import auth, events
from .agent.apply import apply_to_job
from .agent.companies import ResolveError, add_company, payload as company_payload, resolve_job_url, sync_starter
from .agent.formfill import fill_application, shutdown_browser
from .agent.materials import cover_letter
from .agent.profile import heuristic_profile
from .agent.resume import extract_text
from .agent.runner import execute_run, primary_location, _app_payload, _job_payload, _public_profile, _upsert_profile
from .agent.seen import record_seen
from .config import UPLOAD_DIR, settings
from .db import Base, SessionLocal, engine, upgrade_schema
from .models import (
    AdminAuditLog,
    Application,
    Company,
    Invite,
    JobMatch,
    Profile,
    Run,
    SeenJob,
    User,
)
from . import prefill_sessions

Base.metadata.create_all(bind=engine)
upgrade_schema()
with SessionLocal() as _db:
    sync_starter(_db)
    if settings.jobapply_bootstrap_invite:
        # One-time bootstrap invite so the first (admin) account can be
        # created while registration is invite-only.
        code = settings.jobapply_bootstrap_invite.strip()
        if code and not _db.query(Invite).filter(Invite.code == code).first():
            _db.add(Invite(code=code))
            _db.commit()
    _admin = (
        _db.query(User)
        .filter(User.email.in_(auth.admin_emails()))
        .first()
        if auth.admin_emails()
        else None
    )
    if _admin is not None:
        auth.claim_orphans(_db, _admin)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    import asyncio

    async def _expire_loop():
        while True:
            await asyncio.sleep(60)
            await prefill_sessions.expire_idle_sessions()

    task = asyncio.ensure_future(_expire_loop())
    yield
    task.cancel()
    await prefill_sessions.close_all_sessions()
    shutdown_browser()


app = FastAPI(title="JobApply Agent", lifespan=lifespan, dependencies=[Depends(auth.auth_guard)])
app.include_router(prefill_sessions.router)


def _match_key(title: str, company: str) -> str:
    return re.sub(r"\W+", "", f"{title or ''}{company or ''}".lower())

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _run_in_background(run_id: str) -> None:
    import asyncio

    db = SessionLocal()
    try:
        run = db.get(Run, run_id)
        if not run:
            return
        run.status = "running"
        db.commit()
        asyncio.run(execute_run(db, run))
    finally:
        db.close()


@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/config")
def public_config():
    """Non-sensitive client configuration (also used by the logged-out pages)."""
    return {"hosted": bool(settings.jobapply_hosted), "app_name": "JobApply Agent"}


@app.get("/api/public/summary")
def public_summary():
    """Honest public stats for the logged-out home page: the size of the
    shared company index, nothing about any user."""
    db = SessionLocal()
    try:
        rows = db.query(Company).filter(Company.user_id.is_(None), Company.enabled == 1).all()
        return {
            "app_name": "JobApply Agent",
            "tagline": "Match a resume to live roles, then apply without babysitting forms.",
            "companies": len(rows),
            "open_jobs": sum(c.job_count or 0 for c in rows),
        }
    finally:
        db.close()


# ---------------------------------------------------------------- accounts

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _set_session_cookie(response: Response, raw: str) -> None:
    response.set_cookie(
        auth.SESSION_COOKIE,
        raw,
        httponly=True,
        samesite="lax",
        secure=bool(settings.session_cookie_secure),
        max_age=int(auth.SESSION_TTL.total_seconds()),
        path="/",
    )


def _check_password_strength(password: str) -> None:
    if len(password or "") < 10:
        raise HTTPException(400, "Password must be at least 10 characters.")


class RegisterBody(BaseModel):
    email: str
    password: str
    invite_code: str


@app.post("/api/auth/register")
def register(body: RegisterBody, response: Response):
    email = (body.email or "").strip().lower()
    if not _EMAIL_RE.match(email):
        raise HTTPException(400, "Enter a valid email address.")
    _check_password_strength(body.password)
    db = SessionLocal()
    try:
        if db.query(User).filter(User.email == email).first():
            raise HTTPException(400, "An account with that email already exists.")
        code = (body.invite_code or "").strip()
        invite = db.query(Invite).filter(Invite.code == code).first()
        if (
            invite is None
            or invite.used_by is not None
            or (invite.expires_at is not None and invite.expires_at < datetime.utcnow())
        ):
            raise HTTPException(400, "That invite code is not valid. Ask an admin for a new invite.")
        role = "admin" if auth.is_seeded_admin_email(email) else "user"
        user = User(
            email=email,
            password_hash=auth.hash_password(body.password),
            role=role,
            is_active=1,
            email_verified=0,
        )
        db.add(user)
        db.flush()
        # Mark the invite used in the same transaction; the conditional
        # update keeps two simultaneous registrations from sharing one code.
        claimed = (
            db.query(Invite)
            .filter(Invite.id == invite.id, Invite.used_by.is_(None))
            .update({Invite.used_by: user.id}, synchronize_session=False)
        )
        if not claimed:
            db.rollback()
            raise HTTPException(400, "That invite code is not valid. Ask an admin for a new invite.")
        db.commit()
        db.refresh(user)
        auth.send_verification_email(db, user)
        raw = auth.create_session(db, user)
        _set_session_cookie(response, raw)
        auth.claim_orphans(db, user)
        return auth.user_payload(user)
    finally:
        db.close()


class LoginBody(BaseModel):
    email: str
    password: str


@app.post("/api/auth/login")
def login(body: LoginBody, response: Response):
    email = (body.email or "").strip().lower()
    if auth.login_rate_limited(email):
        raise HTTPException(429, "Too many failed sign-in attempts. Try again in a few minutes.")
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == email).first()
        if user is None or not auth.verify_password(body.password, user.password_hash):
            auth.record_failed_login(email)
            raise HTTPException(401, "Email or password is incorrect.")
        if not user.is_active:
            raise HTTPException(403, "This account has been deactivated. Contact an admin.")
        auth.clear_failed_logins(email)
        user.last_login_at = datetime.utcnow()
        db.commit()
        raw = auth.create_session(db, user)
        _set_session_cookie(response, raw)
        auth.claim_orphans(db, user)
        return auth.user_payload(user)
    finally:
        db.close()


@app.post("/api/auth/logout")
def logout(request: Request, response: Response):
    db = SessionLocal()
    try:
        auth.delete_session(db, request.cookies.get(auth.SESSION_COOKIE))
    finally:
        db.close()
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    return {"ok": True}


@app.get("/api/auth/me")
def me(request: Request):
    return auth.user_payload(auth.current_user(request))


class TokenBody(BaseModel):
    token: str


@app.post("/api/auth/verify-email")
def verify_email(body: TokenBody):
    db = SessionLocal()
    try:
        user = auth.consume_action_token(db, body.token, "verify")
        if user is None:
            # Idempotent for an already-verified account: the link may have
            # been clicked (or submitted by the page) more than once.
            already = auth.user_for_action_token(db, body.token, "verify")
            if already is not None and already.email_verified:
                return {"ok": True, "email": already.email, "already_verified": True}
            raise HTTPException(400, "That verification link is invalid or has expired.")
        user.email_verified = 1
        db.commit()
        return {"ok": True, "email": user.email}
    finally:
        db.close()


class RequestResetBody(BaseModel):
    email: str


@app.post("/api/auth/request-password-reset")
def request_password_reset(body: RequestResetBody):
    # Always the same response, whether or not the account exists.
    email = (body.email or "").strip().lower()
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == email).first()
        if user is not None and user.is_active:
            auth.send_password_reset_email(db, user)
        return {"ok": True}
    finally:
        db.close()


class ResetPasswordBody(BaseModel):
    token: str
    password: str


@app.post("/api/auth/reset-password")
def reset_password(body: ResetPasswordBody):
    _check_password_strength(body.password)
    db = SessionLocal()
    try:
        user = auth.consume_action_token(db, body.token, "reset")
        if user is None:
            raise HTTPException(400, "That reset link is invalid, has expired, or was already used.")
        user.password_hash = auth.hash_password(body.password)
        db.commit()
        # Existing sessions were issued under the old password; end them.
        from .models import UserSession

        db.query(UserSession).filter(UserSession.user_id == user.id).delete(synchronize_session=False)
        db.commit()
        return {"ok": True}
    finally:
        db.close()


@app.get("/api/auth/me/export")
def export_my_data(request: Request):
    user = auth.current_user(request)
    db = SessionLocal()
    try:
        auth.claim_orphans(db, user)
        profile = db.query(Profile).filter(Profile.user_id == user.id).first()
        runs = db.query(Run).filter(Run.user_id == user.id).order_by(Run.created_at.desc()).all()
        apps = db.query(Application).filter(Application.user_id == user.id).order_by(Application.id.desc()).all()
        seen = db.query(SeenJob).filter(SeenJob.user_id == user.id).order_by(SeenJob.id.desc()).all()
        return {
            "user": auth.user_payload(user),
            "profile": _profile_payload(profile) if profile else None,
            "runs": [
                {"id": r.id, "status": r.status, "locations": r.locations, "summary": r.summary,
                 "created_at": r.created_at.isoformat() if r.created_at else None}
                for r in runs
            ],
            "applications": [_app_payload(a) for a in apps],
            "seen_jobs": [
                {"title": s.title, "company": s.company, "url": s.url, "status": s.status} for s in seen
            ],
        }
    finally:
        db.close()


@app.delete("/api/auth/me")
def delete_my_account(request: Request, response: Response):
    user = auth.current_user(request)
    db = SessionLocal()
    try:
        auth.delete_user_data(db, user.id)
        db.commit()
    finally:
        db.close()
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    return {"deleted": True}


# ---------------------------------------------------------------- profile

def _profile_payload(row: Profile) -> dict:
    import json as _json

    def _list(raw: str) -> list:
        try:
            value = _json.loads(raw or "[]")
            return value if isinstance(value, list) else []
        except _json.JSONDecodeError:
            return []

    return {
        "name": row.name or "",
        "contact_email": row.contact_email or "",
        "phone": row.phone or "",
        "linkedin": row.linkedin or "",
        "github": row.github or "",
        "titles": _list(row.titles),
        "skills": _list(row.skills),
        "years_experience": row.years_experience,
        "summary": row.summary or "",
        "locations": row.locations or "",
    }


_EMPTY_PROFILE = {
    "name": "", "contact_email": "", "phone": "", "linkedin": "", "github": "",
    "titles": [], "skills": [], "years_experience": None, "summary": "", "locations": "",
}


@app.get("/api/profile")
def get_profile(request: Request):
    user = auth.current_user(request)
    db = SessionLocal()
    try:
        row = db.query(Profile).filter(Profile.user_id == user.id).first()
        return _profile_payload(row) if row else dict(_EMPTY_PROFILE)
    finally:
        db.close()


class ProfileUpdate(BaseModel):
    name: str | None = None
    contact_email: str | None = None
    phone: str | None = None
    linkedin: str | None = None
    github: str | None = None
    titles: list[str] | None = None
    skills: list[str] | None = None
    years_experience: int | None = None
    summary: str | None = None
    locations: str | None = None


@app.put("/api/profile")
def update_profile(body: ProfileUpdate, request: Request):
    user = auth.current_user(request)
    db = SessionLocal()
    try:
        row = db.query(Profile).filter(Profile.user_id == user.id).first()
        if row is None:
            row = Profile(user_id=user.id)
            db.add(row)
        for field in ("name", "contact_email", "phone", "linkedin", "github", "summary", "locations"):
            value = getattr(body, field)
            if value is not None:
                setattr(row, field, value)
        if body.titles is not None:
            row.titles = json.dumps([str(t) for t in body.titles])
        if body.skills is not None:
            row.skills = json.dumps([str(s) for s in body.skills])
        if body.years_experience is not None:
            row.years_experience = body.years_experience
        db.commit()
        db.refresh(row)
        return _profile_payload(row)
    finally:
        db.close()


# ------------------------------------------------------- profile answers

class AnswerItem(BaseModel):
    key: str
    question: str = ""
    answer: str = ""


class ProfileAnswersUpdate(BaseModel):
    items: list[AnswerItem]


@app.get("/api/profile/answers")
def get_profile_answers(request: Request):
    user = auth.current_user(request)
    db = SessionLocal()
    try:
        return {"answers": profile_answers.list_answers(db, user.id)}
    finally:
        db.close()


@app.put("/api/profile/answers")
def put_profile_answers(body: ProfileAnswersUpdate, request: Request):
    """Upsert saved answers (an item with an empty answer deletes it)."""
    user = auth.current_user(request)
    db = SessionLocal()
    try:
        items = [item.model_dump() for item in body.items]
        return {"answers": profile_answers.upsert_answers(db, user.id, items)}
    finally:
        db.close()


@app.delete("/api/profile/answers")
def delete_profile_answer(key: str, request: Request):
    user = auth.current_user(request)
    db = SessionLocal()
    try:
        if not profile_answers.delete_answer(db, user.id, key):
            raise HTTPException(404, "Saved answer not found")
        return {"answers": profile_answers.list_answers(db, user.id)}
    finally:
        db.close()


# ---------------------------------------------------------------- admin

def _audit(db, admin_id: int, action: str, target_user_id: int | None, detail: str = "") -> None:
    db.add(AdminAuditLog(admin_id=admin_id, action=action, target_user_id=target_user_id, detail=detail))
    db.commit()


def _admin_target(db, admin: auth.AuthUser, user_id: int) -> User:
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(404, "User not found")
    return target


def _guard_seeded(target: User, action: str) -> None:
    if auth.is_seeded_admin_email(target.email):
        raise HTTPException(403, f"Seeded admin accounts cannot be {action}.")


@app.get("/api/admin/users")
def admin_list_users(request: Request):
    admin = auth.require_admin(request)
    db = SessionLocal()
    try:
        rows = db.query(User).order_by(User.id.asc()).all()
        out = []
        for u in rows:
            payload = auth.user_payload(u)
            payload["created_at"] = u.created_at.isoformat() if u.created_at else None
            payload["last_login_at"] = u.last_login_at.isoformat() if u.last_login_at else None
            payload["run_count"] = db.query(Run).filter(Run.user_id == u.id).count()
            payload["application_count"] = db.query(Application).filter(Application.user_id == u.id).count()
            out.append(payload)
        return out
    finally:
        db.close()


@app.post("/api/admin/users/{user_id}/deactivate")
def admin_deactivate_user(user_id: int, request: Request):
    admin = auth.require_admin(request)
    if user_id == admin.id:
        raise HTTPException(400, "You cannot deactivate your own account.")
    db = SessionLocal()
    try:
        target = _admin_target(db, admin, user_id)
        _guard_seeded(target, "deactivated")
        target.is_active = 0
        db.commit()
        _audit(db, admin.id, "deactivate", target.id, target.email)
        return auth.user_payload(target)
    finally:
        db.close()


@app.post("/api/admin/users/{user_id}/activate")
def admin_activate_user(user_id: int, request: Request):
    admin = auth.require_admin(request)
    db = SessionLocal()
    try:
        target = _admin_target(db, admin, user_id)
        target.is_active = 1
        db.commit()
        _audit(db, admin.id, "activate", target.id, target.email)
        return auth.user_payload(target)
    finally:
        db.close()


@app.delete("/api/admin/users/{user_id}")
def admin_delete_user(user_id: int, request: Request):
    admin = auth.require_admin(request)
    if user_id == admin.id:
        raise HTTPException(400, "You cannot delete your own account.")
    db = SessionLocal()
    try:
        target = _admin_target(db, admin, user_id)
        _guard_seeded(target, "deleted")
        email = target.email
        auth.delete_user_data(db, user_id)
        db.commit()
        _audit(db, admin.id, "delete", user_id, email)
        return {"deleted": True}
    finally:
        db.close()


@app.post("/api/admin/users/{user_id}/promote")
def admin_promote_user(user_id: int, request: Request):
    admin = auth.require_admin(request)
    db = SessionLocal()
    try:
        target = _admin_target(db, admin, user_id)
        target.role = "admin"
        db.commit()
        _audit(db, admin.id, "promote", target.id, target.email)
        return auth.user_payload(target)
    finally:
        db.close()


@app.post("/api/admin/users/{user_id}/demote")
def admin_demote_user(user_id: int, request: Request):
    admin = auth.require_admin(request)
    if user_id == admin.id:
        raise HTTPException(400, "You cannot demote your own account.")
    db = SessionLocal()
    try:
        target = _admin_target(db, admin, user_id)
        _guard_seeded(target, "demoted")
        target.role = "user"
        db.commit()
        _audit(db, admin.id, "demote", target.id, target.email)
        return auth.user_payload(target)
    finally:
        db.close()


class InviteCreate(BaseModel):
    expires_days: int = 14


@app.post("/api/admin/invites")
def admin_create_invite(body: InviteCreate, request: Request):
    import secrets as _secrets
    from datetime import datetime, timedelta

    admin = auth.require_admin(request)
    db = SessionLocal()
    try:
        code = _secrets.token_urlsafe(12)
        invite = Invite(
            code=code,
            created_by=admin.id,
            expires_at=datetime.utcnow() + timedelta(days=max(1, body.expires_days)),
        )
        db.add(invite)
        db.commit()
        db.refresh(invite)
        _audit(db, admin.id, "invite_create", None, code)
        return {
            "code": invite.code,
            "expires_at": invite.expires_at.isoformat() if invite.expires_at else None,
        }
    finally:
        db.close()


@app.get("/api/admin/invites")
def admin_list_invites(request: Request):
    auth.require_admin(request)
    db = SessionLocal()
    try:
        rows = db.query(Invite).order_by(Invite.id.desc()).limit(100).all()
        return [
            {
                "id": i.id,
                "code": i.code,
                "created_by": i.created_by,
                "used_by": i.used_by,
                "expires_at": i.expires_at.isoformat() if i.expires_at else None,
                "created_at": i.created_at.isoformat() if i.created_at else None,
            }
            for i in rows
        ]
    finally:
        db.close()


@app.get("/api/admin/audit")
def admin_audit_log(request: Request):
    auth.require_admin(request)
    db = SessionLocal()
    try:
        rows = db.query(AdminAuditLog).order_by(AdminAuditLog.id.desc()).limit(100).all()
        return [
            {
                "id": r.id,
                "admin_id": r.admin_id,
                "action": r.action,
                "target_user_id": r.target_user_id,
                "detail": r.detail,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]
    finally:
        db.close()


# ---------------------------------------------------------------- run ownership helpers

def _visible_run(db, user: auth.AuthUser, run_id: str) -> Run:
    auth.claim_orphans(db, user)
    run = db.get(Run, run_id)
    if run is None or run.user_id != user.id:
        raise HTTPException(404, "Run not found")
    return run


def _owned_application(db, user: auth.AuthUser, app_id: int) -> Application:
    auth.claim_orphans(db, user)
    row = db.get(Application, app_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(404, "Application not found")
    return row


def _runs_today(db, user_id: int) -> int:
    from datetime import datetime, time as _time

    start = datetime.combine(datetime.utcnow().date(), _time.min)
    return db.query(Run).filter(Run.user_id == user_id, Run.created_at >= start).count()


def _check_run_quota(db, user: auth.AuthUser) -> None:
    if user.role == "admin":
        return
    if _runs_today(db, user.id) >= settings.max_runs_per_day:
        raise HTTPException(429, f"Daily run limit reached ({settings.max_runs_per_day} runs per day).")





@app.post("/api/runs")
async def create_run(
    background: BackgroundTasks,
    request: Request,
    resume: UploadFile = File(...),
    locations: str = Form(...),
    auto_apply: str = Form("false"),
    browser_fill: str = Form("false"),
    min_score: float = Form(68.0),
    max_applications: int = Form(8),
    contact_email: str = Form(""),
):
    user = auth.current_user(request)
    if not user.email_verified:
        raise HTTPException(403, "Verify your email address before starting a run.")
    suffix = Path(resume.filename or "resume.txt").suffix.lower()
    if suffix not in {".pdf", ".docx", ".doc", ".txt", ".md"}:
        raise HTTPException(400, "Upload a PDF, DOCX, or TXT resume.")
    run_id = uuid.uuid4().hex[:12]
    user_dir = UPLOAD_DIR / str(user.id)
    user_dir.mkdir(parents=True, exist_ok=True)
    dest = user_dir / f"{run_id}{suffix}"
    dest.write_bytes(await resume.read())

    db = SessionLocal()
    try:
        _check_run_quota(db, user)
        run = Run(
            id=run_id,
            user_id=user.id,
            status="queued",
            locations=locations,
            auto_apply=1 if auto_apply.lower() in {"1", "true", "yes", "on"} else 0,
            browser_fill=1 if browser_fill.lower() in {"1", "true", "yes", "on"} else 0,
            min_score=min_score,
            max_applications=max(1, min(int(max_applications), 20)),
            resume_path=str(dest),
            profile_json=json.dumps({"email": contact_email} if contact_email else {}),
        )
        db.add(run)
        db.commit()
        # Seed the persistent profile from this resume right away; the run
        # refreshes it with the final extracted profile when it executes.
        try:
            extracted = heuristic_profile(extract_text(str(dest)))
            if contact_email:
                extracted["email"] = contact_email
            _upsert_profile(db, run, extracted)
        except Exception:
            pass
    finally:
        db.close()

    background.add_task(_run_in_background, run_id)
    return {"id": run_id, "status": "queued"}


@app.get("/api/runs/{run_id}")
def get_run(run_id: str, request: Request):
    user = auth.current_user(request)
    db = SessionLocal()
    try:
        run = _visible_run(db, user, run_id)
        jobs = db.query(JobMatch).filter(JobMatch.run_id == run_id).order_by(JobMatch.score.desc()).all()
        apps = db.query(Application).filter(Application.run_id == run_id).order_by(Application.id.asc()).all()
        profile = {}
        try:
            profile = json.loads(run.profile_json or "{}")
        except json.JSONDecodeError:
            profile = {}
        # A match is new when no earlier run of this user's surfaced the same
        # normalized title + company. Comparison uses existing tables only,
        # and only runs created before this one count as earlier.
        prior_keys: set[str] = set()
        if run.created_at is not None:
            earlier_ids = [
                r.id
                for r in db.query(Run).filter(Run.created_at < run.created_at, Run.user_id == user.id).all()
            ]
            if earlier_ids:
                prior_keys = {
                    _match_key(r.title, r.company)
                    for r in db.query(JobMatch).filter(JobMatch.run_id.in_(earlier_ids)).all()
                }
        job_payloads = []
        for j in jobs:
            payload = _job_payload(j)
            payload["is_new"] = _match_key(j.title, j.company) not in prior_keys
            job_payloads.append(payload)
        return {
            "id": run.id,
            "status": run.status,
            "locations": run.locations,
            "auto_apply": bool(run.auto_apply),
            "browser_fill": bool(run.browser_fill),
            "min_score": run.min_score,
            "max_applications": run.max_applications,
            "summary": run.summary,
            "error": run.error,
            "profile": _public_profile(profile) if profile else {},
            "jobs": job_payloads,
            "applications": [_app_payload(a) for a in apps],
        }
    finally:
        db.close()


@app.get("/api/runs")
def list_runs(request: Request):
    user = auth.current_user(request)
    db = SessionLocal()
    try:
        auth.claim_orphans(db, user)
        rows = db.query(Run).filter(Run.user_id == user.id).order_by(Run.created_at.desc()).limit(50).all()
        return [
            {
                "id": r.id,
                "status": r.status,
                "locations": r.locations,
                "summary": r.summary,
                "error": r.error,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]
    finally:
        db.close()


@app.post("/api/runs/{run_id}/rerun")
def rerun_run(run_id: str, background: BackgroundTasks, request: Request):
    """Re-run with the original run's settings and resume. Scheduling is left
    to the caller (for example a cron job hitting this endpoint daily)."""
    user = auth.current_user(request)
    db = SessionLocal()
    try:
        original = _visible_run(db, user, run_id)
        _check_run_quota(db, user)
        seeded_email = ""
        try:
            seeded_email = (json.loads(original.profile_json or "{}") or {}).get("email") or ""
        except json.JSONDecodeError:
            seeded_email = ""
        new_id = uuid.uuid4().hex[:12]
        run = Run(
            id=new_id,
            user_id=user.id,
            status="queued",
            locations=original.locations,
            auto_apply=original.auto_apply,
            browser_fill=original.browser_fill,
            min_score=original.min_score,
            max_applications=original.max_applications,
            resume_path=original.resume_path,
            profile_json=json.dumps({"email": seeded_email} if seeded_email else {}),
        )
        db.add(run)
        db.commit()
    finally:
        db.close()

    background.add_task(_run_in_background, new_id)
    return {"id": new_id, "status": "queued"}


@app.get("/api/runs/{run_id}/events")
async def stream_events(run_id: str, request: Request):
    user = auth.current_user(request)
    db = SessionLocal()
    try:
        _visible_run(db, user, run_id)
    finally:
        db.close()

    queue = events.subscribe(run_id)

    async def gen():
        try:
            for item in events.history(run_id):
                yield f"data: {json.dumps(item)}\n\n"
            while True:
                item = await queue.get()
                yield f"data: {json.dumps(item)}\n\n"
                if item.get("type") in {"done", "error"}:
                    break
        finally:
            events.unsubscribe(run_id, queue)

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/api/applications")
def list_applications(request: Request):
    user = auth.current_user(request)
    db = SessionLocal()
    try:
        auth.claim_orphans(db, user)
        rows = (
            db.query(Application)
            .filter(Application.user_id == user.id)
            .order_by(Application.id.desc())
            .limit(100)
            .all()
        )
        return [_app_payload(a) for a in rows]
    finally:
        db.close()


def _record_application_seen(db, application, job_row, status: str) -> None:
    """Record an application that was actually sent, prepared, filled, or skipped as
    seen, so future runs skip the job. Called only once an application's
    outcome is known; failed applications record nothing."""
    if status not in {"applied", "prepared", "filled", "skipped"}:
        return
    record_seen(
        db,
        application.run_id,
        {
            "external_id": job_row.external_id if job_row else "",
            "title": application.title,
            "company": application.company,
            "url": job_row.url if job_row else application.apply_url,
        },
        status,
        application.user_id,
    )


@app.post("/api/applications/{app_id}/fill")
async def fill_application_form(app_id: int, request: Request):
    user = auth.current_user(request)
    if settings.jobapply_hosted:
        raise HTTPException(
            400,
            "In hosted mode, pre-fill runs as a streamed session: create one with "
            "POST /api/prefill-sessions for this application instead.",
        )
    db = SessionLocal()
    try:
        row = _owned_application(db, user, app_id)
        if not row.apply_url:
            raise HTTPException(400, "This application has no apply URL.")
        run = db.get(Run, row.run_id)
        job = db.get(JobMatch, row.job_id)
        if not run:
            raise HTTPException(404, "Run not found")
        try:
            profile = json.loads(run.profile_json or "{}")
        except json.JSONDecodeError:
            profile = {}
        result = await fill_application(
            profile=profile,
            job={
                "title": row.title,
                "company": row.company,
                "description": job.description if job else "",
                "apply_url": row.apply_url,
            },
            cover=row.cover_letter,
            resume_path=run.resume_path,
            location=primary_location(run.locations),
            packet_key=f"{run.id}-{row.job_id}",
            answers=profile_answers.answers_map(db, user.id),
        )
        row.status = result["status"]
        row.channel = result["channel"]
        row.notes = result["notes"]
        db.commit()
        db.refresh(row)
        _record_application_seen(db, row, job, result["status"])
        return _app_payload(row)
    finally:
        db.close()


class ApplicationUpdate(BaseModel):
    status: Literal["applied", "filled", "prepared"] | None = None
    cover_letter: str | None = None


@app.patch("/api/applications/{app_id}")
def update_application(app_id: int, body: ApplicationUpdate, request: Request):
    user = auth.current_user(request)
    db = SessionLocal()
    try:
        row = _owned_application(db, user, app_id)
        if body.cover_letter is not None:
            if row.status != "pending_review":
                raise HTTPException(400, "Only applications waiting for review can be edited")
            row.cover_letter = body.cover_letter
        if body.status is not None:
            row.status = body.status
            if body.status == "applied":
                row.notes = "Submitted by you on the employer site. " + (row.notes or "")
        db.commit()
        db.refresh(row)
        return _app_payload(row)
    finally:
        db.close()


@app.get("/api/review")
def list_pending_review(request: Request):
    """Applications waiting for a human approve/skip decision. Nothing here has been sent."""
    user = auth.current_user(request)
    db = SessionLocal()
    try:
        auth.claim_orphans(db, user)
        rows = (
            db.query(Application)
            .filter(Application.status == "pending_review", Application.user_id == user.id)
            .order_by(Application.id.asc())
            .all()
        )
        return [_app_payload(a) for a in rows]
    finally:
        db.close()


@app.post("/api/applications/{app_id}/approve")
async def approve_application(app_id: int, request: Request):
    """Approve a queued application. Sends it by email only when the run opted into
    email sending and an address plus SMTP are available; otherwise marks it prepared.
    When the run opted into browser form filling, approval also pre-fills the
    employer's application form with the reviewed cover letter, exactly like a run
    used to — approval is now the step that triggers it. (In hosted mode the
    local fill step is skipped: pre-fill there is a streamed session the user
    starts from the packet.)"""
    user = auth.current_user(request)
    db = SessionLocal()
    try:
        app = _owned_application(db, user, app_id)
        if app.status != "pending_review":
            raise HTTPException(400, "Only applications waiting for review can be approved")
        run = db.get(Run, app.run_id)
        profile = {}
        if run:
            try:
                profile = json.loads(run.profile_json or "{}")
            except json.JSONDecodeError:
                profile = {}
        job_row = db.get(JobMatch, app.job_id) if app.job_id else None
        result = apply_to_job(
            profile=profile,
            job={
                "title": app.title,
                "company": app.company,
                "location": job_row.location if job_row else "",
                "description": job_row.description if job_row else "",
                "apply_email": job_row.apply_email if job_row else "",
                "apply_url": app.apply_url,
            },
            cover=app.cover_letter,
            resume_path=run.resume_path if run else "",
            auto_apply=bool(run.auto_apply) if run else False,
        )
        if run and run.browser_fill and not settings.jobapply_hosted and result["status"] == "prepared" and app.apply_url:
            result = await fill_application(
                profile=profile,
                job={
                    "title": app.title,
                    "company": app.company,
                    "description": job_row.description if job_row else "",
                    "apply_url": app.apply_url,
                },
                cover=app.cover_letter,
                resume_path=run.resume_path,
                location=primary_location(run.locations),
                packet_key=f"{run.id}-{app.job_id}",
                answers=profile_answers.answers_map(db, user.id),
            )
        app.status = result["status"]
        app.channel = result["channel"]
        app.notes = result["notes"]
        db.commit()
        db.refresh(app)
        _record_application_seen(db, app, job_row, result["status"])
        return _app_payload(app)
    finally:
        db.close()


@app.post("/api/applications/{app_id}/skip")
def skip_application(app_id: int, request: Request):
    user = auth.current_user(request)
    db = SessionLocal()
    try:
        app = _owned_application(db, user, app_id)
        if app.status != "pending_review":
            raise HTTPException(400, "Only applications waiting for review can be skipped")
        job_row = db.get(JobMatch, app.job_id) if app.job_id else None
        app.status = "skipped"
        app.notes = "Skipped during review."
        db.commit()
        db.refresh(app)
        _record_application_seen(db, app, job_row, "skipped")
        return _app_payload(app)
    finally:
        db.close()


def _visible_companies(db, user: auth.AuthUser):
    """Companies a user can see: the global starter list (user_id NULL) plus
    their own additions."""
    return (
        db.query(Company)
        .filter((Company.user_id.is_(None)) | (Company.user_id == user.id))
        .order_by(Company.source.desc(), Company.name)
        .all()
    )


@app.get("/api/companies")
def list_companies(request: Request):
    user = auth.current_user(request)
    db = SessionLocal()
    try:
        rows = _visible_companies(db, user)
        enabled = [c for c in rows if c.enabled]
        return {
            "companies": [company_payload(c) for c in rows],
            "summary": {
                "total": len(rows),
                "enabled": len(enabled),
                "user_added": sum(1 for c in rows if c.source == "user" and c.user_id == user.id),
                "open_jobs": sum(c.job_count for c in enabled),
                "problems": sum(1 for c in enabled if c.status in {"not_found", "error"}),
            },
        }
    finally:
        db.close()


class CompanyCreate(BaseModel):
    query: str


@app.post("/api/companies")
async def create_company(body: CompanyCreate, request: Request):
    user = auth.current_user(request)
    db = SessionLocal()
    try:
        async with httpx.AsyncClient(timeout=25.0, follow_redirects=True) as client:
            try:
                row, created = await add_company(db, client, body.query, user_id=user.id)
            except ResolveError as exc:
                raise HTTPException(422, str(exc)) from exc
        return {"company": company_payload(row), "created": created}
    finally:
        db.close()


def _visible_company(db, user: auth.AuthUser, company_id: int) -> Company:
    row = db.get(Company, company_id)
    if row is None or (row.user_id is not None and row.user_id != user.id):
        raise HTTPException(404, "Company not found")
    if row.user_id is None and user.role != "admin":
        raise HTTPException(403, "Only admins can change the shared company list.")
    return row


class CompanyUpdate(BaseModel):
    enabled: bool


@app.patch("/api/companies/{company_id}")
def update_company(company_id: int, body: CompanyUpdate, request: Request):
    user = auth.current_user(request)
    db = SessionLocal()
    try:
        row = _visible_company(db, user, company_id)
        row.enabled = 1 if body.enabled else 0
        db.commit()
        db.refresh(row)
        return company_payload(row)
    finally:
        db.close()


@app.delete("/api/companies/{company_id}")
def delete_company(company_id: int, request: Request):
    """User-added companies are deleted; starter companies are disabled so a later sync won't re-add them."""
    user = auth.current_user(request)
    db = SessionLocal()
    try:
        row = _visible_company(db, user, company_id)
        if row.source == "starter":
            row.enabled = 0
            db.commit()
            return {"deleted": False, "company": company_payload(row)}
        db.delete(row)
        db.commit()
        return {"deleted": True}
    finally:
        db.close()


class JobLink(BaseModel):
    url: str
    run_id: str = ""
    fill: bool = True


def _profile_run(db, run_id: str, user_id: int) -> Run | None:
    if run_id:
        run = db.get(Run, run_id)
        return run if run is not None and run.user_id == user_id else None
    for run in (
        db.query(Run).filter(Run.user_id == user_id).order_by(Run.created_at.desc()).limit(20)
    ):
        try:
            if json.loads(run.profile_json or "{}").get("name"):
                return run
        except json.JSONDecodeError:
            continue
    return None


@app.post("/api/jobs/from-link")
async def apply_from_link(body: JobLink, request: Request):
    """Prepare (and optionally pre-fill) an application for one specific job link."""
    user = auth.current_user(request)
    db = SessionLocal()
    try:
        auth.claim_orphans(db, user)
        run = _profile_run(db, body.run_id, user.id)
        if not run:
            raise HTTPException(409, "Run the agent once with your resume first, so the app knows your profile.")
        profile = json.loads(run.profile_json or "{}")
        async with httpx.AsyncClient(timeout=25.0, follow_redirects=True) as client:
            try:
                job = await resolve_job_url(client, body.url)
            except ResolveError as exc:
                raise HTTPException(422, str(exc)) from exc

        row = JobMatch(
            run_id=run.id,
            external_id=job.get("external_id") or "",
            title=job.get("title") or "",
            company=job.get("company") or "",
            location=job.get("location") or "",
            source=job.get("source") or "Link",
            url=body.url,
            apply_url=job.get("apply_url") or body.url,
            description=job.get("description") or "",
            score=0,
            reasons="Added from a job link",
            remote=1 if job.get("remote") else 0,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        letter = cover_letter(profile, job)
        result = {"status": "prepared", "channel": "ats_link", "notes": "Packet ready. Open the apply link to submit."}
        if body.fill:
            result = await fill_application(
                profile=profile,
                job={"title": row.title, "company": row.company, "apply_url": row.apply_url},
                cover=letter,
                resume_path=run.resume_path,
                location=primary_location(run.locations),
                packet_key=f"{run.id}-{row.id}",
                answers=profile_answers.answers_map(db, user.id),
            )
        application = Application(
            run_id=run.id,
            user_id=user.id,
            job_id=row.id,
            title=row.title,
            company=row.company,
            status=result["status"],
            channel=result["channel"],
            cover_letter=letter,
            notes=result["notes"],
            apply_url=row.apply_url,
        )
        db.add(application)
        db.commit()
        db.refresh(application)
        return {"job": _job_payload(row), "application": _app_payload(application)}
    finally:
        db.close()


PIPELINE_STAGES = {"applied", "reply", "screen", "interview", "offer", "rejected", "ghosted"}


class TrackerUpdate(BaseModel):
    pipeline_stage: str | None = None
    user_notes: str | None = None
    follow_up_date: str | None = None


@app.patch("/api/applications/{app_id}/tracker")
def update_tracker(app_id: int, update: TrackerUpdate, request: Request):
    """Edit an application's pipeline stage, personal notes, or follow-up date."""
    from datetime import datetime as _datetime

    user = auth.current_user(request)
    db = SessionLocal()
    try:
        app = _owned_application(db, user, app_id)
        if update.pipeline_stage is not None:
            if update.pipeline_stage not in PIPELINE_STAGES:
                raise HTTPException(400, f"Unknown pipeline stage: {update.pipeline_stage}")
            app.pipeline_stage = update.pipeline_stage
        if update.user_notes is not None:
            app.user_notes = update.user_notes
        if update.follow_up_date is not None:
            if update.follow_up_date:
                try:
                    _datetime.strptime(update.follow_up_date, "%Y-%m-%d")
                except ValueError:
                    raise HTTPException(400, "follow_up_date must be YYYY-MM-DD")
            app.follow_up_date = update.follow_up_date
        db.commit()
        db.refresh(app)
        return _app_payload(app)
    finally:
        db.close()


@app.get("/api/seen")
def list_seen_jobs(request: Request):
    """Jobs already applied to or prepared in an earlier run, newest first.
    These are excluded from future runs by default."""
    user = auth.current_user(request)
    db = SessionLocal()
    try:
        auth.claim_orphans(db, user)
        rows = (
            db.query(SeenJob)
            .filter(SeenJob.user_id == user.id)
            .order_by(SeenJob.created_at.desc())
            .limit(200)
            .all()
        )
        return [
            {
                "id": r.id,
                "title": r.title,
                "company": r.company,
                "url": r.url,
                "status": r.status,
                "run_id": r.run_id,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]
    finally:
        db.close()
