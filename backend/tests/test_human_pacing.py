"""Human-like streamed sessions: a generic browser identity per session
and human-paced filling.

Unit coverage: the session context options (client-reported locale /
timezone pass through when valid and fall back to the documented
defaults when not), the user-agent normalization, the pacing helper's
delay bounds (jittered, budget-compressed for long values, zero at
scale 0, legacy delay when the kill switch is off), and the fill
order (visual, top-to-bottom).

Browser coverage: one real session created with client-reported
identity values — the page must report that timezone, no webdriver
flag, and a user agent without the headless marker — whose form is
filled by the paced typing path, including one field a click cannot
reach (covered by an overlay), which must still land via the instant
fallback.

The suite-wide FORMFILL_PACING_SCALE=0 (conftest) keeps the paced path
fast here: the interaction pattern runs, the waits do not.
"""
import asyncio
import contextlib
import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

from app import auth, main, prefill_sessions
from app.agent import formfill, pacing
from app.db import SessionLocal
from app.models import Application, Run, User

PASSWORD = "TestPass123!"


@pytest.fixture(autouse=True)
def _reset_pacing_scale():
    yield
    pacing.set_scale(None)


# ------------------------------------------------------- session identity


def test_context_options_defaults():
    opts = prefill_sessions._session_context_options()
    assert opts["locale"] == prefill_sessions.DEFAULT_LOCALE == "en-US"
    assert opts["timezone_id"] == prefill_sessions.DEFAULT_TIMEZONE == "America/New_York"
    assert opts["color_scheme"] == "light"
    assert opts["extra_http_headers"] == {"Accept-Language": "en-US"}
    assert "--disable-blink-features=AutomationControlled" in opts["args"]
    screen = (opts["screen"]["width"], opts["screen"]["height"])
    assert screen in prefill_sessions._SCREEN_SIZES
    assert opts["viewport"]["width"] == screen[0]
    assert opts["viewport"]["height"] == screen[1] - prefill_sessions._BROWSER_CHROME_PX


def test_context_options_use_client_values():
    opts = prefill_sessions._session_context_options(
        "America/Chicago", "fr-FR", ["fr-FR", "fr", "en"]
    )
    assert opts["timezone_id"] == "America/Chicago"
    assert opts["locale"] == "fr-FR"
    assert opts["extra_http_headers"]["Accept-Language"] == "fr-FR,fr;q=0.9,en;q=0.8"


@pytest.mark.parametrize("bad", ["Not/AZone", "", "   ", None, 42, "x" * 100, "America"])
def test_context_options_reject_bad_timezones(bad):
    opts = prefill_sessions._session_context_options(bad)
    assert opts["timezone_id"] == prefill_sessions.DEFAULT_TIMEZONE


@pytest.mark.parametrize("bad", ["en_US", "english!", "", None, "e", "en-", "a b"])
def test_context_options_reject_bad_languages(bad):
    opts = prefill_sessions._session_context_options(None, bad, [bad])
    assert opts["locale"] == prefill_sessions.DEFAULT_LOCALE


def test_context_options_language_list_skips_junk_and_dedupes():
    opts = prefill_sessions._session_context_options(
        None, None, ["es-419", "junk tag!", "es-419", "es"]
    )
    assert opts["locale"] == "es-419"
    assert opts["extra_http_headers"]["Accept-Language"] == "es-419,es;q=0.9"


def test_context_options_primary_language_wins_over_list():
    opts = prefill_sessions._session_context_options(None, "de-DE", ["en-US"])
    assert opts["locale"] == "de-DE"
    assert opts["extra_http_headers"]["Accept-Language"] == "de-DE,en-US;q=0.9"


def test_normalize_user_agent_strips_only_the_headless_marker():
    ua = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 HeadlessChrome/156.0.8078.4 Safari/537.36"
    normalized = prefill_sessions._normalize_user_agent(ua)
    assert "HeadlessChrome" not in normalized
    assert "Chrome/156.0.8078.4" in normalized  # engine/version untouched
    plain = "Mozilla/5.0 Chrome/156.0.8078.4 Safari/537.36"
    assert prefill_sessions._normalize_user_agent(plain) == plain


# ------------------------------------------------------------- pacing math


def test_keystroke_delays_are_jittered_within_bounds():
    pacing.set_scale(1.0)
    samples = [pacing.keystroke_delay_ms(12) for _ in range(300)]
    assert all(30.0 <= s <= 90.0 for s in samples)
    assert max(samples) - min(samples) > 20.0  # genuinely variable, not a constant


def test_keystroke_delays_compress_for_long_values():
    pacing.set_scale(1.0)
    # 2000 chars: the per-char budget is 2 ms, below the floor, so every
    # keystroke sits at the floor and the field stays within budget.
    assert all(
        s == pacing.MIN_KEY_DELAY_MS for s in (pacing.keystroke_delay_ms(2000) for _ in range(50))
    )
    # 100 chars: budget is 40 ms/char, so the 30–90 jitter compresses to 30–40.
    samples = [pacing.keystroke_delay_ms(100) for _ in range(200)]
    assert all(30.0 <= s <= 40.0 for s in samples)


def test_field_pause_bounds():
    pacing.set_scale(1.0)
    samples = [pacing.field_pause_ms() for _ in range(200)]
    assert all(300.0 <= s <= 1500.0 for s in samples)


def test_zero_scale_zeroes_every_delay():
    pacing.set_scale(0.0)
    assert pacing.keystroke_delay_ms(10) == 0.0
    assert pacing.field_pause_ms() == 0.0
    assert pacing.press_delay_ms(10) == 0.0
    asyncio.run(pacing.pause_between_fields())  # returns at once


def test_press_delay_follows_pacing_when_enabled():
    pacing.set_scale(1.0)
    assert pacing.enabled()
    samples = [pacing.press_delay_ms(10) for _ in range(100)]
    assert all(30.0 <= s <= 90.0 for s in samples)


def test_kill_switch_restores_legacy_press_delay(monkeypatch):
    pacing.set_scale(1.0)
    monkeypatch.setattr(pacing.settings, "formfill_human_pacing", False)
    assert not pacing.enabled()
    assert pacing.press_delay_ms(10) == pacing.LEGACY_PRESS_DELAY_MS == 40.0


# ------------------------------------------------------------ visual order


def _field(fid, frame_seq, pos):
    return {"id": fid, "_frame_seq": frame_seq, "pos": pos}


def test_visual_order_is_top_to_bottom_left_to_right_per_frame():
    fields = [
        _field(1, 0, [300, 10]),  # lower on the page
        _field(2, 0, [100, 200]),  # top row, right
        _field(3, 0, [100, 10]),  # top row, left
        _field(4, 1, [50, 10]),  # second frame: always after frame 0
        _field(5, 0, None),  # unpositioned: end of its frame, scan order kept
        _field(6, 0, None),
    ]
    ordered = [f["id"] for f in formfill._visual_order(fields)]
    assert ordered == [3, 2, 1, 5, 6, 4]


# ------------------------------------------------------- real session (E2E)

FORM_HTML = """<!doctype html>
<html><body>
<h1>Apply — Example Co</h1>
<form action="/submit" method="post">
  <div><label>Full name <input type="text" name="full_name"></label></div>
  <div><label>Email <input type="email" name="email"></label></div>
  <div style="position:relative">
    <label>Confirm email <input type="email" name="confirm_email"></label>
    <div style="position:absolute; inset:0"></div>
  </div>
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
    run_id = f"hp{uuid.uuid4().hex[:10]}"
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


async def _eval_on_page(page, expression):
    return await page.evaluate(expression)


def _page_eval(client: TestClient, session_id: str, expression: str):
    live = prefill_sessions._live[session_id]
    return client.portal.call(_eval_on_page, live.page, expression)


def test_session_presents_client_identity_and_fills_at_a_human_pace(form_server):
    email = f"hp-{uuid.uuid4().hex[:8]}@test.local"
    user_id = _make_user(email)
    app_id = _make_application(user_id, form_server)
    with _logged_in(email) as client:
        r = client.post(
            "/api/prefill-sessions",
            json={
                "application_id": app_id,
                "client_timezone": "America/Chicago",
                "client_language": "en-US",
                "client_languages": ["en-US", "en"],
            },
        )
        if r.status_code == 503:
            pytest.skip(f"Chromium/Playwright unavailable in this environment: {r.json().get('detail')}")
        assert r.status_code == 200, r.text
        session = r.json()
        assert session["status"] == "ready"
        filled = session["filled"]
        assert "Full name" in filled, filled
        assert "Email" in filled, filled
        assert "Phone" in filled, filled
        # The overlay-covered field cannot be clicked, so the typing
        # path demonstrably fails on it — it must still be filled, via
        # the instant fallback.
        assert "Confirm email" in filled, filled

        info = _page_eval(
            client,
            session["id"],
            """() => ({
              tz: Intl.DateTimeFormat().resolvedOptions().timeZone,
              webdriver: navigator.webdriver,
              lang: navigator.language,
              ua: navigator.userAgent,
              name: document.querySelector('[name=full_name]').value,
              confirm: document.querySelector('[name=confirm_email]').value,
              cover: document.querySelector('[name=cover_letter]').value,
            })""",
        )
        assert info["tz"] == "America/Chicago"
        assert info["webdriver"] is False
        assert info["lang"] == "en-US"
        assert "HeadlessChrome" not in info["ua"]
        assert info["name"] == "Priya Nair"
        assert info["confirm"] == "priya.nair@example.com"
        assert info["cover"] == "Dear Example Co team, I am excited to apply."
        assert client.post(f"/api/prefill-sessions/{session['id']}/close").status_code == 200
