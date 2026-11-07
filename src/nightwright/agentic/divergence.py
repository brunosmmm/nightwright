"""SPEC-0247: divergence detection in the implementation loop. A pure reading of a campaign's
ledger: `rounds` cuts it into fix rounds, `assess` computes the divergence signals over them,
`check` does both for the fix that `queue._apply` is about to write. When it trips the campaign
parks `diverging` (the conductor triages: continue, rework or escalate) instead of looping on."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import NamedTuple

from . import personas as PS
from . import store
from .events import _is_transient

DEFAULTS = {"enabled": True, "window": 3, "min_rounds": 2, "churn_ratio": 0.75, "similarity": 0.6}
FIX_REASONS = frozenset({"review_fix", "triage_fix"})
COUNTED_SEVERITIES = frozenset({"must_fix", "major", "minor"})
BLOCKING = frozenset({"must_fix", "major"})
# `implementer` is out: `_record_run` writes each blocker as a `major` finding with one fixed title.
COUNTED_PERSONAS = frozenset(PS.REVIEWER_PERSONAS) | {"review_triager", "gate_triager"}
# SPEC-0261: disk and filesystem failures only. A gate tail is test output, so the bare "429" /
# "overloaded" markers of `events._is_transient` would match `x.py:429: AssertionError`.
ENV_MARKERS = ("enospc", "no space left", "disk full", "errno 28", "edquot", "disk quota",
               "out of inodes", "erofs", "read-only file system")
REPEAT_WEIGHT, NEW_WEIGHT = 1.0, 0.5


class Finding(NamedTuple):
    persona: str
    severity: str
    title: str
    ac: str | None


@dataclass
class Round:
    kind: str                                   # "review" | "gate": the reason that closed it
    findings: list = field(default_factory=list)
    failing_gates: frozenset = frozenset()
    environmental: bool = False                 # SPEC-0261: the round says nothing about convergence

    def blocking(self) -> list:
        return [f for f in self.findings if f.severity in BLOCKING]


def _setting(raw: dict, key: str):
    v, default = raw.get(key), DEFAULTS[key]
    if key == "enabled":
        return v if isinstance(v, bool) else default
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return default
    if key in ("window", "min_rounds"):
        return v if isinstance(v, int) and v >= 2 else default
    return v if 0 < v <= 1 else default


def settings(cfg) -> dict:
    """`agentic.divergence` over the defaults; a wrong-typed or out-of-range key keeps its default."""
    raw = ((cfg or {}).get("agentic") or {}).get("divergence")
    raw = raw if isinstance(raw, dict) else {}
    return {k: _setting(raw, k) for k in DEFAULTS}


# ---- rounds ----------------------------------------------------------------------------------

def _norm(text) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


def key(f: Finding) -> tuple:
    return (_norm(f.title), _norm(f.ac) or None)


def _words(f: Finding) -> set:
    return {w for w in key(f)[0].split() if len(w) >= 3}


def matches(a: Finding, b: Finding, similarity: float = DEFAULTS["similarity"]) -> bool:
    """Same defect: equal keys, or the same `ac` (or none) and title word sets whose Jaccard
    similarity reaches `similarity`. Empty word sets match only on equal keys."""
    ka, kb = key(a), key(b)
    if ka == kb:
        return True
    if ka[1] != kb[1]:
        return False
    wa, wb = _words(a), _words(b)
    if not wa or not wb:
        return False
    return len(wa & wb) / len(wa | wb) >= similarity


def _diverging_park(rec: dict) -> bool:
    return rec.get("reason") == "diverging" or (
        rec.get("reason") == "human_decision" and rec.get("escalated_from") == "diverging")


def _environmental(text) -> bool:
    low = str(text or "").lower()
    return any(m in low for m in ENV_MARKERS)


def _run_environmental(rec: dict) -> bool:
    """An `implementer` run that was blocked by the environment (a) or crashed or was
    rate-limited so the gated tree is incomplete (b). Other personas' runs say nothing about
    why a gate failed."""
    if rec.get("persona") != "implementer":
        return False
    verdict = rec.get("verdict")
    if isinstance(verdict, dict) and verdict.get("status") == "blocked":
        blockers = verdict.get("blockers")
        texts = [str(b) for b in blockers] if isinstance(blockers, list) else []
        if _environmental(" ".join(texts)) or _environmental(verdict.get("summary")):
            return True
    if rec.get("is_error") or rec.get("timed_out"):
        return _is_transient(str(rec.get("result_tail")
                                 or ("timed out" if rec.get("timed_out") else "error")))
    return False


def rounds(records) -> list[Round]:
    """Fix rounds of a ledger (see SPEC-0247 Design): the findings and the last gate result
    between two fix transitions, closed by the second. Records up to the latest `resume` of a
    `diverging` park are dropped, so a resumed campaign has to diverge again on fresh rounds;
    so are those up to a `resume` with `loops_reset` (`--reset-loops`, SPEC-0266), whatever
    park it ended: the conductor's reset gives divergence a fresh start too. A granted extra
    round (SPEC-0461, transition `divergence_extra`) is the same kind of cut: that round is
    judged on what it produces, not on the rounds that tripped the detector."""
    records = list(records)
    cut, diverging = 0, False
    for i, rec in enumerate(records):
        if rec.get("type") == "park":
            diverging = _diverging_park(rec)
        elif rec.get("type") == "resume":
            if diverging or rec.get("loops_reset"):
                cut = i + 1
            diverging = False
        elif rec.get("type") == "transition" and rec.get("to") == "IMPLEMENTING" \
                and rec.get("reason") == "divergence_extra":
            cut = i + 1
            diverging = False
    out: list[Round] = []
    findings: list[Finding] = []
    gate, env = None, False
    for rec in records[cut:]:
        t = rec.get("type")
        if t == "finding":
            persona, severity = rec.get("persona"), rec.get("severity")
            # `_record_run` writes a reviewer's JSON as-is: only strings can be looked up
            if isinstance(persona, str) and isinstance(severity, str) \
                    and persona in COUNTED_PERSONAS and severity in COUNTED_SEVERITIES:
                findings.append(Finding(persona, severity, str(rec.get("title") or ""),
                                        str(rec["ac"]) if rec.get("ac") else None))
        elif t == "gate":
            gate = rec
        elif t == "run.end":
            env = env or _run_environmental(rec)
        elif t == "transition" and rec.get("to") == "IMPLEMENTING" \
                and rec.get("reason") in FIX_REASONS:
            # blocking gates only: an optional gate that always fails says nothing about the loop
            failed = [r for r in (gate or {}).get("results") or []
                      if isinstance(r, dict) and r.get("ok") is False
                      and r.get("required") is not False and isinstance(r.get("name"), str)]
            failing = frozenset(r["name"] for r in failed)
            gate_env = any(_environmental(r.get("tail")) for r in failed)
            out.append(Round("review" if rec["reason"] == "review_fix" else "gate",
                             findings, failing, env or gate_env))
            findings, gate, env = [], None, False
    return out


# ---- signals ---------------------------------------------------------------------------------

def _any_match(f: Finding, group, similarity) -> bool:
    return any(matches(f, g, similarity) for g in group)


def _scores(rs, similarity) -> list[float]:
    """Per round: each blocking finding seen in an earlier round weighs REPEAT_WEIGHT, a new one
    NEW_WEIGHT, so a loop that fixes each blocker while reviewers find different ones is flat."""
    out = []
    for i, r in enumerate(rs):
        earlier = [f for p in rs[:i] for f in p.findings]
        out.append(sum(REPEAT_WEIGHT if _any_match(f, earlier, similarity) else NEW_WEIGHT
                       for f in r.blocking()))
    return out


def _trend(rs, window, similarity=DEFAULTS["similarity"]) -> dict | None:
    if len(rs) < window:
        return None
    scores = _scores(rs, similarity)[-window:]
    if scores[-1] >= 1.0 and all(b >= a for a, b in zip(scores, scores[1:])):
        return {"name": "trend", "evidence": {
            "counts": [len(r.blocking()) for r in rs[-window:]], "scores": scores}}
    return None


def _churn(rs, ratio, similarity) -> dict | None:
    if len(rs) < 3:
        return None
    ratios = []
    for prev, cur in ((rs[-3], rs[-2]), (rs[-2], rs[-1])):
        blocking = cur.blocking()
        if len(blocking) < 2:
            return None
        new = sum(1 for f in blocking if not _any_match(f, prev.findings, similarity))
        ratios.append(round(new / len(blocking), 2))
    if all(r >= ratio for r in ratios):
        return {"name": "churn", "evidence": {"ratios": ratios}}
    return None


def _recurrence(rs, similarity) -> dict | None:
    if len(rs) < 3:
        return None
    last, hits = len(rs) - 1, []
    for f in rs[last].blocking():
        for i in range(last - 1):
            if _any_match(f, rs[i].blocking(), similarity) and any(
                    not _any_match(f, rs[j].blocking(), similarity) for j in range(i + 1, last)):
                hits.append({"title": f.title, "rounds": [i + 1, last + 1]})
                break
    return {"name": "recurrence", "evidence": hits} if hits else None


def _gate_repeat(rs, window) -> dict | None:
    if len(rs) < window:
        return None
    tail = rs[-window:]
    if any(r.kind != "gate" for r in tail):
        return None
    common = frozenset.intersection(*(r.failing_gates for r in tail))
    return {"name": "gate_repeat", "evidence": {"gates": sorted(common)}} if common else None


def assess(rs, cfg_settings: dict | None = None) -> dict | None:
    """`{signals: [{name, evidence}], rounds, excluded_rounds}` when the loop diverges, else
    None. `recurrence` alone trips; so do any two of trend, churn and gate_repeat. Environmental
    gate rounds are dropped first: `rounds` counts the kept ones, and round numbers in the
    evidence index them."""
    s = {**DEFAULTS, **(cfg_settings or {})}
    kept = [r for r in rs if not (r.kind == "gate" and r.environmental)]
    excluded = len(rs) - len(kept)
    rs = kept
    if not s["enabled"] or len(rs) < s["min_rounds"]:
        return None
    window = s["window"]
    recurrence = _recurrence(rs, s["similarity"])
    others = [x for x in (_trend(rs, window, s["similarity"]), _churn(rs, s["churn_ratio"], s["similarity"]),
                          _gate_repeat(rs, window)) if x]
    if recurrence is None and len(others) < 2:
        return None
    return {"signals": ([recurrence] if recurrence else []) + others, "rounds": len(rs),
            "excluded_rounds": excluded}


def _line(title, width: int = 80) -> str:
    """A finding title on one line, cut at `width` characters."""
    text = " ".join(str(title).split())
    return text if len(text) <= width else text[:width - 1] + "…"


def summary(div: dict) -> str:
    """One line for the park detail, e.g. `recurrence: "x" (r1, r3) · trend 3→3→4`."""
    parts = []
    for sig in div["signals"]:
        ev = sig["evidence"]
        if sig["name"] == "recurrence":
            parts.append("recurrence: " + ", ".join(
                f'"{_line(h["title"])}" ({", ".join(f"r{n}" for n in h["rounds"])})' for h in ev))
        elif sig["name"] == "trend":
            parts.append("trend " + "→".join(f"{c:g}" for c in ev["scores"]))
        elif sig["name"] == "churn":
            parts.append("churn " + "/".join(f"{r:.0%}" for r in ev["ratios"]))
        else:
            parts.append("gate_repeat " + ", ".join(_line(g) for g in ev["gates"]))
    if div.get("excluded_rounds"):
        parts.append(f"{div['excluded_rounds']} environmental round(s) excluded; r-numbers count kept rounds")
    return " · ".join(parts)


def check(cfg, state: dict, pending_events, kind: str | None = None) -> dict | None:
    """Assess the loop as it stands with the fix `queue._apply` is about to write: the ledger
    plus `pending_events` ((type, payload) pairs) as the last round. `kind` is "review" or
    "gate" (default: a campaign in TRIAGING closes a gate round). Returns the `loop_divergence`
    payload or None. A ledger read error appends a `note` to `pending_events` and returns None."""
    s = settings(cfg)
    if not s["enabled"]:
        return None
    try:
        records = store.read_ledger(cfg, state["id"],
                                    types=["finding", "gate", "transition", "park", "resume",
                                                  "run.end"])
    except (OSError, ValueError) as e:
        pending_events.append(("note", {"text": f"divergence check skipped: {e}",
                                        "author": "queue"}))
        return None
    kind = kind or ("gate" if state.get("state") == "TRIAGING" else "review")
    records += [{"type": etype, **payload} for etype, payload in pending_events]
    records.append({"type": "transition", "to": "IMPLEMENTING",
                    "reason": "review_fix" if kind == "review" else "triage_fix"})
    div = assess(rounds(records), s)
    if div is not None:
        div["loop"] = int(((state.get("counters") or {}).get("loops")) or 0)
    return div
