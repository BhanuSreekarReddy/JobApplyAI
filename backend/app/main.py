from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

import httpx
from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from .agent.companies import ResolveError, add_company, payload as company_payload, resolve_job_url, sync_starter
from .agent.formfill import fill_application, shutdown_browser
from .agent.materials import cover_letter
from .agent.runner import execute_run, primary_location, _app_payload, _job_payload, _public_profile
from .config import UPLOAD_DIR
from .db import Base, SessionLocal, engine, upgrade_schema
from . import events
from .models import Application, Company, JobMatch, Run

Base.metadata.create_all(bind=engine)
upgrade_schema()
with SessionLocal() as _db:
    sync_starter(_db)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    yield
    shutdown_browser()


app = FastAPI(title="JobApply Agent", lifespan=lifespan)
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
    auto_apply: str = Form("true"),
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
            "jobs": [_job_payload(j) for j in jobs],
            "applications": [_app_payload(a) for a in apps],
        }
    finally:
        db.close()


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
    status: Literal["applied", "filled", "prepared"]


@app.patch("/api/applications/{app_id}")
def update_application(app_id: int, body: ApplicationUpdate):
    db = SessionLocal()
    try:
        row = db.get(Application, app_id)
        if not row:
            raise HTTPException(404, "Application not found")
        row.status = body.status
        if body.status == "applied":
            row.notes = "Submitted by you on the employer site. " + (row.notes or "")
        db.commit()
        db.refresh(row)
        return _app_payload(row)
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
