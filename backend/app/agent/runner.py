from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import httpx
from sqlalchemy.orm import Session

from .. import events
from ..models import Application, Company, JobMatch, Run
from .apply import apply_to_job
from .companies import enabled_boards, relocate, sync_starter
from .formfill import fill_application
from .locations import parse_locations
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

        wanted = parse_locations(run.locations)
        where = ", ".join(wanted.labels()) or "any location"
        await events.publish(
            run_id,
            {
                "type": "status",
                "message": f"Looking for {', '.join((profile.get('titles') or ['engineer'])[:2])} roles in {where}",
            },
        )
        sync_starter(db)
        tracked = enabled_boards(db)

        async def progress(message: str) -> None:
            await events.publish(run_id, {"type": "status", "message": message})

        found = await search_jobs(profile, wanted, [b for _c, b in tracked], progress)
        await _update_companies(db, tracked, found, progress)
        await events.publish(
            run_id,
            {
                "type": "status",
                "message": (
                    f"Scanned {found.scanned:,} openings: {found.title_matches:,} fit your roles, "
                    f"{found.location_matches:,} of those are in {where}. Scoring the top {len(found.jobs)}."
                ),
            },
        )
        ranked = rank_jobs(found.jobs, profile, wanted)
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
        applied = prepared = filled = failed = 0
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
            if run.browser_fill and result["status"] == "prepared" and row.apply_url:
                await events.publish(run_id, {"type": "status", "message": f"Pre-filling the application form for {row.company} in the browser"})
                result = await fill_application(
                    profile=profile,
                    job=job,
                    cover=letter,
                    resume_path=run.resume_path,
                    location=primary_location(run.locations),
                    packet_key=f"{run_id}-{row.id}",
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
            elif result["status"] == "filled":
                filled += 1
            else:
                prepared += 1
            await events.publish(run_id, {"type": "application", "application": _app_payload(app)})

        run.status = "completed"
        filled_note = f"{filled} forms pre-filled for your review, " if filled else ""
        run.summary = (
            f"{len(stored)} matches. {applied} submitted, {filled_note}{prepared} packets ready, {failed} failed."
        )
        db.commit()
        await events.publish(run_id, {"type": "done", "summary": run.summary, "status": run.status})
    except Exception as exc:
        # The session may hold a failed flush; roll back so the failure itself can be recorded.
        db.rollback()
        try:
            run.status = "failed"
            run.error = str(exc)
            db.commit()
        except Exception:
            db.rollback()
        await events.publish(run_id, {"type": "error", "message": str(exc)})


def primary_location(locations: str) -> str:
    """First concrete place the candidate listed, for 'Location' fields on application forms."""
    return parse_locations(locations).primary()


async def _update_companies(db: Session, tracked: list, found: Any, progress: Any) -> None:
    """Record each board's health so the Companies tab shows which ones work, and follow moved boards."""
    now = datetime.utcnow()
    missing = {b.key for b in found.missing}
    failed = {b.key for b in found.failed}
    # The search took a while; the user may have removed or edited companies meanwhile, so reload.
    keys = {company.id: board.key for company, board in tracked}
    db.expire_all()
    fresh = db.query(Company).filter(Company.id.in_(list(keys))).all()
    for company in fresh:
        key = keys[company.id]
        if key in failed:
            company.status, company.note = "error", "Board didn't respond on the last search."
        elif key not in missing:
            if company.status != "ok":
                company.status, company.note = "ok", ""
            company.job_count = found.board_counts.get(key, company.job_count)
        company.last_checked = now
    db.commit()
    gone = [c for c in fresh if keys[c.id] in missing and c.enabled]
    if not gone:
        return
    await progress(f"{len(gone)} company board(s) moved or closed; looking for their new job boards")
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
        for company in gone:
            await relocate(db, client, company)


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
        "pipeline_stage": row.pipeline_stage or "applied",
        "user_notes": row.user_notes or "",
        "follow_up_date": row.follow_up_date or "",
    }
