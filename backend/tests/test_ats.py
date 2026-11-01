import asyncio

import httpx
import pytest

from app.agent import ats
from app.agent.ats import AtsRef, Board, BoardNotFound

UUID = "223f9722-3579-4ff2-b123-e4cc7b0c1a64"
WD = "nvidia.wd5.myworkdayjobs.com|nvidia|NVIDIAExternalCareerSite"


@pytest.mark.parametrize(
    "url, ref",
    [
        ("https://boards.greenhouse.io/stripe", AtsRef("greenhouse", "stripe")),
        ("https://job-boards.greenhouse.io/figma/jobs/5426468004", AtsRef("greenhouse", "figma", "5426468004")),
        ("https://boards.greenhouse.io/figma/jobs/5426468004?gh_jid=5426468004", AtsRef("greenhouse", "figma", "5426468004")),
        ("https://job-boards.greenhouse.io/embed/job_app?for=lucidmotors&token=5197938007", AtsRef("greenhouse", "lucidmotors", "5197938007")),
        ("https://boards.greenhouse.io/embed/job_board?for=acme", AtsRef("greenhouse", "acme")),
        ("https://boards-api.greenhouse.io/v1/boards/acme/jobs/12", AtsRef("greenhouse", "acme", "12")),
        ("https://jobs.lever.co/spotify", AtsRef("lever", "spotify")),
        (f"https://jobs.lever.co/spotify/{UUID}/apply", AtsRef("lever", "spotify", UUID)),
        (f"https://jobs.eu.lever.co/acme/{UUID}", AtsRef("lever_eu", "acme", UUID)),
        ("https://jobs.ashbyhq.com/ramp", AtsRef("ashby", "ramp")),
        (f"https://jobs.ashbyhq.com/ramp/{UUID}/application", AtsRef("ashby", "ramp", UUID)),
        ("https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite", AtsRef("workday", WD)),
        (
            "https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/US-CA-Santa-Clara/SWE_JR1",
            AtsRef("workday", WD, "/job/US-CA-Santa-Clara/SWE_JR1"),
        ),
        ("https://wd1.myworkdaysite.com/recruiting/acme/Careers/job/X_1", AtsRef("workday", "acme.wd1.myworkdayjobs.com|acme|Careers", "/job/X_1")),
        ("https://lucidmotors.com/careers", None),
        ("not a url", None),
    ],
)
def test_parse_ats_url(url, ref):
    assert ats.parse_ats_url(url) == ref


def test_find_ats_refs_in_html_skips_non_board_paths():
    html = """
      <iframe src="https://job-boards.greenhouse.io/embed/job_app?for=lucidmotors&amp;token=5197938007"></iframe>
      <img src="https://job-boards.greenhouse.io/badges/logo.png">
      <script src="https://boards.greenhouse.io/embed/job_board/js?for=lucidmotors"></script>
    """
    refs = ats.find_ats_refs(html)
    assert refs[0] == AtsRef("greenhouse", "lucidmotors", "5197938007")
    assert all(r.slug != "badges" for r in refs)


@pytest.mark.parametrize(
    "url, hint",
    [
        ("https://stripe.com/jobs/search?gh_jid=8172510", "8172510"),
        ("https://lucidmotors.com/careers/search/5197938007", "5197938007"),
        ("https://example.com/careers", ""),
    ],
)
def test_job_id_hint(url, hint):
    assert ats.job_id_hint(url) == hint


def test_slug_candidates_most_specific_first():
    assert ats.slug_candidates("Lucid Motors")[:2] == ["lucidmotors", "lucid-motors"]
    cands = ats.slug_candidates("Lucid Motors, Inc.")
    assert cands[0] == "lucidmotorsinc" and "lucidmotors" in cands and cands.index("lucidmotors") < cands.index("lucid")
    assert ats.slug_candidates("Hims & Hers")[0] == "himsandhers"


def _run(coro):
    return asyncio.run(coro)


def test_greenhouse_board_and_details(mock_http):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/boards/acme/jobs":
            return httpx.Response(200, json={"jobs": [{
                "id": 11, "title": "Senior Software Engineer", "company_name": "Acme",
                "location": {"name": "San Francisco, CA"}, "absolute_url": "https://acme.com/jobs?gh_jid=11",
                "first_published": "2026-10-01T00:00:00Z",
            }]})
        if path == "/v1/boards/acme/jobs/11":
            return httpx.Response(200, json={"content": "&lt;p&gt;Build &amp;amp; ship Python&lt;/p&gt;",
                                             "offices": [{"location": "New York, NY"}, {"name": "US"}]})
        if path == "/v1/boards/gone/jobs":
            return httpx.Response(404, json={})
        raise AssertionError(path)

    mock_http(handler)

    async def go():
        async with httpx.AsyncClient() as client:
            jobs = await ats.fetch_board(client, Board("greenhouse", "acme", "Acme Inc"))
            detailed = await ats.fetch_details(client, jobs[0])
            with pytest.raises(BoardNotFound):
                await ats.fetch_board(client, Board("greenhouse", "gone"))
            return jobs[0], detailed

    job, detailed = _run(go())
    assert job["company"] == "Acme Inc"
    assert job["apply_url"] == "https://job-boards.greenhouse.io/embed/job_app?for=acme&token=11"
    assert job["needs_details"] and job["description"] == ""
    assert "Build & ship Python" in detailed["description"]
    assert detailed["location"] == "San Francisco, CA • New York, NY"  # bare-country office skipped


def test_lever_and_ashby_normalization(mock_http):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.lever.co":
            return httpx.Response(200, json=[{
                "id": UUID, "text": "Backend Engineer", "workplaceType": "remote", "country": "US",
                "categories": {"location": "Remote", "allLocations": ["Remote"]},
                "descriptionPlain": "Python services", "lists": [{"text": "You have", "content": "<li>Go</li>"}],
                "hostedUrl": f"https://jobs.lever.co/acme/{UUID}", "applyUrl": f"https://jobs.lever.co/acme/{UUID}/apply",
                "createdAt": 1790000000000,
            }])
        if request.url.host == "api.ashbyhq.com":
            return httpx.Response(200, json={"jobs": [
                {"id": "a1", "title": "Platform Engineer", "location": "New York, NY", "isRemote": True, "isListed": True,
                 "secondaryLocations": [{"location": "Remote (Canada)"}], "descriptionPlain": "Kubernetes",
                 "jobUrl": "https://jobs.ashbyhq.com/acme/a1", "applyUrl": "https://jobs.ashbyhq.com/acme/a1/application",
                 "address": {"postalAddress": {"addressCountry": "USA"}}},
                {"id": "a2", "title": "Hidden", "isListed": False},
            ]})
        raise AssertionError(str(request.url))

    mock_http(handler)

    async def go():
        async with httpx.AsyncClient() as client:
            return (await ats.fetch_board(client, Board("lever", "acme", "Acme")),
                    await ats.fetch_board(client, Board("ashby", "acme", "Acme")))

    lever, ashby = _run(go())
    assert lever[0]["location"] == "Remote (US)" and lever[0]["remote"]
    assert "You have: Go" in lever[0]["description"]
    assert lever[0]["apply_url"].endswith("/apply")
    assert len(ashby) == 1
    assert ashby[0]["location"].startswith("New York, NY • Remote (Canada)") and ashby[0]["remote"]


def test_lever_not_found_payload_is_board_not_found(mock_http):
    mock_http(lambda r: httpx.Response(200, json={"ok": False, "error": "Document not found"}))

    async def go():
        async with httpx.AsyncClient() as client:
            await ats.fetch_board(client, Board("lever", "plaid"))

    with pytest.raises(BoardNotFound):
        _run(go())


def test_workday_search_and_details(mock_http):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            assert request.url.path == "/wday/cxs/nvidia/NVIDIAExternalCareerSite/jobs"
            return httpx.Response(200, json={"total": 1, "jobPostings": [
                {"title": "Senior Software Engineer", "externalPath": "/job/US-CA/SWE_JR1", "locationsText": "2 Locations",
                 "postedOn": "Posted Today", "bulletFields": ["JR1"]}]})
        return httpx.Response(200, json={"jobPostingInfo": {
            "title": "Senior Software Engineer", "jobDescription": "<p>CUDA</p>", "location": "US, CA, Santa Clara",
            "additionalLocations": ["US, WA, Seattle"], "country": {"descriptor": "United States of America"},
            "externalUrl": "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite/job/US-CA/SWE_JR1"}})

    mock_http(handler)

    async def go():
        async with httpx.AsyncClient() as client:
            jobs = await ats.fetch_board(client, Board("workday", WD, "NVIDIA"), "software engineer")
            return jobs, await ats.fetch_details(client, jobs[0])

    jobs, detailed = _run(go())
    assert jobs[0]["multi_location"] and jobs[0]["company"] == "NVIDIA"
    assert detailed["location"] == "US, CA, Santa Clara • US, WA, Seattle"
    assert detailed["description"] == "CUDA" and not detailed["multi_location"]


def test_best_board_for_name_rejects_other_company(mock_http):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "boards-api.greenhouse.io" and request.url.path == "/v1/boards/lucid":
            return httpx.Response(200, json={"name": "Lucid Software"})
        if request.url.host == "boards-api.greenhouse.io" and request.url.path == "/v1/boards/lucidmotors":
            return httpx.Response(200, json={"name": "Lucid Motors"})
        if request.url.path == "/v1/boards/lucidmotors/jobs":
            return httpx.Response(200, json={"jobs": []})
        return httpx.Response(404, text="nope")

    mock_http(handler)

    async def go(name):
        async with httpx.AsyncClient() as client:
            return await ats.best_board_for_name(client, name)

    assert _run(go("Lucid Motors"))[:2] == ("greenhouse", "lucidmotors")
    assert _run(go("Lucid Group")) is None  # only "Lucid Software" answers to "lucid"


def test_cache_avoids_refetch(mock_http):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={"jobs": []})

    mock_http(handler)

    async def go():
        async with httpx.AsyncClient() as client:
            await ats.fetch_board(client, Board("greenhouse", "acme"))
            await ats.fetch_board(client, Board("greenhouse", "acme"))

    _run(go())
    assert len(calls) == 1


@pytest.mark.parametrize(
    "country, expected",
    [("Canada", "Remote (Canada)"), ("United Kingdom", "Remote (United Kingdom)"), ("GB", "Remote (United Kingdom)"),
     ("United States of America", "Remote (US)"), ("US", "Remote (US)"), ("Atlantis", "Remote")],
)
def test_with_country_accepts_names_and_codes(country, expected):
    assert ats._with_country("Remote", country) == expected


def test_ashby_remote_abroad_is_not_us_remote():
    from app.agent.locations import match_location, parse_locations

    job = ats._ashby_job(Board("ashby", "acme", "Acme"), {
        "id": "x", "title": "Engineer", "location": "Remote", "isRemote": True,
        "address": {"postalAddress": {"addressCountry": "Canada"}},
    })
    assert match_location(job["location"], job["remote"], parse_locations("Remote, US"))[0] == ""
