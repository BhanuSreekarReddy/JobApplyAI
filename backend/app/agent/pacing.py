"""Human pacing for the form filler — every delay lives here.

Employer anti-spam systems score how a form was filled, not just what
was submitted: values set instantly by script, in DOM order, with no
pauses, read as automation. The filler therefore types text fields with
real key events at jittered per-keystroke delays, pauses briefly before
each field, and works down the page in visual order (see formfill).

Two controls, both in app.config settings:
- FORMFILL_HUMAN_PACING=false — the kill switch. The filler reverts to
  instant value-setting, exactly the pre-pacing behavior.
- FORMFILL_PACING_SCALE — multiplies every delay. Production runs at 1;
  the test suite sets 0 so the interaction pattern still runs (clicks,
  key events, ordering) without the wall-clock waits. Tests may also
  override the scale in-process with set_scale().

Bounds: a keystroke lands every 30–90 ms (jittered), compressed for
long values so one field's typing never exceeds roughly
FIELD_TYPE_BUDGET_MS — a person pastes or types fast in a long cover
letter, they do not spend minutes on it. Pauses between fields run
300–1500 ms. A typical ten-field form fills in roughly 15–40 seconds.
"""
from __future__ import annotations

import asyncio
import random

from ..config import settings

KEY_DELAY_MS = (30.0, 90.0)
FIELD_PAUSE_MS = (300.0, 1500.0)
FIELD_TYPE_BUDGET_MS = 4000.0
MIN_KEY_DELAY_MS = 5.0

_scale_override: float | None = None


def set_scale(scale: float | None) -> None:
    """Override the pacing scale in-process (tests); None restores the
    configured FORMFILL_PACING_SCALE."""
    global _scale_override
    _scale_override = scale


def scale() -> float:
    if _scale_override is not None:
        return max(0.0, _scale_override)
    try:
        return max(0.0, float(settings.formfill_pacing_scale))
    except (TypeError, ValueError):
        return 1.0


def enabled() -> bool:
    """The interaction pattern (click, type, pauses). False only when
    the kill switch is off — a zero scale keeps the pattern and removes
    only the waits."""
    return bool(settings.formfill_human_pacing)


def keystroke_delay_ms(value_len: int) -> float:
    """One keystroke's delay for a value of `value_len` characters:
    jittered 30–90 ms, scaled down for long values so the whole field
    stays within the per-field typing budget."""
    delay = random.uniform(*KEY_DELAY_MS)
    if value_len > 0:
        per_char_budget = FIELD_TYPE_BUDGET_MS / value_len
        if per_char_budget < delay:
            delay = max(MIN_KEY_DELAY_MS, per_char_budget)
    return delay * scale()


# The fixed per-key delay combobox/location typing used before pacing.
LEGACY_PRESS_DELAY_MS = 40.0


def press_delay_ms(value_len: int) -> float:
    """Per-key delay for press_sequentially call sites (combobox and
    location filters): the paced delay while pacing is on, the legacy
    fixed delay when the kill switch is off."""
    if not enabled():
        return LEGACY_PRESS_DELAY_MS
    return keystroke_delay_ms(value_len)


def field_pause_ms() -> float:
    """The pause before interacting with the next field."""
    return random.uniform(*FIELD_PAUSE_MS) * scale()


async def pause_between_fields() -> None:
    if enabled():
        delay = field_pause_ms()
        if delay > 0:
            await asyncio.sleep(delay / 1000.0)
