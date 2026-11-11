"""Findings record filter (SPEC-0412): `parse_record_filter` and `query_records` over the history
layer's `nw.history.record.v1` records (`agentic/history.py`, the same data as
`nw campaign records`).

Pure and Textual-free: nothing here imports Textual, renders anything, or writes a file other
than through the history layer's disposable `cache_dir` cache. `matches` is the authority for the
grammar. `query_records` makes one unfiltered history call and applies `matches` to it, so no
flag is passed down (a narrowed call plus a recount for `total_records` cost twice the history
layer's warm-path time); the `history` rows of `GRAMMAR_ROWS` only say which `nw campaign records`
flag has the same semantics.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field

from ..agentic import history as H

KINDS = frozenset({
    "finding", "verdict", "triage", "decision", "escalation", "answer", "human_decision", "mail",
    "followup", "spec_review", "close_check", "note", "flaky", "sentinel",
})
# `must_fix` is what the ledger writes today for the top severity (the spec lists `blocker`).
SEVERITIES = frozenset({"blocker", "must_fix", "major", "minor", "nit", "info"})
STATUSES = frozenset({"open", "fixed", "waived", "filed", "carried", "answered", "withdrawn"})
NONE = "none"

# (token, where): each grammar row and who implements it, in the order of the spec's table.
# `history` = the matching `nw campaign records` flag has the same semantics; `tab` = only `matches`
# implements it. `query_records` itself applies every row through `matches`.
GRAMMAR_ROWS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("agent:X", ("history", "tab")),            # history for a full agent id, else tab
    ("kind:X", ("history",)),
    ("project:X", ("tab",)),
    ("severity:X", ("history", "tab")),         # history for a value, tab for `none`
    ("status:X", ("history", "tab")),           # history for a value, tab for `none`
    ("campaign:X", ("tab",)),
    ("since:D / until:D", ("history",)),        # absolute --since / --until, relative resolved
    ("anything else", ("history", "tab")),      # first term history (--query), the rest tab
)

_TOKEN = re.compile(r'(?:[^\s"]+|"[^"]*"?)+')
_REL = re.compile(r"(\d{1,6})([dh])", re.IGNORECASE)
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_KEYS = ("agent", "kind", "project", "severity", "status", "campaign", "since", "until")
_CLOSED = {"kind": KINDS, "severity": SEVERITIES, "status": STATUSES}


def _now(now) -> dt.datetime:
    now = now or dt.datetime.now()
    return (now.astimezone() if now.tzinfo is None else now).astimezone(dt.timezone.utc)


def _resolve_bound(text: str, now: dt.datetime, *, until: bool) -> dt.datetime:
    """`YYYY-MM-DD` (local day: start for since, start of the next day for until) or `7d`/`12h`
    relative to `now`. Raises ValueError (OverflowError included) on anything else."""
    rel = _REL.fullmatch(text)
    if rel:
        n = int(rel.group(1))
        return now - (dt.timedelta(days=n) if rel.group(2).lower() == "d"
                      else dt.timedelta(hours=n))
    if not _DATE.fullmatch(text):
        raise ValueError(text)
    day = dt.date.fromisoformat(text)
    if until:
        day += dt.timedelta(days=1)
    return dt.datetime.combine(day, dt.time.min).astimezone().astimezone(dt.timezone.utc)


@dataclass(frozen=True)
class RecordFilter:
    agent: str | None = None
    kind: str | None = None
    project: str | None = None
    severity: str | None = None
    status: str | None = None
    campaign: str | None = None
    since: dt.datetime | None = None       # resolved at parse time (UTC); `bounds()` re-resolves
    until: dt.datetime | None = None       # exclusive upper bound
    terms: tuple[str, ...] = ()
    problems: tuple[str, ...] = ()
    impossible: frozenset[str] = frozenset()
    since_text: str | None = field(default=None, compare=False)
    until_text: str | None = field(default=None, compare=False)

    @property
    def is_empty(self) -> bool:
        return not (self.agent or self.kind or self.project or self.severity or self.status
                    or self.campaign or self.since_text or self.until_text or self.terms
                    or self.impossible)

    def bounds(self, now=None) -> tuple[dt.datetime | None, dt.datetime | None]:
        """(since, until) in UTC, relative values resolved against `now` (default: current time)."""
        now = _now(now)
        return tuple(_resolve_bound(text, now, until=is_until) if text else None
                     for text, is_until in ((self.since_text, False), (self.until_text, True)))

    def describe(self) -> str:
        """One-line canonical form, e.g. `kind:finding severity:major "two words"`."""
        def quote(value):
            return f'"{value}"' if re.search(r"\s", value) else value
        parts = [f"{key}:{quote(value)}" for key, value in (
            ("agent", self.agent), ("kind", self.kind), ("project", self.project),
            ("severity", self.severity), ("status", self.status), ("campaign", self.campaign),
            ("since", self.since_text), ("until", self.until_text)) if value]
        parts += [quote(term) for term in self.terms]
        return " ".join(parts)


def parse_record_filter(text, now=None) -> RecordFilter:
    """Parse `key:value` tokens plus free text. Never raises: invalid values are reported in
    `problems`, listed in `impossible` and then match nothing."""
    try:
        return _parse(str(text or ""), now)
    except Exception as exc:                                  # defensive: a filter box is live
        return RecordFilter(problems=(f"unparsable filter: {exc}",), impossible=frozenset({"text"}))


def _parse(text: str, now) -> RecordFilter:
    now_utc = _now(now)
    values: dict[str, str] = {}
    terms: list[str] = []
    problems: list[str] = []
    impossible: set[str] = set()
    for match in _TOKEN.finditer(text):
        raw = match.group(0)
        key, sep, value = raw.partition(":")
        key = key.lower()
        if raw.startswith('"') or not sep or key not in _KEYS:
            term = raw.replace('"', "").strip().lower()
            if term:
                terms.append(term)
            continue
        value = value.replace('"', "").strip()
        if not value:
            problems.append(f"empty value for {key}:")
            continue
        if key in _CLOSED:
            value = value.lower()
        values[key] = value
        impossible.discard(key)
    flt = {k: v for k, v in values.items() if k in ("agent", "project", "campaign")}
    for key, closed in _CLOSED.items():
        if key not in values:
            continue
        flt[key] = values[key]
        if values[key] not in closed and not (values[key] == NONE and key != "kind"):
            problems.append(f"unknown {key} {values[key]!r}")
            impossible.add(key)
    resolved: dict[str, dt.datetime] = {}
    for key in ("since", "until"):
        if key not in values:
            continue
        try:
            resolved[key] = _resolve_bound(values[key].lower(), now_utc, until=key == "until")
            flt[f"{key}_text"] = values[key].lower()
        except (ValueError, OverflowError):
            problems.append(f"bad date {values[key]!r} for {key}")
            impossible.add(key)
    return RecordFilter(
        **flt, since=resolved.get("since"), until=resolved.get("until"), terms=tuple(terms),
        problems=tuple(problems), impossible=frozenset(impossible),
    )


def _matcher(flt: RecordFilter, now=None):
    since, until = flt.bounds(now)
    agent, project = flt.agent, (flt.project or "").casefold()
    campaign = (flt.campaign or "").casefold()
    terms = tuple(term.casefold() for term in flt.terms)

    def one(rec) -> bool:
        if flt.impossible:
            return False
        if agent is not None:
            have = rec.get("agent")
            if not isinstance(have, str) or not (have == agent or have.startswith(agent + ":")):
                return False
        if flt.kind is not None and rec.get("kind") != flt.kind:
            return False
        if flt.project is not None and str(rec.get("project") or "").casefold() != project:
            return False
        for key in ("severity", "status"):
            want = getattr(flt, key)
            if want is not None and rec.get(key) != (None if want == NONE else want):
                return False
        if flt.campaign is not None:
            have = rec.get("campaign")
            if flt.campaign == NONE:
                if have:
                    return False
            elif campaign not in str(have or "").casefold():
                return False
        if since is not None or until is not None:
            stamp = H._parse_ts(rec.get("ts"))
            if stamp is None or (since is not None and stamp < since) \
                    or (until is not None and stamp >= until):
                return False
        if terms:
            hay = f"{rec.get('title') or ''} {rec.get('text') or ''}".casefold()
            if not all(term in hay for term in terms):
                return False
        return True
    return one


def matches(record: dict, flt: RecordFilter, now=None) -> bool:
    """The reference predicate for the grammar, over the fields of `nw.history.record.v1`."""
    return _matcher(flt, now)(record)


@dataclass(frozen=True)
class RecordQueryResult:
    records: list = field(default_factory=list)
    total_matching: int = 0
    total_records: int = 0
    problems: tuple[str, ...] = ()
    error: str | None = None


def _sort_key(rec):
    return (H._time_key(rec.get("ts")), str(rec.get("id") or ""))


def query_records(cfg, flt: RecordFilter, now=None) -> RecordQueryResult:
    """Records matching `flt`, newest first. Synchronous file I/O through the history layer; call
    it from a worker. A history-layer failure is returned in `error`, never raised."""
    if flt.impossible:
        return RecordQueryResult(problems=flt.problems)
    try:
        # One unfiltered call: `total_records` is its length and `matches` does all the narrowing.
        # A narrowed call plus a recount doubled the history layer's cost, which is the budget.
        rows = H.history_records(cfg)
        if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
            raise ValueError("history returned unexpected data")
        total = len(rows)
        keep = _matcher(flt, now)
        out = [rec for rec in rows if keep(rec)]
        out.sort(key=_sort_key, reverse=True)
    except Exception as exc:                                  # corrupt cache, unreadable log, ...
        return RecordQueryResult(problems=flt.problems, error=f"{type(exc).__name__}: {exc}")
    return RecordQueryResult(
        records=out, total_matching=len(out), total_records=total, problems=flt.problems,
    )
