"""Tracker pipeline stage: a packet starts at "not_applied" and only a
genuine submission moves it to "applied".

Preparing a packet, pre-filling a form ("filled"), or skipping it must not
claim the user applied — the old schema default stamped every new packet
"applied" from creation. The backfill in upgrade_schema corrects rows the
old default already stamped."""
import uuid

import pytest
from fastapi.testclient import TestClient

from app import main
from app.agent.seen import advance_stage_on_submission
from app.db import SessionLocal, upgrade_schema
from app.models import Application, JobMatch, Run, SeenJob


@pytest.fixture
def client():
    return TestClient(main.app)


@pytest.fixture
def seeded():
    """A run with one job and one application waiting for review."""
    created = []

    def make(run_id: str) -> Application:
        db = SessionLocal()
        try:
            db.add(
                Run(
                    id=run_id,
                    status="completed",
                    locations="San Francisco, CA",
                    auto_apply=0,
                    browser_fill=0,
                    resume_path="",
                    profile_json='{"name": "Pat", "email": "pat@example.com"}',
                )
            )
            job = JobMatch(
                run_id=run_id,
                external_id="stage-ind-1",
                title="Stage Engineer",
                company="Stage Industries",
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


def _stage_of(app_id: int) -> str:
    db = SessionLocal()
    try:
        return db.get(Application, app_id).pipeline_stage
    finally:
        db.close()


def test_new_application_starts_not_applied(client, seeded):
    app = seeded("stage-default-run")
    assert app.pipeline_stage == "not_applied"

    listed = client.get("/api/applications").json()
    mine = [a for a in listed if a["id"] == app.id]
    assert len(mine) == 1
    assert mine[0]["pipeline_stage"] == "not_applied"


def test_mark_submitted_advances_stage(client, seeded):
    app = seeded("stage-mark-applied-run")
    res = client.patch(f"/api/applications/{app.id}", json={"status": "applied"})
    assert res.status_code == 200
    assert res.json()["pipeline_stage"] == "applied"
    assert _stage_of(app.id) == "applied"


@pytest.mark.parametrize("status", ["prepared", "filled"])
def test_mark_prepared_or_filled_does_not_advance_stage(client, seeded, status):
    app = seeded(f"stage-mark-{status}-run")
    res = client.patch(f"/api/applications/{app.id}", json={"status": status})
    assert res.status_code == 200
    assert res.json()["status"] == status
    assert res.json()["pipeline_stage"] == "not_applied"
    assert _stage_of(app.id) == "not_applied"


def test_approve_prepared_does_not_advance_stage(client, seeded):
    app = seeded("stage-approve-prepared-run")
    res = client.post(f"/api/applications/{app.id}/approve")
    assert res.status_code == 200
    assert res.json()["status"] == "prepared"
    assert res.json()["pipeline_stage"] == "not_applied"


def test_approve_emailed_advances_stage(client, seeded, monkeypatch):
    """When approval actually sends the application email, apply_to_job
    returns "applied" — that is a real submission and must advance."""

    def fake_apply(**kwargs):
        return {"status": "applied", "channel": "email", "notes": "Sent resume and cover letter."}

    monkeypatch.setattr(main, "apply_to_job", fake_apply)
    app = seeded("stage-approve-applied-run")
    res = client.post(f"/api/applications/{app.id}/approve")
    assert res.status_code == 200
    assert res.json()["status"] == "applied"
    assert res.json()["pipeline_stage"] == "applied"


def test_approve_failed_does_not_advance_stage(client, seeded, monkeypatch):
    def fake_apply(**kwargs):
        return {"status": "failed", "channel": "email", "notes": "SMTP unavailable."}

    monkeypatch.setattr(main, "apply_to_job", fake_apply)
    app = seeded("stage-approve-failed-run")
    res = client.post(f"/api/applications/{app.id}/approve")
    assert res.status_code == 200
    assert res.json()["status"] == "failed"
    assert res.json()["pipeline_stage"] == "not_applied"


def test_skip_does_not_advance_stage(client, seeded):
    app = seeded("stage-skip-run")
    res = client.post(f"/api/applications/{app.id}/skip")
    assert res.status_code == 200
    assert res.json()["status"] == "skipped"
    assert res.json()["pipeline_stage"] == "not_applied"


def test_submission_does_not_overwrite_user_advanced_stage(client, seeded):
    app = seeded("stage-keep-screen-run")
    res = client.patch(f"/api/applications/{app.id}/tracker", json={"pipeline_stage": "screen"})
    assert res.status_code == 200
    assert res.json()["pipeline_stage"] == "screen"

    res = client.patch(f"/api/applications/{app.id}", json={"status": "applied"})
    assert res.status_code == 200
    assert res.json()["status"] == "applied"
    assert res.json()["pipeline_stage"] == "screen"
    assert _stage_of(app.id) == "screen"


def test_tracker_accepts_not_applied(client, seeded):
    app = seeded("stage-tracker-run")
    res = client.patch(f"/api/applications/{app.id}", json={"status": "applied"})
    assert res.json()["pipeline_stage"] == "applied"
    res = client.patch(f"/api/applications/{app.id}/tracker", json={"pipeline_stage": "not_applied"})
    assert res.status_code == 200
    assert res.json()["pipeline_stage"] == "not_applied"


# ------------------------------------------------------------ unit: helper


class _FakeApp:
    def __init__(self, stage):
        self.pipeline_stage = stage


@pytest.mark.parametrize("status", ["prepared", "filled", "skipped", "failed", "pending_review"])
def test_helper_ignores_non_submissions(status):
    db = SessionLocal()
    try:
        app = _FakeApp("not_applied")
        advance_stage_on_submission(db, app, status)
        assert app.pipeline_stage == "not_applied"
    finally:
        db.close()


def test_helper_advances_only_from_not_applied():
    db = SessionLocal()
    try:
        fresh = _FakeApp(None)  # an unset stage counts as not applied yet
        advance_stage_on_submission(db, fresh, "applied")
        assert fresh.pipeline_stage == "applied"

        moved = _FakeApp("interview")
        advance_stage_on_submission(db, moved, "applied")
        assert moved.pipeline_stage == "interview"
    finally:
        db.close()


# ------------------------------------------------------------ backfill


def _seed_legacy(run_id: str, status: str, stage: str) -> int:
    db = SessionLocal()
    try:
        app = Application(
            run_id=run_id,
            title="Legacy Role",
            company="Legacy Co",
            status=status,
            pipeline_stage=stage,
        )
        db.add(app)
        db.commit()
        db.refresh(app)
        return app.id
    finally:
        db.close()


def _stages_by_id(ids) -> dict:
    db = SessionLocal()
    try:
        return {a.id: a.pipeline_stage for a in db.query(Application).filter(Application.id.in_(ids)).all()}
    finally:
        db.close()


def test_backfill_corrects_old_default_stamps():
    run_id = f"stage-backfill-{uuid.uuid4().hex[:8]}"
    cases = {
        # (status, stamped stage) -> expected stage after the backfill
        ("pending_review", "applied"): "not_applied",
        ("filled", "applied"): "not_applied",
        ("skipped", "applied"): "not_applied",
        ("applied", "applied"): "applied",
        ("prepared", "screen"): "screen",  # user moved it along: untouched
        ("prepared", "not_applied"): "not_applied",
        ("applied", "reply"): "reply",  # user moved it along: untouched
    }
    ids = {}
    db = SessionLocal()
    try:
        for (status, stage), _expected in cases.items():
            ids[(status, stage)] = _seed_legacy(run_id, status, stage)

        upgrade_schema()
        got = _stages_by_id(list(ids.values()))
        for key, expected in cases.items():
            assert got[ids[key]] == expected, f"{key}: got {got[ids[key]]}, want {expected}"

        # Idempotent: a second startup changes nothing further.
        upgrade_schema()
        assert _stages_by_id(list(ids.values())) == got
    finally:
        db.query(Application).filter(Application.run_id == run_id).delete()
        db.commit()
        db.close()
