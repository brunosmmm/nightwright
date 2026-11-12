"""Effort ceiling (SPEC-0557): no persona run is sent above `agentic.effort_ceiling`.

`agentic.effort_ceiling` defaults to `high`; `null` turns the ceiling off (today's behaviour,
byte for byte). One clamp, `clamp_effort`, used at the two points a run's effort is resolved
(`personas.build_request`, `spec_review`): the profile effort before any backend mapping, and
the tier overlay's effort. `apply` then rewrites what the mapping may already have produced:
an explicit Cursor id (`grok-4.7-xhigh` -> `grok-4.7-high`) and the Pi thinking level.
`effort_clamped_from` is the effort the run would have had, set only when it was lowered."""
from __future__ import annotations

import re

from .profiles import EFFORTS

DEFAULT_CEILING = "high"
# Effort words as they trail a Cursor id; `extra-high` is `xhigh` spelled out (cursor_map).
_RANK = {"low": 0, "medium": 1, "high": 2, "xhigh": 3, "extra-high": 3, "max": 4}
_SUFFIX_RE = re.compile(r"-(low|medium|high|xhigh|extra-high|max)(?P<fast>-fast)?$")


def ceiling(cfg) -> str | None:
    """The configured ceiling: `high` unless set; None when `agentic.effort_ceiling` is null."""
    agentic = (cfg or {}).get("agentic")
    raw = agentic.get("effort_ceiling", DEFAULT_CEILING) if isinstance(agentic, dict) \
        else DEFAULT_CEILING
    if raw is None:
        return None
    if raw not in EFFORTS:
        raise ValueError(f"agentic.effort_ceiling must be one of {', '.join(EFFORTS)} or null, "
                         f"got {raw!r}")
    return raw


def clamp_effort(effort, limit) -> tuple:
    """`(effort, clamped_from)`: `effort` lowered to `limit` when above it, else unchanged with
    `clamped_from` None. An unknown or missing effort, or no limit, passes through."""
    if limit is None or effort not in EFFORTS or limit not in EFFORTS:
        return effort, None
    if EFFORTS.index(effort) > EFFORTS.index(limit):
        return limit, effort
    return effort, None


def clamp_cursor_id(model_id, limit) -> str:
    """A Cursor id whose trailing effort word is above `limit` gets the `limit` word instead
    (`gpt-5.6-sol-xhigh` -> `gpt-5.6-sol-high`, `...-xhigh-fast` keeps its `-fast`)."""
    raw = str(model_id or "")
    m = _SUFFIX_RE.search(raw)
    if limit is None or not m or limit not in _RANK or _RANK[m.group(1)] <= _RANK[limit]:
        return raw
    return raw[:m.start()] + f"-{limit}" + (m.group("fast") or "")


def apply(cfg, *, effort, model, runner, pi_thinking=None, clamped_from=None) -> dict:
    """The final pass over one resolved spawn: `{effort, model, pi_thinking, clamped_from}`.

    `runner` is the effective backend (`claude | cursor | pi | ...`). `clamped_from` carries an
    earlier clamp of the same run (the profile effort) so the first lowered value is kept."""
    limit = ceiling(cfg)
    new_effort, was = clamp_effort(effort, limit)
    clamped_from = clamped_from or was
    if runner == "cursor":
        model = clamp_cursor_id(model, limit)
    thinking, was_t = clamp_effort(pi_thinking, limit)
    if pi_thinking is not None and runner == "pi":
        clamped_from = clamped_from or was_t
    else:
        thinking = pi_thinking
    return {"effort": new_effort, "model": model, "pi_thinking": thinking,
            "clamped_from": clamped_from}
