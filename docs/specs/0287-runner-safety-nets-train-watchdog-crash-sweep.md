---
id: SPEC-0287
title: "Runner safety nets: train watchdog, crash-sweep harvest, terminal tmp, regenerate renames"
status: done
kind: feature
owner: user
created: 2026-10-27
source_idea: IDEA-359
parent: SPEC-0169
tags: [agentic, runner, queue, close, reliability, review-follow-up]
---

## Context

This spec comes from the second triage of the accumulated `wtfug:` review follow-ups
(campaigns of SPEC-0253 and later). Each finding kept here was re-checked against `main`
on 2026-10-27 and is still true. Each one is a runner safety net with a hole: a stall the
watchdog cannot see, a finished step whose result is silently dropped, temp residue that
never goes away, or an undeclared file change that gets into a close commit.

Kept findings:

- **IDEA-359** (SPEC-0260), three reviewers raised the same point:
  - `wtfu:95c3c74072db`, `wtfu:e63e36c623ad`, `wtfu:7e887190ee79`: *A stale
    `closing.queued_behind` hides a waiter from the watchdog.* `events.stuck_items` skips
    every CLOSING campaign that has `closing.queued_behind`. It never checks that the named
    head is still CLOSING. Only `queue._train_head` clears the field, and only when the
    waiter reaches close planning as head. A waiter whose planning never gets there (a
    draining tick, a step that keeps failing to plan) is never reported stuck.
- **IDEA-365** (SPEC-0262)
  - `wtfu:b20f760441b8` (with the test gap `wtfu:5f2c333d5578`): *The crash sweep waits for
    in-process tasks, then discards the ones that finished during the wait.*
    `queue._crash_sweep` harvests once, then loops until `unfinished_in_process()` is
    empty. A gate, resolve or write_spec task that ends inside that loop leaves the
    unfinished list. Nobody applies it, and it is not in `abandoned`, so the crash record
    does not mention it either.
- **IDEA-354** (SPEC-0253)
  - `wtfu:ab99c9bddc2c`: *HANDED_OFF campaigns never get `tmp/` removed and are excluded
    from the sweep.* `queue._transition` clears `tmp/` only for MERGED and ABANDONED.
    `store.sweep_campaign_tmp` runs only over non-terminal campaigns. Residue of a
    handed-off campaign stays on the agentic dir's filesystem for good, and it counts
    toward the SPEC-0253 temp-pressure check that pauses admission.
- **IDEA-366** (SPEC-0266)
  - `wtfu:fead7a0a6a41`: *Staged-scope check uses rename detection, so an undeclared
    deletion can hide behind a rename.* `close._regenerate_after_rebase` computes `extra`
    from `git diff --cached --name-only -z`, which follows `diff.renames`. A regenerate
    command that stages `git mv undeclared declared` lists only `declared`. The deletion of
    the undeclared path then gets into the "regenerate generated files" commit.

Dropped from these ideas and from the other in-scope groups (IDEA-347..389): most were
already fixed by later specs. Examples: gc base-branch, detached-HEAD and mid-rebase
guards (SPEC-0275 rounds); unique run ids (`_ledger_run_max`); atomic gate-seq claims;
pinned duplicate-id writes (SPEC-0267); state checks in `_mark_handed_off` (SPEC-0280). The
rest were test-only, cosmetic, speculative, or a documented design limit.

## Goals / Non-goals

**Goals**
- The stuck watchdog reports a train waiter whose head is gone.
- A runner crash applies, or at least records, every in-process step result that finished
  within the bound.
- No terminal campaign keeps `tmp/` residue, including existing HANDED_OFF ones.
- The regenerate scope check sees every path a staged rename touches.

**Non-goals**
- Changing close-train ordering (SPEC-0260) or the crash bound (`_STOP_BOUND_S`).
- Killing leftover process-group members after a normal `claude` exit.
- Normalizing declared regenerate paths (`./x` versus `x`).

## Decision

Four local fixes, one per hole. The watchdog trusts `queued_behind` only while the head is
CLOSING. The crash sweep harvests again after its wait. Every terminal transition clears
`tmp/`, and `wt queue gc` removes residue left before this change. The staged-scope diff
runs with `--no-renames`.

## Design

1. **Watchdog** (`src/wt/agentic/events.py` `stuck_items`): build
   `closing_ids = {s["id"] for s in rows if s.get("state") == "CLOSING"}` from the same
   `store.list_campaigns(cfg)` call. Skip a campaign for `queued_behind` only when
   `closing.queued_behind in closing_ids` and it is not the campaign's own id. Otherwise the
   normal GATING/CLOSING clock applies, so a stale waiter is reported after
   `max(stuck_after_s, gate_budget_s)`.
2. **Crash harvest** (`src/wt/agentic/queue.py` `_crash_sweep`): after the wait loop, run
   one more guarded harvest (`_harvest(pool, rep2, guard=True)`). Journal it as a
   `drain: true` tick when it applied steps or saw errors, exactly like the first harvest.
   Then recompute `in_process = pool.unfinished_in_process()`, so `abandoned` lists only
   tasks that are still unfinished. Wrap the harvest in the same
   `try/except → harvest_error` journal.
3. **Terminal tmp** (`src/wt/agentic/queue.py` `_transition`): clear `tmp/` when
   `to in TERMINAL_STATES`, not only for MERGED/ABANDONED. **Residue** (`plan_gc` /
   `apply_gc`, same file): for every terminal campaign, honouring `--project`, whose
   `store.campaign_tmp` exists and is not a symlink, add a row `{"kind": "tmp", "path": …,
   "action": "remove", "why": "<STATE> campaign temp residue"}` (via `_gc_row`, `repo` = the
   campaign's root or `None`). `apply_gc` removes those rows with
   `store.clear_campaign_tmp`, before the worktree phase. An error goes on the row, as for
   the other rows. `cli_queue._gc_table` renders the kind like the others. The JSON schema
   name stays `wt.queue.gc.v1`, because adding a row kind is additive.
4. **Regenerate renames** (`src/wt/agentic/close.py` `_regenerate_after_rebase`): compute
   `extra` from `git diff --cached --name-only --no-renames -z`. A staged rename then lists
   both the deleted source and the added destination, and the undeclared source makes the
   step refuse and park `regenerate_failed` as today.

## Alternatives considered

- **Clear `queued_behind` whenever a CLOSING campaign is planned but is not the head.**
  Rejected. The watchdog has to be right even when the runner never plans the waiter,
  which is the failure case itself.
- **Make the crash sweep record finished tasks as `abandoned` instead of applying them.**
  Rejected. The normal drain applies results that finish within the bound, and the crash
  path should match it.
- **Sweep terminal campaigns on every tick.** Rejected. It reads every terminal ledger on
  each 2 s poll. The transition fix plus a one-shot gc is enough.

## Acceptance criteria

- [x] A CLOSING campaign whose `closing.queued_behind` names a campaign that is not CLOSING
      (MERGED, missing, or itself) is reported by `stuck_items` once its clock passes the
      limit. A waiter behind a live CLOSING head is still not reported.
- [x] In `_crash_sweep`, an in-process task that finishes during the bounded wait is applied
      (its state transition and ledger events land) and is not listed in `abandoned`. A
      task still running at the bound is listed, as before.
- [x] A transition to HANDED_OFF removes the campaign's `tmp/`.
- [x] `wt queue gc` lists a `tmp` row for a terminal campaign that still has `tmp/`, and
      `--yes` removes the dir. A live campaign's `tmp/` never gets a row.
- [x] A regenerate command that stages a rename from an undeclared path onto a declared one
      makes `_regenerate_after_rebase` return the "staged undeclared files" detail naming
      the source path. No commit is made.

## Test plan

- **Automated tests:**
  - `tests/test_agentic_close_train.py::test_stale_queued_behind_is_reported_stuck`: write
    a CLOSING state with `queued_behind` naming a MERGED campaign and an old
    `entered_at`/transition. `events.stuck_items` lists it. A twin whose head is a live
    CLOSING campaign is not listed.
  - `tests/test_agentic_exit_paths.py::test_crash_sweep_applies_task_finished_during_wait`:
    a pool with a hookless flight whose future completes about 0.3 s into the wait (a
    `threading.Event` released by a timer; bound 5 s). Assert that `_crash_sweep` returns
    without it in `abandoned`, and that the campaign's state/ledger show the applied step.
    Keep the existing never-finishing case green.
  - `tests/test_agentic_tmp_isolation.py::test_handed_off_transition_clears_tmp`: create
    `tmp/run-001-x/f`, call `_transition(..., "HANDED_OFF", ...)`, and assert `tmp/` is
    gone.
  - `tests/test_agentic_queue_gc.py::test_gc_removes_terminal_tmp_residue`: a HANDED_OFF
    campaign with `tmp/` and a live campaign with `tmp/`. `plan_gc` has exactly one `tmp`
    row, for the terminal one. `apply_gc` removes it (`done: True`) and leaves the live
    one.
  - `tests/test_agentic_close_regenerate.py::test_regenerate_rename_of_undeclared_path_refuses`:
    a regenerate command `git mv undeclared.txt gen.txt` with `gen.txt` declared. The
    return value names `undeclared.txt`, HEAD is unchanged, and `undeclared.txt` is still
    tracked at HEAD.
- **Sabotage checks.** Each one must fail its test; record the result in the spec:
  - restore the unconditional `queued_behind` skip
  - drop the second harvest
  - restore the MERGED/ABANDONED-only clear
  - skip `tmp` rows in `apply_gc`
  - remove `--no-renames`
- **Regression guard:** run the full `uv run pytest`. The close-train, exit-paths,
  tmp-isolation, gc, regenerate and watchdog suites (`test_agentic_watchdog_gaps.py`,
  `test_agentic_events.py`) must stay green.
- **Manual verification:** after merge, `wt queue gc` (dry run) on the live data dir lists
  the `tmp` rows of existing HANDED_OFF campaigns, if any. Record the count.

### Test record (2026-10-27)

- New tests, all passing: `test_stale_queued_behind_is_reported_stuck`,
  `test_crash_sweep_applies_task_finished_during_wait`, `test_handed_off_transition_clears_tmp`,
  `test_gc_removes_terminal_tmp_residue`, `test_regenerate_rename_of_undeclared_path_refuses`.
- Sabotage checks. Each one was applied on its own and each made its test fail (`1 failed`):
  unconditional `queued_behind` skip; no second harvest; MERGED/ABANDONED-only clear;
  `tmp` rows skipped in `apply_gc`; `--no-renames` removed. The files were restored afterwards.
- Targeted suites (close-train, exit-paths, tmp-isolation, gc, regenerate,
  watchdog-gaps, events): 184 passed.
- Gates: `python3 tools/spec_lint.py --write-ledger && python3 tools/spec_lint.py` exits 0;
  full `uv run pytest -q -x`: 3527 passed, 3 skipped (after one missing-head waiter test in `test_agentic_ping.py` was updated, see As shipped).
- Manual: `uv run wt queue gc --json` (dry run, branch code, live data dir) gave `written: false`,
  `tmp_removable: 0`, no errors. No HANDED_OFF residue exists today. The conductor still runs the
  post-merge `wt queue gc` once, as the Rollout section says.

### As shipped (2026-10-27)

- `test_stuck_items_ignores_train_waiter_and_uses_head_since` used to put its waiter behind a
  missing head (`"x"`). Under this spec that waiter is now reported stuck. The test now puts it
  behind the live CLOSING campaign `…-0002`, so it still checks the train-waiter skip.
  `test_agentic_ping.py::test_stuck_closing_measured_from_latest_gate_event` had the same
  missing-head waiter. It now checks both cases: hidden behind a live CLOSING head, reported
  once the head is gone.
- `plan_gc` also returns `tmp_removable`, which is additive like the row kind. The text
  summary mentions tmp dirs only when there are some, so the existing output stays the same.
  A `tmp` row with no repo root has `repo: null`. Other rows always have a root.
- After `store.clear_campaign_tmp`, `apply_gc` checks that the dir is gone, because the removal
  ignores errors. A dir that is still there marks the row `done: false` with an error.

## Rollout / migration

`wt queue gc --yes` clears tmp residue left by earlier HANDED_OFF campaigns. The conductor
runs it once after the merge.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

- (none)

## Clock Log
