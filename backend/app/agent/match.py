from __future__ import annotations

import re
from typing import Any


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


def score_job(job: dict[str, Any], profile: dict[str, Any], locations: list[str]) -> tuple[float, list[str]]:
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

    loc_score = 0.0
    job_loc = (job.get("location") or "").lower()
    remote = bool(job.get("remote")) or "remote" in job_loc or "worldwide" in job_loc
    wanted = [l.lower() for l in locations]
    wants_remote = any("remote" in l or "anywhere" in l for l in wanted)
    if remote and (wants_remote or wanted):
        loc_score = 22.0 if wants_remote or remote else 16.0
        reasons.append("Remote-friendly")
    for loc in wanted:
        city = loc.split(",")[0].strip()
        if city and city in job_loc:
            loc_score = 25.0
            reasons.append(f"Location: {job.get('location')}")
            break
    if not wanted:
        loc_score = 10.0

    sen_score = 8.0
    tags = _seniority(years)
    if any(tag in title_text.split() for tag in tags):
        sen_score = 15.0
        reasons.append("Seniority looks like a fit")
    if years <= 2 and any(x in title_text for x in ("staff", "principal", "director")):
        sen_score = 0.0
        reasons.append("Likely too senior")
    if years >= 8 and any(x in title_text for x in ("intern", "junior", "entry")):
        sen_score = 2.0

    score = min(99.0, skill_score + title_score + loc_score + sen_score)
    if not reasons:
        reasons.append("Partial keyword overlap")
    return round(score, 1), reasons


def rank_jobs(jobs: list[dict[str, Any]], profile: dict[str, Any], locations: list[str]) -> list[dict[str, Any]]:
    ranked = []
    for job in jobs:
        score, reasons = score_job(job, profile, locations)
        ranked.append({**job, "score": score, "reasons": reasons})
    ranked.sort(key=lambda j: j["score"], reverse=True)
    return ranked
