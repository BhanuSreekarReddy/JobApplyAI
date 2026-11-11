"""Profile answer bank: classification, fill planning, capture diffing,
CRUD + per-user isolation, streamed-session reuse, and the streamed input
(keyboard) dispatch fixes.

The real-session tests skip with a clear reason only when Chromium cannot
run in this environment (session creation answers 503 in that case), like
test_prefill_sessions.
"""
import asyncio
import contextlib
import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import auth, main, prefill_sessions
from app.agent import formfill
from app.agent.formfill import _classify, _classify_answer, _planned_value, answer_candidates
from app.db import SessionLocal
from app.models import Application, ProfileAnswer, Run, User

PASSWORD = "TestPass123!"


@pytest.fixture(autouse=True)
def _real_guard(_authenticated_guard):
    main.app.dependency_overrides.pop(auth.auth_guard, None)
    yield


# ------------------------------------------------------------ classification

def _field(label, type_="text", tag="input", name="", options=None):
    f = {
        "label": label, "type": type_, "tag": tag, "name": name, "idAttr": "",
        "placeholder": "", "autocomplete": "", "options": options or [],
        "combobox": False, "filled": False, "required": True,
    }
    f["kind"] = _classify(f)
    f["answer_key"] = _classify_answer(f)
    return f


@pytest.mark.parametrize(
    "label, key",
    [
        ("Will you now or in the future require sponsorship?", "sponsorship"),
        ("Citizenship Status", "citizenship_status"),
        ("Country", "country"),
        ("Are you willing to work onsite?", "work_onsite"),
        ("Please share with us your salary expectations", "salary_expectations"),
        ("What is your earliest start date?", "start_date"),
        ("How did you hear about this opportunity?", "how_heard"),
        ("Have you previously interviewed at Rocket Lab?", "previously_interviewed"),
        ("Have you ever been employed by the company?", "previously_employed"),
        ("Active Security Clearance(s)", "security_clearance"),
        ("Are you authorized to work in the United States?", "work_authorization"),
        ("Gender", "gender"),
        ("Veteran Status", "veteran_status"),
    ],
)
def test_classify_answer(label, key):
    assert _classify_answer({"label": label, "name": "", "placeholder": "", "idAttr": ""}) == key


def test_saved_answer_fills_blocklisted_field():
    f = _field(
        "Will you now or in the future require sponsorship?",
        type_="select", tag="select", options=["Select", "No", "Yes"],
    )
    assert f["kind"] is None  # blocklisted: the resume classifier refuses it
    key, value = _planned_value(f, {"sponsorship": "No"})
    assert (key, value) == ("sponsorship", "No")


def test_blocklisted_field_without_saved_answer_gets_no_value():
    f = _field(
        "Will you now or in the future require sponsorship?",
        type_="select", tag="select", options=["Select", "No", "Yes"],
    )
    key, value = _planned_value(f, {})
    assert value is None  # -> lands in needs_attention, exactly as before


def test_free_form_answer_matches_by_normalized_label():
    f = _field("Any offer of employment is contingent on a background check")
    assert f["answer_key"] is None
    key, value = _planned_value(
        f, {"any offer of employment is contingent on a background check": "I understand"}
    )
    assert value == "I understand"


def test_resume_kind_still_wins_over_answer_bank():
    f = _field("Email address", type_="email")
    key, value = _planned_value(f, {"email": "resume@example.com", "email address": "bank@example.com"})
    assert (key, value) == ("email", "resume@example.com")


# ------------------------------------------------------------ capture diffing

def _scanned(label, value, type_="text", tag="input", name=""):
    return {"label": label, "value": value, "type": type_, "tag": tag, "name": name}


def test_answer_candidates_diffing_and_exclusions():
    scanned = [
        _scanned("Citizenship Status", "U.S. Citizen", "select", "select"),
        _scanned("Gender", "Female", "select", "select"),
        _scanned("Security code", "123456"),
        _scanned("Verification Code", "998877"),
        _scanned("Password", "hunter2", "password"),
        _scanned("Resume", "cv.pdf", "file"),
        _scanned("Email", "priya@example.com", "email"),
        _scanned("Country", "United States", "select", "select"),
        _scanned("Salary expectations", ""),
        _scanned("How did you hear about this opportunity?", "LinkedIn"),
    ]
    cands = answer_candidates(scanned, {"country": "United States"})
    by_key = {c["key"]: c for c in cands}
    # Country was filled by the agent with the same value -> not a candidate.
    # Email is a resume field; codes/passwords/files/empties are excluded.
    assert set(by_key) == {"citizenship_status", "gender", "how_heard"}
    assert by_key["citizenship_status"]["answer"] == "U.S. Citizen"
    assert by_key["citizenship_status"]["sensitive"] is False
    assert by_key["gender"]["sensitive"] is True
    assert by_key["how_heard"]["answer"] == "LinkedIn"


def test_answer_candidates_user_edit_of_agent_value_is_offered():
    scanned = [_scanned("Country", "Canada", "select", "select")]
    cands = answer_candidates(scanned, {"country": "United States"})
    assert [c["key"] for c in cands] == ["country"]
    assert cands[0]["answer"] == "Canada"


# ------------------------------------------------------------ keyboard dispatch

class _FakeCDP:
    def __init__(self):
        self.sent = []

    async def send(self, method, params):
        self.sent.append((method, params))


def _live_with_cdp(tmp_path) -> prefill_sessions._Live:
    live = prefill_sessions._Live("testsession", 1, Path(tmp_path))
    live.cdp = _FakeCDP()
    return live


def _key_events(live) -> list[dict]:
    return [p for m, p in live.cdp.sent if m == "Input.dispatchKeyEvent"]


def test_key_down_with_text_sends_keydown_then_char(tmp_path):
    live = _live_with_cdp(tmp_path)
    asyncio.run(prefill_sessions._dispatch_input(
        live, {"type": "key", "action": "down", "key": "5", "code": "Digit5", "text": "5", "vk": 53}
    ))
    events = _key_events(live)
    assert [e["type"] for e in events] == ["keyDown", "char"]
    assert events[0]["windowsVirtualKeyCode"] == 53
    assert events[0]["nativeVirtualKeyCode"] == 53
    assert events[0]["text"] == "5"
    assert events[1]["text"] == "5"

    asyncio.run(prefill_sessions._dispatch_input(
        live, {"type": "key", "action": "up", "key": "5", "code": "Digit5", "vk": 53}
    ))
    events = _key_events(live)
    assert events[-1]["type"] == "keyUp"
    assert events[-1]["windowsVirtualKeyCode"] == 53


def test_editing_keys_fall_back_to_virtual_key_codes(tmp_path):
    live = _live_with_cdp(tmp_path)
    asyncio.run(prefill_sessions._dispatch_input(
        live, {"type": "key", "action": "down", "key": "Backspace", "code": "Backspace", "text": ""}
    ))
    events = _key_events(live)
    assert [e["type"] for e in events] == ["keyDown"]  # no char for editing keys
    assert events[0]["windowsVirtualKeyCode"] == 8

    asyncio.run(prefill_sessions._dispatch_input(
        live, {"type": "key", "action": "down", "key": "ArrowLeft", "code": "ArrowLeft", "text": ""}
    ))
    assert _key_events(live)[-1]["windowsVirtualKeyCode"] == 37


# ------------------------------------------------------------ API: CRUD + isolation

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


def test_profile_answers_crud_round_trip():
    email = _unique_email("anscrud")
    _make_user(email)
    with _logged_in(email) as client:
        r = client.put(
            "/api/profile/answers",
            json={"items": [
                {"key": "sponsorship", "question": "Will you require sponsorship?", "answer": "No"},
                {"key": "country", "question": "Country", "answer": "United States"},
            ]},
        )
        assert r.status_code == 200, r.text
        got = client.get("/api/profile/answers").json()["answers"]
        assert {(a["key"], a["answer"]) for a in got} == {
            ("sponsorship", "No"), ("country", "United States"),
        }
        # Update one, delete the other via an empty answer.
        client.put(
            "/api/profile/answers",
            json={"items": [
                {"key": "sponsorship", "question": "Will you require sponsorship?", "answer": "Yes"},
                {"key": "country", "question": "Country", "answer": ""},
            ]},
        )
        got = client.get("/api/profile/answers").json()["answers"]
        assert [(a["key"], a["answer"]) for a in got] == [("sponsorship", "Yes")]
        # Explicit delete endpoint.
        r = client.delete("/api/profile/answers", params={"key": "sponsorship"})
        assert r.status_code == 200 and r.json()["answers"] == []
        assert client.delete(
            "/api/profile/answers", params={"key": "sponsorship"}
        ).status_code == 404


def test_profile_answers_never_store_codes():
    email = _unique_email("anscode")
    _make_user(email)
    with _logged_in(email) as client:
        client.put(
            "/api/profile/answers",
            json={"items": [
                {"key": "security code", "question": "Security code", "answer": "123456"},
                {"key": "password", "question": "Password", "answer": "hunter2"},
            ]},
        )
        assert client.get("/api/profile/answers").json()["answers"] == []


def test_profile_answers_per_user_isolation():
    email_a = _unique_email("ansisoa")
    email_b = _unique_email("ansisob")
    _make_user(email_a)
    _make_user(email_b)
    with _logged_in(email_a) as a:
        a.put(
            "/api/profile/answers",
            json={"items": [{"key": "sponsorship", "question": "Sponsorship?", "answer": "No"}]},
        )
    with _logged_in(email_b) as b:
        assert b.get("/api/profile/answers").json()["answers"] == []
        # B cannot delete A's answer, and the attempt changes nothing.
        assert b.delete(
            "/api/profile/answers", params={"key": "sponsorship"}
        ).status_code == 404
        b.put(
            "/api/profile/answers",
            json={"items": [{"key": "sponsorship", "question": "Sponsorship?", "answer": "Yes"}]},
        )
        assert [x["answer"] for x in b.get("/api/profile/answers").json()["answers"]] == ["Yes"]
    with _logged_in(email_a) as a:
        assert [x["answer"] for x in a.get("/api/profile/answers").json()["answers"]] == ["No"]


# ------------------------------------------------------------ streamed sessions

FORM_HTML = """<!doctype html>
<html><body>
<h1>Apply — Example Co</h1>
<form action="/submit" method="post">
  <div><label>Full name <input type="text" name="full_name"></label></div>
  <div><label>Email <input type="email" name="email"></label></div>
  <div><label>Phone <input type="tel" name="phone"></label></div>
  <div><label>Will you now or in the future require sponsorship?
    <select name="sponsorship" required>
      <option value="">Select</option><option>No</option><option>Yes</option>
    </select></label></div>
  <div><label>Country
    <select name="country" required>
      <option value="">Select</option><option>United States</option><option>Canada</option>
    </select></label></div>
  <button type="submit">Submit Application</button>
</form>
</body></html>"""


@pytest.fixture(scope="module")
def answers_form_server():
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


def _make_application(user_id: int, apply_url: str) -> int:
    run_id = f"pa{uuid.uuid4().hex[:10]}"
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


def _save_answer(user_id: int, key: str, question: str, answer: str) -> None:
    with SessionLocal() as db:
        db.add(ProfileAnswer(user_id=user_id, key=key, question=question, answer=answer))
        db.commit()


def test_streamed_session_fills_from_saved_answers(answers_form_server):
    email = _unique_email("ansfill")
    user_id = _make_user(email)
    _save_answer(user_id, "sponsorship", "Will you require sponsorship?", "No")
    _save_answer(user_id, "country", "Country", "United States")
    app_id = _make_application(user_id, answers_form_server)
    with _logged_in(email) as client:
        session = _create_session(client, app_id)
        assert any("sponsorship" in f for f in session["filled"]), session["filled"]
        assert "Country" in session["filled"], session["filled"]
        assert not any("sponsorship" in f for f in session["needs_attention"])
        assert client.post(f"/api/prefill-sessions/{session['id']}/close").status_code == 200


def test_streamed_session_without_saved_answer_flags_field(answers_form_server):
    email = _unique_email("ansnone")
    user_id = _make_user(email)
    app_id = _make_application(user_id, answers_form_server)
    with _logged_in(email) as client:
        session = _create_session(client, app_id)
        assert any("sponsorship" in f for f in session["needs_attention"]), session
        assert not any("sponsorship" in f for f in session["filled"])
        assert client.post(f"/api/prefill-sessions/{session['id']}/close").status_code == 200


def test_session_answer_candidate_and_save_endpoints(answers_form_server):
    email = _unique_email("anscap")
    user_id = _make_user(email)
    app_id = _make_application(user_id, answers_form_server)
    other_email = _unique_email("anscapother")
    _make_user(other_email)
    with _logged_in(email) as client:
        session = _create_session(client, app_id)
        # Everything with a value was filled by the agent; the selects are
        # empty -> no candidates yet.
        r = client.post(f"/api/prefill-sessions/{session['id']}/answer-candidates")
        assert r.status_code == 200, r.text
        assert r.json()["candidates"] == []
        # Saving a selected candidate lands in the profile answer bank.
        r = client.post(
            f"/api/prefill-sessions/{session['id']}/save-answers",
            json={"answers": [{"key": "country", "question": "Country", "answer": "United States"}]},
        )
        assert r.status_code == 200, r.text
        assert [a["key"] for a in r.json()["saved"]] == ["country"]
        got = client.get("/api/profile/answers").json()["answers"]
        assert [(a["key"], a["answer"]) for a in got] == [("country", "United States")]
        # Another user gets 404 on both endpoints for this session. A
        # non-entered client on purpose: entering a second TestClient would
        # run the app lifespan on a second event loop, whose shutdown tries
        # to close this still-live session from the wrong loop.
        other = TestClient(main.app)
        r = other.post("/api/auth/login", json={"email": other_email, "password": PASSWORD})
        assert r.status_code == 200, r.text
        assert other.post(
            f"/api/prefill-sessions/{session['id']}/answer-candidates"
        ).status_code == 404
        assert other.post(
            f"/api/prefill-sessions/{session['id']}/save-answers",
            json={"answers": [{"key": "country", "question": "Country", "answer": "Canada"}]},
        ).status_code == 404
        assert client.post(f"/api/prefill-sessions/{session['id']}/close").status_code == 200
