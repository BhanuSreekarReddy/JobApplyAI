"""Automatic submission detection: when the user submits the employer's
form themselves and the site confirms it, the packet is marked applied
without a second "Mark submitted" click in the app.

Covered here:
- the detector's signals (strong confirmation text; confirmation URL
  only once the form is gone) and its negatives (a Submit button or a
  "Submit your application" heading must never trigger);
- the marking outcome rules (evidence note, stage advance, seen record;
  already-applied is a no-op; emailed packets are never touched);
- a real local fill: the user clicks Submit in the filled tab, the
  employer's confirmation page appears, and the watcher marks the
  packet — while an unsubmitted form is never marked;
- a streamed session that ends on a confirmation page is marked on
  close.
"""
import asyncio
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app import prefill_sessions
from app.agent import formfill, submission
from app.db import SessionLocal
from app.models import Application, JobMatch, PrefillSessionRecord, Run, SeenJob


# ------------------------------------------------------------- fake pages


class _FakeFrame:
    def __init__(self, state, url="https://example.com/apply"):
        self._state = state
        self.url = url

    async def evaluate(self, script, arg=None):
        return self._state


class _FakePage:
    def __init__(self, text, has_form, url="https://example.com/apply"):
        self.frames = [_FakeFrame({"text": text, "has_form": has_form}, url)]
        self.url = url

    def is_closed(self):
        return False


# ------------------------------------------------------------- detector


def test_detector_recognizes_strong_confirmation_text():
    page = _FakePage("Thank you for applying. We have received your application.", has_form=False)
    assert asyncio.run(submission.detect_submission(page)) == "thank you for applying"


def test_detector_recognizes_application_submitted_text():
    page = _FakePage("Your application has been submitted.", has_form=False)
    assert asyncio.run(submission.detect_submission(page)) == "application has been submitted"


def test_detector_uses_confirmation_url_only_when_the_form_is_gone():
    gone = _FakePage("Done.", has_form=False, url="https://example.com/apply/confirmation")
    assert asyncio.run(submission.detect_submission(gone)) == "confirmation-style URL"

    still_there = _FakePage("Done.", has_form=True, url="https://example.com/apply/confirmation")
    assert asyncio.run(submission.detect_submission(still_there)) is None


@pytest.mark.parametrize(
    "text",
    [
        "Submit your application",  # a heading on the unsubmitted form
        "Full name Email Phone Submit Application",  # the form itself
        "Apply now for this role",
    ],
)
def test_detector_ignores_form_prompts(text):
    page = _FakePage(text, has_form=True)
    assert asyncio.run(submission.detect_submission(page)) is None


# ------------------------------------------------------------- marking


def _seed_application(status="filled", channel="browser", notes="Form pre-filled."):
    run_id = f"sub-{uuid.uuid4().hex[:10]}"
    with SessionLocal() as db:
        db.add(Run(id=run_id, status="completed", locations="San Francisco, CA", profile_json="{}"))
        job = JobMatch(
            run_id=run_id,
            title="Software Engineer",
            company="Example Co",
            url="https://example.com/jobs/1",
            apply_url="https://example.com/apply",
        )
        db.add(job)
        db.flush()
        app = Application(
            run_id=run_id,
            job_id=job.id,
            title=job.title,
            company=job.company,
            status=status,
            channel=channel,
            notes=notes,
            apply_url=job.apply_url,
        )
        db.add(app)
        db.commit()
        db.refresh(app)
        return run_id, app.id


def _cleanup(run_id):
    with SessionLocal() as db:
        db.query(SeenJob).filter(SeenJob.run_id == run_id).delete()
        db.query(Application).filter(Application.run_id == run_id).delete()
        db.query(JobMatch).filter(JobMatch.run_id == run_id).delete()
        db.query(Run).filter(Run.id == run_id).delete()
        db.commit()


def _read_application(app_id):
    with SessionLocal() as db:
        app = db.get(Application, app_id)
        return app.status, app.pipeline_stage, app.notes


def test_mark_detected_submission_marks_applied_with_evidence():
    run_id, app_id = _seed_application()
    try:
        outcome = submission.mark_detected_submission(app_id, "application has been submitted")
        assert outcome == "marked"
        status, stage, notes = _read_application(app_id)
        assert status == "applied"
        assert stage == "applied"
        assert notes.startswith(
            "Marked applied automatically — confirmation detected: application has been submitted."
        )
        with SessionLocal() as db:
            rows = db.query(SeenJob).filter(SeenJob.run_id == run_id).all()
            assert len(rows) == 1
            assert rows[0].status == "applied"
    finally:
        _cleanup(run_id)


def test_mark_detected_submission_already_applied_is_a_noop():
    run_id, app_id = _seed_application(status="applied", notes="Sent resume and cover letter.")
    try:
        with SessionLocal() as db:
            app = db.get(Application, app_id)
            app.pipeline_stage = "applied"
            db.commit()
        outcome = submission.mark_detected_submission(app_id, "application has been submitted")
        assert outcome == "already_applied"
        status, stage, notes = _read_application(app_id)
        assert status == "applied"
        assert notes == "Sent resume and cover letter."  # no evidence note appended
        with SessionLocal() as db:
            assert db.query(SeenJob).filter(SeenJob.run_id == run_id).all() == []
    finally:
        _cleanup(run_id)


def test_mark_detected_submission_never_touches_emailed_packets():
    run_id, app_id = _seed_application(status="prepared", channel="mailto", notes="Apply by email.")
    try:
        outcome = submission.mark_detected_submission(app_id, "application has been submitted")
        assert outcome == "ineligible"
        status, stage, notes = _read_application(app_id)
        assert status == "prepared"
        assert stage == "not_applied"
        assert notes == "Apply by email."
    finally:
        _cleanup(run_id)


# ------------------------------------------------------------- local flow


FORM_HTML = """<!doctype html>
<html><body>
<h1>Submit your application</h1>
<form action="/confirmation" method="get">
  <div><label>Full name <input type="text" name="full_name"></label></div>
  <div><label>Email <input type="email" name="email"></label></div>
  <div><label>Phone <input type="tel" name="phone"></label></div>
  <button type="submit">Submit Application</button>
</form>
</body></html>"""

CONFIRMATION_HTML = """<!doctype html>
<html><body>
<h1>Thank you for applying</h1>
<p>Your application has been submitted.</p>
</body></html>"""


@pytest.fixture(scope="module")
def local_form_server():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            path = self.path.split("?", 1)[0]
            body = CONFIRMATION_HTML.encode() if path == "/confirmation" else FORM_HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/apply"
    server.shutdown()


def _local_fill(local_form_server, monkeypatch):
    """Fill the local test form and mirror what the fill endpoint does
    with the result. Returns (run_id, app_id, packet_key)."""
    monkeypatch.setattr(submission, "POLL_INTERVAL_S", 0.1)
    monkeypatch.setattr(submission, "WATCH_TIMEOUT_S", 15.0)
    run_id, app_id = _seed_application(status="prepared", notes="Packet ready.")
    with SessionLocal() as db:
        app = db.get(Application, app_id)
        job_id = app.job_id
        app.apply_url = local_form_server
        db.commit()
    packet_key = f"{run_id}-{job_id}"
    result = asyncio.run(
        formfill.fill_application(
            profile={
                "name": "Priya Nair",
                "email": "priya.nair@example.com",
                "phone": "+1 (415) 555-0148",
            },
            job={"apply_url": local_form_server, "title": "Software Engineer", "company": "Example Co"},
            cover="",
            resume_path="",
            location="San Francisco, CA",
            packet_key=packet_key,
            answers={},
        )
    )
    assert result["status"] == "filled", result["notes"]
    with SessionLocal() as db:
        app = db.get(Application, app_id)
        app.status = result["status"]
        app.channel = result["channel"]
        app.notes = result["notes"]
        db.commit()
    return run_id, app_id, packet_key


def _close_registered_page(packet_key):
    entry = formfill._filled_pages.pop(packet_key, None)
    if entry is None:
        return
    task = entry.get("submission_task")
    if task is not None:
        task.cancel()
    page = entry["page"]

    async def _close():
        await page.close()

    try:
        asyncio.run(formfill._worker.call(_close()))
    except Exception:
        pass


def test_local_fill_marks_applied_after_the_user_submits(local_form_server, monkeypatch):
    run_id, app_id, packet_key = _local_fill(local_form_server, monkeypatch)
    try:
        page = formfill._filled_pages[packet_key]["page"]

        async def _submit():
            await page.get_by_role("button", name="Submit Application").click()
            await page.wait_for_url("**/confirmation**")

        asyncio.run(formfill._worker.call(_submit()))

        deadline = time.time() + 10
        status = stage = notes = ""
        while time.time() < deadline:
            status, stage, notes = _read_application(app_id)
            if status == "applied":
                break
            time.sleep(0.2)
        assert status == "applied"
        assert stage == "applied"
        assert "Marked applied automatically — confirmation detected:" in notes
        with SessionLocal() as db:
            rows = db.query(SeenJob).filter(SeenJob.run_id == run_id).all()
            assert len(rows) == 1
            assert rows[0].status == "applied"
    finally:
        _close_registered_page(packet_key)
        _cleanup(run_id)


def test_local_fill_without_submission_is_not_marked(local_form_server, monkeypatch):
    run_id, app_id, packet_key = _local_fill(local_form_server, monkeypatch)
    try:
        time.sleep(1.0)  # several watcher ticks against the open form
        status, stage, notes = _read_application(app_id)
        assert status == "filled"
        assert stage == "not_applied"
        assert "Marked applied automatically" not in notes
    finally:
        _close_registered_page(packet_key)
        _cleanup(run_id)


# ------------------------------------------------------------- hosted flow


def _seed_hosted_session(tmp_path, page):
    run_id, app_id = _seed_application(status="filled")
    session_id = f"sub{uuid.uuid4().hex[:12]}"
    with SessionLocal() as db:
        db.add(
            PrefillSessionRecord(
                id=session_id,
                user_id=0,
                application_id=app_id,
                apply_url="https://example.com/apply",
                status="ready",
                profile_dir=str(tmp_path / "profile"),
            )
        )
        db.commit()
    live = prefill_sessions._Live(session_id, 0, tmp_path / "profile")
    live.page = page
    return run_id, app_id, session_id, live


def _cleanup_hosted(run_id, session_id):
    with SessionLocal() as db:
        db.query(PrefillSessionRecord).filter(PrefillSessionRecord.id == session_id).delete()
        db.commit()
    _cleanup(run_id)


def test_session_ending_on_a_confirmation_page_marks_applied(tmp_path):
    page = _FakePage("Thank you for applying. Your application has been submitted.", has_form=False)
    run_id, app_id, session_id, live = _seed_hosted_session(tmp_path, page)
    try:
        asyncio.run(prefill_sessions._close_live(live, "test close"))
        status, stage, notes = _read_application(app_id)
        assert status == "applied"
        assert stage == "applied"
        assert "Marked applied automatically — confirmation detected:" in notes
        assert live.submission_evidence == "application has been submitted"
    finally:
        _cleanup_hosted(run_id, session_id)


def test_session_ending_on_the_unsubmitted_form_marks_nothing(tmp_path):
    page = _FakePage("Full name Email Phone Submit your application", has_form=True)
    run_id, app_id, session_id, live = _seed_hosted_session(tmp_path, page)
    try:
        asyncio.run(prefill_sessions._close_live(live, "test close"))
        status, stage, notes = _read_application(app_id)
        assert status == "filled"
        assert stage == "not_applied"
        assert "Marked applied automatically" not in notes
        assert live.submission_evidence is None
    finally:
        _cleanup_hosted(run_id, session_id)
