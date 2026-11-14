"""The profile a fill actually uses.

Runs snapshot the extracted resume profile into ``run.profile_json`` at
execution time, and every fill path (approve, standalone fill, job-link,
streamed session) historically read only that snapshot. The persistent,
user-editable Profile row (GET/PUT /api/profile) was never consulted, so
correcting a value on the profile page — a mis-extracted name, the
first/last split, a new phone number — never reached any form.

``fill_profile`` is the single merge every fill path now uses: the run
snapshot is the base (it carries resume-only material like skills and the
raw excerpt), and the user's saved profile wins for every field it
actually holds. An untouched or missing profile row leaves the snapshot
exactly as it was.
"""
from __future__ import annotations

import json
from typing import Any

from .agent.profile import split_name
from .models import Profile, Run


def fill_profile(db, user_id: int, run: Run | None) -> dict[str, Any]:
    """Merge a run's profile snapshot with the user's persistent profile.

    The persistent row wins for the fields it holds (name and its split,
    contact email -> ``email``, phone, LinkedIn, GitHub, years, summary,
    and the titles/skills lists when non-empty); everything else comes
    from the snapshot.
    """
    snapshot: dict[str, Any] = {}
    if run is not None:
        try:
            parsed = json.loads(run.profile_json or "{}")
            if isinstance(parsed, dict):
                snapshot = parsed
        except json.JSONDecodeError:
            snapshot = {}
    if not user_id:
        return snapshot
    row = db.query(Profile).filter(Profile.user_id == user_id).first()
    if row is None:
        return snapshot

    merged = dict(snapshot)
    first = (row.first_name or "").strip()
    last = (row.last_name or "").strip()
    name = (row.name or "").strip()
    if first or last:
        merged["first_name"] = first
        merged["last_name"] = last
        merged["name"] = name or " ".join(p for p in (first, last) if p)
    elif name:
        merged["name"] = name
        merged["first_name"], merged["last_name"] = split_name(name)
    if (row.contact_email or "").strip():
        merged["email"] = row.contact_email.strip()
    for field in ("phone", "linkedin", "github", "summary"):
        value = (getattr(row, field) or "").strip()
        if value:
            merged[field] = value
    if row.years_experience is not None:
        merged["years_experience"] = row.years_experience
    for field in ("titles", "skills"):
        try:
            value = json.loads(getattr(row, field) or "[]")
        except json.JSONDecodeError:
            value = []
        if isinstance(value, list) and value:
            merged[field] = value
    return merged
