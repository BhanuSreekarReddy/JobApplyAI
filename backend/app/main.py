from __future__ import annotations

import json
import re
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

import httpx
from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from .agent.apply import apply_to_job
from .agent.companies import ResolveError, add_company, payload as company_payload, resolve_job_url, sync_starter
from .agent.formfill import fill_application, shutdown_browser
from .agent.materials import cover_letter
from .agent.runner import execute_run, primary_location, _app_payload, _job_payload, _public_profile
from .config import UPLOAD_DIR
from .db import Base, SessionLocal, engine, upgrade_schema
from . import events
from .models import Application, Company, JobMatch, Run, SeenJob

Base.metadata.create_all(bind=engine)
upgrade_schema()
with SessionLocal() as _db:
    sync_starter(_db)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    yield
    shutdown_browser()


app = FastAPI(title="JobApply Agent", lifespan=lifespan)


def _migrate_application_tracker_columns() -> None:
    """SQLite create_all does not add columns to an existing database, so add
    the tracker columns to databases created before this change."""
    from sqlalchemy import text

    with engine.begin() as conn:
        cols = {row[1] for row in conn.execute(text("PRAGMA table_info(applications)"))}
        for name, ddl in [
            ("pipeline_stage", "ALTER TABLE applications ADD COLUMN pipeline_stage VARCHAR DEFAULT 'applied'"),
            ("user_notes", "ALTER TABLE applications ADD COLUMN user_notes TEXT DEFAULT ''"),
            ("follow_up_date", "ALTER TABLE applications ADD COLUMN follow_up_date VARCHAR DEFAULT ''"),
        ]:
            if name not in cols:
                conn.execute(text(ddl))


_migrate_application_tracker_columns()


def _match_key(title: str, company: str) -> str:
    return re.sub(r"\W+", "", f"{title or ''}{company or ''}".lower())

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _run_in_background(run_id: str) -> None:
    import asyncio

    db = SessionLocal()
    try:
        run = db.get(Run, run_id)
        if not run:
            return
        run.status = "running"
        db.commit()
        asyncio.run(execute_run(db, run))
    finally:
        db.close()


@app.get("/api/health")
def health():
    return {"ok": True}


@app.post("/api/runs")
async def create_run(
    background: BackgroundTasks,
    resume: UploadFile = File(...),
    locations: str = Form(...),
    auto_apply: str = Form("false"),
    browser_fill: str = Form("false"),
    min_score: float = Form(68.0),
    max_applications: int = Form(8),
    contact_email: str = Form(""),
):
    suffix = Path(resume.filename or "resume.txt").suffix.lower()
    if suffix not in {".pdf", ".docx", ".doc", ".txt", ".md"}:
        raise HTTPException(400, "Upload a PDF, DOCX, or TXT resume.")
    run_id = uuid.uuid4().hex[:12]
    dest = UPLOAD_DIR / f"{run_id}{suffix}"
    dest.write_bytes(await resume.read())

    db = SessionLocal()
    try:
        run = Run(
            id=run_id,
            status="queued",
            locations=locations,
            auto_apply=1 if auto_apply.lower() in {"1", "true", "yes", "on"} else 0,
            browser_fill=1 if browser_fill.lower() in {"1", "true", "yes", "on"} else 0,
            min_score=min_score,
            max_applications=max(1, min(int(max_applications), 20)),
            resume_path=str(dest),
            profile_json=json.dumps({"email": contact_email} if contact_email else {}),
        )
        db.add(run)
        db.commit()
    finally:
        db.close()

    background.add_task(_run_in_background, run_id)
    return {"id": run_id, "status": "queued"}


@app.get("/api/runs/{run_id}")
def get_run(run_id: str):
    db = SessionLocal()
    try:
        run = db.get(Run, run_id)
        if not run:
            raise HTTPException(404, "Run not found")
        jobs = db.query(JobMatch).filter(JobMatch.run_id == run_id).order_by(JobMatch.score.desc()).all()
        apps = db.query(Application).filter(Application.run_id == run_id).order_by(Application.id.asc()).all()
        profile = {}
        try:
            profile = json.loads(run.profile_json or "{}")
        except json.JSONDecodeError:
            profile = {}
        # A match is new when no earlier run surfaced the same normalized
        # title + company. Comparison uses existing tables only, and only
        # runs created before this one count as earlier.
        prior_keys: set[str] = set()
        if run.created_at is not None:
            earlier_ids = [
                r.id
                for r in db.query(Run).filter(Run.created_at < run.created_at).all()
            ]
            if earlier_ids:
                prior_keys = {
                    _match_key(r.title, r.company)
                    for r in db.query(JobMatch).filter(JobMatch.run_id.in_(earlier_ids)).all()
                }
        job_payloads = []
        for j in jobs:
            payload = _job_payload(j)
            payload["is_new"] = _match_key(j.title, j.company) not in prior_keys
            job_payloads.append(payload)
        return {
            "id": run.id,
            "status": run.status,
            "locations": run.locations,
            "auto_apply": bool(run.auto_apply),
            "browser_fill": bool(run.browser_fill),
            "min_score": run.min_score,
            "max_applications": run.max_applications,
            "summary": run.summary,
            "error": run.error,
            "profile": _public_profile(profile) if profile else {},
            "jobs": job_payloads,
            "applications": [_app_payload(a) for a in apps],
        }
    finally:
        db.close()


@app.get("/api/runs")
def list_runs():
    db = SessionLocal()
    try:
        rows = db.query(Run).order_by(Run.created_at.desc()).limit(50).all()
        return [
            {
                "id": r.id,
                "status": r.status,
                "locations": r.locations,
                "summary": r.summary,
                "error": r.error,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]
    finally:
        db.close()


@app.post("/api/runs/{run_id}/rerun")
def rerun_run(run_id: str, background: BackgroundTasks):
    """Re-run with the original run's settings and resume. Scheduling is left
    to the caller (for example a cron job hitting this endpoint daily)."""
    db = SessionLocal()
    try:
        original = db.get(Run, run_id)
        if not original:
            raise HTTPException(404, "Run not found")
        seeded_email = ""
        try:
            seeded_email = (json.loads(original.profile_json or "{}") or {}).get("email") or ""
        except json.JSONDecodeError:
            seeded_email = ""
        new_id = uuid.uuid4().hex[:12]
        run = Run(
            id=new_id,
            status="queued",
            locations=original.locations,
            auto_apply=original.auto_apply,
            browser_fill=original.browser_fill,
            min_score=original.min_score,
            max_applications=original.max_applications,
            resume_path=original.resume_path,
            profile_json=json.dumps({"email": seeded_email} if seeded_email else {}),
        )
        db.add(run)
        db.commit()
    finally:
        db.close()

    background.add_task(_run_in_background, new_id)
    return {"id": new_id, "status": "queued"}


@app.get("/api/runs/{run_id}/events")
async def stream_events(run_id: str):
    db = SessionLocal()
    try:
        if not db.get(Run, run_id):
            raise HTTPException(404, "Run not found")
    finally:
        db.close()

    queue = events.subscribe(run_id)

    async def gen():
        try:
            for item in events.history(run_id):
                yield f"data: {json.dumps(item)}\n\n"
            while True:
                item = await queue.get()
                yield f"data: {json.dumps(item)}\n\n"
                if item.get("type") in {"done", "error"}:
                    break
        finally:
            events.unsubscribe(run_id, queue)

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/api/applications")
def list_applications():
    db = SessionLocal()
    try:
        rows = db.query(Application).order_by(Application.id.desc()).limit(100).all()
        return [_app_payload(a) for a in rows]
    finally:
        db.close()


@app.post("/api/applications/{app_id}/fill")
async def fill_application_form(app_id: int):
    db = SessionLocal()
    try:
        row = db.get(Application, app_id)
        if not row:
            raise HTTPException(404, "Application not found")
        if not row.apply_url:
            raise HTTPException(400, "This application has no apply URL.")
        run = db.get(Run, row.run_id)
        job = db.get(JobMatch, row.job_id)
        if not run:
            raise HTTPException(404, "Run not found")
        try:
            profile = json.loads(run.profile_json or "{}")
        except json.JSONDecodeError:
            profile = {}
        result = await fill_application(
            profile=profile,
            job={
                "title": row.title,
                "company": row.company,
                "description": job.description if job else "",
                "apply_url": row.apply_url,
            },
            cover=row.cover_letter,
            resume_path=run.resume_path,
            location=primary_location(run.locations),
            packet_key=f"{run.id}-{row.job_id}",
        )
        row.status = result["status"]
        row.channel = result["channel"]
        row.notes = result["notes"]
        db.commit()
        db.refresh(row)
        return _app_payload(row)
    finally:
        db.close()


class ApplicationUpdate(BaseModel):
    status: Literal["applied", "filled", "prepared"] | None = None
    cover_letter: str | None = None


@app.patch("/api/applications/{app_id}")
def update_application(app_id: int, body: ApplicationUpdate):
    db = SessionLocal()
    try:
        row = db.get(Application, app_id)
        if not row:
            raise HTTPException(404, "Application not found")
        if body.cover_letter is not None:
            if row.status != "pending_review":
                raise HTTPException(400, "Only applications waiting for review can be edited")
            row.cover_letter = body.cover_letter
        if body.status is not None:
            row.status = body.status
            if body.status == "applied":
                row.notes = "Submitted by you on the employer site. " + (row.notes or "")
        db.commit()
        db.refresh(row)
        return _app_payload(row)
    finally:
        db.close()


@app.get("/api/review")
def list_pending_review():
    """Applications waiting for a human approve/skip decision. Nothing here has been sent."""
    db = SessionLocal()
    try:
        rows = (
            db.query(Application)
            .filter(Application.status == "pending_review")
            .order_by(Application.id.asc())
            .all()
        )
        return [_app_payload(a) for a in rows]
    finally:
        db.close()


@app.post("/api/applications/{app_id}/approve")
async def approve_application(app_id: int):
    """Approve a queued application. Sends it by email only when the run opted into
    email sending and an address plus SMTP are available; otherwise marks it prepared.
    When the run opted into browser form filling, approval also pre-fills the
    employer's application form with the reviewed cover letter, exactly like a run
    used to — approval is now the step that triggers it."""
    db = SessionLocal()
    try:
        app = db.get(Application, app_id)
        if not app:
            raise HTTPException(404, "Application not found")
        if app.status != "pending_review":
            raise HTTPException(400, "Only applications waiting for review can be approved")
        run = db.get(Run, app.run_id)
        profile = {}
        if run:
            try:
                profile = json.loads(run.profile_json or "{}")
            except json.JSONDecodeError:
                profile = {}
        job_row = db.get(JobMatch, app.job_id) if app.job_id else None
        result = apply_to_job(
            profile=profile,
            job={
                "title": app.title,
                "company": app.company,
                "location": job_row.location if job_row else "",
                "description": job_row.description if job_row else "",
                "apply_email": job_row.apply_email if job_row else "",
                "apply_url": app.apply_url,
            },
            cover=app.cover_letter,
            resume_path=run.resume_path if run else "",
            auto_apply=bool(run.auto_apply) if run else False,
        )
        if run and run.browser_fill and result["status"] == "prepared" and app.apply_url:
            result = await fill_application(
                profile=profile,
                job={
                    "title": app.title,
                    "company": app.company,
                    "description": job_row.description if job_row else "",
                    "apply_url": app.apply_url,
                },
                cover=app.cover_letter,
                resume_path=run.resume_path,
                location=primary_location(run.locations),
                packet_key=f"{run.id}-{app.job_id}",
            )
        app.status = result["status"]
        app.channel = result["channel"]
        app.notes = result["notes"]
        db.commit()
        db.refresh(app)
        return _app_payload(app)
    finally:
        db.close()


@app.post("/api/applications/{app_id}/skip")
def skip_application(app_id: int):
    db = SessionLocal()
    try:
        app = db.get(Application, app_id)
        if not app:
            raise HTTPException(404, "Application not found")
        if app.status != "pending_review":
            raise HTTPException(400, "Only applications waiting for review can be skipped")
        app.status = "skipped"
        app.notes = "Skipped during review."
        db.commit()
        db.refresh(app)
        return _app_payload(app)
    finally:
        db.close()


@app.get("/api/companies")
def list_companies():
    db = SessionLocal()
    try:
        rows = db.query(Company).order_by(Company.source.desc(), Company.name).all()
        enabled = [c for c in rows if c.enabled]
        return {
            "companies": [company_payload(c) for c in rows],
            "summary": {
                "total": len(rows),
                "enabled": len(enabled),
                "user_added": sum(1 for c in rows if c.source == "user"),
                "open_jobs": sum(c.job_count for c in enabled),
                "problems": sum(1 for c in enabled if c.status in {"not_found", "error"}),
            },
        }
    finally:
        db.close()


class CompanyCreate(BaseModel):
    query: str


@app.post("/api/companies")
async def create_company(body: CompanyCreate):
    db = SessionLocal()
    try:
        async with httpx.AsyncClient(timeout=25.0, follow_redirects=True) as client:
            try:
                row, created = await add_company(db, client, body.query)
            except ResolveError as exc:
                raise HTTPException(422, str(exc)) from exc
        return {"company": company_payload(row), "created": created}
    finally:
        db.close()


class CompanyUpdate(BaseModel):
    enabled: bool


@app.patch("/api/companies/{company_id}")
def update_company(company_id: int, body: CompanyUpdate):
    db = SessionLocal()
    try:
        row = db.get(Company, company_id)
        if not row:
            raise HTTPException(404, "Company not found")
        row.enabled = 1 if body.enabled else 0
        db.commit()
        db.refresh(row)
        return company_payload(row)
    finally:
        db.close()


@app.delete("/api/companies/{company_id}")
def delete_company(company_id: int):
    """User-added companies are deleted; starter companies are disabled so a later sync won't re-add them."""
    db = SessionLocal()
    try:
        row = db.get(Company, company_id)
        if not row:
            raise HTTPException(404, "Company not found")
        if row.source == "starter":
            row.enabled = 0
            db.commit()
            return {"deleted": False, "company": company_payload(row)}
        db.delete(row)
        db.commit()
        return {"deleted": True}
    finally:
        db.close()


class JobLink(BaseModel):
    url: str
    run_id: str = ""
    fill: bool = True


def _profile_run(db, run_id: str) -> Run | None:
    if run_id:
        return db.get(Run, run_id)
    for run in db.query(Run).order_by(Run.created_at.desc()).limit(20):
        try:
            if json.loads(run.profile_json or "{}").get("name"):
                return run
        except json.JSONDecodeError:
            continue
    return None


@app.post("/api/jobs/from-link")
async def apply_from_link(body: JobLink):
    """Prepare (and optionally pre-fill) an application for one specific job link."""
    db = SessionLocal()
    try:
        run = _profile_run(db, body.run_id)
        if not run:
            raise HTTPException(409, "Run the agent once with your resume first, so the app knows your profile.")
        profile = json.loads(run.profile_json or "{}")
        async with httpx.AsyncClient(timeout=25.0, follow_redirects=True) as client:
            try:
                job = await resolve_job_url(client, body.url)
            except ResolveError as exc:
                raise HTTPException(422, str(exc)) from exc

        row = JobMatch(
            run_id=run.id,
            external_id=job.get("external_id") or "",
            title=job.get("title") or "",
            company=job.get("company") or "",
            location=job.get("location") or "",
            source=job.get("source") or "Link",
            url=body.url,
            apply_url=job.get("apply_url") or body.url,
            description=job.get("description") or "",
            score=0,
            reasons="Added from a job link",
            remote=1 if job.get("remote") else 0,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        letter = cover_letter(profile, job)
        result = {"status": "prepared", "channel": "ats_link", "notes": "Packet ready. Open the apply link to submit."}
        if body.fill:
            result = await fill_application(
                profile=profile,
                job={"title": row.title, "company": row.company, "apply_url": row.apply_url},
                cover=letter,
                resume_path=run.resume_path,
                location=primary_location(run.locations),
                packet_key=f"{run.id}-{row.id}",
            )
        application = Application(
            run_id=run.id,
            job_id=row.id,
            title=row.title,
            company=row.company,
            status=result["status"],
            channel=result["channel"],
            cover_letter=letter,
            notes=result["notes"],
            apply_url=row.apply_url,
        )
        db.add(application)
        db.commit()
        db.refresh(application)
        return {"job": _job_payload(row), "application": _app_payload(application)}
    finally:
        db.close()


PIPELINE_STAGES = {"applied", "reply", "screen", "interview", "offer", "rejected", "ghosted"}


class TrackerUpdate(BaseModel):
    pipeline_stage: str | None = None
    user_notes: str | None = None
    follow_up_date: str | None = None


@app.patch("/api/applications/{app_id}/tracker")
def update_tracker(app_id: int, update: TrackerUpdate):
    """Edit an application's pipeline stage, personal notes, or follow-up date."""
    from datetime import datetime as _datetime

    db = SessionLocal()
    try:
        app = db.get(Application, app_id)
        if not app:
            raise HTTPException(404, "Application not found")
        if update.pipeline_stage is not None:
            if update.pipeline_stage not in PIPELINE_STAGES:
                raise HTTPException(400, f"Unknown pipeline stage: {update.pipeline_stage}")
            app.pipeline_stage = update.pipeline_stage
        if update.user_notes is not None:
            app.user_notes = update.user_notes
        if update.follow_up_date is not None:
            if update.follow_up_date:
                try:
                    _datetime.strptime(update.follow_up_date, "%Y-%m-%d")
                except ValueError:
                    raise HTTPException(400, "follow_up_date must be YYYY-MM-DD")
            app.follow_up_date = update.follow_up_date
        db.commit()
        db.refresh(app)
        return _app_payload(app)
    finally:
        db.close()


@app.get("/api/seen")
def list_seen_jobs():
    """Jobs already applied to or prepared in an earlier run, newest first.
    These are excluded from future runs by default."""
    db = SessionLocal()
    try:
        rows = db.query(SeenJob).order_by(SeenJob.created_at.desc()).limit(200).all()
        return [
            {
                "id": r.id,
                "title": r.title,
                "company": r.company,
                "url": r.url,
                "status": r.status,
                "run_id": r.run_id,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]
    finally:
        db.close()
