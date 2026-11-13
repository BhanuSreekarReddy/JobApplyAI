from datetime import datetime

from sqlalchemy import DateTime, Float, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class User(Base):
    """A login account. The job-seeker profile lives in Profile; this row is
    identity, credentials, and role only."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String, unique=True, index=True, default="")
    password_hash: Mapped[str] = mapped_column(String, default="")
    role: Mapped[str] = mapped_column(String, default="user")  # user | admin
    is_active: Mapped[int] = mapped_column(Integer, default=1)
    email_verified: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_login_at: Mapped[datetime] = mapped_column(DateTime, nullable=True, default=None)


class Invite(Base):
    """A single-use registration invite. Registration is invite-only."""

    __tablename__ = "invites"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String, unique=True, index=True, default="")
    created_by: Mapped[int] = mapped_column(Integer, nullable=True, default=None)
    used_by: Mapped[int] = mapped_column(Integer, nullable=True, default=None)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class AuthToken(Base):
    """Single-use, expiring tokens for email verification and password reset.
    Only the hash of the token is stored."""

    __tablename__ = "auth_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True, default=0)
    purpose: Mapped[str] = mapped_column(String, default="")  # verify | reset
    token_hash: Mapped[str] = mapped_column(String, unique=True, index=True, default="")
    expires_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    used_at: Mapped[datetime] = mapped_column(DateTime, nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class UserSession(Base):
    """A server-side login session; the cookie carries the raw token, the
    table carries only its hash."""

    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True, default=0)
    token_hash: Mapped[str] = mapped_column(String, unique=True, index=True, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class EmailOutbox(Base):
    """Every email the app sends (or would send, when SMTP is not configured)
    is recorded here, so verification and reset flows can be completed and
    tested without a mail server."""

    __tablename__ = "email_outbox"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    to_email: Mapped[str] = mapped_column(String, default="")
    subject: Mapped[str] = mapped_column(String, default="")
    body: Mapped[str] = mapped_column(Text, default="")
    kind: Mapped[str] = mapped_column(String, default="")  # verify | reset
    sent: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Profile(Base):
    """The persistent job-seeker profile: one per user, seeded from the
    resume extractor and editable. Runs snapshot it into profile_json."""

    __tablename__ = "profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, unique=True, index=True, default=0)
    name: Mapped[str] = mapped_column(String, default="")
    contact_email: Mapped[str] = mapped_column(String, default="")
    phone: Mapped[str] = mapped_column(String, default="")
    linkedin: Mapped[str] = mapped_column(String, default="")
    github: Mapped[str] = mapped_column(String, default="")
    titles: Mapped[str] = mapped_column(Text, default="[]")  # JSON list
    skills: Mapped[str] = mapped_column(Text, default="[]")  # JSON list
    years_experience: Mapped[int] = mapped_column(Integer, nullable=True, default=None)
    summary: Mapped[str] = mapped_column(Text, default="")
    locations: Mapped[str] = mapped_column(Text, default="")


class ProfileAnswer(Base):
    """One saved answer to a repeat application question, stored on the
    user's profile (the "answer bank"). `key` is a canonical category key
    (work_authorization, sponsorship, ...) when the question matches a known
    category, otherwise the normalized question label. The form filler may
    fill a sensitive/blocklisted field ONLY from a row here — never from a
    guess. One row per (user, key)."""

    __tablename__ = "profile_answers"
    __table_args__ = (UniqueConstraint("user_id", "key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True, default=0)
    key: Mapped[str] = mapped_column(String, default="")
    question: Mapped[str] = mapped_column(String, default="")
    answer: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class AdminAuditLog(Base):
    __tablename__ = "admin_audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    admin_id: Mapped[int] = mapped_column(Integer, default=0)
    action: Mapped[str] = mapped_column(String, default="")
    target_user_id: Mapped[int] = mapped_column(Integer, nullable=True, default=None)
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class PrefillSessionRecord(Base):
    """Audit/status record for a streamed browser pre-fill session. The live
    browser state is held in memory by prefill_sessions; this row is what
    survives for listing and cleanup."""

    __tablename__ = "prefill_sessions"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True, default=0)
    application_id: Mapped[int] = mapped_column(Integer, default=0)
    apply_url: Mapped[str] = mapped_column(String, default="")
    status: Mapped[str] = mapped_column(String, default="starting")  # starting | filling | ready | closed | error
    filled_json: Mapped[str] = mapped_column(Text, default="[]")
    needs_attention_json: Mapped[str] = mapped_column(Text, default="[]")
    notes: Mapped[str] = mapped_column(Text, default="")
    profile_dir: Mapped[str] = mapped_column(String, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_activity_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    closed_at: Mapped[datetime] = mapped_column(DateTime, nullable=True, default=None)


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=True, index=True, default=None)
    status: Mapped[str] = mapped_column(String, default="queued")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    locations: Mapped[str] = mapped_column(Text, default="")
    auto_apply: Mapped[int] = mapped_column(Integer, default=1)
    browser_fill: Mapped[int] = mapped_column(Integer, default=0)
    min_score: Mapped[float] = mapped_column(Float, default=70.0)
    max_applications: Mapped[int] = mapped_column(Integer, default=8)
    resume_path: Mapped[str] = mapped_column(String, default="")
    profile_json: Mapped[str] = mapped_column(Text, default="{}")
    summary: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str] = mapped_column(Text, default="")


class JobMatch(Base):
    __tablename__ = "job_matches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String, index=True)
    external_id: Mapped[str] = mapped_column(String, default="")
    title: Mapped[str] = mapped_column(String, default="")
    company: Mapped[str] = mapped_column(String, default="")
    location: Mapped[str] = mapped_column(String, default="")
    source: Mapped[str] = mapped_column(String, default="")
    url: Mapped[str] = mapped_column(String, default="")
    apply_url: Mapped[str] = mapped_column(String, default="")
    apply_email: Mapped[str] = mapped_column(String, default="")
    description: Mapped[str] = mapped_column(Text, default="")
    score: Mapped[float] = mapped_column(Float, default=0)
    reasons: Mapped[str] = mapped_column(Text, default="")
    remote: Mapped[int] = mapped_column(Integer, default=0)
    posted_date: Mapped[str] = mapped_column(String, default="")


class Application(Base):
    __tablename__ = "applications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String, index=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=True, index=True, default=None)
    job_id: Mapped[int] = mapped_column(Integer, default=0)
    title: Mapped[str] = mapped_column(String, default="")
    company: Mapped[str] = mapped_column(String, default="")
    status: Mapped[str] = mapped_column(String, default="prepared")
    channel: Mapped[str] = mapped_column(String, default="")
    cover_letter: Mapped[str] = mapped_column(Text, default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    apply_url: Mapped[str] = mapped_column(String, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    # Pipeline tracking, edited by the user in the tracker tab. `notes` stays
    # as the system's note; `user_notes` is the user's own free text.
    pipeline_stage: Mapped[str] = mapped_column(String, default="applied")
    user_notes: Mapped[str] = mapped_column(Text, default="")
    follow_up_date: Mapped[str] = mapped_column(String, default="")


class Company(Base):
    """A company whose public ATS job board is searched on every run."""

    __tablename__ = "companies"
    # No unique constraint on (ats, slug): the same board can be a global
    # starter row and also appear in individual users' lists.

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String, default="")
    ats: Mapped[str] = mapped_column(String, default="")
    slug: Mapped[str] = mapped_column(String, default="")
    source: Mapped[str] = mapped_column(String, default="user")  # starter | user
    # NULL = the global starter list, visible to everyone. A user id means the
    # company was added by that user and is visible only to them.
    user_id: Mapped[int] = mapped_column(Integer, nullable=True, index=True, default=None)
    enabled: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String, default="unchecked")  # ok | not_found | error | unchecked
    job_count: Mapped[int] = mapped_column(Integer, default=0)
    note: Mapped[str] = mapped_column(Text, default="")
    last_checked: Mapped[datetime] = mapped_column(DateTime, nullable=True, default=None)


class SeenJob(Base):
    """Every job an application was prepared or sent for, across all runs.

    The guard against re-applying: before a run stores matches, it drops any
    job whose normalized company + title (or exact external id) is already
    here. Within a single run, search._dedupe already collapses cross-board
    re-posts of the same role using the same normalization.
    """

    __tablename__ = "seen_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String, index=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=True, index=True, default=None)
    external_id: Mapped[str] = mapped_column(String, default="")
    title: Mapped[str] = mapped_column(String, default="")
    company: Mapped[str] = mapped_column(String, default="")
    url: Mapped[str] = mapped_column(String, default="")
    key: Mapped[str] = mapped_column(String, index=True, default="")
    status: Mapped[str] = mapped_column(String, default="prepared")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
