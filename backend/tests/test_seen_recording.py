import pytest
from fastapi.testclient import TestClient

from app import main
from app.db import SessionLocal
from app.models import Application, JobMatch, Run, SeenJob


@pytest.fixture
def client():
    return TestClient(main.app)


@pytest.fixture
def seeded():
    """A run with one job and one application waiting for review."""
    created = []

    def make(run_id: str, *, browser_fill: int) -> Application:
        db = SessionLocal()
        try:
            db.add(
                Run(
                    id=run_id,
                    status="completed",
                    locations="San Francisco, CA",
                    auto_apply=0,
                    browser_fill=browser_fill,
                    resume_path="",
                    profile_json='{"name": "Pat", "email": "pat@example.com"}',
                )
            )
            job = JobMatch(
                run_id=run_id,
                external_id="seen-ind-1",
                title="Dedupe Engineer",
                company="Seen Industries",
                location="San Francisco, CA",
                description="Build things.",
                url="https://example.com/jobs/1",
                apply_url="https://example.com/apply/1",
            )
            db.add(job)
            db.flush()
            app = Application(
                run_id=run_id,
                job_id=job.id,
                title=job.title,
                company=job.company,
                status="pending_review",
                channel="review",
                cover_letter="Original letter",
                notes="Waiting for review. Approve to send it, or skip it.",
                apply_url=job.apply_url,
            )
            db.add(app)
            db.commit()
            db.refresh(app)
            created.append(run_id)
            return app
        finally:
            db.close()

    yield make

    db = SessionLocal()
    try:
        for run_id in created:
            db.query(SeenJob).filter(SeenJob.run_id == run_id).delete()
            db.query(Application).filter(Application.run_id == run_id).delete()
            db.query(JobMatch).filter(JobMatch.run_id == run_id).delete()
            db.query(Run).filter(Run.id == run_id).delete()
        db.commit()
    finally:
        db.close()


def _seen_rows(run_id: str):
    db = SessionLocal()
    try:
        return db.query(SeenJob).filter(SeenJob.run_id == run_id).all()
    finally:
        db.close()


def test_approve_prepared_records_seen(client, seeded):
    app = seeded("seen-approve-run", browser_fill=0)
    res = client.post(f"/api/applications/{app.id}/approve")
    assert res.status_code == 200
    assert res.json()["status"] == "prepared"

    rows = _seen_rows("seen-approve-run")
    assert len(rows) == 1
    assert rows[0].status == "prepared"
    assert rows[0].external_id == "seen-ind-1"
    assert rows[0].title == "Dedupe Engineer"
    assert rows[0].company == "Seen Industries"

    listed = client.get("/api/seen").json()
    mine = [r for r in listed if r["run_id"] == "seen-approve-run"]
    assert len(mine) == 1
    assert mine[0]["status"] == "prepared"


def test_approve_with_browser_fill_records_filled(client, seeded, monkeypatch):
    async def fake_fill(**kwargs):
        return {"status": "filled", "channel": "browser", "notes": "Form pre-filled."}

    monkeypatch.setattr(main, "fill_application", fake_fill)
    app = seeded("seen-fill-run", browser_fill=1)
    res = client.post(f"/api/applications/{app.id}/approve")
    assert res.status_code == 200
    assert res.json()["status"] == "filled"

    rows = _seen_rows("seen-fill-run")
    assert len(rows) == 1
    assert rows[0].status == "filled"


def test_standalone_fill_records_seen(client, seeded, monkeypatch):
    async def fake_fill(**kwargs):
        return {"status": "filled", "channel": "browser", "notes": "Form pre-filled."}

    monkeypatch.setattr(main, "fill_application", fake_fill)
    app = seeded("seen-standalone-fill-run", browser_fill=0)
    res = client.post(f"/api/applications/{app.id}/fill")
    assert res.status_code == 200
    assert res.json()["status"] == "filled"

    rows = _seen_rows("seen-standalone-fill-run")
    assert len(rows) == 1
    assert rows[0].status == "filled"


def test_skip_records(client, seeded):
    app = seeded("seen-skip-run", browser_fill=0)
    res = client.post(f"/api/applications/{app.id}/skip")
    assert res.status_code == 200
    assert res.json()["status"] == "skipped"

    rows = _seen_rows("seen-skip-run")
    assert len(rows) == 1
    assert rows[0].status == "skipped"


def test_failed_approval_records_nothing(client, seeded, monkeypatch):
    def fake_apply(**kwargs):
        return {"status": "failed", "channel": "email", "notes": "SMTP unavailable."}

    monkeypatch.setattr(main, "apply_to_job", fake_apply)
    app = seeded("seen-failed-run", browser_fill=0)
    res = client.post(f"/api/applications/{app.id}/approve")
    assert res.status_code == 200
    assert res.json()["status"] == "failed"

    assert _seen_rows("seen-failed-run") == []
