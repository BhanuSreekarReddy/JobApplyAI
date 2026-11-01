import asyncio

import httpx

from app.agent import search
from app.agent.ats import Board
from app.agent.locations import parse_locations
from app.agent.match import rank_jobs
from app.agent.profile import heuristic_profile
from app.agent.search import title_relevance

PROFILE = {"titles": ["software engineer"], "skills": ["python", "react", "aws"], "years_experience": 6}


def test_title_relevance_families():
    assert title_relevance("Senior Software Engineer, Backend", ["software engineer"], ["python"]) >= 3
    assert title_relevance("Python Developer", ["software engineer"], ["python"]) > 1
    assert title_relevance("Account Executive, Enterprise", ["software engineer"], []) == 0
    assert title_relevance("Sales Engineer", ["software engineer"], []) < 1
    assert title_relevance("Recruiter, Engineering", ["software engineer"], []) == 0
    assert title_relevance("Data Scientist", ["data scientist"], []) >= 2
    assert title_relevance("Product Designer", ["product designer"], []) >= 2
    assert title_relevance("Product Designer", ["software engineer"], []) == 0


def _board_handler(request: httpx.Request) -> httpx.Response:
    host, path = request.url.host, request.url.path
    if host == "boards-api.greenhouse.io" and path == "/v1/boards/acme/jobs":
        return httpx.Response(200, json={"jobs": [
            {"id": 1, "title": "Senior Software Engineer", "location": {"name": "San Francisco, CA"}, "absolute_url": "u1"},
            {"id": 2, "title": "Senior Software Engineer", "location": {"name": "London, UK"}, "absolute_url": "u2"},
            {"id": 3, "title": "Account Executive", "location": {"name": "San Francisco, CA"}, "absolute_url": "u3"},
            {"id": 4, "title": "Software Engineer, New Grad", "location": {"name": "Remote - US"}, "absolute_url": "u4"},
        ]})
    if host == "boards-api.greenhouse.io" and path.startswith("/v1/boards/acme/jobs/"):
        return httpx.Response(200, json={"content": "&lt;p&gt;Python React AWS&lt;/p&gt;"})
    if host == "boards-api.greenhouse.io" and path == "/v1/boards/moved/jobs":
        return httpx.Response(404)
    if host == "api.ashbyhq.com":
        return httpx.Response(500)
    if host == "remotive.com":
        return httpx.Response(200, json={"jobs": [
            {"id": 9, "title": "Backend Engineer", "company_name": "Euro", "candidate_required_location": "Europe", "url": "r9"},
            {"id": 10, "title": "Full Stack Engineer", "company_name": "USCo", "candidate_required_location": "USA", "url": "r10"},
        ]})
    return httpx.Response(200, json={})


def test_search_filters_by_title_and_location_and_reports_board_health(mock_http):
    mock_http(_board_handler)
    boards = [Board("greenhouse", "acme", "Acme"), Board("greenhouse", "moved", "Moved"), Board("ashby", "flaky", "Flaky")]
    wanted = parse_locations("San Francisco, CA, Remote")
    result = asyncio.run(search.search_jobs(PROFILE, wanted, boards))

    titles = {(j["company"], j["title"], j["location"]) for j in result.jobs}
    assert ("Acme", "Senior Software Engineer", "San Francisco, CA") in titles
    assert ("USCo", "Full Stack Engineer", "USA") in titles
    assert not any(loc == "London, UK" or company == "Euro" for company, _t, loc in titles)
    assert not any(t == "Account Executive" for _c, t, _l in titles)
    assert [b.slug for b in result.missing] == ["moved"]
    assert [b.slug for b in result.failed] == ["flaky"]
    assert result.board_counts["greenhouse:acme"] == 4
    acme_sf = next(j for j in result.jobs if j["external_id"] == "greenhouse-acme-1")
    assert acme_sf["description"] == "Python React AWS"

    ranked = rank_jobs(result.jobs, PROFILE, wanted)
    assert ranked[0]["title"] == "Senior Software Engineer"
    new_grad = next(j for j in ranked if "New Grad" in j["title"])
    assert new_grad["score"] < ranked[0]["score"] - 30
    assert any("Entry-level" in r for r in new_grad["reasons"])


def test_adzuna_and_usajobs_parsing(mock_http, monkeypatch):
    monkeypatch.setattr(search.settings, "adzuna_app_id", "id")
    monkeypatch.setattr(search.settings, "adzuna_app_key", "key")
    monkeypatch.setattr(search.settings, "usajobs_api_key", "key")
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        if request.url.host == "api.adzuna.com":
            assert request.url.path == "/v1/api/jobs/us/search/1"
            return httpx.Response(200, json={"results": [{
                "id": 5, "title": "Software Engineer", "company": {"display_name": "Adz"},
                "location": {"display_name": "Austin, Travis County"}, "redirect_url": "https://adzuna/5",
                "description": "Email jobs@adz.example to apply. Python.",
            }]})
        if request.url.host == "data.usajobs.gov":
            assert request.headers["Authorization-Key"] == "key"
            return httpx.Response(200, json={"SearchResult": {"SearchResultItems": [{"MatchedObjectDescriptor": {
                "PositionID": "P1", "PositionTitle": "IT Specialist (Software Engineer)", "OrganizationName": "NASA",
                "PositionLocation": [{"PositionLocationDisplay": "Austin, Texas"}], "PositionURI": "https://usajobs/P1"}}]}})
        return httpx.Response(200, json={})

    mock_http(handler)
    result = asyncio.run(search.search_jobs(PROFILE, parse_locations("Austin, TX"), []))
    by_source = {j["source"]: j for j in result.jobs}
    assert by_source["Adzuna"]["apply_email"] == "jobs@adz.example"
    assert by_source["Adzuna"]["location"] == "Austin, Travis County"  # already recognisably US
    assert by_source["USAJobs"]["company"] == "NASA"
    adzuna_req = next(u for u in seen if u.host == "api.adzuna.com")
    assert adzuna_req.params["where"] == "Austin, TX" and adzuna_req.params["what"] == "software engineer"
    assert next(u for u in seen if u.host == "data.usajobs.gov").params["LocationName"] == "Austin, TX"


def test_profile_parses_pdf_style_header_and_years():
    text = (
        "JANE  DOE  Bay  Area,  California  •  5551234567  •  jane@example.com  •  linkedin.com/in/jane\n"
        "Staff  Software  Engineer  |  Distributed  Systems\n"
        "PROFESSIONAL  EXPERIENCE\n"
        "Acme, San Jose, CA 2019 – Present Staff Software Engineer\n"
        "Beta Corp, Austin, TX Jan 2015 - Dec 2018 Software Engineer\n"
        "EDUCATION\n"
        "State University 2011-2015 BS Computer Science, GPA 3.8\n"
    )
    p = heuristic_profile(text)
    assert p["name"] == "Jane Doe"
    assert "software engineer" in p["titles"]
    assert p["years_experience"] >= 10  # 2015 through today, education excluded
    assert p["email"] == "jane@example.com"


def test_profile_stated_years_and_plain_header():
    p = heuristic_profile("Priya Nair\nSan Francisco, CA\npriya@example.com\nSoftware Engineer with 6 years of experience in Python.\n")
    assert p["name"] == "Priya Nair" and p["years_experience"] == 6
