from __future__ import annotations

import re
from typing import Any

from .locations import Wanted, match_location

LOCATION_POINTS = {"city": 25.0, "state": 22.0, "remote": 22.0, "country": 18.0, "any": 10.0}
ENTRY_LEVEL_RE = re.compile(
    r"\b(intern(ship)?|new grad(uate)?|graduate|entry[- ]level|junior|jr|apprentice|co-?op|university|campus|early career)\b"
    r"|\b20\d\d (grad|start)",
)


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9+#.]{2,}", text.lower()) if len(t) > 1}


def _seniority(years: int) -> set[str]:
    if years <= 2:
        return {"junior", "associate", "intern", "i", "entry"}
    if years <= 5:
        return {"mid", "intermediate", "ii", "engineer"}
    if years <= 8:
        return {"senior", "sr", "iii"}
    return {"staff", "principal", "lead", "senior", "staff"}


def score_job(job: dict[str, Any], profile: dict[str, Any], wanted: Wanted) -> tuple[float, list[str]]:
    blob = f"{job.get('title','')} {job.get('description','')} {job.get('location','')}".lower()
    job_tokens = _tokens(blob)
    skills = [s.lower() for s in profile.get("skills") or []]
    titles = [t.lower() for t in profile.get("titles") or []]
    years = int(profile.get("years_experience") or 3)
    reasons: list[str] = []

    skill_hits = [s for s in skills if s in blob or any(part in job_tokens for part in s.split())]
    skill_score = min(40.0, len(skill_hits) * 6.5)
    if skill_hits:
        reasons.append(f"Skills: {', '.join(skill_hits[:6])}")

    title_score = 0.0
    title_text = (job.get("title") or "").lower()
    for title in titles:
        overlap = len(_tokens(title) & _tokens(title_text))
        if title in title_text or overlap >= 2:
            title_score = 30.0
            reasons.append(f"Title aligns with {title}")
            break
        if overlap:
            title_score = max(title_score, 14.0)

    kind = job.get("location_match")
    reason = job.get("location_reason") or ""
    if kind is None:
        kind, reason = match_location(job.get("location") or "", bool(job.get("remote")), wanted)
    loc_score = LOCATION_POINTS.get(kind, 0.0)
    if reason:
        reasons.append(reason)

    sen_score = 8.0
    penalty = 0.0
    tags = _seniority(years)
    if any(tag in re.findall(r"[a-z]+", title_text) for tag in tags):
        sen_score = 15.0
        reasons.append("Seniority looks like a fit")
    if years <= 2 and re.search(r"\b(staff|principal|director|head of|vp)\b", title_text):
        sen_score, penalty = 0.0, 25.0
        reasons.append("Likely too senior")
    if years >= 3 and ENTRY_LEVEL_RE.search(title_text):
        sen_score, penalty = 0.0, 35.0
        reasons.append("Entry-level role; below your experience")

    score = max(0.0, min(99.0, skill_score + title_score + loc_score + sen_score) - penalty)
    if not reasons:
        reasons.append("Partial keyword overlap")
    return round(score, 1), reasons


def rank_jobs(jobs: list[dict[str, Any]], profile: dict[str, Any], wanted: Wanted) -> list[dict[str, Any]]:
    ranked = []
    for job in jobs:
        score, reasons = score_job(job, profile, wanted)
        ranked.append({**job, "score": score, "reasons": reasons})
    ranked.sort(key=lambda j: (j["score"], j.get("relevance", 0.0), j.get("posted") or ""), reverse=True)
    return ranked
