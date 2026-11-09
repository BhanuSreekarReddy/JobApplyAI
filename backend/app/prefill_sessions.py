"""Streamed browser pre-fill sessions (the hosted-mode pre-fill path).

A session opens the employer's application form in a server-side Chromium
with a fresh, ephemeral browser profile, runs the same form-filling logic
the local flow uses, then streams the page to the owner's browser tab over
a WebSocket (CDP screencast) and forwards their mouse/keyboard input back
(CDP Input domain). The user reviews the filled form and clicks Submit
themselves — nothing is ever submitted by the agent.

Hard rules:
- The browser profile lives under data/prefill-sessions/<id>/ and is
  DELETED when the session closes. No cookies or logins persist server-side.
- One active session per user.
- Sessions idle for 30 minutes are closed automatically.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import queue
import shutil
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from . import auth
from .agent import formfill
from .agent.runner import primary_location
from .config import DATA_DIR
from .db import SessionLocal
from .models import Application, JobMatch, PrefillSessionRecord, Run

log = logging.getLogger("jobapply.prefill")

SESSIONS_DIR = DATA_DIR / "prefill-sessions"
IDLE_TIMEOUT = timedelta(minutes=30)

router = APIRouter()


class _Live:
    def __init__(self, session_id: str, user_id: int, profile_dir: Path) -> None:
        self.id = session_id
        self.user_id = user_id
        self.profile_dir = profile_dir
        self.pw: Any = None
        self.context: Any = None
        self.page: Any = None
        self.cdp: Any = None
        # A plain queue.Queue, not asyncio.Queue: CDP frame callbacks fire on
        # the event loop that created the browser, which is not necessarily
        # the loop serving the WebSocket; the pump below polls this queue.
        self.frames: queue.Queue[str] = queue.Queue(maxsize=30)
        self.last_activity = datetime.utcnow()
        self.ws_active = False
        self.closed = False


_live: dict[str, _Live] = {}


def _payload(record: PrefillSessionRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "application_id": record.application_id,
        "apply_url": record.apply_url,
        "status": record.status,
        "filled": json.loads(record.filled_json or "[]"),
        "needs_attention": json.loads(record.needs_attention_json or "[]"),
        "notes": record.notes,
        "created_at": record.created_at.isoformat() if record.created_at else None,
    }


def _owned_record(db, session_id: str, user_id: int) -> PrefillSessionRecord:
    record = db.get(PrefillSessionRecord, session_id)
    if record is None or record.user_id != user_id:
        raise HTTPException(404, "Pre-fill session not found")
    return record


async def _close_live(live: _Live, reason: str) -> None:
    if live.closed:
        return
    live.closed = True
    try:
        if live.cdp is not None:
            try:
                await live.cdp.send("Page.stopScreencast")
            except Exception:
                pass
    finally:
        try:
            if live.context is not None:
                await live.context.close()
        except Exception:
            pass
        try:
            if live.pw is not None:
                await live.pw.stop()
        except Exception:
            pass
    # The ephemeral profile (cookies, storage, any login the user typed into
    # the streamed page) is wiped — nothing persists server-side.
    shutil.rmtree(live.profile_dir, ignore_errors=True)
    _live.pop(live.id, None)
    db = SessionLocal()
    try:
        record = db.get(PrefillSessionRecord, live.id)
        if record is not None and record.status != "error":
            record.status = "closed"
            record.closed_at = datetime.utcnow()
            db.commit()
    finally:
        db.close()
    log.info("prefill session %s closed (%s) for user %s", live.id, reason, live.user_id)


async def close_session(session_id: str, reason: str = "closed by user") -> bool:
    live = _live.get(session_id)
    if live is None:
        return False
    await _close_live(live, reason)
    return True


async def close_all_sessions() -> None:
    for live in list(_live.values()):
        await _close_live(live, "server shutdown")


async def expire_idle_sessions() -> None:
    now = datetime.utcnow()
    for live in list(_live.values()):
        if now - live.last_activity > IDLE_TIMEOUT:
            await _close_live(live, "idle timeout")


class CreateSession(BaseModel):
    application_id: int


@router.post("/api/prefill-sessions")
async def create_prefill_session(body: CreateSession, request: Request):
    user = auth.current_user(request)
    await expire_idle_sessions()
    if any(live.user_id == user.id for live in _live.values()):
        raise HTTPException(409, "You already have an active pre-fill session. Close it before starting another.")

    db = SessionLocal()
    try:
        application = db.get(Application, body.application_id)
        if application is None or application.user_id != user.id:
            raise HTTPException(404, "Application not found")
        if not application.apply_url:
            raise HTTPException(400, "This application has no apply URL.")
        run = db.get(Run, application.run_id)
        if run is None:
            raise HTTPException(404, "Run not found")
        job = db.get(JobMatch, application.job_id) if application.job_id else None
        try:
            profile = json.loads(run.profile_json or "{}")
        except json.JSONDecodeError:
            profile = {}
        cover = application.cover_letter or ""
        resume_path = run.resume_path or ""
        location = primary_location(run.locations)
        apply_url = application.apply_url
        job_description = job.description if job else ""
    finally:
        db.close()

    session_id = uuid.uuid4().hex[:16]
    profile_dir = SESSIONS_DIR / session_id
    record = PrefillSessionRecord(
        id=session_id,
        user_id=user.id,
        application_id=body.application_id,
        apply_url=apply_url,
        status="starting",
        profile_dir=str(profile_dir),
    )
    db = SessionLocal()
    try:
        db.add(record)
        db.commit()
    finally:
        db.close()

    live = _Live(session_id, user.id, profile_dir)
    _live[session_id] = live
    try:
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            raise RuntimeError("Playwright is not installed on the server.")
        live.pw = await async_playwright().start()
        profile_dir.mkdir(parents=True, exist_ok=True)
        live.context = await live.pw.chromium.launch_persistent_context(
            str(profile_dir),
            headless=True,
            viewport={"width": 1280, "height": 800},
        )
        page = live.context.pages[0] if live.context.pages else await live.context.new_page()
        live.page = page
        await page.goto(apply_url, wait_until="domcontentloaded", timeout=45_000)
        await formfill._settle(page)
        files = formfill._packet_files(profile, cover, resume_path, f"{run.id}-{application.job_id}")
        values = formfill._values(profile, location, cover, files)
        result = await formfill._fill_open_page(page, values)
        live.page = result.pop("_page", page)
    except Exception as exc:
        reason = str(exc).splitlines()[0][:200] if str(exc) else exc.__class__.__name__
        log.warning("prefill session %s failed to start: %s", session_id, reason)
        await _close_live(live, "startup failed")
        db = SessionLocal()
        try:
            record = db.get(PrefillSessionRecord, session_id)
            if record is not None:
                record.status = "error"
                record.notes = f"Browser pre-fill is unavailable right now ({reason})."
                record.closed_at = datetime.utcnow()
                db.commit()
        finally:
            db.close()
        raise HTTPException(
            503,
            "Browser pre-fill is unavailable in this environment right now. "
            "The prepared packet is still available — open the apply URL and paste the cover letter.",
        ) from exc

    db = SessionLocal()
    try:
        record = db.get(PrefillSessionRecord, session_id)
        record.status = "ready"
        record.filled_json = json.dumps(result.get("filled") or [])
        record.needs_attention_json = json.dumps(result.get("needs_attention") or [])
        record.notes = result.get("notes") or ""
        record.last_activity_at = datetime.utcnow()
        db.commit()
        application = db.get(Application, body.application_id)
        if application is not None:
            application.status = result.get("status") or application.status
            application.channel = result.get("channel") or application.channel
            application.notes = result.get("notes") or application.notes
            db.commit()
        payload = _payload(record)
    finally:
        db.close()
    log.info("prefill session %s created for user %s (application %s)", session_id, user.id, body.application_id)
    return payload


@router.get("/api/prefill-sessions/{session_id}")
def get_prefill_session(session_id: str, request: Request):
    user = auth.current_user(request)
    db = SessionLocal()
    try:
        return _payload(_owned_record(db, session_id, user.id))
    finally:
        db.close()


@router.post("/api/prefill-sessions/{session_id}/close")
async def close_prefill_session(session_id: str, request: Request):
    user = auth.current_user(request)
    db = SessionLocal()
    try:
        record = _owned_record(db, session_id, user.id)
        record_id = record.id
    finally:
        db.close()
    closed_live = await close_session(record_id)
    if not closed_live:
        db = SessionLocal()
        try:
            record = db.get(PrefillSessionRecord, record_id)
            if record is not None and record.status not in {"closed", "error"}:
                record.status = "closed"
                record.closed_at = datetime.utcnow()
                db.commit()
                if record.profile_dir:
                    shutil.rmtree(record.profile_dir, ignore_errors=True)
        finally:
            db.close()
    return {"closed": True}


async def _dispatch_input(live: _Live, message: dict[str, Any]) -> None:
    cdp = live.cdp
    if cdp is None:
        return
    kind = message.get("type")
    live.last_activity = datetime.utcnow()
    try:
        if kind == "mouse":
            action = message.get("action")
            event_type = {"press": "mousePressed", "release": "mouseReleased", "move": "mouseMoved"}.get(action)
            if event_type:
                await cdp.send(
                    "Input.dispatchMouseEvent",
                    {
                        "type": event_type,
                        "x": float(message.get("x") or 0),
                        "y": float(message.get("y") or 0),
                        "button": message.get("button") or "left",
                        "clickCount": 1 if action in {"press", "release"} else 0,
                    },
                )
        elif kind == "wheel":
            await cdp.send(
                "Input.dispatchMouseEvent",
                {
                    "type": "mouseWheel",
                    "x": float(message.get("x") or 0),
                    "y": float(message.get("y") or 0),
                    "deltaX": float(message.get("deltaX") or 0),
                    "deltaY": float(message.get("deltaY") or 0),
                },
            )
        elif kind == "key":
            action = message.get("action")
            if action == "char":
                await cdp.send("Input.dispatchKeyEvent", {"type": "char", "text": message.get("text") or ""})
            elif action in {"down", "up"}:
                await cdp.send(
                    "Input.dispatchKeyEvent",
                    {
                        "type": "keyDown" if action == "down" else "keyUp",
                        "key": message.get("key") or "",
                        "code": message.get("code") or "",
                        "text": message.get("text") or "",
                    },
                )
        elif kind == "resize":
            width = max(320, int(message.get("width") or 1280))
            height = max(240, int(message.get("height") or 800))
            await cdp.send(
                "Emulation.setDeviceMetricsOverride",
                {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": False},
            )
    except Exception:
        pass  # a dead page/CDP must not kill the stream loop


@router.websocket("/api/prefill-sessions/{session_id}/stream")
async def stream_prefill_session(websocket: WebSocket, session_id: str):
    db = SessionLocal()
    try:
        user = auth.user_for_session_token(db, websocket.cookies.get(auth.SESSION_COOKIE))
        record = db.get(PrefillSessionRecord, session_id)
        if user is None or record is None or record.user_id != user.id:
            await websocket.close(code=4404)
            return
    finally:
        db.close()
    live = _live.get(session_id)
    if live is None or live.closed:
        await websocket.close(code=4404)
        return
    if live.ws_active:
        await websocket.close(code=4409)
        return
    await websocket.accept()
    live.ws_active = True
    live.last_activity = datetime.utcnow()

    async def on_frame(event: dict[str, Any]) -> None:
        try:
            await live.cdp.send("Page.screencastFrameAck", {"sessionId": event["sessionId"]})
        except Exception:
            pass
        if live.frames.full():
            try:
                live.frames.get_nowait()
            except queue.Empty:
                pass
        try:
            live.frames.put_nowait(event["data"])
        except queue.Full:
            pass

    try:
        live.cdp = await live.context.new_cdp_session(live.page)
        live.cdp.on("Page.screencastFrame", lambda event: asyncio.ensure_future(on_frame(event)))
        await live.cdp.send(
            "Page.startScreencast",
            {"format": "jpeg", "quality": 60, "maxWidth": 1440, "maxHeight": 900, "everyNthFrame": 1},
        )

        async def pump() -> None:
            while True:
                try:
                    data = live.frames.get_nowait()
                except queue.Empty:
                    await asyncio.sleep(0.05)
                    continue
                await websocket.send_bytes(base64.b64decode(data))

        pump_task = asyncio.ensure_future(pump())
        try:
            while True:
                raw = await websocket.receive_text()
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if isinstance(message, dict):
                    await _dispatch_input(live, message)
        except WebSocketDisconnect:
            pass
        finally:
            pump_task.cancel()
    finally:
        live.ws_active = False
        # A dropped stream ends the session: the ephemeral profile is wiped
        # rather than left sitting on the server. The close runs in its own
        # task, shielded — a hard client disconnect can cancel this handler
        # task, and the wipe must still complete.
        closing = asyncio.ensure_future(_close_live(live, "stream disconnected"))
        try:
            await asyncio.shield(closing)
        except asyncio.CancelledError:
            pass
