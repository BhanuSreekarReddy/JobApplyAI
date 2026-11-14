from __future__ import annotations

import re
from typing import Any

from sqlalchemy.orm import Session

from ..models import SeenJob


def normalize_key(title: str, company: str) -> str:
    """Same normalization search._dedupe uses, so cross-board re-posts of one
    role collapse to the same key even when the source differs."""
    return re.sub(r"\W+", "", f"{title or ''}{company or ''}".lower())


def _scoped(query, user_id: int | None):
    """Seen rows are per-user; legacy rows (user_id NULL) match only a
    user-less (None) lookup."""
    if user_id is None:
        return query.filter(SeenJob.user_id.is_(None))
    return query.filter(SeenJob.user_id == user_id)


def find_seen(db: Session, job: dict[str, Any], user_id: int | None = None) -> SeenJob | None:
    row = None
    if job.get("external_id"):
        row = _scoped(db.query(SeenJob).filter(SeenJob.external_id == job["external_id"]), user_id).first()
    if row is None:
        key = normalize_key(job.get("title") or "", job.get("company") or "")
        if key:
            row = _scoped(db.query(SeenJob).filter(SeenJob.key == key), user_id).first()
    return row


def is_seen(db: Session, job: dict[str, Any], user_id: int | None = None) -> bool:
    return find_seen(db, job, user_id) is not None


# Application outcomes that count as "previously applied": the job was
# actually sent, prepared, filled, or deliberately skipped. Anything else
# (pending_review, failed, ...) records nothing.
RECORDED_STATUSES = {"applied", "prepared", "filled", "skipped"}


def record_application_seen(db: Session, application, job, status: str) -> None:
    """Record an application whose outcome is known as seen, so future runs
    skip the job. Every path that moves an application to an outcome status
    must call this — approve, skip, local fill, the streamed pre-fill
    session, and manual status marks from the tracker."""
    if status not in RECORDED_STATUSES:
        return
    record_seen(
        db,
        application.run_id,
        {
            "external_id": job.external_id if job else "",
            "title": application.title,
            "company": application.company,
            "url": job.url if job else application.apply_url,
        },
        status,
        application.user_id,
    )


# The one application status that means the application actually went out:
# the user marked it submitted, or the approval email really was sent.
# "filled" only means a browser form was pre-filled — nothing was submitted,
# the user still clicks Submit — and "prepared" is a packet nobody sent.
SUBMITTED_STATUS = "applied"


def advance_stage_on_submission(db: Session, application, status: str) -> None:
    """Move a newly submitted application into the tracker's pipeline.

    Called alongside record_application_seen from every outcome path. Only a
    genuine submission (status "applied") advances the stage, and only from
    "not_applied" — a stage the user has since moved further along (screen,
    interview, ...) is theirs and is never overwritten."""
    if status != SUBMITTED_STATUS:
        return
    if (application.pipeline_stage or "not_applied") != "not_applied":
        return
    application.pipeline_stage = "applied"
    db.commit()


def record_seen(db: Session, run_id: str, job: dict[str, Any], status: str, user_id: int | None = None) -> SeenJob:
    """Record a prepared or sent application. Re-recording only refreshes the
    status and run; the original first-seen date is kept."""
    existing = find_seen(db, job, user_id)
    if existing is not None:
        existing.status = status
        existing.run_id = run_id
        db.commit()
        db.refresh(existing)
        return existing
    row = SeenJob(
        run_id=run_id,
        user_id=user_id,
        external_id=job.get("external_id") or "",
        title=job.get("title") or "",
        company=job.get("company") or "",
        url=job.get("url") or "",
        key=normalize_key(job.get("title") or "", job.get("company") or ""),
        status=status,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row
