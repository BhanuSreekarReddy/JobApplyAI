from datetime import datetime

from sqlalchemy import DateTime, Float, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String, primary_key=True)
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
    __table_args__ = (UniqueConstraint("ats", "slug", name="uq_company_board"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String, default="")
    ats: Mapped[str] = mapped_column(String, default="")
    slug: Mapped[str] = mapped_column(String, default="")
    source: Mapped[str] = mapped_column(String, default="user")  # starter | user
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
    external_id: Mapped[str] = mapped_column(String, default="")
    title: Mapped[str] = mapped_column(String, default="")
    company: Mapped[str] = mapped_column(String, default="")
    url: Mapped[str] = mapped_column(String, default="")
    key: Mapped[str] = mapped_column(String, index=True, default="")
    status: Mapped[str] = mapped_column(String, default="prepared")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
