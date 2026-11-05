import pytest
from fastapi.testclient import TestClient

from app import main
from app.db import SessionLocal
from app.models import Application, JobMatch, Run


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
                title="Senior Engineer",
                company="Acme",
                location="San Francisco, CA",
                description="Build things.",
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
            db.query(Application).filter(Application.run_id == run_id).delete()
            db.query(JobMatch).filter(JobMatch.run_id == run_id).delete()
            db.query(Run).filter(Run.id == run_id).delete()
        db.commit()
    finally:
        db.close()


def test_review_lists_only_pending(client, seeded):
    app = seeded("review-list-run", browser_fill=0)
    rows = client.get("/api/review").json()
    mine = [r for r in rows if r["id"] == app.id]
    assert len(mine) == 1
    assert mine[0]["status"] == "pending_review"
    assert mine[0]["job_id"] == app.job_id


def test_patch_edits_cover_letter_and_still_updates_status(client, seeded):
    app = seeded("review-patch-run", browser_fill=0)
    res = client.patch(f"/api/applications/{app.id}", json={"cover_letter": "Edited letter"})
    assert res.status_code == 200
    assert res.json()["cover_letter"] == "Edited letter"
    assert res.json()["status"] == "pending_review"

    res = client.patch(f"/api/applications/{app.id}", json={"status": "prepared"})
    assert res.status_code == 200
    assert res.json()["status"] == "prepared"


def test_approve_without_browser_fill_prepares_packet_and_never_fills(client, seeded, monkeypatch):
    calls = []

    async def fake_fill(**kwargs):
        calls.append(kwargs)
        return {"status": "filled", "channel": "browser", "notes": "Filled."}

    monkeypatch.setattr(main, "fill_application", fake_fill)
    app = seeded("review-plain-run", browser_fill=0)
    res = client.post(f"/api/applications/{app.id}/approve")
    assert res.status_code == 200
    assert res.json()["status"] == "prepared"
    assert calls == []


def test_approve_with_browser_fill_fills_form_with_reviewed_letter(client, seeded, monkeypatch):
    calls = []

    async def fake_fill(**kwargs):
        calls.append(kwargs)
        return {"status": "filled", "channel": "browser", "notes": "Form pre-filled."}

    monkeypatch.setattr(main, "fill_application", fake_fill)
    app = seeded("review-fill-run", browser_fill=1)
    client.patch(f"/api/applications/{app.id}", json={"cover_letter": "Edited letter"})
    res = client.post(f"/api/applications/{app.id}/approve")
    assert res.status_code == 200
    assert res.json()["status"] == "filled"
    assert len(calls) == 1
    assert calls[0]["cover"] == "Edited letter"
    assert calls[0]["job"]["apply_url"] == "https://example.com/apply/1"
    assert calls[0]["packet_key"] == f"review-fill-run-{app.job_id}"


def test_approve_rejects_non_pending_and_skip_works(client, seeded):
    app = seeded("review-skip-run", browser_fill=0)
    res = client.post(f"/api/applications/{app.id}/skip")
    assert res.status_code == 200
    assert res.json()["status"] == "skipped"

    res = client.post(f"/api/applications/{app.id}/approve")
    assert res.status_code == 400
