---
id: SPEC-0182
title: "Event-driven wt-conduct: Monitor-armed conductor woken by queue events"
status: done
owner: user
created: 2026-10-25
updated: 2026-10-25
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0169
tags: [agentic, conductor, skills, events]
source_idea: IDEA-048
depends_on: [SPEC-0181, SPEC-0183]
supersedes: [SPEC-0177]
---

## Context

Child of epic [SPEC-0169](./0169-mini-agentic-workflow-queue-campaigns-headless.md)
(promoted from `IDEA-048`).

[SPEC-0177](./0177-wt-conduct-skill-docs-and-dogfood-bootstrap-for.md) shipped
`/wt-conduct` as an invoke-once procedure: look at the queue, start the loop, decide parked
items, stop. The user's model is different: the conductor is a **long-lived session that
runs a monitor and is constantly woken by events fired by the queue** — parks, merges,
stuck or stale items, steward messages — and resolves them, delegating real work to
sub-agents so it never blocks. SPEC-0181 provides the event stream with watchdog signals;
SPEC-0183 the mailbox. This spec supersedes SPEC-0177 to replace the conductor procedure;
everything else SPEC-0177 shipped is carried forward unchanged.

## Goals / Non-goals

**Goals**
- `/wt-conduct` arms one Monitor on `wt queue events --follow --for conductor` (timeout at
  the maximum) and re-arms it on every expiry; each notification is one event to resolve.
- A documented decision table per event type (below), with `wt` commands for each action,
  and escalation to the human only when the table says so.
- `wt queue conduct-status --json` (`wt.conduct.status.v1`): one call that returns what a
  freshly woken conductor needs — lease health, active/backlog counts, parked items with
  reasons, unanswered conductor mail, last 10 events — so each wake-up starts from facts.
- One conductor at a time: `wt queue conduct-lease acquire|release|status` (a second lease
  under `agentic/conductor.lock`, same flock + heartbeat design as the runner lease); the
  skill acquires it on start and refuses to run as a second conductor.
- Sets `WT_MAILBOX_ADDR=conductor` for every `wt mailbox send` it issues.

**Carried forward from SPEC-0177 (unchanged):** wt's `.wt/project.yaml` (gates, merge
policy, worktree setup), the dogfood config test, docs pointers, `/wt-conduct` installed via
`wt skills install`.

**Non-goals**
- Automatic retry of transient run failures (separate spec; the conductor resumes them for
  now). The steward skill (SPEC-0184).

## Decision

Rewrite `skills/wt-conduct/SKILL.md` around the Monitor loop and the decision table; add the
`conduct-status` and `conduct-lease` subcommands; keep the skill thin (commands + table, no
flag lists).

## Design

### Decision table (in the skill)

| event | action |
|---|---|
| `runner_dead` / `runner_stopped` with work pending | start the loop (`setsid nohup wt queue run …`), confirm the lease |
| `lease_stale` | report pid + age to the human; do not kill unless told (hung runner) |
| `run_overdue` / `campaign_silent` | read `wt campaign show/log`; if the run's process is gone, `wt queue park` + `resume`; else report and wait one more interval, then escalate |
| `run_failed{transient: true}` | wait for the reset time in the error if present, then resume the campaign with a note |
| `parked{cost_cap}` | resume with `--raise-cap` up to 2× the profile cap once; beyond that escalate |
| `parked{loop_exhausted}` | read the last triage brief; if a steward exists for the project, ask it (`wt mailbox send --to steward:<P> --kind question --campaign …`); else escalate with the brief |
| `parked{rebase_conflict}` on generated files only | resume to CLOSING after regenerating (until the deterministic resolver lands); else escalate |
| `parked{main_dirty | main_not_on_base}` | escalate (human checkout) |
| `parked{triage_escalated | implementer_blocked}` | ask the steward if present; else escalate |
| `message` from a steward | act on `request` (queue add / prioritize / park / resume via dry-run then `--yes`), file `answer` into the campaign (`wt campaign note`) and resume if it unblocks |
| `merged` / `handed_off` | `wt spec reconcile --apply`; report the spec id and cost |
| `backlog_blocked` (unknown dependency) | escalate (spec needs fixing) |

### Commands

- `wt queue conduct-status [--json]`: pure read over `queue_payload`, `events.read_since`,
  `mailbox.list --unanswered --to conductor`.
- `wt queue conduct-lease acquire [--json] | release | status`: `lease.py` generalized with
  a `name` (`queue` | `conductor`); `acquire` holds the flock in a small detached helper
  (`wt queue conduct-lease hold`) whose pid is recorded, so the lease outlives individual
  Bash calls and dies with the session's process tree.

### Skill shape

When to use / Procedure (acquire lease → `conduct-status` → arm Monitor → per event: table
→ act → log `wt campaign note` → back to waiting; re-arm on expiry; release on exit) /
Conventions (never implement; sub-agents for anything long; one conductor; shared ideas
file guard) / Verify.

### As shipped (MVP, 2026-10-25)

- No `conduct-lease` command: the single-conductor guarantee is the `conductor` reader lock
  held by the event follower the Monitor runs (`events.reader_lock`); `conduct-status` reports
  `conductor_pid` from it.
- The decision table gained rows for transient failures (resume with `--reset-loops`), the
  self-restart after a merge that touches `src/wt/agentic/`, and steward requests.
- **Never block** (user rule, 2026-10-25): the conductor's own context only runs quick
  reads/writes; investigations, rebase-conflict resolution, merge repair and any long command
  go to background sub-agents (or background shells for mechanical waits) that report back with
  a recommended `wt` action. The skill has a "Never block" section, per-campaign single-worker
  bookkeeping via `wt campaign note`, and the decision-table rows for `run_overdue`,
  `campaign_silent`, `rebase_conflict` (resolver sub-agent in the campaign worktree, then
  resume to CLOSING for the re-gate), `open_failed`/`gh_failed` and `merged` dispatch
  accordingly.
- Live so far: armed conductor woken by `lease_stale` (false positive, watchdog fixed) and by
  `merged` (reconciled the idea, restarted the loop). Still to observe for the manual AC: a
  park and a `runner_dead`.

## Alternatives considered

- **Keep invoke-once and use /loop** — rejected by the user: polling cannot react to stuck
  items or steward messages promptly, and does not compose with the mailbox.
- **Put the decision table in code** — rejected for wave 2: the table needs judgment
  (reading briefs, asking stewards); deterministic parts already live in the queue.

## Acceptance criteria

- [x] The skill passes the thinness guard, defers to `wt queue --help` / `wt campaign --help` /
      `wt mailbox --help` (as shipped; no per-command `events --help` pointer), contains the
      decision table, and instructs Monitor arming and re-arming.
- [x] `wt queue conduct-status --json` returns lease, counts, parked items, unanswered mail
      and recent events (tested on fabricated state; also verified live).
- [x] Single conductor (as shipped: the `conductor` reader lock held by `wt queue events
      --follow --for conductor`, reported as `conductor_pid`; no `conduct-lease` command, see
      As shipped); the runner lease is unaffected.
- [x] SPEC-0177 marked `superseded_by: SPEC-0182`.
- [x] **Manual:** a conductor session armed on the event stream is woken by a parked
      campaign and resolves it per the table (recorded in Test record; the `runner_dead`
      half was not separately observed, `lease_stale` and `merged` were).
- [x] Full `uv run pytest` green; `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests:** skills guard; `tests/test_agentic_conduct.py` (conduct-status
  payload, conductor lease).
- **Manual verification:** scratch data dir, fake-runner campaign forced to park, conductor
  session with Monitor; kill the loop to trigger `runner_dead`; record what woke it and
  what it did.
- **Regression guard:** full `uv run pytest`.

### Test record

- Automated: `tests/test_agentic_events.py`, `test_agentic_followers.py`, `test_agentic_mailbox.py`,
  `test_skills.py` (thinness guard, conductor skill content); full suite green at close.
- Manual (2026-10-25, production): the armed conductor Monitor was woken by `parked` events
  (e.g. ids 170, 171, 193, 198, 199, all `rebase_conflict`; earlier 101/105/158) and acted on
  them per the table, plus `lease_stale` and `merged`. `wt queue events --type parked --tail 5`
  and `wt queue conduct-status --json` (live lease, conductor_pid, steward, parked list)
  confirm. `runner_dead` was not separately forced; its row is exercised by tests only.

## Clock Log

Built by the agentic loop; idea bookkeeping via `wt spec reconcile --apply` after merge.

## Rollout / migration

Replaces the skill text in place; `wt skills install` symlink picks it up.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

_None — decisions locked above._
