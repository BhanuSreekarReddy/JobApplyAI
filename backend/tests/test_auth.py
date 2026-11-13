"""Accounts, invites, sessions, admin, and per-user isolation.

These tests exercise the REAL auth guard: the module-level autouse fixture
below removes the conftest override that signs other test modules in as a
seeded admin.
"""
import re
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import auth, main
from app.db import SessionLocal
from app.models import EmailOutbox, Invite, Run, User

SAMPLE = str(Path(__file__).resolve().parents[2] / "examples" / "sample-resume.txt")
PASSWORD = "TestPass123!"


@pytest.fixture(autouse=True)
def _real_guard(_authenticated_guard):
    main.app.dependency_overrides.pop(auth.auth_guard, None)
    yield


def _unique_email(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}@test.local"


def _add_invite(code: str) -> None:
    with SessionLocal() as db:
        db.add(Invite(code=code))
        db.commit()


def _make_user(email: str, role: str = "user", verified: int = 1, active: int = 1) -> int:
    with SessionLocal() as db:
        user = User(
            email=email,
            password_hash=auth.hash_password(PASSWORD),
            role=role,
            is_active=active,
            email_verified=verified,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        return user.id


def _client() -> TestClient:
    return TestClient(main.app)


def _logged_in(email: str, password: str = PASSWORD) -> TestClient:
    client = _client()
    r = client.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return client


def _outbox_body(email: str, kind: str) -> str:
    with SessionLocal() as db:
        row = (
            db.query(EmailOutbox)
            .filter(EmailOutbox.to_email == email, EmailOutbox.kind == kind)
            .order_by(EmailOutbox.id.desc())
            .first()
        )
        assert row is not None, f"no {kind} email in outbox for {email}"
        return row.body


def _outbox_token(email: str, kind: str) -> str:
    body = _outbox_body(email, kind)
    match = re.search(r"token=([A-Za-z0-9_-]+)", body)
    assert match, body
    return match.group(1)


def test_unauthenticated_requests_are_rejected():
    client = _client()
    assert client.get("/api/runs").status_code == 401
    assert client.get("/api/companies").status_code == 401
    assert client.get("/api/public/summary").status_code == 200
    assert client.get("/api/health").status_code == 200


def test_register_without_invite_is_rejected():
    client = _client()
    r = client.post(
        "/api/auth/register",
        json={"email": _unique_email("noinvite"), "password": PASSWORD, "invite_code": "bogus-code"},
    )
    assert r.status_code == 400
    r = client.post(
        "/api/auth/register",
        json={"email": _unique_email("noinvite"), "password": "short", "invite_code": "bogus-code"},
    )
    assert r.status_code == 400  # weak password is rejected too


def test_register_with_invite_cannot_reuse_it():
    code = f"INV-{uuid.uuid4().hex[:10]}"
    _add_invite(code)
    email = _unique_email("invited")
    client = _client()
    r = client.post("/api/auth/register", json={"email": email, "password": PASSWORD, "invite_code": code})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["role"] == "user" and body["email_verified"] is False
    assert body["verification_required"] is True
    # Registration does NOT sign the new user in: the email address must be
    # verified first, so there is no session and sign-in is refused.
    assert client.get("/api/auth/me").status_code == 401
    r = client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 403
    assert "Verify your email" in r.json()["detail"]

    other = _client()
    r = other.post(
        "/api/auth/register",
        json={"email": _unique_email("second"), "password": PASSWORD, "invite_code": code},
    )
    assert r.status_code == 400


def test_seeded_admin_email_registers_as_admin():
    # Another fixture may already have created this account; start clean.
    with SessionLocal() as db:
        db.query(User).filter(User.email == "admin@test.local").delete()
        db.commit()
    code = f"INV-{uuid.uuid4().hex[:10]}"
    _add_invite(code)
    client = _client()
    r = client.post(
        "/api/auth/register",
        json={"email": "admin@test.local", "password": PASSWORD, "invite_code": code},
    )
    assert r.status_code == 200, r.text
    assert r.json()["role"] == "admin"
    assert r.json()["seeded_admin"] is True


def test_login_wrong_password_is_401():
    email = _unique_email("login")
    _make_user(email)
    client = _client()
    r = client.post("/api/auth/login", json={"email": email, "password": "WrongPass123!"})
    assert r.status_code == 401
    r = client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200


def test_email_verification_flow_and_run_gate(monkeypatch):
    code = f"INV-{uuid.uuid4().hex[:10]}"
    _add_invite(code)
    email = _unique_email("verify")
    client = _client()
    r = client.post("/api/auth/register", json={"email": email, "password": PASSWORD, "invite_code": code})
    assert r.status_code == 200

    # Unverified accounts cannot sign in, so they cannot start a run either.
    r = client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 403
    with open(SAMPLE, "rb") as fh:
        r = client.post("/api/runs", files={"resume": ("resume.txt", fh, "text/plain")},
                        data={"locations": "San Francisco, CA"})
    assert r.status_code == 401

    token = _outbox_token(email, "verify")
    # The emailed link is absolute, built from the default public base URL.
    assert "http://localhost:5173/verify-email?token=" in _outbox_body(email, "verify")
    r = client.post("/api/auth/verify-email", json={"token": token})
    assert r.status_code == 200
    # Re-submitting the same link is idempotent once verified (mail
    # scanners pre-fetch links; pages double-submit), but a bogus token
    # is still rejected.
    r = client.post("/api/auth/verify-email", json={"token": token})
    assert r.status_code == 200 and r.json()["already_verified"] is True
    assert client.post("/api/auth/verify-email", json={"token": "nope"}).status_code == 400

    # Verified now: sign-in works and run creation is accepted
    # (background execution stubbed).
    r = client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    monkeypatch.setattr(main, "_run_in_background", lambda run_id: None)
    with open(SAMPLE, "rb") as fh:
        r = client.post("/api/runs", files={"resume": ("resume.txt", fh, "text/plain")},
                        data={"locations": "San Francisco, CA"})
    assert r.status_code == 200, r.text
    run_id = r.json()["id"]
    with SessionLocal() as db:
        run = db.get(Run, run_id)
        assert run is not None and run.user_id is not None
    # Creating the run seeded the persistent profile from the resume.
    profile = client.get("/api/profile").json()
    assert profile["name"] == "Priya Nair"
    assert profile["contact_email"] == "priya.nair@example.com"


def _verify_outbox_count(email: str) -> int:
    with SessionLocal() as db:
        return (
            db.query(EmailOutbox)
            .filter(EmailOutbox.to_email == email, EmailOutbox.kind == "verify")
            .count()
        )


def _backdate_verify_emails(email: str, minutes: int) -> None:
    with SessionLocal() as db:
        rows = (
            db.query(EmailOutbox)
            .filter(EmailOutbox.to_email == email, EmailOutbox.kind == "verify")
            .all()
        )
        for row in rows:
            row.created_at = datetime.utcnow() - timedelta(minutes=minutes)
        db.commit()


def test_resend_verification_flow():
    code = f"INV-{uuid.uuid4().hex[:10]}"
    _add_invite(code)
    email = _unique_email("resend")
    client = _client()
    r = client.post("/api/auth/register", json={"email": email, "password": PASSWORD, "invite_code": code})
    assert r.status_code == 200
    assert _verify_outbox_count(email) == 1  # the registration email

    # Inside the cooldown the resend is a no-op (but still answers ok).
    r = client.post("/api/auth/resend-verification", json={"email": email})
    assert r.status_code == 200
    assert _verify_outbox_count(email) == 1

    # Past the cooldown a fresh link is sent, and it verifies the account.
    _backdate_verify_emails(email, 5)
    r = client.post("/api/auth/resend-verification", json={"email": email})
    assert r.status_code == 200
    assert _verify_outbox_count(email) == 2
    token = _outbox_token(email, "verify")  # newest outbox row
    r = client.post("/api/auth/verify-email", json={"token": token})
    assert r.status_code == 200

    # Already verified: resend sends nothing.
    _backdate_verify_emails(email, 5)
    r = client.post("/api/auth/resend-verification", json={"email": email})
    assert r.status_code == 200
    assert _verify_outbox_count(email) == 2

    # Unknown accounts get the same response and no email (no enumeration).
    ghost = _unique_email("ghostresend")
    r = client.post("/api/auth/resend-verification", json={"email": ghost})
    assert r.status_code == 200
    assert _verify_outbox_count(ghost) == 0


def test_resend_verification_daily_cap():
    code = f"INV-{uuid.uuid4().hex[:10]}"
    _add_invite(code)
    email = _unique_email("resendcap")
    client = _client()
    r = client.post("/api/auth/register", json={"email": email, "password": PASSWORD, "invite_code": code})
    assert r.status_code == 200
    # Simulate a day of resends up to the cap: 5 verify emails in 24h.
    with SessionLocal() as db:
        rows = (
            db.query(EmailOutbox)
            .filter(EmailOutbox.to_email == email, EmailOutbox.kind == "verify")
            .all()
        )
        for row in rows:
            row.created_at = datetime.utcnow() - timedelta(hours=2)
        for _ in range(auth.RESEND_DAILY_CAP - 1):
            db.add(
                EmailOutbox(
                    to_email=email,
                    subject="Verify your JobApply Agent account",
                    body="",
                    kind="verify",
                    sent=0,
                    created_at=datetime.utcnow() - timedelta(hours=2),
                )
            )
        db.commit()
    assert _verify_outbox_count(email) == auth.RESEND_DAILY_CAP
    r = client.post("/api/auth/resend-verification", json={"email": email})
    assert r.status_code == 200
    assert _verify_outbox_count(email) == auth.RESEND_DAILY_CAP  # capped


def test_verification_email_uses_configured_public_base_url(monkeypatch):
    # A trailing slash in the configured value must not produce "//".
    monkeypatch.setattr(auth.settings, "public_base_url", "https://example.com/")
    code = f"INV-{uuid.uuid4().hex[:10]}"
    _add_invite(code)
    email = _unique_email("baseurl")
    client = _client()
    r = client.post("/api/auth/register", json={"email": email, "password": PASSWORD, "invite_code": code})
    assert r.status_code == 200, r.text
    body = _outbox_body(email, "verify")
    assert "https://example.com/verify-email?token=" in body
    assert "example.com//verify-email" not in body


def test_password_reset_flow_uses_outbox():
    email = _unique_email("reset")
    _make_user(email)
    client = _client()
    r = client.post("/api/auth/request-password-reset", json={"email": email})
    assert r.status_code == 200
    # Unknown accounts get the same response (no enumeration).
    r = client.post("/api/auth/request-password-reset", json={"email": _unique_email("ghost")})
    assert r.status_code == 200

    token = _outbox_token(email, "reset")
    # The emailed link is absolute, built from the default public base URL.
    assert "http://localhost:5173/reset-password?token=" in _outbox_body(email, "reset")
    r = client.post("/api/auth/reset-password", json={"token": token, "password": "NewPass456!"})
    assert r.status_code == 200
    # Single use.
    r = client.post("/api/auth/reset-password", json={"token": token, "password": "NewPass456!"})
    assert r.status_code == 400
    # Old password fails, new one works.
    assert _client().post("/api/auth/login", json={"email": email, "password": PASSWORD}).status_code == 401
    assert _client().post("/api/auth/login", json={"email": email, "password": "NewPass456!"}).status_code == 200


def test_profile_round_trip():
    email = _unique_email("profile")
    _make_user(email)
    client = _logged_in(email)
    r = client.put(
        "/api/profile",
        json={
            "name": "Sam Reyes",
            "contact_email": "sam@example.com",
            "phone": "+1 555 0100",
            "linkedin": "linkedin.com/in/samreyes",
            "github": "github.com/samreyes",
            "titles": ["staff engineer"],
            "skills": ["python", "go"],
            "years_experience": 9,
            "summary": "Builds things.",
            "locations": "Austin, TX, Remote",
        },
    )
    assert r.status_code == 200, r.text
    got = client.get("/api/profile").json()
    assert got["name"] == "Sam Reyes"
    assert got["skills"] == ["python", "go"]
    assert got["years_experience"] == 9
    assert got["locations"] == "Austin, TX, Remote"


def test_cross_user_run_is_404():
    email_a, email_b = _unique_email("owner"), _unique_email("other")
    a_id = _make_user(email_a)
    _make_user(email_b)
    with SessionLocal() as db:
        db.add(Run(id="ownedrun0001", user_id=a_id, status="completed", locations="SF"))
        db.commit()
    client_b = _logged_in(email_b)
    assert client_b.get("/api/runs/ownedrun0001").status_code == 404
    assert client_b.get("/api/runs").json() == []
    client_a = _logged_in(email_a)
    assert client_a.get("/api/runs/ownedrun0001").status_code == 200


def test_admin_user_management():
    admin_email = _unique_email("boss")
    _make_user(admin_email, role="admin")
    victim_email = _unique_email("victim")
    victim_id = _make_user(victim_email)
    with SessionLocal() as db:
        db.add(Run(id="victimrun001", user_id=victim_id, status="completed", locations="SF"))
        db.commit()

    # A regular user is forbidden from the admin API.
    user_client = _logged_in(victim_email)
    assert user_client.get("/api/admin/users").status_code == 403
    assert user_client.post("/api/admin/invites", json={}).status_code == 403

    admin = _logged_in(admin_email)
    users = admin.get("/api/admin/users").json()
    victim = next(u for u in users if u["id"] == victim_id)
    assert victim["run_count"] == 1 and victim["application_count"] == 0

    # Admins can mint invites.
    r = admin.post("/api/admin/invites", json={"expires_days": 7})
    assert r.status_code == 200 and r.json()["code"]

    # Deactivation blocks sign-in; activation restores it.
    assert admin.post(f"/api/admin/users/{victim_id}/deactivate").status_code == 200
    r = _client().post("/api/auth/login", json={"email": victim_email, "password": PASSWORD})
    assert r.status_code == 403
    assert admin.post(f"/api/admin/users/{victim_id}/activate").status_code == 200
    assert _client().post("/api/auth/login", json={"email": victim_email, "password": PASSWORD}).status_code == 200

    # Promote/demote work for non-seeded users.
    assert admin.post(f"/api/admin/users/{victim_id}/promote").json()["role"] == "admin"
    assert admin.post(f"/api/admin/users/{victim_id}/demote").json()["role"] == "user"

    # Deletion removes the user and their data.
    assert admin.delete(f"/api/admin/users/{victim_id}").status_code == 200
    with SessionLocal() as db:
        assert db.get(User, victim_id) is None
        assert db.get(Run, "victimrun001") is None


def test_seeded_admin_cannot_be_deactivated_or_deleted():
    admin_email = _unique_email("boss2")
    _make_user(admin_email, role="admin")
    admin = _logged_in(admin_email)
    users = admin.get("/api/admin/users").json()
    seeded = next((u for u in users if u["email"] == "admin@test.local"), None)
    if seeded is None:
        pytest.skip("seeded admin account not present in this test database")
    assert admin.post(f"/api/admin/users/{seeded['id']}/deactivate").status_code == 403
    assert admin.delete(f"/api/admin/users/{seeded['id']}").status_code == 403
    assert admin.post(f"/api/admin/users/{seeded['id']}/demote").status_code == 403
