"""Resumable implementer and reviewer sessions (SPEC-0556, phase 4 of SPEC-0552).

With `state.lean.sessions` pinned on (it needs `packet` and `collator`; SPEC-0553 pins the flags
at open), the implementer and the three builtin code reviewers keep one backend session per
campaign and persona. Each of their runs persists its session; a later run resumes the
persona's recorded session when the registry entry is still valid for this spawn, with a short
continuation prompt instead of the full template. Everything else runs fresh, as today.

**Registry** (`state.sessions`, updated by `queue._record_run` through `record`):
`{persona: {backend, session_id, host, model, tier, worktree, campaign, first_loop, last_loop,
context_tokens, session_cost_usd, run_id}}`. An entry is reused only when backend, model, tier,
worktree and campaign match, the backend is in `runner.SESSION_RESUME`, and `context_tokens`
is under `agentic.lean_session_max_tokens`; the host (`local` or the worker name) is checked
where the run is placed (`place`). A run that did not end with a conforming verdict drops its
persona's entry, so the next spawn is fresh.

**Fallback.** A resumed request carries the fresh prompt it replaced (`fresh_prompt`): today's
template plus the packet and the findings history. A resume that fails (`resume_failed`) is run
once more in the same step from that prompt, in a fresh session (`queue._rerun_locally`, a
`session_fallback` event); it never parks. A request placed on a host other than its
session's is turned into that fresh request before it starts (`place`).

Never resumed: triagers, the marshal, the collator, spec personas, the conflict resolver and
the hand-raised deep close pass (SPEC-0553), which also leaves the registry alone."""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from . import store

PERSONAS = ("implementer", "conformance_reviewer", "test_reviewer", "adversarial_reviewer")
REVIEWERS = PERSONAS[1:]
LOCAL = "local"
DEFAULT_MAX_TOKENS = 400_000
_HISTORY_MAX = 30                  # in-scope records listed per findings file


# ---- flags ----------------------------------------------------------------------------------

def on(state) -> bool:
    """`sessions` pinned on with its `packet` and `collator` dependencies (enforced at open by
    `tiers.effective_lean`; checked again so a hand-edited state cannot run it alone)."""
    from . import tiers as T
    lean = T.pinned(state)
    return all(lean.get(f) is True for f in ("sessions", "packet", "collator"))


def max_tokens(cfg) -> int:
    v = ((cfg or {}).get("agentic") or {}).get("lean_session_max_tokens")
    return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else DEFAULT_MAX_TOKENS


def managed(state, persona) -> bool:
    """This persona's runs keep a session in this campaign (the deep close pass never does)."""
    from . import tiers as T
    if not on(state) or persona not in PERSONAS:
        return False
    return not (persona == T.DEEP_PERSONA and T.deep_pass_pending(state))


def resumable(backend) -> bool:
    from . import runner as R
    return R.SESSION_RESUME.get(str(backend or "")) is True


def registry(state) -> dict:
    reg = (state or {}).get("sessions")
    return reg if isinstance(reg, dict) else {}


# ---- validity -------------------------------------------------------------------------------

def context_tokens(usage) -> int:
    """The context a run ended with: its last model call's input + cache read + cache creation
    when the usage lists the calls (claude's `iterations`), else the run's summed input + cache
    read (an upper bound: a multi-turn run reads the same prefix on every call)."""
    usage = usage if isinstance(usage, dict) else {}

    def n(d, *keys):
        total = 0
        for k in keys:
            try:
                total += int(d.get(k) or 0)
            except (TypeError, ValueError):
                pass
        return total
    its = usage.get("iterations")
    if isinstance(its, list) and its and isinstance(its[-1], dict):
        return n(its[-1], "input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    return n(usage, "input_tokens", "cache_read_input_tokens")


def check(cfg, state, persona, *, backend, model, tier) -> tuple:
    """`(entry, None)` when the persona's registry entry may be resumed by this spawn, else
    `(None, reason)`. The host is checked at placement (`place`)."""
    e = registry(state).get(persona)
    if not isinstance(e, dict) or not e.get("session_id"):
        return None, "no_session"
    if not resumable(backend):
        return None, "backend_not_resumable"
    want = {"backend": backend, "model": model, "tier": tier,
            "worktree": (state.get("repo") or {}).get("worktree"), "campaign": state.get("id")}
    for k, v in want.items():
        if e.get(k) != v:
            return None, f"{k}_changed"
    if int(e.get("context_tokens") or 0) >= max_tokens(cfg):
        return None, "context_limit"
    return e, None


# ---- prompts --------------------------------------------------------------------------------

def _latest(cfg, state, prefix: str) -> list:
    """`[(loop, path)]` of this campaign's `<prefix><N>.json` files, by loop."""
    cdir = store.campaign_dir(cfg, state["id"])
    out = []
    for p in cdir.glob(f"{prefix}*.json"):
        tail = p.name[len(prefix):-len(".json")]
        if tail.isdigit():
            out.append((int(tail), p))
    return sorted(out)


def _records(path: Path) -> list:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    recs = doc.get("collated") if isinstance(doc, dict) else None
    return [r for r in recs or [] if isinstance(r, dict)]


def history_note(cfg, state) -> str:
    """The findings history a fresh loop ≥ 2 run gets: the latest review packet and each
    `findings-loop-N.json` with its in-scope findings. Empty before the first review."""
    packets = _latest(cfg, state, "packet-loop-")
    files = _latest(cfg, state, "findings-loop-")
    if not files:                       # no review has been collated yet: loop 1
        return ""
    lines = ["## Findings history (this run starts a fresh session)", ""]
    if packets:
        lines.append(f"The latest review packet is `{packets[-1][1]}` (loop {packets[-1][0]}): "
                     "every finding of earlier loops with its status, the ACs and the gates.")
    for loop, path in files:
        recs = [r for r in _records(path) if r.get("scope") != "out"]
        lines += ["", f"- Loop {loop}: `{path}` ({len(recs)} in scope)"]
        for r in recs[:_HISTORY_MAX]:
            tie = r.get("tie") if r.get("tie") not in (None, "none") else None
            lines.append(f"  - {r.get('severity')}{f' [{tie}]' if tie else ''}: {r.get('title')}")
        if len(recs) > _HISTORY_MAX:
            lines.append(f"  - … {len(recs) - _HISTORY_MAX} more in the file")
    return "\n".join(lines)


IMPLEMENTER_CONTINUATION = """\
# Continue: implementer — campaign {{campaign}}, loop {{loop}} of {{loop_max}}

This continues your earlier session in this campaign. The worktree is `{{worktree}}` and the
spec is `{{spec_rel_path}}`; the rules of your first message still hold (the spec guard, the
tests to run, the status, ticks and ledger steps, the output object). Re-read only the files
you need: the review or the gate changed nothing you did not see, except what is below.

Review packet: {{packet_path}}. Collated findings of the last review: {{findings_path}}.

## Brief from the previous loop (in-scope findings only)

{{pending_brief}}

## Last gate results

{{gate_results}}

## Human overrides

{{human_overrides}}

Fix what the brief and the gates ask for, then finish with ONE JSON object matching your
output schema, as before."""

REVIEWER_CONTINUATION = """\
# Continue: {{persona}} — campaign {{campaign}}, review loop {{packet_loop}}

This continues your review session in this campaign. The implementer has changed the code
since your last review: re-verify every prior finding against the files as they are now, not
against what you remember. The worktree is `{{worktree}}`.

## Delta since the head you last reviewed (`{{base}}`)

```diff
{{diff}}
```

{{packet_note}}

Finish with ONE JSON object matching your output schema, as before."""


def continuation(cfg, state, persona, ctx) -> str:
    from .personas import render
    if persona == "implementer":
        packets = _latest(cfg, state, "packet-loop-")
        files = _latest(cfg, state, "findings-loop-")
        vals = {**ctx, "packet_path": f"`{packets[-1][1]}`" if packets else "(none)",
                "findings_path": f"`{files[-1][1]}`" if files else "(none)"}
        return render(IMPLEMENTER_CONTINUATION, vals) + "\n"
    from . import packet as PK
    return render(REVIEWER_CONTINUATION, {**ctx, "persona": persona,
                                          "packet_note": PK.note(ctx)}) + "\n"


# ---- the request ----------------------------------------------------------------------------

def shape(cfg, state, persona, ctx, prompt, *, backend, model, tier) -> dict:
    """The `RunRequest` overrides of a managed persona's run (`{}` when not managed): the prompt
    (today's plus the findings history; a continuation when resuming), and the session fields.
    A reviewer resumes only in a delta review (SPEC-0554 `review_mode: delta`)."""
    if not managed(state, persona):
        return {}
    history = history_note(cfg, state)
    fresh = prompt.rstrip("\n") + "\n\n" + history + "\n" if history else prompt
    out: dict = {"prompt": fresh}
    if not resumable(backend):
        return out
    out.update(persist_session=True, session_backend=backend)
    if backend == "pi":
        out["session_dir"] = str(store.campaign_dir(cfg, state["id"]) / "sessions" / "pi")
    entry, _why = check(cfg, state, persona, backend=backend, model=model, tier=tier)
    if entry is None or (persona in REVIEWERS and ctx.get("review_mode") != "delta"):
        return out
    out.update(prompt=continuation(cfg, state, persona, ctx), fresh_prompt=fresh,
               resume_session=str(entry["session_id"]),
               session_host=str(entry.get("host") or LOCAL))
    if backend in ("claude", "fake") and entry.get("session_cost_usd") is not None:
        out["resume_cost_base"] = float(entry["session_cost_usd"])
    return out


def as_fresh(req):
    """The fresh-session request of a resumed one: its fallback prompt, no session to resume
    (it still persists a new one)."""
    if not getattr(req, "resume_session", None):
        return req
    return replace(req, prompt=req.fresh_prompt if req.fresh_prompt is not None else req.prompt,
                   resume_session=None, session_host=None, resume_cost_base=None,
                   fresh_prompt=None)


def place(req, host: str | None, backend: str | None = None):
    """The request as it runs on `host` (`local` or a worker name): a resume of a session that
    lives elsewhere becomes the fresh request. Pi and Cursor sessions stay on the coordinator:
    a remote run of theirs neither resumes nor persists."""
    host = host or LOCAL
    if not (getattr(req, "resume_session", None) or getattr(req, "persist_session", False)):
        return req
    if host != LOCAL and backend in ("pi", "cursor"):
        return replace(as_fresh(req), persist_session=False, session_dir=None)
    if req.resume_session and (req.session_host or LOCAL) != host:
        return as_fresh(req)
    return req


def resume_failed(req, rr) -> bool:
    """A resumed run that failed as a resume: it ended without a conforming verdict (a missing
    or expired session, a backend error, a context limit) or in another session than the one
    it resumed. A timed-out or interrupted run is not retried (it ran; the entry is dropped)."""
    if not getattr(req, "resume_session", None) or rr is None:
        return False
    if getattr(rr, "interrupted", False) or getattr(rr, "timed_out", False):
        return False
    if rr.is_error or not rr.verdict_ok:
        return True
    sid = getattr(rr, "session_id", None)
    return bool(sid) and sid != req.resume_session


# ---- the registry ---------------------------------------------------------------------------

def _request_doc(result) -> dict:
    raw = getattr(result, "raw_path", None)
    if not raw:
        return {}
    try:
        doc = json.loads((Path(raw).parent / "request.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def record(state, persona, run_id, result, *, interrupted=False) -> dict:
    """Update `state.sessions` for a finished run of a managed persona and return the marks for
    its `run.end` (`resumed`). A run that kept no session changes nothing; one that ended
    without a conforming verdict drops the persona's entry."""
    if not on(state) or persona not in PERSONAS or result is None:
        return {}
    doc = _request_doc(result)
    if not (doc.get("persist_session") or doc.get("resume_session") or doc.get("session_backend")):
        return {}
    marks = {"resumed": bool(doc.get("resume_session"))}
    reg = state.setdefault("sessions", {})
    if not doc.get("persist_session"):          # placed where it keeps no session (`place`)
        reg.pop(persona, None)
        return marks
    ok = bool(result.verdict_ok and not result.is_error and not interrupted
              and not result.interrupted and result.session_id)
    if doc.get("resume_session") and result.session_id != doc.get("resume_session"):
        ok = False
    if not ok:
        reg.pop(persona, None)
        return marks
    from . import packet as PK
    loop = PK.loop_of(state)
    prev = reg.get(persona) if doc.get("resume_session") else None
    total = getattr(result, "session_cost_usd", None)
    if total is None:
        total = result.cost_usd
    reg[persona] = {
        "backend": doc.get("session_backend"), "session_id": result.session_id,
        "host": getattr(result, "worker", None) or LOCAL,
        "model": doc.get("model"), "tier": doc.get("tier"),
        "worktree": (state.get("repo") or {}).get("worktree"), "campaign": state.get("id"),
        "first_loop": (prev or {}).get("first_loop", loop), "last_loop": loop,
        "context_tokens": context_tokens(result.usage), "session_cost_usd": total,
        "run_id": run_id,
    }
    return marks
