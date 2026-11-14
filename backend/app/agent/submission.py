"""Detect a real submission on an employer's form and mark it applied.

The filler never submits anything itself: the user reviews the filled
form and clicks the employer's Submit button. Until now the user then
had to ALSO click "Mark submitted" back in the app — double bookkeeping
for one action. This module watches pages the user is submitting from
(locally filled tabs and streamed pre-fill sessions) for the employer's
own confirmation, and when one is found it marks the application
submitted through the same code path the manual button uses
(`seen.mark_application_submitted`).

Conservative by design — a false "applied" is far worse than a miss:

- Only strong confirmation phrases count ("application has been
  submitted", "thank you for applying", ...). A "Submit" button or a
  "Submit your application" heading is the form asking, not the site
  confirming, and never triggers.
- A confirmation-style URL counts only together with the application
  form no longer being present on the page.
- Only visible page text is read; hidden markup cannot trigger.
- Emailed applications are never touched: they have no browser form,
  and the ones the email flow sent are already "applied".
- The manual "Mark submitted" button stays as the fallback for sites
  whose confirmation this does not recognise.
"""
from __future__ import annotations

import asyncio
import re
import time
from typing import Any
from urllib.parse import urlsplit

from ..db import SessionLocal
from ..models import Application, JobMatch
from .seen import mark_application_submitted

# How often a watched page is checked, and how long watching continues.
# The timeout is deliberately generous: a user may leave a filled tab
# open for a long while before they finish and submit it.
POLL_INTERVAL_S = 3.0
WATCH_TIMEOUT_S = 2 * 60 * 60.0
# An application row can appear just AFTER its page was registered (the
# apply-from-link flow fills first, then creates the packet). A detected
# confirmation is retried against the database for this long before the
# watcher gives up on it.
APPLICATION_GRACE_S = 60.0

# Application statuses a browser submission can plausibly complete.
# Anything else (skipped, failed, already applied) is a settled outcome
# the detector must not rewrite.
_WATCHABLE_STATUSES = {"pending_review", "prepared", "filled"}

_CONFIRMATION_PHRASES = (
    "application has been submitted",
    "application submitted",
    "thank you for applying",
    "we have received your application",
    "we've received your application",
    "successfully submitted",
    "your application is complete",
)

_CONFIRMATION_URL_RE = re.compile(r"confirmation|thank[-_ ]?you|submitted|success", re.I)

_PAGE_STATE_JS = r"""
() => {
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const parts = [];
  if (document.body) {
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    let node;
    while ((node = walker.nextNode())) {
      const parent = node.parentElement;
      if (!parent) continue;
      if (parent.closest('script,style,noscript,select,textarea,option,button')) continue;
      if (parent.checkVisibility && !parent.checkVisibility()) continue;
      parts.push(node.nodeValue);
    }
  }
  return {
    text: clean(parts.join(' ')).slice(0, 12000),
    has_form: !!document.querySelector('form'),
  };
}
"""


def _normalize(text: str) -> str:
    """Lowercase, unify apostrophes, and drop them, so "We've" in a page
    matches the phrase list however the site punctuates it."""
    text = (text or "").replace("’", "'").replace("‘", "'").lower()
    text = text.replace("'", "")
    return re.sub(r"\s+", " ", text).strip()


async def detect_submission(page: Any) -> str | None:
    """Return the confirmation evidence visible on `page`, or None.

    The evidence is the matched confirmation phrase, or the fixed text
    "confirmation-style URL" when the URL-plus-no-form signal fired (the
    URL itself is never recorded — it can carry tokens)."""
    if page is None:
        return None
    try:
        if page.is_closed():
            return None
    except Exception:
        pass
    texts: list[str] = []
    has_form = False
    try:
        frames = list(page.frames)
    except Exception:
        return None
    for frame in frames:
        try:
            state = await frame.evaluate(_PAGE_STATE_JS)
        except Exception:
            continue
        if not isinstance(state, dict):
            continue
        if state.get("text"):
            texts.append(str(state["text"]))
        has_form = has_form or bool(state.get("has_form"))
    visible = _normalize(" ".join(texts))
    if visible:
        for phrase in _CONFIRMATION_PHRASES:
            if _normalize(phrase) in visible:
                return phrase
    try:
        url = page.url or ""
    except Exception:
        url = ""
    if url:
        parts = urlsplit(url)
        haystack = f"{parts.path} {parts.query} {parts.fragment}"
        if _CONFIRMATION_URL_RE.search(haystack) and not has_form:
            return "confirmation-style URL"
    return None


def _mark_detected(db, application, evidence: str) -> str:
    """One detected-confirmation marking attempt. Outcomes: "marked",
    "already_applied", "ineligible" (a settled or emailed application),
    or "missing" (no application row yet)."""
    if application is None:
        return "missing"
    if application.status == "applied":
        return "already_applied"
    if (application.channel or "") in {"email", "mailto"}:
        return "ineligible"
    if application.status not in _WATCHABLE_STATUSES:
        return "ineligible"
    job = db.get(JobMatch, application.job_id) if application.job_id else None
    return "marked" if mark_application_submitted(db, application, job, evidence=evidence) else "ineligible"


def mark_detected_submission(application_id: int, evidence: str) -> str:
    """Mark one application from a detected confirmation, by id."""
    if not application_id or not evidence:
        return "missing"
    db = SessionLocal()
    try:
        return _mark_detected(db, db.get(Application, application_id), evidence)
    finally:
        db.close()


def mark_detected_submission_for_packet(packet_key: str, evidence: str) -> str:
    """Mark the application a locally filled page belongs to, by packet
    key ("<run_id>-<job_id>"). The newest matching packet wins."""
    if not packet_key or not evidence:
        return "missing"
    run_id, sep, job_id = packet_key.rpartition("-")
    if not sep or not job_id.isdigit():
        return "missing"
    db = SessionLocal()
    try:
        application = (
            db.query(Application)
            .filter(Application.run_id == run_id, Application.job_id == int(job_id))
            .order_by(Application.id.desc())
            .first()
        )
        return _mark_detected(db, application, evidence)
    finally:
        db.close()


def _page_is_closed(page: Any) -> bool:
    try:
        return bool(page.is_closed())
    except Exception:
        return False


async def watch_page_for_submission(
    page: Any,
    *,
    application_id: int = 0,
    packet_key: str = "",
    interval_s: float | None = None,
    timeout_s: float | None = None,
) -> str | None:
    """Poll `page` until the employer's confirmation appears, then mark
    the application submitted. Returns the evidence on success.

    Stops quietly (None) when the page closes, the watch times out, or
    the application turns out to be one the detector must not touch.
    A confirmation whose application row has not appeared yet (the
    apply-from-link flow creates it just after the fill) is retried for
    APPLICATION_GRACE_S before being dropped."""
    if page is None or (not application_id and not packet_key):
        return None
    interval = POLL_INTERVAL_S if interval_s is None else interval_s
    timeout = WATCH_TIMEOUT_S if timeout_s is None else timeout_s
    deadline = time.monotonic() + timeout
    pending: str | None = None
    pending_since = 0.0
    while time.monotonic() < deadline:
        await asyncio.sleep(interval)
        if _page_is_closed(page):
            return None
        if pending is None:
            try:
                pending = await detect_submission(page)
            except Exception:
                pending = None
            if pending is not None:
                pending_since = time.monotonic()
        if pending is None:
            continue
        if application_id:
            outcome = mark_detected_submission(application_id, pending)
        else:
            outcome = mark_detected_submission_for_packet(packet_key, pending)
        if outcome in {"marked", "already_applied"}:
            return pending
        if outcome == "ineligible":
            return None
        # "missing": the packet may not have been created yet.
        if time.monotonic() - pending_since > APPLICATION_GRACE_S:
            return None
    return None
