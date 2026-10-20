from __future__ import annotations

import json
import re
from typing import Any

from .skills import SKILLS, TITLES

EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
PHONE_RE = re.compile(r"(?:\+?\d{1,3}[\s.-]?)?(?:\(?\d{3}\)?[\s.-]?)\d{3}[\s.-]?\d{4}")
LINKEDIN_RE = re.compile(r"(https?://)?(www\.)?linkedin\.com/in/[A-Za-z0-9_-]+/?", re.I)
GITHUB_RE = re.compile(r"(https?://)?(www\.)?github\.com/[A-Za-z0-9_-]+/?", re.I)
YEARS_RE = re.compile(r"(\d+)\+?\s*\+?\s*(?:years?|yrs)", re.I)


def heuristic_profile(text: str) -> dict[str, Any]:
    lower = text.lower()
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    name = next((ln for ln in lines[:8] if "@" not in ln and not ln.lower().startswith("http") and 2 <= len(ln.split()) <= 5), "Candidate")
    emails = EMAIL_RE.findall(text)
    phones = PHONE_RE.findall(text)
    skills = [s for s in SKILLS if re.search(rf"\b{re.escape(s)}\b", lower)]
    titles = [t for t in TITLES if t in lower]
    years = [int(m.group(1)) for m in YEARS_RE.finditer(text)]
    linkedin = LINKEDIN_RE.search(text)
    github = GITHUB_RE.search(text)
    summary = " ".join(lines[:12])[:600]
    return {
        "name": name[:80],
        "email": emails[0] if emails else "",
        "phone": phones[0] if phones else "",
        "linkedin": linkedin.group(0) if linkedin else "",
        "github": github.group(0) if github else "",
        "skills": skills[:24],
        "titles": titles[:8] or ["software engineer"],
        "years_experience": max(years) if years else 3,
        "summary": summary,
        "raw_excerpt": text[:4000],
    }


def enhance_with_llm(profile: dict[str, Any], text: str) -> dict[str, Any]:
    from ..config import settings

    if not settings.openai_api_key:
        return profile
    from openai import OpenAI

    client = OpenAI(api_key=settings.openai_api_key)
    prompt = (
        "Extract a job-seeker profile as JSON with keys: name, email, phone, linkedin, "
        "github, skills (array of strings), titles (target job titles), years_experience (int), "
        "summary (3 sentences). Use only facts from the resume.\n\n"
        f"{text[:12000]}"
    )
    try:
        resp = client.chat.completions.create(
            model=settings.openai_model,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0.1,
        )
        parsed = json.loads(resp.choices[0].message.content or "{}")
        merged = {**profile, **{k: v for k, v in parsed.items() if v}}
        if isinstance(merged.get("skills"), list):
            merged["skills"] = [str(s) for s in merged["skills"][:24]]
        if isinstance(merged.get("titles"), list):
            merged["titles"] = [str(t) for t in merged["titles"][:8]]
        return merged
    except Exception:
        return profile
