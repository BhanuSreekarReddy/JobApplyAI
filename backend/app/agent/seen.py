from __future__ import annotations

import re
from typing import Any

from sqlalchemy.orm import Session

from ..models import SeenJob


def normalize_key(title: str, company: str) -> str:
    """Same normalization search._dedupe uses, so cross-board re-posts of one
    role collapse to the same key even when the source differs."""
    return re.sub(r"\W+", "", f"{title or ''}{company or ''}".lower())


def find_seen(db: Session, job: dict[str, Any]) -> SeenJob | None:
    row = None
    if job.get("external_id"):
        row = db.query(SeenJob).filter(SeenJob.external_id == job["external_id"]).first()
    if row is None:
        key = normalize_key(job.get("title") or "", job.get("company") or "")
        if key:
            row = db.query(SeenJob).filter(SeenJob.key == key).first()
    return row


def is_seen(db: Session, job: dict[str, Any]) -> bool:
    return find_seen(db, job) is not None


def record_seen(db: Session, run_id: str, job: dict[str, Any], status: str) -> SeenJob:
    """Record a prepared or sent application. Re-recording only refreshes the
    status and run; the original first-seen date is kept."""
    existing = find_seen(db, job)
    if existing is not None:
        existing.status = status
        existing.run_id = run_id
        db.commit()
        db.refresh(existing)
        return existing
    row = SeenJob(
        run_id=run_id,
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
