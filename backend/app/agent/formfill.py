"""Pre-fill employer application forms in a visible browser for the candidate to review.

The filler never clicks a submit button. It fills fields it can map confidently from the
resume profile, uploads the resume, outlines anything that still needs a human, and leaves
the tab open so the candidate can review and submit.
"""
from __future__ import annotations

import asyncio
import re
import threading
from pathlib import Path
from typing import Any

from ..config import BROWSER_PROFILE_DIR, UPLOAD_DIR, settings
from .locations import US_STATES

FILL_TIMEOUT_S = 120
ATTR = "data-jobapply-id"

# Questions the agent must never answer on the candidate's behalf (or can't answer from a resume).
SENSITIVE_RE = re.compile(
    r"gender|\bsex\b|\brace\b|ethnic|hispanic|latin[oax]|veteran|disab|pronoun|orientation|transgender"
    r"|birth|\bage\b|\bssn\b|social security|salary|compensation|\bpay\b|sponsor|visa|authori[sz]"
    r"|work permit|citizen|clearance|criminal|convict|relocat|notice period|start date|\breferr"
    r"|hear about|password|signature|consent|agree|acknowledg|certify|pronunc|phonetic"
)

RULES: list[tuple[str, re.Pattern[str]]] = [
    ("email", re.compile(r"\be ?mail\b")),
    ("first_name", re.compile(r"\b(first|given|fore) ?name\b|\bfname\b")),
    ("last_name", re.compile(r"\b(last|family|sur) ?name\b|\bsurname\b|\blname\b")),
    ("phone", re.compile(r"\b(phone|mobile|cell|telephone)\b")),
    ("linkedin", re.compile(r"linked ?in")),
    ("github", re.compile(r"git ?hub")),
    ("website", re.compile(r"\b(website|portfolio|personal site|homepage|blog)\b")),
    ("cover_letter", re.compile(r"cover ?letter|motivation|why (do )?you want|why are you interested|additional information|anything else")),
    ("years", re.compile(r"years? of (\w+ )?experience|how many years")),
    ("location", re.compile(r"\b(location|city)\b|where are you (located|based)")),
]
FULL_NAME_RE = re.compile(r"^(your |full |legal )?name\b")

# Saved-answer categories. The filler never guesses answers to the questions
# below (most are on the SENSITIVE_RE blocklist); it may fill them ONLY from
# the user's saved answer bank (profile_answers), keyed by these canonical
# keys. Free-form saved answers are keyed by their normalized question label
# instead. EEO/demographic categories are a separate sensitivity class: they
# are saved only when the user explicitly opts in per question.
ANSWER_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("security_clearance", re.compile(r"security clearance|\bclearance\b")),
    ("sponsorship", re.compile(r"sponsor")),
    ("citizenship_status", re.compile(r"citizen")),
    ("work_authorization", re.compile(r"authori[sz]|eligible to work|legally entitled to work|work permit")),
    ("previously_interviewed", re.compile(r"interviewed|prior interview")),
    ("previously_employed", re.compile(r"previously employed|previously worked|former employee|been employed by|ever worked (for|at|here)")),
    ("how_heard", re.compile(r"hear about|how did you (hear|find|learn)|\breferr")),
    ("salary_expectations", re.compile(r"salary|compensation|expected pay|pay expectation|desired pay")),
    ("start_date", re.compile(r"start date|when can you start|earliest.{0,20}start|available to start|notice period")),
    ("work_onsite", re.compile(r"on ?site|work in (the|our) office|in person at (the|our)")),
    ("country", re.compile(r"\bcountry\b")),
    ("gender", re.compile(r"gender|\bsex\b")),
    ("race_ethnicity", re.compile(r"\brace\b|ethnic|hispanic|latin[oax]")),
    ("veteran_status", re.compile(r"veteran")),
    ("disability_status", re.compile(r"disab")),
    ("pronouns", re.compile(r"pronoun")),
]
EEO_ANSWER_KEYS = {"gender", "race_ethnicity", "veteran_status", "disability_status", "pronouns"}
EEO_LABEL_RE = re.compile(
    r"gender|\bsex\b|\brace\b|ethnic|hispanic|latin[oax]|veteran|disab|pronoun|orientation|transgender"
)
# One-time codes and human checks must never be captured into the answer bank.
CODE_LABEL_RE = re.compile(
    r"security code|verification code|one[- ]?time|passcode|\botp\b|captcha|human check|access code"
)


def _classify_answer(f: dict[str, Any]) -> str | None:
    """Canonical answer-bank key for a field, or None. Unlike _classify this
    deliberately matches blocklisted questions — it only ever *labels* a
    field; the value always comes from the user's saved answers."""
    label = f.get("label") or ""
    if not label and not f.get("name"):
        return None
    hay = _norm(label, f.get("placeholder") or "", f.get("name") or "", f.get("idAttr") or "")
    if not hay:
        return None
    for key, rx in ANSWER_RULES:
        if rx.search(hay):
            return key
    return None


def answer_is_sensitive(key: str, question: str = "") -> bool:
    """EEO/demographic answers are a separate sensitivity class: offered for
    saving only as an explicit per-question opt-in."""
    if key in EEO_ANSWER_KEYS:
        return True
    return bool(question and EEO_LABEL_RE.search(_norm(question)))
FULL_NAME_ATTRS = {"name", "full name", "fullname", "candidate name", "your name"}
TEXT_TYPES = {"text", "email", "tel", "url", "number", "textarea", ""}
CONTACT_KINDS = {"email", "first_name", "last_name", "full_name", "phone"}

APPLY_RE = re.compile(
    r"^\s*(apply( now| here| online| to this job| for (this|the) (job|position|role))?"
    r"|i'?m interested|start (my |your )?application)\s*[›»→>]?\s*$",
    re.I,
)
CAPTCHA_RE = re.compile(r"recaptcha|hcaptcha|turnstile|challenges\.cloudflare|arkoselabs", re.I)

# Whether clicking an element could submit a form. A <button> inside a form submits unless its
# type is explicitly "button"/"reset" (e.type reports "submit" for a missing type attribute).
_SUBMITS_FN = r"""
const submits = (n) => {
  if (!n) return false;
  const t = n.tagName.toLowerCase();
  if (t === 'input') return n.type === 'submit' || n.type === 'image';
  if (t === 'button') return n.type === 'submit' && !!(n.form || n.closest('form'));
  return false;
};
"""
# For "Apply" controls: also refuse any non-link control inside a form; its script may submit it.
MAY_SUBMIT_JS = (
    "(e) => {" + _SUBMITS_FN + r"""
  if (submits(e) || submits(e.closest('button'))) return true;
  return e.tagName.toLowerCase() !== 'a' && !!(e.form || e.closest('form'));
}"""
)
# For autocomplete options (often <li>/<div> inside the form): only native submitters are unsafe.
OPTION_MAY_SUBMIT_JS = "(e) => {" + _SUBMITS_FN + " return submits(e) || submits(e.closest('button')); }"

GATE_JS = r"""
() => Array.from(document.querySelectorAll('[role=dialog], dialog, [aria-modal=true], [class*=modal], [class*=popup]'))
  .filter((d) => d.checkVisibility && d.checkVisibility())
  .some((d) => /\b(sign in|log in|login|create (a )?(free )?account|register)\b/i.test(d.innerText || ''))
"""

APPLY_LINKS_JS = r"""
(pattern) => {
  const rx = new RegExp(pattern, 'i');
  const host = (h) => h.replace(/^www\./, '');
  const here = host(location.hostname);
  const hrefs = Array.from(document.querySelectorAll('a[href]'))
    .filter((a) => rx.test((a.textContent || '').replace(/\s+/g, ' ').trim()))
    .map((a) => a.href)
    .filter((h) => { try { const u = new URL(h); return /^https?:$/.test(u.protocol) && host(u.hostname) !== here; } catch { return false; } });
  return Array.from(new Set(hrefs));
}
"""

SCAN_JS = r"""
(start) => {
  const ATTR = 'data-jobapply-id';
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const ownText = (root) => {
    const parts = [];
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    let n;
    while ((n = walker.nextNode())) {
      const parent = n.parentElement;
      if (parent && parent.closest('select,textarea,option,script,style,button')) continue;
      if (parent && parent.checkVisibility && !parent.checkVisibility()) continue;
      parts.push(n.nodeValue);
    }
    return clean(parts.join(' ')).slice(0, 300);
  };
  const isVisible = (el) => {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
  };
  const CONTROLS = 'input:not([type=hidden]),textarea,select';
  const labelFor = (el) => {
    const ids = el.getAttribute('aria-labelledby');
    if (ids) {
      const t = clean(ids.split(/\s+/).map((i) => { const e = document.getElementById(i); return e ? e.textContent : ''; }).join(' '));
      if (t) return t;
    }
    if (el.labels && el.labels.length) {
      const t = clean(Array.from(el.labels).map(ownText).join(' '));
      if (t) return t;
    }
    const aria = clean(el.getAttribute('aria-label'));
    if (aria) return aria;
    let node = el.parentElement;
    for (let depth = 0; node && depth < 5; depth++, node = node.parentElement) {
      if (node.querySelectorAll(CONTROLS).length > 1) break;
      const t = ownText(node);
      if (t) return t;
    }
    return '';
  };
  const groupLabel = (el) => {
    const fs = el.closest('fieldset');
    const legend = fs && fs.querySelector('legend');
    if (legend) return clean(legend.textContent);
    const rg = el.closest('[role=radiogroup],[role=group]');
    if (rg) return clean(rg.getAttribute('aria-label')) || ownText(rg).slice(0, 160);
    if (el.name) {
      // Climb to the smallest container holding the whole group, then strip the option labels.
      const members = Array.from(document.querySelectorAll(`input[name="${CSS.escape(el.name)}"]`));
      const options = members.flatMap((m) => Array.from(m.labels || []).map(ownText)).filter(Boolean);
      let node = el.parentElement;
      for (let depth = 0; node && depth < 8; depth++, node = node.parentElement) {
        if (Array.from(node.querySelectorAll(CONTROLS)).some((c) => !members.includes(c))) break;
        if (!members.every((m) => node.contains(m))) continue;
        let t = ownText(node);
        options.forEach((o) => { t = t.split(o).join(' '); });
        t = clean(t);
        if (t) return t.slice(0, 160);
      }
    }
    return labelFor(el);
  };

  let id = start;
  const fields = [];
  const groups = new Map();
  let hasPassword = false;
  for (const el of document.querySelectorAll('input, textarea, select')) {
    const tag = el.tagName.toLowerCase();
    const type = tag === 'input' ? (el.getAttribute('type') || 'text').toLowerCase() : tag;
    if (type === 'password' && isVisible(el)) hasPassword = true;
    if (['hidden', 'submit', 'button', 'reset', 'image', 'password', 'search'].includes(type)) continue;
    if (el.disabled || el.readOnly) continue;
    const isChoice = type === 'radio' || type === 'checkbox';
    const visible = type === 'file' || isVisible(el) || (isChoice && el.labels && Array.from(el.labels).some(isVisible));
    if (!visible) continue;
    // Custom dropdowns (react-select etc.) shadow the real control with an invisible "required" input.
    if (!isChoice && type !== 'file' && (el.getAttribute('aria-hidden') === 'true' || getComputedStyle(el).opacity === '0')) continue;

    let label = isChoice ? groupLabel(el) : labelFor(el);
    let required = el.required || el.getAttribute('aria-required') === 'true' || /\*|✱/.test(label);
    label = clean(label.replace(/[*✱]/g, ''));

    if (isChoice) {
      const key = type + ':' + (el.name || label);
      const existing = groups.get(key);
      if (existing) {
        existing.required = existing.required || required;
        existing.filled = existing.filled || el.checked;
        continue;
      }
    }
    el.setAttribute(ATTR, String(id));
    const field = {
      id: id++,
      tag,
      type,
      label,
      name: el.getAttribute('name') || '',
      idAttr: el.id || '',
      placeholder: el.getAttribute('placeholder') || '',
      autocomplete: (el.getAttribute('autocomplete') || '').toLowerCase(),
      required,
      filled: type === 'file' ? el.files.length > 0
        : isChoice ? el.checked
        : tag === 'select' ? el.selectedIndex > 0 || (el.selectedIndex === 0 && clean(el.value) !== '' && !/^(select|choose|please|--|none)/i.test(clean(el.options[0].text)))
        : clean(el.value) !== '',
      options: tag === 'select' ? Array.from(el.options).map((o) => clean(o.text)).slice(0, 80) : [],
      combobox: el.getAttribute('role') === 'combobox' || el.hasAttribute('aria-autocomplete') || el.hasAttribute('list'),
      form: el.form ? Array.from(document.forms).indexOf(el.form) : -1,
    };
    if (isChoice) groups.set(type + ':' + (el.name || label), field);
    fields.push(field);
  }
  return { fields, next: id, hasPassword };
}
"""

# Like SCAN_JS, but returns the CURRENT value of every field — used after the
# user has answered questions by hand in a streamed session, to offer those
# answers for saving into their profile answer bank. Password, file, hidden
# and button inputs are never read. Radio/checkbox groups report the checked
# member's label text.
CAPTURE_JS = r"""
() => {
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const ownText = (root) => {
    const parts = [];
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    let n;
    while ((n = walker.nextNode())) {
      const parent = n.parentElement;
      if (parent && parent.closest('select,textarea,option,script,style,button')) continue;
      if (parent && parent.checkVisibility && !parent.checkVisibility()) continue;
      parts.push(n.nodeValue);
    }
    return clean(parts.join(' ')).slice(0, 300);
  };
  const isVisible = (el) => {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
  };
  const CONTROLS = 'input:not([type=hidden]),textarea,select';
  const labelFor = (el) => {
    const ids = el.getAttribute('aria-labelledby');
    if (ids) {
      const t = clean(ids.split(/\s+/).map((i) => { const e = document.getElementById(i); return e ? e.textContent : ''; }).join(' '));
      if (t) return t;
    }
    if (el.labels && el.labels.length) {
      const t = clean(Array.from(el.labels).map(ownText).join(' '));
      if (t) return t;
    }
    const aria = clean(el.getAttribute('aria-label'));
    if (aria) return aria;
    let node = el.parentElement;
    for (let depth = 0; node && depth < 5; depth++, node = node.parentElement) {
      if (node.querySelectorAll(CONTROLS).length > 1) break;
      const t = ownText(node);
      if (t) return t;
    }
    return '';
  };
  const memberLabel = (el) => {
    if (el.labels && el.labels.length) return clean(Array.from(el.labels).map(ownText).join(' '));
    const lab = el.closest('label');
    return lab ? ownText(lab) : '';
  };
  const groupLabel = (el) => {
    const fs = el.closest('fieldset');
    const legend = fs && fs.querySelector('legend');
    if (legend) return clean(legend.textContent);
    const rg = el.closest('[role=radiogroup],[role=group]');
    if (rg) return clean(rg.getAttribute('aria-label')) || ownText(rg).slice(0, 160);
    if (el.name) {
      const members = Array.from(document.querySelectorAll(`input[name="${CSS.escape(el.name)}"]`));
      const options = members.flatMap((m) => Array.from(m.labels || []).map(ownText)).filter(Boolean);
      let node = el.parentElement;
      for (let depth = 0; node && depth < 8; depth++, node = node.parentElement) {
        if (Array.from(node.querySelectorAll(CONTROLS)).some((c) => !members.includes(c))) break;
        if (!members.every((m) => node.contains(m))) continue;
        let t = ownText(node);
        options.forEach((o) => { t = t.split(o).join(' '); });
        t = clean(t);
        if (t) return t.slice(0, 160);
      }
    }
    return labelFor(el);
  };

  const fields = [];
  const groups = new Set();
  for (const el of document.querySelectorAll('input, textarea, select')) {
    const tag = el.tagName.toLowerCase();
    const type = tag === 'input' ? (el.getAttribute('type') || 'text').toLowerCase() : tag;
    if (['hidden', 'submit', 'button', 'reset', 'image', 'password', 'search', 'file'].includes(type)) continue;
    if (el.disabled) continue;
    const isChoice = type === 'radio' || type === 'checkbox';
    if (isChoice) {
      const key = type + ':' + (el.name || groupLabel(el));
      if (groups.has(key)) continue;
      groups.add(key);
      const members = el.name
        ? Array.from(document.querySelectorAll(`input[name="${CSS.escape(el.name)}"]`))
        : [el];
      const checked = members.find((m) => m.checked);
      fields.push({
        label: clean(groupLabel(el).replace(/[*✱]/g, '')),
        type, tag,
        name: el.getAttribute('name') || '',
        value: checked ? memberLabel(checked) : '',
      });
      continue;
    }
    if (el.readOnly || !isVisible(el)) continue;
    let value = '';
    if (tag === 'select') {
      const opt = el.options[el.selectedIndex];
      const text = opt ? clean(opt.text) : '';
      value = el.selectedIndex === 0 && /^(select|choose|please|--|none)/i.test(text) ? '' : text;
    } else {
      value = clean(el.value);
    }
    fields.push({
      label: clean(labelFor(el).replace(/[*✱]/g, '')),
      type, tag,
      name: el.getAttribute('name') || '',
      value,
    });
  }
  return fields;
}
"""

MARK_JS = r"""
({ done, todo }) => {
  const mark = (ids, color) => ids.forEach((i) => {
    const el = document.querySelector(`[data-jobapply-id="${i}"]`);
    if (!el) return;
    let target = el;
    if (el.type === 'radio' || el.type === 'checkbox') {
      target = el.closest('fieldset,[role=radiogroup],[role=group]') || el.closest('label') || el;
    } else if (el.getAttribute('role') === 'combobox' && el.closest('[class*=control]')) {
      target = el.closest('[class*=control]');
    } else {
      // Tiny inputs (custom dropdown search boxes, visually-hidden file inputs): outline the visible control.
      while (target.parentElement && target.getBoundingClientRect().width < 120) target = target.parentElement;
    }
    target.style.setProperty('outline', `2px solid ${color}`, 'important');
    target.style.setProperty('outline-offset', '2px', 'important');
  });
  mark(done, '#3fb950');
  mark(todo, '#f0883e');
}
"""

# Custom dropdowns keep their search <input> empty and render the chosen value next to it.
FILLED_JS = r"""
(ids) => ids.filter((i) => {
  const el = document.querySelector(`[data-jobapply-id="${i}"]`);
  if (!el || el.getAttribute('role') !== 'combobox' || (el.value || '').trim()) return false;
  const controls = (n) => Array.from(n.querySelectorAll('input:not([type=hidden]),textarea,select'))
    .filter((c) => c.getAttribute('aria-hidden') !== 'true').length;
  let box = el.closest('[class*=control]');
  if (!box) {
    box = el.parentElement;
    while (box && box.parentElement && controls(box.parentElement) <= 1 && !box.parentElement.querySelector('label')) box = box.parentElement;
  }
  if (!box) return false;
  const text = (box.textContent || '').replace(/\s+/g, ' ').trim();
  const placeholder = (el.getAttribute('placeholder') || '').trim();
  return !!text && text !== placeholder && !/^(select|choose|search|type to search|start typing)\b/i.test(text);
})
"""

BANNER_JS = r"""
(message) => {
  document.getElementById('jobapply-banner')?.remove();
  const bar = document.createElement('div');
  bar.id = 'jobapply-banner';
  bar.setAttribute('role', 'status');
  bar.style.cssText = 'position:fixed;top:12px;right:12px;z-index:2147483647;max-width:380px;padding:12px 36px 12px 14px;'
    + 'background:#10140c;color:#f3f0e4;border:1px solid #d6f16a;border-radius:12px;font:13px/1.45 system-ui,sans-serif;'
    + 'box-shadow:0 8px 24px rgba(0,0,0,.35);white-space:pre-line';
  bar.textContent = message;
  const close = document.createElement('button');
  close.textContent = '×';
  close.setAttribute('aria-label', 'Dismiss');
  close.style.cssText = 'position:absolute;top:6px;right:8px;background:none;border:0;color:inherit;font-size:18px;cursor:pointer';
  close.onclick = () => bar.remove();
  bar.appendChild(close);
  document.body.appendChild(bar);
}
"""


SUGGEST_JS = r"""
({ id, city, quals, phase }) => {
  const el = document.querySelector(`[data-jobapply-id="${id}"]`);
  const want = city.toLowerCase();
  document.querySelectorAll('[data-jobapply-pick]').forEach((n) => n.removeAttribute('data-jobapply-pick'));
  const OPTIONISH = /option|suggest|dropdown|result|item|menu|pac-|autocomplete|listbox/i;
  const has = (n) => (n.textContent || '').toLowerCase().includes(want);
  if (phase === 'mark') {
    document.querySelectorAll('[role=option], li, div, span, a').forEach((n) => { if (has(n)) n.setAttribute('data-jobapply-pre', '1'); });
    return null;
  }
  if (phase === 'check') {
    if (!el) return false;
    if ((el.value || '').toLowerCase().includes(want)) return true;
    // Custom selects render the chosen value beside the (now empty) search input.
    const controls = (n) => Array.from(n.querySelectorAll('input:not([type=hidden]),textarea,select'))
      .filter((c) => c.getAttribute('aria-hidden') !== 'true').length;
    for (let box = el.parentElement, depth = 0; box && depth < 6 && controls(box) <= 1; box = box.parentElement, depth++) {
      if (has(box)) return true;
    }
    return false;
  }
  const shown = (n) => {
    const text = (n.textContent || '').trim();
    return text && text.length <= 150 && !n.querySelector('input,textarea,select') && n.checkVisibility && n.checkVisibility();
  };
  // Prefer the listbox the input says it controls; fall back to suggestion-looking nodes that appeared after typing.
  const listId = el && (el.getAttribute('aria-controls') || el.getAttribute('aria-owns'));
  const listbox = listId && document.getElementById(listId);
  let cands = listbox
    ? Array.from(listbox.querySelectorAll('[role=option]')).concat(Array.from(listbox.children))
    : Array.from(document.querySelectorAll('[role=option], li, div, span, a')).filter((n) => {
        if (n.hasAttribute('data-jobapply-pre')) return false;
        const ctx = [n, n.parentElement, n.parentElement && n.parentElement.parentElement]
          .filter(Boolean).map((x) => `${x.getAttribute('role') || ''} ${x.className || ''}`).join(' ');
        return n.getAttribute('role') === 'option' || OPTIONISH.test(ctx);
      });
  cands = Array.from(new Set(cands)).filter((n) => has(n) && shown(n));
  cands = cands.filter((n) => !cands.some((o) => o !== n && n.contains(o)));
  if (!cands.length) return null;
  const esc = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const score = (n) => {
    const text = n.textContent.trim().toLowerCase();
    let s = text.startsWith(want) ? 2 : 0;
    quals.forEach((q) => { if (new RegExp(`\\b${esc(q.toLowerCase())}\\b`).test(text)) s += 1; });
    return s;
  };
  const scored = cands.map((n) => [score(n), n]).sort((a, b) => b[0] - a[0]);
  if (scored.length > 1 && scored[1][0] === scored[0][0]) return 'ambiguous';
  scored[0][1].setAttribute('data-jobapply-pick', '1');
  return 'pick';
}
"""


class _BrowserWorker:
    """Owns one long-lived event loop + browser so tabs outlive the request or run that opened them."""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread_lock = threading.Lock()
        self._ctx_lock: asyncio.Lock | None = None
        self._pw: Any = None
        self._context: Any = None

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._thread_lock:
            if self._loop is None:
                loop = asyncio.new_event_loop()
                threading.Thread(target=loop.run_forever, name="jobapply-browser", daemon=True).start()
                self._loop = loop
            return self._loop

    async def call(self, coro: Any) -> Any:
        fut = asyncio.run_coroutine_threadsafe(coro, self._ensure_loop())
        return await asyncio.wrap_future(fut)

    async def context(self) -> Any:
        if self._ctx_lock is None:
            self._ctx_lock = asyncio.Lock()
        async with self._ctx_lock:
            if self._context is None:
                from playwright.async_api import async_playwright

                if self._pw is None:
                    self._pw = await async_playwright().start()
                BROWSER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
                ctx = await self._pw.chromium.launch_persistent_context(
                    str(BROWSER_PROFILE_DIR),
                    headless=settings.formfill_headless,
                    no_viewport=True,
                )
                ctx.on("close", lambda _ctx: setattr(self, "_context", None))
                self._context = ctx
            return self._context

    async def _close(self) -> None:
        if self._context is not None:
            await self._context.close()
            self._context = None
        if self._pw is not None:
            await self._pw.stop()
            self._pw = None

    def shutdown(self) -> None:
        loop = self._loop
        if loop is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(self._close(), loop).result(timeout=10)
        except Exception:
            pass
        loop.call_soon_threadsafe(loop.stop)
        self._loop = None


_worker = _BrowserWorker()


def shutdown_browser() -> None:
    _worker.shutdown()


async def fill_application(
    *,
    profile: dict[str, Any],
    job: dict[str, Any],
    cover: str,
    resume_path: str,
    location: str,
    packet_key: str,
    answers: dict[str, str] | None = None,
) -> dict[str, str]:
    url = (job.get("apply_url") or "").strip()
    if not url.startswith(("http://", "https://")):
        return {"status": "prepared", "channel": "ats_link", "notes": "No usable apply URL to open in the browser."}
    try:
        import playwright  # noqa: F401
    except ImportError:
        return {
            "status": "prepared",
            "channel": "ats_link",
            "notes": "Browser pre-fill unavailable: run `pip install playwright && playwright install chromium`.",
        }
    files = _packet_files(profile, cover, resume_path, packet_key)
    values = _values(profile, location, cover, files, answers)
    try:
        return await _worker.call(asyncio.wait_for(_fill(url, values), FILL_TIMEOUT_S))
    except Exception as exc:
        reason = "timed out" if isinstance(exc, (asyncio.TimeoutError, TimeoutError)) else str(exc).splitlines()[0][:200]
        return {
            "status": "prepared",
            "channel": "ats_link",
            "notes": f"Browser pre-fill failed ({reason}). Packet is ready at the official apply URL.",
        }


async def _fill(url: str, values: dict[str, Any]) -> dict[str, str]:
    ctx = await _worker.context()
    blank = [p for p in ctx.pages if p.url in ("about:blank", "chrome://newtab/")]
    page = blank[0] if blank else await ctx.new_page()
    await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
    await _settle(page)
    result = await _fill_open_page(page, values)
    result.pop("_page", None)
    return result


async def _fill_open_page(page: Any, values: dict[str, Any]) -> dict[str, Any]:
    """Fill the application form on an already-open page. Shared by the local
    browser flow and by streamed pre-fill sessions, which supply their own
    page in an ephemeral context. Returns the usual status/channel/notes
    plus `filled` and `needs_attention` field labels and the active `_page`
    (clicking through an Apply link can move to a different page)."""
    fields = await _scan(page)
    tried: set[str] = set()
    for _ in range(4):
        if _has_form(fields):
            break
        nxt = await _click_apply(page, tried)
        if nxt is None:
            break
        page = nxt
        fields = await _scan(page)

    captcha = await _visible_captcha(page)
    needs_login = any(f.get("_password") for f in fields) or await _sign_in_gate(page)
    if not _has_form(fields):
        by_email = await _email_instructions(page)
        if by_email:
            address, subject = by_email
            note = f"This employer takes applications by email: send your resume and cover letter to {address}"
            note += f" with the subject “{subject}”." if subject else "."
            await _banner(page, f"JobApply Agent: {note}")
            await page.bring_to_front()
            return {"status": "prepared", "channel": "mailto", "notes": note, "apply_email": address, "email_subject": subject, "filled": [], "needs_attention": [], "_page": page}
        note = "Couldn't find an application form at the apply URL."
        if needs_login:
            note = (
                f"{_site(page.url)} asks you to sign in before showing the employer's application. "
                "Sign in in the open browser window (it remembers you), then click Fill form again."
            )
        await _banner(page, f"JobApply Agent: {note}")
        await page.bring_to_front()
        return {"status": "prepared", "channel": "ats_link", "notes": note + " Packet is ready.", "filled": [], "needs_attention": [], "_page": page}

    done: list[dict[str, Any]] = []
    todo: list[dict[str, Any]] = []
    used: set[str] = set()
    filled_values: dict[str, str] = {}
    for f in _application_form(fields):
        kind = f["kind"]
        if kind in {"resume", "cover_file"} and kind in used:
            kind = None
        fill_kind, value = _planned_value(f, values, kind)
        ok = False
        attempted = bool(value) and not (f["filled"] and f["type"] != "file")
        if attempted:
            try:
                ok = await _fill_field(f, fill_kind, value)
            except Exception:
                ok = False
        if ok:
            if kind:
                used.add(kind)
            done.append(f)
            if f.get("label"):
                filled_values[_norm(f["label"])] = str(value)
        elif attempted or (f["required"] and not f["filled"]):
            todo.append(f)

    todo = await _drop_filled(todo)

    for frame in {id(f["frame"]): f["frame"] for f in done + todo}.values():
        try:
            await frame.evaluate(
                MARK_JS,
                {"done": [f["id"] for f in done if f["frame"] is frame], "todo": [f["id"] for f in todo if f["frame"] is frame]},
            )
        except Exception:
            pass

    done_names = "; ".join(dict.fromkeys(_display(f) for f in done)) or "nothing"
    todo_names = "; ".join(dict.fromkeys(_display(f) for f in todo))
    lines = [f"Filled {len(done)} field(s): {done_names}"]
    if todo:
        lines.append(f"Needs you ({len(todo)}): {todo_names}")
    if captcha:
        lines.append("A CAPTCHA is on the page; complete it yourself.")
    lines.append("Nothing was submitted. Review the form in the browser window and click Submit yourself.")
    await _banner(page, "JobApply Agent\n" + "\n".join(lines) + "\nGreen = filled, orange = needs you.")
    await page.bring_to_front()
    return {
        "status": "filled" if done else "prepared",
        "channel": "browser",
        "notes": " · ".join(lines),
        "filled": list(dict.fromkeys(_display(f) for f in done)),
        "needs_attention": list(dict.fromkeys(_display(f) for f in todo)),
        "filled_values": filled_values,
        "_page": page,
    }


async def _drop_filled(todo: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Re-check flagged dropdowns: some were preset by the site or set as a side effect (e.g. phone country)."""
    filled: set[tuple[int, int]] = set()
    frames = {id(f["frame"]): f["frame"] for f in todo if f["combobox"] and f["kind"] != "location"}
    for key, frame in frames.items():
        try:
            ids = await frame.evaluate(FILLED_JS, [f["id"] for f in todo if id(f["frame"]) == key])
            filled.update((key, i) for i in ids)
        except Exception:
            pass
    return [f for f in todo if (id(f["frame"]), f["id"]) not in filled]


async def _settle(page: Any) -> None:
    try:
        await page.wait_for_load_state("networkidle", timeout=8_000)
    except Exception:
        pass


async def _scan(page: Any) -> list[dict[str, Any]]:
    fields: list[dict[str, Any]] = []
    next_id = 0
    for frame in page.frames:
        if CAPTCHA_RE.search(frame.url or ""):
            continue
        try:
            res = await frame.evaluate(SCAN_JS, next_id)
        except Exception:
            continue
        next_id = res["next"]
        for f in res["fields"]:
            f["frame"] = frame
            f["kind"] = _classify(f)
            f["answer_key"] = _classify_answer(f)
            fields.append(f)
        if res.get("hasPassword"):
            fields.append({"_password": True, "frame": frame})
    return fields


async def _scan_form_values(page: Any) -> list[dict[str, Any]]:
    """Current field values on an open page (CAPTURE_JS per frame). Used to
    offer the answers a user typed into a streamed session for saving."""
    fields: list[dict[str, Any]] = []
    for frame in page.frames:
        if CAPTCHA_RE.search(frame.url or ""):
            continue
        try:
            fields.extend(await frame.evaluate(CAPTURE_JS))
        except Exception:
            continue
    return fields


def answer_candidates(
    scanned: list[dict[str, Any]], agent_filled: dict[str, str]
) -> list[dict[str, Any]]:
    """Diff a form's current values against what the agent filled, and return
    the values the USER supplied as save-for-next-time candidates.

    Excluded: empty values, password/file/hidden fields (the capture scan
    never returns them, but the filter is enforced here too), one-time-code /
    verification / captcha fields, and fields the resume profile already
    covers (they classify to a resume kind). A value identical to what the
    agent filled for that label is the agent's, not the user's, and is
    skipped. EEO/demographic candidates are flagged `sensitive` so the UI
    can leave them unchecked by default.
    """
    out: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    for f in scanned:
        label = (f.get("label") or "").strip()
        value = (f.get("value") or "").strip()
        if not label or not value:
            continue
        if f.get("type") in {"password", "file", "hidden"} or f.get("tag") == "file":
            continue
        hay = _norm(label, f.get("name") or "")
        if CODE_LABEL_RE.search(hay):
            continue
        probe = {
            "label": label,
            "type": f.get("type") or "text",
            "tag": f.get("tag") or "input",
            "name": f.get("name") or "",
            "placeholder": "",
            "idAttr": "",
            "autocomplete": "",
            "options": [],
            "combobox": False,
        }
        if _classify(probe):
            continue  # resume basics live on the profile, not in the answer bank
        if agent_filled.get(_norm(label)) == value:
            continue  # the agent put this value there
        key = _classify_answer(probe) or _norm(label)
        if not key or key in seen_keys:
            continue
        seen_keys.add(key)
        out.append(
            {
                "key": key,
                "question": label,
                "answer": value,
                "sensitive": answer_is_sensitive(key, label),
            }
        )
    return out


def _form_weight(group: list[dict[str, Any]]) -> int:
    return sum(3 if f["kind"] == "resume" else 1 if f["kind"] in CONTACT_KINDS else 0 for f in group)


def _form_groups(fields: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    groups: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for f in fields:
        if not f.get("_password"):
            groups.setdefault((id(f["frame"]), f["form"]), []).append(f)
    return list(groups.values())


def _is_application(group: list[dict[str, Any]]) -> bool:
    # A lone email box is a newsletter signup and a lone upload is often a job board's "upload your CV" promo.
    contacts = sum(1 for f in group if f["kind"] in CONTACT_KINDS)
    has_resume = any(f["kind"] == "resume" for f in group)
    return contacts >= 3 or (has_resume and contacts >= 1)


def _has_form(fields: list[dict[str, Any]]) -> bool:
    return any(_is_application(g) for g in _form_groups(fields))


def _application_form(fields: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Career pages often carry other forms (job alerts, newsletter, search); keep only the application."""
    groups = [g for g in _form_groups(fields) if _is_application(g)]
    return max(groups, key=_form_weight) if groups else []


async def _click_apply(page: Any, tried: set[str]) -> Any:
    """Move from a job listing to its application form. Job boards often link out to the employer's
    ATS (sometimes via a hidden anchor behind a JS button), so external apply links win; otherwise
    click untried Apply buttons one at a time, skipping in-page jumps like `#apply`."""
    try:
        external = await page.evaluate(APPLY_LINKS_JS, APPLY_RE.pattern)
    except Exception:
        external = []
    for href in external:
        if href not in tried:
            tried.add(href)
            await page.goto(href, wait_until="domcontentloaded", timeout=45_000)
            await _settle(page)
            return page

    for role in ("link", "button"):
        loc = page.get_by_role(role, name=APPLY_RE)
        for i in range(min(await loc.count(), 10)):
            el = loc.nth(i)
            try:
                if not await el.is_visible() or await el.evaluate(MAY_SUBMIT_JS):
                    continue
                href = (await el.get_attribute("href") or "").strip()
                if href.startswith("#"):
                    continue
                key = f"{page.url}|{role}|{i}|{href}"
                if key in tried:
                    continue
                tried.add(key)
                before = list(page.context.pages)
                await el.click(timeout=5_000)
            except Exception:
                continue
            await asyncio.sleep(1.5)
            new_pages = [p for p in page.context.pages if p not in before]
            target = new_pages[-1] if new_pages else page
            await _settle(target)
            return target
    return None


def _norm(*parts: str) -> str:
    s = " ".join(p for p in parts if p)
    s = re.sub(r"([a-z])([A-Z])", r"\1 \2", s)
    s = re.sub(r"[_\-\[\]\.:/()?]+", " ", s)
    return re.sub(r"\s+", " ", s).strip().lower()


def _classify(f: dict[str, Any]) -> str | None:
    ftype = f["type"]
    if ftype in {"radio", "checkbox"}:
        return None
    label = _norm(f["label"])
    hay = _norm(f["label"], f["placeholder"], f["name"], f["idAttr"])
    if SENSITIVE_RE.search(hay):
        return None
    if ftype == "file":
        if re.search(r"autofill|auto fill|parse|import", hay):
            return None
        return "cover_file" if re.search(r"\bcover\b", hay) else "resume"

    auto = f["autocomplete"]
    by_auto = {"given-name": "first_name", "family-name": "last_name", "name": "full_name", "email": "email", "url": "website"}
    kind: str | None = by_auto.get(auto) or ("phone" if auto.startswith("tel") else None)
    if not kind:
        for key, rx in RULES:
            if rx.search(hay):
                kind = key
                break
    if not kind and (FULL_NAME_RE.search(label) or _norm(f["name"]) in FULL_NAME_ATTRS):
        kind = "full_name"
    if not kind:
        kind = {"email": "email", "tel": "phone"}.get(ftype)

    if f["tag"] == "select":
        return "years" if kind == "years" else None
    if kind == "cover_letter" and ftype != "textarea":
        return None
    if ftype not in TEXT_TYPES:
        return None
    return kind


def _planned_value(
    f: dict[str, Any], values: dict[str, Any], kind: str | None = None
) -> tuple[str | None, Any]:
    """The (key, value) the filler will use for a field. Resume-derived kinds
    win; otherwise fall back to the saved answer bank — by the field's
    canonical answer key first, then by its normalized label (free-form saved
    answers). This is the ONLY path by which a blocklisted field can receive
    a value: a saved answer the user stored on their profile."""
    if kind is None:
        kind = f.get("kind")
    value = values.get(kind) if kind else None
    if value:
        return kind, value
    answer_key = f.get("answer_key")
    if answer_key and values.get(answer_key):
        return answer_key, values[answer_key]
    label_key = _norm(f.get("label") or "")
    if label_key and values.get(label_key):
        return label_key, values[label_key]
    return kind, None


async def _fill_field(f: dict[str, Any], kind: str, value: Any) -> bool:
    loc = f["frame"].locator(f'[{ATTR}="{f["id"]}"]')
    if f["type"] == "file":
        await loc.set_input_files(str(value), timeout=10_000)
        return True
    if f["tag"] == "select":
        option = (
            _pick_years_option(f["options"], int(value))
            if kind == "years"
            else _pick_option_by_text(f["options"], str(value))
        )
        if not option:
            return False
        await loc.select_option(label=option, timeout=5_000)
        return True
    if kind == "location":
        return await _fill_location(f, loc, str(value))
    await loc.fill(str(value), timeout=5_000)
    return True


async def _fill_location(f: dict[str, Any], loc: Any, value: str) -> bool:
    """Type the location and pick a matching suggestion. Autocomplete widgets often clear typed text on
    blur, so success is only reported if the value is still there afterwards. Ambiguous matches
    (e.g. four different "Newark"s) are left for the candidate rather than guessed."""
    frame = f["frame"]
    parts = [p.strip() for p in value.split(",") if p.strip()]
    if not parts:
        return False
    city = parts[0]
    quals: list[str] = []
    for q in parts[1:]:
        quals.append(q)
        if q.lower() in US_STATES:
            quals.append(US_STATES[q.lower()])
    args = {"id": f["id"], "city": city, "quals": quals}
    try:
        await frame.evaluate(SUGGEST_JS, {**args, "phase": "mark"})
        await loc.fill("", timeout=3_000)
        await loc.press_sequentially(value, delay=40, timeout=10_000)
        ambiguous = 0
        for _ in range(24):
            await asyncio.sleep(0.25)
            picked = await frame.evaluate(SUGGEST_JS, {**args, "phase": "pick"})
            if picked == "pick":
                target = frame.locator('[data-jobapply-pick="1"]').first
                if await target.evaluate(OPTION_MAY_SUBMIT_JS):
                    break
                await target.click(timeout=2_500)
                break
            if picked == "ambiguous":
                ambiguous += 1
                if ambiguous >= 4:
                    break
        else:
            picked = None
        if picked != "pick" and f["combobox"]:
            await loc.fill("", timeout=3_000)
            await loc.press("Escape", timeout=2_000)
            return False
        await asyncio.sleep(0.3)
        return bool(await frame.evaluate(SUGGEST_JS, {**args, "phase": "check"}))
    except Exception:
        return False


async def _sign_in_gate(page: Any) -> bool:
    try:
        return bool(await page.evaluate(GATE_JS))
    except Exception:
        return False


EMAIL_APPLY_RE = re.compile(
    r"(?:apply|send|email|e-mail|submit)[^.]{0,120}?\b([\w.+-]+@[\w-]+(?:\.[\w-]+)+)", re.I
)
EMAIL_SUBJECT_RE = re.compile(r"(?:subject|email title|title)\s*(?:line)?\s*[:\-]?\s*[\[\"“']([^\]\"”']{3,140})[\]\"”']", re.I)


async def _email_instructions(page: Any) -> tuple[str, str] | None:
    """'To apply, send an email to jobs@acme.com with the title [Role @ Acme]' -> (address, subject)."""
    try:
        chunks = await page.evaluate(
            "() => { const vis = (d) => d.checkVisibility && d.checkVisibility();"
            " const dialogs = Array.from(document.querySelectorAll('[role=dialog], dialog, [aria-modal=true], [class*=modal], [class*=popup]')).filter(vis);"
            " return dialogs.map((d) => d.innerText).concat([document.body.innerText]).map((t) => t.slice(0, 20000)); }"
        )
    except Exception:
        return None
    for text in chunks or []:
        found = _email_in(text)
        if found:
            return found
    return None


def _email_in(text: str) -> tuple[str, str] | None:
    """First apply-by-email address, preferring a mention that also names the required subject line."""
    first: tuple[str, str] | None = None
    for m in EMAIL_APPLY_RE.finditer(text or ""):
        address = m.group(1).rstrip(".")
        if re.search(r"(privacy|accommodation|accessibility|noreply|no-reply|support|help|press|legal)", address, re.I):
            continue
        subject = EMAIL_SUBJECT_RE.search(text[m.end(): m.end() + 300])
        if subject:
            return address, subject.group(1).strip()
        first = first or (address, "")
    return first


def _site(url: str) -> str:
    from urllib.parse import urlparse

    host = urlparse(url).hostname or "The site"
    return host.removeprefix("www.")


async def _visible_captcha(page: Any) -> bool:
    for frame in page.frames:
        url = frame.url or ""
        if not CAPTCHA_RE.search(url) or "size=invisible" in url:
            continue
        try:
            el = await frame.frame_element()
            box = await el.bounding_box()
            if box and box["height"] > 40 and await el.is_visible():
                return True
        except Exception:
            continue
    return False


def _pick_option_by_text(options: list[str], answer: str) -> str | None:
    """Match a saved answer against a <select>'s option texts: exact match
    first, then prefix, then containment. Placeholder prompts are ignored."""
    want = _norm(answer)
    if not want:
        return None
    usable = [o for o in options if _norm(o) and not re.match(r"^(select|choose|please|--|—)", _norm(o))]
    for opt in usable:
        if _norm(opt) == want:
            return opt
    for opt in usable:
        n = _norm(opt)
        if n.startswith(want) or want.startswith(n):
            return opt
    for opt in usable:
        n = _norm(opt)
        if want in n or n in want:
            return opt
    return None


def _pick_years_option(options: list[str], years: int) -> str | None:
    for opt in options:
        nums = [int(n) for n in re.findall(r"\d+", opt)]
        if not nums:
            continue
        if len(nums) >= 2 and nums[0] <= years <= nums[1]:
            return opt
        if len(nums) == 1 and ("+" in opt or "more" in opt.lower()) and years >= nums[0]:
            return opt
        if len(nums) == 1 and nums[0] == years:
            return opt
    return None


def _display(f: dict[str, Any]) -> str:
    if f.get("kind") == "resume":
        return "Resume upload"
    if f.get("kind") == "cover_file":
        return "Cover letter upload"
    text = f["label"] or f["placeholder"] or "Unlabeled field"
    return text if len(text) <= 60 else text[:57].rstrip() + "…"


async def _banner(page: Any, message: str) -> None:
    try:
        await page.evaluate(BANNER_JS, message)
    except Exception:
        pass


def _url(value: str | None) -> str:
    value = (value or "").strip()
    if value and not value.startswith(("http://", "https://")):
        value = "https://" + value
    return value


def _values(
    profile: dict[str, Any],
    location: str,
    cover: str,
    files: dict[str, Path],
    answers: dict[str, str] | None = None,
) -> dict[str, Any]:
    name = (profile.get("name") or "").strip()
    parts = name.split()
    github = _url(profile.get("github"))
    years = profile.get("years_experience")
    values: dict[str, Any] = {
        "email": profile.get("email") or "",
        "full_name": name,
        "first_name": parts[0] if parts else "",
        "last_name": " ".join(p for p in parts[1:] if not re.fullmatch(r"[A-Za-z]\.?", p)),
        "phone": profile.get("phone") or "",
        "linkedin": _url(profile.get("linkedin")),
        "github": github,
        "website": _url(profile.get("website")) or github,
        "location": location,
        "years": str(years) if isinstance(years, int) and years > 0 else "",
        "cover_letter": cover,
        "resume": files.get("resume"),
        "cover_file": files.get("cover_file"),
    }
    # Saved answers join the same value map, keyed by canonical answer key or
    # normalized question label. Resume kinds are already set above and are
    # never overridden by an answer-bank entry.
    for key, answer in (answers or {}).items():
        if key and answer and key not in values:
            values[key] = answer
    return values


def _packet_files(profile: dict[str, Any], cover: str, resume_path: str, packet_key: str) -> dict[str, Path]:
    """Copy the resume under a human-friendly name so employers don't receive `<run-id>.pdf`."""
    stem = re.sub(r"[^A-Za-z0-9]+", "_", (profile.get("name") or "Candidate").strip()).strip("_") or "Candidate"
    out = UPLOAD_DIR / "packets" / re.sub(r"[^A-Za-z0-9_-]+", "", packet_key)
    out.mkdir(parents=True, exist_ok=True)
    files: dict[str, Path] = {}
    src = Path(resume_path)
    if src.is_file():
        dest = out / f"{stem}_Resume{src.suffix.lower()}"
        dest.write_bytes(src.read_bytes())
        files["resume"] = dest
    if cover.strip():
        letter = out / f"{stem}_Cover_Letter.txt"
        letter.write_text(cover, encoding="utf-8")
        files["cover_file"] = letter
    return files
