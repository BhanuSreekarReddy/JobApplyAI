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
