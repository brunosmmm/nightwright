"""Queue state machine and loop (SPEC-0176): the engine of the agentic workflow.

`plan_step` is a pure function from campaign state (+ profile + project config) to the one
step the campaign needs next; `tick` executes those steps — agent steps (implement, gate,
triage, review) concurrently in a thread pool whose steps carry across ticks (SPEC-0213),
repository steps (open, close) serially — and applies every result on the main thread under the campaign lock, so `state.json` and `ledger.jsonl`
can never disagree: every transition goes through `_transition`, which records the event in
the same locked section that writes the state.

Invariant (SPEC-0169): Python decides every transition; personas only produce commits and one
structured verdict. Gates are truth; verdicts steer. Before every spawn the campaign cap is
checked pessimistically (`total + persona budget > cap` ⇒ PARKED). Humans act only on
`PARKED` items (`park` / `resume` / `abandon`)."""
from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path

import yaml

from .. import specs as SP
from ..specmeta import split_frontmatter
from . import close as CL
from . import followups as FU
from . import gates as G
from . import gitops as git
from . import lease as L
from . import personas as PS
from . import project as P
from . import spec_review as SR
from . import spec_write as SW
from . import schemas as SC
from . import store
from .codewatch import _CODE_PATHS, _git_out, code_changes, code_checkout  # noqa: F401
from .gitops import GitError
from .profiles import (SEVERITIES, persona_settings, resolve_profile, review_params,
                       reviewers_of, select_profile, triager_enabled)
from .events import settings as events_settings
from .runner import RunResult, select_runner

STATES = ("QUEUED", "WAITING", "OPENING", "SPEC_WRITING", "IMPLEMENTING", "GATING", "TRIAGING", "REVIEWING",
          "CLOSING", "PARKED", "MERGED", "HANDED_OFF", "ABANDONED")
# Slots are for campaigns that are open and delivering (ACTIVE_STATES). QUEUED = the backlog,
# never started. WAITING (SPEC-0200) = started (worktree, commits, counters, brief) and ready to
# continue, but no slot was free; it takes the next free slot ahead of the backlog and resumes
# at `waiting.resume_to` without re-opening. PARKED = needs a decision; holds no slot.
TERMINAL_STATES = store.TERMINAL_STATES
ACTIVE_STATES = frozenset({"OPENING", "SPEC_WRITING", "IMPLEMENTING", "GATING", "TRIAGING", "REVIEWING",
                           "CLOSING"})
RESUMABLE_STATES = ("SPEC_WRITING", "IMPLEMENTING", "GATING", "REVIEWING", "CLOSING", "QUEUED")
PARK_REASONS = frozenset({
    "cost_cap", "loop_exhausted", "implementer_blocked", "triage_escalated", "open_failed",
    "rebase_conflict", "main_dirty", "main_not_on_base", "base_moving", "gh_failed", "human",
    "review_escalated", "config_invalid", "spec_writer_unavailable", "spec_write_failed",
    "spec_review_unavailable",
    "gates_failed", "worktree_dirty",                  # SPEC-0233: merge campaigns
})
# SPEC-0222: who a park waits for. The human only for their own checkout and explicit
# escalations (`human_decision` is reserved for IDEA-087); everything else is the conductor's.
PARK_OWNERS = {"main_dirty": "human", "main_not_on_base": "human", "human_decision": "human"}


def park_owner(reason) -> str:
    return PARK_OWNERS.get(reason or "", "conductor")


# How many times in a row the implementer may report `blocked` before the campaign parks.
BLOCKED_STREAK_MAX = 2
# SPEC-0215: rejected spec writes retried before the campaign parks `spec_write_failed`.
SPEC_WRITE_RETRY_MAX = 1
# Malformed triage verdicts tolerated per loop before the triage is treated as `escalate`.
TRIAGE_RETRY_MAX = 1
# `retry_gate` dispositions honored per loop before they are treated as `fix`.
GATE_RETRY_MAX = 1
# Malformed conformance-review verdicts tolerated per loop before the review escalates.
REVIEW_RETRY_MAX = 1
# SPEC-0221: conflict-resolver runs per entry into CLOSING before the rebase_conflict park.
MAX_RESOLVE_ATTEMPTS = 2
_STOP_GRACE_S = 30
# With steps in flight, the loop never waits less than this between ticks (no busy spin).
_MIN_INFLIGHT_WAIT_S = 0.1
PRIORITY_MIN, PRIORITY_MAX, PRIORITY_DEFAULT = 1, 5, 3
# SPEC-0233: a merge request is short and usually unblocks a person, so it goes first.
MERGE_PRIORITY_DEFAULT = 2
# A dependency counts as satisfied once its spec reached one of these in the base checkout.
DEPENDENCY_SATISFIED = frozenset({"done", "superseded"})


# ---- pure planning ---------------------------------------------------------------------

@dataclass(frozen=True)
class Step:
    """What a campaign needs next. `kind` ∈ open | write_spec | implement | gate | triage | review |
    close | resolve | park | none; `park` steps carry `reason` / `detail` / `resume_to`."""
    kind: str
    persona: str | None = None
    reason: str | None = None
    detail: str | None = None
    resume_to: str | None = None
    # SPEC-0201: a `review` step names every pending reviewer; `decide_reviews` fills the rest.
    personas: tuple = ()
    missing: tuple = ()
    escalated: tuple = ()
    combined: dict | None = None


def _cap_blocks(state: dict, profile: dict, persona) -> str | None:
    """Pessimistic cap check: a run may spend up to its budget, so never start one that could
    cross the campaign cap. `persona` is a name or a list of names (a fan-out is checked as
    the sum of its budgets). Returns the park detail or None."""
    cost = state.get("cost") or {}
    cap = cost.get("cap_usd")
    if cap is None:
        return None
    names = [persona] if isinstance(persona, str) else list(persona)
    budget = sum(float(persona_settings(profile, p).get("max_budget_usd") or 0.0) for p in names)
    total = float(cost.get("total_usd") or 0.0)
    if total + budget > float(cap):
        who = f"a {names[0]} run" if len(names) == 1 else f"{', '.join(names)} runs"
        return f"spent ${total:.2f}; {who} may cost ${budget:.2f}; cap ${float(cap):.2f}"
    return None


def _valid_verdicts(state: dict, profile: dict) -> dict:
    """`{persona: stored verdict}` for the reviewers with a valid verdict of the current loop
    (SPEC-0201), in profile order. A stale-loop entry, or an old-shape `last_review` without
    `verdicts`, counts as none."""
    stored = (state.get("last_review") or {}).get("verdicts") or {}
    loop = int((state.get("counters") or {}).get("loops", 0))
    return {p: stored[p] for p in reviewers_of(profile)
            if isinstance(stored.get(p), dict) and stored[p].get("loop") == loop}


def _pending_reviewers(state: dict, profile: dict) -> list:
    """Reviewers without a valid current-loop verdict; the full list when none is pending
    (a review is never skipped)."""
    valid = _valid_verdicts(state, profile)
    return [p for p in reviewers_of(profile) if p not in valid] or reviewers_of(profile)


def campaign_label(state: dict) -> str:
    """What a human sees for a campaign: its spec id, `merge:<branch>` for a merge request
    (SPEC-0233), else its idea id."""
    if state.get("spec_id"):
        return str(state["spec_id"])
    if state.get("kind") == "merge":
        return f"merge:{(state.get('merge') or {}).get('branch') or (state.get('repo') or {}).get('branch') or '?'}"
    return str((state.get("idea") or {}).get("id") or "?")


def plan_step(state: dict, profile: dict, project_cfg: dict | None = None) -> Step:
    """Pure: the next step for `state`. Never touches disk."""
    st = state.get("state")
    if state.get("park_requested") and st not in TERMINAL_STATES and st != "PARKED":
        req = state["park_requested"]
        return Step("park", reason="human", detail=req.get("detail") or req.get("reason"),
                    resume_to=_resume_target(st))
    if st in TERMINAL_STATES or st in ("PARKED", "QUEUED"):
        return Step("none")
    if st == "OPENING":
        return Step("open")
    if state.get("kind") == "merge" and st != "CLOSING":
        # SPEC-0233: open goes straight to CLOSING; no persona ever runs for a merge request
        return Step("park", reason="human", detail=f"merge campaign in {st}", resume_to="CLOSING")
    if st == "SPEC_WRITING":
        # SPEC-0215: the spec writer writes (no spec yet) or revises (findings in the brief)
        if (state.get("spec") or {}).get("id") and not state.get("pending_brief"):
            return Step("park", reason="spec_review_unavailable", resume_to="SPEC_WRITING",
                        detail="nothing to revise; resume with --note to have the writer revise")
        blocked = _cap_blocks(state, profile, "spec_writer")
        if blocked:
            return Step("park", persona="spec_writer", reason="cost_cap", detail=blocked,
                        resume_to=st)
        return Step("write_spec", persona="spec_writer")
    if st == "GATING":
        return Step("gate")
    if st == "CLOSING":
        if (state.get("closing") or {}).get("resolve"):         # SPEC-0221
            blocked = _cap_blocks(state, profile, "conflict_resolver")
            if blocked:
                return Step("park", persona="conflict_resolver", reason="cost_cap",
                            detail=blocked, resume_to=st)
            return Step("resolve", persona="conflict_resolver")
        return Step("close")
    if st == "REVIEWING":
        reviewers = reviewers_of(profile)
        if len(_valid_verdicts(state, profile)) == len(reviewers) \
                and (state.get("last_review") or {}).get("triage") == "pending":
            # SPEC-0206: the one exception to "a review is never skipped"; the cap was checked
            # when `triage` became pending
            return Step("review", persona="review_triager", personas=("review_triager",))
        pending = _pending_reviewers(state, profile)
        blocked = _cap_blocks(state, profile, pending)
        if blocked:
            return Step("park", persona=pending[0], personas=tuple(pending), reason="cost_cap",
                        detail=blocked, resume_to=st)
        return Step("review", persona=pending[0], personas=tuple(pending))
    persona, kind = ("implementer", "implement") if st == "IMPLEMENTING" \
        else ("gate_triager", "triage")
    blocked = _cap_blocks(state, profile, persona)
    if blocked:
        return Step("park", persona=persona, reason="cost_cap", detail=blocked, resume_to=st)
    return Step(kind, persona=persona)


class _ReviewGroup:
    """One campaign's reviewer fan-out inside a tick (SPEC-0201): the per-persona results
    (prepared runs fill in after the pool finishes), applied once, in profile order."""

    def __init__(self, state: dict, step: Step):
        self.state, self.step = state, step
        self.results: dict = {}          # persona -> StepResult | Exception
        self.meta: dict = {}             # persona -> (run_id, budget) of prepared runs

    def __bool__(self):
        return bool(self.results or self.meta)


def _active_count(cfg) -> int:
    return sum(1 for s in store.list_campaigns(cfg) if s.get("state") in ACTIVE_STATES)


def _enter_slot_or_wait(cfg, st: dict, to: str, reason: str, *, by="tick", busy=None,
                        slots=None, extra_events=()) -> dict:
    """Move a started campaign to `to`, or to WAITING when `to` needs a slot and none is free
    (SPEC-0200). `busy`/`slots` default to the live counts."""
    if to in ACTIVE_STATES:
        busy = _active_count(cfg) if busy is None else busy
        slots = slots_for(cfg) if slots is None else slots
        if busy >= slots:
            st["waiting"] = {"resume_to": to, "since": store.now_iso(), "reason": reason}
            return _transition(cfg, st, "WAITING", f"{reason}_waiting", by=by,
                               extra_events=extra_events)
    st.pop("waiting", None)
    return _transition(cfg, st, to, reason, by=by, extra_events=extra_events)


def _resume_target(st: str) -> str:
    """Where a human park on an active item resumes to: the step that was interrupted
    (OPENING restarts from QUEUED; TRIAGING re-runs its gates; REVIEWING re-runs the review)."""
    return {"OPENING": "QUEUED", "TRIAGING": "GATING"}.get(st, st if st in RESUMABLE_STATES
                                                            else "IMPLEMENTING")


def decide_triage(state: dict, profile: dict, verdict: dict | None, verdict_ok: bool) -> Step:
    """Pure: the transition a gate-triager verdict implies, given the loop counters.

    - malformed / missing verdict: one retry per loop (stay in TRIAGING), then escalate;
    - `fix`: loops += 1; beyond `loop_max` ⇒ loop_exhausted, else back to IMPLEMENTING;
    - `retry_gate`: once per loop ⇒ GATING, else treated as `fix`;
    - `waive`: only when every failing gate is optional ⇒ CLOSING, else escalate;
    - `escalate` ⇒ PARKED(triage_escalated)."""
    counters = state.get("counters") or {}
    loop_max = int(profile.get("loop_max") or 3)
    if not verdict_ok or not isinstance(verdict, dict):
        if int(counters.get("triage_retries", 0)) < TRIAGE_RETRY_MAX:
            return Step("none", reason="triage_retry")
        return Step("park", reason="triage_escalated",
                    detail="the gate triager returned no usable verdict twice", resume_to="GATING")
    disp = verdict.get("disposition")
    brief = (verdict.get("fix_brief") or "").strip()
    if disp == "retry_gate":
        if int(counters.get("gate_retries", 0)) < GATE_RETRY_MAX:
            return Step("gate", reason="retry_gate")
        disp = "fix"
    if disp == "waive":
        failing = [r for r in ((state.get("last_gate") or {}).get("results") or [])
                   if not r.get("ok")]
        if failing and all(not r.get("required") for r in failing):
            return Step("close", reason="waived")
        return Step("park", reason="triage_escalated",
                    detail="waive requested but a required gate is failing: " + brief,
                    resume_to="GATING")
    if disp == "escalate":
        return Step("park", reason="triage_escalated", detail=brief or "triager escalated",
                    resume_to="GATING")
    # fix (and anything unexpected the schema let through)
    loops = int(counters.get("loops", 0)) + 1
    if loops > loop_max:
        return Step("park", reason="loop_exhausted",
                    detail=f"{loops - 1}/{loop_max} implement loops used; still failing gates: "
                           + brief, resume_to="IMPLEMENTING")
    return Step("implement", reason="triage_fix")


def norm_finding_title(title) -> str:
    """Lower-case, whitespace-collapsed title: the identity of a finding across reviewers
    (SPEC-0203; SPEC-0207 reuses it for its follow-up key)."""
    return " ".join(str(title or "").lower().split())


def finding_detail(f: dict):
    """A finding's `detail` with its `trigger` folded in (SPEC-0202), so the ledger event and
    the implementer brief carry the same text."""
    detail, trigger = f.get("detail"), f.get("trigger")
    if detail and trigger:
        return f"{detail}\ntrigger: {trigger}"
    return f"trigger: {trigger}" if trigger else detail


def _covers(finding_ac, entry_ac) -> bool:
    """Does a finding's `ac` cover an `ac_assessment` entry's `ac`? Equal after `norm`, or one
    contains the other with length >= 8; an empty `ac` covers nothing."""
    a, b = norm_finding_title(finding_ac), norm_finding_title(entry_ac)
    if not a or not b:
        return False
    return a == b or (a in b and len(a) >= 8) or (b in a and len(b) >= 8)


def triage_reviews(verdicts: dict, profile: dict, waived=()) -> dict:
    """Pure (SPEC-0203): score and route the reviewers' verdicts `{persona: verdict}` (valid
    verdicts, in profile order) into `{outcome, score, score_only, blocking, advisory,
    waivable_weight, fixing, synthetic}`.

    Normalize (finding ids, synthetic `major`s for an unexplained `fix` and for unmet ACs,
    de-duplication across reviewers) ⇒ waive (`waived` ids, `minor`/`nit` only) ⇒ a `fix`
    verdict survives iff unexplained or backed by a `major`/`must_fix` ⇒ score
    `max(0, 100 - Σ weight)` ⇒ `fix` on a `must_fix`, a surviving `fix` verdict, an unmet AC
    or `score < pass_score`. `fixing` = the personas whose `fix` verdict survived; `synthetic`
    = ids of the synthetic findings."""
    pass_score, weights = review_params(profile)
    personas = list(verdicts)
    own = {}                      # persona -> its finding records, in order
    wants_fix, unmet = {}, False
    for p in personas:
        v = verdicts[p]
        raw = [f for f in v.get("findings") or [] if isinstance(f, dict)]
        recs = []
        for i, f in enumerate(raw):
            sev = f.get("severity") if f.get("severity") in SEVERITIES else "minor"
            recs.append({"id": f"{p}#{i}", "personas": [p], "severity": sev,
                         "title": f.get("title") or "", "detail": finding_detail(f),
                         "files": list(f.get("files") or []), "ac": f.get("ac"),
                         "category": f.get("category"), "synthetic": False})
        wants_fix[p] = v.get("verdict") == "fix" or any(r["severity"] == "must_fix"
                                                       for r in recs)
        if v.get("verdict") == "fix" and not raw:
            brief = (v.get("fix_brief") or "").strip()[:80]
            recs.append({"id": f"{p}#fix", "personas": [p], "severity": "major",
                         "title": brief or f"{p} returned fix without findings", "detail": None,
                         "files": [], "ac": None, "category": None, "synthetic": True})
        for j, entry in enumerate(v.get("ac_assessment") or []):
            if not isinstance(entry, dict) or entry.get("met") is not False:
                continue
            unmet = True
            if any(not r["synthetic"] and r["severity"] in ("major", "must_fix")
                   and _covers(r["ac"], entry.get("ac")) for r in recs):
                continue
            recs.append({"id": f"{p}#ac{j}", "personas": [p], "severity": "major",
                         "title": str(entry.get("ac") or ""), "detail": entry.get("evidence"),
                         "files": [], "ac": entry.get("ac"), "category": None,
                         "synthetic": True})
        own[p] = recs
    # 1d: identical real findings (same files and title) become one merged finding
    rank = {s: i for i, s in enumerate(SEVERITIES)}
    merged, groups = [], {}                   # all findings in order; key -> merged record
    belongs = {p: [] for p in personas}       # persona -> merged records it reported
    for p in personas:
        for r in own[p]:
            key = (tuple(sorted(r["files"])), norm_finding_title(r["title"]))
            m = groups.get(key) if r["files"] and not r["synthetic"] else None
            if m is None:
                m = r
                merged.append(m)
                if r["files"] and not r["synthetic"]:
                    groups[key] = m
            else:
                if rank[r["severity"]] < rank[m["severity"]]:      # strictly higher wins
                    m.update(id=r["id"], severity=r["severity"], detail=r["detail"],
                             ac=r["ac"], category=r["category"])
                if p not in m["personas"]:
                    m["personas"].append(p)
            if all(m is not x for x in belongs[p]):
                belongs[p].append(m)
    # 2: waive (minor/nit only; synthetic findings never)
    waived = set(waived or ())
    gone = {id(m) for m in merged if m["id"] in waived and not m["synthetic"]
            and m["severity"] in ("minor", "nit")}
    surviving = [m for m in merged if id(m) not in gone]
    # 3: a fix verdict survives iff backed by a surviving major/must_fix (synthetics are majors)
    fixing = [p for p in personas if wants_fix[p] and any(
        id(m) not in gone and m["severity"] in ("major", "must_fix") for m in belongs[p])]
    # 4/5: score and outcome
    score = max(0, 100 - sum(weights[m["severity"]] for m in surviving))
    must = any(m["severity"] == "must_fix" for m in surviving)
    hard = must or bool(fixing) or unmet
    outcome = "fix" if hard or score < pass_score else "pass"
    score_only = outcome == "fix" and not hard
    # 6: blocking and advisory
    if outcome == "pass":
        blocking = []
    elif score_only:
        blocking = sorted(surviving, key=lambda m: -weights[m["severity"]])
    else:
        blocking = [m for m in surviving if m["severity"] in ("must_fix", "major")]
    advisory = [m for m in surviving if m not in blocking]
    keys = ("id", "personas", "severity", "title", "detail", "files", "ac", "category")
    out = lambda ms: [{k: m[k] for k in keys} for m in ms]
    return {"outcome": outcome, "score": score, "score_only": score_only,
            "blocking": out(blocking), "advisory": out(advisory),
            "waivable_weight": sum(weights[m["severity"]] for m in surviving
                                   if m["severity"] in ("minor", "nit")),
            "fixing": fixing, "synthetic": [m["id"] for m in surviving if m["synthetic"]]}


def _bullet(f: dict) -> str:
    files = ", ".join(f.get("files") or [])
    return (f"- [{f['severity']}] {f['title']}" + (f" ({files})" if files else "")
            + (f": {f['detail']}" if f.get("detail") else ""))


def review_brief(verdicts: dict, tri: dict, pass_score) -> dict:
    """Pure: the fix brief a `fix` triage implies (SPEC-0203) as `{fix_brief, must_fix,
    personas}` (`personas` only with several reviewers). One reviewer keeps SPEC-0187's shape
    (its own brief verbatim; `must_fix` entries without `id` / `severity` / `personas`)."""
    blocking = tri["blocking"]
    tail = ([f"Review score {tri['score']} is below the pass score {pass_score}; address the "
             f"highest-weight findings first."] if tri["score_only"] else [])
    tail += [_bullet(f) for f in blocking]
    if len(verdicts) == 1:
        persona = next(iter(verdicts))
        brief = verdicts[persona].get("fix_brief")
        if not (brief or "").strip() and (tri["score_only"] or any(
                f["id"] in tri["synthetic"] for f in blocking)):
            brief = "\n".join(tail)
        return {"fix_brief": brief, "must_fix": [
            _with_category({"ac": f["ac"], "title": f["title"], "detail": f["detail"],
                            "files": f["files"]}, f) for f in blocking]}
    head = "\n\n".join(f"[{p}] {(verdicts[p].get('fix_brief') or '').strip()}".rstrip()
                       for p in tri["fixing"])
    reporters = {p for f in blocking for p in f["personas"]} | set(tri["fixing"])
    return {"fix_brief": "\n\n".join(x for x in (head, "\n".join(tail)) if x),
            "personas": [p for p in verdicts if p in reporters],
            "must_fix": [_with_category({"ac": f["ac"], "title": f["title"],
                                         "detail": f["detail"], "files": f["files"],
                                         "id": f["id"], "severity": f["severity"],
                                         "personas": f["personas"]}, f) for f in blocking]}


def _with_category(item: dict, f: dict) -> dict:
    """A brief entry carries the finding's `category` when it has one (SPEC-0202)."""
    if f.get("category"):
        item["category"] = f["category"]
    return item


def _retries_used(counters: dict, persona: str, n_reviewers: int) -> int:
    """Retries `persona` used in this review: its own counter, else (one reviewer only) the
    legacy int `review_retries`, else 0."""
    by = counters.get("review_retries_by_persona") or {}
    if persona in by:
        return int(by[persona])
    return int(counters.get("review_retries", 0)) if n_reviewers == 1 else 0


def needs_triager(result: dict, profile: dict) -> bool:
    """Pure (SPEC-0206): could the review triager change the outcome of `triage_reviews`
    `result`? Only a score-only `fix` that waiving every waivable finding would lift to the pass
    score, with `review.triager` on."""
    if not triager_enabled(profile) or result.get("outcome") != "fix" \
            or not result.get("score_only"):
        return False
    return result["score"] + result["waivable_weight"] >= review_params(profile)[0]


def decide_reviews(state: dict, profile: dict, valid: dict, failed=(), waived=()) -> Step:
    """Pure: the transition the reviewers' verdicts imply (SPEC-0187, SPEC-0201).

    `valid` = ordered `{persona: verdict}` of every reviewer with a valid current-loop
    verdict; `failed` = the personas that produced no usable verdict in this pass.
    - a reviewer without a valid verdict is *missing*: no routing while any is missing;
      each missing one gets one retry (`Step("none", reason="review_retry", missing=…)`);
      if any is out of retries ⇒ PARKED(review_escalated) naming those (`escalated`);
    - all valid: `triage_reviews` (with the `waived` ids, SPEC-0206); `pass` ⇒ CLOSING; `fix` ⇒ loops += 1, beyond `loop_max`
      ⇒ loop_exhausted, else back to IMPLEMENTING. `combined` carries the triage,
      the pass score and (on `fix`) the brief.
    The caller mutates the counters."""
    counters = state.get("counters") or {}
    loop_max = int(profile.get("loop_max") or 3)
    reviewers = reviewers_of(profile)
    missing = tuple(p for p in reviewers if p not in valid)
    if missing:
        out = tuple(p for p in missing
                    if _retries_used(counters, p, len(reviewers)) >= REVIEW_RETRY_MAX)
        if out:
            who = "the conformance reviewer" if out == ("conformance_reviewer",) \
                else ", ".join(out)
            return Step("park", reason="review_escalated", missing=missing, escalated=out,
                        detail=f"{who} returned no usable verdict twice", resume_to="REVIEWING")
        return Step("none", reason="review_retry", missing=missing)
    valid = {p: valid[p] for p in reviewers}
    tri = triage_reviews(valid, profile, waived)
    pass_score = review_params(profile)[0]
    combined = {"triage": tri, "pass_score": pass_score, "brief": None}
    if tri["outcome"] == "pass":
        return Step("close", reason="review_pass", combined=combined)
    combined["brief"] = review_brief(valid, tri, pass_score)
    loops = int(counters.get("loops", 0)) + 1
    if loops > loop_max:
        brief = (combined["brief"]["fix_brief"] or "").strip()
        return Step("park", reason="loop_exhausted", combined=combined,
                    detail=f"{loops - 1}/{loop_max} implement loops used; the conformance "
                           f"review still asks for fixes: " + brief, resume_to="IMPLEMENTING")
    return Step("implement", reason="review_fix", combined=combined)


def decide_review(state: dict, profile: dict, verdict: dict | None, verdict_ok: bool) -> Step:
    """Pure: the transition one conformance-reviewer verdict implies (SPEC-0187) — a thin
    one-reviewer wrapper over `decide_reviews`.

    - malformed / missing verdict: one retry per loop (stay in REVIEWING), then
      PARKED(review_escalated);
    - `pass` with no `must_fix` finding ⇒ CLOSING;
    - `fix`, or any `must_fix` finding even under `pass`: loops += 1; beyond `loop_max` ⇒
      loop_exhausted, else back to IMPLEMENTING."""
    solo = dict(profile, reviewers=["conformance_reviewer"])
    ok = bool(verdict_ok) and isinstance(verdict, dict)
    return decide_reviews(state, solo, {"conformance_reviewer": verdict} if ok else {},
                          () if ok else ("conformance_reviewer",))


# ---- backlog: ordering, readiness, admission (SPEC-0180) ----------------------------------

def check_priority(value) -> int:
    """`value` as a priority in 1..5 (1 highest), else ValueError."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = None
    if n is None or isinstance(value, bool) or not PRIORITY_MIN <= n <= PRIORITY_MAX:
        raise ValueError(f"priority must be an integer {PRIORITY_MIN}..{PRIORITY_MAX} "
                         f"(1 highest), got {value!r}")
    return n


def _priority(state: dict) -> int:
    try:
        return check_priority(state.get("priority", PRIORITY_DEFAULT))
    except ValueError:
        return PRIORITY_DEFAULT


def admission_key(state: dict) -> tuple:
    """Admission order: highest priority first, oldest first among equals."""
    return (_priority(state), state.get("created_at") or "", state.get("id") or "")


def pick_admissions(backlog, busy: int, slots: int, readiness) -> list:
    """Pure: the backlog items to admit now, in admission order. Candidates are ready and not
    park-requested; at most `slots - busy` are taken. `readiness(state) -> (ready, blocked_by)`."""
    free = max(0, int(slots) - int(busy))
    picks = []
    for st in sorted(backlog, key=admission_key):
        if len(picks) >= free:
            break
        if st.get("park_requested") or not readiness(st)[0]:
            continue
        picks.append(st)
    return picks


def has_no_spec(st: dict) -> bool:
    """SPEC-0209: an idea campaign whose spec is not written yet. Keys on `kind` first: spec
    campaigns created earlier carry no `spec.id` and keep their old behaviour."""
    return st.get("kind") == "idea" and not (st.get("spec") or {}).get("id")


def idea_is_open(task) -> bool:
    """SPEC-0209: an idea a campaign may still work: IDEA or INCUBATE, or SPECCED with no
    `:SPEC:`."""
    state = task.state or ""
    return state in ("IDEA", "INCUBATE") or (
        state == "SPECCED" and not (task.properties.get("SPEC") or "").strip())


def _idea_task(cfg, idea_id):
    """The idea task for `idea_id` (exact `:ID:` or task id), or None."""
    from .. import org as ORG
    ORG.invalidate_tasks_cache()          # org writes can land within one mtime tick
    for t in ORG.filter_tasks(ORG.load_tasks(cfg), is_idea=True):
        if idea_id in ((t.properties.get("ID") or "").strip(), t.id):
            return t
    return None


def _idea_id_of(st: dict) -> str | None:
    return (st.get("idea") or {}).get("id")


def backlog_readiness(cfg, state: dict) -> tuple[bool, list[str]]:
    """Can this queued campaign be admitted? Re-reads the spec and its `depends_on` from the
    project's base checkout (`repo.root`): the spec must still be `accepted` (`in-progress` for a
    `--force` add) and every dependency `done` or `superseded`. Unknown ids block. An idea
    campaign without a spec (SPEC-0209) is ready while its idea is still open. A merge request
    (SPEC-0233) is ready while its branch exists."""
    if state.get("kind") == "merge":
        repo = state.get("repo") or {}
        branch = repo.get("branch") or ""
        if branch and git.branch_exists(repo.get("root") or ".", branch):
            return True, []
        return False, [f"branch {branch} missing"]
    if has_no_spec(state):
        iid = _idea_id_of(state)
        task = _idea_task(cfg, iid)
        if task is None:
            return False, [f"idea {iid} not found"]
        if idea_is_open(task):
            return True, []
        return False, [f"idea {iid} is {task.state}"]
    root = Path((state.get("repo") or {}).get("root") or "")
    spec_path = root / ((state.get("spec") or {}).get("rel_path") or "")
    try:
        fm, _ = split_frontmatter(spec_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False, ["spec file missing"]
    fm = fm if isinstance(fm, dict) else {}
    blocked = []
    status = str(fm.get("status") or "")
    allowed = ("accepted", "in-progress") if state.get("force") else ("accepted",)
    if status not in allowed:
        blocked.append(f"spec status is {status or 'missing'}")
    deps = fm.get("depends_on") or []
    for dep in [deps] if isinstance(deps, str) else deps:
        dep = str(dep)
        try:
            dep_fm, _ = split_frontmatter(SP._find_spec(spec_path.parent, dep)
                                          .read_text(encoding="utf-8"))
        except (OSError, ValueError):
            blocked.append(f"{dep} not found")
            continue
        dep_status = str((dep_fm or {}).get("status") or "")
        if dep_status not in DEPENDENCY_SATISFIED:
            blocked.append(f"waiting on {dep} ({dep_status or 'no status'})")
    return not blocked, blocked


def backlog_view(cfg, *, readiness=None) -> list[dict]:
    """Every QUEUED campaign in admission order with its live readiness (`wt queue backlog`,
    the `backlog` object of `wt.queue.v1`)."""
    queued = sorted((s for s in store.list_campaigns(cfg) if s.get("state") == "QUEUED"),
                    key=admission_key)
    rows = []
    for pos, st in enumerate(queued, 1):
        ready, blocked_by = (readiness or (lambda s: backlog_readiness(cfg, s)))(st)
        rows.append({"campaign": st.get("id"), "spec_id": st.get("spec_id"),
                     "project": st.get("project"), "priority": _priority(st), "position": pos, "ready": ready,
                     "blocked_by": blocked_by, "age_s": _age(st.get("created_at"))})
        if st.get("kind") == "idea":
            rows[-1]["idea_id"] = _idea_id_of(st)
        if st.get("kind") == "merge":                                   # SPEC-0233
            rows[-1].update(kind="merge", label=campaign_label(st))
    return rows


def _record_backlog(cfg, st: dict, ready: bool, blocked_by: list) -> None:
    """Write the item's `backlog` object and one `backlog` event, only when it changed."""
    cur = st.get("backlog") or {}
    if "ready" in cur and cur["ready"] == ready and list(cur.get("blocked_by") or []) == blocked_by:
        return
    with store.item_lock(cfg, st["id"]):
        st["backlog"] = {"ready": ready, "blocked_by": blocked_by, "since": store.now_iso()}
        store.append_event(cfg, st["id"], "backlog", {"ready": ready, "blocked_by": blocked_by})
        store.write_state(cfg, st["id"], st)


# ---- state helpers -------------------------------------------------------------------------

def _counters(state: dict) -> dict:
    c = state.setdefault("counters", {})
    for k in ("loops", "runs", "gate_runs", "gate_retries", "triage_retries",
              "blocked_streak", "close_attempts", "parks", "review_retries"):
        c.setdefault(k, 0)
    return c


def _cost(state: dict) -> dict:
    c = state.setdefault("cost", {})
    c.setdefault("total_usd", 0.0)
    c.setdefault("by_persona", {})
    return c


def _begin_review(state: dict) -> None:
    """Entering REVIEWING always starts a fresh review (SPEC-0201): clear the previous
    verdicts and per-review counters, and remember the run count so reviewers never see runs
    of this pass-through."""
    counters = _counters(state)
    lr = state.get("last_review") if isinstance(state.get("last_review"), dict) else {}
    for k in ("verdicts", "triage", "triager_run", "waived"):
        lr.pop(k, None)
    lr["entry_run"] = counters["runs"]
    state["last_review"] = lr
    counters["review_retries"] = 0
    counters.pop("review_retries_by_persona", None)


def _transition(cfg, state: dict, to: str, reason: str, *, by="tick", extra_events=()):
    """Record `extra_events` then the transition, and write the state — all under the
    campaign lock, so the ledger and `state.json` cannot disagree. Mutates and returns
    `state`."""
    cid = state["id"]
    with store.item_lock(cfg, cid):
        for etype, payload in extra_events:
            store.append_event(cfg, cid, etype, payload, by=by)
        frm = state.get("state")
        if to != frm:
            if to == "REVIEWING":
                _begin_review(state)
            if to == "CLOSING":                  # SPEC-0220: the close retry budget restarts
                state["closing"] = {"attempts": 0}
            elif frm == "CLOSING":
                state.pop("closing", None)
            state["prev_state"] = frm
            state["state"] = to
            store.append_event(cfg, cid, "transition", {"from": frm, "to": to, "reason": reason},
                               by=by)
        store.write_state(cfg, cid, state)
    return state


def _park(cfg, state: dict, reason: str, detail: str, resume_to: str, *, by="tick",
          extra_events=()):
    _counters(state)["parks"] += 1
    state["park"] = {"reason": reason, "detail": detail, "since": store.now_iso(),
                     "resume_to": resume_to}
    state.pop("park_requested", None)
    events = list(extra_events) + [("park", dict(state["park"]))]
    return _transition(cfg, state, "PARKED", reason, by=by, extra_events=events)


def _record_run(state: dict, persona: str, run_id: str, result, budget: float, *,
                interrupted: bool = False) -> list:
    """Cost + run.end + findings for a finished persona run. Returns ledger events; mutates
    the counters and cost in `state`. `interrupted` is the step's view (the loop was told to
    stop while the run was in flight), which the RunResult cannot know."""
    interrupted = bool(interrupted or result.interrupted)
    cost = _cost(state)
    estimated = result.cost_usd is None
    charged = budget if estimated else result.cost_usd
    cost["total_usd"] = round(float(cost["total_usd"]) + float(charged), 4)
    if estimated:
        cost["estimated_usd"] = round(float(cost.get("estimated_usd") or 0.0) + float(charged), 4)
    cost["by_persona"][persona] = round(float(cost["by_persona"].get(persona, 0.0))
                                        + float(charged), 4)
    events = [("run.end", {
        "persona": persona, "run_id": run_id, "exit_code": result.exit_code,
        "is_error": result.is_error, "timed_out": result.timed_out,
        "interrupted": interrupted, "duration_s": result.duration_s,
        "cost_usd": result.cost_usd, "charged_usd": charged, "cost_estimated": estimated,
        "usage": result.usage,
        "session_id": result.session_id, "verdict_ok": result.verdict_ok,
        "verdict": result.verdict,
        "result_tail": (result.result_text or "")[-400:] if not result.verdict_ok else "",
    }), ("cost", {"persona": persona, "delta_usd": charged, "total_usd": cost["total_usd"],
                  "cap_usd": cost.get("cap_usd"), "estimated": estimated})]
    if estimated:
        reason = ("interrupted" if interrupted else "timed out" if result.timed_out
                  else "no cost reported")
        events.append(("finding", {"persona": persona, "severity": "info", "gate": None,
                                   "title": "run cost unknown; charged its budget as an estimate",
                                   "detail": f"${budget:.2f} charged as an upper bound ({reason})",
                                   "files": []}))
    verdict = result.verdict if isinstance(result.verdict, dict) else {}
    for item in verdict.get("must_fix") or []:
        if isinstance(item, dict):
            events.append(("finding", {"persona": persona, "severity": "must_fix",
                                       "gate": item.get("gate"), "title": item.get("title"),
                                       "detail": item.get("detail"),
                                       "files": list(item.get("files") or [])}))
    for blocker in verdict.get("blockers") or []:
        events.append(("finding", {"persona": persona, "severity": "major", "gate": None,
                                   "title": "implementer blocker", "detail": str(blocker),
                                   "files": []}))
    for item in verdict.get("findings") or []:        # reviewers (SPEC-0187, SPEC-0202)
        if isinstance(item, dict):
            ev = {"persona": persona, "severity": item.get("severity"),
                  "gate": None, "ac": item.get("ac"),
                  "title": item.get("title"), "detail": finding_detail(item),
                  "files": list(item.get("files") or [])}
            if item.get("category"):
                ev["category"] = item["category"]
            events.append(("finding", ev))
    denials = _raw_permission_denials(result)
    if denials:
        events.append(("finding", {"persona": persona, "severity": "info", "gate": None,
                                   "title": "permission denials during the run",
                                   "detail": str(denials)[:800], "files": []}))
    return events


def _raw_permission_denials(result):
    """`permission_denials` from the raw claude result, when the runner saved one."""
    try:
        import json
        if result.raw_path and Path(result.raw_path).is_file():
            obj = json.loads(Path(result.raw_path).read_text(encoding="utf-8"))
            if isinstance(obj, dict):
                return obj.get("permission_denials") or []
    except (OSError, ValueError):
        pass
    return []


def _spec_sha(path) -> str | None:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


# ---- step execution --------------------------------------------------------------------

class ProjectConfigError(ValueError):
    """The project's `.wt/project.yaml` could not be loaded (SPEC-0194): parks as
    `config_invalid`, and the campaign resumes by itself once the config loads again."""


@dataclass
class StepResult:
    """What a step produced. Applied on the main thread by `_apply`."""
    campaign: str
    step: Step
    from_state: str
    run_id: str | None = None
    run_result: object = None
    budget: float = 0.0
    commit_sha: str | None = None
    commit_message: str | None = None
    gate_run: object = None
    close_outcome: object = None
    prepared: object = None                          # SPEC-0220: a close's PreparedClose
    resolution: dict | None = None                   # SPEC-0221: {verified, failed, head}
    error: str | None = None
    interrupted: bool = False
    events_before: list = field(default_factory=list)
    config_error: bool = False
    reviews: list = field(default_factory=list)      # per-reviewer StepResults, profile order
    followups: dict | None = None                    # SPEC-0207: the close step's filer summary
    crashed: str | None = None                       # unexpected worker exception caught in-step
    write: dict | None = None                        # SPEC-0215: a write_spec step's outcome


class _Ctx:
    """Per-tick context: cfg, resolved profiles / project configs, the runner, the stop flag."""

    def __init__(self, cfg, *, stop: threading.Event | None = None, runner=None, lease=None):
        self.cfg = cfg
        self.stop = stop or threading.Event()
        self.lease = lease            # SPEC-0228: where `_busy` declares long main-thread steps
        self._runner = runner
        self._profiles: dict = {}
        self._projects: dict = {}

    @property
    def runner(self):
        if self._runner is None:
            self._runner = select_runner(self.cfg)
        return self._runner

    def profile(self, state) -> dict:
        name = state.get("profile")
        if name not in self._profiles:
            self._profiles[name] = resolve_profile(self.cfg, name)
        return self._profiles[name]

    def project(self, state) -> dict:
        name = state.get("project")
        if name not in self._projects:
            try:
                self._projects[name] = P.project_config(self.cfg, name)
            except ValueError as e:
                raise ProjectConfigError(str(e)) from e
        return self._projects[name]


@contextmanager
def _busy(lease, kind: str, *, campaign: str | None = None, budget_s: float):
    """Declare a long main-thread step (`open` | `close` | `drain`) in the lease info for the
    out-of-process watchdog (SPEC-0228): the loop cannot tick meanwhile, so a silent journal
    is not a stall within `budget_s`. Cleared in `finally`. Without a lease (one-shot ticks,
    tests) it does nothing. A new long main-thread step must use it."""
    if lease is None:
        yield
        return
    prev = lease.info.get("busy")     # a harvest inside the drain can run a nested close
    lease.update_info(busy={"kind": kind, "campaign": campaign, "since": store.now_iso(),
                            "budget_s": budget_s})
    try:
        yield
    finally:
        lease.end_busy(prev)          # one write: drop `busy` and stamp `busy_ended_at`


def _do_open(ctx: _Ctx, state: dict) -> StepResult:
    """Create the worktree + branch, run the project's setup commands, record base sha and
    spec hash. Serial (touches the main repository). Declared `busy` (SPEC-0228): the setup
    commands (`uv sync`, `npm ci`) run here with no timeout — both call sites of `_tick`."""
    with _busy(ctx.lease, "open", campaign=state["id"],
               budget_s=events_settings(ctx.cfg)["open_budget_s"]):
        return _do_open_unwrapped(ctx, state)


def _do_open_unwrapped(ctx: _Ctx, state: dict) -> StepResult:
    res = StepResult(state["id"], Step("open"), "OPENING")
    repo = state.setdefault("repo", {})
    if state.get("kind") == "merge":
        return _do_open_merge(ctx, state, res)
    try:
        pcfg = ctx.project(state)
        root = Path(repo["root"])
        base = repo.get("base") or pcfg.get("base") or "main"
        base_sha = git.rev_parse(root, base)
        worktree = Path(repo["worktree"])
        git.worktree_add(root, worktree, repo["branch"], base)
        for cmd in (pcfg.get("worktrees") or {}).get("setup") or []:
            r = subprocess.run(cmd, shell=True, cwd=str(worktree), capture_output=True, text=True)
            if r.returncode != 0:
                tail = "\n".join(((r.stdout or "") + (r.stderr or "")).splitlines()[-20:])
                raise GitError([cmd], r.returncode, tail)
        repo["base_sha_at_open"] = base_sha
        repo["head"] = git.rev_parse(worktree)
        if not has_no_spec(state):
            spec = state.setdefault("spec", {})
            spec["sha256_at_open"] = _spec_sha(worktree / spec.get("rel_path", ""))
            # SPEC-0186: keep the accepted text so the close step can detect spec drift
            spec_file = worktree / spec.get("rel_path", "")
            if spec.get("rel_path") and spec_file.is_file():
                store.campaign_dir(ctx.cfg, state["id"]).mkdir(parents=True, exist_ok=True)
                (store.campaign_dir(ctx.cfg, state["id"]) / CL.SPEC_AT_OPEN).write_text(
                    spec_file.read_text(encoding="utf-8"), encoding="utf-8")
    except (GitError, ValueError, OSError) as e:
        res.error = getattr(e, "tail", None) or str(e)
        res.config_error = isinstance(e, ProjectConfigError)
    return res


def _do_open_merge(ctx: _Ctx, state: dict, res: StepResult) -> StepResult:
    """SPEC-0233: a merge request opens its worktree and nothing else. A created worktree gets
    the project's setup commands; an adopted one is the requester's and stays as it is."""
    repo = state["repo"]
    merge = state.get("merge") or {}
    try:
        pcfg = ctx.project(state)
        root, worktree, branch = Path(repo["root"]), Path(repo["worktree"]), repo["branch"]
        base = repo.get("base") or pcfg.get("base") or "main"
        base_sha = git.rev_parse(root, base)
        if merge.get("own_worktree"):
            git.worktree_add_existing(root, worktree, branch)
            for cmd in (pcfg.get("worktrees") or {}).get("setup") or []:
                r = subprocess.run(cmd, shell=True, cwd=str(worktree), capture_output=True,
                                   text=True)
                if r.returncode != 0:
                    tail = "\n".join(((r.stdout or "") + (r.stderr or "")).splitlines()[-20:])
                    raise GitError([cmd], r.returncode, tail)
        else:
            if not worktree.is_dir():
                raise ValueError(f"worktree {worktree} no longer exists")
            if git.current_branch(worktree) != branch:
                raise ValueError(f"worktree {worktree} is not on branch {branch}")
            if not git.is_clean(worktree, untracked=True):
                raise ValueError(f"worktree {worktree} has uncommitted or untracked changes")
        repo["base_sha_at_open"] = base_sha
        repo["head"] = git.rev_parse(worktree)
    except (GitError, ValueError, OSError) as e:
        res.error = getattr(e, "tail", None) or str(e)
        res.config_error = isinstance(e, ProjectConfigError)
    return res


class _RunStarted(OSError):
    """`_prepare_run` failed after `run.start` was written; carries what a matching
    `run.end` needs."""

    def __init__(self, cause, run_id, budget):
        super().__init__(str(cause))
        self.run_id, self.budget = run_id, budget


def _previous_runs(cfg, state: dict, persona: str) -> list:
    """The `run.end` events shown to `persona` as previous verdicts (tail 2). A reviewer never
    sees the runs of its own review pass-through (run number > `last_review.entry_run`)."""
    entry = (state.get("last_review") or {}).get("entry_run")
    if persona not in PS.REVIEWER_PERSONAS or entry is None:
        return store.read_ledger(cfg, state["id"], types=["run.end"], tail=2)
    ends = store.read_ledger(cfg, state["id"], types=["run.end"])
    ends = [e for e in ends if int(str(e.get("run_id") or "0-").split("-")[0] or 0) <= entry]
    return ends[-2:]


def _triage_input(state: dict, profile: dict) -> str:
    """The `triage_input` prompt value of the review triager (SPEC-0206): the score, the
    reviewers' verdict and brief, and the waivable (surviving `minor`/`nit`) findings."""
    valid = _valid_verdicts(state, profile)
    tri = triage_reviews(valid, profile)
    keys = ("id", "personas", "severity", "title", "detail", "files")
    return json.dumps({
        "score": tri["score"], "pass_score": review_params(profile)[0],
        "reviewer_verdicts": {p: {"verdict": v.get("verdict"), "fix_brief": v.get("fix_brief")}
                              for p, v in valid.items()},
        "waivable": [{k: f[k] for k in keys} for f in tri["blocking"] + tri["advisory"]
                     if f["severity"] in ("minor", "nit")],
    }, indent=2, sort_keys=True)


def _prepare_run(ctx: _Ctx, state: dict, persona: str):
    """Main thread: mint the run id, build the request, append `run.start`, bump `runs` and
    write the state. Returns (run_id, request, budget). Everything that can raise happens
    before the `run.start` append; a failure after it raises `_RunStarted`."""
    cfg = ctx.cfg
    cid = state["id"]
    profile = ctx.profile(state)
    pcfg = ctx.project(state)
    counters = _counters(state)
    run_no = counters["runs"] + 1
    run_id = f"{run_no:03d}-{persona}"
    run_dir = store.runs_dir(cfg, cid) / run_id
    if persona == "review_triager":     # its input carries the verdicts already (SPEC-0206)
        previous, extra = [], {"triage_input": _triage_input(state, profile)}
    elif persona == "spec_writer":
        previous, extra = [], _prepare_spec_write(ctx, state)
    else:
        previous, extra = _previous_runs(cfg, state, persona), None
    req = PS.build_request(cfg, profile, pcfg, state, persona, run_dir=run_dir, previous=previous,
                           extra=extra)
    budget = float(req.max_budget_usd)
    with store.item_lock(cfg, cid):
        store.append_event(cfg, cid, "run.start", {
            "persona": persona, "run_id": run_id, "model": req.model, "effort": req.effort,
            "max_budget_usd": budget, "timeout_s": req.timeout_s,
            "permission_mode": req.permission_mode, "prompt_sha256": PS.prompt_sha256(req.prompt),
        })
        counters["runs"] = run_no
        try:
            store.write_state(cfg, cid, state)
        except (ValueError, OSError) as e:
            raise _RunStarted(e, run_id, budget) from e
    return run_id, req, budget


def _prepare_spec_write(ctx: _Ctx, state: dict) -> dict:
    """Main thread, before the writer's `run.start` (SPEC-0215): resolve the parent epic, fix the
    spec number and path, record `state.spec_write` and return the prompt values. Raises
    ValueError (the tick parks `spec_write_failed`) when nothing may be run."""
    worktree = Path((state.get("repo") or {}).get("worktree") or ".")
    idea = state.get("idea") or {}
    epic = (idea.get("epic") or "").strip() or None
    epic_text = SW.epic_context(worktree, epic) if epic else None
    spec = state.get("spec") or {}
    counters = _counters(state)
    try:
        if SW.tracked_dirty(worktree):
            raise ValueError("the worktree has uncommitted changes to tracked files; clean the "
                             "worktree, then resume")
        if spec.get("id"):
            mode, number, path = "revise", int(str(spec["id"])[5:]), spec["rel_path"]
        else:
            from .. import spec_numbers as SN
            rec = SN.allocate(ctx.cfg, by="queue", campaign=state["id"],
                              specs_dir=worktree / "docs" / "specs")
            mode, number = "write", rec["number"]
            path = f"docs/specs/{rec['number']:04d}-{SP.slugify(idea.get('heading') or '')}.md"
        sw = SW.snapshot(worktree, mode=mode, number=number, path=path)
    except GitError as e:
        raise ValueError(e.tail or str(e)) from e
    prior = state.get("spec_write") or {}
    if counters.get("spec_write_retries", 0) and prior.get("untracked_before") is not None:
        sw["untracked_before"] = prior["untracked_before"]      # a failed attempt's leftovers
    if path in sw["untracked_before"]:      # the spec file is never setup output: a leftover of
        sw["untracked_before"].remove(path)                     # an earlier attempt is removed,
        try:                                                    # so it can neither hide the new
            (worktree / path).unlink()                          # write nor survive a rollback
        except OSError as e:
            raise ValueError(f"cannot remove the leftover {path}: {e}") from e
    state["spec_write"] = sw                                    # never join the baseline
    brief = state.get("pending_brief")
    spec_id = SW.spec_id_of(number)
    return {
        "spec_id": spec_id, "spec_rel_path": path,
        "idea_text": json.dumps(idea, indent=2, ensure_ascii=False),
        "template_text": _read_text(worktree / "docs" / "specs" / "TEMPLATE.md"),
        "schema_text": _read_text(worktree / "docs" / "specs" / "SCHEMA.md"),
        "write_problems": SW.write_problems_text(brief),
        "write_rules": SW.write_rules(mode, sw, idea, brief),
        "epic_text": epic_text,
    }


def _read_text(path) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError:
        return None


def _do_write_spec(ctx: _Ctx, state: dict, step: Step, run_id: str, req,
                   budget: float) -> StepResult:
    """Worker thread (SPEC-0215): run the spec writer, validate what it left, regenerate the
    project's generated files, then commit — or roll the worktree back to where it was."""
    res = StepResult(state["id"], step, state["state"], run_id=run_id, budget=budget)
    worktree = (state.get("repo") or {}).get("worktree")
    sw = dict(state["spec_write"])
    res.write = {"problems": [], "blocked": None, "committed": False, "sha": None,
                 "title": None, "mode": sw["mode"], "rollback_failed": False}
    w = res.write
    branch = (state.get("repo") or {}).get("branch")

    def on_branch():
        """A reset must only ever hit the campaign branch: the writer runs with full permissions
        and may have switched or detached HEAD. Otherwise nothing is reset and a human fixes it."""
        if branch and git.current_branch(worktree) != branch:
            w["rollback_failed"] = True
            raise ValueError(f"HEAD is no longer on {branch}; check the worktree out on it, "
                             "clean it, then resume")

    def roll_back():
        on_branch()
        try:
            SW.rollback(worktree, sw)
        except GitError:
            w["rollback_failed"] = True
            raise

    try:
        res.run_result = rr = ctx.runner.run(req)
        res.interrupted = ctx.stop.is_set()
        verdict = rr.verdict if isinstance(rr.verdict, dict) else {}
        if res.interrupted or rr.interrupted:
            roll_back()
        elif rr.verdict_ok and verdict.get("status") == "blocked":
            w["blocked"] = [str(b) for b in verdict.get("blockers") or []] \
                or [str(verdict.get("summary") or "no reason given")]
            roll_back()
        else:
            on_branch()
            problems = SW.verdict_problems(rr, sw)
            if not problems:
                problems = SW.validate_spec_change(worktree, sw, state.get("idea"))
            generated = []
            if not problems:
                pcfg = ctx.project(state)
                regen = (pcfg.get("merge") or {}).get("regenerate") or []
                problems, generated = SW.regenerate(worktree, regen, sw)
            if problems:
                w["problems"] = problems
                roll_back()
            else:
                fm, _ = split_frontmatter((Path(worktree) / sw["path"]).read_text(encoding="utf-8"))
                w["title"] = str(fm.get("title") or verdict.get("title") or "")
                msg = (f"wt({state['id']}): spec {sw['mode']} — "
                       f"{SW.spec_id_of(sw['number'])} {w['title']}")
                trailer = ((ctx.project(state).get("agents") or {}).get("commit_trailer")
                           or "").strip()
                if trailer:
                    msg += f"\n\n{trailer}"
                w["sha"] = SW.commit_staged(worktree, [sw["path"], *generated], msg)
                w["committed"] = True
                w["head"] = git.rev_parse(worktree)
    except Exception as e:                      # nothing may leave the worktree unrolled
        res.error = getattr(e, "tail", None) or str(e)
        if not isinstance(e, (GitError, ValueError, OSError)):
            res.error = repr(e)
            res.crashed = repr(e)               # still a report.errors entry
        if not w["rollback_failed"] and not res.interrupted:
            try:
                roll_back()
            except (GitError, ValueError):
                pass                            # roll_back marked rollback_failed
        if w["rollback_failed"]:
            res.error = "rollback failed; clean the worktree, then resume"
    return res


def _do_persona(ctx: _Ctx, state: dict, step: Step, run_id: str, req, budget: float) -> StepResult:
    """Worker thread: run the persona; for the implementer, commit afterwards (the triager and
    the reviewer are read-only and commit nothing)."""
    res = StepResult(state["id"], step, state["state"], run_id=run_id, budget=budget)
    try:
        res.run_result = ctx.runner.run(req)
        res.interrupted = ctx.stop.is_set()
        if step.kind == "implement" and not res.interrupted:
            pcfg = ctx.project(state)
            summary = ""
            if isinstance(res.run_result.verdict, dict):
                summary = str(res.run_result.verdict.get("summary") or "").strip().splitlines()
                summary = summary[0] if summary else ""
            msg = f"wt({state['id']}): implementer run {run_id[:3]}"
            if summary:
                msg += f" — {summary[:72]}"
            trailer = ((pcfg.get("agents") or {}).get("commit_trailer") or "").strip()
            if trailer:
                msg += f"\n\n{trailer}"
            res.commit_message = msg
            res.commit_sha = git.commit_all((state.get("repo") or {}).get("worktree"), msg)
    except (GitError, ValueError, OSError) as e:
        res.error = getattr(e, "tail", None) or str(e)
    return res


def _do_resolve(ctx: _Ctx, state: dict, step: Step, run_id: str, req, budget: float) -> StepResult:
    """Worker thread (SPEC-0221): restore the campaign branch to the recorded `pre_head`, run
    the resolver persona, then verify its work with runner-side git reads (the verdict is not
    trusted). Anything not verified, interrupted or failed is restored to `pre_head`, so a
    discarded attempt leaves the branch as it was and writes nothing outside the worktree."""
    res = StepResult(state["id"], step, state["state"], run_id=run_id, budget=budget)
    repo = state.get("repo") or {}
    worktree, branch = repo.get("worktree"), repo.get("branch")
    rs = (state.get("closing") or {}).get("resolve") or {}
    pre_head = rs.get("pre_head")
    try:
        git.restore_branch(worktree, branch, pre_head)
        crash = None
        try:
            res.run_result = rr = ctx.runner.run(req)
        except Exception as e:          # a crashing resolver is a failed attempt, not a stuck close
            crash = f"the run raised: {e}"
            res.run_result = rr = _synthetic_failure(crash)
        res.interrupted = ctx.stop.is_set()
        verdict = rr.verdict if isinstance(rr.verdict, dict) else {}
        failed = None
        if res.interrupted or rr.interrupted:
            failed = "interrupted"
        elif not rr.verdict_ok or rr.is_error or rr.exit_code != 0:
            failed = crash or "the run failed or returned no valid verdict"
        elif verdict.get("status") != "resolved":
            failed = f"the resolver reported {verdict.get('status')}"
        else:
            try:
                failed = git.verify_resolution(worktree, branch, rs["target_sha"], rs["fork"],
                                               pre_head)
            except (GitError, KeyError) as e:
                failed = f"verification error: {getattr(e, 'tail', None) or e}"
        res.resolution = {"verified": failed is None, "failed": failed, "head": None}
        if failed is None:
            git.clean_untracked(worktree)   # residue must not reach the close-time snapshot
            res.resolution["head"] = git.rev_parse(worktree)
        else:
            git.restore_branch(worktree, branch, pre_head)
    except (GitError, ValueError, OSError) as e:
        res.error = getattr(e, "tail", None) or str(e)
        res.resolution = None
        if pre_head and worktree and branch:    # a verified result that errored after is undone
            try:
                git.restore_branch(worktree, branch, pre_head)
            except (GitError, ValueError, OSError):
                pass                            # the park detail already names the first error
    return res


def _do_gate(ctx: _Ctx, state: dict) -> StepResult:
    res = StepResult(state["id"], Step("gate"), "GATING")
    try:
        pcfg = ctx.project(state)
        res.gate_run = G.run_all(pcfg, state, ctx.cfg, seq=G.next_gate_seq(ctx.cfg, state["id"]))
    except (ValueError, OSError) as e:
        res.error = str(e)
    return res


def _do_prepare_close(ctx: _Ctx, state: dict) -> StepResult:
    """The close's worktree phase (SPEC-0220): worker-safe, no main-checkout writes."""
    res = StepResult(state["id"], Step("close"), "CLOSING")
    # SPEC-0207: file the follow-up ideas first; the filer never raises, and its events and
    # summary reach `_apply` whether or not the close then succeeds.
    if state.get("kind") != "merge":            # SPEC-0233: a merge request has no review
        res.events_before, res.followups = FU.file_followups(ctx.cfg, state, ctx.profile(state))
    try:
        res.prepared = CL.prepare_close(ctx.cfg, state, ctx.project(state))
    except (GitError, ValueError, OSError) as e:
        res.error = getattr(e, "tail", None) or str(e)
    except Exception as e:                      # nothing may drop the filer's events
        res.error = repr(e)
        res.crashed = repr(e)                   # SPEC-0220: still a report.errors entry
    return res


# ---- applying results (main thread) ---------------------------------------------------------

def _fresh(cfg, state: dict) -> dict | None:
    """Re-read the campaign from disk; None when a human moved it meanwhile (parked, abandoned
    or resumed) — then the step's events are still recorded but the transition is skipped."""
    on_disk = store.read_state(cfg, state["id"])
    if on_disk.get("state") != state.get("state"):
        return None
    return on_disk


def _apply(ctx: _Ctx, state: dict, res: StepResult) -> dict:
    """Apply one StepResult: events, counters, cost, then the transition. Returns the report
    row for `wt.queue.tick.v1`."""
    cfg = ctx.cfg
    cid = state["id"]
    if res.step.kind == "review" and not res.reviews and not res.error:
        # one reviewer's bare result (SPEC-0201): the aggregate path is the only code path
        res = StepResult(res.campaign, res.step, res.from_state, reviews=[res])
    row = {"campaign": cid, "from": res.from_state, "to": None, "action": res.step.kind,
           "run_id": res.run_id, "cost_usd": None, "reason": None}
    events: list = list(res.events_before)
    profile = ctx.profile(state)
    counters = _counters(state)

    def finish(to, reason, *, park=None):
        current = _fresh(cfg, state)
        if current is None:
            # A human changed the state while the step ran: keep the facts, not the move.
            with store.item_lock(cfg, cid):
                for etype, payload in events:
                    store.append_event(cfg, cid, etype, payload)
                store.append_event(cfg, cid, "note", {
                    "text": f"step {res.step.kind} finished after a human changed the state; "
                            f"transition to {to} skipped", "author": "queue"})
            row["to"], row["reason"] = store.read_state(cfg, cid).get("state"), "superseded_by_human"
            return row
        for k in ("counters", "cost", "repo", "spec", "last_gate", "last_verdict", "last_review",
                  "pending_brief", "close", "review_carry", "closing", "spec_write", "spec_id"):
            if k in state:
                current[k] = state[k]
        requested = current.get("park_requested") or state.get("park_requested")
        if requested and park is None and to not in TERMINAL_STATES:
            req = requested
            _park(cfg, current, "human", req.get("detail") or req.get("reason") or "parked",
                  _resume_target(to), by="human", extra_events=events)
            row["to"], row["reason"] = "PARKED", "human"
            return row
        if park:
            _park(cfg, current, park["reason"], park["detail"], park["resume_to"],
                  extra_events=events)
            row["to"], row["reason"] = "PARKED", park["reason"]
            return row
        _transition(cfg, current, to, reason, extra_events=events)
        row["to"], row["reason"] = to, reason
        return row

    kind = res.step.kind
    if res.error and res.config_error:
        resume = "QUEUED" if kind == "open" and res.from_state == "OPENING" else res.from_state
        return finish("PARKED", "config_invalid", park={
            "reason": "config_invalid", "detail": f"project config: {res.error}",
            "resume_to": resume if resume in RESUMABLE_STATES else "IMPLEMENTING"})
    if res.error and kind != "close":
        reason = {"open": "open_failed", "write_spec": "spec_write_failed"}.get(kind, "open_failed")
        detail = f"{kind} step failed: {res.error}"
        if kind == "write_spec" and (res.write or {}).get("rollback_failed"):
            detail = res.error
        if kind == "write_spec" and res.run_result is not None:     # the run was paid for
            events += _record_run(state, res.step.persona, res.run_id, res.run_result,
                                  res.budget, interrupted=res.interrupted)
        resume = {"open": "QUEUED", "implement": "IMPLEMENTING", "gate": "GATING",
                  "triage": "GATING", "review": "REVIEWING",
                  "resolve": "CLOSING", "write_spec": "SPEC_WRITING"}.get(kind, "IMPLEMENTING")
        if kind == "resolve" and res.run_id:    # run.start is on the ledger: close it, run or no run
            rr = res.run_result or replace(_synthetic_failure(str(res.error)), cost_usd=0.0)
            events += _record_run(state, res.step.persona, res.run_id, rr, res.budget,
                                  interrupted=res.interrupted)
        return finish("PARKED", reason, park={"reason": reason, "detail": detail,
                                               "resume_to": resume})

    if kind == "open":
        if state.get("kind") == "merge":
            return finish("CLOSING", "opened")
        return finish("SPEC_WRITING" if has_no_spec(state) else "IMPLEMENTING", "opened")

    if kind == "review":
        return _apply_reviews(ctx, state, profile, counters, res, row, events, finish)

    if kind == "write_spec":
        return _apply_spec_write(ctx, state, counters, res, row, events, finish)

    if kind in ("implement", "triage", "resolve"):
        persona = res.step.persona
        rr = res.run_result
        events += _record_run(state, persona, res.run_id, rr, res.budget,
                              interrupted=res.interrupted)
        row["cost_usd"] = rr.cost_usd if rr.cost_usd is not None else res.budget
        row["cost_estimated"] = rr.cost_usd is None
        state["last_verdict"] = {"persona": persona, "run": res.run_id, "verdict": rr.verdict,
                                 "verdict_ok": rr.verdict_ok}
        if res.interrupted or rr.interrupted:
            # State unchanged: the next tick spawns a fresh agent (run.end carries interrupted).
            with store.item_lock(cfg, cid):
                for etype, payload in events:
                    store.append_event(cfg, cid, etype, payload)
                store.write_state(cfg, cid, state)
            row["to"], row["reason"] = res.from_state, "interrupted"
            return row
        if kind == "resolve":
            return _apply_resolve(state, counters, res, rr, row, events, finish)
        if kind == "implement":
            if res.commit_sha is not None or res.commit_message:
                events.append(("commit", {"sha": res.commit_sha, "message": res.commit_message}))
                if res.commit_sha:
                    state.setdefault("repo", {})["head"] = res.commit_sha
            state["pending_brief"] = None
            status = (rr.verdict or {}).get("status") if isinstance(rr.verdict, dict) else None
            if status == "blocked":
                counters["blocked_streak"] += 1
                if counters["blocked_streak"] >= BLOCKED_STREAK_MAX:
                    blockers = "; ".join(str(b) for b in (rr.verdict.get("blockers") or [])) \
                        or (rr.verdict.get("summary") or "")
                    return finish("PARKED", "implementer_blocked", park={
                        "reason": "implementer_blocked",
                        "detail": f"blocked {counters['blocked_streak']}× in a row: {blockers}",
                        "resume_to": "IMPLEMENTING"})
            else:
                counters["blocked_streak"] = 0
            return finish("GATING", "implemented")
        # triage
        decision = decide_triage(state, profile, rr.verdict, rr.verdict_ok)
        if decision.kind == "none":                      # malformed verdict, retry allowed
            counters["triage_retries"] += 1
            return finish("TRIAGING", "triage_retry")
        if decision.kind == "park":
            return finish("PARKED", decision.reason, park={
                "reason": decision.reason, "detail": decision.detail,
                "resume_to": decision.resume_to})
        if decision.kind == "gate":
            counters["gate_retries"] += 1
            return finish("GATING", "retry_gate")
        if decision.kind == "close":
            return finish("CLOSING", "waived")
        # fix
        counters["loops"] += 1
        counters["gate_retries"] = 0
        counters["triage_retries"] = 0
        v = rr.verdict or {}
        notes = ((state.get("pending_brief") or {}).get("human_notes") or [])
        state["pending_brief"] = {"source": "gate_triager", "fix_brief": v.get("fix_brief"),
                                  "must_fix": list(v.get("must_fix") or []),
                                  "human_notes": list(notes)}
        return finish("IMPLEMENTING", "triage_fix")

    if kind == "gate":
        run = res.gate_run
        counters["gate_runs"] += 1
        payload = G.as_payload(run)
        events.append(("gate", payload))
        state["last_gate"] = payload
        if run.ok:
            return finish("REVIEWING", "gates_green")
        return finish("TRIAGING", "gates_failed")

    if kind == "close":
        if ctx.stop.is_set() and res.prepared is not None:
            # SPEC-0220: a result that lands during a stop is discarded — a killed re-gate
            # must not send the campaign to TRIAGING; the next run redoes the close
            row["to"], row["reason"] = "CLOSING", "interrupted"
            return row
        counters["close_attempts"] += 1
        if res.followups is not None:
            fu = res.followups                      # counts and ids, plus the detail lists
            row["followups"] = {"filed": len(fu["filed"]), "unfiled": len(fu["unfiled"]),
                                "capped": len(fu.get("capped") or []),
                                "idea_id": fu.get("idea_id"),
                                "ids": list(dict.fromkeys(f["idea_id"] for f in fu["filed"])),
                                "items": list(fu["filed"]), "unfiled_items": list(fu["unfiled"]),
                                "waived": list(fu["waived"])}
        if res.error:
            return finish("PARKED", "open_failed", park={
                "reason": "open_failed", "detail": f"close step failed: {res.error}",
                "resume_to": "CLOSING"})
        oc = res.close_outcome
        if oc is None and res.prepared.outcome is not None:
            oc = res.prepared.outcome
        absorbed = list(oc.events) if oc is not None else list(res.prepared.events)
        if oc is None:
            # finish phase (main thread): a park / abandon / resume made during the worktree
            # phase wins over a merge that has not happened yet
            current = _fresh(cfg, state)
            if current is None or current.get("park_requested") or state.get("park_requested"):
                _absorb_close_events(state, counters, events, absorbed)
                return finish("CLOSING", "close_superseded")
            attempts = int((state.get("closing") or {}).get("attempts") or 0) + 1
            state["closing"] = {**(state.get("closing") or {}), "attempts": attempts,
                                "target_sha": res.prepared.target_sha}
            try:
                with _busy(ctx.lease, "close", campaign=cid,
                           budget_s=events_settings(cfg)["gate_budget_s"]):
                    oc = CL.finish_close(cfg, state, ctx.project(state), res.prepared,
                                         attempts=attempts)
            except (GitError, ValueError, OSError) as e:
                _absorb_close_events(state, counters, events, absorbed)
                return finish("PARKED", "open_failed", park={
                    "reason": "open_failed", "resume_to": "CLOSING",
                    "detail": f"close step failed: {getattr(e, 'tail', None) or e}"})
            absorbed += list(oc.events)
        _absorb_close_events(state, counters, events, absorbed)
        if oc.retry:
            events.append(("finding", {
                "persona": "close", "severity": "info", "gate": "base",
                "title": "base moved during re-gate; redoing close", "detail": "", "files": []}))
            return finish("CLOSING", "base_moved")
        if oc.next_state in ("MERGED", "HANDED_OFF"):
            state["close"] = dict(oc.close)
            if res.followups is not None:           # after the assignment replaced the dict
                state["close"]["followups"] = res.followups
            state["park"] = None
            return finish(oc.next_state, "closed")
        if oc.next_state == "TRIAGING" and state.get("kind") == "merge":
            # SPEC-0233: no gate triager for a merge request; the conductor relays the failure
            return finish("PARKED", "gates_failed", park={
                "reason": "gates_failed", "detail": oc.gate_detail or "gates failed",
                "resume_to": "CLOSING"})
        if oc.next_state == "TRIAGING":
            if oc.synthetic_gate is not None:
                state["last_gate"] = {"gate_seq": None, "ok": False, "started_at": store.now_iso(),
                                      "duration_s": 0.0, "results": [oc.synthetic_gate]}
                return finish("TRIAGING", "spec_dod")
            return finish("TRIAGING", "regate_failed")
        park = oc.park or {"reason": "open_failed", "detail": "close returned PARKED without a reason",
                           "resume_to": "CLOSING"}
        if park["reason"] == "rebase_conflict" and park.get("conflicts"):
            if _dispatch_resolver(ctx, state, res, park, events):
                return finish("CLOSING", "resolve_dispatched")
            park = _spent_resolver_park(state, park)
        return finish("PARKED", park["reason"], park=park)

    return finish(res.from_state, "noop")


def _dispatch_resolver(ctx: _Ctx, state: dict, res: StepResult, park: dict, events: list) -> bool:
    """SPEC-0221: a close rebase conflicted on non-generated paths. With budget left (and the
    project not opted out) record `closing.resolve` and a finding, and stay CLOSING; the next
    plan is the `resolve` step. False means park as before."""
    closing = dict(state.get("closing") or {"attempts": 0})
    if int(closing.get("resolve_attempts") or 0) >= MAX_RESOLVE_ATTEMPTS:
        return False
    if (ctx.project(state).get("merge") or {}).get("resolver", True) is False:
        return False
    repo = state.get("repo") or {}
    pre_head = getattr(res.prepared, "pre_head", "") or ""
    target_sha = park.get("target_sha") or ""
    if not (pre_head and target_sha):
        return False
    try:
        fork = git.merge_base(repo["worktree"], pre_head, target_sha)
    except (GitError, KeyError):
        return False
    closing["resolve"] = {"target": park.get("target"), "target_sha": target_sha,
                          "conflicts": list(park["conflicts"]), "pre_head": pre_head,
                          "fork": fork, "branch": repo.get("branch")}
    state["closing"] = closing
    events.append(("finding", {
        "persona": "close", "severity": "info", "gate": "rebase",
        "title": "rebase conflict; resolver dispatched",
        "detail": park["detail"], "files": list(park["conflicts"])}))
    return True


def _spent_resolver_park(state: dict, park: dict) -> dict:
    """The park detail after resolver runs were used up. Failed attempts are reported with the
    last failure; runs that were all verified (the base moved and conflicted again each time)
    are reported as such, so the conductor does not hunt for a failure that never happened."""
    closing = state.get("closing") or {}
    used = int(closing.get("resolve_attempts") or 0)
    if not used:
        return park
    failed = closing.get("resolve_failed") or {}
    if failed.get("n") and closing.get("resolve_last_verified"):
        tail = (f" (resolver: {used} attempts; the last was verified but the base moved and "
                f"conflicted again; an earlier attempt failed: {failed.get('last')})")
    elif failed.get("n"):
        tail = f" (resolver: {used} attempts, last: {failed.get('last')})"
    else:
        tail = f" (resolver: {used} resolutions verified; the base moved and conflicted again)"
    return {**park, "detail": park["detail"] + tail}


def _apply_resolve(state: dict, counters: dict, res: StepResult, rr, row: dict, events: list,
                   finish):
    """SPEC-0221 harvest of a `resolve` step (not interrupted). Verified: clear
    `closing.resolve`, record `rebase_resolved` and stay CLOSING for the normal close.
    Otherwise a `major` finding; stay CLOSING with the same `resolve` while attempts remain,
    else park `rebase_conflict`."""
    closing = dict(state.get("closing") or {"attempts": 0})
    rs = closing.get("resolve") or {}
    info = res.resolution or {}
    verdict = rr.verdict if isinstance(rr.verdict, dict) else {}
    closing["resolve_attempts"] = used = int(closing.get("resolve_attempts") or 0) + 1
    closing["resolve_last_verified"] = bool(info.get("verified"))
    state["closing"] = closing
    if info.get("verified"):
        closing.pop("resolve", None)
        head = info.get("head")
        events.append(("rebase_resolved", {
            "run_id": res.run_id, "target_sha": rs.get("target_sha"),
            "conflicts": list(rs.get("conflicts") or []),
            "resolutions": list(verdict.get("resolutions") or []),
            "summary": verdict.get("summary"), "head": head}))
        state.setdefault("repo", {})["head"] = head
        return finish("CLOSING", "rebase_resolved")
    why = str(verdict.get("summary") or "").strip() or str(info.get("failed") or "not resolved")
    if verdict.get("summary") and info.get("failed"):
        why += f" [{info['failed']}]"
    questions = [str(q) for q in verdict.get("questions") or []]
    closing["resolve_failed"] = {"n": int((closing.get("resolve_failed") or {}).get("n") or 0) + 1,
                                 "last": why}
    events.append(("finding", {
        "persona": "conflict_resolver", "severity": "major", "gate": "rebase",
        "title": "conflict resolver did not resolve",
        "detail": why + ("\nQuestions:\n" + "\n".join(f"- {q}" for q in questions)
                        if questions else ""),
        "files": list(rs.get("conflicts") or [])}))
    if used < MAX_RESOLVE_ATTEMPTS:
        return finish("CLOSING", "resolve_failed")
    paths = ", ".join(rs.get("conflicts") or [])
    return finish("PARKED", "rebase_conflict", park={
        "reason": "rebase_conflict", "resume_to": "CLOSING",
        "detail": f"rebase onto {rs.get('target')} ({str(rs.get('target_sha'))[:10]}) conflicts: "
                  f"{paths} (resolver: {used} attempts, last: {why})"})


def _apply_spec_write(ctx, state, counters, res, row, events, finish):
    """A `write_spec` result (SPEC-0215): problems retry once then park, `blocked` parks, a
    committed spec is recorded on the campaign and the idea, then the campaign parks until the
    spec review (SPEC-0214) exists."""
    cfg, cid = ctx.cfg, state["id"]
    rr, w, persona = res.run_result, res.write, res.step.persona
    events += _record_run(state, persona, res.run_id, rr, res.budget, interrupted=res.interrupted)
    row["cost_usd"] = rr.cost_usd if rr.cost_usd is not None else res.budget
    row["cost_estimated"] = rr.cost_usd is None
    state["last_verdict"] = {"persona": persona, "run": res.run_id, "verdict": rr.verdict,
                             "verdict_ok": rr.verdict_ok}
    if res.interrupted or rr.interrupted:
        # rolled back by the worker; no retry counted, the next tick runs a fresh writer
        with store.item_lock(cfg, cid):
            for etype, payload in events:
                store.append_event(cfg, cid, etype, payload)
            store.write_state(cfg, cid, state)
        row["to"], row["reason"] = res.from_state, "interrupted"
        return row
    park_back = {"resume_to": "SPEC_WRITING"}
    if w["blocked"]:
        return finish("PARKED", "spec_write_failed", park={
            "reason": "spec_write_failed", "detail": "blocked: " + "; ".join(w["blocked"]),
            **park_back})
    if w["problems"]:
        for problem in w["problems"]:
            events.append(("finding", {"persona": persona, "severity": "major",
                                       "gate": "spec-write", "title": problem[:120],
                                       "detail": problem, "files": []}))
        brief = state.get("pending_brief") or {"source": "spec_write", "fix_brief": None,
                                               "must_fix": [], "human_notes": []}
        brief["write_problems"] = list(w["problems"])
        state["pending_brief"] = brief
        counters["spec_write_retries"] = counters.get("spec_write_retries", 0) + 1
        if counters["spec_write_retries"] <= SPEC_WRITE_RETRY_MAX:
            return finish("SPEC_WRITING", "spec_write_retry")
        return finish("PARKED", "spec_write_failed", park={
            "reason": "spec_write_failed", "detail": "; ".join(w["problems"]), **park_back})
    sw = state["spec_write"]
    spec_id = SW.spec_id_of(sw["number"])
    record = {"id": spec_id, "rel_path": sw["path"], "internal": True}
    if w["sha"]:
        events.append(("commit", {"sha": w["sha"], "message": f"spec {sw['mode']} — {spec_id}"}))
    state.setdefault("repo", {})["head"] = w.get("head") or w["sha"]
    with store.item_lock(cfg, cid):
        on_disk = store.read_state(cfg, cid)
        on_disk["spec"] = {**(on_disk.get("spec") or {}), **record}
        on_disk["spec_id"] = spec_id
        store.write_state(cfg, cid, on_disk)
        if sw["mode"] == "write" and on_disk.get("state") not in TERMINAL_STATES:
            _link_idea(cfg, state, spec_id)
    state["spec"] = {**(state.get("spec") or {}), **record}
    state["spec_id"] = spec_id
    counters["spec_write_retries"] = 0
    state["pending_brief"] = None
    events.append(("spec_written", {"spec_id": spec_id, "rel_path": sw["path"],
                                    "title": w["title"], "mode": sw["mode"]}))
    return finish("PARKED", "spec_written", park={
        "reason": "spec_review_unavailable", "resume_to": "SPEC_WRITING",
        "detail": f"{spec_id} {sw['mode']}; no spec review yet (SPEC-0214)"})


def _link_idea(cfg, state: dict, spec_id: str) -> None:
    """Give the campaign's idea `:SPEC:` and SPECCED; both are idempotent."""
    from .. import org_write as OW
    task = _idea_task(cfg, _idea_id_of(state))
    if task is None:
        return
    if (task.properties.get("SPEC") or "").strip() != spec_id:
        OW.set_property(cfg, task, "SPEC", spec_id)
        task = _idea_task(cfg, _idea_id_of(state))
    if task is not None and task.state != "SPECCED":
        OW.set_state(cfg, task, "SPECCED")


def _absorb_close_events(state: dict, counters: dict, events: list, close_events) -> None:
    """Fold a close's ledger events into the apply: gate runs counted, head tracked."""
    for etype, payload in close_events:
        if etype == "park":
            continue                                   # re-emitted by _park
        events.append((etype, payload))
        if etype == "gate":
            counters["gate_runs"] += 1
            state["last_gate"] = payload
        if etype == "commit" and payload.get("sha"):
            state.setdefault("repo", {})["head"] = payload["sha"]


def _synthetic_failure(error: str) -> RunResult:
    """The RunResult recorded for a reviewer whose worker raised after `run.start`."""
    return RunResult(exit_code=-1, timed_out=False, interrupted=False, duration_s=0.0,
                     is_error=True, cost_usd=None, usage={}, session_id=None, verdict=None,
                     verdict_ok=False, result_text=error, raw_path=None)


def _store_verdicts(state: dict, profile: dict, counters: dict, results) -> bool:
    """Merge this pass's valid verdicts into `last_review.verdicts` (overwriting a retried
    reviewer's entry) and refresh `run` / `runs` / `loop`. True when any verdict was stored."""
    lr = state.get("last_review") if isinstance(state.get("last_review"), dict) else {}
    verdicts = dict(lr.get("verdicts") or {})
    stored = False
    for r in results:
        rr = r.run_result
        if rr is None or not rr.verdict_ok or not isinstance(rr.verdict, dict) \
                or r.interrupted or rr.interrupted:
            continue
        v = rr.verdict
        entry = {"verdict": v.get("verdict"), "score": v.get("score"),
                 "fix_brief": v.get("fix_brief"),
                 "findings": [f for f in v.get("findings") or [] if isinstance(f, dict)]}
        if "ac_assessment" in v:
            entry["ac_assessment"] = v["ac_assessment"]
        if "coverage_assessment" in v:
            entry["coverage_assessment"] = v["coverage_assessment"]
        entry.update(run=r.run_id, loop=counters["loops"])
        verdicts[r.step.persona] = entry
        stored = True
    if stored:
        order = [p for p in reviewers_of(profile) if p in verdicts]
        lr.update(verdicts=verdicts, runs={p: verdicts[p]["run"] for p in order},
                  run=verdicts[order[-1]]["run"], loop=counters["loops"])
        state["last_review"] = lr
    return stored


def _accept_waivers(state: dict, profile: dict, items) -> tuple:
    """`(accepted, ignored)` for the triager's `waive` list (SPEC-0206): accepted = waived
    findings `{id, personas, severity, title, detail, files, ac, category, reason}`; ignored =
    `[(id, why)]`. Only a surviving `minor`/`nit` finding with a non-empty reason, once."""
    tri = triage_reviews(_valid_verdicts(state, profile), profile)
    found = {f["id"]: f for f in tri["blocking"] + tri["advisory"]}
    accepted, ignored, seen = [], [], set()
    for item in items:
        fid = item.get("id") if isinstance(item, dict) else None
        reason = item.get("reason") if isinstance(item, dict) else None
        if not isinstance(fid, str) or fid not in found:
            ignored.append((fid, "not a finding of this review"))
        elif fid in tri["synthetic"]:
            ignored.append((fid, "synthetic findings cannot be waived"))
        elif found[fid]["severity"] not in ("minor", "nit"):
            ignored.append((fid, f"{found[fid]['severity']} findings cannot be waived"))
        elif not isinstance(reason, str) or not reason.strip():
            ignored.append((fid, "empty reason"))
        elif fid in seen:
            ignored.append((fid, "duplicate"))
        else:
            seen.add(fid)
            accepted.append(dict(found[fid], reason=reason.strip()))
    return accepted, ignored


def _apply_triager(ctx, state, profile, counters, res, row, events):
    """Record the review triager's run and accept its waivers (SPEC-0206). The result never
    enters `last_review.verdicts` nor the reviewers' retry logic: a failed, malformed or
    timed-out run only adds a note, and `triage` becomes `done` so it runs once per loop.
    Returns the report row when the run was interrupted (state unchanged: `triage` stays
    `pending`), else None — the caller then routes the review with the accepted waivers."""
    cfg, cid = ctx.cfg, state["id"]
    r = res.reviews[0]
    events += r.events_before
    rr = r.run_result
    lr = state["last_review"]
    if rr is not None:
        events += _record_run(state, "review_triager", r.run_id, rr, r.budget,
                              interrupted=r.interrupted)
        row["run_id"] = r.run_id
        row["cost_usd"] = rr.cost_usd if rr.cost_usd is not None else r.budget
        row["cost_estimated"] = rr.cost_usd is None
        if r.interrupted or rr.interrupted:
            with store.item_lock(cfg, cid):
                for etype, payload in events:
                    store.append_event(cfg, cid, etype, payload)
                store.write_state(cfg, cid, state)
            row["to"], row["reason"] = res.from_state, "interrupted"
            return row
    verdict = rr.verdict if rr is not None and rr.verdict_ok else None
    if not isinstance(verdict, dict) or not isinstance(verdict.get("waive"), list):
        why = (f"review triager run not started: {r.error}" if rr is None else
               "review triager timed out" if rr.timed_out else
               "review triager run failed" if rr.is_error else
               "review triager returned a malformed verdict")
        events.append(("note", {"text": f"{why}; the deterministic review outcome stands",
                                "author": "queue"}))
        accepted = []
    else:
        accepted, ignored = _accept_waivers(state, profile, verdict["waive"])
        for fid, why in ignored:
            events.append(("note", {"text": f"triager ignored {fid}: {why}", "author": "queue"}))
        for w in accepted:
            events.append(("note", {"text": f"waived {w['id']}: {w['reason']}",
                                    "author": "queue"}))
    lr.update(triage="done", waived=accepted, triager_run=r.run_id)
    return None


def _carry_source(state: dict, tri: dict) -> dict:
    """The `{blocking, advisory, waived}` of a review, for `_merge_carry`."""
    return {"blocking": tri["blocking"], "advisory": tri["advisory"],
            "waived": (state.get("last_review") or {}).get("waived") or []}


def _merge_carry(state: dict, review: dict) -> None:
    """SPEC-0207: on a `fix` outcome merge the review's `advisory` and `waived` findings into
    `state["review_carry"]` (de-duplicated by key, so re-merging the same review adds nothing)
    and drop every key that is blocking in this review (the implementer is told to fix it).
    `review` = `{blocking, advisory, waived}` of the triage."""
    spec_id = state.get("spec_id")
    blocking = {FU.finding_key(spec_id, f.get("title"), f.get("files"))
                for f in review.get("blocking") or []}
    carry = [e for e in state.get("review_carry") or [] if e["key"] not in blocking]
    for e in FU.considered(spec_id, [], review):
        if e["key"] not in blocking:
            FU.merge_entry(carry, e)
    state["review_carry"] = carry


def _apply_reviews(ctx, state, profile, counters, res, row, events, finish):
    """Route the reviewers' results (SPEC-0187, SPEC-0201): record every run, store the valid
    verdicts in `last_review`, then follow `decide_reviews`. `res.reviews` is in profile
    order; `events` is `_apply`'s list, `finish` its closure. The review triager's result
    (SPEC-0206) is handled by `_apply_triager` and then routed the same way."""
    cfg, cid = ctx.cfg, state["id"]
    reviewers = reviewers_of(profile)
    if res.step.persona == "review_triager":
        out = _apply_triager(ctx, state, profile, counters, res, row, events)
        return out if out is not None else _route_reviews(
            ctx, state, profile, counters, row, events, finish)
    multi = len(reviewers) > 1
    charged, last = 0.0, None
    for r in res.reviews:
        events += r.events_before
        rr = r.run_result
        if rr is None:                      # prepared nothing: no run.start, no cost
            events.append(("note", {"text": f"{r.step.persona}: review run not started: "
                                            f"{r.error}", "author": "queue"}))
            continue
        events += _record_run(state, r.step.persona, r.run_id, rr, r.budget,
                              interrupted=r.interrupted)
        charged += rr.cost_usd if rr.cost_usd is not None else r.budget
        row["cost_estimated"] = bool(row.get("cost_estimated")) or rr.cost_usd is None
        last = r
    if last is not None:
        row["run_id"], row["cost_usd"] = last.run_id, charged
        state["last_verdict"] = {"persona": last.step.persona, "run": last.run_id,
                                 "verdict": last.run_result.verdict,
                                 "verdict_ok": last.run_result.verdict_ok}
    bad = next((r for r in res.reviews if r.config_error), None)
    interrupted = any(r.interrupted or (r.run_result and r.run_result.interrupted)
                      for r in res.reviews) or (multi and ctx.stop.is_set())
    if interrupted:
        # No decision, no retry counted: the next tick re-spawns the reviewers still lacking a
        # valid verdict (one reviewer: state unchanged, as SPEC-0187).
        if multi:
            _store_verdicts(state, profile, counters, res.reviews)
        with store.item_lock(cfg, cid):
            for etype, payload in events:
                store.append_event(cfg, cid, etype, payload)
            store.write_state(cfg, cid, state)
        row["to"], row["reason"] = res.from_state, "interrupted"
        return row
    if bad is not None:
        return finish("PARKED", "config_invalid", park={
            "reason": "config_invalid", "detail": f"project config: {bad.error}",
            "resume_to": "REVIEWING"})
    _store_verdicts(state, profile, counters, res.reviews)
    return _route_reviews(ctx, state, profile, counters, row, events, finish)


def _route_reviews(ctx, state, profile, counters, row, events, finish):
    """Follow `decide_reviews` on the stored verdicts and waivers (SPEC-0203, SPEC-0206): retry
    missing reviewers, ask the triager (`triage` pending) or skip it on the cap, then pass, park
    or send the implementer back."""
    reviewers = reviewers_of(profile)
    multi = len(reviewers) > 1
    valid = _valid_verdicts(state, profile)
    lr = state.get("last_review") or {}
    decision = decide_reviews(state, profile, valid, [p for p in reviewers if p not in valid],
                              [w["id"] for w in lr.get("waived") or [] if isinstance(w, dict)])
    if decision.kind == "none":                          # missing verdicts, retry allowed
        by = counters.setdefault("review_retries_by_persona", {})
        for p in decision.missing:
            by[p] = by.get(p, 0) + 1
        counters["review_retries"] += 1
        return finish("REVIEWING", "review_retry")
    if decision.combined is not None:
        c = decision.combined
        tri = c["triage"]
        lr = state["last_review"]
        if lr.get("triage") is None and needs_triager(tri, profile):
            if _cap_blocks(state, profile, ["review_triager"]) is None:
                lr["triage"] = "pending"
                return finish("REVIEWING", "review_triage")
            lr["triage"] = "skipped"
            events.append(("note", {"text": "triager skipped: cost cap", "author": "queue"}))
        surviving = tri["blocking"] + tri["advisory"]
        lr.update(
            verdict=tri["outcome"], score=tri["score"], findings=len(surviving),
            must_fix=sum(1 for f in surviving if f["severity"] == "must_fix"),
            waived=lr.get("waived") or [], advisory=tri["advisory"], loop=counters["loops"])
        events.append(("note", {"text": f"review score {tri['score']}/{c['pass_score']} "
                                        f"{tri['outcome']}", "author": "queue"}))
        if tri["outcome"] == "pass":
            for f in tri["advisory"]:
                if f["severity"] == "major":
                    events.append(("note", {"text": f"pass over major: {f['id']} {f['title']}",
                                            "author": "queue"}))
    if decision.kind == "park":
        if decision.reason == "loop_exhausted" and decision.combined is not None:
            _merge_carry(state, _carry_source(state, decision.combined["triage"]))
        return finish("PARKED", decision.reason, park={
            "reason": decision.reason, "detail": decision.detail,
            "resume_to": decision.resume_to})
    counters["review_retries"] = 0
    counters.pop("review_retries_by_persona", None)
    if decision.kind == "close":
        return finish("CLOSING", "review_pass")
    # fix
    counters["loops"] += 1
    counters["gate_retries"] = 0
    counters["triage_retries"] = 0
    _merge_carry(state, _carry_source(state, decision.combined["triage"]))
    notes = ((state.get("pending_brief") or {}).get("human_notes") or [])
    brief = decision.combined["brief"]
    state["pending_brief"] = {
        "source": "review" if multi else reviewers[0], "fix_brief": brief["fix_brief"],
        "must_fix": brief["must_fix"], "human_notes": list(notes)}
    if multi:
        state["pending_brief"]["personas"] = brief["personas"]
    return finish("IMPLEMENTING", "review_fix")


# ---- tick / run ------------------------------------------------------------------------------

@dataclass
class TickReport:
    steps: list = field(default_factory=list)
    admitted: int = 0
    idle: bool = True
    errors: list = field(default_factory=list)
    started: list = field(default_factory=list)     # SPEC-0213: steps submitted this tick
    in_flight: list = field(default_factory=list)   # campaigns still running at return
    drain: bool = False                             # the shutdown / re-exec drain report

    def payload(self) -> dict:
        return {"schema": "wt.queue.tick.v1", "steps": self.steps, "admitted": self.admitted,
                "idle": self.idle, "errors": self.errors, "started": self.started,
                "in_flight": self.in_flight, "drain": self.drain}


def slots_for(cfg, profile_name=None) -> int:
    try:
        return int(resolve_profile(cfg, profile_name).get("slots") or 1)
    except ValueError:
        return 1


@dataclass
class _Flight:
    """One campaign's in-flight agent step (SPEC-0213): one task for implement / triage /
    gate, one task per reviewer (and the `_ReviewGroup`) for a fan-out. Keeps the `_Ctx` it
    was planned with; its state dict belongs to the workers until it is harvested."""
    state: dict
    ctx: "_Ctx"
    tasks: list = field(default_factory=list)       # [(Step, Future)]
    group: object = None
    started_at: str = field(default_factory=lambda: store.now_iso())
    runs: list = field(default_factory=list)        # [(run_id, timeout_s)] of its persona runs

    def done(self) -> bool:
        return all(f.done() for _, f in self.tasks)


class StepPool:
    """The persistent executor and the per-campaign flights (SPEC-0213). `run()` owns one for
    its whole life; `tick()` without one makes a throwaway pool and waits for it. Threads are
    created on demand, so the high cap never runs more than slots × reviewers at once."""

    MAX_WORKERS = 64

    def __init__(self):
        self._pool: ThreadPoolExecutor | None = None
        self.flights: dict = {}                    # campaign id -> _Flight, submission order

    def submit(self, fn, *args) -> Future:
        if self._pool is None:
            self._pool = ThreadPoolExecutor(max_workers=self.MAX_WORKERS,
                                            thread_name_prefix="wt-step")
        return self._pool.submit(fn, *args)

    def ids(self) -> set:
        return set(self.flights)

    def any_done(self) -> bool:
        return any(fl.done() for fl in self.flights.values())

    def wait_any(self, timeout=None) -> None:
        pending = [f for fl in self.flights.values() for _, f in fl.tasks if not f.done()]
        if pending and not self.any_done():
            wait(pending, timeout=timeout, return_when=FIRST_COMPLETED)

    def wait_all(self) -> None:
        wait([f for fl in self.flights.values() for _, f in fl.tasks])

    def shutdown(self, *, cancel=False) -> None:
        if self._pool is not None:
            self._pool.shutdown(wait=True, cancel_futures=cancel)
            self._pool = None


def _harvest(pool: StepPool, report: TickReport) -> None:
    """Main thread: apply every finished flight, in submission order (SPEC-0213)."""
    for cid in [c for c, fl in pool.flights.items() if fl.done()]:
        fl = pool.flights.pop(cid)
        st, ctx = fl.state, fl.ctx
        if fl.group is not None:
            for step, fut in fl.tasks:
                try:
                    fl.group.results[step.persona] = fut.result()
                except Exception as e:          # a bug in a step must park, not kill the loop
                    fl.group.results[step.persona] = e
            report.steps.append(_finish_reviews(ctx, report, fl.group))
            continue
        (_step, fut), = fl.tasks
        try:
            res = fut.result()
        except Exception as e:
            res = StepResult(st["id"], _step, st["state"], error=repr(e))
            report.errors.append(f"{st['id']}: {e!r}")
        if res.crashed:
            report.errors.append(f"{st['id']}: {res.crashed}")
        report.steps.append(_apply(ctx, st, res))


def _submit(ctx: _Ctx, pool: StepPool, report: TickReport, pending_agent: list,
            groups: list) -> None:
    """Submit the planned agent steps as one flight per campaign."""
    rows = {}
    for st, step, run_id, req, budget, group in pending_agent:
        fl = pool.flights.get(st["id"])
        if fl is None:
            fl = pool.flights[st["id"]] = _Flight(st, ctx, group=group)
            rows[st["id"]] = {"campaign": st["id"], "action": step.kind,
                              "state": st.get("state"), "run_ids": []}
            report.started.append(rows[st["id"]])
        if step.kind == "gate":
            fl.tasks.append((step, pool.submit(_do_gate, ctx, st)))
        elif step.kind == "close":
            fl.tasks.append((step, pool.submit(_do_prepare_close, ctx, st)))
        elif step.kind == "resolve":
            fl.tasks.append((step, pool.submit(_do_resolve, ctx, st, step, run_id, req, budget)))
            rows[st["id"]]["run_ids"].append(run_id)
            fl.runs.append((run_id, getattr(req, "timeout_s", None)))
        elif step.kind == "write_spec":
            fl.tasks.append((step, pool.submit(_do_write_spec, ctx, st, step, run_id, req,
                                               budget)))
            rows[st["id"]]["run_ids"].append(run_id)
            fl.runs.append((run_id, getattr(req, "timeout_s", None)))
        else:
            fl.tasks.append((step, pool.submit(_do_persona, ctx, st, step, run_id, req, budget)))
            rows[st["id"]]["run_ids"].append(run_id)
            fl.runs.append((run_id, getattr(req, "timeout_s", None)))
    for group in groups:              # a fan-out whose every prepare failed: nothing to run
        if group.state["id"] not in pool.flights:
            report.steps.append(_finish_reviews(ctx, report, group))


def tick(cfg, *, stop: threading.Event | None = None, runner=None, admit=True,
         pool: StepPool | None = None, lease=None) -> TickReport:
    """One bounded iteration: apply finished steps, admit, open, plan, submit agent steps,
    close serially; every result is applied on this thread. The caller holds the lease.

    With `pool` (SPEC-0213, owned by `run()`), agent steps outlive the tick: a campaign with a
    step in flight is skipped (and counts as busy) until a later tick harvests its result.
    Without one, the tick waits for its own steps before the closes (single shot). With `lease`
    (SPEC-0228, passed by `run()`), the long main-thread steps declare themselves `busy`."""
    own = pool is None
    pool = StepPool() if own else pool
    ctx = _Ctx(cfg, stop=stop, runner=runner, lease=lease)
    report = TickReport()
    try:
        _tick(cfg, ctx, pool, report, admit=admit, own=own)
    finally:
        if own:
            pool.shutdown()
    report.in_flight = list(pool.flights)
    remaining = [s for s in store.list_campaigns(cfg) if s.get("state") != "PARKED"]
    report.idle = not remaining and not pool.flights
    return report


def _tick(cfg, ctx: _Ctx, pool: StepPool, report: TickReport, *, admit: bool, own: bool):
    _harvest(pool, report)            # finished steps first: their slots are free this tick
    flying = pool.ids()
    items = [s for s in store.list_campaigns(cfg) if s.get("state") != "PARKED"]
    slots = slots_for(cfg)
    busy = [s for s in items if s.get("state") in ACTIVE_STATES]
    queued = [s for s in items if s.get("state") == "QUEUED"]

    def n_busy() -> int:              # SPEC-0200 slots; in-flight campaigns always count
        return len({s["id"] for s in busy} | flying)

    # 0. campaigns parked on a bad project config resume once it loads (SPEC-0194) — into a
    #    free slot only, so a recovery never runs more campaigns than the profile allows
    for st in store.list_campaigns(cfg, state="PARKED"):
        park = st.get("park") or {}
        if park.get("reason") != "config_invalid" or st["id"] in flying:
            continue
        try:
            ctx.project(st)
        except ProjectConfigError:
            continue
        to = park.get("resume_to") or "QUEUED"
        frm = st.get("state")
        st["park"] = None
        _enter_slot_or_wait(cfg, st, to, "config_recovered", busy=n_busy(), slots=slots)
        report.steps.append({"campaign": st["id"], "from": frm, "to": st["state"],
                             "action": "recover", "reason": "config_recovered",
                             "run_id": None, "cost_usd": None})
        if st["state"] in ACTIVE_STATES:
            busy.append(st)
        elif st["state"] == "QUEUED":
            queued.append(st)

    # 0b. started campaigns waiting for a slot go first (SPEC-0200): they continue where they
    #     stopped — no re-open — so their branch does not go stale behind new work
    waiting = sorted((s for s in items if s.get("state") == "WAITING"), key=admission_key)
    for st in waiting:
        if not admit or ctx.stop.is_set() or n_busy() >= slots:
            break
        if st.get("park_requested") or st["id"] in flying:
            continue
        to = (st.get("waiting") or {}).get("resume_to") or "IMPLEMENTING"
        st.pop("waiting", None)
        _transition(cfg, st, to, "slot_free")
        report.steps.append({"campaign": st["id"], "from": "WAITING", "to": to,
                             "action": "continue", "reason": "slot_free",
                             "run_id": None, "cost_usd": None})
        busy.append(st)

    # 1. admission + opening (serial); an item admitted now waits for the next tick
    ready = {}
    for st in queued:
        ready[st["id"]] = backlog_readiness(cfg, st)
        _record_backlog(cfg, st, *ready[st["id"]])
    admitted_ids = set()
    if admit and not ctx.stop.is_set():
        for st in pick_admissions([s for s in queued if s["id"] not in flying], n_busy(),
                                  slots, lambda s: ready[s["id"]]):
            _transition(cfg, st, "OPENING", "admitted")
            report.admitted += 1
            admitted_ids.add(st["id"])
            report.steps.append(_apply(ctx, st, _do_open(ctx, st)))
            if st.get("state") in ACTIVE_STATES:
                busy.append(st)

    # 2. plan every active item without a step in flight (never a second step: SPEC-0213)
    pending_agent = []       # (state, step, run_id, req, budget, review group) to submit
    groups = []              # reviewer fan-outs, applied once each when harvested (SPEC-0201)
    pending_close = []
    for st in busy:
        if st.get("state") not in ACTIVE_STATES or st["id"] in admitted_ids \
                or st["id"] in flying:
            continue
        try:
            profile = ctx.profile(st)
            pcfg = ctx.project(st)
        except ValueError as e:
            report.steps.append(_apply(ctx, st, StepResult(
                st["id"], Step("open"), st["state"], error=str(e),
                config_error=isinstance(e, ProjectConfigError))))
            continue
        step = plan_step(st, profile, pcfg)
        if step.kind == "none":
            continue
        if step.kind == "park":
            report.steps.append(_park_row(cfg, st, step))
            continue
        if step.kind == "open":
            report.steps.append(_apply(ctx, st, _do_open(ctx, st)))
            continue
        if step.kind == "close":
            if ctx.stop.is_set():
                continue
            if own:
                pending_close.append(st)
            else:
                pending_agent.append((st, step, None, None, 0.0, None))
            continue
        if step.kind == "gate":
            pending_agent.append((st, step, None, None, 0.0, None))
            continue
        if ctx.stop.is_set():
            continue
        if step.kind == "review":
            group = _prepare_reviews(ctx, st, step, profile, pending_agent, report)
            if group:
                groups.append(group)
            continue
        try:
            run_id, req, budget = _prepare_run(ctx, st, step.persona)
        except (ValueError, OSError) as e:
            started = e if isinstance(e, _RunStarted) else None     # run.start is on the ledger
            report.steps.append(_apply(ctx, st, StepResult(
                st["id"], step, st["state"], error=str(e),
                run_id=started.run_id if started else None,
                budget=started.budget if started else 0.0)))
            continue
        pending_agent.append((st, step, run_id, req, budget, None))

    # 3. agent steps: submitted; a single-shot tick waits for its own and applies them now
    _submit(ctx, pool, report, pending_agent, groups)
    if own:
        pool.wait_all()
        _harvest(pool, report)

    # 4. single shot: closes one at a time, worktree phase then finish phase (SPEC-0220); a
    #    moved base redoes the close within the tick until the budget is spent
    for st in pending_close:
        while not ctx.stop.is_set():
            pool.flights[st["id"]] = fl = _Flight(st, ctx)
            fl.tasks.append((Step("close"), pool.submit(_do_prepare_close, ctx, st)))
            pool.wait_all()
            _harvest(pool, report)
            if report.steps[-1].get("reason") != "base_moved":
                break
            st = store.read_state(cfg, st["id"])


def _prepare_reviews(ctx: _Ctx, st: dict, step: Step, profile: dict, pending_agent: list,
                     report: TickReport):
    """Prepare one run per pending reviewer, in profile order (run ids are allocated before any
    task starts). A failure with one reviewer configured is the campaign-wide error of
    SPEC-0187; with several it is that reviewer's missing result and its siblings still run.
    Returns the `_ReviewGroup` to apply when its flight is harvested, or None."""
    # the triager (SPEC-0206) never takes the one-reviewer campaign-wide error path
    multi = len(reviewers_of(profile)) > 1 or step.persona == "review_triager"
    group = _ReviewGroup(st, step)
    for persona in step.personas:
        if ctx.stop.is_set():
            break
        sub = Step("review", persona=persona, personas=step.personas)
        try:
            run_id, req, budget = _prepare_run(ctx, st, persona)
        except (ValueError, OSError) as e:
            if not multi:
                report.steps.append(_apply(ctx, st, StepResult(
                    st["id"], step, st["state"], error=str(e))))
                return None
            if isinstance(e, _RunStarted):          # run.start is written: it needs a run.end
                report.errors.append(f"{st['id']}: {e!r}")
                group.results[persona] = StepResult(
                    st["id"], sub, st["state"], run_id=e.run_id, budget=e.budget,
                    run_result=_synthetic_failure(repr(e)))
            else:
                group.results[persona] = StepResult(
                    st["id"], sub, st["state"], error=str(e),
                    config_error=isinstance(e, ProjectConfigError))
            continue
        group.meta[persona] = (run_id, budget)
        pending_agent.append((st, sub, run_id, req, budget, group))
    return group


def _finish_reviews(ctx: _Ctx, report: TickReport, group: _ReviewGroup) -> dict:
    """Apply a reviewer fan-out once, results in profile order. One reviewer: its bare result
    (or the campaign-wide error of a raising worker), as SPEC-0187. Several: a worker that
    raised or errored after `run.start` becomes a synthetic failed run (a `run.end` and the
    budget charge), so the reviewer is simply missing."""
    st = group.state
    multi = len(reviewers_of(ctx.profile(st))) > 1 or group.step.persona == "review_triager"
    ordered = []
    for persona in group.step.personas:
        item = group.results.get(persona)
        if item is None:                 # never prepared (stop set between two prepares)
            continue
        if not multi:
            if isinstance(item, Exception):
                report.errors.append(f"{st['id']}: {item!r}")
                item = StepResult(st["id"], Step("implement"), st["state"], error=repr(item))
            return _apply(ctx, st, item)
        run_id, budget = group.meta.get(persona, (None, 0.0))
        if isinstance(item, Exception) or (item.error and item.run_result is None
                                           and run_id is not None):
            err = repr(item) if isinstance(item, Exception) else item.error
            report.errors.append(f"{st['id']}: {persona}: {err}")
            item = StepResult(st["id"], Step("review", persona=persona), st["state"],
                              run_id=run_id, budget=budget,
                              run_result=_synthetic_failure(err))
        ordered.append(item)
    return _apply(ctx, st, StepResult(st["id"], group.step, st["state"], reviews=ordered))


def _park_row(cfg, state: dict, step: Step) -> dict:
    frm = state.get("state")
    by = "human" if step.reason == "human" else "tick"
    _park(cfg, state, step.reason, step.detail or step.reason, step.resume_to or frm, by=by)
    return {"campaign": state["id"], "from": frm, "to": "PARKED", "action": "park",
            "run_id": None, "cost_usd": None, "reason": step.reason}


def runner_journal_path(cfg) -> Path:
    return store.agentic_dir(cfg) / "runner.jsonl"


def journal(cfg, kind: str, **fields) -> dict:
    """Append one `wt.runner.record.v1` line to `<agentic>/runner.jsonl` (SPEC-0192): a single
    O_APPEND write, so concurrent writers never interleave. Never raises."""
    import json
    rec = {"schema": SC.RUNNER_SCHEMA, "ts": store.now_iso(), "kind": kind, "pid": os.getpid(),
           **fields}
    try:
        path = runner_journal_path(cfg)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, (json.dumps(rec, ensure_ascii=False, default=str) + "\n").encode())
        finally:
            os.close(fd)
    except OSError:
        pass
    return rec


def read_journal(cfg, *, tail=None) -> list[dict]:
    """`runner.jsonl` records in order (unparseable lines skipped)."""
    import json
    path = runner_journal_path(cfg)
    if not path.is_file():
        return []
    out = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(raw)
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out[-tail:] if tail else out


def journal_tail_for_pid(cfg, pid, max_bytes: int = 262144) -> dict | None:
    """The newest `runner.jsonl` record written by `pid` within the last `max_bytes` of the
    file (SPEC-0228), else None. A bounded backwards read — never the whole journal; the
    partial first line of the window and unparseable lines are skipped."""
    path = runner_journal_path(cfg)
    try:
        with open(path, "rb") as f:
            size = f.seek(0, os.SEEK_END)
            start = max(0, size - max_bytes)
            f.seek(start)
            data = f.read()
    except OSError:
        return None
    lines = data.split(b"\n")
    if start > 0:
        lines = lines[1:]                           # the window began mid-line
    for raw in reversed(lines):
        try:
            rec = json.loads(raw)
        except ValueError:
            continue
        if isinstance(rec, dict) and rec.get("pid") == pid:
            return rec
    return None


# ---- code-change re-exec (SPEC-0194) ------------------------------------------------------------
# The runner imports wt from a checkout (an editable install of main). When a merge changes
# that code, a long-lived loop would keep running the old code — and reject config the new
# code accepts. The loop checks the checkout's HEAD between ticks; on a change it stops
# submitting, drains the in-flight steps (SPEC-0213), and re-executes itself, handing its lease
# over so the queue is never unowned.

_EXEC = os.execve                      # tests replace this


class _heartbeat_while:
    """Heartbeat the lease from a daemon thread (SPEC-0204): a tick that runs a close's gates,
    or the drain of long persona runs, can outlast the lease TTL, and an alive runner must
    never look stale. `run()` holds it for the loop's whole life (SPEC-0213). Beats every
    ttl/4 seconds (at least 1s)."""

    def __init__(self, lease, interval_s=None):
        self.lease = lease
        ttl = float((lease.info or {}).get("ttl_s") or 120)
        self.interval = interval_s if interval_s is not None else max(1.0, ttl / 4)
        self._stop = threading.Event()
        self._thread = None

    def _beat(self):
        last_logged = None
        while not self._stop.wait(self.interval):
            try:
                self.lease.heartbeat()
            except Exception as e:             # SPEC-0224: this thread never dies silently
                now = time.monotonic()
                if last_logged is None or now - last_logged >= 60:
                    last_logged = now
                    journal(self.lease.cfg, "heartbeat_error",
                            error=f"{type(e).__name__}: {e}")

    def __enter__(self):
        self._thread = threading.Thread(target=self._beat, name="wt-lease-heartbeat",
                                        daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(timeout=5)
        return False


# ---- config reload (SPEC-0226) --------------------------------------------------------------
# The loop re-reads the user config when its file changes (an invalid file never replaces the
# working one), and journals what changed in every live project's `.wt/project.yaml`, which the
# per-tick `_Ctx` already re-reads.

_RUNTIME_PATHS = frozenset({"config_dir", "data_dir", "cache_dir"})
_DIFF_CAP = 50
_MISSING = object()


def _flatten(doc: dict, prefix: str = "") -> dict:
    out = {}
    for k, v in doc.items():
        k = str(k)
        if k.startswith("_") or (not prefix and k in _RUNTIME_PATHS):
            continue
        if isinstance(v, dict) and v:
            out.update(_flatten(v, f"{prefix}{k}."))
        else:
            out[f"{prefix}{k}"] = v
    return out


def config_diff(old: dict, new: dict) -> list[str]:
    """Sorted dotted paths whose values differ (added and removed included); mappings are
    flattened, lists compare whole, `_*` and runtime path keys are ignored. Capped at 50 with
    a trailing `"…"`."""
    a, b = _flatten(old or {}), _flatten(new or {})
    keys = sorted(k for k in a.keys() | b.keys()
                  if a.get(k, _MISSING) != b.get(k, _MISSING))
    return keys[:_DIFF_CAP] + ["…"] if len(keys) > _DIFF_CAP else keys


def _positive_number(agentic: dict, key: str) -> None:
    v = agentic.get(key)
    if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or not v > 0):
        raise ValueError(f"agentic.{key} must be a number > 0, got {v!r}")


def validate_runner_cfg(cfg: dict, campaigns=()) -> None:
    """ValueError when `cfg` cannot drive the runner: the default profile and every profile a
    live campaign names must resolve with `slots` an int >= 1; `poll_interval_s` and
    `lease_ttl_s` must be numbers > 0; `runner` must be claude or fake."""
    agentic = cfg.get("agentic") or {}
    if not isinstance(agentic, dict):
        raise ValueError("agentic must be a mapping")
    for name in [None, *sorted({c["profile"] for c in campaigns if c.get("profile")})]:
        slots = resolve_profile(cfg, name).get("slots")
        if isinstance(slots, bool) or not isinstance(slots, int) or slots < 1:
            raise ValueError(f"profile {name or 'default'!r}: slots must be an int >= 1, "
                             f"got {slots!r}")
    _positive_number(agentic, "poll_interval_s")
    _positive_number(agentic, "lease_ttl_s")
    if agentic.get("runner") not in (None, "claude", "fake"):
        raise ValueError(f"unknown agentic.runner {agentic['runner']!r} "
                         f"(expected claude or fake)")


_UNSEEN = object()                                # a stat key no file can have


def _stat_key(path) -> tuple | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _err(e: Exception) -> str:
    return f"{type(e).__name__}: {e}"


class ConfigWatch:
    """Per-tick config change detection for `run()`. Compares `(mtime_ns, size)` of the user
    config and of the `.wt/project.yaml` of every project with a live campaign against the last
    seen; first sight seeds silently. Broken files are reported through the lease info
    (`config_errors`, path -> entry), each file setting and clearing only its own entry."""

    def __init__(self, cfg: dict, loader):
        self.loader = loader
        config_dir = cfg.get("config_dir")
        self.user_path = Path(config_dir) / "config.yaml" if config_dir else None
        self.user_key = _stat_key(self.user_path) if self.user_path else None
        self._projects: dict = {}                 # path -> {"key", "doc" (last good or None)}
        self._errors: dict = {}
        self._first = True
        self._moved = False

    def user_invalid(self) -> bool:
        """True while the user config on disk is broken and the old cfg is still in use."""
        return any(e["scope"] == "user" for e in self._errors.values())

    def check(self, cfg: dict, lease, campaigns) -> dict:
        """The cfg the next tick should use: the freshly loaded user config when it changed and
        is valid, else `cfg`."""
        if self._first:
            self._first = False
            if lease is not None and (lease.info or {}).get("config_errors"):
                lease.pop_info("config_errors")       # left by the image before a re-exec
        if self.user_path is not None:
            key = _stat_key(self.user_path)
            if key != self.user_key:
                self.user_key = key
                cfg = self._reload_user(cfg, lease, campaigns, key)
                if self._moved:                       # rewritten while we read it: retry next tick
                    self._moved = False
                    self.user_key = _UNSEEN
        self._check_projects(cfg, lease, campaigns)
        return cfg

    def _read_user_file(self) -> None:
        """Raise when the raw file is missing, empty or not a mapping: `load_config` would
        otherwise return pure defaults for it."""
        try:
            text = self.user_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise ValueError(f"{self.user_path} is missing")
        if not text.strip():
            raise ValueError(f"{self.user_path} is empty")
        doc = yaml.safe_load(text)
        if not isinstance(doc, dict):
            raise ValueError(f"{self.user_path} is not a mapping (got {type(doc).__name__})")

    def _reload_user(self, cfg, lease, campaigns, key) -> dict:
        path = str(self.user_path)
        try:
            self._read_user_file()
            new = self.loader()
            validate_runner_cfg(new, campaigns)
            if _stat_key(self.user_path) != key:      # truncated or rewritten while we read it
                self._moved = True
                return cfg
        except Exception as e:
            error = _err(e)
            journal(cfg, "config_load_failed", scope="user", path=path, error=error)
            self._set_error(lease, path, "user", error, key)
            return cfg
        self._clear_error(lease, path)
        changed = config_diff(cfg, new)
        if changed:
            journal(cfg, "config_reloaded", scope="user", path=path, changed=changed,
                    restart_required=[k for k in changed if k == "agentic.lease_ttl_s"])
        return new

    def _check_projects(self, cfg, lease, campaigns) -> None:
        live = {}
        for name in sorted({c["project"] for c in campaigns if c.get("project")}):
            try:
                live[name] = P.config_path(P.project_root(cfg, name))
            except Exception:                         # the tick reports an unregistered project
                continue
        wanted = {str(p) for p in live.values()}
        for path in [p for p in self._projects if p not in wanted]:
            del self._projects[path]
            self._clear_error(lease, path)
        for name, p in live.items():
            path, key, scope = str(p), _stat_key(p), f"project:{name}"
            seen = self._projects.get(path)
            if seen is not None and seen["key"] == key:
                continue
            last_good = seen["doc"] if seen else None
            try:
                doc = P.load_project_config(p)
                if doc.get("project") and doc["project"] != name:
                    raise ValueError(f"{p}: project: {doc['project']!r} does not match "
                                     f"{name!r}")
            except Exception as e:
                error = _err(e)
                journal(cfg, "config_load_failed", scope=scope, path=path, error=error)
                self._set_error(lease, path, scope, error, key)
                self._projects[path] = {"key": key, "doc": last_good}
                continue
            self._clear_error(lease, path)
            changed = config_diff(last_good, doc) if last_good is not None else []
            if changed:
                journal(cfg, "config_reloaded", scope=scope, path=path, changed=changed)
            self._projects[path] = {"key": key, "doc": doc}

    def _set_error(self, lease, path, scope, error, key) -> None:
        self._errors[path] = {"scope": scope, "error": error, "since": store.now_iso(),
                              "mtime_ns": key[0] if key else 0}
        self._publish(lease)

    def _clear_error(self, lease, path) -> None:
        if self._errors.pop(path, None) is not None:
            self._publish(lease)

    def _publish(self, lease) -> None:
        if lease is None:
            return
        if self._errors:
            lease.update_info(config_errors=dict(self._errors))   # always a new dict
        else:
            lease.pop_info("config_errors")


def _waiting_on(cfg, pool: StepPool) -> list:
    """The in-flight flights as `{campaign, kind, run_ids, timeout_s}` (SPEC-0224)."""
    out = []
    for cid, fl in pool.flights.items():
        kind = fl.tasks[0][0].kind if fl.tasks else "implement"
        if kind == "gate":
            timeout = events_settings(cfg)["gate_budget_s"]
        else:
            timeout = max((t for _, t in fl.runs if t is not None), default=None)
        out.append({"campaign": cid, "kind": kind, "run_ids": [r for r, _ in fl.runs],
                    "timeout_s": timeout})
    return out


def _refresh_backlog(cfg) -> None:
    """Store the readiness of every QUEUED item (what step 1 of `_tick` does, without
    admitting), so a merge that just made one ready is visible to the watchdog."""
    for st in store.list_campaigns(cfg, state="QUEUED"):
        _record_backlog(cfg, st, *backlog_readiness(cfg, st))


class _SlotWatch:
    """The runner's last seen occupied count — the tick's `n_busy`, active-state campaigns
    plus flights (SPEC-0228). `check` stamps `slot_freed_at` in the lease when it drops, so
    the watchdog reads when a slot freed instead of guessing it from state rows."""

    def __init__(self, cfg, pool: StepPool):
        self.cfg, self.pool = cfg, pool
        self.last = self.count()

    def count(self) -> int:
        active = {s["id"] for s in store.list_campaigns(self.cfg)
                  if s.get("state") in ACTIVE_STATES}
        return len(active | self.pool.ids())

    def check(self, lease, **info) -> None:
        """One `update_info` call: `info` plus `in_flight` and, on a drop, `slot_freed_at`."""
        now = self.count()
        if now < self.last:
            info["slot_freed_at"] = store.now_iso()
        self.last = now
        lease.update_info(in_flight=sorted(self.pool.ids()), **info)


def _drain(cfg, pool: StepPool, lease, *, reason: str = "signal",
           interval: float = 20.0, slots: "_SlotWatch | None" = None) -> TickReport:
    """Join every flight, applying each as it finishes (main thread), before the loop stops
    or re-execs (SPEC-0213). Returns once `pool.flights` is empty. While it waits, the lease
    info carries `draining` (SPEC-0224) for the out-of-process watchdog, and the stored
    backlog readiness is refreshed every `interval` seconds. The drain is a declared `busy`
    step (SPEC-0228), so the watchdog does not call a quiet journal a stall within its budget:
    the largest flight timeout, from drain start — conservative on purpose, since flights
    began earlier. A harvest refreshes `in_flight` (and `slot_freed_at` on a drop)."""
    report = TickReport(drain=True)
    since = store.now_iso()
    waiting_on = _waiting_on(cfg, pool)
    gate_budget = events_settings(cfg)["gate_budget_s"]
    budget = max((w.get("timeout_s") or gate_budget for w in waiting_on), default=gate_budget)
    lease.update_info(draining={"reason": reason, "since": since, "waiting_on": waiting_on})
    try:
        with _busy(lease, "drain", budget_s=budget):
            next_refresh = time.monotonic()
            while pool.flights:
                if time.monotonic() >= next_refresh:
                    _refresh_backlog(cfg)
                    next_refresh = time.monotonic() + interval
                pool.wait_any(timeout=0.5)
                before = len(pool.flights)
                _harvest(pool, report)
                if len(pool.flights) != before:
                    lease.update_info(draining={"reason": reason, "since": since,
                                                "waiting_on": _waiting_on(cfg, pool)})
                    if slots is not None:
                        slots.check(lease)
                    else:
                        lease.update_info(in_flight=sorted(pool.ids()))
    finally:
        lease.pop_info("draining")
    lease.heartbeat()
    report.idle = not [s for s in store.list_campaigns(cfg) if s.get("state") != "PARKED"]
    return report


def run(cfg, *, once=False, max_ticks=None, poll_interval_s=None, until_idle=False,
        on_tick=None, runner=None, argv=None, reexec=False, code_root=None,
        config_loader=None) -> int:
    """The loop: take the lease, tick, heartbeat, sleep. Agent steps run in a pool owned by
    the loop and carry across ticks (SPEC-0213); the wait between ticks ends early when one
    finishes. SIGINT/SIGTERM stop admissions, forward SIGTERM to this process group's children
    (the `claude` runs) when we lead the group, drain the in-flight steps (each records
    itself), and release the lease. Every exit drains first. Returns the number of ticks
    performed.

    With `reexec` (SPEC-0194), when a commit to the wt checkout (`code_root`, default the one
    this package is imported from) changes wt's code, the loop stops between ticks, journals
    `stop` with reason `code_changed` once its in-flight steps are drained, and re-executes
    the same command keeping its lease.

    With `config_loader` (SPEC-0226, a callable returning a fresh cfg), the loop re-reads the
    user config before each tick when its file changed; in-flight steps keep the cfg they were
    planned with. A config that fails to load or validate never replaces the working one."""
    agentic = cfg.get("agentic") or {}
    interval = float(poll_interval_s if poll_interval_s is not None
                     else agentic.get("poll_interval_s") or 20)
    stop = threading.Event()
    lease = L.acquire(cfg, argv or ["wt", "queue", "run"])
    lease.update_info(poll_interval_s=interval, slot_freed_at=store.now_iso())   # SPEC-0228
    previous = {}

    def _handler(signum, _frame):
        if stop.is_set():
            return
        stop.set()
        try:
            if os.getpgid(0) == os.getpid():                # we lead the group (setsid/nohup)
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
                os.killpg(os.getpgid(0), signal.SIGTERM)
        except OSError:
            pass

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            previous[sig] = signal.signal(sig, _handler)
        except ValueError:                                  # not the main thread (tests)
            pass
    ticks = 0
    reason = "signal"
    watch_root = (code_root or code_checkout()) if reexec else None
    code_sha = _git_out(watch_root, "rev-parse", "HEAD") if watch_root else None
    restart = {}
    deferred = None                                   # head a re-exec was deferred for
    pool = StepPool()                                 # SPEC-0213: flights outlive ticks
    watch = ConfigWatch(cfg, config_loader) if config_loader else None
    slots = _SlotWatch(cfg, pool)

    def emit(report):
        if report.steps or report.admitted or report.errors or report.started:
            journal(cfg, "tick", tick=ticks, steps=report.steps, admitted=report.admitted,
                    errors=report.errors, started=report.started,
                    in_flight=report.in_flight, drain=report.drain)
        if on_tick:
            on_tick(report)

    journal(cfg, "start", argv=list(argv or ["wt", "queue", "run"]), interval_s=interval,
            code_sha=code_sha)
    crashed = False
    try:
        # SPEC-0204/0213: one heartbeat thread for the loop's whole life — ticks, waits, the
        # drain — joined before the lease is released or handed over
        with _heartbeat_while(lease):
            while not stop.is_set():
                if watch:
                    live = [c for c in store.list_campaigns(cfg)
                            if c.get("state") not in TERMINAL_STATES]
                    new_cfg = watch.check(cfg, lease, live)
                    if new_cfg is not cfg:
                        cfg = new_cfg
                        if poll_interval_s is None:
                            interval = float((cfg.get("agentic") or {}).get("poll_interval_s")
                                             or 20)
                            lease.update_info(poll_interval_s=interval)   # SPEC-0228
                report = tick(cfg, stop=stop, runner=runner, pool=pool, lease=lease)
                ticks += 1
                lease.heartbeat()
                # SPEC-0228: stamp before `emit`, so a slow `on_tick` never delays it
                slots.check(lease, tick_at=store.now_iso())
                emit(report)
                if once or (max_ticks and ticks >= max_ticks) or (until_idle and report.idle):
                    reason = "once" if once else ("max_ticks" if max_ticks and ticks >= max_ticks
                                                  else "idle")
                    break
                if code_sha:
                    head, changed = code_changes(watch_root, code_sha)
                    if changed and watch and watch.user_invalid():
                        # SPEC-0226: the new image would load the broken user config (crash
                        # or pure defaults); keep running on the last good one and keep
                        # `code_sha`, so the re-exec happens once the file is fixed
                        if deferred != head:
                            deferred = head
                            journal(cfg, "reexec_deferred", reason="user_config_invalid",
                                    to_sha=head)
                    else:
                        if changed:
                            # SPEC-0194 + SPEC-0213: stop admitting and submitting; the drain
                            # below records every in-flight step before the exec
                            reason = "code_changed"
                            restart = {"from_sha": code_sha, "to_sha": head,
                                       "paths": changed[:50]}
                            break
                        code_sha = head or code_sha   # docs-only commits: keep running
                wait_s = max(interval, _MIN_INFLIGHT_WAIT_S) if pool.flights else interval
                deadline = time.monotonic() + wait_s
                while time.monotonic() < deadline and not stop.is_set():
                    if pool.any_done():               # a finished step: apply it now
                        break
                    time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))
            if pool.flights:
                emit(_drain(cfg, pool, lease, reason=reason, interval=interval, slots=slots))
            if reason == "code_changed" and stop.is_set():
                reason, restart = "signal", {}        # signalled during the drain: no exec
    except BaseException as e:
        crashed = True
        if not isinstance(e, (KeyboardInterrupt, SystemExit)):
            import traceback
            journal(cfg, "crash", ticks=ticks, error=f"{type(e).__name__}: {e}",
                    traceback=traceback.format_exc()[-4000:])
            reason = "crash"
        raise
    finally:
        if crashed:
            stop.set()                                # workers see it: runs end as interrupted
        pool.shutdown(cancel=crashed)
        if reason != "crash":
            journal(cfg, "stop", ticks=ticks, reason=reason, **restart)
        handover_env = lease.handover() if reason == "code_changed" else None
        lease.release()
        for sig, prev in previous.items():
            try:
                signal.signal(sig, prev)
            except (ValueError, TypeError):
                pass
    if handover_env is not None:
        import sys
        sys.stdout.flush()
        sys.stderr.flush()
        _EXEC(sys.executable, [sys.executable, *sys.argv], {**os.environ, **handover_env})
    return ticks


# ---- listing -----------------------------------------------------------------------------------

def _age(ts: str | None, now=None) -> int | None:
    if not ts:
        return None
    import datetime as dt
    try:
        t = dt.datetime.fromisoformat(ts)
    except ValueError:
        return None
    now = now or dt.datetime.now().astimezone()
    if t.tzinfo is None:
        t = t.astimezone()
    return max(0, int((now - t).total_seconds()))


def park_view(cfg, state: dict, now, sett) -> dict:
    """SPEC-0222: who a PARKED campaign waits for and whether it was decided. Derived from the
    park and its ledger (`park` / `note` events); nothing is stored."""
    park = state.get("park") or {}
    reason = park.get("reason")
    owner = park_owner(reason)
    led = store.read_ledger(cfg, state["id"], types=["park", "note"])
    last = next((e for e in reversed(led) if e.get("type") == "park"), None)
    park_seq = (last or {}).get("seq") or 0
    parked_at = (last or {}).get("ts") or park.get("since")
    note = next((e for e in reversed(led)
                 if e.get("type") == "note" and (e.get("seq") or 0) > park_seq), None)
    decision = ({"text": note.get("text"), "ts": note.get("ts"),
                 "by": note.get("author") or note.get("by")} if note else None)
    decided = decision is not None or reason == "human"
    parked_for = _age(parked_at, now) or 0
    overdue = owner == "conductor" and not decided and parked_for > sett["park_decide_s"]
    return {"owner": owner, "decision": decision, "decided": decided, "overdue": overdue,
            "parked_at": parked_at, "park_seq": park_seq, "parked_for_s": parked_for}


_PARK_VIEW_KEYS = ("owner", "decision", "decided", "overdue", "parked_at")


def queue_payload(cfg, *, include_terminal=False, project=None, state=None, now=None) -> dict:
    """`wt.queue.v1`: the lease, the slots and every in-flight item."""
    import datetime as dt
    from . import events as EV
    now = now or dt.datetime.now().astimezone()
    sett = EV.settings(cfg)
    rows = store.list_campaigns(cfg, include_terminal=include_terminal, project=project,
                                state=state)
    slots = slots_for(cfg)
    busy = sum(1 for r in rows if r.get("state") in ACTIVE_STATES)
    backlog = {b["campaign"]: b for b in backlog_view(cfg)}
    items = []
    for r in rows:
        counters = r.get("counters") or {}
        cost = r.get("cost") or {}
        try:
            loop_max = resolve_profile(cfg, r.get("profile")).get("loop_max")
        except ValueError:
            loop_max = None
        repo = r.get("repo") or {}
        sizing = r.get("sizing") or {}
        park = r.get("park")
        if r.get("state") == "PARKED":
            view = park_view(cfg, r, now, sett)
            park = {**(park or {}), **{k: view[k] for k in _PARK_VIEW_KEYS}}
        items.append({
            "campaign": r.get("id"), "spec_id": r.get("spec_id"), "project": r.get("project"),
            "kind": r.get("kind"), "idea_id": _idea_id_of(r), "label": campaign_label(r),
            "merge_branch": (r.get("merge") or {}).get("branch") if r.get("kind") == "merge" else None,
            "profile": r.get("profile"), "state": r.get("state"),
            "size": sizing.get("size"), "tone": sizing.get("tone"),
            "profile_source": sizing.get("source"),
            "loops": f"{counters.get('loops', 0)}/{loop_max if loop_max is not None else '?'}",
            "runs": counters.get("runs", 0),
            "cost_usd": round(float(cost.get("total_usd") or 0.0), 4),
            "cap_usd": cost.get("cap_usd"),
            "cost": {"estimated_usd": round(float(cost.get("estimated_usd") or 0.0), 4)},
            "age_s": _age(r.get("created_at")), "updated_age_s": _age(r.get("updated_at")),
            "park": park, "park_requested": bool(r.get("park_requested")),
            "worktree": repo.get("worktree"), "branch": repo.get("branch"),
            "priority": _priority(r), "waiting": r.get("waiting"),
            "review_score": (r.get("last_review") or {}).get("score"),
        })
        b = backlog.get(r.get("id"))
        if b and r.get("state") == "QUEUED":
            items[-1]["backlog"] = {k: b[k] for k in ("position", "priority", "ready", "blocked_by")}
    return {"schema": "wt.queue.v1", "lease": L.lease_status(cfg),
            "slots": {"total": slots, "busy": busy}, "items": items}


# ---- dashboard (wt queue watch, SPEC-0185) -------------------------------------------------------

PHASES = ("OPENING", "SPEC_WRITING", "IMPLEMENTING", "GATING", "TRIAGING", "REVIEWING", "CLOSING")
_TITLE_CACHE: dict = {}


def _spec_title(state: dict) -> str | None:
    """The spec's frontmatter title in the project repo (cached on path + mtime); the idea's
    heading for an idea campaign without a spec (SPEC-0209)."""
    if state.get("kind") == "merge":
        return (state.get("merge") or {}).get("note") or "merge request"
    if has_no_spec(state):
        return (state.get("idea") or {}).get("heading")
    root = (state.get("repo") or {}).get("root")
    rel = (state.get("spec") or {}).get("rel_path")
    if not root or not rel:
        return None
    path = Path(root) / rel
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    hit = _TITLE_CACHE.get(str(path))
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        fm, _ = split_frontmatter(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    title = str((fm or {}).get("title") or "") or None
    _TITLE_CACHE[str(path)] = (mtime, title)
    return title


def _activity(state: dict, ledger: list, now=None) -> dict:
    """What the campaign is doing now: an open persona run, gates, or idle."""
    open_run = None
    for ev in ledger:
        if ev.get("type") == "run.start":
            open_run = ev
        elif ev.get("type") == "run.end" and open_run and ev.get("run_id") == open_run.get("run_id"):
            open_run = None
    if open_run and state.get("state") not in TERMINAL_STATES:
        return {"kind": "run", "persona": open_run.get("persona"), "model": open_run.get("model"),
                "effort": open_run.get("effort"), "run_id": open_run.get("run_id"),
                "elapsed_s": _age(open_run.get("ts"), now), "timeout_s": open_run.get("timeout_s"),
                "max_budget_usd": open_run.get("max_budget_usd")}
    if state.get("state") in ("GATING", "CLOSING"):
        return {"kind": "gates"}
    return {"kind": "idle"}


def _gate_summary(state: dict) -> dict | None:
    g = state.get("last_gate")
    if not g:
        return None
    return {"gate_seq": g.get("gate_seq"), "ok": g.get("ok"),
            "results": [{"name": r.get("name"), "ok": r.get("ok"), "required": r.get("required"),
                         "duration_s": r.get("duration_s")} for r in g.get("results") or []]}


def _verdict_summary(state: dict) -> dict | None:
    v = state.get("last_verdict")
    if not v:
        return None
    verdict = v.get("verdict") or {}
    summary = str(verdict.get("summary") or "").strip()
    return {"persona": v.get("persona"), "run": v.get("run"), "status": verdict.get("status"),
            "summary": summary.splitlines()[0] if summary else ""}


def dashboard_payload(cfg, *, project=None, recent=5, recent_hours=24, now=None) -> dict:
    """`wt.queue.dashboard.v1`: everything `wt queue watch` shows — the runner, the active
    campaigns with what each is doing now, what needs attention, the backlog, and what was
    recently delivered."""
    import datetime as dt
    now = now or dt.datetime.now().astimezone()
    states = {s.get("id"): s for s in store.list_campaigns(cfg, include_terminal=True,
                                                            project=project)}
    base = queue_payload(cfg, include_terminal=True, project=project, now=now)
    active, needs_you, conductor, waiting, backlog, delivered = [], [], [], [], [], []
    spend_today = 0.0
    for it in base["items"]:
        st = states.get(it["campaign"]) or {}
        title = _spec_title(st)
        updated = st.get("updated_at") or st.get("created_at")
        try:
            if updated and dt.datetime.fromisoformat(updated).astimezone().date() == now.date():
                spend_today += it["cost_usd"]
        except ValueError:
            pass
        state = it["state"]
        if state in TERMINAL_STATES:
            age = _age(updated, now)
            if age is not None and age <= recent_hours * 3600:
                close = st.get("close") or {}
                delivered.append({
                    "campaign": it["campaign"], "spec_id": it["spec_id"], "spec_title": title,
                    "kind": it["kind"], "merge_branch": it["merge_branch"], "label": it["label"],
                    "project": it["project"], "state": state,
                    "merged_sha": close.get("merged_sha"), "pr_url": close.get("pr_url"),
                    "cost_usd": it["cost_usd"],
                    "duration_s": (_age(st.get("created_at"), now) or 0) - age,
                    "finished_age_s": age})
            continue
        if state == "WAITING":
            waiting.append({**it, "spec_title": title,
                            "resume_to": (st.get("waiting") or {}).get("resume_to"),
                            "waiting_s": _age((st.get("waiting") or {}).get("since"), now)})
            continue
        if state == "QUEUED":
            backlog.append({"position": 0, "priority": it["priority"], "ready": True,
                            "blocked_by": [], **(it.get("backlog") or {}),
                            "campaign": it["campaign"], "spec_id": it["spec_id"],
                            "kind": it["kind"], "merge_branch": it["merge_branch"],
                            "label": it["label"],
                            "spec_title": title, "project": it["project"], "age_s": it["age_s"],
                            "size": it["size"], "tone": it["tone"], "profile": it["profile"],
                            "profile_source": it["profile_source"]})
            continue
        ledger = store.read_ledger(cfg, it["campaign"])
        if state == "PARKED" or it["park_requested"]:
            park = dict(it.get("park") or {})
            parked_at = park.get("parked_at")
            if state != "PARKED":
                # SPEC-0222: a pending request is the conductor's, and its detail is the decision
                req = st.get("park_requested") or {}
                parked_at = req.get("at")
                park = {"reason": None, "owner": "conductor", "decided": True, "overdue": False,
                        "decision": {"text": f"park requested: {req.get('detail') or req.get('reason')}",
                                     "ts": req.get("at"), "by": req.get("by") or "human"},
                        "parked_at": parked_at}
            row = {**it, "spec_title": title, "park": park or None,
                   "park_age_s": _age(parked_at, now), "last_verdict": _verdict_summary(st)}
            (needs_you if park.get("owner") == "human" else conductor).append(row)
            if state == "PARKED":
                continue
        active.append({**it, "spec_title": title,
                       "phase_index": PHASES.index(state) if state in PHASES else None,
                       "activity": _activity(st, ledger, now),
                       "last_gate": _gate_summary(st), "last_verdict": _verdict_summary(st),
                       "recent_events": ledger[-3:]})
    conductor.sort(key=lambda c: (not (c["park"] or {}).get("overdue"),
                                  -(c.get("park_age_s") or 0)))
    needs_you.sort(key=lambda c: -(c.get("park_age_s") or 0))
    attention = needs_you + conductor
    backlog.sort(key=lambda b: b["position"])
    delivered.sort(key=lambda d: d["finished_age_s"])
    delivered = delivered[:recent]
    ready = sum(1 for b in backlog if b["ready"])
    by_project: dict = {}
    waiting.sort(key=lambda w: (w.get("priority") or 3, -(w.get("waiting_s") or 0)))
    for key, rows in (("active", active), ("needs_you", needs_you), ("conductor", conductor),
                      ("waiting", waiting), ("backlog", backlog), ("delivered", delivered)):
        for r in rows:
            c = by_project.setdefault(r.get("project") or "?", {"active": 0, "needs_you": 0,
                                                                 "conductor": 0, "waiting": 0,
                                                                 "backlog": 0, "delivered": 0})
            c[key] += 1
    return {
        "schema": "wt.queue.dashboard.v1", "generated_at": now.isoformat(timespec="seconds"),
        "project": project, "lease": base["lease"], "slots": base["slots"],
        "totals": {"active": len(active), "attention": len(attention), "waiting": len(waiting),
                   "needs_you": len(needs_you), "conductor_handling": len(conductor),
                   "conductor_overdue": sum(1 for c in conductor if c["park"].get("overdue")),
                   "backlog": len(backlog),
                   "ready": ready, "blocked": len(backlog) - ready, "delivered": len(delivered),
                   "spend_today_usd": round(spend_today, 4), "by_project": by_project},
        "active": active, "needs_you": needs_you, "conductor_handling": conductor,
        "attention": attention, "waiting": waiting, "backlog": backlog,
        "delivered": delivered,
    }


# ---- admission (wt queue add) --------------------------------------------------------------------

def plan_add(cfg, spec_id, *, project=None, profile=None, base=None, force=False,
             priority=PRIORITY_DEFAULT) -> dict:
    """Resolve everything `wt queue add` needs and return the plan (no writes). Raises
    ValueError with the fix when the spec is not admissible."""
    spec_id = (spec_id or "").strip()
    if not spec_id:
        raise ValueError("spec id is required")
    priority = check_priority(priority)
    outbound = SP._is_outbound_spec_id(spec_id)
    path = SP.resolve_spec_path(cfg, spec_id)
    fm, _ = split_frontmatter(path.read_text(encoding="utf-8"))
    fm = fm or {}
    status = str(fm.get("status") or "")
    if status != "accepted" and not (force and status == "in-progress"):
        raise ValueError(f"{spec_id} is {status or 'missing status'!r}; only `accepted` specs are "
                         f"queued (pass --force for in-progress)")
    if outbound:
        project = project or fm.get("target_project") or path.parent.name
    else:
        project = project or "Meta-Tools"
    pcfg = P.project_config(cfg, project)
    root = Path(pcfg["_root"])
    if outbound:
        target = fm.get("target_spec_path")
        if not target or not Path(target).is_file():
            raise ValueError(f"{spec_id}: portable spec not found at {target!r}; run "
                             f"`wt spec export {spec_id}` first")
        spec_path = Path(target).resolve()
    else:
        spec_path = path.resolve()
    try:
        rel = str(spec_path.relative_to(root.resolve()))
    except ValueError:
        raise ValueError(f"{spec_id}: spec file {spec_path} is not inside the project repo {root}")
    active = store.active_campaign_for_spec(cfg, spec_id)
    if active:
        raise ValueError(f"{spec_id} already has an active campaign {active['id']} "
                         f"({active.get('state')}); abandon it first")
    revision = None
    if force:                                                          # SPEC-0188
        review, review_doc = "forced", None
    else:
        review_doc = SR.check_admissible(cfg, path, spec_id)
        review = review_doc["admission"]
        if review == "after_revision":                                 # SPEC-0230
            revision = {"review_sha": review_doc.get("spec_sha256"),
                        "current_sha": review_doc["current_sha"],
                        "findings": review_doc.get("findings") or []}
    pick = select_profile(cfg, flag=profile, review=review_doc)         # SPEC-0219
    prof = resolve_profile(cfg, pick["profile"])
    base = base or (pcfg.get("merge") or {}).get("base") or pcfg.get("base") or "main"
    cid = store.new_campaign_id(cfg, spec_id)
    worktree = P.worktree_root(pcfg, root, project) / cid
    return {
        "campaign": cid, "spec_id": spec_id, "project": project, "profile": prof["name"],
        "priority": priority, "force": bool(force), "review": review, "revision": revision, "base": base,
        "repo_root": str(root), "spec_rel_path": rel, "spec_status": status,
        "internal": not outbound, "worktree": str(worktree), "branch": f"wt/{cid}",
        "cap_usd": prof.get("campaign_cap_usd"), "loop_max": prof.get("loop_max"),
        "slots": prof.get("slots"), "merge_policy": (pcfg.get("merge") or {}).get("policy"),
        "size": pick["size"], "tone": pick["tone"],
        "sizing_rationale": (review_doc or {}).get("rationale") if pick["size"] else None,
        "profile_source": pick["source"], "profile_reason": pick["reason"],
        "review_sha256": (review_doc or {}).get("spec_sha256"),
    }


def apply_add(cfg, plan: dict, *, by="human") -> dict:
    """Create the campaign in `QUEUED` from a `plan_add` plan. Re-checks the active-campaign
    guard against live state before writing."""
    state = {
        "schema": "wt.campaign.v1", "id": plan["campaign"], "kind": "implement",
        "spec_id": plan["spec_id"], "project": plan["project"], "profile": plan["profile"],
        "state": "QUEUED", "prev_state": None, "created_at": store.now_iso(),
        "priority": check_priority(plan.get("priority", PRIORITY_DEFAULT)),
        "force": bool(plan.get("force")), "backlog": None,
        "repo": {"root": plan["repo_root"], "base": plan["base"], "base_sha_at_open": None,
                 "branch": plan["branch"], "worktree": plan["worktree"], "head": None},
        "spec": {"rel_path": plan["spec_rel_path"], "internal": plan["internal"],
                 "sha256_at_open": None},
        "counters": {"loops": 0, "runs": 0, "gate_runs": 0, "gate_retries": 0,
                     "triage_retries": 0, "blocked_streak": 0, "close_attempts": 0, "parks": 0},
        "cost": {"total_usd": 0.0, "cap_usd": plan.get("cap_usd"), "by_persona": {}},
        "last_gate": None, "last_verdict": None,
        "pending_brief": None, "park": None, "close": None,
        "sizing": {"size": plan.get("size"), "tone": plan.get("tone"),
                   "rationale": plan.get("sizing_rationale"),
                   "source": plan.get("profile_source"), "reason": plan.get("profile_reason"),
                   "review_sha256": plan.get("review_sha256")},
    }
    revision = plan.get("revision")
    if revision:                                                       # SPEC-0230
        state["pending_brief"] = SR.revision_brief(
            {"spec_sha256": revision["review_sha"], "findings": revision["findings"]})
    with store.add_lock(cfg):                                          # SPEC-0209
        active = store.active_campaign_for_spec(cfg, plan["spec_id"])
        if active:
            raise ValueError(f"{plan['spec_id']} already has an active campaign {active['id']}")
        store.create_campaign(cfg, state, by=by)
    if revision:
        store.append_event(cfg, plan["campaign"], "admitted_after_revision", revision, by=by)
    return state


def _campaign_on_branch(cfg, project, branch) -> dict | None:
    """The non-terminal campaign of `project` whose branch is `branch`, or None."""
    for st in store.list_campaigns(cfg):
        if st.get("project") == project and (st.get("repo") or {}).get("branch") == branch:
            return st
    return None


def plan_merge(cfg, branch, *, project=None, worktree=None, profile=None,
               priority=MERGE_PRIORITY_DEFAULT, note=None, requested_by="human") -> dict:
    """SPEC-0233: resolve everything `wt queue merge` needs and return the plan (no writes).
    Raises ValueError naming the fix when the branch cannot be queued for a merge."""
    branch = (branch or "").strip()
    if not branch:
        raise ValueError("branch is required")
    priority = check_priority(priority)
    project = project or P.find_project_for_cwd(cfg) or "Meta-Tools"
    pcfg = P.project_config(cfg, project)
    root = Path(pcfg["_root"])
    policy = (pcfg.get("merge") or {}).get("policy")
    if policy != "merge":
        raise ValueError(f"{project} merges with policy {policy!r}; `wt queue merge` needs "
                         f"merge.policy: merge")
    base = (pcfg.get("merge") or {}).get("base") or pcfg.get("base") or "main"
    if not git.branch_exists(root, branch):
        raise ValueError(f"branch {branch!r} does not exist in {root}; it must be a local branch")
    if branch == base:
        raise ValueError(f"{branch!r} is the base branch; nothing to merge")
    if git.current_branch(root) == branch:
        raise ValueError(f"the main checkout {root} is on {branch!r}; switch it to {base} first")
    ahead = int(git.git(root, "rev-list", "--count", f"{base}..{branch}").stdout.strip() or 0)
    if ahead < 1:
        raise ValueError(f"nothing to merge: {branch!r} is not ahead of {base}")
    active = _campaign_on_branch(cfg, project, branch)
    if active:
        raise ValueError(f"{branch!r} already has an active campaign {active['id']} "
                         f"({active.get('state')}); abandon it first")
    existing = git.worktree_for_branch(root, branch)
    cid = store.new_campaign_id(cfg, "MERGE")
    if worktree is not None:
        if existing is None or Path(worktree).resolve() != existing:
            raise ValueError(f"--worktree {worktree} is not the worktree of {branch!r} "
                             f"({existing or 'none'}); drop --worktree to let wt choose")
    if existing is not None:
        if not git.is_clean(existing, untracked=True):
            raise ValueError(f"worktree {existing} has uncommitted or untracked changes; "
                             f"commit or stash them before queueing the merge")
        wt_path, own = existing, False
    else:
        wt_path, own = P.worktree_root(pcfg, root, project) / cid, True
    prof = resolve_profile(cfg, profile)
    return {
        "campaign": cid, "kind": "merge", "branch": branch, "project": project,
        "profile": prof["name"], "priority": priority, "base": base, "repo_root": str(root),
        "worktree": str(wt_path), "own_worktree": own,
        "head_at_request": git.rev_parse(root, branch), "ahead": ahead,
        "requested_by": requested_by, "note": note, "cap_usd": prof.get("campaign_cap_usd"),
        "slots": prof.get("slots"), "merge_policy": policy,
    }


def apply_merge(cfg, plan: dict, *, by="human") -> dict:
    """Create the merge campaign in `QUEUED` from a `plan_merge` plan. Re-checks the
    one-campaign-per-branch guard against live state before writing."""
    state = {
        "schema": "wt.campaign.v1", "id": plan["campaign"], "kind": "merge",
        "spec_id": None, "project": plan["project"], "profile": plan["profile"],
        "state": "QUEUED", "prev_state": None, "created_at": store.now_iso(),
        "priority": check_priority(plan.get("priority", MERGE_PRIORITY_DEFAULT)),
        "force": False, "backlog": None,
        "repo": {"root": plan["repo_root"], "base": plan["base"], "base_sha_at_open": None,
                 "branch": plan["branch"], "worktree": plan["worktree"], "head": None},
        "spec": None,
        "merge": {"branch": plan["branch"], "requested_by": plan.get("requested_by") or by,
                  "note": plan.get("note"), "head_at_request": plan["head_at_request"],
                  "own_worktree": bool(plan["own_worktree"]), "docs_only": False},
        "counters": {"loops": 0, "runs": 0, "gate_runs": 0, "gate_retries": 0,
                     "triage_retries": 0, "blocked_streak": 0, "close_attempts": 0, "parks": 0},
        "cost": {"total_usd": 0.0, "cap_usd": plan.get("cap_usd"), "by_persona": {}},
        "last_gate": None, "last_verdict": None,
        "pending_brief": None, "park": None, "close": None,
    }
    with store.add_lock(cfg):
        active = _campaign_on_branch(cfg, plan["project"], plan["branch"])
        if active:
            raise ValueError(f"{plan['branch']!r} already has an active campaign {active['id']}")
        store.create_campaign(cfg, state, by=by)
    return state


_IDEA_ID_RE = re.compile(r"^IDEA-\d+$")


def _open_idea_campaign(cfg, idea_id) -> dict | None:
    """The non-terminal `kind: idea` campaign for `idea_id`, or None."""
    for st in store.list_campaigns(cfg):
        if st.get("kind") == "idea" and _idea_id_of(st) == idea_id:
            return st
    return None


def plan_add_idea(cfg, idea_id, *, project=None, profile=None, base=None,
                  priority=PRIORITY_DEFAULT) -> dict:
    """`plan_add` for an idea (SPEC-0209): no writes. Refusals, first match wins: unknown idea,
    idea with a `:SPEC:`, closed idea, open campaign for the idea, unloadable project."""
    idea_id = (idea_id or "").strip()
    priority = check_priority(priority)
    task = _idea_task(cfg, idea_id)
    if task is None:
        raise ValueError(f"unknown idea {idea_id}")
    spec = (task.properties.get("SPEC") or "").strip()
    if spec:
        raise ValueError(f"{idea_id} already has {spec}; queue that spec instead: "
                         f"wt queue add {spec}")
    if not idea_is_open(task):
        raise ValueError(f"{idea_id} is {task.state}; only open ideas (IDEA, INCUBATE, or "
                         f"SPECCED without a :SPEC:) are queued")
    active = _open_idea_campaign(cfg, idea_id)
    if active:
        raise ValueError(f"{idea_id} already has an active campaign {active['id']} "
                         f"({active.get('state')}); abandon it first")
    project = project or (task.properties.get("PROJECT") or "").strip() or "Meta-Tools"
    pcfg = P.project_config(cfg, project)
    root = Path(pcfg["_root"])
    prof = resolve_profile(cfg, profile)
    base = base or (pcfg.get("merge") or {}).get("base") or pcfg.get("base") or "main"
    cid = store.new_campaign_id(cfg, idea_id)
    worktree = P.worktree_root(pcfg, root, project) / cid
    return {
        "kind": "idea", "idea_id": idea_id, "campaign": cid, "project": project,
        "profile": prof["name"], "priority": priority, "base": base, "repo_root": str(root),
        "worktree": str(worktree), "branch": f"wt/{cid}",
        "cap_usd": prof.get("campaign_cap_usd"), "loop_max": prof.get("loop_max"),
        "slots": prof.get("slots"), "merge_policy": (pcfg.get("merge") or {}).get("policy"),
    }


def apply_add_idea(cfg, plan: dict, *, by="human") -> dict:
    """Create the idea campaign in `QUEUED` from a `plan_add_idea` plan and link the idea
    (`:CAMPAIGN:`). The duplicate re-check and the create run under the queue-wide add lock."""
    from .. import explore as EX
    from .. import org_write as OW
    idea_id = plan["idea_id"]
    with store.add_lock(cfg):
        active = _open_idea_campaign(cfg, idea_id)
        if active:
            raise ValueError(f"{idea_id} already has an active campaign {active['id']} "
                             f"({active.get('state')}); abandon it first")
        task = _idea_task(cfg, idea_id)
        if task is None:
            raise ValueError(f"unknown idea {idea_id}")
        idea = EX.idea_show_payload(cfg, task)
        idea["epic"] = task.properties.get("EPIC")
        idea["state_before"] = task.state
        state = {
            "schema": "wt.campaign.v1", "id": plan["campaign"], "kind": "idea",
            "spec_id": None, "idea": idea, "project": plan["project"],
            "profile": plan["profile"], "state": "QUEUED", "prev_state": None,
            "created_at": store.now_iso(),
            "priority": check_priority(plan.get("priority", PRIORITY_DEFAULT)),
            "force": False, "backlog": None,
            "repo": {"root": plan["repo_root"], "base": plan["base"], "base_sha_at_open": None,
                     "branch": plan["branch"], "worktree": plan["worktree"], "head": None},
            "spec": {"id": None, "rel_path": None, "internal": True},
            "counters": {"loops": 0, "runs": 0, "gate_runs": 0, "gate_retries": 0,
                         "triage_retries": 0, "blocked_streak": 0, "close_attempts": 0,
                         "parks": 0},
            "cost": {"total_usd": 0.0, "cap_usd": plan.get("cap_usd"), "by_persona": {}},
            "last_gate": None, "last_verdict": None,
            "pending_brief": None, "park": None, "close": None,
        }
        store.create_campaign(cfg, state, by=by)
        OW.set_property(cfg, task, "CAMPAIGN", plan["campaign"])
    return state


# ---- human commands ------------------------------------------------------------------------------

def plan_prioritize(cfg, campaign, priority) -> dict:
    priority = check_priority(priority)
    st = store.read_state(cfg, campaign)
    if st.get("state") not in ("QUEUED", "WAITING", "PARKED"):
        raise ValueError(f"{campaign} is {st.get('state')}; only QUEUED, WAITING or PARKED "
                         f"campaigns can be re-prioritized")
    return {"campaign": campaign, "state": st.get("state"), "from": _priority(st),
            "to": priority}


def apply_prioritize(cfg, plan: dict) -> dict:
    cid = plan["campaign"]
    with store.item_lock(cfg, cid):
        st = store.read_state(cfg, cid)
        if st.get("state") not in ("QUEUED", "WAITING", "PARKED"):
            raise ValueError(f"{cid} is {st.get('state')}; only QUEUED, WAITING or PARKED "
                             f"campaigns can be re-prioritized")
        ev = ("priority", {"from": _priority(st), "to": plan["to"]})
        st["priority"] = plan["to"]
        return _transition(cfg, st, st["state"], "prioritized", by="human", extra_events=[ev])


def plan_park(cfg, campaign, reason) -> dict:
    st = store.read_state(cfg, campaign)
    s = st.get("state")
    if s in TERMINAL_STATES or s == "PARKED":
        raise ValueError(f"{campaign} is {s}; nothing to park")
    running = L.is_held(cfg) and s in ACTIVE_STATES
    return {"campaign": campaign, "state": s, "reason": reason,
            "mode": "deferred" if running else "immediate",
            "note": ("a loop holds the lease: the running step finishes and is recorded, then "
                     "the park applies" if running else "no loop is running: parks now"),
            "resume_to": ((st.get("waiting") or {}).get("resume_to") or "IMPLEMENTING")
            if s == "WAITING" else (_resume_target(s) if s != "QUEUED" else "QUEUED")}


def apply_park(cfg, plan: dict) -> dict:
    cid = plan["campaign"]
    with store.item_lock(cfg, cid):
        st = store.read_state(cfg, cid)
        if st.get("state") in TERMINAL_STATES or st.get("state") == "PARKED":
            raise ValueError(f"{cid} is {st.get('state')}; nothing to park")
        if plan["mode"] == "deferred":
            st["park_requested"] = {"reason": "human", "detail": plan["reason"],
                                    "at": store.now_iso()}
            store.append_event(cfg, cid, "note", {"text": f"park requested: {plan['reason']}",
                                                  "author": "human"}, by="human")
            store.write_state(cfg, cid, st)
            return st
        return _park(cfg, st, "human", plan["reason"], plan["resume_to"], by="human")


def plan_resume(cfg, campaign, *, to=None, note=None, raise_cap=None, reset_loops=False) -> dict:
    st = store.read_state(cfg, campaign)
    if st.get("state") != "PARKED":
        raise ValueError(f"{campaign} is {st.get('state')}, not PARKED")
    park = st.get("park") or {}
    target = to or park.get("resume_to") or "IMPLEMENTING"
    if st.get("kind") == "merge" and target not in ("CLOSING", "QUEUED"):
        raise ValueError(f"a merge campaign resumes to CLOSING or QUEUED, not {target!r}")
    if target not in RESUMABLE_STATES:
        raise ValueError(f"cannot resume to {target!r}; one of {', '.join(RESUMABLE_STATES)}")
    waits = target in ACTIVE_STATES and _active_count(cfg) >= slots_for(cfg)
    return {"campaign": campaign, "from": "PARKED", "to": target, "note": note,
            "raise_cap": raise_cap, "reset_loops": bool(reset_loops),
            "park": park, "cap_usd": (st.get("cost") or {}).get("cap_usd"),
            "waits_for_slot": waits}


def apply_resume(cfg, plan: dict) -> dict:
    cid = plan["campaign"]
    with store.item_lock(cfg, cid):
        st = store.read_state(cfg, cid)
        if st.get("state") != "PARKED":
            raise ValueError(f"{cid} is {st.get('state')}, not PARKED")
        if plan.get("note"):
            brief = st.get("pending_brief") or {"source": "human", "fix_brief": None,
                                                "must_fix": [], "human_notes": []}
            brief.setdefault("human_notes", []).append(plan["note"])
            st["pending_brief"] = brief
        if plan.get("raise_cap") is not None:
            _cost(st)["cap_usd"] = float(plan["raise_cap"])
        if (st.get("park") or {}).get("reason") == "spec_write_failed":
            _reset_spec_write(cfg, st)
        if plan.get("reset_loops"):
            c = _counters(st)
            c["loops"] = 0
            c["blocked_streak"] = 0
            c["triage_retries"] = 0
            c["gate_retries"] = 0
        st["park"] = None
        st.pop("park_requested", None)
        ev = ("resume", {"to": plan["to"], "note": plan.get("note"),
                         "cap_usd": plan.get("raise_cap"), "loops_reset": bool(plan.get("reset_loops"))})
        return _enter_slot_or_wait(cfg, st, plan["to"], "resumed", by="human", extra_events=[ev])


def _reset_spec_write(cfg, st: dict) -> None:
    """Resuming `spec_write_failed` (SPEC-0215): a fresh retry budget and a fresh idea snapshot
    (its text may have been edited meanwhile), keeping `state_before`."""
    _counters(st)["spec_write_retries"] = 0
    old = st.get("idea") or {}
    task = _idea_task(cfg, old.get("id")) if st.get("kind") == "idea" else None
    if task is not None:
        from .. import explore as EX
        fresh = EX.idea_show_payload(cfg, task)
        fresh["epic"] = task.properties.get("EPIC")
        fresh["state_before"] = old.get("state_before")
        st["idea"] = fresh


def plan_abandon(cfg, campaign, *, keep_worktree=False) -> dict:
    st = store.read_state(cfg, campaign)
    if st.get("state") not in ("PARKED", "QUEUED", "WAITING"):
        raise ValueError(f"{campaign} is {st.get('state')}; only PARKED, QUEUED or WAITING "
                         f"campaigns can be abandoned (park it first)")
    repo = st.get("repo") or {}
    wt_path = repo.get("worktree")
    present = bool(wt_path and os.path.isdir(wt_path))
    # SPEC-0233: a worktree wt did not create belongs to the requester and is never removed
    adopted = st.get("kind") == "merge" and not (st.get("merge") or {}).get("own_worktree")
    return {"campaign": campaign, "from": st.get("state"), "worktree": wt_path,
            "worktree_present": present,
            "remove_worktree": present and not keep_worktree and not adopted,
            "branch": repo.get("branch")}


def _unlink_idea(cfg, st: dict) -> list:
    """Abandoning an idea campaign (SPEC-0209): clear the idea's `:CAMPAIGN:` (and its `:SPEC:`
    when that is this campaign's spec), restore its state when nobody moved it meanwhile, and
    release the campaign's spec-number reservations. Returns ledger events."""
    from .. import org_write as OW
    from .. import spec_numbers as SN
    cid, idea = st["id"], st.get("idea") or {}
    spec_id = (st.get("spec") or {}).get("id")
    events = []
    task = _idea_task(cfg, idea.get("id"))
    if task is not None:
        cur, linked = task.state, (task.properties.get("SPEC") or "").strip()
        restore = cur == idea.get("state_before") or (cur == "SPECCED" and linked == spec_id)
        if task.properties.get("CAMPAIGN"):
            OW.clear_property(cfg, task, "CAMPAIGN")
            task = _idea_task(cfg, idea.get("id"))
        if spec_id and linked == spec_id:
            OW.clear_property(cfg, task, "SPEC")
            task = _idea_task(cfg, idea.get("id"))
        if restore and idea.get("state_before") and task.state != idea["state_before"]:
            OW.set_state(cfg, task, idea["state_before"])
        elif not restore:
            events.append(("note", {"text": f"idea state kept: {cur}", "author": "queue"}))
    specs_dir = Path((st.get("repo") or {}).get("root") or "") / "docs" / "specs"
    for rec in SN.list_reservations(cfg, specs_dir=specs_dir):
        if rec.get("campaign") == cid:
            try:
                SN.release(cfg, rec["number"], specs_dir=specs_dir)
            except ValueError:
                pass
    return events


def apply_abandon(cfg, plan: dict) -> dict:
    cid = plan["campaign"]
    with store.item_lock(cfg, cid):
        st = store.read_state(cfg, cid)
        if st.get("state") not in ("PARKED", "QUEUED", "WAITING"):
            raise ValueError(f"{cid} is {st.get('state')}; only PARKED, QUEUED or WAITING "
                             f"campaigns can be abandoned")
        events = []
        if plan.get("remove_worktree"):
            try:
                git.worktree_remove((st.get("repo") or {}).get("root"), plan["worktree"], force=True)
            except GitError as e:
                events.append(("finding", {"persona": "human", "severity": "minor",
                                           "gate": "cleanup", "title": "worktree removal failed",
                                           "detail": e.tail, "files": []}))
        st["park"] = None
        st.pop("waiting", None)
        st.pop("park_requested", None)
        if st.get("kind") == "idea":
            events += _unlink_idea(cfg, st)
        events.append(("close", {"mode": "abandoned", "merged_sha": None, "pr_url": None,
                                 "gates_seq": None, "worktree_removed": bool(plan.get("remove_worktree"))}))
        return _transition(cfg, st, "ABANDONED", "abandoned", by="human", extra_events=events)


def tick_row_text(row: dict) -> str:
    extra = f" run {row['run_id']}" if row.get("run_id") else ""
    cost = ""
    if row.get("cost_usd") is not None:
        cost = f" ~${row['cost_usd']:.2f} est." if row.get("cost_estimated") else f" ${row['cost_usd']:.2f}"
    return f"{row['campaign']}: {row['from']} → {row['to']} ({row['action']}; {row['reason']}){extra}{cost}"

