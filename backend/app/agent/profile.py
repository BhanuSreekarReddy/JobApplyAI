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


SECTION_RE = re.compile(r"^(summary|profile|about( me)?|objective|(professional|career) (summary|profile))\s*:?\s*$", re.I)
CAPS_HEADING_RE = re.compile(r"^[A-Z][A-Z &/]{3,}:?$")
KNOWN_HEADING_RE = re.compile(
    r"^(education|(professional |work )?experience|employment( history)?|(technical )?skills"
    r"|projects|certifications?|awards|publications|languages|interests)\b.{0,25}:?$",
    re.I,
)


def split_name(name: str) -> tuple[str, str]:
    """Split a full name into (first, last) on the first space: the first
    token is the first name and the remainder is the last name. A
    single-token name goes entirely to the first name."""
    parts = (name or "").strip().split(None, 1)
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[1].strip()


def _summary(lines: list[str], name: str) -> str:
    """Prose from the top of the resume or its Summary/Profile section, skipping the name/contact header.
    This text goes into cover letters, so contact lines, headings, and other sections must not leak in."""
    picked: list[str] = []
    for ln in lines[:30]:
        if ln == name or EMAIL_RE.search(ln) or PHONE_RE.search(ln) or "linkedin.com" in ln.lower() or "github.com" in ln.lower():
            continue
        if SECTION_RE.match(ln):
            continue
        if CAPS_HEADING_RE.match(ln) or KNOWN_HEADING_RE.match(ln):
            break
        if ln.startswith(("-", "•", "*")) or len(ln) < 40 or not re.search(r"[a-z]{3,}\s+[a-z]{3,}", ln):
            if picked:
                break
            continue
        picked.append(ln)
        if sum(len(p) for p in picked) > 400:
            break
    return " ".join(picked)[:600]


MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
DATE = rf"(?:{MONTH}\s*\d{{4}}|\d{{1,2}}/\d{{4}}|(?:19|20)\d{{2}})"
RANGE_RE = re.compile(rf"({DATE})\s*(?:[-–—]|to)\s*({DATE}|present|current|now|today)", re.I)
EDUCATION_RE = re.compile(
    r"\b(university|college|school|institute|bachelor|master|b\.?\s?tech|m\.?\s?tech|b\.?s\.?|m\.?s\.?|ph\.?d|degree|gpa|coursework)\b",
    re.I,
)
NAME_STOP = re.compile(
    r"\b(resume|curriculum|vitae|cv|experience|summary|profile|education|skills|objective|engineer|developer|"
    r"manager|scientist|designer|analyst|consultant|architect|lead|intern|contact|professional)\b",
    re.I,
)
NAME_TOKEN = re.compile(r"^[A-Za-zÀ-ÖØ-öø-ÿ][A-Za-zÀ-ÖØ-öø-ÿ'.-]*$")


def _name(lines: list[str]) -> str:
    """The candidate's name: usually the first line, often fused with location/contact details in PDFs."""
    for ln in lines[:8]:
        head = re.split(r"\s*[•|·●▪,]\s*|\s{2,}(?=\S+@)|\s+(?=\S+@)", ln)[0].strip()
        words = head.split()
        caps = []
        for w in words:
            if w.isupper() and NAME_TOKEN.match(w) and len(w.strip(".")) > 1:
                caps.append(w)
            else:
                break
        if len(caps) >= 2 and len(caps) < len(words):
            words = caps
        if not 2 <= len(words) <= 4 or not all(NAME_TOKEN.match(w) for w in words):
            continue
        if NAME_STOP.search(" ".join(words)) or EMAIL_RE.search(head):
            continue
        name = " ".join(words)
        return name.title() if name.isupper() else name
    return "Candidate"


def _years_from_dates(lines: list[str]) -> int:
    """Total years covered by the employment date ranges (education ranges excluded, overlaps merged)."""
    from datetime import date

    now = date.today().year + (date.today().month - 1) / 12

    def to_year(token: str) -> float | None:
        token = token.lower().strip()
        if token in {"present", "current", "now", "today"}:
            return now
        m = re.search(r"((?:19|20)\d{2})", token)
        if not m:
            return None
        year = float(m.group(1))
        month = re.match(MONTH, token)
        if month:
            year += (["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"].index(token[:3]) / 12)
        else:
            slash = re.match(r"(\d{1,2})/", token)
            if slash:
                year += (int(slash.group(1)) - 1) / 12
        return year

    spans: list[tuple[float, float]] = []
    for ln in lines:
        if EDUCATION_RE.search(ln):
            continue
        for start_s, end_s in RANGE_RE.findall(ln):
            start, end = to_year(start_s), to_year(end_s)
            if start and end and 1970 < start <= end <= now + 1 and end - start < 45:
                spans.append((start, end))
    total, cur_start, cur_end = 0.0, None, None
    for start, end in sorted(spans):
        if cur_end is None or start > cur_end:
            if cur_end is not None:
                total += cur_end - cur_start
            cur_start, cur_end = start, end
        else:
            cur_end = max(cur_end, end)
    if cur_end is not None:
        total += cur_end - cur_start
    return int(total + 0.5)


def heuristic_profile(text: str) -> dict[str, Any]:
    # PDF extraction often emits double spaces between words ("Staff  Software  Engineer").
    text = re.sub(r"[ \t\u00a0]+", " ", text)
    lower = text.lower()
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    name = _name(lines)
    emails = EMAIL_RE.findall(text)
    phones = PHONE_RE.findall(text)
    skills = [s for s in SKILLS if re.search(rf"\b{re.escape(s)}\b", lower)]
    titles = [t for t in TITLES if t in lower]
    stated = [int(m.group(1)) for m in YEARS_RE.finditer(text) if int(m.group(1)) <= 45]
    years = max(stated + [_years_from_dates(lines)]) or 3
    linkedin = LINKEDIN_RE.search(text)
    github = GITHUB_RE.search(text)
    summary = _summary(lines, name)
    first_name, last_name = split_name(name)
    return {
        "name": name[:80],
        "first_name": first_name[:80],
        "last_name": last_name[:80],
        "email": emails[0] if emails else "",
        "phone": phones[0] if phones else "",
        "linkedin": linkedin.group(0) if linkedin else "",
        "github": github.group(0) if github else "",
        "skills": skills[:24],
        "titles": titles[:8] or ["software engineer"],
        "years_experience": years,
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
        if parsed.get("name"):
            # The LLM supplied a different full name; re-derive the split so
            # it never goes stale against the heuristic split.
            merged["first_name"], merged["last_name"] = split_name(str(merged["name"]))
        if isinstance(merged.get("skills"), list):
            merged["skills"] = [str(s) for s in merged["skills"][:24]]
        if isinstance(merged.get("titles"), list):
            merged["titles"] = [str(t) for t in merged["titles"][:8]]
        return merged
    except Exception:
        return profile
