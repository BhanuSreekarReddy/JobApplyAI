from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any

import httpx
from sqlalchemy.orm import Session

from .. import events
from ..models import Application, Company, JobMatch, Run
from .companies import enabled_boards, relocate, sync_starter
from .locations import parse_locations
from .match import rank_jobs
from .materials import cover_letter
from .profile import enhance_with_llm, heuristic_profile
from .resume import extract_text
from .search import search_jobs
from .seen import is_seen


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
        # Freshness: listings older than 30 days lose 15 points and are flagged.
        # Only sources that expose a posted date participate; the rest are
        # left unscored on freshness rather than guessed.
        today = date.today()
        for job in ranked:
            posted = (job.get("posted") or "")[:10]
            try:
                age = (today - date.fromisoformat(posted)).days if posted else None
            except ValueError:
                age = None
            if age is not None and age > 30:
                job["score"] = max(0.0, round(job["score"] - 15.0, 1))
                job["reasons"] = (job.get("reasons") or []) + ["Posted over 30 days ago"]
        ranked.sort(key=lambda j: j["score"], reverse=True)
        keep = [j for j in ranked if j["score"] >= run.min_score][: max(run.max_applications * 3, 12)]
        if not keep:
            keep = ranked[:8]
            await events.publish(run_id, {"type": "status", "message": "No high-confidence matches; keeping the closest roles."})

        # Cross-run guard: drop anything already applied to or prepared in an
        # earlier run, so a re-run never re-surfaces or re-sends it.
        seen_count = 0
        fresh = []
        for job in keep:
            if is_seen(db, job):
                seen_count += 1
            else:
                fresh.append(job)
        keep = fresh
        if seen_count:
            await events.publish(
                run_id,
                {"type": "status", "message": f"Skipped {seen_count} jobs already applied to or skipped in earlier runs."},
            )

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
                posted_date=(job.get("posted") or "")[:10],
            )
            db.add(row)
            stored.append(row)
        db.commit()
        for row in stored:
            db.refresh(row)
            await events.publish(run_id, {"type": "job", "job": _job_payload(row)})

        targets = stored[: run.max_applications]
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
            # Nothing is sent from a run anymore. Every application waits in the
            # review queue until a human approves it (see /api/review). Sending —
            # email, packet preparation, and browser form filling — happens at
            # approval time, not here.
            app = Application(
                run_id=run_id,
                job_id=row.id,
                title=row.title,
                company=row.company,
                status="pending_review",
                channel="review",
                cover_letter=letter,
                notes="Waiting for review. Approve to send it, or skip it.",
                apply_url=row.apply_url,
            )
            db.add(app)
            db.commit()
            db.refresh(app)
            await events.publish(run_id, {"type": "application", "application": _app_payload(app)})

        run.status = "completed"
        run.summary = (
            f"{len(stored)} matches. {len(targets)} applications waiting for review."
        )
        if seen_count:
            run.summary += f" {seen_count} previously seen jobs skipped."
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
        "posted_date": row.posted_date or "",
        "description": (row.description or "")[:900],
    }


def _app_payload(row: Application) -> dict[str, Any]:
    return {
        "id": row.id,
        "job_id": row.job_id,
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
