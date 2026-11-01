import asyncio
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app import main
from app.agent import companies, runner
from app.agent.ats import Board
from app.agent.search import SearchResult
from app.db import SessionLocal
from app.models import Application, Company, Run

SAMPLE = str(Path(__file__).resolve().parents[2] / "examples" / "sample-resume.txt")


@pytest.fixture
def client():
    return TestClient(main.app)


def test_starter_companies_loaded(client):
    data = client.get("/api/companies").json()
    starter = json.loads(companies.STARTER_FILE.read_text())
    assert data["summary"]["total"] >= len(starter) > 300
    assert all(c["source"] == "starter" for c in data["companies"][: len(starter)] if c["source"] != "user")


def test_add_disable_and_remove_company(client, monkeypatch):
    async def fake_resolve(_client, text, use_browser=True):
        if text == "nope":
            raise companies.ResolveError("No board found for “nope”.")
        return {"name": "Newco", "ats": "ashby", "slug": "newco", "job_id": ""}

    async def fake_fetch(_client, board, query=""):
        return [{"title": "x"}] * 3

    monkeypatch.setattr(companies, "resolve_input", fake_resolve)
    monkeypatch.setattr(companies.ats, "fetch_board", fake_fetch)

    r = client.post("/api/companies", json={"query": "Newco"})
    assert r.status_code == 200 and r.json()["created"] is True
    company = r.json()["company"]
    assert company["job_count"] == 3 and company["board_url"] == "https://jobs.ashbyhq.com/newco"
    assert client.post("/api/companies", json={"query": "https://jobs.ashbyhq.com/newco"}).json()["created"] is False

    assert client.post("/api/companies", json={"query": "nope"}).status_code == 422
    assert client.patch(f"/api/companies/{company['id']}", json={"enabled": False}).json()["enabled"] is False
    assert client.delete(f"/api/companies/{company['id']}").json() == {"deleted": True}

    starter = next(c for c in client.get("/api/companies").json()["companies"] if c["source"] == "starter")
    r = client.delete(f"/api/companies/{starter['id']}").json()
    assert r["deleted"] is False and r["company"]["enabled"] is False
    with SessionLocal() as db:
        companies.sync_starter(db)  # a removed starter company must not come back
        assert db.get(Company, starter["id"]).enabled == 0
    client.patch(f"/api/companies/{starter['id']}", json={"enabled": True})


def test_relocate_moved_board(monkeypatch):
    async def fake_exists(_client, ats_name, slug):
        return (ats_name == "ashby" and slug == "movedco"), "MovedCo"

    monkeypatch.setattr(companies.ats, "board_exists", fake_exists)
    with SessionLocal() as db:
        row = Company(name="MovedCo", ats="lever", slug="movedco", source="user")
        db.add(row)
        db.commit()
        board = asyncio.run(companies.relocate(db, httpx.AsyncClient(), row))
        assert board == Board("ashby", "movedco", "MovedCo")
        assert row.ats == "ashby" and row.status == "ok" and "Moved from Lever to Ashby" in row.note
        db.delete(row)
        db.commit()


def test_relocate_gives_up_cleanly(monkeypatch):
    async def none_exists(_client, ats_name, slug):
        return False, ""

    async def no_name_match(_client, name):
        return None

    monkeypatch.setattr(companies.ats, "board_exists", none_exists)
    monkeypatch.setattr(companies.ats, "best_board_for_name", no_name_match)
    with SessionLocal() as db:
        row = Company(name="GoneCo", ats="greenhouse", slug="goneco", source="user")
        db.add(row)
        db.commit()
        assert asyncio.run(companies.relocate(db, httpx.AsyncClient(), row)) is None
        assert row.status == "not_found" and "no other board" in row.note
        db.delete(row)
        db.commit()


def test_job_link_needs_profile_then_prepares_packet(client, monkeypatch):
    with SessionLocal() as db:
        db.query(Run).delete()
        db.commit()
    assert client.post("/api/jobs/from-link", json={"url": "https://x.example/job/1"}).status_code == 409

    with SessionLocal() as db:
        profile = {"name": "Priya Nair", "email": "priya@example.com", "skills": ["python"], "years_experience": 6}
        db.add(Run(id="linkrun00001", status="completed", locations="Newark, CA, Remote", resume_path=SAMPLE,
                   profile_json=json.dumps(profile)))
        db.commit()

    async def fake_job(_client, url, use_browser=True):
        if "careers" in url:
            raise companies.ResolveError("That link is Acme's careers page, not a specific job.")
        return {"external_id": "greenhouse-acme-7", "title": "Staff Engineer", "company": "Acme", "location": "Newark, CA",
                "source": "Greenhouse", "apply_url": "https://job-boards.greenhouse.io/embed/job_app?for=acme&token=7",
                "description": "Python", "remote": False}

    fills = []

    async def fake_fill(**kwargs):
        fills.append(kwargs)
        return {"status": "filled", "channel": "browser", "notes": "Filled 5 field(s)"}

    monkeypatch.setattr(main, "resolve_job_url", fake_job)
    monkeypatch.setattr(main, "fill_application", fake_fill)

    r = client.post("/api/jobs/from-link", json={"url": "https://acme.com/careers"})
    assert r.status_code == 422 and "careers page" in r.json()["detail"]

    r = client.post("/api/jobs/from-link", json={"url": "https://acme.com/jobs/7"})
    assert r.status_code == 200
    app_row = r.json()["application"]
    assert app_row["status"] == "filled" and app_row["company"] == "Acme"
    assert "Dear Acme" in app_row["cover_letter"]
    assert fills[0]["location"] == "Newark, CA"  # state kept, "Remote" skipped

    r = client.post("/api/jobs/from-link", json={"url": "https://acme.com/jobs/7", "fill": False})
    assert r.json()["application"]["status"] == "prepared" and len(fills) == 1


def test_full_run_records_company_health(monkeypatch):
    with SessionLocal() as db:
        db.add_all([
            Company(name="Healthy", ats="greenhouse", slug="healthy", source="user"),
            Company(name="Moved", ats="lever", slug="moved", source="user"),
        ])
        db.add(Run(id="fullrun00001", status="running", locations="San Francisco, CA, Remote", auto_apply=1,
                   browser_fill=0, min_score=0, max_applications=2, resume_path=SAMPLE))
        db.commit()

    async def fake_search(profile, wanted, boards, progress=None):
        assert wanted.labels() == ["San Francisco, CA", "Remote"]
        if progress:
            await progress("searching")
        res = SearchResult(boards_searched=len(boards), scanned=10, title_matches=4, location_matches=2)
        res.board_counts = {"greenhouse:healthy": 42}
        res.missing = [Board("lever", "moved", "Moved")]
        res.jobs = [{
            "external_id": "greenhouse-healthy-1", "title": "Senior Software Engineer", "company": "Healthy",
            "location": "San Francisco, CA", "source": "Greenhouse", "url": "u", "apply_url": "https://a/1",
            "apply_email": "", "description": "python typescript", "remote": False, "location_match": "city",
            "location_reason": "Location: San Francisco, CA",
        }]
        return res

    relocated = []

    async def fake_relocate(db, _client, company):
        relocated.append(company.slug)
        company.status = "not_found"
        db.commit()

    monkeypatch.setattr(runner, "search_jobs", fake_search)
    monkeypatch.setattr(runner, "relocate", fake_relocate)
    monkeypatch.setattr(runner, "sync_starter", lambda db: 0)
    monkeypatch.setattr(runner, "enabled_boards", lambda db: [
        (c, Board(c.ats, c.slug, c.name)) for c in db.query(Company).filter(Company.source == "user", Company.enabled == 1)
    ])

    with SessionLocal() as db:
        run = db.get(Run, "fullrun00001")
        asyncio.run(runner.execute_run(db, run))
        assert run.status == "completed", run.error
        healthy = db.query(Company).filter_by(slug="healthy").one()
        assert healthy.status == "ok" and healthy.job_count == 42 and healthy.last_checked
        assert relocated == ["moved"]
        app_row = db.query(Application).filter_by(run_id="fullrun00001").one()
        assert app_row.company == "Healthy" and app_row.status == "prepared"
