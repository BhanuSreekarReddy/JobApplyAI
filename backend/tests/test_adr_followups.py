"""Regression tests for the follow-up improvements ADR (PR #11):
Decision 2 (environment-configured CORS origins) and Decision 4
(seen row recorded on the add-from-link path)."""
import json

import pytest
from fastapi.testclient import TestClient

from app import main
from app.config import Settings, cors_origin_list, settings
from app.db import SessionLocal
from app.models import Application, JobMatch, Run, SeenJob, User


# ---------------------------------------------------------------------------
# Decision 2: CORS origins come from the CORS_ORIGINS setting.
# ---------------------------------------------------------------------------


def test_cors_setting_defaults_to_localhost():
    assert Settings.model_fields["cors_origins"].default == (
        "http://localhost:5173,http://127.0.0.1:5173"
    )


def test_cors_origin_list_parses_configured_value(monkeypatch):
    monkeypatch.setattr(
        settings, "cors_origins", " https://jobapplylive.com ,https://staging.example.com ,,"
    )
    assert cors_origin_list() == ["https://jobapplylive.com", "https://staging.example.com"]


def test_cors_headers_use_the_default_allowlist():
    client = TestClient(main.app)
    allowed = client.get("/api/health", headers={"Origin": "http://localhost:5173"})
    assert allowed.headers.get("access-control-allow-origin") == "http://localhost:5173"
    blocked = client.get("/api/health", headers={"Origin": "https://unconfigured.example"})
    assert "access-control-allow-origin" not in blocked.headers


# ---------------------------------------------------------------------------
# Decision 4: POST /api/jobs/from-link records its seen row, like every
# other application-outcome path.
# ---------------------------------------------------------------------------

LINK_RUN_ID = "adr11-link-run"


@pytest.fixture
def client():
    return TestClient(main.app)


@pytest.fixture
def link_run(client, monkeypatch):
    """A profile run owned by the test-guard admin, with the job-link
    resolver stubbed. The endpoint builds a real httpx client before
    calling the resolver, and this environment's proxy variables make
    that construction fail, so they are cleared for the test."""
    for var in (
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
        "http_proxy", "https_proxy", "all_proxy", "no_proxy",
    ):
        monkeypatch.delenv(var, raising=False)
    client.get("/api/health")  # let the auth guard seed the admin user first
    db = SessionLocal()
    try:
        admin = db.query(User).filter(User.email == "admin@test.local").first()
        db.add(
            Run(
                id=LINK_RUN_ID,
                user_id=admin.id,
                status="completed",
                locations="Newark, CA",
                resume_path="",
                profile_json=json.dumps({"name": "Priya Nair", "email": "priya@example.com"}),
            )
        )
        db.commit()
    finally:
        db.close()

    async def fake_job(_client, url, use_browser=True):
        return {
            "external_id": "greenhouse-acme-11",
            "title": "Staff Engineer",
            "company": "Acme",
            "location": "Newark, CA",
            "source": "Greenhouse",
            "apply_url": "https://boards.example.com/acme/11",
            "description": "Python",
            "remote": False,
        }

    async def fake_fill(**_kwargs):
        return {"status": "filled", "channel": "browser", "notes": "Filled 5 field(s)"}

    monkeypatch.setattr(main, "resolve_job_url", fake_job)
    monkeypatch.setattr(main, "fill_application", fake_fill)
    yield LINK_RUN_ID

    db = SessionLocal()
    try:
        db.query(SeenJob).filter(SeenJob.run_id == LINK_RUN_ID).delete()
        db.query(Application).filter(Application.run_id == LINK_RUN_ID).delete()
        db.query(JobMatch).filter(JobMatch.run_id == LINK_RUN_ID).delete()
        db.query(Run).filter(Run.id == LINK_RUN_ID).delete()
        db.commit()
    finally:
        db.close()


def _link_seen_rows():
    db = SessionLocal()
    try:
        return db.query(SeenJob).filter(SeenJob.run_id == LINK_RUN_ID).all()
    finally:
        db.close()


def test_from_link_filled_records_seen(client, link_run):
    r = client.post("/api/jobs/from-link", json={"url": "https://acme.com/jobs/11", "run_id": link_run})
    assert r.status_code == 200, r.text
    assert r.json()["application"]["status"] == "filled"
    rows = _link_seen_rows()
    assert len(rows) == 1
    assert rows[0].status == "filled"
    assert (rows[0].title, rows[0].company) == ("Staff Engineer", "Acme")
    # ...and it is what the tracker's 'Previously applied' section reads.
    listed = client.get("/api/seen").json()
    assert any(row["title"] == "Staff Engineer" and row["status"] == "filled" for row in listed)
    # A fill is not a submission: the tracker stage stays at not_applied.
    assert r.json()["application"]["pipeline_stage"] == "not_applied"


def test_from_link_prepared_records_seen(client, link_run):
    r = client.post(
        "/api/jobs/from-link",
        json={"url": "https://acme.com/jobs/11", "run_id": link_run, "fill": False},
    )
    assert r.status_code == 200, r.text
    assert r.json()["application"]["status"] == "prepared"
    rows = _link_seen_rows()
    assert len(rows) == 1 and rows[0].status == "prepared"


def test_from_link_failed_records_nothing(client, link_run, monkeypatch):
    async def failing_fill(**_kwargs):
        return {"status": "failed", "channel": "browser", "notes": "Could not open the form."}

    monkeypatch.setattr(main, "fill_application", failing_fill)
    r = client.post("/api/jobs/from-link", json={"url": "https://acme.com/jobs/11", "run_id": link_run})
    assert r.status_code == 200, r.text
    assert r.json()["application"]["status"] == "failed"
    assert _link_seen_rows() == []
