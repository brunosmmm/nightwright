---
id: SPEC-0556
title: Resumable implementer and reviewer sessions per backend, with a fresh-session fallback
status: in-progress
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

### Design as built (where it differs from the above)

- **Module.** `agentic/sessions.py` holds the flag (`on`: `sessions`, `packet` and `collator`
  all pinned), the managed personas (`implementer` and the three builtin code reviewers; the
  deep close pass is not managed and leaves the registry alone), validity (`check`), the
  prompts (`continuation`, `history_note`), `shape` (called at the end of
  `personas.build_request`, after tiers and the effort ceiling picked the model), `place`,
  `as_fresh`, `resume_failed` and `record` (called first thing in `queue._record_run`).
- **Capability.** `runner.SESSION_RESUME = {"claude": True, "cursor": False, "pi": True,
  "fake": True}`: Cursor stays off until its real call is checked (see As shipped); a
  non-resumable backend runs the same flow fresh (no `persist_session`), with the findings
  history. `RunRequest` gains `persist_session`, `resume_session`, `session_host`,
  `resume_cost_base`, `fresh_prompt`, `session_dir` (pi) and `session_backend`; all unset
  with the flag off, and `request.json` / `run.json` / the remote job doc carry them only when
  set (`runner.session_doc`), so flag-off docs are byte-identical.
- **Claude.** `build_argv`: `--resume <id>` when resuming (no `--fork-session`; the resumed run
  keeps the same session id), `--no-session-persistence` dropped only when persisting or
  resuming. Checked against claude 2.1.322: a resumed `-p` run reports the session's
  **cumulative** `total_cost_usd` (and `modelUsage`), while `usage` is the run's own; so the
  registry keeps the session's cumulative cost and a resumed run is charged `total − base`
  (`runner.session_cost`, applied in `collect`, the remote `_applied` path and FakeRunner;
  the cumulative figure is kept as `session_cost_usd` in `run.json`). `--max-budget-usd` is per
  invocation (a resumed run whose session already cost more than its budget still ran), so the
  per-run budget and `timeout_s` cap a resumed run exactly as a fresh one. `--resume <id>`
  finds the session from any cwd of the same HOME (checked), so a worker's per-job tree is
  not a problem; a missing session ends `is_error`, `num_turns: 0`, cost 0 ("No conversation
  found").
- **Cursor.** `cursor_exec` takes `--resume <agent id>` (added by `build_cursor_argv`) and calls
  `Agent.resume(agent_id, options)` with the same `AgentOptions` as a create (`agent_resume`
  injectable); unverified against the real SDK, so not enabled.
- **Pi.** `pi_exec.mjs`: `SessionManager.create(cwd, session_dir)` for a persisted run
  (`session_dir` = `<campaign_dir>/sessions/pi`, set by the queue), `SessionManager.open(path,
  undefined, cwd)` to resume (`session_id` = the session file path); a missing file fails
  before any turn (`error_code: resume_missing`). A resumed session's state holds every earlier
  turn, so usage and cost count only the messages after the resume point. Pi and Cursor
  sessions stay on the coordinator: `place` turns a remote pi/cursor run into a fresh,
  non-persisted one (and its record drops the entry); the session dir never travels in a job.
- **Registry.** `state.sessions[persona] = {backend, session_id, host, model, tier, worktree,
  campaign, first_loop, last_loop, context_tokens, session_cost_usd, run_id}` (copied by the
  apply's `finish` like the other lean keys). Valid when backend, model, tier, worktree and
  campaign match, the backend is resumable and `context_tokens` is under
  `agentic.lean_session_max_tokens` (400000, in the nightwright config defaults). The host is
  checked at placement. `context_tokens` = the last model call's input + cache read + cache
  creation when the usage lists calls (claude's `iterations`), else the run's summed input +
  cache read (an upper bound). A run that ends without a conforming verdict (error, timeout,
  interrupt, a different session id) drops the entry; a successful one replaces it.
- **When to resume.** The implementer resumes on any later run with a valid entry (a review
  fix or a gate fix). A reviewer resumes only in a delta review (SPEC-0554 `review_mode:
  delta`); in a full review it runs fresh and its new session replaces the entry.
- **Prompts.** No template changed. A resumed implementer gets `IMPLEMENTER_CONTINUATION`
  (loop, worktree, spec path, the packet and latest `findings-loop-N.json` paths, the
  `pending_brief`, which with the collator holds only in-scope findings with their ties, the
  gate results and human overrides); a resumed reviewer gets `REVIEWER_CONTINUATION` (head
  change warning, the delta, then SPEC-0554's delta packet note with its own open findings).
  A fresh run once a review was collated (loop ≥ 2) gets today's prompt plus `history_note`:
  the latest packet path and each `findings-loop-N.json` with its in-scope records (severity,
  tie, title; 30 per file). The fresh prompt is kept on a resumed request (`fresh_prompt`,
  `prompt.fresh.md` in the run dir so an adopted run keeps it).
- **Remote runs.** `queue._do_persona` calls `sessions.place(req, worker or "local")` after
  `remote_personas.dispatch` picked the host: a resume whose `session_host` differs becomes the
  fresh request before it starts (no failure, no event). Dispatch order is unchanged (the
  campaign's affine worker comes first, so a campaign usually stays on its worker).
  `request_to_job` carries `resume_session` / `persist_session` / `resume_cost_base`; an older
  worker that ignores them runs the continuation fresh with another session id, which
  `resume_failed` treats as a failed resume (fallback).
- **Fallback.** `sessions.resume_failed`: a resumed run that ended without a conforming verdict
  or with another session id, not timed out or interrupted, not an environment fault or a
  usage-limit stop. `_do_persona` then reuses `_rerun_locally(..., fallback=True)`: the failed
  run's `run.end` and cost are recorded (its entry dropped), a `session_fallback` event
  (`persona`, `run_id`, `retry_run_id`, `session_id`, `worker`, `error`) and the new run's
  `run.start` (`retry_of`, `session_fallback: true`) are appended, and the fresh request runs
  locally under the new run id in the same step. The fallback is never a resume, so it cannot
  loop. An adopted resumed run (only a stripped request is left) that failed is marked
  interrupted instead: no retry is spent, the entry is dropped, and the next tick prepares the
  step fresh. Neither path parks.
- **Export.** `runs.resumed` from the run's `run.end` (`resumed`, written only for a session
  run), `runs.session_fallback` from its `run.start`.

## Alternatives considered

- **Resume only on Claude** — rejected: the human asked for every backend through the same
  logic; Cursor and Pi SDKs both expose resume.
- **Fork the session per loop** (`--fork-session`) — keeps a clean base but re-pays the
  history each time; plain resume is cheaper and the registry bounds context growth.
- **Require resume** — rejected by the human: a backend without it must still run the flow.

## Acceptance criteria

- [x] With `lean.sessions` on, a claude campaign's loop-2 implementer and each loop-2
      reviewer run with `--resume <their loop-1 session id>` and a continuation prompt that
      holds only the delta and the collated in-scope (implementer) or own prior (reviewer)
      findings.
- [ ] The cursor runner resumes via `Agent.resume` and the pi runner via a file-backed
      `SessionManager`, each proven by one real call recorded in the spec.
- [x] A registry entry is not reused when backend, model, tier or host differ or its context
      exceeds the threshold; the run starts fresh with the packet and findings history.
- [x] A failed resume falls back once to a fresh run in the same step, records
      `session_fallback`, and never parks the campaign.
- [x] Triagers, marshal, collator, spec personas and the close-time deep pass never resume.
- [x] With the flag off, argv and SDK calls are identical to today (incl.
      `--no-session-persistence`).
- [x] Runs export `resumed` and `session_fallback`.

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

## As shipped

Built 2026-11-12 behind `agentic.lean.sessions` (default **off**, already in
`config.DEFAULT_CONFIG` from SPEC-0553; needs `packet` and `collator`; pinned per campaign at
creation). New config default `agentic.lean_session_max_tokens: 400000`. Status stays
**in-progress**: the Cursor real call is not done (AC2) and the manual two-loop campaigns and
the measurement need live campaigns after the merge.

**Flag off, or a campaign pinned without `sessions: true`** (whatever the live config says
after the open): no session field on any `RunRequest`, `build_argv` unchanged with
`--no-session-persistence`, Cursor gets no `--resume` and calls `Agent.create`, Pi stays
`SessionManager.inMemory` with today's usage/cost counting, `request.json` / `run.json` / job
docs carry no session key, no `prompt.fresh.md`, no `state.sessions`, no `resumed` on
`run.end`, no `session_fallback` event, today's prompts. Asserted on the FakeRunner queue path
with `lean` absent, `{packet, collator}` and `{tiers, packet, collator}` pinned and the live
config flipped to all four after the open, and with `sessions.shape` / `place` patched to
raise. A campaign opened before this merge with `sessions: true` already pinned (possible since
SPEC-0553, which pinned all four flags) starts keeping sessions from its next run; the live
config has no `sessions: true`.

**Modules.** `agentic/sessions.py` (new); `runner.py` (`SESSION_RESUME`, session fields,
`build_argv` / `build_cursor_argv`, `session_doc` / `session_fields` / `session_cost`,
`prompt.fresh.md`, `rebuild_request`, `request_to_job` / `request_from_job`, FakeRunner
`resume_error` and cumulative cost); `cursor_exec.py` (`--resume`, `Agent.resume`);
`pi_exec.mjs` (file-backed `SessionManager.create` / `open`, own-turn usage on resume);
`personas.build_request` (`sessions.shape`); `queue.py` (`_record_run` → `sessions.record`
and `run.end.resumed`, `_do_persona` placement and fallback, `_rerun_locally(fallback=True)`,
`finish` copies `sessions`); `remote_personas.py` (`session_cost_usd` in the result doc, cost
on the `_applied` path); `schemas.py` (`session_fallback` event); `campaign_export.py`
(`runs.resumed`, `runs.session_fallback`); `config.py`; docs (capabilities, README).

**Tests.** New `tests/test_agentic_sessions.py`: validity per backend / model / tier /
worktree / campaign / threshold and no cross-persona reuse; `context_tokens`; personas that
never keep a session (triagers, marshal, collator, spec writer / reviewer, resolver) and the
deep close pass; `place` / `resume_failed` / `record`; FakeRunner queue runs: two loops with the
loop-2 implementer and both reviewers resumed (`--resume` ids, continuation prompts with only
the delta and the in-scope / own findings, the fresh prompt kept, resumed runs charged their own
cost, export columns), a non-resumable backend running the same flow fresh with the packet and
findings history, a failed resume falling back once in the same step (`session_fallback`, no
park, the fallback's fresh session recorded), remote placement (`box`→`box` resumes,
`box`→local and `box`→`other` run fresh), the remote result doc, flag off / pinned before the
flag. Extended: `tests/test_agentic_runner.py` (argv snapshot, persist / resume argv, cumulative
cost, docs without session keys, adoption rebuild, job doc round trip, FakeRunner semantics),
`tests/test_cursor_sdk_exec.py` (`Agent.resume` with `--resume`, `Agent.create` otherwise,
argv), `tests/js/pi_exec.test.js` via `tests/test_pi_exec_js.py` (create in `session_dir`,
open + own-turn cost, missing file), `tests/test_agentic_campaign_export.py` (pinned columns).
The remote placement test sits in `test_agentic_sessions.py` (a stand-in for the dispatched
worker) rather than `test_remote_personas.py`.

**Real calls.** Claude (`claude` 2.1.322, `claude-haiku-5-5`, effort `medium`), through
`runner.ClaudeRunner` (the real `build_argv`, wrapper, `collect`, `session_cost`) on a scratch
clone, a read-only reviewer-shaped run (review `sessions.py` + its call sites, up to 3
findings), then a one-line change committed, then loop 2 two ways:

| Run | Cost | Input | Cache create | Cache read | Output | Time |
|---|---|---|---|---|---|---|
| loop 1 (persisted) | $0.0131 | 12 | 47,268 | 170,764 | 3,920 | 21.6 s |
| loop 2 resumed (`--resume`, continuation: delta + prior findings) | **$0.0025** (session total $0.0156 − $0.0131) | 4 | 5,272 | 96,611 | 982 | 6.4 s |
| loop 2 fresh (full prompt + delta + prior findings) | $0.0081 | 8 | 27,895 | 97,095 | 3,044 | 15.9 s |
| resume of a missing session id | $0.00 | 0 | 0 | 0 | 0 | 0.8 s, `is_error`, 0 turns |

The resumed turn cost 69% less than the fresh one (cache writes 5.3k vs 27.9k, output 1.0k vs
3.0k, 2.5× faster) and kept the session id. An earlier CLI-only pair (Haiku, effort `low`, a
Q&A over `packet.py`) gave the same picture: resumed $0.0014 (cache create 782, read 89,701)
vs fresh $0.0066 (cache create 26,357, read 77,717), and showed the cumulative
`total_cost_usd` the runner now subtracts. Total real-call spend about $0.08.

Pi (`pi-coding-agent` 1.5.0, `anthropic/claude-haiku-4-5`, thinking `low`, stored OAuth
credentials) through `runner.PiRunner` / the real `pi_exec.mjs`: a persisted run wrote
`<session_dir>/<ts>_<uuid>.jsonl` ($0.0169, input 4,070, cache write 8,359); the resumed run
opened that file and answered a question about the first turn from memory ($0.0119, input 10,
cache write 8,424, own-turn usage only); the fresh equivalent cost $0.0172 (input 4,086); a
missing session file failed before any turn (`session not found`). Pi shows no cache reads
across processes (`cacheWarming: off`), so its saving is the re-read input, not the cache.

Cursor: **unverified** (the API key sits behind pass/pinentry and was not available
non-interactively). The `Agent.resume` path is built and unit-tested with injected fakes only;
`SESSION_RESUME["cursor"]` stays false, so Cursor campaigns run the same flow fresh with the
packet and findings history until one real resume is recorded here.

**Post-merge.** Manual verification (one real two-loop campaign per backend: claude, pi) and
the measurement (≥ 10 Meta-Tools campaigns with all four flags vs three: median tokens per loop
for the implementer and reviewers, loops 2+, `resumed`; cost per campaign) need live campaigns
with the flag on (`agentic.project_lean.Meta-Tools.sessions: true`).

## Open questions

- Default for `agentic.lean_session_max_tokens` (400k proposed) — tune from measured
  context sizes after the first campaigns.
