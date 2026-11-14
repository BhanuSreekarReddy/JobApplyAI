"""First/last name split on the profile: the split rule, the backfill of
profiles stored before the split existed, the /api/profile round-trip,
and the form filler's use of the stored split."""
import uuid

import pytest
from fastapi.testclient import TestClient

from app import auth, main
from app.agent.formfill import _values
from app.agent.profile import split_name
from app.db import SessionLocal, upgrade_schema
from app.models import Profile, User

PASSWORD = "TestPass123!"


@pytest.mark.parametrize(
    "full, expected",
    [
        ("Mary Jane Watson", ("Mary", "Jane Watson")),
        ("Ada Lovelace", ("Ada", "Lovelace")),
        ("Madonna", ("Madonna", "")),
        ("", ("", "")),
        ("  spaced   out  ", ("spaced", "out")),
    ],
)
def test_split_name(full, expected):
    assert split_name(full) == expected


def _seed_legacy_profile(user_id: int, name: str, first: str = "", last: str = "") -> None:
    with SessionLocal() as db:
        db.add(Profile(user_id=user_id, name=name, first_name=first, last_name=last))
        db.commit()


def _read_profile(user_id: int) -> Profile | None:
    with SessionLocal() as db:
        row = db.query(Profile).filter(Profile.user_id == user_id).first()
        if row is not None:
            db.expunge(row)
        return row


def _drop_profile(user_id: int) -> None:
    with SessionLocal() as db:
        db.query(Profile).filter(Profile.user_id == user_id).delete()
        db.commit()


def test_backfill_splits_legacy_profiles():
    base = 970_000_000 + uuid.uuid4().int % 1_000_000
    try:
        _seed_legacy_profile(base + 1, "Mary Jane Watson")
        _seed_legacy_profile(base + 2, "Madonna")
        _seed_legacy_profile(base + 3, "Already Split", first="Already", last="Split")

        upgrade_schema()

        row = _read_profile(base + 1)
        assert (row.first_name, row.last_name) == ("Mary", "Jane Watson")
        row = _read_profile(base + 2)
        assert (row.first_name, row.last_name) == ("Madonna", "")
        # A profile that already carries a split is left alone.
        row = _read_profile(base + 3)
        assert (row.first_name, row.last_name) == ("Already", "Split")
    finally:
        for offset in (1, 2, 3):
            _drop_profile(base + offset)


@pytest.fixture(autouse=True)
def _real_guard(_authenticated_guard):
    main.app.dependency_overrides.pop(auth.auth_guard, None)
    yield


def _make_user() -> tuple[int, str]:
    email = f"pnames-{uuid.uuid4().hex[:8]}@test.local"
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
        return user.id, email


def test_profile_update_round_trip():
    user_id, email = _make_user()
    try:
        with TestClient(main.app) as client:
            r = client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
            assert r.status_code == 200, r.text

            # Split fields in -> combined full name out, split preserved.
            r = client.put(
                "/api/profile",
                json={"first_name": "Ada", "last_name": "Lovelace"},
            )
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["first_name"] == "Ada"
            assert body["last_name"] == "Lovelace"
            assert body["name"] == "Ada Lovelace"

            got = client.get("/api/profile").json()
            assert got["first_name"] == "Ada"
            assert got["last_name"] == "Lovelace"
            assert got["name"] == "Ada Lovelace"

            # A caller sending only the full name gets the split derived.
            r = client.put("/api/profile", json={"name": "Grace Brewster Hopper"})
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["name"] == "Grace Brewster Hopper"
            assert body["first_name"] == "Grace"
            assert body["last_name"] == "Brewster Hopper"

            # A single-token name is all first name.
            r = client.put("/api/profile", json={"first_name": "Madonna", "last_name": ""})
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["name"] == "Madonna"
            assert body["first_name"] == "Madonna"
            assert body["last_name"] == ""
    finally:
        _drop_profile(user_id)
        with SessionLocal() as db:
            db.query(User).filter(User.id == user_id).delete()
            db.commit()


def test_values_prefers_the_stored_split():
    v = _values(
        {"first_name": "Ada", "last_name": "Lovelace Byron"},
        "London, UK",
        "Dear team",
        {},
    )
    assert v["first_name"] == "Ada"
    assert v["last_name"] == "Lovelace Byron"
    # No full name stored: the single-name field gets the recombined parts.
    assert v["full_name"] == "Ada Lovelace Byron"

    v = _values(
        {"name": "Ada Lovelace", "first_name": "Ada", "last_name": "Lovelace Byron"},
        "London, UK",
        "Dear team",
        {},
    )
    assert v["full_name"] == "Ada Lovelace"
    assert v["last_name"] == "Lovelace Byron"
