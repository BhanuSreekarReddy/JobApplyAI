"""Streamed pre-fill sessions: ownership, single-session rule, ephemeral
profile cleanup, and a real session against a local test form.

The real-session tests skip with a clear reason only when Chromium cannot
run in this environment (session creation answers 503 in that case).

Clients are used as context managers on purpose: an entered TestClient
serves every request (and the WebSocket) from one event loop, which is
also how the real server runs — Playwright objects are bound to the loop
that created them.
"""
import contextlib
import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import auth, main
from app.db import SessionLocal
from app.models import Application, PrefillSessionRecord, Run, User

PASSWORD = "TestPass123!"

FORM_HTML = """<!doctype html>
<html><body>
<h1>Apply — Example Co</h1>
<form action="/submit" method="post">
  <div><label>Full name <input type="text" name="full_name"></label></div>
  <div><label>Email <input type="email" name="email"></label></div>
  <div><label>Phone <input type="tel" name="phone"></label></div>
  <div><label>Cover letter <textarea name="cover_letter" rows="6" cols="50"></textarea></label></div>
  <button type="submit">Submit Application</button>
</form>
</body></html>"""


@pytest.fixture(scope="module")
def form_server():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = FORM_HTML.encode()
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


@pytest.fixture(autouse=True)
def _real_guard(_authenticated_guard):
    main.app.dependency_overrides.pop(auth.auth_guard, None)
    yield


def _unique_email(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}@test.local"


def _make_user(email: str) -> int:
    with SessionLocal() as db:
        user = User(
            email=email,
            password_hash=auth.hash_password(PASSWORD),
            role="user",
            is_active=1,
            email_verified=1,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        return user.id


@contextlib.contextmanager
def _logged_in(email: str):
    with TestClient(main.app) as client:
        r = client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
        assert r.status_code == 200, r.text
        yield client


def _make_application(user_id: int, apply_url: str) -> int:
    run_id = f"pf{uuid.uuid4().hex[:10]}"
    profile = {
        "name": "Priya Nair",
        "email": "priya.nair@example.com",
        "phone": "+1 (415) 555-0148",
    }
    with SessionLocal() as db:
        db.add(
            Run(
                id=run_id,
                user_id=user_id,
                status="completed",
                locations="San Francisco, CA",
                profile_json=json.dumps(profile),
            )
        )
        app = Application(
            run_id=run_id,
            user_id=user_id,
            title="Software Engineer",
            company="Example Co",
            apply_url=apply_url,
            status="prepared",
            cover_letter="Dear Example Co team, I am excited to apply.",
        )
        db.add(app)
        db.commit()
        db.refresh(app)
        return app.id


def _create_session(client: TestClient, application_id: int):
    r = client.post("/api/prefill-sessions", json={"application_id": application_id})
    if r.status_code == 503:
        pytest.skip(f"Chromium/Playwright unavailable in this environment: {r.json().get('detail')}")
    assert r.status_code == 200, r.text
    return r.json()


def _wait_for_status(client: TestClient, session_id: str, status: str, timeout: float = 10.0) -> dict:
    deadline = time.time() + timeout
    payload = {}
    while time.time() < deadline:
        payload = client.get(f"/api/prefill-sessions/{session_id}").json()
        if payload.get("status") == status:
            return payload
        time.sleep(0.25)
    return payload


def test_session_create_for_another_users_application_is_404(form_server):
    owner_id = _make_user(_unique_email("pfowner"))
    other_email = _unique_email("pfother")
    _make_user(other_email)
    app_id = _make_application(owner_id, form_server)
    with _logged_in(other_email) as other:
        r = other.post("/api/prefill-sessions", json={"application_id": app_id})
        assert r.status_code == 404


def test_second_concurrent_session_is_409(form_server):
    email = _unique_email("pftwo")
    user_id = _make_user(email)
    app_id = _make_application(user_id, form_server)
    with _logged_in(email) as client:
        session = _create_session(client, app_id)
        r = client.post("/api/prefill-sessions", json={"application_id": app_id})
        assert r.status_code == 409
        assert client.post(f"/api/prefill-sessions/{session['id']}/close").status_code == 200


def test_close_wipes_the_ephemeral_profile(form_server):
    email = _unique_email("pfwipe")
    user_id = _make_user(email)
    app_id = _make_application(user_id, form_server)
    with _logged_in(email) as client:
        session = _create_session(client, app_id)
        with SessionLocal() as db:
            profile_dir = Path(db.get(PrefillSessionRecord, session["id"]).profile_dir)
        assert profile_dir.exists()
        r = client.post(f"/api/prefill-sessions/{session['id']}/close")
        assert r.status_code == 200
        assert not profile_dir.exists()
        got = _wait_for_status(client, session["id"], "closed")
        assert got["status"] == "closed"


def test_real_session_fills_form_and_streams_frames(form_server):
    email = _unique_email("pfreal")
    user_id = _make_user(email)
    app_id = _make_application(user_id, form_server)
    with _logged_in(email) as client:
        session = _create_session(client, app_id)
        assert session["status"] == "ready"
        filled = session["filled"]
        assert "Full name" in filled, filled
        assert "Email" in filled, filled
        assert "Phone" in filled, filled

        with SessionLocal() as db:
            profile_dir = Path(db.get(PrefillSessionRecord, session["id"]).profile_dir)

        with client.websocket_connect(f"/api/prefill-sessions/{session['id']}/stream") as ws:
            frame = ws.receive_bytes()
            assert len(frame) > 100  # a JPEG screencast frame
            ws.send_text(json.dumps({"type": "mouse", "action": "press", "x": 40, "y": 40, "button": "left"}))
            ws.send_text(json.dumps({"type": "mouse", "action": "release", "x": 40, "y": 40, "button": "left"}))
        # Disconnecting the stream closes the session and wipes the profile.
        got = _wait_for_status(client, session["id"], "closed")
        assert got["status"] == "closed"
        assert not profile_dir.exists()


TYPE_FORM_HTML = """<!doctype html>
<html><body style="margin:0">
<form>
  <div><label>Nickname <input id="nick" type="text" name="nickname" autofocus style="width:600px;height:80px"></label></div>
</form>
</body></html>"""


@pytest.fixture(scope="module")
def type_form_server():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = TYPE_FORM_HTML.encode()
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


def test_typed_characters_appear_once(type_form_server):
    """Regression: one keystroke in the streamed viewer must insert one
    character. The keyboard fix sent keyDown (carrying text) followed by a
    char event, and Chromium inserts the character for both."""
    email = _unique_email("pftype")
    user_id = _make_user(email)
    app_id = _make_application(user_id, type_form_server)
    with _logged_in(email) as client:
        session = _create_session(client, app_id)
        with client.websocket_connect(f"/api/prefill-sessions/{session['id']}/stream") as ws:
            ws.receive_bytes()  # first frame: stream is live
            ws.send_json({"type": "mouse", "action": "press", "x": 200, "y": 40, "button": "left"})
            ws.send_json({"type": "mouse", "action": "release", "x": 200, "y": 40, "button": "left"})
            for key, code, vk in (("a", "KeyA", 65), ("b", "KeyB", 66)):
                ws.send_json({"type": "key", "action": "down", "key": key, "code": code, "text": key, "vk": vk})
                ws.send_json({"type": "key", "action": "up", "key": key, "code": code, "vk": vk})
            time.sleep(0.75)
            r = client.post(f"/api/prefill-sessions/{session['id']}/answer-candidates")
            assert r.status_code == 200, r.text
            candidates = r.json()["candidates"]
        assert client.post(f"/api/prefill-sessions/{session['id']}/close").status_code == 200
    nick = [c for c in candidates if "Nickname" in c["question"]]
    assert nick, f"no Nickname candidate in {candidates}"
    assert nick[0]["answer"] == "ab", f"typed 'ab', field holds {nick[0]['answer']!r}"
