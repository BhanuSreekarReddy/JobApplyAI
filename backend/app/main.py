from __future__ import annotations

import json
import uuid
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from .agent.runner import execute_run, _app_payload, _job_payload, _public_profile
from .config import UPLOAD_DIR
from .db import Base, SessionLocal, engine
from . import events
from .models import Application, JobMatch, Run

Base.metadata.create_all(bind=engine)

app = FastAPI(title="JobApply Agent")
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
