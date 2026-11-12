---
id: SPEC-0556
title: Resumable implementer and reviewer sessions per backend, with a fresh-session fallback
status: accepted
owner: user
created: 2026-11-11
parent: SPEC-0552
depends_on: [SPEC-0555]
tags: [agentic, cost, runner]
source_idea: IDEA-1297
---

## Context

Phase 4 of [SPEC-0552](./0552-leaner-agentic-campaign-loop.md). Every persona run is a fresh
session on every backend, so each fix loop re-reads the spec, the repository and the
history:

- **claude** — `runner.build_argv` runs `claude -p … --no-session-persistence`; no
  persona run can be resumed. The binary itself supports `--resume <id>` (used today only
  for interactive conductor/steward sessions in `launch.build_plan`).
- **cursor** — `cursor_exec._default_create` calls `cursor_sdk.Agent.create` per run and
  returns the agent id as `session_id` (`cursor_exec._result_event`), but never resumes. The
  installed `cursor_sdk` has `Agent.resume(agent_id, options)`.
- **pi** — `pi_exec.mjs` builds the session with `pi.SessionManager.inMemory(cwd)`; the
  installed Pi SDK (1.5.0) has file-backed `SessionManager.create(cwd, sessionDir)` and
  `SessionManager.open(path)`.
- **remote workers** (SPEC-0344, `runner.request_to_job` / `remote_personas.py`) run a
  persona on another host, where a local session file does not exist.

So no backend resumes a persona session today. Implementer runs are 14% of cost and 4 per
median campaign; with phases 2–3 a loop-2 implementer or reviewer only needs the delta and
the collated findings, which a resumed session can take without re-reading.

## Goals / Non-goals

**Goals**
- One implementer session per campaign, resumed each fix loop where the backend supports
  it; fix loops send only the collated in-scope findings (SPEC-0555).
- One session per code reviewer per campaign, resumed each review loop with the new packet
  delta (SPEC-0554).
- A backend or situation without resume uses a fresh session fed the packet and the
  findings history: resume is an optimisation, never a requirement.

**Non-goals**
- Resuming triagers, the marshal, the collator, spec personas or the close-time deep pass
  (all short or deliberately fresh).
- Moving session files between hosts.

## Decision

Add a per-backend session capability to the runner and a per-campaign session registry.
For `implementer` and the three code reviewers, a run in loop ≥ 2 resumes the persona's
recorded session when the registry entry is valid for this spawn, sending a short
continuation prompt (loop number, packet path, delta, and the in-scope findings or own prior
findings); otherwise it starts a fresh session whose prompt carries the packet and the full
findings history. Any resume failure retries once as a fresh session in the same step.

## Design

- **Capability** (`runner.py`): `SESSION_RESUME = {"claude": True, "cursor": True, "pi":
  True, "fake": True}`, each only after its real-call check (below); `RunRequest` gains
  `resume_session: str | None` and `persist_session: bool`.
  - claude: `build_argv` drops `--no-session-persistence` when `persist_session`, adds
    `--resume <id>` when `resume_session`; the session id comes back in the result event as
    today.
  - cursor: `cursor_exec.execute` uses `Agent.resume(agent_id, options)` when given
    `--resume <id>`, else `Agent.create`.
  - pi: `pi_exec.mjs` uses `SessionManager.create(cwd, <campaign_dir>/sessions/pi)` for a
    persisted run and `SessionManager.open(<path>)` to resume; the session file path is the
    returned `session_id`.
  - FakeRunner: records resume requests so the loop is testable offline.
- **Registry** (`state.sessions`, written in `queue._record_run`):
  `{persona: {backend, session_id, host, model, tier, worktree, first_loop, last_loop,
  context_tokens}}`. An entry is valid for a spawn only if backend, model, tier and host
  (local, or the worker name of a remote run) match and `context_tokens` (last run's
  input + cache-read) is under `agentic.lean_session_max_tokens` (default 400k); otherwise
  the spawn is fresh and the entry is replaced.
- **Prompts** (`personas.build_context` / templates): a `{{continuation}}` block used only
  when resuming (implementer: in-scope findings of `findings-loop-N.json` with AC ties and
  the gate results; reviewer: packet path, delta, own prior findings). A fresh loop ≥ 2
  run gets today's template plus the packet and the findings history from the
  `findings-loop-*.json` files.
- **Remote runs:** `request_to_job` carries `resume_session` only when the job is pinned to
  the host in the registry; if the scheduler places it elsewhere the run is fresh.
- **Fallback:** a resume that fails (unknown session, expired, backend error before the first
  turn) is retried once as a fresh run in the same step with a `session_fallback` event;
  it never parks the campaign.
- **Budgets:** `max_budget_usd` stays per run; a resumed run's cost includes its cache
  reads, so the measured cost is honest.
- **Export:** runs gain `resumed` (bool) and `session_fallback` (bool).

Flag: `state.lean.sessions` (requires `packet` and `collator`; pinned at open, SPEC-0553).

## Alternatives considered

- **Resume only on Claude** — rejected: the human asked for every backend through the same
  logic; Cursor and Pi SDKs both expose resume.
- **Fork the session per loop** (`--fork-session`) — keeps a clean base but re-pays the
  history each time; plain resume is cheaper and the registry bounds context growth.
- **Require resume** — rejected by the human: a backend without it must still run the flow.

## Acceptance criteria

- [ ] With `lean.sessions` on, a claude campaign's loop-2 implementer and each loop-2
      reviewer run with `--resume <their loop-1 session id>` and a continuation prompt that
      holds only the delta and the collated in-scope (implementer) or own prior (reviewer)
      findings.
- [ ] The cursor runner resumes via `Agent.resume` and the pi runner via a file-backed
      `SessionManager`, each proven by one real call recorded in the spec.
- [ ] A registry entry is not reused when backend, model, tier or host differ or its context
      exceeds the threshold; the run starts fresh with the packet and findings history.
- [ ] A failed resume falls back once to a fresh run in the same step, records
      `session_fallback`, and never parks the campaign.
- [ ] Triagers, marshal, collator, spec personas and the close-time deep pass never resume.
- [ ] With the flag off, argv and SDK calls are identical to today (incl.
      `--no-session-persistence`).
- [ ] Runs export `resumed` and `session_fallback`.

## Test plan

- **Automated tests:**
  - `tests/test_agentic_runner.py` (extend): `build_argv` with/without
    `persist_session`/`resume_session`; flag-off argv snapshot unchanged.
  - `tests/test_cursor_runner.py` (extend): `execute` calls `Agent.resume` with `--resume`,
    `Agent.create` otherwise (injected `agent_create`/resume fakes).
  - `tests/test_pi_exec_js.py` / `tests/test_pi_runner.py` (extend): file-backed session
    create and open.
  - `tests/test_agentic_sessions.py` (new): registry validity (backend/model/tier/host/
    threshold), fallback on resume failure, personas that never resume.
  - `tests/test_remote_personas.py` (extend): resume only when pinned to the recorded host.
  - FakeRunner queue test: two loops; loop-2 implementer and reviewers resumed; a backend
    marked non-resumable runs the same flow fresh with packet + findings history.
- **Manual verification:** one real two-loop campaign per backend (claude; cursor and pi
  where credentials exist), confirming the second loop resumed (transcript continues, cache
  reads high, new input low).
- **Measurement:** ≥10 Meta-Tools campaigns with all four flags vs three: median tokens
  (input + cache read + output) per loop for `implementer` and the reviewers (runs export
  `loop`, `resumed`), loops 2+ only; plus cost per campaign. Regression-set replay as in
  SPEC-0552.

## Rollout / migration

Flag off by default; requires `packet` and `collator`. Enable per backend as its real-call
check passes (a backend not yet confirmed is treated as non-resumable). Then default-on.
Session files under `<campaign_dir>/sessions/` are removed with the campaign dir; Claude's
own session files follow Claude's retention.

## Risks

- **Context bloat in long campaigns** — bounded by the token threshold, after which a fresh
  session with the packet takes over.
- **Stale beliefs in a resumed reviewer** (it "remembers" code that the implementer changed)
  — the continuation prompt always names the new head and the delta, and the reviewer must
  re-verify its prior findings against files, not memory.
- **Session store on another host / retention expiry** — host pinning and the fresh
  fallback.
- **Correlated blind spots** — the same reviewer session may keep missing the same class
  of bug; the hand-raised deep close pass (SPEC-0553) is fresh by design, and the
  regression set guards default campaigns.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; affected tests pass; real-call checks and measurement recorded.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated.

## Open questions

- Default for `agentic.lean_session_max_tokens` (400k proposed) — tune from measured
  context sizes after the first campaigns.
