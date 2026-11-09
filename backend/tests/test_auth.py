"""Accounts, invites, sessions, admin, and per-user isolation.

These tests exercise the REAL auth guard: the module-level autouse fixture
below removes the conftest override that signs other test modules in as a
seeded admin.
"""
import re
import uuid
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


def _outbox_token(email: str, kind: str) -> str:
    with SessionLocal() as db:
        row = (
            db.query(EmailOutbox)
            .filter(EmailOutbox.to_email == email, EmailOutbox.kind == kind)
            .order_by(EmailOutbox.id.desc())
            .first()
        )
        assert row is not None, f"no {kind} email in outbox for {email}"
        match = re.search(r"token=([A-Za-z0-9_-]+)", row.body)
        assert match, row.body
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
    # The registration also signed the new user in.
    assert client.get("/api/auth/me").json()["email"] == email

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

    # Unverified accounts cannot start a run.
    with open(SAMPLE, "rb") as fh:
        r = client.post("/api/runs", files={"resume": ("resume.txt", fh, "text/plain")},
                        data={"locations": "San Francisco, CA"})
    assert r.status_code == 403

    token = _outbox_token(email, "verify")
    r = client.post("/api/auth/verify-email", json={"token": token})
    assert r.status_code == 200
    # The same token cannot be used twice.
    assert client.post("/api/auth/verify-email", json={"token": token}).status_code == 400

    # Verified now: run creation is accepted (background execution stubbed).
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
