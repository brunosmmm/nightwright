---
id: SPEC-0176
title: "Queue state machine with lease-guarded tick/run, park/resume and listing"
status: accepted
owner: user
created: 2026-10-25
updated: 2026-10-25
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0169
tags: [agentic, queue, state-machine, cli]
source_idea: IDEA-023
depends_on: [SPEC-0170, SPEC-0171, SPEC-0172, SPEC-0173, SPEC-0174, SPEC-0175]
---

## Context

Child of epic [SPEC-0169](./0169-mini-agentic-workflow-queue-campaigns-headless.md)
(promoted from `IDEA-023`).

This is the engine. The user decided that wt owns the loop: a Python process advances
campaigns through a fixed state machine, spawning personas (SPEC-0174), running gates
(SPEC-0173), opening worktrees (SPEC-0172) and closing (SPEC-0175), while recording every
step in the campaign store (SPEC-0171). Humans add work, watch it, and act only on parked
items. The conductor skill (SPEC-0177) starts the loop detached and supervises it.

## Goals / Non-goals

**Goals**
- A pure `plan_step` that maps (state, profile, project config, last results) to one
  `Step`, and an `apply_step` that executes it and records transition and cost events.
- FIFO admission bounded by the profile's `slots`; agent steps run concurrently in a
  thread pool; repository steps (`OPENING`, `CLOSING`) run serially; all state writes on
  the main thread under the campaign lock.
- `wt queue add|list|watch|tick|run|park|resume|abandon` with the repo's dry-run and
  `--json` conventions; `wt queue list --json` (`wt.queue.v1`) is the wave-1 watch.
- Parks with named reasons and a `resume_to`; `resume` with `--note`, `--raise-cap`,
  `--reset-loops`; abandon with cleanup.
- Signal handling that leaves state consistent and never loses a run's cost.
- Offline end-to-end tests of the whole loop.

**Non-goals**
- Reviews, the spec phase, mailbox, TUI tab (later waves add states and personas).
- Running without the lease; two loops at once.
- Auto-takeover of a stale lease.

## Decision

Add `src/wt/agentic/queue.py` and the `queue` commands in `src/wt/agentic/cli.py`. The
state machine is data (`TRANSITIONS`, `PARK_REASONS`) plus `plan_step`; every transition
written to a campaign passes through one function that also appends the `transition`
event, so the ledger can never disagree with `state.json`.

## Design

### States and transitions

`STATES = QUEUED, OPENING, IMPLEMENTING, GATING, TRIAGING, CLOSING, PARKED, MERGED,
HANDED_OFF, ABANDONED`; terminal = the last three.

| from | step | to |
|---|---|---|
| `QUEUED` | admit (busy < slots, oldest first) | `OPENING` |
| `OPENING` | `worktree_add` + `worktrees.setup` cmds; record `base_sha_at_open`, `spec.sha256_at_open` | `IMPLEMENTING`; failure ⇒ `PARKED(open_failed)` |
| `IMPLEMENTING` | cap check; spawn implementer; `commit_all` with message `wt(<id>): implementer run NNN — <summary line>` + trailer | `GATING`; verdict `blocked` twice in a row ⇒ `PARKED(implementer_blocked)` |
| `GATING` | `gates.run_all` | all required ok ⇒ `CLOSING`; else `TRIAGING` |
| `TRIAGING` | cap check; spawn gate triager (read-only) | `fix` ⇒ `loops += 1`; `loops > loop_max` ⇒ `PARKED(loop_exhausted)` else `IMPLEMENTING` with `pending_brief`; `retry_gate` (≤ 1 per loop, else treated as `fix`) ⇒ `GATING`; `waive` ⇒ `CLOSING` only if every failing gate is optional, else `escalate`; `escalate` ⇒ `PARKED(triage_escalated)`; malformed verdict ⇒ one retry, then `escalate` |
| `CLOSING` | `close.close_campaign` (retry from target capture on `base_moving` signal, `close_attempts ≤ 2`) | `MERGED` / `HANDED_OFF` / `TRIAGING` (loop counted) / `PARKED(<reason>)` |
| `PARKED` | human `resume` | `park.resume_to` or `--to` |
| `PARKED`, `QUEUED` | human `abandon` | `ABANDONED` |

Counters: `loops` on `fix`; `runs` on every spawn; `gate_runs` on every gate run;
`gate_retries` reset per loop; `parks` on every park. Cost cap before every spawn:
`cost.total_usd + persona.max_budget_usd > cost.cap_usd` ⇒ `PARKED(cost_cap)`. After every
run: `cost` event, `cost.total_usd += cost_usd or persona.max_budget_usd`,
`by_persona` updated, `run.end` event, `finding` events from `must_fix` / `blockers`.

### `plan_step(state, profile, project_cfg) -> Step | None`

Pure: returns `Step(kind ∈ {open, implement, gate, triage, close, none}, persona=None,
reason=…)` or a `Park` decision (cost cap, loop exhaustion) without touching anything.

### `tick(cfg, *, lease, now) -> TickReport`

1. Scan `list_campaigns` (non-terminal, not `PARKED`).
2. Admit `QUEUED` items oldest-first while `busy < slots` (busy = items in `OPENING`
   through `CLOSING`), transitioning them to `OPENING` and opening them serially.
3. For each active item compute `plan_step`; run `implement` / `gate` / `triage` steps in
   `ThreadPoolExecutor(max_workers=slots)` (the step returns the run/gate results only);
   run `close` steps serially after the pool joins.
4. Apply results on the main thread under `item_lock`: events, counters, cost, the
   transition. `TickReport(steps=[{campaign, from, to, action, run_id, cost_usd,
   reason}])` → `wt.queue.tick.v1`.

### `run(cfg, *, once=False, max_ticks=None, poll_interval_s=None, until_idle=False)`

`lease.acquire` (error if held); loop: `tick`, `heartbeat`, sleep; `--until-idle` exits
when no non-terminal, non-parked items remain; SIGINT/SIGTERM set a stop flag (no new
admissions), the running `claude` children get SIGTERM then SIGKILL after 30 s, their
`run.end{interrupted: true}` and cost are recorded, state stays as it was (the next tick
respawns a fresh agent); `lease.release()` in `finally`. One line per step on stdout;
`--json-lines` prints one `wt.queue.tick.v1` object per tick.

### CLI (`queue` group; `gates` from SPEC-0173 lives here too)

| command | behavior | `--json` | writes |
|---|---|---|---|
| `add SPEC_ID [--project] [--profile] [--base] [--force] [--yes]` | resolve project (internal id ⇒ Meta-Tools, outbound id ⇒ its outbox project, or `--project`), `project_config`, spec path and frontmatter (`accepted` required; `--force` admits `in-progress`), no active campaign for the spec; prints the plan; `--yes` creates the campaign in `QUEUED` | `wt.queue.add.v1` `{plan…, written}` | dry-run default |
| `list [--all] [--project] [--state]` | items + `lease_status` + slots | `wt.queue.v1` | no |
| `watch [--interval 2]` | Rich `Live` table over the same payload | — | no |
| `tick` | one iteration under the lease | `wt.queue.tick.v1` | yes |
| `run [--once] [--max-ticks N] [--poll-interval S] [--until-idle] [--json-lines]` | the loop | per tick | yes |
| `park CAMPAIGN --reason TEXT [--yes]` | human park; a step already running finishes and is recorded, then the park applies on the next tick | `wt.queue.park.v1` | dry-run default |
| `resume CAMPAIGN [--to STATE] [--note TEXT] [--raise-cap USD] [--reset-loops] [--yes]` | `--to` is a `click.Choice(IMPLEMENTING, GATING, CLOSING, QUEUED)`; default `park.resume_to`; `--note` goes to `pending_brief.human_notes`; records `resume` | `wt.queue.resume.v1` | dry-run default |
| `abandon CAMPAIGN [--keep-worktree] [--yes]` | `ABANDONED`; removes the worktree unless kept; keeps the branch | `wt.queue.abandon.v1` | dry-run default |

`SPEC_ID` completes with `complete_any_spec_id`; `CAMPAIGN` with `complete_campaign_id`;
`--project` with `complete_project`.

### Docs

`docs/product/capabilities/agentic.md` gets the state machine, the commands and the park
reasons; `docs/product/reference/index.md` gets the `queue` row; the agent-surface guide
gets the schema rows; `docs/WORKFLOW.md` gets a three-line "agentic loop" entry.

## Alternatives considered

- **Per-campaign processes instead of a thread pool** — rejected for wave 1. The pool is
  enough for two to three slots, and state writes stay on one thread; a `--detach` /
  supervision child idea exists if this proves limiting.
- **Triage on every gate run** — rejected. A green gate needs no model; it saves a run per
  loop.
- **Auto-resume after a cooldown** — rejected. `PARKED` is the human touchpoint by design.
- **A separate queue index file** — rejected (SPEC-0171): the scan of `state.json` files is
  the single source of truth.

## Acceptance criteria

- [ ] Every row of the transition table holds (parametrized tests over `plan_step` and
      `apply_step` with fake results), including `retry_gate` limited to one per loop,
      `waive` only for optional failures, malformed-verdict retry-then-escalate, and the
      double-`blocked` park.
- [ ] Loop exhaustion, the pessimistic cost cap, triage escalate and open failure each park
      with the named reason and the documented `resume_to`.
- [ ] Admission is FIFO and respects `slots`; a second `tick` while the lease is held
      fails naming the holder.
- [ ] `park` on a running item applies after its step is recorded; `resume --note`
      appears in the next implementer prompt; `--raise-cap` / `--reset-loops` are recorded
      and effective.
- [ ] SIGTERM during a fake run with `sleep_s` leaves state unchanged, records
      `run.end{interrupted}` with its cost, and releases the lease.
- [ ] `wt queue add` refuses a non-accepted spec, an unknown project and a spec with an
      active campaign; writes only with `--yes`; `--json` emits `wt.queue.add.v1`.
- [ ] `wt queue list --json` emits `wt.queue.v1` with lease, slots and items; `watch`
      renders without error for two intervals in a pilot run.
- [ ] **End-to-end (internal):** temp repo as `wt_checkout` with `.wt/project.yaml` (two
      `sh` gates), an accepted spec, `FakeRunner` script whose first run writes the feature
      file and sets the spec `done` ⇒ `wt queue add --yes` + `wt queue run --until-idle`
      ⇒ `MERGED`; main contains the file; worktree removed; ledger contains `open,
      transition×n, run.start, run.end, commit, gate×2, cost, close`; `wt queue list --json`
      has no items.
- [ ] **End-to-end (outbound):** same with an `outbox_targets` project and policy `pr`
      with the fake `gh` ⇒ `HANDED_OFF` with a URL.
- [ ] **Park variants:** never-green gate ⇒ `loop_exhausted` after `loop_max` loops (with
      the triager returning `fix` each time); dirty main ⇒ `main_dirty`, then `resume`
      after cleaning ⇒ `MERGED`; `campaign_cap_usd 0.01` ⇒ `cost_cap` before the first
      spawn; conflicting base commit ⇒ `rebase_conflict`.
- [ ] Completion, reference, agent-surface and mkdocs guards pass.
- [ ] Full `uv run pytest` green; `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests:** `tests/test_agentic_queue.py` (pure transitions, counters, cap
  math, admission, lease refusal, signal handling with a fake run that sleeps);
  `tests/test_agentic_queue_cli.py` (`add` / `park` / `resume` / `abandon` dry-run and
  `--yes`, `list --json`); `tests/test_agentic_e2e.py` (the two end-to-end runs and the
  four park variants, all through `CliRunner` or `subprocess` with `WT_DATA_DIR` /
  `WT_CONFIG_DIR` / `WT_AGENTIC_RUNNER=fake` pointed at `tmp_path`).
- **Manual verification:** on a scratch clone of this repo with a trivial accepted spec and
  the fake runner, `wt queue add --yes` then `wt queue run --until-idle` and read
  `wt campaign log`. The real-runner run belongs to SPEC-0177.
- **Regression guard:** full `uv run pytest`.

## Clock Log

Internal work: clock on IDEA-023 with `wt idea clock-in` / `clock-out`.

## Rollout / migration

Nothing runs until a human runs `wt queue add --yes` and starts `wt queue run`. Existing
commands are unchanged.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

_None — decisions locked above._
