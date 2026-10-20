from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote_plus

import httpx
from bs4 import BeautifulSoup

from ..config import settings

HEADERS = {"User-Agent": "JobApplyAgent/1.0 (personal job search)"}


def _plain(html: str) -> str:
    if not html:
        return ""
    soup = BeautifulSoup(html, "lxml")
    return re.sub(r"\s+", " ", soup.get_text(" ", strip=True))[:4000]


def _job(
    *,
    external_id: str,
    title: str,
    company: str,
    location: str,
    source: str,
    url: str,
    description: str = "",
    apply_url: str = "",
    apply_email: str = "",
    remote: bool = False,
) -> dict[str, Any]:
    return {
        "external_id": str(external_id),
        "title": title.strip(),
        "company": company.strip(),
        "location": location.strip() or ("Remote" if remote else ""),
        "source": source,
        "url": url,
        "apply_url": apply_url or url,
        "apply_email": apply_email,
        "description": _plain(description),
        "remote": remote,
    }


async def search_jobs(titles: list[str], skills: list[str], locations: list[str]) -> list[dict[str, Any]]:
    query = " ".join((titles[:2] + skills[:3])).strip() or "software engineer"
    jobs: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=25.0, headers=HEADERS, follow_redirects=True) as client:
        jobs.extend(await _remotive(client, query))
        jobs.extend(await _jobicy(client, query))
        jobs.extend(await _himalayas(client))
        jobs.extend(await _arbeitnow(client))
        if settings.adzuna_app_id and settings.adzuna_app_key:
            for loc in locations[:3]:
                jobs.extend(await _adzuna(client, query, loc))
        if settings.usajobs_api_key:
            jobs.extend(await _usajobs(client, query, locations[:1]))
    return _dedupe(jobs)


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


COUNTRY_MAP = {
    "united states": "us",
    "usa": "us",
    "us": "us",
    "india": "in",
    "united kingdom": "gb",
    "uk": "gb",
    "canada": "ca",
    "germany": "de",
    "australia": "au",
    "netherlands": "nl",
    "france": "fr",
    "singapore": "sg",
}


def _country_from_location(location: str) -> tuple[str, str]:
    lower = location.lower()
    for name, code in COUNTRY_MAP.items():
        if name in lower:
            city = re.sub(name, "", location, flags=re.I).strip(" ,")
            return code, city or location
    return "us", location


async def _adzuna(client: httpx.AsyncClient, query: str, location: str) -> list[dict[str, Any]]:
    country, where = _country_from_location(location)
    try:
        r = await client.get(
            f"https://api.adzuna.com/v1/api/jobs/{country}/search/1",
            params={
                "app_id": settings.adzuna_app_id,
                "app_key": settings.adzuna_app_key,
                "results_per_page": 20,
                "what": query,
                "where": where,
                "content-type": "application/json",
            },
        )
        r.raise_for_status()
        data = r.json()
    except Exception:
        return []
    out = []
    for item in data.get("results", [])[:20]:
        loc_obj = item.get("location") or {}
        loc = loc_obj.get("display_name") if isinstance(loc_obj, dict) else str(loc_obj)
        company = (item.get("company") or {}).get("display_name") if isinstance(item.get("company"), dict) else ""
        desc = item.get("description") or ""
        email = ""
        found = re.search(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", desc, re.I)
        if found:
            email = found.group(0)
        out.append(
            _job(
                external_id=f"adzuna-{item.get('id')}",
                title=item.get("title") or "",
                company=company or "",
                location=loc or location,
                source="Adzuna",
                url=item.get("redirect_url") or "",
                description=desc,
                apply_email=email,
                remote="remote" in (loc or "").lower() or "remote" in (item.get("title") or "").lower(),
            )
        )
    return out


async def _usajobs(client: httpx.AsyncClient, query: str, locations: list[str]) -> list[dict[str, Any]]:
    headers = {
        "Host": "data.usajobs.gov",
        "User-Agent": settings.usajobs_user_agent,
        "Authorization-Key": settings.usajobs_api_key,
    }
    params: dict[str, Any] = {"Keyword": query, "ResultsPerPage": 20}
    if locations:
        params["LocationName"] = locations[0]
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


def _dedupe(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out = []
    for job in jobs:
        key = re.sub(r"\W+", "", f"{job['title']}{job['company']}".lower())
        if not job["title"] or key in seen:
            continue
        seen.add(key)
        out.append(job)
    return out


def search_url(query: str, location: str) -> str:
    return f"https://www.google.com/search?q={quote_plus(query + ' ' + location + ' jobs')}"
