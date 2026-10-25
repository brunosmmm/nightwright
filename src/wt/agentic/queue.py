"""Queue state machine and loop (SPEC-0176): the engine of the agentic workflow.

`plan_step` is a pure function from campaign state (+ profile + project config) to the one
step the campaign needs next; `tick` executes those steps — agent steps (implement, gate,
triage, review) concurrently in a thread pool, repository steps (open, close) serially — and applies
every result on the main thread under the campaign lock, so `state.json` and `ledger.jsonl`
can never disagree: every transition goes through `_transition`, which records the event in
the same locked section that writes the state.

Invariant (SPEC-0169): Python decides every transition; personas only produce commits and one
structured verdict. Gates are truth; verdicts steer. Before every spawn the campaign cap is
checked pessimistically (`total + persona budget > cap` ⇒ PARKED). Humans act only on
`PARKED` items (`park` / `resume` / `abandon`)."""
from __future__ import annotations

import hashlib
import os
import signal
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from .. import specs as SP
from ..specmeta import split_frontmatter
from . import close as CL
from . import gates as G
from . import gitops as git
from . import lease as L
from . import personas as PS
from . import project as P
from . import spec_review as SR
from . import schemas as SC
from . import store
from .gitops import GitError
from .profiles import persona_settings, resolve_profile
from .runner import select_runner

STATES = ("QUEUED", "WAITING", "OPENING", "IMPLEMENTING", "GATING", "TRIAGING", "REVIEWING",
          "CLOSING", "PARKED", "MERGED", "HANDED_OFF", "ABANDONED")
# Slots are for campaigns that are open and delivering (ACTIVE_STATES). QUEUED = the backlog,
# never started. WAITING (SPEC-0200) = started (worktree, commits, counters, brief) and ready to
# continue, but no slot was free; it takes the next free slot ahead of the backlog and resumes
# at `waiting.resume_to` without re-opening. PARKED = needs a decision; holds no slot.
TERMINAL_STATES = store.TERMINAL_STATES
ACTIVE_STATES = frozenset({"OPENING", "IMPLEMENTING", "GATING", "TRIAGING", "REVIEWING",
                           "CLOSING"})
RESUMABLE_STATES = ("IMPLEMENTING", "GATING", "REVIEWING", "CLOSING", "QUEUED")
PARK_REASONS = frozenset({
    "cost_cap", "loop_exhausted", "implementer_blocked", "triage_escalated", "open_failed",
    "rebase_conflict", "main_dirty", "main_not_on_base", "base_moving", "gh_failed", "human",
    "review_escalated", "config_invalid",
})
# How many times in a row the implementer may report `blocked` before the campaign parks.
BLOCKED_STREAK_MAX = 2
# Malformed triage verdicts tolerated per loop before the triage is treated as `escalate`.
TRIAGE_RETRY_MAX = 1
# `retry_gate` dispositions honored per loop before they are treated as `fix`.
GATE_RETRY_MAX = 1
# Malformed conformance-review verdicts tolerated per loop before the review escalates.
REVIEW_RETRY_MAX = 1
_STOP_GRACE_S = 30
PRIORITY_MIN, PRIORITY_MAX, PRIORITY_DEFAULT = 1, 5, 3
# A dependency counts as satisfied once its spec reached one of these in the base checkout.
DEPENDENCY_SATISFIED = frozenset({"done", "superseded"})


# ---- pure planning ---------------------------------------------------------------------

@dataclass(frozen=True)
class Step:
    """What a campaign needs next. `kind` ∈ open | implement | gate | triage | review | close |
    park | none; `park` steps carry `reason` / `detail` / `resume_to`."""
    kind: str
    persona: str | None = None
    reason: str | None = None
    detail: str | None = None
    resume_to: str | None = None


def _cap_blocks(state: dict, profile: dict, persona: str) -> str | None:
    """Pessimistic cap check: a run may spend up to its budget, so never start one that could
    cross the campaign cap. Returns the park detail or None."""
    cost = state.get("cost") or {}
    cap = cost.get("cap_usd")
    if cap is None:
        return None
    budget = float(persona_settings(profile, persona).get("max_budget_usd") or 0.0)
    total = float(cost.get("total_usd") or 0.0)
    if total + budget > float(cap):
        return (f"spent ${total:.2f}; a {persona} run may cost ${budget:.2f}; "
                f"cap ${float(cap):.2f}")
    return None


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
    if st == "GATING":
        return Step("gate")
    if st == "CLOSING":
        return Step("close")
    persona, kind = {"IMPLEMENTING": ("implementer", "implement"),
                     "REVIEWING": ("conformance_reviewer", "review")}.get(
                         st, ("gate_triager", "triage"))
    blocked = _cap_blocks(state, profile, persona)
    if blocked:
        return Step("park", persona=persona, reason="cost_cap", detail=blocked, resume_to=st)
    return Step(kind, persona=persona)


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


def decide_review(state: dict, profile: dict, verdict: dict | None, verdict_ok: bool) -> Step:
    """Pure: the transition a conformance-reviewer verdict implies (SPEC-0187).

    - malformed / missing verdict: one retry per loop (stay in REVIEWING), then
      PARKED(review_escalated);
    - `pass` with no `must_fix` finding ⇒ CLOSING;
    - `fix`, or any `must_fix` finding even under `pass`: loops += 1; beyond `loop_max` ⇒
      loop_exhausted, else back to IMPLEMENTING."""
    counters = state.get("counters") or {}
    loop_max = int(profile.get("loop_max") or 3)
    if not verdict_ok or not isinstance(verdict, dict):
        if int(counters.get("review_retries", 0)) < REVIEW_RETRY_MAX:
            return Step("none", reason="review_retry")
        return Step("park", reason="review_escalated",
                    detail="the conformance reviewer returned no usable verdict twice",
                    resume_to="REVIEWING")
    must_fix = [f for f in verdict.get("findings") or []
                if isinstance(f, dict) and f.get("severity") == "must_fix"]
    if verdict.get("verdict") == "pass" and not must_fix:
        return Step("close", reason="review_pass")
    loops = int(counters.get("loops", 0)) + 1
    if loops > loop_max:
        brief = (verdict.get("fix_brief") or "").strip()
        return Step("park", reason="loop_exhausted",
                    detail=f"{loops - 1}/{loop_max} implement loops used; the conformance "
                           f"review still asks for fixes: " + brief, resume_to="IMPLEMENTING")
    return Step("implement", reason="review_fix")


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


def backlog_readiness(cfg, state: dict) -> tuple[bool, list[str]]:
    """Can this queued campaign be admitted? Re-reads the spec and its `depends_on` from the
    project's base checkout (`repo.root`): the spec must still be `accepted` (`in-progress` for a
    `--force` add) and every dependency `done` or `superseded`. Unknown ids block."""
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
    charged = result.cost_usd if result.cost_usd is not None else budget
    cost["total_usd"] = round(float(cost["total_usd"]) + float(charged), 4)
    cost["by_persona"][persona] = round(float(cost["by_persona"].get(persona, 0.0))
                                        + float(charged), 4)
    events = [("run.end", {
        "persona": persona, "run_id": run_id, "exit_code": result.exit_code,
        "is_error": result.is_error, "timed_out": result.timed_out,
        "interrupted": interrupted, "duration_s": result.duration_s,
        "cost_usd": result.cost_usd, "charged_usd": charged, "usage": result.usage,
        "session_id": result.session_id, "verdict_ok": result.verdict_ok,
        "verdict": result.verdict,
        "result_tail": (result.result_text or "")[-400:] if not result.verdict_ok else "",
    }), ("cost", {"persona": persona, "delta_usd": charged, "total_usd": cost["total_usd"],
                  "cap_usd": cost.get("cap_usd")})]
    if result.cost_usd is None and not interrupted:
        events.append(("finding", {"persona": persona, "severity": "info", "gate": None,
                                   "title": "run reported no cost; charged its budget",
                                   "detail": f"${budget:.2f} charged pessimistically",
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
    for item in verdict.get("findings") or []:        # conformance reviewer (SPEC-0187)
        if isinstance(item, dict):
            events.append(("finding", {"persona": persona, "severity": item.get("severity"),
                                       "gate": None, "ac": item.get("ac"),
                                       "title": item.get("title"), "detail": item.get("detail"),
                                       "files": list(item.get("files") or [])}))
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
    error: str | None = None
    interrupted: bool = False
    events_before: list = field(default_factory=list)
    config_error: bool = False


class _Ctx:
    """Per-tick context: cfg, resolved profiles / project configs, the runner, the stop flag."""

    def __init__(self, cfg, *, stop: threading.Event | None = None, runner=None):
        self.cfg = cfg
        self.stop = stop or threading.Event()
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


def _do_open(ctx: _Ctx, state: dict) -> StepResult:
    """Create the worktree + branch, run the project's setup commands, record base sha and
    spec hash. Serial (touches the main repository)."""
    res = StepResult(state["id"], Step("open"), "OPENING")
    repo = state.setdefault("repo", {})
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


def _prepare_run(ctx: _Ctx, state: dict, persona: str):
    """Main thread: mint the run id, build the request, append `run.start`, bump `runs` and
    write the state. Returns (run_id, request, budget)."""
    cfg = ctx.cfg
    cid = state["id"]
    profile = ctx.profile(state)
    pcfg = ctx.project(state)
    counters = _counters(state)
    run_no = counters["runs"] + 1
    run_id = f"{run_no:03d}-{persona}"
    run_dir = store.runs_dir(cfg, cid) / run_id
    previous = store.read_ledger(cfg, cid, types=["run.end"], tail=2)
    req = PS.build_request(cfg, profile, pcfg, state, persona, run_dir=run_dir, previous=previous)
    budget = float(req.max_budget_usd)
    with store.item_lock(cfg, cid):
        counters["runs"] = run_no
        store.append_event(cfg, cid, "run.start", {
            "persona": persona, "run_id": run_id, "model": req.model, "effort": req.effort,
            "max_budget_usd": budget, "timeout_s": req.timeout_s,
            "permission_mode": req.permission_mode, "prompt_sha256": PS.prompt_sha256(req.prompt),
        })
        store.write_state(cfg, cid, state)
    return run_id, req, budget


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


def _do_gate(ctx: _Ctx, state: dict) -> StepResult:
    res = StepResult(state["id"], Step("gate"), "GATING")
    try:
        pcfg = ctx.project(state)
        res.gate_run = G.run_all(pcfg, state, ctx.cfg, seq=G.next_gate_seq(ctx.cfg, state["id"]))
    except (ValueError, OSError) as e:
        res.error = str(e)
    return res


def _do_close(ctx: _Ctx, state: dict) -> StepResult:
    res = StepResult(state["id"], Step("close"), "CLOSING")
    try:
        res.close_outcome = CL.close_campaign(ctx.cfg, state, ctx.project(state))
    except (GitError, ValueError, OSError) as e:
        res.error = getattr(e, "tail", None) or str(e)
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
                  "pending_brief", "close"):
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
        reason = {"open": "open_failed"}.get(kind, "open_failed")
        detail = f"{kind} step failed: {res.error}"
        resume = {"open": "QUEUED", "implement": "IMPLEMENTING", "gate": "GATING",
                  "triage": "GATING", "review": "REVIEWING"}.get(kind, "IMPLEMENTING")
        return finish("PARKED", reason, park={"reason": reason, "detail": detail,
                                               "resume_to": resume})

    if kind == "open":
        return finish("IMPLEMENTING", "opened")

    if kind in ("implement", "triage", "review"):
        persona = res.step.persona
        rr = res.run_result
        events += _record_run(state, persona, res.run_id, rr, res.budget,
                              interrupted=res.interrupted)
        row["cost_usd"] = rr.cost_usd if rr.cost_usd is not None else res.budget
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
        if kind == "review":
            return _apply_review(state, profile, counters, rr, res, finish)
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
        counters["close_attempts"] += 1
        if res.error:
            return finish("PARKED", "open_failed", park={
                "reason": "open_failed", "detail": f"close step failed: {res.error}",
                "resume_to": "CLOSING"})
        oc = res.close_outcome
        for etype, payload in oc.events:
            if etype == "park":
                continue                                   # re-emitted by _park below
            events.append((etype, payload))
            if etype == "gate":
                counters["gate_runs"] += 1
                state["last_gate"] = payload
            if etype == "commit" and payload.get("sha"):
                state.setdefault("repo", {})["head"] = payload["sha"]
        if oc.next_state in ("MERGED", "HANDED_OFF"):
            state["close"] = oc.close
            state["park"] = None
            return finish(oc.next_state, "closed")
        if oc.next_state == "TRIAGING":
            if oc.synthetic_gate is not None:
                state["last_gate"] = {"gate_seq": None, "ok": False, "started_at": store.now_iso(),
                                      "duration_s": 0.0, "results": [oc.synthetic_gate]}
                return finish("TRIAGING", "spec_dod")
            return finish("TRIAGING", "regate_failed")
        park = oc.park or {"reason": "open_failed", "detail": "close returned PARKED without a reason",
                           "resume_to": "CLOSING"}
        return finish("PARKED", park["reason"], park=park)

    return finish(res.from_state, "noop")


def _apply_review(state, profile, counters, rr, res, finish):
    """Route a conformance-reviewer result (SPEC-0187): record `last_review`, then follow
    `decide_review`. `finish` is `_apply`'s closure."""
    v = rr.verdict if isinstance(rr.verdict, dict) else {}
    findings = [f for f in v.get("findings") or [] if isinstance(f, dict)]
    if rr.verdict_ok:
        state["last_review"] = {
            "run": res.run_id, "verdict": v.get("verdict"), "score": v.get("score"),
            "findings": len(findings),
            "must_fix": sum(1 for f in findings if f.get("severity") == "must_fix"),
            "loop": counters["loops"]}
    decision = decide_review(state, profile, rr.verdict, rr.verdict_ok)
    if decision.kind == "none":                          # malformed verdict, retry allowed
        counters["review_retries"] += 1
        return finish("REVIEWING", "review_retry")
    if decision.kind == "park":
        return finish("PARKED", decision.reason, park={
            "reason": decision.reason, "detail": decision.detail,
            "resume_to": decision.resume_to})
    counters["review_retries"] = 0
    if decision.kind == "close":
        return finish("CLOSING", "review_pass")
    # fix
    counters["loops"] += 1
    counters["gate_retries"] = 0
    counters["triage_retries"] = 0
    notes = ((state.get("pending_brief") or {}).get("human_notes") or [])
    state["pending_brief"] = {
        "source": "conformance_reviewer", "fix_brief": v.get("fix_brief"),
        "must_fix": [{"ac": f.get("ac"), "title": f.get("title"), "detail": f.get("detail"),
                      "files": list(f.get("files") or [])}
                     for f in findings if f.get("severity") == "must_fix"],
        "human_notes": list(notes)}
    return finish("IMPLEMENTING", "review_fix")


# ---- tick / run ------------------------------------------------------------------------------

@dataclass
class TickReport:
    steps: list = field(default_factory=list)
    admitted: int = 0
    idle: bool = True
    errors: list = field(default_factory=list)

    def payload(self) -> dict:
        return {"schema": "wt.queue.tick.v1", "steps": self.steps, "admitted": self.admitted,
                "idle": self.idle, "errors": self.errors}


def slots_for(cfg, profile_name=None) -> int:
    try:
        return int(resolve_profile(cfg, profile_name).get("slots") or 1)
    except ValueError:
        return 1


def tick(cfg, *, stop: threading.Event | None = None, runner=None, admit=True) -> TickReport:
    """One bounded iteration: admit, open, plan, run agent steps concurrently, close serially,
    apply everything on this thread. The caller holds the lease."""
    ctx = _Ctx(cfg, stop=stop, runner=runner)
    report = TickReport()
    items = [s for s in store.list_campaigns(cfg) if s.get("state") != "PARKED"]
    report.idle = not items
    slots = slots_for(cfg)
    busy = [s for s in items if s.get("state") in ACTIVE_STATES]
    queued = [s for s in items if s.get("state") == "QUEUED"]

    # 0. campaigns parked on a bad project config resume once it loads (SPEC-0194) — into a
    #    free slot only, so a recovery never runs more campaigns than the profile allows
    for st in store.list_campaigns(cfg, state="PARKED"):
        park = st.get("park") or {}
        if park.get("reason") != "config_invalid":
            continue
        try:
            ctx.project(st)
        except ProjectConfigError:
            continue
        to = park.get("resume_to") or "QUEUED"
        frm = st.get("state")
        st["park"] = None
        _enter_slot_or_wait(cfg, st, to, "config_recovered", busy=len(busy), slots=slots)
        report.steps.append({"campaign": st["id"], "from": frm, "to": st["state"],
                             "action": "recover", "reason": "config_recovered",
                             "run_id": None, "cost_usd": None})
        report.idle = False
        if st["state"] in ACTIVE_STATES:
            busy.append(st)
        elif st["state"] == "QUEUED":
            queued.append(st)

    # 0b. started campaigns waiting for a slot go first (SPEC-0200): they continue where they
    #     stopped — no re-open — so their branch does not go stale behind new work
    waiting = sorted((s for s in items if s.get("state") == "WAITING"), key=admission_key)
    for st in waiting:
        if not admit or ctx.stop.is_set() or len(busy) >= slots:
            break
        if st.get("park_requested"):
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
        for st in pick_admissions(queued, len(busy), slots, lambda s: ready[s["id"]]):
            _transition(cfg, st, "OPENING", "admitted")
            report.admitted += 1
            admitted_ids.add(st["id"])
            report.steps.append(_apply(ctx, st, _do_open(ctx, st)))
            if st.get("state") in ACTIVE_STATES:
                busy.append(st)

    # 2. plan every item that was already active at the start of the tick
    pending_agent = []       # (state, step, run_id, req, budget) for the pool
    pending_close = []
    for st in busy:
        if st.get("state") not in ACTIVE_STATES or st["id"] in admitted_ids:
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
            pending_close.append(st)
            continue
        if step.kind == "gate":
            pending_agent.append((st, step, None, None, 0.0))
            continue
        if ctx.stop.is_set():
            continue
        try:
            run_id, req, budget = _prepare_run(ctx, st, step.persona)
        except (ValueError, OSError) as e:
            report.steps.append(_apply(ctx, st, StepResult(
                st["id"], step, st["state"], error=str(e))))
            continue
        pending_agent.append((st, step, run_id, req, budget))

    # 3. agent steps concurrently
    if pending_agent:
        with ThreadPoolExecutor(max_workers=max(1, slots)) as pool:
            futures = []
            for st, step, run_id, req, budget in pending_agent:
                if step.kind == "gate":
                    futures.append((st, pool.submit(_do_gate, ctx, st)))
                else:
                    futures.append((st, pool.submit(_do_persona, ctx, st, step, run_id, req, budget)))
            for st, fut in futures:
                try:
                    res = fut.result()
                except Exception as e:      # a bug in a step must park, not kill the loop
                    res = StepResult(st["id"], Step("implement"), st["state"], error=repr(e))
                    report.errors.append(f"{st['id']}: {e!r}")
                report.steps.append(_apply(ctx, st, res))

    # 4. close steps serially (they touch the main checkout)
    for st in pending_close:
        if ctx.stop.is_set():
            break
        report.steps.append(_apply(ctx, st, _do_close(ctx, st)))

    remaining = [s for s in store.list_campaigns(cfg) if s.get("state") != "PARKED"]
    report.idle = not remaining
    return report


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


# ---- code-change re-exec (SPEC-0194) ------------------------------------------------------------
# The runner imports wt from a checkout (an editable install of main). When a merge changes
# that code, a long-lived loop would keep running the old code — and reject config the new
# code accepts. Between ticks nothing is in flight, so the loop checks the checkout's HEAD there
# and re-executes itself, handing its lease over so the queue is never unowned.

_CODE_PATHS = ("src/wt", "pyproject.toml")
_EXEC = os.execve                      # tests replace this


def code_checkout() -> Path | None:
    """The git checkout this wt package is imported from, or None (not a checkout)."""
    root = Path(__file__).resolve().parents[3]
    return root if (root / ".git").exists() and (root / "src" / "wt").is_dir() else None


def _git_out(root, *args) -> str | None:
    try:
        r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                           timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def code_changes(root, since: str) -> tuple[str | None, list[str]]:
    """(current HEAD, wt code paths changed since `since`) for the checkout at `root`."""
    head = _git_out(root, "rev-parse", "HEAD")
    if not head or head == since:
        return head, []
    diff = _git_out(root, "diff", "--name-only", since, head, "--", *_CODE_PATHS)
    return head, [p for p in (diff or "").splitlines() if p]


class _heartbeat_while:
    """Heartbeat the lease from a daemon thread while a tick runs (SPEC-0204): a tick that
    runs gates or waits on persona runs can outlast the lease TTL, and an alive runner must
    never look stale. Beats every ttl/4 seconds (at least 1s)."""

    def __init__(self, lease, interval_s=None):
        self.lease = lease
        ttl = float((lease.info or {}).get("ttl_s") or 120)
        self.interval = interval_s if interval_s is not None else max(1.0, ttl / 4)
        self._stop = threading.Event()
        self._thread = None

    def _beat(self):
        while not self._stop.wait(self.interval):
            try:
                self.lease.heartbeat()
            except OSError:
                pass

    def __enter__(self):
        self._thread = threading.Thread(target=self._beat, name="wt-lease-heartbeat",
                                        daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(timeout=5)
        return False


def run(cfg, *, once=False, max_ticks=None, poll_interval_s=None, until_idle=False,
        on_tick=None, runner=None, argv=None, reexec=False, code_root=None) -> int:
    """The loop: take the lease, tick, heartbeat, sleep. SIGINT/SIGTERM stop admissions,
    forward SIGTERM to this process group's children (the `claude` runs) when we lead the
    group, let in-flight steps record themselves, and release the lease. Returns the number
    of ticks performed.

    With `reexec` (SPEC-0194), when a commit to the wt checkout (`code_root`, default the one
    this package is imported from) changes wt's code, the loop stops between ticks, journals
    `stop` with reason `code_changed`, and re-executes the same command keeping its lease."""
    agentic = cfg.get("agentic") or {}
    interval = float(poll_interval_s if poll_interval_s is not None
                     else agentic.get("poll_interval_s") or 20)
    stop = threading.Event()
    lease = L.acquire(cfg, argv or ["wt", "queue", "run"])
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
    journal(cfg, "start", argv=list(argv or ["wt", "queue", "run"]), interval_s=interval,
            code_sha=code_sha)
    try:
        while not stop.is_set():
            with _heartbeat_while(lease):
                report = tick(cfg, stop=stop, runner=runner)
            ticks += 1
            lease.heartbeat()
            if report.steps or report.admitted or report.errors:      # idle ticks stay silent
                journal(cfg, "tick", tick=ticks, steps=report.steps, admitted=report.admitted,
                        errors=report.errors)
            if on_tick:
                on_tick(report)
            if once or (max_ticks and ticks >= max_ticks) or (until_idle and report.idle):
                reason = "once" if once else ("max_ticks" if max_ticks and ticks >= max_ticks
                                              else "idle")
                break
            if code_sha:
                head, changed = code_changes(watch_root, code_sha)
                if changed:
                    reason = "code_changed"
                    restart = {"from_sha": code_sha, "to_sha": head, "paths": changed[:50]}
                    break
                code_sha = head or code_sha       # docs-only commits: keep running
            deadline = time.monotonic() + interval
            while time.monotonic() < deadline and not stop.is_set():
                time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))
    except BaseException as e:
        if not isinstance(e, (KeyboardInterrupt, SystemExit)):
            import traceback
            journal(cfg, "crash", ticks=ticks, error=f"{type(e).__name__}: {e}",
                    traceback=traceback.format_exc()[-4000:])
            reason = "crash"
        raise
    finally:
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


def queue_payload(cfg, *, include_terminal=False, project=None, state=None) -> dict:
    """`wt.queue.v1`: the lease, the slots and every in-flight item."""
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
        items.append({
            "campaign": r.get("id"), "spec_id": r.get("spec_id"), "project": r.get("project"),
            "profile": r.get("profile"), "state": r.get("state"),
            "loops": f"{counters.get('loops', 0)}/{loop_max if loop_max is not None else '?'}",
            "runs": counters.get("runs", 0),
            "cost_usd": round(float(cost.get("total_usd") or 0.0), 4),
            "cap_usd": cost.get("cap_usd"),
            "age_s": _age(r.get("created_at")), "updated_age_s": _age(r.get("updated_at")),
            "park": r.get("park"), "park_requested": bool(r.get("park_requested")),
            "worktree": repo.get("worktree"), "branch": repo.get("branch"),
            "priority": _priority(r), "waiting": r.get("waiting"),
        })
        b = backlog.get(r.get("id"))
        if b and r.get("state") == "QUEUED":
            items[-1]["backlog"] = {k: b[k] for k in ("position", "priority", "ready", "blocked_by")}
    return {"schema": "wt.queue.v1", "lease": L.lease_status(cfg),
            "slots": {"total": slots, "busy": busy}, "items": items}


# ---- dashboard (wt queue watch, SPEC-0185) -------------------------------------------------------

PHASES = ("OPENING", "IMPLEMENTING", "GATING", "TRIAGING", "REVIEWING", "CLOSING")
_TITLE_CACHE: dict = {}


def _spec_title(state: dict) -> str | None:
    """The spec's frontmatter title in the project repo (cached on path + mtime)."""
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
    base = queue_payload(cfg, include_terminal=True, project=project)
    active, attention, waiting, backlog, delivered = [], [], [], [], []
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
                            "spec_title": title, "project": it["project"], "age_s": it["age_s"]})
            continue
        ledger = store.read_ledger(cfg, it["campaign"])
        if state == "PARKED" or it["park_requested"]:
            park = dict(st.get("park") or {})
            parked_at = next((e.get("ts") for e in reversed(ledger) if e.get("type") == "park"),
                             None)
            attention.append({**it, "spec_title": title, "park": park or None,
                              "park_age_s": _age(parked_at, now),
                              "last_verdict": _verdict_summary(st)})
            if state == "PARKED":
                continue
        active.append({**it, "spec_title": title,
                       "phase_index": PHASES.index(state) if state in PHASES else None,
                       "activity": _activity(st, ledger, now),
                       "last_gate": _gate_summary(st), "last_verdict": _verdict_summary(st),
                       "recent_events": ledger[-3:]})
    backlog.sort(key=lambda b: b["position"])
    delivered.sort(key=lambda d: d["finished_age_s"])
    delivered = delivered[:recent]
    ready = sum(1 for b in backlog if b["ready"])
    by_project: dict = {}
    waiting.sort(key=lambda w: (w.get("priority") or 3, -(w.get("waiting_s") or 0)))
    for key, rows in (("active", active), ("attention", attention), ("waiting", waiting),
                      ("backlog", backlog), ("delivered", delivered)):
        for r in rows:
            c = by_project.setdefault(r.get("project") or "?", {"active": 0, "attention": 0,
                                                                 "waiting": 0, "backlog": 0,
                                                                 "delivered": 0})
            c[key] += 1
    return {
        "schema": "wt.queue.dashboard.v1", "generated_at": now.isoformat(timespec="seconds"),
        "project": project, "lease": base["lease"], "slots": base["slots"],
        "totals": {"active": len(active), "attention": len(attention), "waiting": len(waiting),
                   "backlog": len(backlog),
                   "ready": ready, "blocked": len(backlog) - ready, "delivered": len(delivered),
                   "spend_today_usd": round(spend_today, 4), "by_project": by_project},
        "active": active, "attention": attention, "waiting": waiting, "backlog": backlog,
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
    if force:                                                          # SPEC-0188
        review = "forced"
    else:
        SR.check_admissible(path, spec_id)
        review = "approved"
    prof = resolve_profile(cfg, profile)
    base = base or (pcfg.get("merge") or {}).get("base") or pcfg.get("base") or "main"
    cid = store.new_campaign_id(cfg, spec_id)
    worktree = P.worktree_root(pcfg, root, project) / cid
    return {
        "campaign": cid, "spec_id": spec_id, "project": project, "profile": prof["name"],
        "priority": priority, "force": bool(force), "review": review, "base": base, "repo_root": str(root), "spec_rel_path": rel, "spec_status": status,
        "internal": not outbound, "worktree": str(worktree), "branch": f"wt/{cid}",
        "cap_usd": prof.get("campaign_cap_usd"), "loop_max": prof.get("loop_max"),
        "slots": prof.get("slots"), "merge_policy": (pcfg.get("merge") or {}).get("policy"),
    }


def apply_add(cfg, plan: dict, *, by="human") -> dict:
    """Create the campaign in `QUEUED` from a `plan_add` plan. Re-checks the active-campaign
    guard against live state before writing."""
    active = store.active_campaign_for_spec(cfg, plan["spec_id"])
    if active:
        raise ValueError(f"{plan['spec_id']} already has an active campaign {active['id']}")
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
    }
    store.create_campaign(cfg, state, by=by)
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


def plan_abandon(cfg, campaign, *, keep_worktree=False) -> dict:
    st = store.read_state(cfg, campaign)
    if st.get("state") not in ("PARKED", "QUEUED", "WAITING"):
        raise ValueError(f"{campaign} is {st.get('state')}; only PARKED, QUEUED or WAITING "
                         f"campaigns can be abandoned (park it first)")
    repo = st.get("repo") or {}
    wt_path = repo.get("worktree")
    present = bool(wt_path and os.path.isdir(wt_path))
    return {"campaign": campaign, "from": st.get("state"), "worktree": wt_path,
            "worktree_present": present, "remove_worktree": present and not keep_worktree,
            "branch": repo.get("branch")}


def apply_abandon(cfg, plan: dict) -> dict:
    cid = plan["campaign"]
    with store.item_lock(cfg, cid):
        st = store.read_state(cfg, cid)
        if st.get("state") not in ("PARKED", "QUEUED"):
            raise ValueError(f"{cid} is {st.get('state')}; only PARKED or QUEUED campaigns can "
                             f"be abandoned")
        events = []
        if plan.get("remove_worktree"):
            try:
                git.worktree_remove((st.get("repo") or {}).get("root"), plan["worktree"], force=True)
            except GitError as e:
                events.append(("finding", {"persona": "human", "severity": "minor",
                                           "gate": "cleanup", "title": "worktree removal failed",
                                           "detail": e.tail, "files": []}))
        st["park"] = None
        st.pop("park_requested", None)
        events.append(("close", {"mode": "abandoned", "merged_sha": None, "pr_url": None,
                                 "gates_seq": None, "worktree_removed": bool(plan.get("remove_worktree"))}))
        return _transition(cfg, st, "ABANDONED", "abandoned", by="human", extra_events=events)


def tick_row_text(row: dict) -> str:
    extra = f" run {row['run_id']}" if row.get("run_id") else ""
    cost = f" ${row['cost_usd']:.2f}" if row.get("cost_usd") is not None else ""
    return f"{row['campaign']}: {row['from']} → {row['to']} ({row['action']}; {row['reason']}){extra}{cost}"

