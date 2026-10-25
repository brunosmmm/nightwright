---
id: SPEC-0176
title: "Queue state machine with lease-guarded tick/run, park/resume and listing"
status: done
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

- [x] Every row of the transition table holds (parametrized tests over `plan_step` and
      `apply_step` with fake results), including `retry_gate` limited to one per loop,
      `waive` only for optional failures, malformed-verdict retry-then-escalate, and the
      double-`blocked` park.
- [x] Loop exhaustion, the pessimistic cost cap, triage escalate and open failure each park
      with the named reason and the documented `resume_to`.
- [x] Admission is FIFO and respects `slots`; a second `tick` while the lease is held
      fails naming the holder.
- [x] `park` on a running item applies after its step is recorded; `resume --note`
      appears in the next implementer prompt; `--raise-cap` / `--reset-loops` are recorded
      and effective.
- [x] SIGTERM during a fake run with `sleep_s` leaves state unchanged, records
      `run.end{interrupted}` with its cost, and releases the lease.
- [x] `wt queue add` refuses a non-accepted spec, an unknown project and a spec with an
      active campaign; writes only with `--yes`; `--json` emits `wt.queue.add.v1`.
- [x] `wt queue list --json` emits `wt.queue.v1` with lease, slots and items; `watch`
      renders without error for two intervals in a pilot run.
- [x] **End-to-end (internal):** temp repo as `wt_checkout` with `.wt/project.yaml` (two
      `sh` gates), an accepted spec, `FakeRunner` script whose first run writes the feature
      file and sets the spec `done` ⇒ `wt queue add --yes` + `wt queue run --until-idle`
      ⇒ `MERGED`; main contains the file; worktree removed; ledger contains `open,
      transition×n, run.start, run.end, commit, gate×2, cost, close`; `wt queue list --json`
      has no items.
- [x] **End-to-end (outbound):** same with an `outbox_targets` project and policy `pr`
      with the fake `gh` ⇒ `HANDED_OFF` with a URL.
- [x] **Park variants:** never-green gate ⇒ `loop_exhausted` after `loop_max` loops (with
      the triager returning `fix` each time); dirty main ⇒ `main_dirty`, then `resume`
      after cleaning ⇒ `MERGED`; `campaign_cap_usd 0.01` ⇒ `cost_cap` before the first
      spawn; conflicting base commit ⇒ `rebase_conflict`.
- [x] Completion, reference, agent-surface and mkdocs guards pass.
- [x] Full `uv run pytest` green; `python3 tools/spec_lint.py` exits 0.

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

### As shipped (deviations from the Design above, recorded 2026-10-25)

- **Loop counting.** A loop is counted exactly once, when the triager sends work back
  (`fix`, or a `retry_gate` beyond its allowance). A close that routes to `TRIAGING`
  (re-gate failure or the `spec-dod` synthetic gate) does not itself increment `loops`;
  the triage that follows does. This avoids double-counting one failure.
- **Human park timing.** `wt queue park --yes` parks immediately when no loop holds the
  lease. With a loop running it only records `park_requested`; the next tick parks the
  item before spawning anything if the request arrived between ticks, or right after the
  in-flight step is recorded if it arrived mid-step (`park.resume_to` then points past the
  finished step, e.g. `GATING` after an implementer run). `human` is a park reason.
- **Signals.** On SIGINT/SIGTERM the loop stops admitting and, when it leads its process
  group (started with `setsid`/`nohup` in its own session, or `start_new_session`), forwards
  SIGTERM to the group so running `claude` children stop; it then waits for the in-flight
  steps, records them with `run.end{interrupted: true}` (state unchanged, cost charged),
  and releases the lease. No SIGKILL of the group (that would kill the loop itself);
  the runner's own timeout remains the hard stop.
- **Step failures.** An exception inside any step (git, config, runner) parks the campaign
  with reason `open_failed` and a `detail` naming the step, instead of crashing the loop.
- **Human changes mid-step.** If a human moved the item (abandon/resume) while a step ran,
  the step's events are still appended and a `note` says the transition was skipped.
- **`item_lock` reentrancy** (SPEC-0171 module, changed here): per-campaign
  `threading.RLock` + depth counter; the `flock` is taken at depth 1 only, so
  `append_event` nests inside `item_lock`. Cross-process exclusion unchanged (tested).
- **Outbound admission** uses the portable file at the spec's frozen `target_spec_path`
  inside the target repo; a not-yet-exported outbound spec is refused with the export hint.
- `wt queue watch` has a hidden `--iterations N` for tests; `wt queue list` folds the park
  reason into the state cell to fit 80 columns, and never truncates campaign ids.
- Extra counters: `triage_retries`, `blocked_streak` (reset by `resume --reset-loops`).

### Test record (2026-10-25)

- **Automated:** `tests/test_agentic_queue.py` (26: `plan_step` table and pessimistic cap,
  human park request per state, `decide_triage` matrix, `_apply` over synthetic results —
  blocked×2, missing cost, triage fix/brief/reset, malformed retry→escalate, gate routing,
  close outcomes incl. synthetic gate and park de-duplication, step error, human-moved
  item — FIFO admission by slots, lease refusal), `tests/test_agentic_queue_cli.py` (8:
  add dry-run/envelope/refusals/`--force`/outside-repo, list shape+filters, park/resume/
  abandon dry-runs and errors, help), `tests/test_agentic_e2e.py` (10: internal → MERGED
  with the full ledger trail and commit trailer; outbound `pr` → HANDED_OFF with fake `gh`;
  `loop_exhausted`; `main_dirty` then `resume` → MERGED; `cost_cap` before any spawn then
  `--raise-cap`; `rebase_conflict`; immediate park + abandon; deferred park both timings;
  `watch`; SIGTERM against a real `wt queue run` process), reentrancy test in
  `tests/test_agentic_store.py`. Full suite in the worktree: **1449 passed, 1 skipped,
  0 failed**; completion, reference, agent-surface and mkdocs guards included.
- **Manual (scratch clone of this checkout, tmp dir):** committed a scratch
  `.wt/project.yaml` (gates: `spec_lint --write-ledger && spec_lint`, `uv run pytest
  tests/test_specs.py`; setup `uv sync`) and an accepted scratch spec; fake implementer
  writes a file and sets the spec `done`. `wt queue add SPEC-0179 --yes` then `wt queue run
  --until-idle`: 4 ticks, 7.6 s wall, `MERGED` — ledger: open, admitted, opened, run.start,
  run.end ($0.42), cost, commit, implemented, gate #1 ok, gates_green, close-time snapshot
  commit (the regenerated ledger), gate #2 ok on the rebased tree, close, closed. Clone
  `main` has the two commits and a clean tree; the worktree was removed. `wt queue list
  --all` shows `MERGED $0.42/10`.
- **Not done here (SPEC-0177):** a real `claude` run through the loop.

## Clock Log

Internal work: clock on IDEA-023 with `wt idea clock-in` / `clock-out`.

## Rollout / migration

Nothing runs until a human runs `wt queue add --yes` and starts `wt queue run`. Existing
commands are unchanged.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

_None — decisions locked above._
