"""Streamed browser pre-fill sessions (the hosted-mode pre-fill path).

A session opens the employer's application form in a server-side Chromium
with a fresh, ephemeral browser profile, runs the same form-filling logic
the local flow uses, then streams the page to the owner's browser tab over
a WebSocket (CDP screencast) and forwards their mouse/keyboard input back
(CDP Input domain). The user reviews the filled form and clicks Submit
themselves — nothing is ever submitted by the agent.

Clipboard: a screencast shares no clipboard with the user's machine, so
copy/paste is bridged at the keyboard level. On a "paste" message the
server grants clipboard permission for the page's origin (CDP
Browser.grantPermissions), writes the text onto the SESSION browser's
clipboard (navigator.clipboard.writeText), then dispatches a genuine
Ctrl+V (CDP Input.dispatchKeyEvent) — the page sees a real paste event
and its own paste handlers run, exactly as if the user had pasted
locally. On a "copy" message the server dispatches a genuine Ctrl+C,
reads the session clipboard back (navigator.clipboard.readText), and
returns the text for the client to place on the user's real clipboard.
Copy/paste WITHIN the session (field to field, page to page) never
leaves the session browser and is fully native, rich text included.
Only plain text crosses the local<->session boundary. Each native route
has a fallback: paste degrades to CDP Input.insertText at the page's
focus ("insert-text"), and copy degrades to reading the page's current
selection ("copy-selection"). The older message names are kept for the
fallback UI and older clients.

Hard rules:
- The browser profile lives under data/prefill-sessions/<id>/ and is
  DELETED when the session closes. No cookies or logins persist server-side.
- One active session per user.
- Sessions idle for 30 minutes are closed automatically.
- The session browser presents a generic, internally consistent
  identity instead of automation defaults: locale and timezone come
  from the connecting client (validated, with documented fallbacks),
  the screen is a common desktop size, and the user agent is the
  bundled browser's own with the headless marker removed. It is
  configured at launch and discarded with the profile — nothing about
  it is stored per user.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import queue
import random
import re
import shutil
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, available_timezones

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from . import answers as profile_answers
from . import auth
from .profile_source import fill_profile
from .agent import formfill
from .agent.runner import primary_location
from .agent.seen import advance_stage_on_submission, record_application_seen
from .agent.submission import detect_submission, mark_detected_submission, watch_page_for_submission
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
        self.stream_gen = 0
        self.closed = False
        # Normalized label -> value for everything the agent filled at
        # startup; the answer-capture diff compares the live form against
        # this so only values the USER typed are offered for saving.
        self.filled_values: dict[str, str] = {}
        # Submission detection: a watcher task polls the page while the
        # session runs, and _close_live checks once more at the end. The
        # evidence is the confirmation signal that marked the packet.
        self.submission_task: Any = None
        self.submission_evidence: str | None = None


_live: dict[str, _Live] = {}


# --- The session browser's identity -------------------------------------
# A fresh automation context announces itself: navigator.webdriver is
# set, the user agent says HeadlessChrome, the timezone is the SERVER's
# (UTC in most deployments) while the language claims en-US, and the
# "screen" is exactly the viewport. Employer anti-spam systems score
# exactly those inconsistencies. The session therefore launches with a
# generic, internally consistent identity: the connecting client's own
# locale and timezone (validated — anything implausible is ignored),
# a common desktop screen size, and the bundled browser's own user
# agent with the headless marker removed. All of it is launch-time
# configuration for this one ephemeral session; none of it is written
# to the database or kept after the session closes.

# Fallbacks when the client reports nothing usable: a generic US
# identity (US users are the product's home market).
DEFAULT_LOCALE = "en-US"
DEFAULT_TIMEZONE = "America/New_York"

# Common desktop screen sizes; the viewport sits a browser-chrome
# height below the screen, the way a maximized real browser reports it.
_SCREEN_SIZES = [(1920, 1080), (1536, 864), (1440, 900), (1366, 768), (1280, 800)]
_BROWSER_CHROME_PX = 120

_LANGUAGE_RE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8}){0,3}$")
_KNOWN_TIMEZONES: set[str] | None = None


def _valid_timezone(value: Any) -> str | None:
    """A client-reported timezone, or None when it is not a plausible
    IANA name (unknown zone, junk string, wrong type)."""
    global _KNOWN_TIMEZONES
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > 64:
        return None
    try:
        if _KNOWN_TIMEZONES is None:
            _KNOWN_TIMEZONES = available_timezones()
        if value not in _KNOWN_TIMEZONES:
            return None
        ZoneInfo(value)
    except Exception:
        return None
    return value


def _valid_language(value: Any) -> str | None:
    """A client-reported language tag, or None when it is not a
    plausible BCP-47 tag."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > 35 or not _LANGUAGE_RE.match(value):
        return None
    return value


def _client_languages(client_language: Any, client_languages: Any) -> list[str]:
    """The session's language list: the client's reported languages,
    validated and de-duplicated (primary first), or the default."""
    out: list[str] = []
    candidates: list[Any] = []
    if isinstance(client_languages, (list, tuple)):
        candidates.extend(client_languages)
    if client_language:
        candidates.insert(0, client_language)
    for candidate in candidates:
        valid = _valid_language(candidate)
        if valid and valid not in out:
            out.append(valid)
        if len(out) >= 4:
            break
    return out or [DEFAULT_LOCALE]


def _accept_language(languages: list[str]) -> str:
    parts = []
    for i, lang in enumerate(languages):
        parts.append(lang if i == 0 else f"{lang};q={max(0.5, 1.0 - 0.1 * i):.1f}")
    return ",".join(parts)


def _session_context_options(
    client_timezone: Any = None,
    client_language: Any = None,
    client_languages: Any = None,
) -> dict[str, Any]:
    """launch_persistent_context kwargs giving the session its generic
    identity (everything except the user agent, which is read from the
    browser itself — see _session_user_agent). Pure and unit-tested:
    client values that fail validation fall back to the documented
    defaults, never to server-local settings."""
    languages = _client_languages(client_language, client_languages)
    width, height = random.choice(_SCREEN_SIZES)
    return {
        "viewport": {"width": width, "height": height - _BROWSER_CHROME_PX},
        "screen": {"width": width, "height": height},
        "locale": languages[0],
        "timezone_id": _valid_timezone(client_timezone) or DEFAULT_TIMEZONE,
        "color_scheme": "light",
        "extra_http_headers": {"Accept-Language": _accept_language(languages)},
        # Without this, Chromium exposes navigator.webdriver — the
        # loudest automation tell a page can check.
        "args": ["--disable-blink-features=AutomationControlled"],
    }


def _normalize_user_agent(ua: str) -> str:
    """The bundled browser's own user agent with the headless marker
    removed. Engine and version tokens are untouched, so the string
    still describes the real browser — only the mode flag goes."""
    return ua.replace("HeadlessChrome", "Chrome")


_DEFAULT_UA: str | None = None


async def _session_user_agent(pw: Any) -> str | None:
    """The normalized user agent for session contexts, read once per
    process from the bundled browser itself (so it can never drift
    from the real engine/version). None when the probe launch fails —
    the session then keeps the browser's default agent rather than
    failing to start."""
    global _DEFAULT_UA
    if _DEFAULT_UA is not None:
        return _DEFAULT_UA
    try:
        browser = await pw.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            ua = await page.evaluate("() => navigator.userAgent")
        finally:
            await browser.close()
    except Exception:
        return None
    if isinstance(ua, str) and ua:
        _DEFAULT_UA = _normalize_user_agent(ua)
        return _DEFAULT_UA
    return None


def _payload(record: PrefillSessionRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "application_id": record.application_id,
        "apply_url": record.apply_url,
        "status": record.status,
        "filled": json.loads(record.filled_json or "[]"),
        "needs_attention": json.loads(record.needs_attention_json or "[]"),
        "pending_candidates": json.loads(record.pending_candidates_json or "[]"),
        "notes": record.notes,
        "created_at": record.created_at.isoformat() if record.created_at else None,
    }


def _owned_record(db, session_id: str, user_id: int) -> PrefillSessionRecord:
    record = db.get(PrefillSessionRecord, session_id)
    if record is None or record.user_id != user_id:
        raise HTTPException(404, "Pre-fill session not found")
    return record


async def _stash_pending_candidates(live: _Live) -> None:
    """Last-chance answer capture before a session's browser is torn
    down: scan the form and keep whatever the user typed (diffed against
    what the agent filled) as PENDING candidates on the session record.
    Runs for every close reason — an idle timeout or a dropped stream
    must not silently throw away answers the user typed. Nothing is
    written to the answer bank here; the user still confirms each item
    before anything is saved."""
    if live.page is None:
        return
    try:
        scanned = await asyncio.wait_for(formfill._scan_form_values(live.page), timeout=10)
        candidates = formfill.answer_candidates(scanned, live.filled_values)
    except Exception:
        return
    if not candidates:
        return
    db = SessionLocal()
    try:
        # Answers already in the bank (e.g. saved moments ago from this
        # same session) are not pending — don't offer them twice.
        bank = profile_answers.answers_map(db, live.user_id)
        candidates = [c for c in candidates if bank.get(c["key"]) != c["answer"]]
        if not candidates:
            return
        record = db.get(PrefillSessionRecord, live.id)
        if record is not None:
            record.pending_candidates_json = json.dumps(candidates)
            db.commit()
    except Exception:
        pass
    finally:
        db.close()


async def _detect_and_mark_submission(live: _Live) -> None:
    """Check the session's page for the employer's submission
    confirmation and, when one is showing, mark the packet applied
    through the shared submission path. Runs when a session ends, so a
    submission the periodic watcher had no chance to see (the user
    submitted and closed the view immediately) is still caught."""
    if live.page is None:
        return
    try:
        evidence = await detect_submission(live.page)
    except Exception:
        return
    if not evidence:
        return
    live.submission_evidence = evidence
    db = SessionLocal()
    try:
        record = db.get(PrefillSessionRecord, live.id)
        application_id = record.application_id if record is not None else 0
    finally:
        db.close()
    if application_id:
        try:
            mark_detected_submission(application_id, evidence)
        except Exception:
            log.warning("prefill session %s: submission marking failed", live.id, exc_info=True)


async def _watch_live_submission(live: _Live, application_id: int) -> None:
    """Poll the session page while it runs; on the employer's
    confirmation, mark the packet applied (the tracker reflects it on
    the next refresh — no second click in the app)."""
    evidence = await watch_page_for_submission(live.page, application_id=application_id)
    if evidence:
        live.submission_evidence = evidence


async def _close_live(live: _Live, reason: str) -> None:
    if live.closed:
        return
    live.closed = True
    task = live.submission_task
    if task is not None and task is not asyncio.current_task():
        task.cancel()
    # A session that failed to start never showed the user a form, so
    # nothing on its page can be a submission they made.
    if reason != "startup failed":
        await _detect_and_mark_submission(live)
    await _stash_pending_candidates(live)
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
    # The connecting browser's own locale/timezone, used only to give
    # this session a consistent identity (see _session_context_options).
    # Session-scoped launch configuration — validated server-side and
    # never stored.
    client_timezone: str | None = None
    client_language: str | None = None
    client_languages: list[str] | None = None


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
        # The user's saved profile wins over the run's snapshot, so edits
        # made on the profile page reach this session's fill.
        profile = fill_profile(db, user.id, run)
        cover = application.cover_letter or ""
        resume_path = run.resume_path or ""
        location = primary_location(run.locations)
        apply_url = application.apply_url
        job_description = job.description if job else ""
        saved_answers = profile_answers.answers_map(db, user.id)
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
        context_options = _session_context_options(
            body.client_timezone, body.client_language, body.client_languages
        )
        user_agent = await _session_user_agent(live.pw)
        if user_agent:
            context_options["user_agent"] = user_agent
        live.context = await live.pw.chromium.launch_persistent_context(
            str(profile_dir),
            headless=True,
            **context_options,
        )
        page = live.context.pages[0] if live.context.pages else await live.context.new_page()
        live.page = page
        await page.goto(apply_url, wait_until="domcontentloaded", timeout=45_000)
        await formfill._settle(page)
        files = formfill._packet_files(profile, cover, resume_path, f"{run.id}-{application.job_id}")
        values = formfill._values(profile, location, cover, files, saved_answers)
        result = await formfill._fill_open_page(page, values)
        live.page = result.pop("_page", page)
        live.filled_values = result.pop("filled_values", {}) or {}
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
            job = db.get(JobMatch, application.job_id) if application.job_id else None
            record_application_seen(db, application, job, application.status)
            # A pre-fill outcome ("filled"/"prepared") is not a submission —
            # the user still clicks Submit — so this only fires if a session
            # outcome ever becomes a genuine "applied".
            advance_stage_on_submission(db, application, application.status)
        payload = _payload(record)
    finally:
        db.close()
    live.submission_task = asyncio.ensure_future(_watch_live_submission(live, body.application_id))
    log.info("prefill session %s created for user %s (application %s)", session_id, user.id, body.application_id)
    return payload


@router.get("/api/prefill-sessions/pending-candidates")
def pending_candidates(request: Request):
    """The newest session that ended with unreviewed answer candidates
    (stream drop, idle timeout, or a close before saving). The client
    offers them for confirmation; nothing here is in the bank yet."""
    user = auth.current_user(request)
    db = SessionLocal()
    try:
        rows = (
            db.query(PrefillSessionRecord)
            .filter(PrefillSessionRecord.user_id == user.id)
            .order_by(PrefillSessionRecord.created_at.desc())
            .limit(10)
            .all()
        )
        for record in rows:
            candidates = json.loads(record.pending_candidates_json or "[]")
            if candidates:
                return {
                    "session_id": record.id,
                    "application_id": record.application_id,
                    "candidates": candidates,
                }
        return {"session_id": None, "application_id": None, "candidates": []}
    finally:
        db.close()


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


@router.post("/api/prefill-sessions/{session_id}/answer-candidates")
async def answer_candidates(session_id: str, request: Request):
    """Re-scan the live form and return the values the user typed themselves
    (diffed against what the agent filled) as answer-bank candidates.
    Nothing is saved by this endpoint — the client shows the candidates and
    the user picks which ones to keep."""
    user = auth.current_user(request)
    db = SessionLocal()
    try:
        _owned_record(db, session_id, user.id)
    finally:
        db.close()
    live = _live.get(session_id)
    if live is None or live.closed or live.page is None:
        raise HTTPException(409, "This pre-fill session is no longer live.")
    scanned = await formfill._scan_form_values(live.page)
    return {"candidates": formfill.answer_candidates(scanned, live.filled_values)}


class SaveAnswers(BaseModel):
    answers: list[dict[str, str]]


@router.post("/api/prefill-sessions/{session_id}/save-answers")
def save_session_answers(session_id: str, body: SaveAnswers, request: Request):
    """Save the user-selected candidates into their profile answer bank."""
    user = auth.current_user(request)
    db = SessionLocal()
    try:
        record = _owned_record(db, session_id, user.id)
        saved = profile_answers.upsert_answers(db, user.id, body.answers)
        # Reviewed is reviewed: the pending stash from this session (if
        # any) has now been considered, so it must not be offered again.
        if record.pending_candidates_json not in (None, "", "[]"):
            record.pending_candidates_json = "[]"
            db.commit()
        by_key = {a["key"]: a for a in saved}
        return {"saved": [by_key[a["key"]] for a in body.answers if a.get("key") in by_key]}
    finally:
        db.close()


@router.post("/api/prefill-sessions/{session_id}/dismiss-candidates")
def dismiss_candidates(session_id: str, request: Request):
    """Drop a session's pending answer candidates without saving them."""
    user = auth.current_user(request)
    db = SessionLocal()
    try:
        record = _owned_record(db, session_id, user.id)
        record.pending_candidates_json = "[]"
        db.commit()
        return {"dismissed": True}
    finally:
        db.close()


# Fallback virtual key codes when the client did not send one (older client,
# or a key whose keyCode the browser reports as 0). Chromium performs no
# default editing for key events without a virtual key code, which is why
# Backspace/Delete/arrows used to do nothing in the streamed page.
_KEY_VK = {
    "Backspace": 8,
    "Tab": 9,
    "Enter": 13,
    "Escape": 27,
    " ": 32,
    "Spacebar": 32,
    "Space": 32,
    "PageUp": 33,
    "PageDown": 34,
    "End": 35,
    "Home": 36,
    "ArrowLeft": 37,
    "ArrowUp": 38,
    "ArrowRight": 39,
    "ArrowDown": 40,
    "Delete": 46,
}


# Keyboard-native clipboard bridging. Tests flip this to False to prove
# the fallbacks still work when the native route is unavailable.
NATIVE_CLIPBOARD = True

# CDP Browser.grantPermissions permission names for clipboard access. The
# sanitized-write variant restricts writes to sanitized formats (plain
# text is unaffected); Chromium builds that do not know it reject the
# whole grant, so the write-only name is retried on its own.
_CLIPBOARD_PERMISSIONS = ("clipboardReadWrite", "clipboardSanitizedWrite")
_CTRL_MODIFIER = 2  # CDP Input modifier bitmask bit for Control


def _page_origin(live: _Live) -> str | None:
    if live.page is None:
        return None
    try:
        from urllib.parse import urlsplit

        parts = urlsplit(live.page.url or "")
        if parts.scheme in {"http", "https"} and parts.netloc:
            return f"{parts.scheme}://{parts.netloc}"
    except Exception:
        pass
    return None


async def _grant_clipboard(live: _Live) -> bool:
    """Grant the session page's origin access to the session browser's
    clipboard. CDP first; Playwright's context-level grant (which drives
    the same CDP command at browser level) as a fallback."""
    origin = _page_origin(live)
    if origin is None:
        return False
    if live.cdp is not None:
        for permissions in (_CLIPBOARD_PERMISSIONS, ("clipboardReadWrite",)):
            try:
                await live.cdp.send(
                    "Browser.grantPermissions",
                    {"origin": origin, "permissions": list(permissions)},
                )
                return True
            except Exception:
                continue
    if live.context is not None:
        try:
            await live.context.grant_permissions(
                ["clipboard-read", "clipboard-write"], origin=origin
            )
            return True
        except Exception:
            pass
    return False


async def _clipboard_write(live: _Live, text: str) -> bool:
    """Set the session browser's clipboard from the page context. The
    main frame is tried first (it owns the granted origin); other frames
    follow in case focus/permission sits with one of them."""
    if live.page is None:
        return False
    frames = [live.page.main_frame] + [f for f in live.page.frames if f is not live.page.main_frame]
    for frame in frames:
        try:
            await frame.evaluate(
                "async (t) => { await navigator.clipboard.writeText(t); }", text
            )
            return True
        except Exception:
            continue
    return False


async def _clipboard_read(live: _Live) -> str | None:
    """Read the session browser's clipboard text, or None when the
    clipboard API is unreachable (denied permission, unfocused document,
    non-secure context)."""
    if live.page is None:
        return None
    frames = [live.page.main_frame] + [f for f in live.page.frames if f is not live.page.main_frame]
    for frame in frames:
        try:
            value = await frame.evaluate("async () => await navigator.clipboard.readText()")
        except Exception:
            continue
        if isinstance(value, str):
            return value
    return None


async def _dispatch_ctrl_key(live: _Live, key: str, code: str, vk: int) -> None:
    """A genuine Ctrl+<key> keystroke in the session page: the browser's
    own editing command runs (paste/copy), including the page's event
    handlers — unlike Input.insertText, which edits directly."""
    if live.cdp is None:
        raise RuntimeError("no CDP session")
    params = {
        "key": key,
        "code": code,
        "windowsVirtualKeyCode": vk,
        "nativeVirtualKeyCode": vk,
        "modifiers": _CTRL_MODIFIER,
    }
    await live.cdp.send("Input.dispatchKeyEvent", {"type": "keyDown", **params})
    await live.cdp.send("Input.dispatchKeyEvent", {"type": "keyUp", **params})


# The current selection, in whichever form the page holds it: text in a
# form control lives in the control's own selection range (Chrome does
# not expose it through window.getSelection), other text in the document
# selection. Password fields are excluded — browsers refuse to copy from
# them natively, and the bridge must not do it either.
_SELECTION_JS = """() => {
  const ae = document.activeElement;
  if (ae && typeof ae.selectionStart === 'number' && typeof ae.value === 'string'
      && (ae.tagName === 'TEXTAREA' || (ae.tagName === 'INPUT' && ae.type !== 'password'))) {
    return ae.value.substring(ae.selectionStart, ae.selectionEnd);
  }
  return window.getSelection ? window.getSelection().toString() : '';
}"""


async def _selection_text(live: _Live) -> str:
    """The text currently selected in the streamed page ('' if none).
    This is all the fallback copy route can reach: no selection, no
    copy; arbitrary page content is never read."""
    if live.page is None:
        return ""
    for frame in live.page.frames:
        if formfill.CAPTCHA_RE.search(frame.url or ""):
            continue
        try:
            value = await frame.evaluate(_SELECTION_JS)
        except Exception:
            continue
        if value and str(value).strip():
            return str(value)
    return ""


async def _native_paste(live: _Live, text: str) -> bool:
    """Put `text` on the session browser's clipboard and dispatch a real
    Ctrl+V. False when any step is unavailable — the caller degrades to
    Input.insertText."""
    try:
        if live.cdp is None or live.page is None:
            return False
        if not await _grant_clipboard(live):
            return False
        if not await _clipboard_write(live, text):
            return False
        await _dispatch_ctrl_key(live, "v", "KeyV", 86)
        return True
    except Exception:
        return False


async def _native_copy(live: _Live) -> str | None:
    """Dispatch a real Ctrl+C and read the session clipboard back.
    Returns the copied text ('' when nothing is selected, matching the
    fallback's answer), or None when the native route is unavailable and
    the caller should fall back to reading the selection."""
    try:
        if live.cdp is None or live.page is None:
            return None
        selection = await _selection_text(live)
        if not selection:
            return ""
        if not await _grant_clipboard(live):
            return None
        before = await _clipboard_read(live)
        await _dispatch_ctrl_key(live, "c", "KeyC", 67)
        # The clipboard write lands asynchronously after the keystroke.
        # Poll briefly, and distrust a value that never changed: if the
        # copy did not take effect, the clipboard still holds whatever
        # was there before, which is NOT what the user just selected.
        after: str | None = None
        for _ in range(8):
            await asyncio.sleep(0.1)
            after = await _clipboard_read(live)
            if after is not None and (after == selection or after != before):
                break
        if after is None:
            return None
        if after == before and after != selection:
            return None
        return after
    except Exception:
        return None


async def _dispatch_input(live: _Live, message: dict[str, Any]) -> dict[str, Any] | None:
    """Forward one client input message to the streamed page. Returns an
    optional reply for the client (clipboard text, paste outcome)."""
    cdp = live.cdp
    if cdp is None:
        return None
    kind = message.get("type")
    live.last_activity = datetime.utcnow()
    try:
        if kind == "paste":
            # Native paste: the client sends the user's clipboard text
            # (captured from the browser's paste event); the server puts
            # it on the session browser's clipboard and dispatches a
            # genuine Ctrl+V, so the page pastes exactly as it would for
            # a local user. Degrades to insertText when the clipboard
            # route is unavailable.
            text = (message.get("text") or "")[:10_000]
            via = "insert-text"
            if text:
                if NATIVE_CLIPBOARD and await _native_paste(live, text):
                    via = "native"
                else:
                    await cdp.send("Input.insertText", {"text": text})
            return {"type": "paste-result", "via": via}
        elif kind == "copy":
            # Native copy: a genuine Ctrl+C in the session page, with
            # the session clipboard read back for the client to place on
            # the user's real clipboard. Degrades to reading the page's
            # current selection.
            copied: str | None = None
            via = "selection"
            if NATIVE_CLIPBOARD:
                copied = await _native_copy(live)
                if copied is not None:
                    via = "native"
            if copied is None:
                copied = await _selection_text(live)
            return {"type": "clipboard", "text": copied, "via": via}
        elif kind == "insert-text":
            # Fallback paste (older clients, and the fallback paste box):
            # the text is inserted at the page's current focus/caret in
            # one edit. No paste event fires and the session clipboard is
            # untouched — which is exactly why it is only the fallback.
            text = message.get("text") or ""
            if text:
                await cdp.send("Input.insertText", {"text": text[:10_000]})
        elif kind == "copy-selection":
            # Fallback copy (older clients): read the page's current
            # selection back for the client to place on the user's real
            # clipboard. Only the selection is reachable.
            return {"type": "clipboard", "text": await _selection_text(live)}
        elif kind == "mouse":
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
                # Legacy single-character message from an older client.
                await cdp.send("Input.dispatchKeyEvent", {"type": "char", "text": message.get("text") or ""})
            elif action in {"down", "up"}:
                key = message.get("key") or ""
                code = message.get("code") or ""
                text = message.get("text") or ""
                vk = message.get("vk")
                if not isinstance(vk, int) or vk <= 0:
                    vk = _KEY_VK.get(key) or _KEY_VK.get(code) or 0
                params: dict[str, Any] = {"key": key, "code": code}
                if vk:
                    params["windowsVirtualKeyCode"] = vk
                    params["nativeVirtualKeyCode"] = vk
                modifiers = message.get("modifiers")
                if isinstance(modifiers, int) and modifiers:
                    params["modifiers"] = modifiers
                if action == "down":
                    down = dict(params)
                    if text:
                        down["text"] = text
                    # A full keystroke: keyDown carrying the text, then
                    # keyUp. Segmented one-time-code inputs run their state
                    # machines on keydown, so a bare char event — what the
                    # old client sent — left them unable to edit. But do
                    # NOT follow keyDown with a char event: Chromium
                    # inserts the character for keyDown-with-text already,
                    # so the extra char typed every character twice.
                    await cdp.send("Input.dispatchKeyEvent", {"type": "keyDown", **down})
                else:
                    await cdp.send("Input.dispatchKeyEvent", {"type": "keyUp", **params})
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
        # A previous stream may still be tearing down (the web client can
        # reconnect immediately, e.g. React StrictMode's double effect in
        # dev). Give it a moment to release before refusing the new one.
        for _ in range(20):
            if not live.ws_active or live.closed:
                break
            await asyncio.sleep(0.1)
    if live.closed:
        await websocket.close(code=4404)
        return
    if live.ws_active:
        await websocket.close(code=4409)
        return
    await websocket.accept()
    live.ws_active = True
    live.stream_gen += 1
    stream_gen = live.stream_gen
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
                    reply = await _dispatch_input(live, message)
                    if reply is not None:
                        await websocket.send_text(json.dumps(reply))
        except WebSocketDisconnect:
            pass
        finally:
            pump_task.cancel()
    finally:
        live.ws_active = False
        # A dropped stream ends the session — after a short grace period.
        # The web client can reconnect instantly (React StrictMode remounts
        # in dev; browsers retry after a blip), and a reconnect with the
        # same owner reattaches to the still-open form instead of losing
        # it. If nobody comes back, the ephemeral profile is wiped.
        async def _close_after_grace(gen: int = stream_gen) -> None:
            await asyncio.sleep(3)
            if live.stream_gen == gen and not live.ws_active and not live.closed:
                await _close_live(live, "stream disconnected")

        asyncio.ensure_future(_close_after_grace())
