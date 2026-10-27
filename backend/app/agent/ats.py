"""Public job-board APIs of the applicant tracking systems most US tech companies use.

Each ATS exposes every customer's open jobs through one unauthenticated endpoint keyed by a
board slug, so supporting a company means knowing (ats, slug), not writing a scraper.
Workday has no documented API; its careers sites call a JSON endpoint (`/wday/cxs/...`) that is
used here read-only for search and job details.
"""
from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from .locations import COUNTRIES, countries_in, country_code, is_remote
from .normalize import make_job, plain_text

ATS_LABELS = {
    "greenhouse": "Greenhouse",
    "lever": "Lever",
    "lever_eu": "Lever",
    "ashby": "Ashby",
    "workday": "Workday",
}
BOARD_ATS = ("greenhouse", "lever", "ashby")
CACHE_TTL_S = 30 * 60
WORKDAY_PAGE = 20
HEADERS = {"User-Agent": "Mozilla/5.0 (JobApplyAgent personal job search)", "Accept": "application/json"}

_cache: dict[tuple[str, str, str], tuple[float, list[dict[str, Any]]]] = {}


class BoardNotFound(Exception):
    """The ATS has no board under this slug (company left the ATS or the slug is wrong)."""


@dataclass(frozen=True)
class Board:
    ats: str
    slug: str
    company: str = ""

    @property
    def key(self) -> str:
        return f"{self.ats}:{self.slug}"


def clear_cache() -> None:
    _cache.clear()


async def fetch_board(client: httpx.AsyncClient, board: Board, query: str = "") -> list[dict[str, Any]]:
    """All open jobs on a board (Workday: jobs matching `query`). Raises BoardNotFound on 404."""
    cache_key = (board.ats, board.slug, query if board.ats == "workday" else "")
    hit = _cache.get(cache_key)
    if hit and time.monotonic() - hit[0] < CACHE_TTL_S:
        return [dict(j, company=board.company or j["company"]) for j in hit[1]]
    fetcher = {
        "greenhouse": _greenhouse,
        "lever": _lever,
        "lever_eu": _lever,
        "ashby": _ashby,
        "workday": _workday,
    }[board.ats]
    jobs = await fetcher(client, board, query)
    _cache[cache_key] = (time.monotonic(), jobs)
    return jobs


async def _get_json(client: httpx.AsyncClient, url: str, **kwargs: Any) -> Any:
    r = await client.get(url, headers=HEADERS, **kwargs)
    if r.status_code == 404:
        raise BoardNotFound(url)
    r.raise_for_status()
    return r.json()


def _iso_from_ms(ms: Any) -> str:
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).isoformat()
    except (TypeError, ValueError):
        return ""


def _with_country(location: str, country: str) -> str:
    """Append the ATS's country field when the free-text location doesn't say it ("Remote" + US).
    Lever sends ISO codes ("US", "CA"); Ashby and Workday send names ("Canada", "United States of America")."""
    raw = (country or "").strip()
    code = raw.lower()
    if code not in COUNTRIES:
        code = country_code(raw)
    if code in COUNTRIES and code not in countries_in(location):
        label = "US" if code == "us" else COUNTRIES[code][0].title()
        return f"{location} ({label})" if location else label
    return location


# Greenhouse ---------------------------------------------------------------------------------

def greenhouse_apply_url(slug: str, job_id: str) -> str:
    # The embeddable application page is served for every board, including companies whose
    # careers site wraps Greenhouse in their own page (absolute_url points there).
    return f"https://job-boards.greenhouse.io/embed/job_app?for={slug}&token={job_id}"


async def _greenhouse(client: httpx.AsyncClient, board: Board, _query: str) -> list[dict[str, Any]]:
    data = await _get_json(client, f"https://boards-api.greenhouse.io/v1/boards/{board.slug}/jobs")
    out = []
    for item in data.get("jobs") or []:
        loc = ((item.get("location") or {}).get("name") or "").strip()
        out.append(
            make_job(
                external_id=f"greenhouse-{board.slug}-{item['id']}",
                title=item.get("title") or "",
                company=board.company or item.get("company_name") or board.slug,
                location=loc,
                source="Greenhouse",
                url=item.get("absolute_url") or "",
                apply_url=greenhouse_apply_url(board.slug, str(item["id"])),
                remote=is_remote(loc),
                posted=item.get("first_published") or item.get("updated_at") or "",
                ats="greenhouse",
                ats_slug=board.slug,
                ats_job_id=str(item["id"]),
                needs_details=True,
            )
        )
    return out


async def greenhouse_board_name(client: httpx.AsyncClient, slug: str) -> str:
    try:
        return (await _get_json(client, f"https://boards-api.greenhouse.io/v1/boards/{slug}")).get("name") or ""
    except Exception:
        return ""


# Lever --------------------------------------------------------------------------------------

def _lever_base(ats: str) -> str:
    return "https://api.eu.lever.co" if ats == "lever_eu" else "https://api.lever.co"


def _lever_job(board: Board, item: dict[str, Any]) -> dict[str, Any]:
    cats = item.get("categories") or {}
    locs = [l for l in (cats.get("allLocations") or []) if l] or ([cats["location"]] if cats.get("location") else [])
    loc = " • ".join(locs)
    remote = (item.get("workplaceType") or "").lower() == "remote" or is_remote(loc)
    if remote and not is_remote(loc):
        loc = f"Remote - {loc}" if loc else "Remote"
    loc = _with_country(loc, item.get("country") or "")
    lists = " ".join(f"{l.get('text', '')}: {plain_text(l.get('content', ''))}" for l in item.get("lists") or [])
    description = " ".join(
        p for p in (item.get("descriptionPlain") or plain_text(item.get("description") or ""), lists, item.get("additionalPlain") or "") if p
    )
    return make_job(
        external_id=f"lever-{board.slug}-{item['id']}",
        title=item.get("text") or "",
        company=board.company or board.slug,
        location=loc,
        source="Lever",
        url=item.get("hostedUrl") or "",
        apply_url=item.get("applyUrl") or "",
        description=description,
        remote=remote,
        posted=_iso_from_ms(item.get("createdAt")),
        ats=board.ats,
        ats_slug=board.slug,
        ats_job_id=str(item["id"]),
    )


async def _lever(client: httpx.AsyncClient, board: Board, _query: str) -> list[dict[str, Any]]:
    data = await _get_json(client, f"{_lever_base(board.ats)}/v0/postings/{board.slug}", params={"mode": "json"})
    if isinstance(data, dict):  # {"ok": false, "error": "Document not found"}
        raise BoardNotFound(board.key)
    return [_lever_job(board, item) for item in data if isinstance(item, dict)]


# Ashby --------------------------------------------------------------------------------------

def _ashby_job(board: Board, item: dict[str, Any]) -> dict[str, Any]:
    locs = [item.get("location") or ""] + [s.get("location") or "" for s in item.get("secondaryLocations") or []]
    loc = " • ".join(dict.fromkeys(l for l in locs if l))
    remote = bool(item.get("isRemote")) or (item.get("workplaceType") or "").lower() == "remote" or is_remote(loc)
    country = (((item.get("address") or {}).get("postalAddress") or {}).get("addressCountry") or "")
    loc = _with_country(loc, country)
    if remote and not is_remote(loc):
        loc = f"{loc} • Remote" if loc else "Remote"
    return make_job(
        external_id=f"ashby-{board.slug}-{item.get('id')}",
        title=item.get("title") or "",
        company=board.company or board.slug,
        location=loc,
        source="Ashby",
        url=item.get("jobUrl") or "",
        apply_url=item.get("applyUrl") or item.get("jobUrl") or "",
        description=item.get("descriptionPlain") or item.get("descriptionHtml") or "",
        remote=remote,
        posted=item.get("publishedAt") or "",
        ats="ashby",
        ats_slug=board.slug,
        ats_job_id=str(item.get("id")),
    )


async def _ashby(client: httpx.AsyncClient, board: Board, _query: str) -> list[dict[str, Any]]:
    data = await _get_json(client, f"https://api.ashbyhq.com/posting-api/job-board/{board.slug}")
    return [_ashby_job(board, item) for item in data.get("jobs") or [] if item.get("isListed", True)]


# Workday ------------------------------------------------------------------------------------

def workday_parts(slug: str) -> tuple[str, str, str]:
    host, tenant, site = slug.split("|")
    return host, tenant, site


def workday_slug(host: str, tenant: str, site: str) -> str:
    return f"{host}|{tenant}|{site}"


def _workday_url(host: str, site: str, path: str) -> str:
    return f"https://{host}/{site}{path}"


async def _workday(client: httpx.AsyncClient, board: Board, query: str) -> list[dict[str, Any]]:
    host, tenant, site = workday_parts(board.slug)
    endpoint = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    out: list[dict[str, Any]] = []
    for page in range(3):
        r = await client.post(
            endpoint,
            headers=HEADERS,
            json={"appliedFacets": {}, "limit": WORKDAY_PAGE, "offset": page * WORKDAY_PAGE, "searchText": query},
        )
        if r.status_code in (404, 422):
            raise BoardNotFound(board.key)
        r.raise_for_status()
        data = r.json()
        postings = data.get("jobPostings") or []
        for p in postings:
            path = p.get("externalPath") or ""
            loc = p.get("locationsText") or ""
            url = _workday_url(host, site, path)
            out.append(
                make_job(
                    external_id=f"workday-{tenant}-{(p.get('bulletFields') or [path])[0]}",
                    title=p.get("title") or "",
                    company=board.company or tenant,
                    location=loc,
                    source="Workday",
                    url=url,
                    apply_url=url,
                    remote=is_remote(loc),
                    posted=p.get("postedOn") or "",
                    ats="workday",
                    ats_slug=board.slug,
                    ats_job_id=path,
                    needs_details=True,
                    multi_location=bool(re.fullmatch(r"\d+ Locations", loc.strip())),
                )
            )
        if len(postings) < WORKDAY_PAGE or len(out) >= (data.get("total") or 0):
            break
    return out


# Details ------------------------------------------------------------------------------------

async def fetch_details(client: httpx.AsyncClient, job: dict[str, Any]) -> dict[str, Any]:
    """Fill description (and full location list) for boards whose list endpoint omits them."""
    if not job.get("needs_details"):
        return job
    ats, slug, job_id = job.get("ats"), job.get("ats_slug"), job.get("ats_job_id")
    try:
        if ats == "greenhouse":
            d = await _get_json(client, f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{job_id}")
            offices = [o.get("location") or o.get("name") for o in d.get("offices") or [] if o.get("location") or o.get("name")]
            loc = job["location"]
            country_words = {a for names in COUNTRIES.values() for a in names}
            extra = [
                o for o in dict.fromkeys(offices)
                if o and o.lower() not in loc.lower() and o.strip().lower() not in country_words
            ]
            if extra and len(loc) < 120:
                loc = " • ".join([loc] + extra) if loc else " • ".join(extra)
            return {**job, "description": plain_text(d.get("content") or ""), "location": loc, "needs_details": False}
        if ats == "workday":
            host, tenant, site = workday_parts(slug)
            d = (await _get_json(client, f"https://{host}/wday/cxs/{tenant}/{site}{job_id}")).get("jobPostingInfo") or {}
            locs = [d.get("location") or ""] + list(d.get("additionalLocations") or [])
            loc = " • ".join(l for l in locs if l) or job["location"]
            country = ((d.get("country") or {}).get("descriptor")) or ""
            loc = _with_country(loc, country)
            return {
                **job,
                "title": d.get("title") or job["title"],
                "description": plain_text(d.get("jobDescription") or ""),
                "location": loc,
                "remote": job["remote"] or is_remote(loc) or "remote" in (d.get("remoteType") or "").lower(),
                "url": d.get("externalUrl") or job["url"],
                "apply_url": d.get("externalUrl") or job["apply_url"],
                "needs_details": False,
                "multi_location": False,
            }
    except Exception:
        pass
    return job


async def fetch_job(client: httpx.AsyncClient, ats: str, slug: str, job_id: str, company: str = "") -> dict[str, Any] | None:
    """One specific job, used when the candidate pastes a job link."""
    board = Board(ats, slug, company)
    if ats == "greenhouse":
        d = await _get_json(client, f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{job_id}")
        loc = ((d.get("location") or {}).get("name") or "").strip()
        return make_job(
            external_id=f"greenhouse-{slug}-{d['id']}",
            title=d.get("title") or "",
            company=company or d.get("company_name") or await greenhouse_board_name(client, slug) or slug,
            location=loc,
            source="Greenhouse",
            url=d.get("absolute_url") or "",
            apply_url=greenhouse_apply_url(slug, str(d["id"])),
            description=d.get("content") or "",
            remote=is_remote(loc),
            ats="greenhouse",
            ats_slug=slug,
            ats_job_id=str(d["id"]),
        )
    if ats in ("lever", "lever_eu"):
        d = await _get_json(client, f"{_lever_base(ats)}/v0/postings/{slug}/{job_id}", params={"mode": "json"})
        if not isinstance(d, dict) or d.get("ok") is False:
            raise BoardNotFound(f"{ats}:{slug}/{job_id}")
        return _lever_job(board, d)
    if ats == "ashby":
        for job in await fetch_board(client, board):
            if job["ats_job_id"] == job_id:
                return job
        raise BoardNotFound(f"ashby:{slug}/{job_id}")
    if ats == "workday":
        host, tenant, _site = workday_parts(slug)
        stub = make_job(
            external_id=f"workday-{tenant}-{job_id}", title="", company=company or tenant, location="",
            source="Workday", url="", ats="workday", ats_slug=slug, ats_job_id=job_id, needs_details=True,
        )
        job = await fetch_details(client, stub)
        if job.get("needs_details"):
            raise BoardNotFound(f"workday:{slug}{job_id}")
        return job
    return None


# Link parsing and discovery -----------------------------------------------------------------

@dataclass
class AtsRef:
    ats: str
    slug: str
    job_id: str = ""


_GH_HOSTS = {"boards.greenhouse.io", "job-boards.greenhouse.io", "boards-api.greenhouse.io"}
_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"


def parse_ats_url(url: str) -> AtsRef | None:
    """Recognise ATS-hosted board/job/application URLs."""
    try:
        u = urlparse(url.strip())
    except ValueError:
        return None
    host = (u.hostname or "").lower()
    parts = [p for p in u.path.split("/") if p]
    qs = parse_qs(u.query)

    if host in _GH_HOSTS:
        if parts[:1] == ["embed"]:
            slug = (qs.get("for") or [""])[0]
            return AtsRef("greenhouse", slug, (qs.get("token") or [""])[0]) if slug else None
        if host == "boards-api.greenhouse.io" and len(parts) >= 3 and parts[:2] == ["v1", "boards"]:
            job = parts[4] if len(parts) >= 5 and parts[3] == "jobs" else ""
            return AtsRef("greenhouse", parts[2], job)
        if parts:
            job = parts[2] if len(parts) >= 3 and parts[1] == "jobs" else (qs.get("gh_jid") or [""])[0]
            return AtsRef("greenhouse", parts[0], job)
        return None

    if host in {"jobs.lever.co", "jobs.eu.lever.co"} and parts:
        ats = "lever_eu" if ".eu." in host else "lever"
        job = parts[1] if len(parts) >= 2 and re.fullmatch(_UUID, parts[1]) else ""
        return AtsRef(ats, parts[0], job)
    if host in {"api.lever.co", "api.eu.lever.co"} and len(parts) >= 3 and parts[:2] == ["v0", "postings"]:
        return AtsRef("lever_eu" if ".eu." in host else "lever", parts[2], parts[3] if len(parts) >= 4 else "")

    if host == "jobs.ashbyhq.com" and parts:
        job = parts[1] if len(parts) >= 2 and re.fullmatch(_UUID, parts[1]) else ""
        return AtsRef("ashby", parts[0], job)
    if host == "api.ashbyhq.com" and len(parts) >= 3 and parts[:2] == ["posting-api", "job-board"]:
        return AtsRef("ashby", parts[2])

    m = re.fullmatch(r"([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com", host)
    if m and parts:
        tenant = m.group(1)
        if parts[:2] == ["wday", "cxs"] and len(parts) >= 4:
            site, rest = parts[3], parts[4:]
        else:
            if re.fullmatch(r"[a-z]{2}-[A-Z]{2}", parts[0]):
                parts = parts[1:]
            if not parts:
                return None
            site, rest = parts[0], parts[1:]
        job = "/" + "/".join(rest) if rest[:1] == ["job"] else ""
        return AtsRef("workday", workday_slug(host, tenant, site), job)
    m = re.fullmatch(r"(wd\d+)\.myworkdaysite\.com", host)
    if m and len(parts) >= 3 and parts[0] == "recruiting":
        tenant, site, rest = parts[1], parts[2], parts[3:]
        job = "/" + "/".join(rest) if rest[:1] == ["job"] else ""
        return AtsRef("workday", workday_slug(f"{tenant}.{m.group(1)}.myworkdayjobs.com", tenant, site), job)
    return None


_EMBED_PATTERNS = [
    re.compile(r"https?://(?:boards|job-boards)\.greenhouse\.io/embed/job_(?:board|app)[^\"'\s<>]*", re.I),
    re.compile(r"https?://(?:boards|job-boards)\.greenhouse\.io/[a-z0-9_-]+(?:/jobs/\d+)?", re.I),
    re.compile(r"https?://jobs\.(?:eu\.)?lever\.co/[a-z0-9_.-]+(?:/" + _UUID + ")?", re.I),
    re.compile(r"https?://jobs\.ashbyhq\.com/[a-z0-9_.%-]+(?:/" + _UUID + ")?", re.I),
    re.compile(r"https?://[a-z0-9-]+\.wd\d+\.myworkdayjobs\.com/[^\"'\s<>]+", re.I),
]
_SCRIPT_SLUG_PATTERNS = [
    (re.compile(r"greenhouse\.io/embed/job_board/js\?for=([a-z0-9_-]+)", re.I), "greenhouse"),
    (re.compile(r"grnhse_board\s*=\s*['\"]([a-z0-9_-]+)", re.I), "greenhouse"),
    (re.compile(r"jobs\.ashbyhq\.com/([a-z0-9_.%-]+)/embed", re.I), "ashby"),
]


_NOT_SLUGS = {"embed", "v1", "badges", "static", "assets", "favicon.ico", "robots.txt", "api", "jobs", "careers"}


def find_ats_refs(text: str) -> list[AtsRef]:
    """ATS references embedded in a careers page's HTML or in URLs it loaded."""
    refs: list[AtsRef] = []
    for rx in _EMBED_PATTERNS:
        for m in rx.finditer(text):
            ref = parse_ats_url(m.group(0).replace("&amp;", "&"))
            if ref and ref.slug and ref.slug.lower() not in _NOT_SLUGS:
                refs.append(ref)
    for rx, ats in _SCRIPT_SLUG_PATTERNS:
        refs.extend(AtsRef(ats, m.group(1)) for m in rx.finditer(text))
    seen: set[tuple[str, str, str]] = set()
    unique = []
    for r in sorted(refs, key=lambda r: not r.job_id):
        if (r.ats, r.slug, r.job_id) not in seen:
            seen.add((r.ats, r.slug, r.job_id))
            unique.append(r)
    return unique


def job_id_hint(url: str) -> str:
    """Company careers pages that wrap an ATS usually carry the job id (gh_jid=..., /jobs/123)."""
    u = urlparse(url)
    qs = parse_qs(u.query)
    for key in ("gh_jid", "ashby_jid", "lever-jid", "jobId", "job_id", "jid"):
        if qs.get(key):
            return qs[key][0]
    m = re.search(r"/(\d{6,})(?:[/?#]|$)", u.path)
    return m.group(1) if m else ""


async def discover(url: str, client: httpx.AsyncClient, use_browser: bool = True) -> list[AtsRef]:
    """Find which ATS board powers a careers page: static HTML first, then a headless browser."""
    direct = parse_ats_url(url)
    if direct:
        return [direct]
    hint = job_id_hint(url)
    refs: list[AtsRef] = []
    try:
        r = await client.get(url, headers={"User-Agent": HEADERS["User-Agent"]}, follow_redirects=True)
        final = parse_ats_url(str(r.url))
        if final:
            return [final]
        refs = find_ats_refs(r.text)
    except Exception:
        pass
    if not refs and use_browser:
        refs = await _discover_with_browser(url)
    if hint:
        # Links to *other* jobs on the page are noise; the job is the one named in the URL.
        refs = [AtsRef(r.ats, r.slug, hint) for r in refs if not r.job_id or r.job_id == hint]
    elif len({r.job_id for r in refs if r.job_id}) > 1:
        # A careers page listing many jobs identifies the board, not one job.
        refs = [AtsRef(r.ats, r.slug, "") for r in refs]
    seen: set[tuple[str, str, str]] = set()
    return [r for r in refs if (k := (r.ats, r.slug, r.job_id)) not in seen and not seen.add(k)]


async def _discover_with_browser(url: str) -> list[AtsRef]:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return []
    seen_urls: list[str] = []
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                page.on("request", lambda req: seen_urls.append(req.url))
                await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                try:
                    await page.wait_for_load_state("networkidle", timeout=10_000)
                except Exception:
                    pass
                seen_urls.extend(f.url for f in page.frames)
                seen_urls.append(page.url)
                html_text = await page.content()
            finally:
                await browser.close()
    except Exception:
        return []
    return find_ats_refs("\n".join(seen_urls) + "\n" + html_text)


def slug_candidates(name: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+", name.lower().replace("&", " and "))
    if not words:
        return []
    filler = {"inc", "llc", "ltd", "corp", "corporation", "co", "company", "the", "technologies", "technology", "labs", "hq"}
    core = [w for w in words if w not in filler] or words
    cands = ["".join(words), "".join(core), "-".join(core), "".join(core) + "hq", "".join(core) + "inc", core[0]]
    if len(core) > 1:
        cands.append("".join(core[:2]))
    return list(dict.fromkeys(c for c in cands if len(c) >= 2))


async def probe(client: httpx.AsyncClient, ats: str, slug: str) -> int:
    """Number of open jobs on a board, or -1 if it doesn't exist."""
    try:
        return len(await fetch_board(client, Board(ats, slug)))
    except BoardNotFound:
        return -1
    except Exception:
        return -1


async def board_exists(client: httpx.AsyncClient, ats: str, slug: str) -> tuple[bool, str]:
    """Cheap existence check plus the board's display name where the ATS exposes one.
    Transient failures are retried once so a rate-limit blip isn't mistaken for 'no board'."""
    for attempt in range(2):
        try:
            if ats == "greenhouse":
                d = await _get_json(client, f"https://boards-api.greenhouse.io/v1/boards/{slug}")
                return True, d.get("name") or ""
            if ats in ("lever", "lever_eu"):
                d = await _get_json(client, f"{_lever_base(ats)}/v0/postings/{slug}", params={"mode": "json", "limit": 1})
                return isinstance(d, list), (await board_display_name(client, ats, slug) if isinstance(d, list) else "")
            if ats == "ashby":
                await fetch_board(client, Board(ats, slug))
                return True, await board_display_name(client, ats, slug)
            return False, ""
        except BoardNotFound:
            return False, ""
        except Exception:
            if attempt == 0:
                await asyncio.sleep(0.8)
    return False, ""


def _name_key(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def names_match(display: str, wanted: str) -> bool:
    a, b = _name_key(display), _name_key(wanted)
    return bool(a and b) and (a == b or a.startswith(b) or b.startswith(a))


async def board_display_name(client: httpx.AsyncClient, ats: str, slug: str) -> str:
    if ats == "greenhouse":
        return await greenhouse_board_name(client, slug)
    page = {
        "ashby": f"https://jobs.ashbyhq.com/{slug}",
        "lever": f"https://jobs.lever.co/{slug}",
        "lever_eu": f"https://jobs.eu.lever.co/{slug}",
    }.get(ats)
    if not page:
        return ""
    try:
        r = await client.get(page, headers={"User-Agent": HEADERS["User-Agent"]})
        if r.status_code != 200:
            return ""
        m = re.search(r"<title>\s*([^<]+?)\s*</title>", r.text, re.I)
        title = m.group(1) if m else ""
        title = re.sub(r"^(jobs|careers)\s+at\s+|\s+(jobs|careers)$|\s*[|–-].*$", "", title.strip(), flags=re.I)
        return "" if title.lower() in {"jobs", "careers", ""} else title.strip()
    except Exception:
        return ""


async def best_board_for_name(client: httpx.AsyncClient, name: str) -> tuple[str, str, int, str] | None:
    """(ats, slug, job_count, display_name) of the board most likely to belong to `name`.

    Slug guesses are tried most-specific first ("lucidmotors" before "lucid"); a board whose
    display name contradicts the requested name is rejected so "Lucid Motors" never lands on
    another company's "lucid" board."""
    fallback: tuple[str, str, str] | None = None
    for slug in slug_candidates(name):
        results = await asyncio.gather(*(board_exists(client, ats, slug) for ats in BOARD_ATS))
        for ats, (exists, display) in zip(BOARD_ATS, results):
            if not exists:
                continue
            if display and names_match(display, name):
                return ats, slug, await probe(client, ats, slug), display
            if not display and fallback is None:
                fallback = (ats, slug, display)
    if fallback:
        ats, slug, display = fallback
        return ats, slug, await probe(client, ats, slug), display
    return None
