---
id: SPEC-0220
title: "Close re-gate runs off the runner main thread"
status: done
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
  writes the main checkout or the remote (finish: merge, push, PR) is serialized.
- The SPEC-0175 safety rules stay as they are. The tree that is merged is the exact tree that
  was gated. A dirty or off-base main checkout is never touched. Nothing is force-pushed.
- Keep: the SPEC-0194/0213 drain (in-flight closes are drained before a re-exec), the
  SPEC-0186 spec guard, park reasons and `resume_to`, ledger event shapes (additive only),
  `wt queue tick` single-shot determinism, `events.runner_busy`.

**Non-goals**
- Running OPENING concurrently. It stays on the main thread.
- Running finish phases concurrently, or merging without the base check.
- Changing the close algorithm's order or its outcomes (TRIAGING / PARKED / MERGED /
  HANDED_OFF) beyond the split itself.
- Changing the watchdog rules in `events.py`. The fix removes the cause.

## Decision

Split `close_campaign` into two phases:

- **(a) Worktree phase.** `prepare_close(cfg, state, project_cfg)` runs the spec guard, the
  spec DoD, `commit_all`, target capture, rebase (with regenerate resolution) and the
  re-gate. It has no external side effects beyond the campaign worktree and branch. It runs
  as an in-flight close step on the `StepPool`, like a gate step. It returns a
  `PreparedClose`.
- **(b) Finish phase.** `finish_close(...)` runs on the main thread inside `_apply` when the
  flight is harvested. It first re-reads the campaign from disk, and a park, abandon or
  resume made during the worktree phase wins: no merge, no push. For `merge` it re-checks
  the main checkout (on base, tracked-clean), checks that the base sha still equals the sha
  the worktree phase rebased onto, and checks that the worktree HEAD and the branch tip are
  still the gated head. Then it does the ff-only merge, `post_cmds`, `push_after_merge` and
  cleanup. For PR policies it does the push and `gh pr create`, records the close event and
  runs cleanup. If base or branch moved, the campaign stays CLOSING
  and the next plan resubmits the worktree phase. That counts against the existing
  `base_moving` budget (`MAX_CLOSE_ATTEMPTS = 2`), and once the budget is spent the campaign
  parks `base_moving`.

Finish phases are serialized because only the main thread runs them, one harvested flight at
a time, and opening also runs on that thread. A single-shot `tick()` (throwaway pool) runs
its closes one at a time, worktree phase then finish phase. Its results match today's.

## Design

### `close.py`: split, same rules

- `PreparedClose(frozen dataclass)`: `outcome: CloseOutcome | None` (set when the worktree
  phase already decided: TRIAGING on spec guard / DoD / red re-gate, or a PARK such as
  `rebase_conflict` or `open_failed` (target)), plus `target`, `target_sha`, `head`
  (worktree HEAD after the rebase), `gate_run`, `seq`, `policy`, `events`.
- `prepare_close` does steps 0–5 of today's `close_campaign` and nothing else. Target
  capture (`rev_parse(root, base)`, and `fetch` for PR policies) reads or updates shared
  refs only. Git's ref locks already serialize that against the main thread's merges, as
  the SPEC-0213 concurrency note describes.
- **PR push and `gh pr create` run in the finish phase (main thread).** Chosen over making
  the worktree phase idempotent with a `gh pr view` lookup. Reason: a stop can discard a
  worktree phase's result (see Interrupt), and a discarded worker that had already pushed
  and opened a PR would open a duplicate PR on the redo, or leave one the ledger never
  recorded. With the push and PR creation on the main thread, they happen only once the
  result is being applied, after the fresh-state check, and never inside a result that
  might be discarded. The cost is a few seconds of network I/O on the main thread per PR
  close. That is small next to the ~3 min re-gate that this spec removes, and it needs no
  new `gh` query surface. `gh_failed` parks therefore come from the finish phase.
- `finish_close(cfg, state, project_cfg, prepared, *, attempts, max_attempts)` →
  `CloseOutcome`. For `merge`, in this order:
  1. main checkout on base (`main_not_on_base` park);
  2. tracked-clean (`main_dirty` park);
  3. `rev_parse(root, base) == prepared.target_sha`;
  4. **gated-tree check:** `rev_parse(worktree, "HEAD") == prepared.head` and
     `rev_parse(root, branch) == prepared.head`, so what is merged is exactly what was gated.
     A mismatch (something committed to the worktree or moved the branch after the re-gate)
     is handled like a moved base: it is redone, counts against the same budget, and parks
     `base_moving` with detail "branch moved after re-gate" once the budget is spent.
     Choice: no new park reason, because `PARK_REASONS` stays as it is and the remedy is the
     same, a resume re-gates.

  If step 3 or 4 fails: `attempts < max_attempts` ⇒ a `CloseOutcome("CLOSING", retry=True)`,
  otherwise a `base_moving` park. Then `ff_merge`. A refused ff-merge is handled like a moved
  base. A merged sha other than `prepared.head` ⇒ `base_moving` park (as today). Then
  `post_cmds`, `push_after_merge`, the `close` event (same payload, including `target_sha`),
  and `_cleanup`. For `pr`/`draft-pr`: the gated-tree check (step 4, worktree HEAD only),
  then push, PR body, `gh pr create` (`gh_failed` park on error), the `close` event and
  `_cleanup`, as today's step 7.
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
- **Harvest / finish phase.** `_apply` handles `kind == "close"` and receives the
  `PreparedClose`. The order is:
  1. **Fresh-state gate (before any merge).** Re-read the campaign (`_fresh` /
     `store.read_state`). If the on-disk state is no longer CLOSING (a human parked it, or
     abandoned or resumed it elsewhere), `finish_close` is not called. The worktree phase's
     events are recorded and the row is `superseded_by_human`, as in the existing `finish()`
     path. If `park_requested` is set, `finish_close` is not called either: the campaign
     parks `human` with `resume_to: CLOSING` and the worktree-phase events attached. Today's
     `finish()` honours `park_requested` only after the step ran, and only for non-terminal
     targets, so a merge would already have happened. Here the check moves ahead of the
     merge. Rationale: a park or abandon is a human decision, and it must win over an
     autonomous merge that has not happened yet.
  2. If `outcome` is set, it is applied exactly as today.
  3. Otherwise `_apply` calls `finish_close` on the main thread and applies its outcome
     through `finish()`.

  `_harvest` handles one flight at a time, so two finish phases never overlap.
- **Worker exception.** `_harvest`'s exception branch must build the synthetic
  `StepResult` with the flight's own step kind, taken from `fl.tasks[0][0]`. Today it
  hardcodes `Step("implement")`, which for a close would apply implement rules. With the
  real kind, a close worker exception goes down `_apply`'s close error path: the existing
  `open_failed` park with `resume_to: CLOSING` ("close step failed: …"), plus a
  `report.errors` entry. The same fix makes a gate worker exception follow the gate path.
- **Retry budget across ticks.** The attempt count lives in the campaign state as
  `closing: {attempts, target_sha}` (additive key). `"closing"` is **added to the key
  whitelist that `_apply.finish()` copies** from the step's state into the fresh on-disk
  state (`counters, cost, repo, spec, …, close`). Without that, the increment would be lost
  and the budget never reached. Choice: a dedicated key rather than `counters`, because
  counters are cumulative for the whole campaign while this budget resets on every entry
  into CLOSING. It is set to `{attempts: 0}` on every transition into CLOSING
  (`review_pass`, `waived`, resume from a park, WAITING → CLOSING, config recovery), in
  `_transition`/`finish()`/`apply_resume` when `to == "CLOSING"` and the previous state was
  not CLOSING. Each applied worktree phase increments it, and it is removed on any
  transition out of CLOSING. A campaign that is CLOSING without the key (legacy, from
  before this change) is read as `{attempts: 0}`. A `retry` outcome keeps the state CLOSING
  (row `reason: "base_moved"`, plus a
  `finding` event, `persona: close`, `severity: info`, `gate: base`, title "base moved
  during re-gate; redoing close"). The harvest-then-plan order of `_tick` resubmits the
  worktree phase in the same tick. Resuming a `base_moving` park re-enters CLOSING, which
  resets the budget. This is what happens today when a resume starts a new `close_campaign`
  call. `counters.close_attempts` still counts each applied close step.
- **Single shot.** `pool is None` (`wt queue tick`, tests): the tick plans closes into
  `pending_close` as today. After the agent steps are harvested, it runs each close by
  submitting its worktree phase, waiting for it, and harvesting it (finish phase), one
  campaign after the other, in today's order. A `retry` outcome there loops within the tick
  until the budget is spent, so the single-shot results match the old in-call loop.
- **Interrupt.** A worker that finishes while `stop` is set (signal) has its result
  discarded at harvest. No gate event, no merge, no transition. The state stays CLOSING,
  `closing.attempts` is not incremented, and the row reads `reason: "interrupted"`.
  Rationale: a SIGTERM forwarded to the process group kills the re-gate's pytest, and that
  failure must not send the campaign to TRIAGING. Redoing is safe. The DoD and guard are
  pure, `commit_all` is a no-op on a clean tree, rebasing onto the same target is a no-op,
  and the re-gate simply runs again. Nothing outside the worktree and its branch happened:
  no merge, push or PR is ever made inside a result that can be discarded. A process killed during the worktree phase leaves the
  same state (CLOSING, worktree possibly mid-rebase). `git.rebase` already aborts on
  conflict. A rebase left in progress by a hard kill is aborted at the start of
  `prepare_close` (`rebase --abort` when `.git/rebase-merge` or `rebase-apply` exists), so
  the redo starts clean.
- **Drain and re-exec (SPEC-0194 / 0213).** In-flight closes are flights, so `_drain` joins
  them and applies each one, finish phase included, before `stop code_changed` and `_EXEC`.
  No new close is submitted once a code change is detected. A merge done during the drain is
  fine because the re-exec follows anyway.

### Watchdog (`events.py`), no code change

- `runner_busy` already counts any active CLOSING campaign as busy. That stays correct: the
  campaign is CLOSING on disk for the whole worktree phase.
- `ready_not_admitted` fired because admission stalled for minutes. With the re-gate off the
  main thread, admission happens within one inter-tick wait (≤ `poll_interval_s`) even
  mid-close. The finish phase is seconds plus `post_cmds`. Choice: no "closing ⇒ suppress"
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
- **PR push and `gh pr create` in the worktree phase, made idempotent through
  `gh pr view <branch>`.** Rejected. It adds a remote query whose failure modes (auth,
  rate limit, a stale closed PR) are new park paths, and a pushed-but-unrecorded branch
  would still exist after a discard. Moving those calls to the finish phase removes the
  hazard rather than patching it.
- **Suppress `ready_not_admitted` while CLOSING.** It silences the symptom and leaves
  the ready item waiting.
- **Run the re-gate on the merged main tree after the merge.** That breaks the "gate the
  exact tree you merge before you merge it" rule from SPEC-0175.

## Acceptance criteria

- [x] With campaign A's close re-gate held (blocking gate command), the tick returns
      promptly, and a ready QUEUED item B is admitted and opened, and moves through its own
      transitions, before A's re-gate finishes.
- [x] Two campaigns can be in the worktree phase concurrently (both re-gates observed
      running at once), and their finish phases run one after the other on the main thread.
      Both reach MERGED, the second one after a `base_moved` retry.
- [x] Base moved during the re-gate: the finish phase does not merge, the campaign stays
      CLOSING with a `base_moved` row and finding, and the worktree phase is resubmitted onto
      the new base and merges. When base moves on every attempt, `closing.attempts` persists
      across ticks and the campaign parks `base_moving` (`resume_to: CLOSING`) after exactly
      `MAX_CLOSE_ATTEMPTS`. Every entry into CLOSING (resume, WAITING → CLOSING) resets the
      budget, and a legacy CLOSING campaign without the key starts at 0.
- [x] The finish phase still parks `main_dirty` / `main_not_on_base` (`resume_to: CLOSING`)
      when the main checkout changes during the worktree phase, and redoes the close when the
      worktree HEAD or branch tip no longer equals the gated head. The spec guard, spec DoD,
      `rebase_conflict`, regenerate resolution and red re-gate outcomes are unchanged.
- [x] A park requested, or an abandon or park applied, during the worktree phase wins: no
      ff-merge, push or PR happens. A requested park parks `human` with `resume_to: CLOSING`,
      and a state changed on disk gives `superseded_by_human`.
- [x] A code change detected while a close is in flight drains it (finish phase applied)
      before `stop code_changed` and `_EXEC`, and no close is submitted after detection.
- [x] A stop (signal) during the worktree phase leaves the campaign CLOSING with no gate
      event and no transition, and the next run redoes the close and merges. A worktree left
      mid-rebase is aborted and redone. For `pr`/`draft-pr`, push and `gh pr create` run only
      in the finish phase, so a discarded worktree phase never pushes or opens a PR, and a
      redo opens exactly one PR. A close worker exception parks `open_failed`
      (`resume_to: CLOSING`) through the close path, not the implement path.
- [x] `ready_not_admitted_items` stays empty, and `runner_busy` is true, while a close
      re-gate runs with a ready item and a free slot.
- [x] `wt queue tick` (no pool) gives the same transitions and ledger events as before for
      the existing close and e2e suites. Ledger event shapes are unchanged apart from the
      additions listed.
- [x] `uv run pytest` passes; `python3 tools/spec_lint.py` exits 0.

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
  - `test_worktree_commit_after_regate_redoes`: a commit is made in A's worktree after the
    held gate has passed but before harvest, so the HEAD differs from `prepared.head`. Result:
    no merge, `base_moved` row, a redo that gates the new HEAD, then MERGED with
    `merged_sha` = the new HEAD.
  - `test_park_requested_during_worktree_phase_blocks_merge`: `wt queue park` (with the
    lease held, so `park_requested`) while the re-gate is held. On release: PARKED `human`,
    `resume_to: CLOSING`, `main` unchanged (no ff-merge), and the gate event is recorded.
  - `test_abandon_during_worktree_phase_blocks_merge`: the state is set ABANDONED (and,
    separately, PARKED with no lease) on disk mid-phase. At harvest: `superseded_by_human`,
    `main` unchanged, and `finish_close` is not called (spy).
  - `test_budget_reached_across_ticks`: `closing.attempts` read from disk after each
    harvest is 1, then 2, then the campaign parks. This fails if `closing` is missing from
    the `finish()` whitelist.
  - `test_closing_reset_on_resume_and_waiting`: from a `base_moving` park, `wt queue
    resume` gives `closing == {attempts: 0}`. A WAITING campaign with
    `resume_to: CLOSING` gets `{attempts: 0}` on `slot_free`. Leaving CLOSING (MERGED /
    TRIAGING) removes the key.
  - `test_legacy_closing_without_key`: a CLOSING state.json without `closing` closes
    normally, with the budget counted from 0.
  - `test_close_worker_exception_parks_open_failed`: `prepare_close` is patched to raise.
    Result: PARKED `open_failed`, `resume_to: CLOSING`, detail "close step failed", a
    `report.errors` entry, and no implement-path counters touched.
  - `test_pr_policy_push_in_finish_phase`: `merge.policy: pr` with `git.push_branch` and
    `gh_pr_create` monkeypatched to record the calling thread. Both run on the main thread,
    after the worktree phase. A stop-discarded worktree phase records zero calls, and the
    redo records exactly one PR create, ending HANDED_OFF with the `pr_url`.
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
  `finish_close`. Drop the gated-tree check. Drop the fresh-state gate. Remove `closing`
  from the `finish()` whitelist. Drop the `stop` discard. Record the outcomes in the spec.
- **Regression guard:** full `uv run pytest`.
- **Manual verification:** after the merge, the live runner re-execs (SPEC-0194). On the
  next real close, `wt queue runner-log` shows `started close` followed by other campaigns'
  rows before the close's result row.

### Test record (2026-10-25)

- `uv run pytest tests/test_agentic_close_inflight.py` — 19 tests, all pass (offline, FakeRunner
  and tmp git repos; the held re-gate is a gate command that blocks on a `hold-<spec>` marker
  from its second run on). Covers every test named in the plan, plus
  `test_finish_rechecks_base_sha_itself`.
- `uv run pytest` — 1873 passed, 1 skipped. The existing close, nonblocking, reexec, events and
  e2e suites pass unchanged.
- `python3 tools/spec_lint.py` exits 0 with the ledger regenerated.
- Sabotage checks (each applied alone, new suite re-run, source restored):
  - closes back on the main thread (`_submit` runs `_do_prepare_close` inline):
    `test_admission_while_close_regate_runs` fails;
  - gated-tree check dropped: `test_worktree_commit_after_regate_redoes` fails;
  - fresh-state gate dropped: the park-requested test and both abandon/park-on-disk tests fail;
  - `closing` removed from the `finish()` whitelist: `test_base_moving_every_attempt_parks` fails;
  - `stop` discard dropped: `test_stop_during_worktree_phase_leaves_closing` and
    `test_pr_policy_push_in_finish_phase` fail;
  - base sha re-check dropped: the flow tests still pass, because the ff-only merge refuses a
    moved base and takes the same retry path. The check is pinned directly by
    `test_finish_rechecks_base_sha_itself` (a stale `target_sha` on a fast-forwardable branch),
    which fails without it.
- Manual verification (`wt queue runner-log` showing `started close` followed by other
  campaigns' rows) can only happen after this merges and the live runner re-execs; it is not
  done yet.

### As shipped (2026-10-25)

As Design, with these details:

- `closing` is reset and removed centrally in `_transition` (entering CLOSING from another state
  sets `{attempts: 0}`, leaving it pops the key), which covers `review_pass`, `waived`, resume,
  WAITING → CLOSING and config recovery in one place. `finish()` copies `closing` from the step
  state. The finish phase increments it and stores `target_sha` too.
- `finish_close` also takes the test-only `_hooks` argument, so `close_campaign` keeps the
  `before_merge` hook between the two phases.
- `PreparedClose.events` is the full worktree-phase event list; when `outcome` is set it is the
  same list as `outcome.events`.
- The worktree HEAD is captured right after the rebase, before the re-gate, so a commit made
  during the gate is also caught by the gated-tree check.
- `_apply` still accepts a ready-made `StepResult.close_outcome` (existing unit tests); a
  `StepResult.prepared` is the in-flight path.
- In a single-shot tick, a `base_moved` retry loops within the tick, and each attempt gets its
  own report row (the old in-call loop gave one row).
- `_harvest`'s exception branch now uses the flight's own step kind for every kind, so a triage
  worker exception parks with `resume_to: GATING` and the message "triage step failed".
- `close_campaign` (serial wrapper) re-runs the whole worktree phase on a retry, so the spec
  guard's "skipped" info finding may appear once per attempt.

## Rollout / migration

No data migration. A campaign already CLOSING when the new code starts has no `closing` key.
It is treated as `{attempts: 0}`. Merging this changes the close code itself. The live
runner drains its in-flight steps (including a close already running the old code), then
re-execs into the new code.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

_None — decisions locked above._
