from __future__ import annotations

from typing import Any


def cover_letter(profile: dict[str, Any], job: dict[str, Any]) -> str:
    from ..config import settings

    if settings.openai_api_key:
        drafted = _llm_letter(profile, job)
        if drafted:
            return drafted
    name = profile.get("name") or "the candidate"
    skills = ", ".join((profile.get("skills") or [])[:6]) or "the skills in my resume"
    years = profile.get("years_experience") or "several"
    summary = (profile.get("summary") or "").strip()
    summary_block = f"{summary}\n\n" if summary else ""
    return (
        f"Dear {job.get('company') or 'Hiring Team'},\n\n"
        f"I am applying for the {job.get('title')} role. I bring about {years} years of relevant experience "
        f"and hands-on work with {skills}.\n\n"
        f"{summary_block}"
        "I would welcome the chance to contribute and am happy to share more detail.\n\n"
        f"Sincerely,\n{name}\n"
        f"{profile.get('email') or ''}\n"
        f"{profile.get('phone') or ''}\n"
    )


def _llm_letter(profile: dict[str, Any], job: dict[str, Any]) -> str:
    from openai import OpenAI

    from ..config import settings

    client = OpenAI(api_key=settings.openai_api_key)
    prompt = (
        "Write a concise, specific 180-220 word cover letter. No fluff, no cliches. "
        "Do not invent employers or metrics that are not in the resume. "
        f"Candidate: {profile.get('name')}, {profile.get('years_experience')} years, "
        f"skills={profile.get('skills')}, summary={profile.get('summary')}. "
        f"Role: {job.get('title')} at {job.get('company')} in {job.get('location')}. "
        f"Job excerpt: {(job.get('description') or '')[:1800]}"
    )
    try:
        resp = client.chat.completions.create(
            model=settings.openai_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.4,
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception:
        return ""
