import asyncio

import httpx
import pytest

from app.agent import companies, runner
from app.agent.ats import Board
from app.agent.search import SearchResult
from app.db import SessionLocal
from app.models import Company

LINKEDIN_HTML = """<html><head>
<meta property="og:title" content="Acme hiring Senior Software Engineer in San Francisco, CA | LinkedIn">
<meta property="og:site_name" content="LinkedIn"></head><body>Build Python services.</body></html>"""


def test_aggregator_links_never_resolve_to_the_aggregators_own_board(mock_http):
    probed = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host.endswith("linkedin.com"):
            return httpx.Response(200, text=LINKEDIN_HTML)
        probed.append(str(request.url))
        return httpx.Response(200, json={"name": "LinkedIn Partner Sandbox", "jobs": []})

    mock_http(handler)

    async def go():
        async with httpx.AsyncClient() as client:
            return await companies.resolve_job_url(client, "https://www.linkedin.com/jobs/view/4012345678", use_browser=False)

    job = asyncio.run(go())
    assert probed == []  # no ATS lookups for a "linkedin" board
    assert job["source"] == "Link"
    assert (job["company"], job["title"]) == ("Acme", "Senior Software Engineer")
    assert job["location"] == "San Francisco, CA"


def test_domain_guess_only_trusted_when_job_exists(mock_http):
    def handler(request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        if host == "stripe.com":
            return httpx.Response(200, text="<html><head><title>Abuse Investigator | Stripe</title></head></html>")
        if host == "boards-api.greenhouse.io" and path == "/v1/boards/stripe":
            return httpx.Response(200, json={"name": "Stripe"})
        if host == "boards-api.greenhouse.io" and path == "/v1/boards/stripe/jobs":
            return httpx.Response(200, json={"jobs": []})
        if host == "boards-api.greenhouse.io" and path == "/v1/boards/stripe/jobs/8172510":
            return httpx.Response(200, json={"id": 8172510, "title": "Abuse Investigator", "location": {"name": "Seattle"},
                                             "absolute_url": "https://stripe.com/jobs/search?gh_jid=8172510", "content": ""})
        return httpx.Response(404)

    mock_http(handler)

    async def go(url):
        async with httpx.AsyncClient() as client:
            return await companies.resolve_job_url(client, url, use_browser=False)

    found = asyncio.run(go("https://stripe.com/jobs/search?gh_jid=8172510"))
    assert found["source"] == "Greenhouse" and found["apply_url"].endswith("for=stripe&token=8172510")
    closed = asyncio.run(go("https://stripe.com/jobs/search?gh_jid=999999"))
    assert closed["source"] == "Link" and closed["title"] == "Abuse Investigator"


def test_company_deleted_during_run_does_not_break_status_update():
    with SessionLocal() as db:
        keep = Company(name="KeepCo", ats="greenhouse", slug="keepco-r", source="user")
        drop = Company(name="DropCo", ats="greenhouse", slug="dropco-r", source="user")
        db.add_all([keep, drop])
        db.commit()
        tracked = [(keep, Board("greenhouse", "keepco-r", "KeepCo")), (drop, Board("greenhouse", "dropco-r", "DropCo"))]

        with SessionLocal() as other:  # user removes DropCo on the Companies tab mid-search
            other.delete(other.get(Company, drop.id))
            other.commit()

        found = SearchResult(board_counts={"greenhouse:keepco-r": 5, "greenhouse:dropco-r": 2})

        async def progress(_m):
            pass

        asyncio.run(runner._update_companies(db, tracked, found, progress))
        assert db.query(Company).filter_by(slug="keepco-r").one().job_count == 5
        assert db.query(Company).filter_by(slug="dropco-r").first() is None
        db.delete(db.query(Company).filter_by(slug="keepco-r").one())
        db.commit()


SUBMIT_PAGE = """
<form>
  <button id="bare">Apply</button>
  <button id="typed-button" type="button">Apply</button>
  <input id="submit-input" type="submit" value="Apply">
  <a id="link-in-form" href="/apply">Apply</a>
  <ul><li id="option" role="option">San Francisco, CA</li></ul>
  <button id="option-button">San Francisco</button>
</form>
<a id="link" href="/apply">Apply now</a>
<button id="loose" type="button">Apply</button>
"""


def test_click_guards_refuse_anything_that_could_submit():
    playwright = pytest.importorskip("playwright.async_api")
    from app.agent.formfill import MAY_SUBMIT_JS, OPTION_MAY_SUBMIT_JS

    async def go():
        async with playwright.async_playwright() as p:
            try:
                browser = await p.chromium.launch()
            except Exception as exc:  # browser binaries not installed
                pytest.skip(f"chromium unavailable: {exc}")
            page = await browser.new_page()
            await page.set_content(SUBMIT_PAGE)
            out = {}
            for el_id in ("bare", "typed-button", "submit-input", "link-in-form", "link", "loose", "option", "option-button"):
                loc = page.locator(f"#{el_id}")
                out[el_id] = (await loc.evaluate(MAY_SUBMIT_JS), await loc.evaluate(OPTION_MAY_SUBMIT_JS))
            await browser.close()
            return out

    r = asyncio.run(go())
    assert r["bare"] == (True, True)  # <button> with no type inside a form submits it
    assert r["submit-input"] == (True, True)
    assert r["typed-button"][0] is True  # in-form controls are never clicked as "Apply"
    assert r["link-in-form"][0] is False and r["link"][0] is False and r["loose"][0] is False
    assert r["option"][1] is False  # plain autocomplete options are still clickable
    assert r["option-button"][1] is True


def test_unreadable_aggregator_page_asks_for_employer_link(mock_http):
    mock_http(lambda r: httpx.Response(200, text="<html><head><title>LinkedIn</title>"
                                                 '<meta property="og:site_name" content="LinkedIn"></head></html>'))

    async def go():
        async with httpx.AsyncClient() as client:
            await companies.resolve_job_url(client, "https://www.linkedin.com/jobs/view/1", use_browser=False)

    with pytest.raises(companies.ResolveError, match="employer's own careers site"):
        asyncio.run(go())


def test_email_apply_instructions_are_detected():
    playwright = pytest.importorskip("playwright.async_api")
    from app.agent.formfill import _email_instructions

    pages = {
        "remotive": "<div role='dialog'>To apply to this job please send an email directly to\n\njobs@mitremedia.com\n\n"
                    "with the email title [Tech Lead Full-Stack Rails Engineer @ Mitre Media]</div>",
        "accommodation": "<main>Need an accommodation? Email accommodations@acme.com and we'll help.</main>",
        "none": "<main>This job is closed.</main>",
    }

    async def go():
        async with playwright.async_playwright() as p:
            try:
                browser = await p.chromium.launch()
            except Exception as exc:
                pytest.skip(f"chromium unavailable: {exc}")
            page = await browser.new_page()
            out = {}
            for key, html in pages.items():
                await page.set_content(html)
                out[key] = await _email_instructions(page)
            await browser.close()
            return out

    r = asyncio.run(go())
    assert r["remotive"] == ("jobs@mitremedia.com", "Tech Lead Full-Stack Rails Engineer @ Mitre Media")
    assert r["accommodation"] is None and r["none"] is None


def test_email_subject_prefers_the_instruction_mention():
    from app.agent.formfill import _email_in

    text = ("Send a brief note about why you're excited to jobs@acme.com.\n\nAbout Acme..." + " filler" * 600 +
            "\n\nTo apply to this job please send an email directly to\n\njobs@acme.com\n\nwith the email title [Engineer @ Acme]")
    assert _email_in(text) == ("jobs@acme.com", "Engineer @ Acme")
