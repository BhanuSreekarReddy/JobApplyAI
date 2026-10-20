from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from .. import events
from ..models import Application, JobMatch, Run
from .apply import apply_to_job
from .match import rank_jobs
from .materials import cover_letter
from .profile import enhance_with_llm, heuristic_profile
from .resume import extract_text
from .search import search_jobs


async def execute_run(db: Session, run: Run) -> None:
    run_id = run.id
    try:
        await events.publish(run_id, {"type": "status", "message": "Reading resume"})
        text = extract_text(run.resume_path)
        if len(text.strip()) < 40:
            raise ValueError("Could not extract enough text from the resume. Try PDF, DOCX, or TXT.")

        await events.publish(run_id, {"type": "status", "message": "Building candidate profile"})
        profile = heuristic_profile(text)
        profile = enhance_with_llm(profile, text)
        try:
            seeded = json.loads(run.profile_json or "{}")
        except json.JSONDecodeError:
            seeded = {}
        if seeded.get("email"):
            profile["email"] = seeded["email"]
        run.profile_json = json.dumps(profile)
        db.commit()
        await events.publish(run_id, {"type": "profile", "profile": _public_profile(profile)})

        locations = [p.strip() for p in run.locations.split(",") if p.strip()]
        await events.publish(
            run_id,
            {
                "type": "status",
                "message": f"Searching roles for {', '.join((profile.get('titles') or ['engineer'])[:2])} in {', '.join(locations) or 'any location'}",
            },
        )
        jobs = await search_jobs(profile.get("titles") or [], profile.get("skills") or [], locations)
        await events.publish(run_id, {"type": "status", "message": f"Fetched {len(jobs)} listings. Scoring against your experience."})
        ranked = rank_jobs(jobs, profile, locations)
        keep = [j for j in ranked if j["score"] >= run.min_score][: max(run.max_applications * 3, 12)]
        if not keep:
            keep = ranked[:8]
            await events.publish(run_id, {"type": "status", "message": "No high-confidence matches; keeping the closest roles."})

        stored: list[JobMatch] = []
        for job in keep:
            row = JobMatch(
                run_id=run_id,
                external_id=job.get("external_id") or "",
                title=job.get("title") or "",
                company=job.get("company") or "",
                location=job.get("location") or "",
                source=job.get("source") or "",
                url=job.get("url") or "",
                apply_url=job.get("apply_url") or job.get("url") or "",
                apply_email=job.get("apply_email") or "",
                description=job.get("description") or "",
                score=job.get("score") or 0,
                reasons="; ".join(job.get("reasons") or []),
                remote=1 if job.get("remote") else 0,
            )
            db.add(row)
            stored.append(row)
        db.commit()
        for row in stored:
            db.refresh(row)
            await events.publish(run_id, {"type": "job", "job": _job_payload(row)})

        targets = stored[: run.max_applications]
        applied = prepared = failed = 0
        for row in targets:
            await events.publish(run_id, {"type": "status", "message": f"Drafting application for {row.title} at {row.company}"})
            job = {
                "title": row.title,
                "company": row.company,
                "location": row.location,
                "description": row.description,
                "apply_email": row.apply_email,
                "apply_url": row.apply_url,
            }
            letter = cover_letter(profile, job)
            result = apply_to_job(
                profile=profile,
                job=job,
                cover=letter,
                resume_path=run.resume_path,
                auto_apply=bool(run.auto_apply),
            )
            app = Application(
                run_id=run_id,
                job_id=row.id,
                title=row.title,
                company=row.company,
                status=result["status"],
                channel=result["channel"],
                cover_letter=letter,
                notes=result["notes"],
                apply_url=row.apply_url,
            )
            db.add(app)
            db.commit()
            db.refresh(app)
            if result["status"] == "applied":
                applied += 1
            elif result["status"] == "failed":
                failed += 1
            else:
                prepared += 1
            await events.publish(run_id, {"type": "application", "application": _app_payload(app)})

        run.status = "completed"
        run.summary = (
            f"{len(stored)} matches. {applied} submitted, {prepared} packets ready, {failed} failed."
        )
        db.commit()
        await events.publish(run_id, {"type": "done", "summary": run.summary, "status": run.status})
    except Exception as exc:
        run.status = "failed"
        run.error = str(exc)
        db.commit()
        await events.publish(run_id, {"type": "error", "message": str(exc)})


def _public_profile(profile: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": profile.get("name"),
        "email": profile.get("email"),
        "phone": profile.get("phone"),
        "linkedin": profile.get("linkedin"),
        "github": profile.get("github"),
        "skills": profile.get("skills") or [],
        "titles": profile.get("titles") or [],
        "years_experience": profile.get("years_experience"),
        "summary": profile.get("summary"),
    }


def _job_payload(row: JobMatch) -> dict[str, Any]:
    return {
        "id": row.id,
        "title": row.title,
        "company": row.company,
        "location": row.location,
        "source": row.source,
        "url": row.url,
        "apply_url": row.apply_url,
        "apply_email": row.apply_email,
        "score": row.score,
        "reasons": row.reasons,
        "remote": bool(row.remote),
        "description": (row.description or "")[:900],
    }


def _app_payload(row: Application) -> dict[str, Any]:
    return {
        "id": row.id,
        "title": row.title,
        "company": row.company,
        "status": row.status,
        "channel": row.channel,
        "notes": row.notes,
        "apply_url": row.apply_url,
        "cover_letter": row.cover_letter,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }
