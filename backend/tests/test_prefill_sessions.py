"""Streamed pre-fill sessions: ownership, single-session rule, ephemeral
profile cleanup, and a real session against a local test form.

The real-session tests skip with a clear reason only when Chromium cannot
run in this environment (session creation answers 503 in that case).

Clients are used as context managers on purpose: an entered TestClient
serves every request (and the WebSocket) from one event loop, which is
also how the real server runs — Playwright objects are bound to the loop
that created them.
"""
import asyncio
import contextlib
import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import auth, main, prefill_sessions
from app.agent import formfill
from app.db import SessionLocal
from app.models import Application, PrefillSessionRecord, ProfileAnswer, Run, SeenJob, User

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


SCREENSHOTS_DIR = Path(__file__).parent / "e2e_screenshots"


async def _take_screenshot(page, path: Path) -> None:
    await page.screenshot(path=str(path))


def _save_e2e_screenshot(client: TestClient, session_id: str, filename: str) -> None:
    """Save a PNG of the live session page into e2e_screenshots/ as
    review evidence. Purely a side effect: the page belongs to the app's
    event loop, so the capture is dispatched through the client's
    blocking portal, and any failure is swallowed — the assertions are
    the test, the screenshot never is."""
    try:
        live = prefill_sessions._live.get(session_id)
        portal = getattr(client, "portal", None)
        if live is None or live.page is None or portal is None:
            return
        SCREENSHOTS_DIR.mkdir(exist_ok=True)
        portal.call(_take_screenshot, live.page, SCREENSHOTS_DIR / filename)
    except Exception:
        pass


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


def test_session_fill_records_seen(form_server):
    """A streamed pre-fill moves the application to 'filled'; that outcome
    must also land in Previously applied, like the local fill path does."""
    email = _unique_email("pfseen")
    user_id = _make_user(email)
    app_id = _make_application(user_id, form_server)
    with _logged_in(email) as client:
        session = _create_session(client, app_id)
        with SessionLocal() as db:
            app = db.get(Application, app_id)
            assert app.status == "filled"
            rows = db.query(SeenJob).filter(SeenJob.user_id == user_id).all()
            assert len(rows) == 1
            assert rows[0].status == "filled"
            assert rows[0].title == "Software Engineer"
            assert rows[0].run_id == app.run_id
        listed = client.get("/api/seen").json()
        assert any(r["status"] == "filled" for r in listed)
        assert client.post(f"/api/prefill-sessions/{session['id']}/close").status_code == 200


def test_session_fill_does_not_advance_stage(form_server):
    """A streamed pre-fill fills the form but submits nothing — the user
    still clicks Submit — so the tracker stage must stay "not_applied"
    even though the application status becomes "filled"."""
    email = _unique_email("pfstage")
    user_id = _make_user(email)
    app_id = _make_application(user_id, form_server)
    with _logged_in(email) as client:
        session = _create_session(client, app_id)
        with SessionLocal() as db:
            app = db.get(Application, app_id)
            assert app.status == "filled"
            assert app.pipeline_stage == "not_applied"
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
        _save_e2e_screenshot(client, session["id"], "session_form_filled_from_profile.png")

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
            # The assertions below read `candidates`, already collected;
            # this extra message exercises the clipboard paste-in path
            # (insert-text over the same stream) purely so its result can
            # be screenshotted as evidence — the field ends up holding
            # the key-typed "ab" followed by the pasted-in text.
            ws.send_json({"type": "insert-text", "text": " pasted-in"})
            time.sleep(0.75)
            _save_e2e_screenshot(client, session["id"], "clipboard_paste_in.png")
        assert client.post(f"/api/prefill-sessions/{session['id']}/close").status_code == 200
    nick = [c for c in candidates if "Nickname" in c["question"]]
    assert nick, f"no Nickname candidate in {candidates}"
    assert nick[0]["answer"] == "ab", f"typed 'ab', field holds {nick[0]['answer']!r}"


RADIO_FORM_HTML = """<!doctype html>
<html><body>
<h1>Apply — Example Co</h1>
<form action="/submit" method="post">
  <div><label>Full name <input type="text" name="full_name"></label></div>
  <div><label>Email <input type="email" name="email"></label></div>
  <div><label>Phone <input type="tel" name="phone"></label></div>
  <fieldset>
    <legend>Are you authorized to work in the United States?</legend>
    <label><input type="radio" name="auth" value="yes" required> Yes</label>
    <label><input type="radio" name="auth" value="no" required> No</label>
  </fieldset>
  <button type="submit">Submit Application</button>
</form>
</body></html>"""

RADIO_QUESTION = "Are you authorized to work in the United States?"


@pytest.fixture(scope="module")
def radio_form_server():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = RADIO_FORM_HTML.encode()
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


def _save_answer(user_id: int, key: str, question: str, answer: str) -> None:
    with SessionLocal() as db:
        db.add(ProfileAnswer(user_id=user_id, key=key, question=question, answer=answer))
        db.commit()


def test_session_selects_the_saved_radio_answer(radio_form_server):
    """A stored answer bank entry selects the matching radio option during a
    streamed pre-fill. The filler only reports the group as filled when the
    option is actually checked in the page, so this is the real click."""
    email = _unique_email("pfradio")
    user_id = _make_user(email)
    _save_answer(user_id, "work_authorization", RADIO_QUESTION, "Yes")
    app_id = _make_application(user_id, radio_form_server)
    with _logged_in(email) as client:
        session = _create_session(client, app_id)
        assert RADIO_QUESTION in session["filled"], session["filled"]
        assert RADIO_QUESTION not in session["needs_attention"]
        _save_e2e_screenshot(client, session["id"], "radio_answer_selected.png")
        assert client.post(f"/api/prefill-sessions/{session['id']}/close").status_code == 200


def test_session_leaves_a_radio_group_without_a_saved_answer(radio_form_server):
    """No stored answer -> the group is never guessed; it stays highlighted
    in needs_attention for the user to answer themselves."""
    email = _unique_email("pfradiomissing")
    user_id = _make_user(email)
    app_id = _make_application(user_id, radio_form_server)
    with _logged_in(email) as client:
        session = _create_session(client, app_id)
        assert RADIO_QUESTION not in session["filled"]
        assert RADIO_QUESTION in session["needs_attention"], session["needs_attention"]
        assert client.post(f"/api/prefill-sessions/{session['id']}/close").status_code == 200



CLIP_FORM_HTML = """<!doctype html>
<html><body style="margin:0">
<form>
  <div><label>First entry <input id="first" type="text" name="first_entry" style="width:600px;height:70px"></label></div>
  <div style="margin-top:10px"><label>Second entry <input id="second" type="text" name="second_entry" style="width:600px;height:70px"></label></div>
</form>
<script>
  window.__pastes = [];
  document.addEventListener('paste', (e) => {
    window.__pastes.push({
      text: e.clipboardData ? e.clipboardData.getData('text') : null,
      target: e.target && e.target.id ? e.target.id : '',
      trusted: e.isTrusted,
    });
  });
</script>
</body></html>"""


@pytest.fixture(scope="module")
def clip_form_server():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = CLIP_FORM_HTML.encode()

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



# A react-select / Greenhouse-style custom dropdown: a role=combobox search
# input inside a [class*=control] container; the chosen value is rendered
# next to the (cleared) input, never as the input's own value. Typing into
# the search box only filters the menu — leaving the field without a click
# reverts it to the "Select..." placeholder.
DROPDOWN_FORM_HTML = """<!doctype html>
<html><body>
<h1>Apply — Example Co</h1>
<form action="/submit" method="post">
  <div><label>Full name <input type="text" name="full_name"></label></div>
  <div><label>Email <input type="email" name="email"></label></div>
  <div><label>Phone <input type="tel" name="phone"></label></div>
  <div class="question">
    <span class="question-label" id="gender-label">How do you identify? (gender identity)</span>
    <div class="select">
      <div class="select__control control">
        <div class="select__value-container">
          <div class="select__placeholder">Select...</div>
          <div class="select__input-container"><input type="text" role="combobox" aria-autocomplete="list" aria-controls="gender-listbox" aria-labelledby="gender-label" required></div>
        </div>
      </div>
      <div class="select__menu" hidden>
        <div role="listbox" id="gender-listbox">
          <div role="option" class="select__option">Female</div>
          <div role="option" class="select__option">Male</div>
          <div role="option" class="select__option">Non-binary</div>
          <div role="option" class="select__option">Prefer not to say</div>
        </div>
      </div>
    </div>
  </div>
  <button type="submit">Submit Application</button>
</form>
<script>
  const combo = document.querySelector('input[role="combobox"]');
  const menu = document.querySelector('.select__menu');
  const shown = document.querySelector('.select__placeholder');
  const opts = Array.from(document.querySelectorAll('[role="option"]'));
  let chosen = '';
  function render() {
    shown.textContent = chosen || 'Select...';
    const q = combo.value.trim().toLowerCase();
    opts.forEach((o) => {
      o.style.display = (!q || o.textContent.trim().toLowerCase().includes(q)) ? '' : 'none';
    });
  }
  combo.addEventListener('focus', () => { menu.hidden = false; render(); });
  combo.addEventListener('input', () => { menu.hidden = false; render(); });
  opts.forEach((o) => o.addEventListener('click', () => {
    chosen = o.textContent.trim();
    combo.value = '';
    menu.hidden = true;
    render();
  }));
  combo.addEventListener('blur', () => setTimeout(() => {
    menu.hidden = true;
    combo.value = '';
    render();
  }, 200));
  render();
</script>
</body></html>"""

DROPDOWN_QUESTION = "How do you identify? (gender identity)"


@pytest.fixture(scope="module")
def dropdown_form_server():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = DROPDOWN_FORM_HTML.encode()

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



async def _eval_on_page(page, expression):
    return await page.evaluate(expression)


def _page_eval(client: TestClient, session_id: str, expression: str):
    """Evaluate JS on the live session page. The page belongs to the
    app's event loop, so — like the screenshot helper — the call goes
    through the client's blocking portal."""
    live = prefill_sessions._live[session_id]
    return client.portal.call(_eval_on_page, live.page, expression)


def _await_reply(ws, want_type: str, timeout: float = 20.0) -> dict:
    """Read stream messages (screencast frames included) until the JSON
    control reply of the wanted type arrives."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        message = ws.receive()
        text = message.get("text")
        if not text:
            continue  # a binary screencast frame
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and data.get("type") == want_type:
            return data
    raise AssertionError(f"no {want_type!r} reply within {timeout}s")


def _click_field(ws, x: int, y: int) -> None:
    ws.send_json({"type": "mouse", "action": "press", "x": x, "y": y, "button": "left"})
    ws.send_json({"type": "mouse", "action": "release", "x": x, "y": y, "button": "left"})


def test_native_paste_fires_a_real_paste_event(clip_form_server):
    """The keyboard-native paste: the client forwards clipboard text, the
    server puts it on the session browser's clipboard and dispatches a
    genuine Ctrl+V. Proof this is not the old insert-text path: the page
    observes a trusted paste event carrying the text (insertText edits
    the field silently and fires no paste event at all)."""
    email = _unique_email("pfpaste")
    user_id = _make_user(email)
    app_id = _make_application(user_id, clip_form_server)
    with _logged_in(email) as client:
        session = _create_session(client, app_id)
        with client.websocket_connect(f"/api/prefill-sessions/{session['id']}/stream") as ws:
            ws.receive_bytes()  # first frame: stream is live
            _click_field(ws, 200, 35)
            assert _page_eval(client, session["id"], "() => document.activeElement.id") == "first"
            ws.send_json({"type": "paste", "text": "native paste text"})
            reply = _await_reply(ws, "paste-result")
            assert reply["via"] == "native", reply
            value = _page_eval(client, session["id"], "() => document.getElementById('first').value")
            assert value == "native paste text", value
            pastes = _page_eval(client, session["id"], "() => window.__pastes")
            assert pastes == [
                {"text": "native paste text", "target": "first", "trusted": True}
            ], pastes
        assert client.post(f"/api/prefill-sessions/{session['id']}/close").status_code == 200


def test_native_copy_then_paste_round_trip(clip_form_server):
    """Select text in the session page, copy with the native flow (real
    Ctrl+C + session clipboard read-back), then paste the returned text
    into a second field through the native paste — the full round trip
    the viewer's Ctrl+C / Ctrl+V now performs."""
    email = _unique_email("pfroundtrip")
    user_id = _make_user(email)
    app_id = _make_application(user_id, clip_form_server)
    with _logged_in(email) as client:
        session = _create_session(client, app_id)
        with client.websocket_connect(f"/api/prefill-sessions/{session['id']}/stream") as ws:
            ws.receive_bytes()
            _click_field(ws, 200, 35)
            ws.send_json({"type": "paste", "text": "round trip text"})
            assert _await_reply(ws, "paste-result")["via"] == "native"
            selected = _page_eval(
                client,
                session["id"],
                "() => { const el = document.getElementById('first');"
                " el.setSelectionRange(0, el.value.length);"
                " return el.value.substring(el.selectionStart, el.selectionEnd); }",
            )
            assert selected == "round trip text", selected
            ws.send_json({"type": "copy"})
            reply = _await_reply(ws, "clipboard")
            assert reply["via"] == "native", reply
            assert reply["text"] == "round trip text", reply
            _click_field(ws, 200, 115)
            assert _page_eval(client, session["id"], "() => document.activeElement.id") == "second"
            ws.send_json({"type": "paste", "text": reply["text"]})
            assert _await_reply(ws, "paste-result")["via"] == "native"
            value = _page_eval(client, session["id"], "() => document.getElementById('second').value")
            assert value == "round trip text", value
        assert client.post(f"/api/prefill-sessions/{session['id']}/close").status_code == 200


def test_clipboard_falls_back_when_native_route_is_unavailable(clip_form_server, monkeypatch):
    """With the native clipboard route disabled (as when the browser
    denies clipboard permission/API access), paste still lands — via
    insertText, which fires NO paste event — and copy still returns the
    selected text, read from the page's selection."""
    monkeypatch.setattr(prefill_sessions, "NATIVE_CLIPBOARD", False)
    email = _unique_email("pffallback")
    user_id = _make_user(email)
    app_id = _make_application(user_id, clip_form_server)
    with _logged_in(email) as client:
        session = _create_session(client, app_id)
        with client.websocket_connect(f"/api/prefill-sessions/{session['id']}/stream") as ws:
            ws.receive_bytes()
            _click_field(ws, 200, 35)
            ws.send_json({"type": "paste", "text": "fallback paste text"})
            reply = _await_reply(ws, "paste-result")
            assert reply["via"] == "insert-text", reply
            value = _page_eval(client, session["id"], "() => document.getElementById('first').value")
            assert value == "fallback paste text", value
            # insertText edits directly: no paste event may have fired.
            pastes = _page_eval(client, session["id"], "() => window.__pastes")
            assert pastes == [], pastes
            _page_eval(
                client,
                session["id"],
                "() => { const el = document.getElementById('first');"
                " el.setSelectionRange(0, el.value.length); }",
            )
            ws.send_json({"type": "copy"})
            reply = _await_reply(ws, "clipboard")
            assert reply["via"] == "selection", reply
            assert reply["text"] == "fallback paste text", reply
        assert client.post(f"/api/prefill-sessions/{session['id']}/close").status_code == 200



async def _control_text(page) -> str:
    return await page.evaluate(
        "() => { const box = document.querySelector('.select__control');"
        " return box ? (box.textContent || '').replace(/\\s+/g, ' ').trim() : ''; }"
    )


def _session_control_text(client: TestClient, session_id: str) -> str:
    """The dropdown control's rendered text on the live session page, read
    through the client's blocking portal (the same dispatch the screenshot
    helper uses)."""
    live = prefill_sessions._live.get(session_id)
    portal = getattr(client, "portal", None)
    assert live is not None and live.page is not None and portal is not None
    return portal.call(_control_text, live.page)


def test_session_selects_the_saved_combobox_answer(dropdown_form_server):
    """A stored answer selects the matching option in a custom dropdown
    during a streamed pre-fill. The regression this pins: the filler used
    to type the answer into the dropdown's search box and report the field
    filled while the control still showed "Select...". The rendered text
    of the control — not just the fill report — must show the choice."""
    email = _unique_email("pfcombo")
    user_id = _make_user(email)
    _save_answer(user_id, "gender", DROPDOWN_QUESTION, "Female")
    app_id = _make_application(user_id, dropdown_form_server)
    with _logged_in(email) as client:
        session = _create_session(client, app_id)
        assert DROPDOWN_QUESTION in session["filled"], session["filled"]
        assert DROPDOWN_QUESTION not in session["needs_attention"]
        assert _session_control_text(client, session["id"]) == "Female"
        _save_e2e_screenshot(client, session["id"], "combobox_answer_selected.png")
        assert client.post(f"/api/prefill-sessions/{session['id']}/close").status_code == 200


def test_session_combobox_answer_matching_no_option_needs_attention(dropdown_form_server):
    """A stored answer that matches none of the dropdown's options selects
    nothing: the question stays in needs_attention instead of being
    reported filled on the strength of typed text."""
    email = _unique_email("pfcombonone")
    user_id = _make_user(email)
    _save_answer(user_id, "gender", DROPDOWN_QUESTION, "Decline to answer")
    app_id = _make_application(user_id, dropdown_form_server)
    with _logged_in(email) as client:
        session = _create_session(client, app_id)
        assert DROPDOWN_QUESTION not in session["filled"], session["filled"]
        assert DROPDOWN_QUESTION in session["needs_attention"], session["needs_attention"]
        assert _session_control_text(client, session["id"]) == "Select..."
        assert client.post(f"/api/prefill-sessions/{session['id']}/close").status_code == 200


def test_local_fill_demotes_a_combobox_fill_that_did_not_happen(dropdown_form_server, monkeypatch):
    """Verify-and-demote: when a fill reports success but the value never
    lands in the widget, the field must end up in needs_attention — not in
    filled, and not in filled_values (the answer-capture diff treats
    filled_values entries as agent-filled). Fields that really filled
    keep their filled status."""
    real_fill_field = formfill._fill_field

    async def stub_fill_field(f, kind, value):
        if f.get("combobox"):
            return True  # claims success without touching the page
        return await real_fill_field(f, kind, value)

    monkeypatch.setattr(formfill, "_fill_field", stub_fill_field)
    result = asyncio.run(
        formfill.fill_application(
            profile={
                "name": "Priya Nair",
                "email": "priya.nair@example.com",
                "phone": "+1 (415) 555-0148",
            },
            job={"apply_url": dropdown_form_server},
            cover="",
            resume_path="",
            location="San Francisco, CA",
            packet_key=f"combo-demote-{uuid.uuid4().hex[:8]}",
            answers={"gender": "Female"},
        )
    )
    assert result["channel"] == "browser", result["notes"]
    assert DROPDOWN_QUESTION not in result["filled"], result["filled"]
    assert DROPDOWN_QUESTION in result["needs_attention"], result["needs_attention"]
    assert "Full name" in result["filled"]
    assert formfill._norm(DROPDOWN_QUESTION) not in result["filled_values"]

