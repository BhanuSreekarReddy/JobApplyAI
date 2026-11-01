from __future__ import annotations

import html
import re
from typing import Any

from bs4 import BeautifulSoup


def plain_text(markup: str, limit: int = 4000) -> str:
    if not markup:
        return ""
    if "&lt;" in markup and "<" not in markup:
        markup = html.unescape(markup)
    soup = BeautifulSoup(markup, "lxml")
    return re.sub(r"\s+", " ", soup.get_text(" ", strip=True))[:limit]


def make_job(
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
    posted: str = "",
    **extra: Any,
) -> dict[str, Any]:
    return {
        "external_id": str(external_id),
        "title": html.unescape(title or "").strip(),
        "company": html.unescape(company or "").strip(),
        "location": (location or "").strip() or ("Remote" if remote else ""),
        "source": source,
        "url": url,
        "apply_url": apply_url or url,
        "apply_email": apply_email,
        "description": plain_text(description),
        "remote": remote,
        "posted": posted,
        **extra,
    }
