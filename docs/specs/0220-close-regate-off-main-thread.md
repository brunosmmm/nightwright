---
id: SPEC-0220
title: "Close re-gate runs off the runner main thread"
status: accepted
owner: user
created: 2026-10-25
updated: 2026-10-25
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0169
tags: [agentic, runner, queue, concurrency, merge]
source_idea: IDEA-093
depends_on: [SPEC-0213]
---

## Context

Child of epic [SPEC-0169](./0169-mini-agentic-workflow-queue-campaigns-headless.md),
promoted from bug `IDEA-093` ("Close step re-gate blocks the runner tick").

[SPEC-0213](./0213-nonblocking-queue-ticks.md) moved the agent steps onto a persistent
`StepPool`, so a slow implementer or gate no longer blocks the tick. It kept CLOSING serial
on the main thread on purpose (its Non-goals, and Decision item 4), because the close step
([SPEC-0175](./0175-close-step-rebase-re-gate-ff-only-merge-or-pr.md)) touches the main
checkout. Most of a close, though, never touches it. The spec guard (SPEC-0186), the DoD
audit, the snapshot commit, the rebase (with the
[SPEC-0191](./0191-deterministic-rebase-resolver-for-generated-files.md) regenerate
resolver) and the re-gate all run inside the campaign worktree. The re-gate is the full gate
set (`uv run pytest -q -x` for wt), about 3 minutes. While it runs the main thread does
nothing else: no harvest, no admission, no opening, no WAITING promotion. On 2026-10-25 a
ready item with 2 of 5 slots free waited about 4 minutes while another campaign closed. The
`ready_not_admitted` watchdog (`ready_grace_s` 60 s) fires on exactly that.

## Goals / Non-goals

**Goals**
- A close re-gate never delays admission, opening, harvest, or another campaign's
  transitions.
- Two campaigns can be in the worktree phase of a close at the same time. Only the step that
  writes the main checkout (merge) is serialized.
- The SPEC-0175 safety rules stay as they are. The tree that is merged is the exact tree that
  was gated. A dirty or off-base main checkout is never touched. Nothing is force-pushed.
- Keep: the SPEC-0194/0213 drain (in-flight closes are drained before a re-exec), the
  SPEC-0186 spec guard, park reasons and `resume_to`, ledger event shapes (additive only),
  `wt queue tick` single-shot determinism, `events.runner_busy`.

**Non-goals**
- Running OPENING concurrently. It stays on the main thread.
- Running merge phases concurrently, or merging without the base check.
- Changing the close algorithm's order or its outcomes (TRIAGING / PARKED / MERGED /
  HANDED_OFF) beyond the split itself.
- Changing the watchdog rules in `events.py`. The fix removes the cause.

## Decision

Split `close_campaign` into two phases:

- **(a) Worktree phase.** `prepare_close(cfg, state, project_cfg)` runs the spec guard, the
  spec DoD, `commit_all`, target capture, rebase (with regenerate resolution), the re-gate,
  and for `pr`/`draft-pr` the push and `gh pr create`. It runs as an in-flight close step
  on the `StepPool`, like a gate step. It returns a `PreparedClose`.
- **(b) Merge phase.** `finish_close(...)` runs on the main thread inside `_apply` when the
  flight is harvested. For `merge` it re-checks the main checkout (on base, tracked-clean)
  and checks that the base sha still equals the sha the worktree phase rebased onto. Then it
  does the ff-only merge, `post_cmds`, `push_after_merge` and cleanup. For PR policies it
  only records the close event and runs cleanup. If base moved, the campaign stays CLOSING
  and the next plan resubmits the worktree phase. That counts against the existing
  `base_moving` budget (`MAX_CLOSE_ATTEMPTS = 2`), and once the budget is spent the campaign
  parks `base_moving`.

Merge phases are serialized because only the main thread runs them, one harvested flight at
a time, and opening also runs on that thread. A single-shot `tick()` (throwaway pool) runs
its closes one at a time, worktree phase then merge phase. Its results match today's.

## Design

### `close.py`: split, same rules

- `PreparedClose(frozen dataclass)`: `outcome: CloseOutcome | None` (set when the worktree
  phase already decided: TRIAGING on spec guard / DoD / red re-gate, or a PARK such as
  `rebase_conflict`, `open_failed` (target), `gh_failed`), plus `target`, `target_sha`,
  `head` (worktree HEAD after the rebase), `gate_run`, `seq`, `policy`, `pr_url`, `events`.
- `prepare_close` does steps 0–5 of today's `close_campaign`, plus step 7's push and PR for
  PR policies. Choice: the PR push and `gh` calls are network I/O that never touch the main
  checkout, so they leave the main thread too. Target capture (`rev_parse(root, base)`, and
  `fetch` for PR policies) reads or updates shared refs only. Git's ref locks already
  serialize that against the main thread's merges, as the SPEC-0213 concurrency note
  describes.
- `finish_close(cfg, state, project_cfg, prepared, *, attempts, max_attempts)` →
  `CloseOutcome`. For `merge`, in this order: main checkout on base (`main_not_on_base`
  park), tracked-clean (`main_dirty` park), `rev_parse(root, base) == prepared.target_sha`.
  If base moved: `attempts < max_attempts` ⇒ a `CloseOutcome("CLOSING", retry=True)`,
  otherwise a `base_moving` park. Then `ff_merge`. A refused ff-merge is handled like a moved
  base. A merged sha other than `prepared.head` ⇒ `base_moving` park (as today). Then
  `post_cmds`, `push_after_merge`, the `close` event (same payload, including `target_sha`),
  and `_cleanup`.
- `close_campaign` stays as a thin serial wrapper (`prepare_close` then `finish_close`, with
  the retry loop) for existing callers and tests. Its behaviour does not change, and the
  `before_merge` hook still runs between the two phases.
- `CloseOutcome` gains `retry: bool = False`. It is additive and never persisted.

### `queue.py`: close as an in-flight step

- **Planning.** `plan_step` still returns `Step("close")` for CLOSING. In the planning loop,
  with a persistent pool, a close goes into `pending_agent` as
  `(st, step, None, None, 0.0, None)`, like a gate. `_submit` submits
  `_do_prepare_close(ctx, st)` as one task. The `started` row reads `action: "close"`. The
  existing in-flight skip means a campaign never has two closes, or a close and anything
  else, at once. Slot accounting needs no change: the campaign is CLOSING on disk and in
  `pool.ids()`.
- **Harvest / merge phase.** `_apply` for `kind == "close"` receives the `PreparedClose`.
  If `outcome` is set, it is applied exactly as today. Otherwise `_apply` calls
  `finish_close` on the main thread, under the same locked section that writes the state,
  and applies the resulting outcome. `_harvest` handles one flight at a time, so two merge
  phases never overlap. A worker exception becomes the existing `open_failed` park with
  `resume_to: CLOSING` ("close step failed: …"), and a `report.errors` entry is added. This
  is the current `_do_close` error path, reached through the generic harvest exception
  branch with the step kind kept as `close`.
- **Retry budget across ticks.** The attempt count lives in the campaign state as
  `closing: {attempts, target_sha}` (additive key). It is set to `{attempts: 0}` on every
  transition into CLOSING (`review_pass`, `waived`, resume from a park, WAITING → CLOSING).
  Each harvested worktree phase increments it, and it is removed on any transition out of
  CLOSING. A `retry` outcome keeps the state CLOSING (row `reason: "base_moved"`, plus a
  `finding` event, `persona: close`, `severity: info`, `gate: base`, title "base moved
  during re-gate; redoing close"). The harvest-then-plan order of `_tick` resubmits the
  worktree phase in the same tick. Resuming a `base_moving` park re-enters CLOSING, which
  resets the budget. This is what happens today when a resume starts a new `close_campaign`
  call. `counters.close_attempts` still counts each applied close step.
- **Single shot.** `pool is None` (`wt queue tick`, tests): the tick plans closes into
  `pending_close` as today. After the agent steps are harvested, it runs each close by
  submitting its worktree phase, waiting for it, and harvesting it (merge phase), one
  campaign after the other, in today's order. A `retry` outcome there loops within the tick
  until the budget is spent, so the single-shot results match the old in-call loop.
- **Interrupt.** A worker that finishes while `stop` is set (signal) has its result
  discarded at harvest. No gate event, no merge, no transition. The state stays CLOSING,
  `closing.attempts` is not incremented, and the row reads `reason: "interrupted"`.
  Rationale: a SIGTERM forwarded to the process group kills the re-gate's pytest, and that
  failure must not send the campaign to TRIAGING. Redoing is safe. The DoD and guard are
  pure, `commit_all` is a no-op on a clean tree, rebasing onto the same target is a no-op,
  and the re-gate simply runs again. A process killed during the worktree phase leaves the
  same state (CLOSING, worktree possibly mid-rebase). `git.rebase` already aborts on
  conflict. A rebase left in progress by a hard kill is aborted at the start of
  `prepare_close` (`rebase --abort` when `.git/rebase-merge` or `rebase-apply` exists), so
  the redo starts clean.
- **Drain and re-exec (SPEC-0194 / 0213).** In-flight closes are flights, so `_drain` joins
  them and applies each one, merge phase included, before `stop code_changed` and `_EXEC`.
  No new close is submitted once a code change is detected. A merge done during the drain is
  fine because the re-exec follows anyway.

### Watchdog (`events.py`), no code change

- `runner_busy` already counts any active CLOSING campaign as busy. That stays correct: the
  campaign is CLOSING on disk for the whole worktree phase.
- `ready_not_admitted` fired because admission stalled for minutes. With the re-gate off the
  main thread, admission happens within one inter-tick wait (≤ `poll_interval_s`) even
  mid-close. The merge phase is seconds plus `post_cmds`. Choice: no "closing ⇒ suppress"
  rule. It would also hide a real stall (the IDEA-081 class). A test pins the observable
  property: the watchdog stays silent while a close re-gate runs and a ready item is waiting.

### Visible behaviour

- `wt queue run` / runner journal: a close shows `started close in CLOSING`, and its result
  row (MERGED / TRIAGING / PARKED / `base_moved` / `interrupted`) appears in a later tick or
  in the drain record, like other steps. `wt queue tick` output is unchanged.
- Ledger: same event types and payloads. Additions: the `finding` row on a base-moved retry,
  and the `closing` state key.

## Alternatives considered

- **Whole close on the pool, with a merge lock.** It hides a main-checkout write inside a
  worker, so the "Python on the main thread decides every transition" guarantee
  (SPEC-0176) would hold only by convention. It also races opening, which uses the main
  checkout on the main thread.
- **Separate merger process.** That is a second writer of campaign state outside the lease,
  the same objection SPEC-0213 raised against processes per campaign.
- **Suppress `ready_not_admitted` while CLOSING.** It silences the symptom and leaves
  the ready item waiting.
- **Run the re-gate on the merged main tree after the merge.** That breaks the "gate the
  exact tree you merge before you merge it" rule from SPEC-0175.

## Acceptance criteria

- [ ] With campaign A's close re-gate held (blocking gate command), the tick returns
      promptly, and a ready QUEUED item B is admitted and opened, and moves through its own
      transitions, before A's re-gate finishes.
- [ ] Two campaigns can be in the worktree phase concurrently (both re-gates observed
      running at once), and their merge phases run one after the other on the main thread.
      Both reach MERGED, the second one after a `base_moved` retry.
- [ ] Base moved during the re-gate: the merge phase does not merge, the campaign stays
      CLOSING with a `base_moved` row and finding, and the worktree phase is resubmitted onto
      the new base and merges. When base moves on every attempt, the campaign parks
      `base_moving` (`resume_to: CLOSING`) after `MAX_CLOSE_ATTEMPTS`, and a resume resets
      the budget.
- [ ] The merge phase still parks `main_dirty` / `main_not_on_base` (`resume_to: CLOSING`)
      when the main checkout changes during the worktree phase. The spec guard, spec DoD,
      `rebase_conflict`, regenerate resolution and red re-gate outcomes are unchanged.
- [ ] A code change detected while a close is in flight drains it (merge phase applied)
      before `stop code_changed` and `_EXEC`, and no close is submitted after detection.
- [ ] A stop (signal) during the worktree phase leaves the campaign CLOSING with no gate
      event and no transition, and the next run redoes the close and merges. A worktree left
      mid-rebase is aborted and redone.
- [ ] `ready_not_admitted_items` stays empty, and `runner_busy` is true, while a close
      re-gate runs with a ready item and a free slot.
- [ ] `wt queue tick` (no pool) gives the same transitions and ledger events as before for
      the existing close and e2e suites. Ledger event shapes are unchanged apart from the
      additions listed.
- [ ] `uv run pytest` passes; `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests:** new `tests/test_agentic_close_inflight.py`, all offline, built with
  FakeRunner and tmp git repos (helpers from `tests/test_agentic_e2e.py` /
  `tests/test_agentic_nonblocking.py`). The held re-gate is a project gate command that
  blocks while a `hold-<spec>` marker file exists, so "A is still re-gating" is
  deterministic: the GATING step passes because the marker is created only once A reaches
  CLOSING.
  - `test_admission_while_close_regate_runs`: slots 2 and a persistent pool. A reaches
    CLOSING and its re-gate is held. B is queued and ready. The ticks return in < 5 s, B is
    admitted, opened, implements and reaches GATING/REVIEWING while A is still CLOSING with
    one `started close`. After release, A → MERGED.
  - `test_two_worktree_phases_concurrent_merges_serial`: A and B both CLOSING. Both held
    gates are seen running at once (marker files written by the gate). On release, one merges
    first, the other gets `base_moved`, redoes and merges. `main` ends ff-only with both.
  - `test_base_moved_during_regate_redoes_then_merges`: a commit lands on `main` while A's
    re-gate is held. On release: no merge, `base_moved` row and finding, a second
    `started close`, and the second gate run's tree contains the new base commit. Then
    MERGED with `close.target_sha` = the new base.
  - `test_base_moving_every_attempt_parks`: base moves during every re-gate ⇒ PARKED
    `base_moving`, `resume_to: CLOSING`, after 2 attempts. `wt queue resume` resets
    `closing.attempts`, and the next close merges.
  - `test_main_dirtied_during_regate_parks_main_dirty` and `..._off_base_parks`.
  - `test_reexec_drains_inflight_close`: patched `_EXEC` checks, when called, that A is
    MERGED, no `wt-step` thread is alive, and the journal ends `tick(drain) → stop
    code_changed`.
  - `test_stop_during_worktree_phase_leaves_closing`: `stop` set while the gate is held. At
    harvest: state CLOSING, no `gate` event, row `interrupted`. A fresh tick merges.
  - `test_redo_aborts_leftover_rebase`: the worktree is left with a rebase in progress, and
    the next close aborts it and merges.
  - `test_watchdog_quiet_while_closing`: during a held re-gate with B ready and a slot free
    (`ready_grace_s: 0`), after a tick, `ready_not_admitted_items == []` and
    `stuck_items`/digest report the runner busy.
- **Existing suites unchanged:** `tests/test_agentic_close.py` (through the
  `close_campaign` wrapper, including `before_merge`-hook base-moving tests),
  `tests/test_agentic_nonblocking.py`, `tests/test_agentic_reexec.py`,
  `tests/test_agentic_events.py`, `tests/test_agentic_e2e.py`.
- **Sabotage checks:** run the new suite after each of these (restoring in between) and
  confirm it fails. Run closes back on the main thread. Drop the base sha re-check in
  `finish_close`. Drop the `stop` discard. Record the outcomes in the spec.
- **Regression guard:** full `uv run pytest`.
- **Manual verification:** after the merge, the live runner re-execs (SPEC-0194). On the
  next real close, `wt queue runner-log` shows `started close` followed by other campaigns'
  rows before the close's result row.

## Clock Log

Built by the agentic loop or the human lane; idea bookkeeping via `wt spec reconcile --apply`.

## Rollout / migration

No data migration. A campaign already CLOSING when the new code starts has no `closing` key.
It is treated as `{attempts: 0}`. Merging this changes the close code itself. The live
runner drains its in-flight steps (including a close already running the old code), then
re-execs into the new code.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

_None — decisions locked above._
