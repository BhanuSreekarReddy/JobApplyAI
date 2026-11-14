import os
import tempfile

# Must run before any `app` import: keeps tests away from the real data/ directory.
os.environ["JOBAPPLY_DATA_DIR"] = tempfile.mkdtemp(prefix="jobapply-test-")
os.environ["FORMFILL_HEADLESS"] = "true"
os.environ["ADMIN_EMAILS"] = "admin@test.local"
for key in ("OPENAI_API_KEY", "ADZUNA_APP_ID", "ADZUNA_APP_KEY", "USAJOBS_API_KEY", "SMTP_HOST"):
    os.environ[key] = ""

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi import Request  # noqa: E402

from app.agent import ats  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_cache():
    ats.clear_cache()
    yield
    ats.clear_cache()


@pytest.fixture(autouse=True)
def _authenticated_guard():
    """Existing tests exercise the API as one signed-in user. Override the
    global auth guard with a seeded admin (admin@test.local is in
    ADMIN_EMAILS), so they keep running without each test logging in.
    Auth-specific tests pop this override to exercise the real guard."""
    from app import auth, main
    from app.db import SessionLocal
    from app.models import User

    def fake_guard(request: Request):
        db = SessionLocal()
        try:
            user = db.query(User).filter(User.email == "admin@test.local").first()
            if user is None:
                user = User(
                    email="admin@test.local",
                    password_hash=auth.hash_password("TestPass123!"),
                    role="admin",
                    is_active=1,
                    email_verified=1,
                )
                db.add(user)
                db.commit()
                db.refresh(user)
            auth.claim_orphans(db, user)
            request.state.user = auth.snapshot(user)
        finally:
            db.close()

    main.app.dependency_overrides[auth.auth_guard] = fake_guard
    yield
    main.app.dependency_overrides.pop(auth.auth_guard, None)


@pytest.fixture
def mock_http(monkeypatch):
    """Route every httpx.AsyncClient through a handler: `mock_http(lambda request: httpx.Response(...))`."""
    real = httpx.AsyncClient

    def install(handler):
        transport = httpx.MockTransport(handler)

        def factory(*args, **kwargs):
            kwargs.pop("transport", None)
            kwargs.pop("limits", None)
            return real(*args, transport=transport, **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", factory)
        return transport

    return install
