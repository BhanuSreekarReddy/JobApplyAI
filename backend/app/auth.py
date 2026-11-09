"""Accounts, sessions, invites, and email tokens.

Passwords are hashed with argon2id (pbkdf2-sha256 fallback if argon2-cffi is
unavailable). Session and action tokens are random; only their SHA-256 hash
is stored, so a database leak does not leak usable credentials.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import smtplib
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.message import EmailMessage

from fastapi import HTTPException, Request, WebSocket
from sqlalchemy.orm import Session

from .config import settings
from .models import (
    Application,
    AuthToken,
    EmailOutbox,
    Invite,
    JobMatch,
    PrefillSessionRecord,
    Profile,
    Run,
    SeenJob,
    User,
    UserSession,
)

log = logging.getLogger("jobapply.auth")

SESSION_COOKIE = "jobapply_session"
SESSION_TTL = timedelta(days=30)
TOKEN_TTL = timedelta(hours=24)
RESET_TTL = timedelta(hours=2)

try:  # argon2id preferred
    from argon2 import PasswordHasher

    _hasher = PasswordHasher()
except Exception:  # pragma: no cover - exercised only without argon2-cffi
    _hasher = None

_PBKDF2_ITERATIONS = 600_000


def hash_password(password: str) -> str:
    if _hasher is not None:
        return _hasher.hash(password)
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ITERATIONS)
    return f"pbkdf2${_PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    if not stored:
        return False
    if stored.startswith("pbkdf2$"):
        try:
            _scheme, iterations, salt_hex, digest_hex = stored.split("$")
            digest = hashlib.pbkdf2_hmac(
                "sha256", password.encode(), bytes.fromhex(salt_hex), int(iterations)
            )
            return hmac.compare_digest(digest.hex(), digest_hex)
        except Exception:
            return False
    if _hasher is None:
        return False
    try:
        return _hasher.verify(stored, password)
    except Exception:
        return False


def _token_hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def admin_emails() -> set[str]:
    return {e.strip().lower() for e in (settings.admin_emails or "").split(",") if e.strip()}


def is_seeded_admin_email(email: str) -> bool:
    return (email or "").strip().lower() in admin_emails()


@dataclass
class AuthUser:
    """A session-independent snapshot of the logged-in user."""

    id: int
    email: str
    role: str
    is_active: bool
    email_verified: bool
    seeded_admin: bool


def snapshot(user: User) -> AuthUser:
    return AuthUser(
        id=user.id,
        email=user.email,
        role=user.role,
        is_active=bool(user.is_active),
        email_verified=bool(user.email_verified),
        seeded_admin=is_seeded_admin_email(user.email),
    )


def user_payload(user: User | AuthUser) -> dict:
    seeded = user.seeded_admin if isinstance(user, AuthUser) else is_seeded_admin_email(user.email)
    return {
        "id": user.id,
        "email": user.email,
        "role": user.role,
        "is_active": bool(user.is_active),
        "email_verified": bool(user.email_verified),
        "seeded_admin": seeded,
    }


# ---------------------------------------------------------------- sessions

def create_session(db: Session, user: User) -> str:
    raw = secrets.token_urlsafe(32)
    now = datetime.utcnow()
    db.add(
        UserSession(
            user_id=user.id,
            token_hash=_token_hash(raw),
            created_at=now,
            expires_at=now + SESSION_TTL,
            last_seen_at=now,
        )
    )
    db.commit()
    return raw


def user_for_session_token(db: Session, raw: str | None) -> User | None:
    if not raw:
        return None
    row = db.query(UserSession).filter(UserSession.token_hash == _token_hash(raw)).first()
    if row is None or row.expires_at < datetime.utcnow():
        return None
    user = db.get(User, row.user_id)
    if user is None or not user.is_active:
        return None
    row.last_seen_at = datetime.utcnow()
    db.commit()
    return user


def delete_session(db: Session, raw: str | None) -> None:
    if not raw:
        return
    row = db.query(UserSession).filter(UserSession.token_hash == _token_hash(raw)).first()
    if row is not None:
        db.delete(row)
        db.commit()


# ---------------------------------------------------------------- login throttling

_attempts: dict[str, list[float]] = {}
MAX_FAILED_LOGINS = 10
LOGIN_WINDOW_SECONDS = 15 * 60


def login_rate_limited(email: str) -> bool:
    now = time.time()
    recent = [t for t in _attempts.get(email, []) if now - t < LOGIN_WINDOW_SECONDS]
    _attempts[email] = recent
    return len(recent) >= MAX_FAILED_LOGINS


def record_failed_login(email: str) -> None:
    _attempts.setdefault(email, []).append(time.time())


def clear_failed_logins(email: str) -> None:
    _attempts.pop(email, None)


# ---------------------------------------------------------------- email + tokens

def create_action_token(db: Session, user: User, purpose: str) -> str:
    raw = secrets.token_urlsafe(32)
    ttl = RESET_TTL if purpose == "reset" else TOKEN_TTL
    db.add(
        AuthToken(
            user_id=user.id,
            purpose=purpose,
            token_hash=_token_hash(raw),
            expires_at=datetime.utcnow() + ttl,
        )
    )
    db.commit()
    return raw


def consume_action_token(db: Session, raw: str, purpose: str) -> User | None:
    row = (
        db.query(AuthToken)
        .filter(AuthToken.token_hash == _token_hash(raw), AuthToken.purpose == purpose)
        .first()
    )
    if row is None or row.used_at is not None or row.expires_at < datetime.utcnow():
        return None
    row.used_at = datetime.utcnow()
    db.commit()
    return db.get(User, row.user_id)


def send_email(db: Session, to_email: str, subject: str, body: str, kind: str) -> EmailOutbox:
    """Record the message in the outbox (always) and send it via SMTP when
    configured. The outbox is what lets verification/reset flows complete in
    local runs and tests without a mail server."""
    sent = 0
    if settings.smtp_host:
        try:
            msg = EmailMessage()
            msg["From"] = settings.smtp_from or settings.smtp_user
            msg["To"] = to_email
            msg["Subject"] = subject
            msg.set_content(body)
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as smtp:
                smtp.starttls()
                if settings.smtp_user:
                    smtp.login(settings.smtp_user, settings.smtp_password)
                smtp.send_message(msg)
            sent = 1
        except Exception as exc:  # mail must never break the request flow
            log.warning("SMTP send to %s failed: %s", to_email, exc)
    row = EmailOutbox(to_email=to_email, subject=subject, body=body, kind=kind, sent=sent)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def send_verification_email(db: Session, user: User) -> None:
    raw = create_action_token(db, user, "verify")
    send_email(
        db,
        user.email,
        "Verify your JobApply Agent account",
        "Welcome to JobApply Agent.\n\n"
        f"Verify your email address by opening this link:\n/verify-email?token={raw}\n\n"
        "The link expires in 24 hours. If you did not create this account, ignore this email.",
        "verify",
    )


def send_password_reset_email(db: Session, user: User) -> None:
    raw = create_action_token(db, user, "reset")
    send_email(
        db,
        user.email,
        "Reset your JobApply Agent password",
        "A password reset was requested for your JobApply Agent account.\n\n"
        f"Open this link to choose a new password:\n/reset-password?token={raw}\n\n"
        "The link expires in 2 hours and can be used once. If you did not request "
        "this, ignore this email — your password is unchanged.",
        "reset",
    )


# ---------------------------------------------------------------- ownership helpers

def claim_orphans(db: Session, user: User | AuthUser) -> None:
    """Assign pre-accounts rows (user_id NULL) to the first seeded admin.

    Applications and seen rows follow their run; leftover seen rows with no
    claimed run go to the admin as well. Companies are NOT claimed: a NULL
    company user_id means the global starter list by design.
    """
    seeded = user.seeded_admin if isinstance(user, AuthUser) else is_seeded_admin_email(user.email)
    if not seeded:
        return
    orphan_runs = db.query(Run).filter(Run.user_id.is_(None)).all()
    if not orphan_runs:
        return
    run_ids = [r.id for r in orphan_runs]
    for run in orphan_runs:
        run.user_id = user.id
    db.query(Application).filter(
        Application.user_id.is_(None), Application.run_id.in_(run_ids)
    ).update({Application.user_id: user.id}, synchronize_session=False)
    db.query(SeenJob).filter(SeenJob.user_id.is_(None)).update(
        {SeenJob.user_id: user.id}, synchronize_session=False
    )
    db.commit()


def delete_user_data(db: Session, user_id: int) -> None:
    """Delete a user and everything they own. Caller handles audit + commit."""
    run_ids = [r.id for r in db.query(Run.id).filter(Run.user_id == user_id)]
    if run_ids:
        db.query(JobMatch).filter(JobMatch.run_id.in_(run_ids)).delete(synchronize_session=False)
    db.query(PrefillSessionRecord).filter(PrefillSessionRecord.user_id == user_id).delete(synchronize_session=False)
    db.query(Application).filter(Application.user_id == user_id).delete(synchronize_session=False)
    db.query(SeenJob).filter(SeenJob.user_id == user_id).delete(synchronize_session=False)
    db.query(Run).filter(Run.user_id == user_id).delete(synchronize_session=False)
    db.query(Profile).filter(Profile.user_id == user_id).delete(synchronize_session=False)
    db.query(UserSession).filter(UserSession.user_id == user_id).delete(synchronize_session=False)
    db.query(AuthToken).filter(AuthToken.user_id == user_id).delete(synchronize_session=False)
    db.query(Invite).filter((Invite.used_by == user_id) | (Invite.created_by == user_id)).delete(
        synchronize_session=False
    )
    user = db.get(User, user_id)
    if user is not None:
        db.delete(user)


# ---------------------------------------------------------------- request guard

_PUBLIC_PREFIXES = ("/api/public/",)
_PUBLIC_PATHS = {
    "/api/health",
    "/api/config",
    # Only the credential-flow endpoints are public; /api/auth/me, logout,
    # export, and account deletion require a session like everything else.
    "/api/auth/register",
    "/api/auth/login",
    "/api/auth/verify-email",
    "/api/auth/request-password-reset",
    "/api/auth/reset-password",
}


async def auth_guard(request: Request = None, websocket: WebSocket = None) -> None:
    """Global guard: everything under /api requires a logged-in, active user
    except the credential-flow endpoints, the public summary, and health.
    WebSocket routes do their own cookie auth and are skipped here (FastAPI
    solves this dependency with whichever scope object the route uses)."""
    scope_owner = request if request is not None else websocket
    if scope_owner is None or scope_owner.scope.get("type") != "http":
        return
    request = scope_owner
    path = request.url.path
    if not path.startswith("/api/") or path in _PUBLIC_PATHS or path.startswith(_PUBLIC_PREFIXES):
        return
    if path.endswith("/stream"):
        return
    from .db import SessionLocal

    db = SessionLocal()
    try:
        user = user_for_session_token(db, request.cookies.get(SESSION_COOKIE))
        if user is None:
            raise HTTPException(401, "Sign in required")
        request.state.user = snapshot(user)
    finally:
        db.close()


def current_user(request: Request) -> AuthUser:
    user = getattr(request.state, "user", None)
    if user is None:
        raise HTTPException(401, "Sign in required")
    return user


def require_admin(request: Request) -> AuthUser:
    user = current_user(request)
    if user.role != "admin":
        raise HTTPException(403, "Admin access required")
    return user
