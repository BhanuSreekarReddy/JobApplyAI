from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import httpx

from ..config import settings
from . import ats
from .ats import Board, BoardNotFound
from .locations import Wanted, countries_in, match_location
from .normalize import make_job as _job
from .normalize import plain_text as _plain

HEADERS = {"User-Agent": "JobApplyAgent/1.0 (personal job search)"}
BOARD_CONCURRENCY = 24
DETAIL_CONCURRENCY = 16
MAX_CANDIDATES = 200

# A job title must belong to one of the candidate's role families to be considered at all.
FAMILY_PATTERNS: list[tuple[re.Pattern[str], re.Pattern[str]]] = [
    (
        re.compile(r"engineer|developer|full ?stack|front ?end|back ?end|devops|sre|ios|android|mobile|programmer"),
        re.compile(r"\b(engineer(ing)?|developer|swe|sde|programmer|sre|architect|member of technical staff|mts)\b", re.I),
    ),
    (re.compile(r"scientist|research"), re.compile(r"\b(scientist|researcher|research)\b", re.I)),
    (re.compile(r"analyst"), re.compile(r"\banalyst\b", re.I)),
    (re.compile(r"manager"), re.compile(r"\b(manager|head of|director|lead)\b", re.I)),
    (re.compile(r"designer"), re.compile(r"\bdesign(er)?\b", re.I)),
]
OFF_FAMILY = re.compile(
    r"\b(sales|account executive|recruit(er|ing)|marketing|attorney|counsel|paralegal|nurse|driver|technician|"
    r"customer success|support specialist)\b",
    re.I,
)
SENIORITY_WORDS = {"senior", "sr", "staff", "principal", "lead", "junior", "jr", "mid", "level", "i", "ii", "iii", "iv"}


@dataclass
class SearchResult:
    jobs: list[dict[str, Any]] = field(default_factory=list)
    boards_searched: int = 0
    scanned: int = 0
    title_matches: int = 0
    location_matches: int = 0
    missing: list[Board] = field(default_factory=list)
    failed: list[Board] = field(default_factory=list)
    board_counts: dict[str, int] = field(default_factory=dict)
    by_source: dict[str, int] = field(default_factory=dict)


def _title_words(titles: list[str]) -> set[str]:
    words: set[str] = set()
    for t in titles:
        words |= {w for w in re.findall(r"[a-z]+", t.lower()) if w not in SENIORITY_WORDS and len(w) > 1}
    return words


def title_relevance(title: str, profile_titles: list[str], skills: list[str]) -> float:
    """0 when the job is outside the candidate's role families; higher for closer title matches."""
    t = (title or "").lower()
    titles = [p.lower() for p in profile_titles] or ["software engineer"]
    family_ok = any(
        want.search(" ".join(titles)) and job_rx.search(t) for want, job_rx in FAMILY_PATTERNS
    )
    if not family_ok:
        return 0.0
    if OFF_FAMILY.search(t) and not re.search(r"\b(software|platform|backend|frontend|data)\b", t):
        return 0.0
    if re.search(r"\bsales engineer|solutions? engineer|field engineer|support engineer\b", t):
        family_ok_score = 0.5
    else:
        family_ok_score = 1.0
    words = set(re.findall(r"[a-z+#.]+", t))
    overlap = len(_title_words(titles) & words)
    skill_hits = sum(1 for s in skills[:20] if len(s) > 2 and re.search(rf"(?<![\w]){re.escape(s.lower())}(?![\w])", t))
    return family_ok_score + overlap + 0.5 * skill_hits


async def search_jobs(
    profile: dict[str, Any],
    wanted: Wanted,
    boards: list[Board],
    progress: Callable[[str], Awaitable[None]] | None = None,
) -> SearchResult:
    titles = profile.get("titles") or []
    skills = profile.get("skills") or []
    query = " ".join((titles[:2] + skills[:3])).strip() or "software engineer"
    board_query = (titles[0] if titles else "software engineer")
    result = SearchResult(boards_searched=len(boards))
    sem = asyncio.Semaphore(BOARD_CONCURRENCY)

    async def one_board(board: Board) -> list[dict[str, Any]]:
        async with sem:
            try:
                jobs = await ats.fetch_board(client, board, board_query)
                result.board_counts[board.key] = len(jobs)
                return jobs
            except BoardNotFound:
                result.missing.append(board)
            except Exception:
                result.failed.append(board)
            return []

    limits = httpx.Limits(max_connections=BOARD_CONCURRENCY * 2, max_keepalive_connections=BOARD_CONCURRENCY)
    async with httpx.AsyncClient(timeout=25.0, headers=HEADERS, follow_redirects=True, limits=limits) as client:
        if progress and boards:
            await progress(f"Searching {len(boards)} company job boards (Greenhouse, Lever, Ashby, Workday) and public job sites")
        tasks: list[Awaitable[list[dict[str, Any]]]] = [one_board(b) for b in boards]
        tasks += [_remotive(client, query), _jobicy(client, query), _himalayas(client), _arbeitnow(client)]
        if settings.adzuna_app_id and settings.adzuna_app_key:
            # Adzuna and USAJobs AND their keywords together, so search by title only and let scoring weigh skills.
            tasks += [_adzuna(client, board_query, p) for p in _adzuna_places(wanted)]
        if settings.usajobs_api_key and (not wanted.places or "us" in wanted.home_countries):
            tasks.append(_usajobs(client, board_query, _usajobs_location(wanted)))
        batches = await asyncio.gather(*tasks)
        jobs = _dedupe([j for batch in batches for j in batch])
        result.scanned = len(jobs)

        relevant = []
        for job in jobs:
            rel = title_relevance(job["title"], titles, skills)
            if rel > 0:
                relevant.append({**job, "relevance": rel})
        result.title_matches = len(relevant)

        multi = [j for j in relevant if j.get("multi_location")]
        if multi:
            resolved = await _with_details(client, multi[:60])
            by_id = {j["external_id"]: j for j in resolved}
            relevant = [by_id.get(j["external_id"], j) for j in relevant]

        located = []
        for job in relevant:
            kind, reason = match_location(job["location"], job["remote"], wanted)
            if kind:
                located.append({**job, "location_match": kind, "location_reason": reason})
        located.sort(key=lambda j: (j["relevance"], j.get("posted") or ""), reverse=True)
        located = _dedupe(located, by_location=False)
        result.location_matches = len(located)
        top = located[:MAX_CANDIDATES]
        if progress and any(j.get("needs_details") for j in top):
            await progress(f"Reading descriptions for the top {len(top)} of {len(located)} matching roles")
        result.jobs = await _with_details(client, top)

    for job in result.jobs:
        result.by_source[job["source"]] = result.by_source.get(job["source"], 0) + 1
    return result


async def _with_details(client: httpx.AsyncClient, jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sem = asyncio.Semaphore(DETAIL_CONCURRENCY)

    async def one(job: dict[str, Any]) -> dict[str, Any]:
        if not job.get("needs_details"):
            return job
        async with sem:
            return await ats.fetch_details(client, job)

    return list(await asyncio.gather(*(one(j) for j in jobs)))


async def _remotive(client: httpx.AsyncClient, query: str) -> list[dict[str, Any]]:
    try:
        r = await client.get(
            "https://remotive.com/api/remote-jobs",
            params={"search": query, "limit": 30},
        )
        r.raise_for_status()
        data = r.json()
    except Exception:
        return []
    out = []
    for item in data.get("jobs", [])[:40]:
        loc = item.get("candidate_required_location") or "Remote"
        out.append(
            _job(
                external_id=f"remotive-{item.get('id')}",
                title=item.get("title") or "",
                company=item.get("company_name") or "",
                location=loc,
                source="Remotive",
                url=item.get("url") or "",
                description=item.get("description") or "",
                remote=True,
                posted=item.get("publication_date") or "",
            )
        )
    return out


async def _jobicy(client: httpx.AsyncClient, query: str) -> list[dict[str, Any]]:
    try:
        r = await client.get(
            "https://jobicy.com/api/v2/remote-jobs",
            params={"count": 30, "tag": query.split()[0] if query else "dev"},
        )
        r.raise_for_status()
        data = r.json()
    except Exception:
        return []
    out = []
    for item in data.get("jobs", [])[:40]:
        out.append(
            _job(
                external_id=f"jobicy-{item.get('id')}",
                title=item.get("jobTitle") or "",
                company=item.get("companyName") or "",
                location=item.get("jobGeo") or "Remote",
                source="Jobicy",
                url=item.get("url") or "",
                description=item.get("jobDescription") or "",
                apply_url=item.get("url") or "",
                remote=True,
            )
        )
    return out


async def _himalayas(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    try:
        r = await client.get("https://himalayas.app/jobs/api", params={"limit": 40})
        r.raise_for_status()
        data = r.json()
    except Exception:
        return []
    items = data if isinstance(data, list) else data.get("jobs") or data.get("data") or []
    out = []
    for item in items[:40]:
        if not isinstance(item, dict):
            continue
        title = item.get("title") or item.get("jobTitle") or ""
        company = ""
        if isinstance(item.get("company"), dict):
            company = item["company"].get("name") or ""
        else:
            company = item.get("companyName") or item.get("company") or ""
        url = item.get("applicationLink") or item.get("url") or item.get("guid") or ""
        loc = ", ".join(item.get("locationRestrictions") or []) or item.get("location") or "Remote"
        out.append(
            _job(
                external_id=f"himalayas-{item.get('id') or url}",
                title=title,
                company=str(company),
                location=str(loc),
                source="Himalayas",
                url=url,
                description=item.get("description") or "",
                remote=True,
            )
        )
    return out


async def _arbeitnow(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    try:
        r = await client.get("https://www.arbeitnow.com/api/job-board-api")
        r.raise_for_status()
        data = r.json()
    except Exception:
        return []
    out = []
    for item in data.get("data", [])[:50]:
        remote = bool(item.get("remote"))
        out.append(
            _job(
                external_id=f"arbeitnow-{item.get('slug') or item.get('url')}",
                title=item.get("title") or "",
                company=item.get("company_name") or "",
                location=item.get("location") or ("Remote" if remote else ""),
                source="Arbeitnow",
                url=item.get("url") or "",
                description=item.get("description") or "",
                remote=remote,
            )
        )
    return out


ADZUNA_COUNTRIES = {"us", "gb", "ca", "au", "de", "fr", "in", "nl", "sg", "it", "es", "pl", "br", "mx", "nz", "za", "at", "be", "ch"}


@dataclass
class AdzunaQuery:
    country: str
    where: str = ""
    remote: bool = False


def _adzuna_places(wanted: Wanted) -> list[AdzunaQuery]:
    """At most three Adzuna searches: the candidate's cities/states, plus a remote search at home."""
    out: list[AdzunaQuery] = []
    for p in wanted.onsite:
        country = p.country or "us"
        if country not in ADZUNA_COUNTRIES:
            continue
        where = "" if p.kind == "country" else p.label
        out.append(AdzunaQuery(country, where))
    if wanted.remote:
        homes = sorted(wanted.home_countries & ADZUNA_COUNTRIES) or ["us"]
        out.append(AdzunaQuery(homes[0], "", remote=True))
    if not wanted.places:
        out.append(AdzunaQuery("us"))
    return out[:3]


def _usajobs_location(wanted: Wanted) -> str:
    for p in wanted.onsite:
        if p.country == "us" and p.kind in {"city", "state"}:
            return p.label
    return ""


async def _adzuna(client: httpx.AsyncClient, query: str, place: AdzunaQuery) -> list[dict[str, Any]]:
    params: dict[str, Any] = {
        "app_id": settings.adzuna_app_id,
        "app_key": settings.adzuna_app_key,
        "results_per_page": 50,
        "what": f"{query} remote" if place.remote else query,
    }
    if place.where:
        params["where"] = place.where
    try:
        r = await client.get(
            f"https://api.adzuna.com/v1/api/jobs/{place.country}/search/1",
            params=params,
            headers={**HEADERS, "Accept": "application/json"},
        )
        r.raise_for_status()
        data = r.json()
    except Exception:
        return []
    out = []
    for item in data.get("results", [])[:50]:
        loc_obj = item.get("location") or {}
        loc = loc_obj.get("display_name") if isinstance(loc_obj, dict) else str(loc_obj)
        company = (item.get("company") or {}).get("display_name") if isinstance(item.get("company"), dict) else ""
        desc = item.get("description") or ""
        email = ""
        found = re.search(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", desc, re.I)
        if found:
            email = found.group(0)
        remote = place.remote or "remote" in f"{loc} {item.get('title') or ''}".lower()
        if loc and place.country not in countries_in(loc):
            loc = f"{loc}, {place.country.upper()}"
        out.append(
            _job(
                external_id=f"adzuna-{item.get('id')}",
                title=item.get("title") or "",
                company=company or "",
                location=f"Remote ({place.country.upper()})" if remote and not loc else (loc or place.where),
                source="Adzuna",
                url=item.get("redirect_url") or "",
                description=desc,
                apply_email=email,
                remote=remote,
                posted=item.get("created") or "",
            )
        )
    return out


async def _usajobs(client: httpx.AsyncClient, query: str, location: str) -> list[dict[str, Any]]:
    headers = {
        "Host": "data.usajobs.gov",
        "User-Agent": settings.usajobs_user_agent,
        "Authorization-Key": settings.usajobs_api_key,
    }
    params: dict[str, Any] = {"Keyword": query, "ResultsPerPage": 50}
    if location:
        params["LocationName"] = location
    try:
        r = await client.get("https://data.usajobs.gov/api/search", headers=headers, params=params)
        r.raise_for_status()
        data = r.json()
    except Exception:
        return []
    items = (((data.get("SearchResult") or {}).get("SearchResultItems")) or [])
    out = []
    for wrap in items:
        item = wrap.get("MatchedObjectDescriptor") or {}
        loc = ", ".join(
            (p.get("PositionLocationDisplay") or "")
            for p in (item.get("PositionLocation") or [])[:2]
        )
        url = item.get("PositionURI") or ""
        out.append(
            _job(
                external_id=f"usajobs-{item.get('PositionID')}",
                title=item.get("PositionTitle") or "",
                company=(item.get("OrganizationName") or "USAJobs"),
                location=loc,
                source="USAJobs",
                url=url,
                description=item.get("UserArea", {}).get("Details", {}).get("JobSummary")
                if isinstance(item.get("UserArea"), dict)
                else "",
            )
        )
    return out


def _dedupe(jobs: list[dict[str, Any]], by_location: bool = True) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out = []
    for job in jobs:
        parts = f"{job['title']}{job['company']}{job['location'] if by_location else ''}"
        key = re.sub(r"\W+", "", parts.lower())
        if not job["title"] or key in seen:
            continue
        seen.add(key)
        out.append(job)
    return out
