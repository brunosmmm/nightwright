---
id: SPEC-0359
title: "Close-tier reuse of a green loop gate, with a full-suite sentinel on the base branch"
status: done
owner: user
created: 2026-11-01
updated: 2026-11-01
kind: feature
source_idea: IDEA-649
tags: [agentic, gates, close, merge, tests]
depends_on: ["SPEC-0358"]
supersedes: ["SPEC-0355"]
---

## Context

[SPEC-0328](./0328-tiered-gates-affected-test-selection.md) split gates into a loop tier
(`pytest-affected`: `tools/affected_tests.py` picks the tests a branch affects) and a close tier
(the full `pytest` suite once, on the rebased tree). [SPEC-0350](./0350-per-gate-path-sensitivity.md)
skips close gates whose `paths` the branch does not touch. Only docs-only merges got faster
(24 s instead of about 600 s). This spec is part 3 of 3 of the split of SPEC-0355 (which it
supersedes together with the other two parts). It covers the **close and merge tier**.

The 2026-11-01 ledgers and gate logs show: the full `pytest` gate takes 566-822 s for any `src/**`
or `tools/**` change. It runs even when the loop tier just proved the same branch green and the
rebase only brought in unrelated main commits. Slot waits of 317-671 s add to this, and shorter
gate runs make those waits shorter too.

This spec builds on the earlier parts of the split, both already `done`:
[SPEC-0357](./0357-narrow-affected-tests-py-fallbacks-and-write-the.md) (part 1: the
`NW_GATE_REPORT` selection report with `head_sha`, `tests` and `full`, and the
`tools/affected_tests.py --json --closure` CLI mode that exposes the selection and the forward
closure) supplies the inputs for rules R2, R5 and R6. [SPEC-0358](./0358-failed-first-loop-rounds.md)
(part 2: failed-first rounds) shares the gate-result plumbing in `run_all`. This spec depends on
both: SPEC-0357 directly (R2, R5, R6 read its report fields and CLI output) and SPEC-0358 through
`run_all`. The `depends_on` frontmatter lists SPEC-0358, which itself depends on SPEC-0357.

## Goals / Non-goals

**Goals**
- G2. A close-tier gate may reuse the last green loop-tier result instead of running, when the
  rebase is *clean* (defined under Design). Each reuse is recorded as a `gate.reused` ledger event
  with its evidence. Each decline is recorded as a `gate.reuse_declined` event.
- G3. A periodic **full-suite sentinel** on the base branch catches what reuse lets through. A red
  sentinel turns reuse off until main is green again. Reuse also declines while the sentinel is
  overdue.

**Non-goals**
- Narrowing the affected-test fallbacks and writing the gate report
  ([SPEC-0357](./0357-narrow-affected-tests-py-fallbacks-and-write-the.md), which adds
  `NW_GATE_REPORT` and the JSON CLI mode of `affected_tests.py`). This spec consumes them.
- Failed-first loop rounds ([SPEC-0358](./0358-failed-first-loop-rounds.md)): the
  `failed_first` gate key and the prelude run.
- Coverage-based or predictive selection (SPEC-0328 phase 3).
- Batched close trains or bisecting (SPEC-0328 phase 2).
- Changing `merge.docs_only_paths` or SPEC-0350 `paths`.
- Reuse for `nw queue merge` campaigns. They have no loop tier, so they always run the close gates.
- Reuse under `merge.policy: pr` or `draft-pr`. Those closes merge nothing on main, so the sentinel
  could not cover them; they always run the close gates (R1).

## Decision

1. **Record head, tree and report on gate payloads.** Every gate run records the worktree `HEAD` and
   `HEAD^{tree}` at its start, and carries the SPEC-0357 selection report. The campaign state keeps
   `last_green_loop`, the last green loop-tier payload.
2. **Close reuse.** A close gate with `reuse_from: <loop gate name>` is skipped when the rebase is
   clean by rules R1-R7 below. The loop gate's green result then stands for it. Every rule fails
   closed, including on tool errors (see R2). This is opt-in per gate (the generic default is no
   reuse), only under `merge.policy: merge`, and the escape hatches force the full run.
3. **Sentinel.** The queue runner runs the project's close-tier gates on the base HEAD, in a
   dedicated detached worktree, whenever reuses have accumulated (see Design). A red sentinel sets
   `force_full_close` on the project and escalates to the conductor with the list of suspect merges.
   Reuse declines while the sentinel is overdue, so reuse cannot run unchecked for long. The
   counter and the suspects move only when a reused close actually merges, and a sentinel result
   only settles the suspects its tested sha contains.

## Design

### 1. Recording

`run_gate` sets `NW_GATE_REPORT=<campaign>/gates/<seq:03>-<name>.report.json` for every gate. A gate
result may carry `report` (the parsed JSON, under 64 KiB; anything larger is dropped). The `gate`
ledger payload and `last_gate` gain optional `head` and `tree`: the worktree's `HEAD` and
`HEAD^{tree}` when the run started. The campaign state keeps `last_green_loop`, a copy of the last
loop-tier payload with `ok` true and no `interrupted`. Any later red loop run clears it.

### 2. Config

A gate may set `reuse_from: <name>` (validated: the gate is `when: close`, and the name is a
`when: loop` gate of the same config). The project may set `close.force_full: true` (the escape
hatch in the config), and env `NW_CLOSE_FULL=1` does the same for one runner. Both turn reuse off.
The project schema is strict (unknown keys fail), so `project.py` gains two optional top-level
sections, validated like `merge` with defaults in `PROJECT_DEFAULTS`:
- `close`: `force_full: false`, `reuse_max_age_s: 86400`, `reuse_max_delta: 200`.
- `sentinel`: `enabled: true`, `after_reuses: 3`, `max_age_s: 86400`.

### 3. Clean rebase, decided in `close._reuse_loop_green` before `G.run_all(..., tier="close")`

Let `G` be `last_green_loop.head`, `H` the rebased HEAD, `T` the target sha, `B` =
`changed_files(H vs T)` (the branch's own changes after the rebase) and `D` =
`git diff --name-only G H` (everything that differs between the gated tree and the close tree).
Reuse requires all of the following, evaluated in order (the first failing rule is the one
recorded):

- **R1 enabled.** The gate has `reuse_from`. The project's `merge.policy` is `merge`.
  `close.force_full`, `NW_CLOSE_FULL` and the project's sentinel `force_full_close` are all off,
  and the sentinel is not overdue (see section 5). An unreadable `gate_reuse.json` also declines
  here (see section 4, *State loss*).
- **R2 green source.** `last_green_loop` exists. Its `reuse_from` gate result is `ok`, has a
  `report`, and that report's `head_sha` equals `G`. If `G` is not a valid commit in the repository
  (for example garbage-collected after the rebase), reuse declines with R2. If computing `D`,
  `sel_H` or a closure errors or times out (a non-zero `git`/`affected_tests.py` exit, unparseable
  JSON, or the 60 s per-call timeout), reuse declines with the rule being evaluated (R3 for `D`,
  R4 for `sel_H(D)`, R5 for `sel_H(B)`, R6 for a closure) and `detail` carries the error.
- **R3 branch unchanged.** `D ∩ B = ∅`: every file the branch changes has the same blob in `H` as in
  `G`. So no fix commits came after the green round, and the rebase changed no branch file (a
  conflict resolution or a regenerated file in `B` breaks this).
- **R4 main delta mappable.** No path in `D` is a full trigger (pyproject, lockfile, conftest, waits,
  removed module) and none is a `.nw/**` gate-definition change. The selection for `D`, computed on
  `H`, is not full.
- **R5 selection did not grow.** `sel_H(B)`, the branch's selection recomputed on `H`'s import graph,
  is a subset of the report's `tests`, or the report was full.
- **R6 no interaction.** The forward static closure of `sel_H(B)` (imports plus reference scan) on
  `H` contains no path in `D`. The forward closure of `sel_H(D)` contains no path in `B`. In words:
  the tests that proved the branch read nothing main changed, and the tests main's changes affect
  read nothing the branch changed.
- **R7 bounded age.** `G` was gated less than `close.reuse_max_age_s` ago (default 86400) and `D`
  has at most `close.reuse_max_delta` paths (default 200). A large or old delta is not worth the
  static reasoning.

`sel_H` and the forward closure come from `tools/affected_tests.py --json --closure`
([SPEC-0357](./0357-narrow-affected-tests-py-fallbacks-and-write-the.md)).

When every rule holds, the gate is removed from the close run (like a SPEC-0350 skip) and a
`gate.reused` event is appended. Required keys: `gate`, `from_gate`, `from_gate_seq`, `gated_head`,
`close_head`, `target_sha`, `delta_count`, `selection_count`, `rules`. `rules` maps R1-R7 to `true`.
Optional keys are `selection_reason` and `full_source` (whether the source run was full). A reused
required gate counts as satisfied. When a rule fails, `gate.reuse_declined` is appended (required
keys `gate`, `rule`, `detail`) and the gate runs as today. Both events go into
`schemas.EVENT_KEYS`. These events are evidence only: they are written in the prepare phase and
never touch `gate_reuse.json`. The close's gate run carries `reused: [<gate names>]` so that
`finish_close` knows whether the merge it performs rests on a reuse.

**Why this definition.** R3 and R5 make sure the branch's tested content is byte-identical and its
selection has not grown. R6 is the only new trust. It extends to the main-plus-branch combination
the same static reasoning the loop tier already trusts for the branch alone, and it fails closed
whenever the two change sets touch shared test dependencies. The failures that remain possible come
from dynamic coupling, covered under Risks, and the sentinel backs those up. The rule uses the
static graph, so the decision takes seconds, not minutes.

### 4. Sentinel (periodic full suite on base)

**Where it runs.** The queue runner gets a new step, `sentinel`, on a worker thread. It is scheduled
only when no campaign is waiting for a gate slot. It runs `gates_for(cfg, "close")` with no `paths`
skip and no reuse, on a detached worktree at `<agentic>/sentinel/<project>`, reset to the base HEAD
before each run (`worktrees.setup` runs on creation). It takes a normal host gate slot (SPEC-0309).
The sha the worktree was reset to is the run's **tested sha X**.

**State.** The project file `<agentic>/projects/<project>/gate_reuse.json` holds
`{reuses_since_green, last_green_sha, last_green_at, due_since, force_full_close, suspects}`, where
each suspect is `{campaign, merge_sha}`. Every read-modify-write of the file happens under a project
lock (`gate_reuse.json.lock`, the same `fcntl` pattern as `events._locked`), so worker threads and
the runner never lose an update.

`reuses_since_green` is incremented and `{campaign, merge_sha}` appended in `finish_close` when the
outcome is `MERGED` and the close's gate run reused a gate, under the project lock on
`gate_reuse.json`. `merge_sha` is the close's `merged_sha`. `gate.reused` itself does not touch the
file. That transition is the only place the counter grows, so a prepare that retries after the base
moved, a close that parks (`main_dirty`, `base_moving`, a red gate) or a close that hands off
records nothing. Under `merge.policy: pr` / `draft-pr` reuse never happens (R1), so the file is
never touched by those closes.

`due_since` is set to the current time by whoever first observes the due condition of section 5
becoming true (the runner's scheduling check or the `finish_close` increment) while it is null, and
cleared when a green sentinel leaves no suspects.

**State loss.** When `gate_reuse.json` is missing, it is rebuilt from the project's queue events
under the lock: the suspects are the `MERGED` closes after the last `sentinel` event whose gate run
reused a gate (all of them if there is none), `reuses_since_green` is their count,
`last_green_at` and `last_green_sha` come from the last `sentinel` event with `ok: true` (unknown
if there is none), and `force_full_close` is `true` when the last `sentinel` event has `ok: false`,
or when there is no `sentinel` event at all while reuse events exist. When the file exists but is
unreadable (invalid JSON or schema), it is left in place and every close declines reuse with R1
(`detail: "gate_reuse.json unreadable"`) until a sentinel run rewrites it.

### 5. When the sentinel is due

The sentinel is **due** when `reuses_since_green >= sentinel.after_reuses` (default 3), or when
`reuses_since_green >= 1` and `last_green_at` is older than `sentinel.max_age_s` (default 86400).
An unknown `last_green_at` counts as older than `max_age_s`. That gives at least one full run per
day while reuse is in use, and none when nothing was reused.

The sentinel is **overdue** when either:
- `due_since` is set and `now - due_since > sentinel.max_age_s`, or
- `sentinel.enabled` is false and `reuses_since_green >= sentinel.after_reuses`.

Rule R1 then declines reuse. These are the only definitions; every term maps to a field of
`gate_reuse.json` or the `sentinel` config. This keeps reuse from running unchecked when the queue
is too busy for the sentinel to be scheduled.

**Outcome.** On green at tested sha X: remove from `suspects` only the entries whose `merge_sha` is
X or an ancestor of X (`git merge-base --is-ancestor`), set `reuses_since_green` to the number of
entries that remain, record `last_green_sha = X` and `last_green_at`, clear `force_full_close`,
and clear `due_since` when no suspects remain (otherwise set it to now if the remaining count is
still due). Reused merges that landed after X while the run was in progress survive and are
covered by the next run. Append the queue event `sentinel` (`ok: true`).

On red at tested sha X: set `force_full_close` (all later closes of the project run full), name as
suspects only the entries whose `merge_sha` is X or an ancestor of X, append `sentinel`
(`ok: false`, failing ids, those suspects), and file an escalation to the conductor naming those
suspect merges. The suspect list in the file is left unchanged on red.

`nw queue sentinel clear --project P` clears `force_full_close` and `due_since` without touching
the suspects. `nw queue sentinel run --project P` runs the sentinel on demand (it waits for a gate
slot like any gate run).

The `sentinel` queue event's required keys are `project`, `ok`, `sha` (the tested sha X) and
`suspects` (the list of `{campaign, merge_sha}` settled by this run), plus `failing` (the failing
test ids, or the failing gate names when no ids are parsed) when `ok` is false. It goes into
`schemas.EVENT_KEYS` with those required keys.

### Dogfood config (`.nw/project.yaml`)

- `pytest` (close): `reuse_from: pytest-affected`.
- No `close.force_full`. Sentinel defaults.

### Implementation order

If the change is too large for one campaign, ship the sentinel (sections 4-5, `gate_reuse.json`,
the runner step and the CLI) first. It is idle until a reuse is recorded, so it is safe alone.
Reuse (sections 1-3 and the dogfood `reuse_from`) follows. The spec stays one unit; the order is a
sequencing hint only.

## Alternatives considered

- **Re-run only the selection at close instead of reusing.** This still costs one selected run per
  close and gives nothing that R6 does not. Reuse at zero cost is the point.
- **Reuse on equal tree hash only (`H^{tree} == G^{tree}`).** This is never true after a rebase onto
  a moved main, which is the common case. It would almost never fire.
- **Periodic full run inside the next campaign's close.** It would blame and triage an unrelated
  campaign for a regression an earlier reuse let in. A separate sentinel on base keeps the
  attribution right.
- **Count reuse at gate time (`gate.reused`).** No merge sha exists yet, retries double-count and
  parked closes leave phantom suspects. Counting at the `MERGED` transition is exact.
- **Record PR-policy reuses as `{campaign, pr_url}`.** The sentinel would have to track when each
  PR merges on a remote. Declining reuse under PR policies is simpler and loses little, since the
  dogfood project merges locally.

## Acceptance criteria

- [x] Gate results carry `report` (parsed JSON under 64 KiB, larger dropped), and `gate` payloads and
      `last_gate` carry `head` and `tree`. `last_green_loop` holds the last green, non-interrupted
      loop payload, and a later red loop run clears it.
- [x] A close gate with `reuse_from` is skipped and a `gate.reused` event with all required keys is
      recorded only when R1-R7 hold. Each failing rule appends `gate.reuse_declined` naming that
      rule, and the gate runs.
- [x] A `G` that is not a valid commit declines with R2; a `git` or `affected_tests.py` error or
      timeout declines with the rule being evaluated, with the error in `detail`.
- [x] `close.force_full`, `NW_CLOSE_FULL=1`, `merge.policy` other than `merge`, a red sentinel, an
      overdue sentinel and an unreadable `gate_reuse.json` each force the full close run (a
      `gate.reuse_declined` with rule R1).
- [x] `reuses_since_green` and `suspects` change only in `finish_close` on a `MERGED` outcome whose
      gate run reused a gate, under the project lock; a retried prepare, a parked close and a
      PR-policy close leave them unchanged.
- [x] Due and overdue follow section 5 exactly, using `due_since`, `last_green_at` (unknown counts
      as older than `max_age_s`) and the `sentinel` config.
- [x] `reuse_from` is validated (it names a loop gate and sits on a close gate), and the `close` and
      `sentinel` project sections are validated with their defaults; unknown keys in them fail.
- [x] All three new events (`gate.reused`, `gate.reuse_declined`, `sentinel`) are in
      `schemas.EVENT_KEYS` with their required keys.
- [x] The sentinel runs the close gates on a detached base worktree when due by reuse count or age,
      never while a campaign waits for a slot. Green at tested sha X removes only suspects that are
      X or its ancestors, sets the counter to the remaining count and clears the flag. Red sets
      `force_full_close` and escalates naming only suspects that are X or its ancestors.
      `nw queue sentinel run|clear` work.
- [x] A missing `gate_reuse.json` is rebuilt from queue events, restoring `force_full_close: true`
      after a last red sentinel (or no sentinel event while reuse events exist).
- [x] The dogfood `.nw/project.yaml` sets `reuse_from`. `docs/product/capabilities/agentic.md`
      documents `reuse_from`, `close.force_full`, `NW_CLOSE_FULL` and `nw queue sentinel run|clear`.

## Test plan

- **Automated: reuse** (`tests/test_agentic_close_reuse.py`, close fixtures as in
  `test_agentic_close.py` / `test_agentic_gate_paths.py`): a clean rebase onto unrelated main commits
  gives `gate.reused` with every key, and the pytest gate does not run. Then one test per rule for
  the decline: a reuse flag that is off or forced, PR policy, an unreadable state file, or an
  overdue sentinel (R1), no or stale green, or a `G` that is not a valid commit (R2), a fix commit
  after green or a conflict resolution in a branch file (R3), a main delta with `pyproject.toml` or
  `.nw/project.yaml` (R4), a grown selection (R5), main changing a module a branch test imports, or
  a test reading a branch data file (R6), old or large (R7). One tool-error case: a stubbed
  `affected_tests.py --json --closure` that exits non-zero (and one that times out) declines with
  R6 and the error in `detail`.
- **Automated: counter placement** (`tests/test_agentic_close_reuse.py`): a reused close that merges
  increments `reuses_since_green` once and appends `{campaign, merge_sha}` with the merged sha; a
  reused close whose prepare retries after the base moved and then merges increments it once; a
  reused close that parks (`main_dirty`, `base_moving`) leaves the counter and suspects unchanged;
  two concurrent `finish_close` increments under the lock both land.
- **Automated: recording** (`tests/test_agentic_gates.py`): `NW_GATE_REPORT` is set per gate, a
  report under the cap is attached and one over it is dropped, `head`/`tree` appear on the payload
  and `last_gate`, `last_green_loop` is set by a green loop run and cleared by a later red one.
- **Automated: schema and config**: a schema test asserting `gate.reused`, `gate.reuse_declined`
  and `sentinel` are in `schemas.EVENT_KEYS` with their required keys (including `failing` only
  when not ok), and `reuse_from`, `close` and `sentinel` validation in
  `tests/test_agentic_project.py`.
- **Automated: sentinel** (`tests/test_agentic_sentinel.py`): due and not-due by count and age, and
  due with an unknown `last_green_at` after one reuse; `due_since` set when due first holds and
  cleared on a green with no suspects left; overdue by `due_since` age and by `enabled: false`;
  no scheduling while a slot waiter exists; green resets; red sets `force_full_close`, escalates
  and makes the next close decline with R1; **merge during run**: a reused campaign merges after the
  sentinel reset its worktree to X and before it finishes green, and that entry survives in
  `suspects` with `reuses_since_green == 1`; the same on red, where the escalation and event name
  only the ancestors of X; the CLI `run`/`clear`; a deleted `gate_reuse.json` with no sentinel
  history is rebuilt from events and a single reuse makes the sentinel due; **deleted after red**:
  a red sentinel, then the file is deleted, then the next reused-eligible close declines with R1
  because the rebuild restored `force_full_close: true`; a corrupt file declines with R1 until a
  sentinel run rewrites it.
- **Manual verification:** (a) Run one dogfood code campaign through the queue and record close-gate
  wall time, and whether a `gate.reused` or `gate.reuse_declined` event appeared and with which rule.
  (b) `nw queue sentinel run --project Meta-Tools` on main: green, with the state file reset.
  (c) `docs/product/capabilities/agentic.md` has a section covering `reuse_from`,
  `close.force_full`, `NW_CLOSE_FULL` and `nw queue sentinel run|clear`.
- **Regression guard:** `python3 tools/affected_tests.py --run uv run pytest -q` plus
  `test_agentic_close*`, `test_agentic_gate*`, `test_agentic_queue*`, `test_agentic_project`,
  `test_agentic_dogfood_config`; `python3 tools/spec_lint.py` exits 0; the full suite runs at the
  queue's close gate (this change edits `.nw/project.yaml`, so R4 declines reuse for itself).

### Test record (2026-11-01)

- `uv run pytest -q -n 4 tests/test_agentic_close_reuse.py` (new, 29 tests): the clean rebase gives
  `gate.reused` with every required key and the `pytest` gate does not run; one decline per rule
  (R1: `close.force_full`, `NW_CLOSE_FULL=1`, PR policy, unreadable state file, overdue sentinel,
  red sentinel, merge request; R2: no green, stale report, `G` not a commit; R3: fix commit after
  green, a rebase that changed a branch file; R4: `pyproject.toml`, `.nw/extra.yaml`, missing
  tool; R5: grown selection; R6: both interaction directions; R7: old, large); a real stub
  `affected_tests.py` that exits non-zero on `--closure` and one that times out both decline
  with R6 and the error in `detail`; counter placement: a merged reused close counts once, a
  retried prepare (base moved) then merge counts once, `main_dirty` and `base_moving` parks and a
  PR-policy close leave it at 0, eight concurrent `record_merge` calls all land. Passed.
- `uv run pytest -q -n 4 tests/test_agentic_sentinel.py` (new, 17 tests): due/not-due by count,
  age and unknown `last_green_at`; `due_since` set when due first holds and cleared on a green
  with no suspects; overdue by `due_since` and by `enabled: false`; green resets; merge during
  run (green: the late merge survives with `reuses_since_green == 1`; red: the event and the
  escalation name only the ancestor of X, the file's suspects stay); red makes the next close
  decline with R1; deleted with no history is rebuilt (reuse merges, `force_full_close: true`,
  due); deleted after red declines with R1; corrupt file declines until a sentinel rewrites it;
  interrupted run settles nothing; no scheduling while a slot waiter or a deferred GATING
  campaign exists; CLI `run` (red exits 1, `--json`) and `clear`. Passed.
- `tests/test_agentic_gates.py` (+5: report env/attach/cap, stale report, head/tree, no git,
  `last_green_loop` set by green and cleared by red through `queue._apply`) and
  `tests/test_agentic_project.py` (+3: `reuse_from`, `close`/`sentinel` validation and defaults,
  the three events and `failing` only when not ok in `schemas`/`events`). Passed.
- Regression guard: `python3 tools/affected_tests.py` selects the full suite for this change
  (103/219 files), so the guard set ran instead: every `test_agentic_close*`, `test_agentic_gate*`,
  `test_agentic_queue*`, `test_agentic_events*`, `test_agentic_escalation*`, `test_agentic_remote*`,
  `test_docs_*`, `test_agentic_project`, `test_agentic_dogfood_config`, `test_specs`: 991 passed;
  then `test_completion`, `test_agentic_dashboard`, `test_agentic_e2e`, `test_agentic_resolver`,
  `test_spec_status`, `test_worker_jobs`, `test_idea_capture_guard`, `test_test_waits`: 306 passed.
  The full suite was left to the queue's close gate. `python3 tools/spec_lint.py` exits 0.
- Manual: (a)/(b) as a scratch run, since inside the campaign the queue may not create worktrees
  of this repo and main's config has no `reuse_from` until merge: a `/tmp` repo with
  `src/nightwright` modules, real tests and this branch's real `tools/affected_tests.py`; the loop
  report written through `NW_GATE_REPORT`, then `close_campaign` after an unrelated main commit:
  MERGED in 1.2 s with `gate.reused` (`delta_count 1`, `selection_count 1`), the full pytest gate
  not run, `reuses_since_green 1` with the merged sha as suspect; then `sentinel.run` on the base
  ran the real pytest suite green, settled that suspect and reset the counter to 0. The real tool
  was also called on this worktree (`--json`, `--json --closure`, and `pyproject.toml` -> full),
  about 3 s per call. The dogfood queue measurements of (a) and `nw queue sentinel run --project
  Meta-Tools` on main belong to the rollout watch (step 3) after merge. (c) done: the new section
  "Close-tier reuse and the sentinel (SPEC-0359)" in `docs/product/capabilities/agentic.md`.
- Gate loop 1 fix: `tests/data/remote_gates_golden.json` re-recorded (`NW_RECORD_GOLDEN=1`) for the
  new `head`/`tree` on the gate payload; the diff adds only those two keys (payload and `gate`
  event). `uv run pytest -q -n 2 tests/test_remote_gates.py tests/test_agentic_gates.py`: 173 passed.

- Review loop 2 fix (sentinel scheduling and serialization): `uv run pytest -q -n 4
  tests/test_agentic_sentinel.py` (22 tests, +5): a red sentinel is not resubmitted at the same
  base sha on the next ticks and escalates once, a rerun of the same red sha after the retry
  interval appends a second `sentinel` event but no second escalation, `sentinel clear` is not
  undone at that sha, a moved base runs again and goes green; a green sha is not rerun and
  suspects not on the base are dropped (`dropped` on the event) so the counter reaches 0; a
  failed `worktrees.setup` backs off, is rerun on the next run and is not repeated after it
  succeeded; a CLI `sentinel run` is refused (exit 1, "already in progress") while the
  runner's run holds the lock, the worktree is not cleaned under it and the runner's step skips
  the project; the drain's `waiting_on` lists a running sentinel. Then the close, close-reuse,
  gates, project, dogfood-config and `test_agentic_queue*` files (671 passed) and the drain /
  exec / lease / quiet / escalation / remote-gates / `test_test_waits` files (533 passed,
  after the new lock test was moved to `waits.join`), and `test_docs_*`, `test_specs`,
  `test_completion` (75 passed). The full suite was left to the queue's close gate. `python3 tools/spec_lint.py` exits 0.

- Review loop 3 fix (git errors, own-selector reuse, settle only ancestors):
  `tests/test_agentic_close_reuse.py` (42 tests, +13): the branch or main editing
  `tools/affected_tests.py` declines with R4 ("reuse tooling"), the full `pytest` gate runs and
  the selector is never called; a `tools/*.py` the loop gate's `cmd` runs counts as tooling, an
  unrelated `tools/` file does not; a real stub tool that exits non-zero, times out or prints no
  JSON on the R4 call declines with R4, the same on the R5 call with R5 (and the stub with no
  failure reuses); a `git` diff error declines with R3; an unexpected exception declines with R1
  (`reuse check failed`). The R6 tool-error cases now put the stub into the gated base, since a
  tool change in `D` declines earlier with R4. `tests/test_agentic_gates.py` (+1): an
  interrupted `ok` loop run clears `last_green_loop` (fails with the guard removed).
  `tests/test_agentic_sentinel.py` (26 tests, +4, one rewritten): a green run settles only
  ancestors of X, keeps an unknown sha silently and a known off-base sha as `orphans`; with
  `merge-base`, `rev-parse` and `cat-file` raising `GitError` a green settle still finishes,
  settles only the tested sha and keeps the other suspect; a due project whose `merge.base`
  does not resolve journals one `sentinel_failed` over three steps and a whole `Q.tick`, starts
  no run (fails with `GitError` left out of the except); any exception in the step is journaled
  by the tick; `nw queue sentinel run|clear` report a git error without a traceback. Then
  the guard set: `test_agentic_close*`, `test_agentic_gate*`, `test_agentic_queue*`,
  `test_agentic_sentinel`, `test_agentic_project`, `test_agentic_dogfood_config`, `test_docs_*`,
  `test_specs`, `test_agentic_escalation*`, `test_remote_gates`, `test_test_waits`,
  `test_completion`: 1072 passed (`affected_tests.py` selects the full suite for this change). `python3
  tools/spec_lint.py` exits 0; the full suite is left to the queue's close gate.

- Review loop 4 fix (a red full close pins the gate): `tests/test_agentic_close_reuse.py`
  (+2): a close that declined reuse (`NW_CLOSE_FULL`) runs `pytest` red and goes to TRIAGING;
  after a `retry_gate`-style green loop at the rebased head the next close declines with R2
  ("was red") and the full `pytest` gate runs; that green full run lifts the pin and the next
  clean close reuses again; an interrupted or missing run changes nothing.
  `tests/test_agentic_gates.py` (+1): `queue._apply` of a TRIAGING close with a red gate run
  persists `close_red`, a MERGED close with a green run clears it.
  `tests/test_agentic_dogfood_config.py` (+1 assertion block, +1 test): the dogfood `pytest`
  close gate has `reuse_from: pytest-affected` and no `close.force_full`; `agentic.md` names
  `reuse_from`, `close.force_full`, `NW_CLOSE_FULL`, `nw queue sentinel run` and `clear`.
  Then `test_agentic_close*`, `test_agentic_queue*`, `test_agentic_gate*`,
  `test_agentic_project`, `test_agentic_sentinel`, `test_remote_gates`,
  `test_agentic_outbound_e2e`, `test_docs_*`: 925 + 156 passed. The full suite is left to the
  queue's close gate. `python3 tools/spec_lint.py` exits 0.

### As shipped (2026-11-01)

- Built to Design. New module `agentic/sentinel.py` holds `gate_reuse.json` (lock
  `gate_reuse.json.lock` via `events._locked`), due/overdue, `record_merge`, the rebuild, the run,
  `settle` and `clear`; `close._reuse_loop_green` / `_reuse_evidence` evaluate R1-R7;
  `queue._sentinel_step` schedules it on the runner's pool after the agent steps are submitted
  (run loop only, not while draining); `nw queue sentinel run|clear` in `cli_queue.py`.
- R4/R5 call `affected_tests.py --json --changed=...` and R6 calls it with `--closure` (two
  calls), so a tool error is attributed to the rule whose call failed; with an empty `D` (or `B`)
  no call is made and R6 holds trivially. A missing tool declines with R4. An unexpected
  exception in the rule code declines with R1 (`reuse check failed`). A merge request declines
  with R1.
- The merged close's `close` ledger event carries `reused: [...]`; the rebuild finds reused
  merges there. Deviation from section 4 *State loss*: suspects are the reused merges that the
  last **green** sentinel's sha does not contain (by ancestry; by timestamp when the sha is
  unknown to git), not "the merges after the last sentinel event". This is what the file would
  hold, since a red sentinel leaves the suspects unchanged. "Reuse events exist" is read as
  "reused merges exist", so a rebuilt `force_full_close` always comes with a due sentinel that
  can clear it.
- The red-sentinel escalation is a campaign-less escalation record (`nw escalations`, by
  `conductor`, project set) whose question and facts name the settled suspects; the `sentinel`
  queue event (ok false) reaches the conductor's event follow as well.
- `due_since` is also cleared by a green run that leaves suspects that are not still due.
  The runner's drain waits for a running sentinel like a gate flight (a stop ends its gates).
  Sentinel logs go to `<agentic>/sentinel/<project>.logs/NNN-<gate>.log`.
- Review loop 2 additions, all failing toward fewer redundant runs, none changing what a run
  settles. (1) `SN.run` holds a non-blocking per-project run lock,
  `<agentic>/sentinel/<project>.lock`, across the worktree reset, the gates (and the log seq)
  and the settle; a second run raises `Busy` (the CLI exits 1 with the holder's pid, the runner
  skips the project and journals `sentinel_skipped` if it lost the race). (2) Each finished run
  is noted in `<agentic>/sentinel/<project>.last.json` (`sha`, `at`, `ok`, `error`, `cleared`;
  outside `gate_reuse.json`, whose shape is unchanged). The runner's step (`SN.backoff`) does
  not start a run at the base sha the last run tested when it was green, or red and cleared by
  `nw queue sentinel clear`; a red or raised run at that sha is retried after `SN.RETRY_S`
  (3600 s, a constant rather than a config key, since the project schema is fixed by this spec);
  a moved base always runs; a deferral is journaled once per reason (`sentinel_deferred`).
  (3) `settle` skips the escalation when the project's previous `sentinel` event was already
  red at the same sha (the event is still appended). (4) (Replaced in review loop 3, see
  below: orphan suspects are no longer dropped.) (5) `worktrees.setup` success is marked in `<project>.setup.json` (with the commands),
  so a failed or changed setup recreates the worktree on the next run. (6) The drain lists a
  running sentinel in `waiting_on` and does not detach-and-exec while it runs.
- Review loop 3 changes. (1) No git failure leaves the runner's tick: `queue._sentinel_step`
  resolves the base inside `except (GitError, ValueError, OSError)` and journals
  `sentinel_failed` once per reason instead of starting a run (an unresolvable `merge.base`),
  and the tick wraps the whole step (`sentinel_failed`, never raised). `nw queue sentinel
  run|clear` turn a `GitError` into a ClickException. In `sentinel.py` every ancestry question
  goes through `_ancestor` (exit 0 true, exit 1 false, anything else or a `GitError` "cannot
  say") and `_known` (`cat-file -e`); "cannot say" never settles or drops a suspect. (2) A green
  run settles only the suspects that are X or its ancestors, as section 5 says; the loop 2
  dropping of suspects not on the base is gone. A suspect git knows that is not an ancestor of
  the base tip (exit exactly 1, a rewritten base) is named as `orphans` on the `sentinel` event
  and kept. Such a suspect keeps the sentinel due and eventually overdue, so reuse turns off for
  the project (fail closed) until a human repairs `gate_reuse.json`. (3) Rule R4 also declines
  when `B` or `D` contains the reuse tooling: `tools/affected_tests.py` (`close.AFFECTED_TOOL`)
  and every `tools/*.py` path named in the close gate's or its `reuse_from` gate's `cmd`. The
  selector that decides R4-R6 and chose the loop gate's tests is then the one already on the
  base, never one the branch edited; a selector change always runs the full close gate.
- Review loop 4 change: a red full close run pins that gate to full runs for the campaign.
  `queue._apply` (kind `close`) calls `close.note_close_gates` with the close's gate run: every
  red, non-interrupted close gate result is recorded in the campaign state's `close_red`
  (`name -> {head, gate_seq}`), a green one removes its entry. Rule R2 declines reuse for a gate
  listed there ("the full ... close run was red"), so after a triage `retry_gate` the loop
  gate's new green cannot stand in for the full suite this campaign just saw fail; the next
  full run of the gate decides, and once it is green reuse is possible again.

## Risks

- **Dynamic coupling the static graph misses** (a plugin registry, `importlib` by computed name, env
  or config read through a helper, test ordering or shared global state). A reused close can merge a
  regression. *Mitigation:* R6 fails closed on any shared static dependency. The sentinel runs the
  full suite at most 3 reuses or 24 h later and names the suspect merges; an overdue sentinel turns
  reuse off. A red sentinel turns reuse off project-wide until main is green.
- **Flaky tests** let a reused green hide a flake that the full run would have hit. This is
  accepted: SPEC-0293 already tracks flakes, and the sentinel retries through the normal ladder.
- **State loss** (`gate_reuse.json` deleted) is rebuilt from queue events, including a red
  sentinel's `force_full_close`, and an unknown `last_green_at` counts as old, so the sentinel is
  due after one reuse. A corrupt file declines reuse until a sentinel runs. Both fail toward
  running the full suite.

## Clock Log

CLOCK-IN: n/a
CLOCK-OUT: n/a

## Rollout / migration

1. Ship the recording (`report`, `head`, `tree`, `last_green_loop`), the `close` and `sentinel`
   config sections and the sentinel (default on, idle until the first reuse). Reuse is **off** by
   default: no `reuse_from` means no reuse.
2. In the same change or right after, turn on `reuse_from` in the dogfood `.nw/project.yaml`.
   Campaigns already in flight pick it up at their next close, because main's config is read. Their
   first close declines with R2, since earlier loop runs have no report.
3. Watch `gate.reused` / `gate.reuse_declined` counts and sentinel results for a week. Tune
   `after_reuses` and `reuse_max_delta` from evidence and record the result in *As shipped*.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; affected tests pass; manual steps performed and recorded.
- [x] No regressions (queue close gate full suite).
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated.

## Open questions

None.
