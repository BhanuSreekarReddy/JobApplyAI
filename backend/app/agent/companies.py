"""Registry of companies whose ATS boards are searched, and resolution of names/links to boards."""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from sqlalchemy.orm import Session

from ..models import Company
from . import ats
from .ats import ATS_LABELS, AtsRef, Board, BoardNotFound
from .normalize import make_job, plain_text

STARTER_FILE = Path(__file__).resolve().parents[1] / "data" / "companies.json"
_GENERIC_HOST_PARTS = {"www", "careers", "jobs", "job", "apply", "work", "boards", "en", "us", "about", "team", "join"}
# Job aggregators run their own (unrelated) hiring boards, so their domain never names the employer.
AGGREGATOR_DOMAINS = {
    "linkedin", "indeed", "ziprecruiter", "glassdoor", "monster", "dice", "simplyhired", "careerbuilder",
    "wellfound", "angel", "builtin", "google", "lever", "greenhouse", "ashbyhq", "myworkdayjobs", "workday",
    "remotive", "jobicy", "himalayas", "arbeitnow", "adzuna", "usajobs", "otta", "welcometothejungle",
    "workatastartup", "ycombinator", "hired", "flexjobs", "weworkremotely", "remoteok", "joinhandshake",
}


class ResolveError(ValueError):
    pass


def sync_starter(db: Session) -> int:
    """Add starter companies that aren't in the database yet. Never re-enables ones the user removed."""
    if not STARTER_FILE.exists():
        return 0
    rows = json.loads(STARTER_FILE.read_text())
    existing = {(c.ats, c.slug) for c in db.query(Company.ats, Company.slug)}
    added = 0
    for row in rows:
        if (row["ats"], row["slug"]) in existing:
            continue
        db.add(Company(name=row["name"], ats=row["ats"], slug=row["slug"], source="starter"))
        added += 1
    if added:
        db.commit()
    return added


def enabled_boards(db: Session, user_id: int | None = None) -> list[tuple[Company, Board]]:
    query = db.query(Company).filter(Company.enabled == 1)
    if user_id is not None:
        # A user's searches cover the global starter list plus their own adds.
        query = query.filter((Company.user_id.is_(None)) | (Company.user_id == user_id))
    rows = query.order_by(Company.name).all()
    return [(c, Board(c.ats, c.slug, c.name)) for c in rows]


def payload(c: Company) -> dict[str, Any]:
    return {
        "id": c.id,
        "name": c.name,
        "ats": c.ats,
        "ats_label": ATS_LABELS.get(c.ats, c.ats),
        "slug": c.slug,
        "board_url": board_url(c.ats, c.slug),
        "source": c.source,
        "enabled": bool(c.enabled),
        "status": c.status,
        "job_count": c.job_count,
        "note": c.note,
        "last_checked": c.last_checked.isoformat() if c.last_checked else None,
    }


def board_url(ats_name: str, slug: str) -> str:
    if ats_name == "greenhouse":
        return f"https://job-boards.greenhouse.io/{slug}"
    if ats_name == "lever":
        return f"https://jobs.lever.co/{slug}"
    if ats_name == "lever_eu":
        return f"https://jobs.eu.lever.co/{slug}"
    if ats_name == "ashby":
        return f"https://jobs.ashbyhq.com/{slug}"
    if ats_name == "workday":
        host, _tenant, site = ats.workday_parts(slug)
        return f"https://{host}/{site}"
    return ""


def _is_url(text: str) -> bool:
    return bool(re.match(r"^(https?://|www\.)", text.strip(), re.I)) or bool(
        re.match(r"^[\w-]+(\.[\w-]+)+(/|$)", text.strip())
    )


def _normalize_url(text: str) -> str:
    text = text.strip()
    return text if re.match(r"^https?://", text, re.I) else f"https://{text}"


def domain_name_guess(url: str) -> str:
    """careers.lucidmotors.com -> 'lucidmotors'; stripe.com/jobs -> 'stripe'."""
    host = (urlparse(url).hostname or "").lower()
    labels = [l for l in host.split(".") if l]
    if len(labels) >= 2:
        labels = labels[:-1]
        if len(labels) >= 2 and labels[-1] in {"co", "com"}:
            labels = labels[:-1]
    meaningful = [l for l in labels if l not in _GENERIC_HOST_PARTS]
    return meaningful[-1] if meaningful else ""


async def _verified(client: httpx.AsyncClient, refs: list[AtsRef]) -> tuple[AtsRef, str] | None:
    for ref in refs:
        if ref.ats == "workday":
            try:
                await ats.fetch_board(client, Board("workday", ref.slug), "")
                return ref, ""
            except Exception:
                continue
        exists, display = await ats.board_exists(client, ref.ats, ref.slug)
        if exists:
            return ref, display
    return None


async def resolve_input(client: httpx.AsyncClient, text: str, use_browser: bool = True) -> dict[str, Any]:
    """Resolve what the user typed (company name, board link, careers page, or job link) to a board."""
    text = (text or "").strip()
    if not text:
        raise ResolveError("Enter a company name or a careers/job link.")
    if _is_url(text):
        url = _normalize_url(text)
        direct = ats.parse_ats_url(url)
        refs = [direct] if direct else await ats.discover(url, client, use_browser=use_browser)
        found = await _verified(client, refs)
        if found:
            ref, display = found
            name = display or await ats.board_display_name(client, ref.ats, ref.slug) or _workday_name(ref) or ref.slug
            return {"name": name, "ats": ref.ats, "slug": ref.slug, "job_id": ref.job_id}
        if direct:
            label = ATS_LABELS.get(direct.ats, direct.ats)
            raise ResolveError(
                f"{label} has no job board at “{direct.slug}” right now (or it didn't respond). "
                "Check the link, or try again in a minute."
            )
        guess = domain_name_guess(url)
        if guess and guess not in AGGREGATOR_DOMAINS:
            best = await ats.best_board_for_name(client, guess)
            if best:
                ats_name, slug, _count, display = best
                return {
                    "name": display or guess.title(), "ats": ats_name, "slug": slug,
                    "job_id": ats.job_id_hint(url), "guessed": True,
                }
        raise ResolveError(
            "Couldn't find a Greenhouse, Lever, Ashby, or Workday job board behind that link. "
            "Try the company's careers page or its name."
        )
    best = await ats.best_board_for_name(client, text)
    if not best:
        raise ResolveError(
            f"No Greenhouse, Lever, or Ashby board found for “{text}”. "
            "Paste the company's careers page link instead (Workday sites work too)."
        )
    ats_name, slug, _count, display = best
    return {"name": display or text, "ats": ats_name, "slug": slug, "job_id": ""}


def _workday_name(ref: AtsRef) -> str:
    if ref.ats != "workday":
        return ""
    _host, tenant, _site = ats.workday_parts(ref.slug)
    return tenant.replace("-", " ").title()


async def add_company(
    db: Session, client: httpx.AsyncClient, text: str, use_browser: bool = True, user_id: int | None = None
) -> tuple[Company, bool]:
    """Resolve and store a company. Returns (company, created). Companies are
    looked up among the rows the user can see (global + their own)."""
    visible = db.query(Company)
    if user_id is not None:
        visible = visible.filter((Company.user_id.is_(None)) | (Company.user_id == user_id))
    key = re.sub(r"[^a-z0-9]", "", (text or "").lower())
    known = None
    if key and not _is_url(text):
        known = next((c for c in visible.all() if re.sub(r"[^a-z0-9]", "", c.name.lower()) == key), None)
    info = (
        {"name": known.name, "ats": known.ats, "slug": known.slug}
        if known
        else await resolve_input(client, text, use_browser=use_browser)
    )
    row = (
        visible.filter(Company.ats == info["ats"], Company.slug == info["slug"]).first()
    )
    created = row is None
    if row is None:
        row = Company(name=info["name"], ats=info["ats"], slug=info["slug"], source="user", user_id=user_id)
        db.add(row)
    elif row.user_id is None and user_id is not None:
        # A regular user re-adding a global (starter) board must not mutate
        # the shared row; only admins manage global companies.
        return row, False
    row.enabled = 1
    try:
        jobs = await ats.fetch_board(client, Board(row.ats, row.slug, row.name), "")
        row.job_count, row.status, row.note = len(jobs), "ok", ""
    except Exception as exc:
        row.status, row.note = "error", f"Board check failed: {exc}"[:200]
    row.last_checked = datetime.utcnow()
    db.commit()
    db.refresh(row)
    return row, created


async def relocate(db: Session, client: httpx.AsyncClient, company: Company) -> Board | None:
    """A board returned 404: the company probably switched ATS. Look for it elsewhere."""
    old = ATS_LABELS.get(company.ats, company.ats)
    for ats_name in ats.BOARD_ATS:
        if ats_name == company.ats:
            continue
        exists, display = await ats.board_exists(client, ats_name, company.slug)
        if exists and (not display or ats.names_match(display, company.name)):
            return _moved(db, company, ats_name, company.slug, old)
    best = await ats.best_board_for_name(client, company.name)
    if best and (best[0], best[1]) != (company.ats, company.slug):
        return _moved(db, company, best[0], best[1], old)
    company.status = "not_found"
    company.note = f"No longer has a {old} board, and no other board was found. Remove it or re-add it with its careers link."
    company.last_checked = datetime.utcnow()
    db.commit()
    return None


def _moved(db: Session, company: Company, ats_name: str, slug: str, old: str) -> Board | None:
    clash = db.query(Company).filter(Company.ats == ats_name, Company.slug == slug, Company.id != company.id).first()
    if clash:
        company.enabled, company.status = 0, "not_found"
        company.note = f"Moved off {old}; already tracked as {clash.name}."
        db.commit()
        return None
    company.ats, company.slug, company.status = ats_name, slug, "ok"
    company.note = f"Moved from {old} to {ATS_LABELS.get(ats_name, ats_name)}."
    company.last_checked = datetime.utcnow()
    db.commit()
    return Board(company.ats, company.slug, company.name)


async def resolve_job_url(client: httpx.AsyncClient, url: str, use_browser: bool = True) -> dict[str, Any]:
    """A single job from a pasted link: ATS job pages, careers pages that wrap an ATS, or any page."""
    url = _normalize_url(url)
    try:
        info = await resolve_input(client, url, use_browser=use_browser)
    except ResolveError:
        info = None
    if info and info.get("guessed"):
        # A board guessed from the domain only counts if this exact job id exists on it.
        if info.get("job_id"):
            try:
                job = await ats.fetch_job(client, info["ats"], info["slug"], info["job_id"], info["name"])
                if job:
                    return {**job, "board": info}
            except Exception:
                pass
        return await _generic_job(client, url)
    if info and info.get("job_id"):
        try:
            job = await ats.fetch_job(client, info["ats"], info["slug"], info["job_id"], info["name"])
        except BoardNotFound:
            raise ResolveError("That job is no longer open on the company's job board.")
        if job:
            return {**job, "url": url if not job.get("url") else job["url"], "board": info}
    if info and not info.get("job_id"):
        raise ResolveError(
            f"That link is {info['name']}'s careers page, not a specific job. "
            "Open the job you want and paste its link, or add the company on the Companies tab."
        )
    return await _generic_job(client, url)


async def _generic_job(client: httpx.AsyncClient, url: str) -> dict[str, Any]:
    """Unknown site: take the title/company from the page so a packet can still be prepared."""
    try:
        r = await client.get(url, headers={"User-Agent": ats.HEADERS["User-Agent"]}, follow_redirects=True)
        page = r.text
    except Exception as exc:
        raise ResolveError(f"Couldn't open that link: {exc}") from exc
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(page, "lxml")

    def meta(prop: str) -> str:
        tag = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
        return (tag.get("content") or "").strip() if tag else ""

    title = meta("og:title") or (soup.title.string.strip() if soup.title and soup.title.string else "")
    site = meta("og:site_name") or domain_name_guess(url).title()
    company, location = site, ""
    if re.sub(r"[^a-z]", "", site.lower()).removesuffix("com") in AGGREGATOR_DOMAINS or domain_name_guess(url) in AGGREGATOR_DOMAINS:
        # Aggregator pages name the employer in the title, not the site name.
        title = re.sub(r"\s*[|–-]\s*(" + re.escape(site) + r"|indeed\.com|linkedin)\s*$", "", title, flags=re.I)
        company = ""
        m = re.match(r"^(.+?) hiring (.+?)(?: in (.+))?$", title)
        if m:
            company, title, location = m.group(1), m.group(2), m.group(3) or ""
        else:
            parts = [p.strip() for p in re.split(r"\s+-\s+", title)]
            if len(parts) >= 2:
                title, company = parts[0], parts[1]
                location = parts[2] if len(parts) > 2 else ""
        if not company or title.strip().lower() == site.strip().lower():
            raise ResolveError(
                f"Couldn't read the job from that {site} page (it may need you to sign in). "
                "Open the job on the employer's own careers site and paste that link instead."
            )
    elif title and site and title.lower().endswith(site.lower()):
        title = re.sub(r"\s*[-|–@]\s*" + re.escape(site) + r"$", "", title, flags=re.I)
    if not title:
        raise ResolveError("Couldn't read a job title from that page.")
    return make_job(
        external_id=f"link-{abs(hash(url))}",
        title=title[:200],
        company=company,
        location=location,
        source="Link",
        url=url,
        description=plain_text(meta("og:description") + " " + page),
    )
