---
id: SPEC-0309
title: "Faster gates: parallel pytest under a host-wide gate-slot pool, affected-tests scope in fix loops, reuse of an already-green tree at close"
status: proposed
owner: user
created: 2026-10-30
milestone: "M10: agentic MVP"
kind: feature
parent: SPEC-0298
source_idea: IDEA-430
tags: [agentic, gates, throughput, mvp]
---

## Context

Measured on 2026-10-30 (`wt campaign export --since 2026-10-30`, `gates/*.json`): implement
campaigns spent 490 min GATING against 232 min IMPLEMENTING and 58 min REVIEWING, plus 186 min
CLOSING (rebase and re-gate). The one expensive gate is `pytest` in `.wt/project.yaml`
(`uv run pytest -q -x`): the whole suite, serial. 4074 tests pass in 800-1160 s, and slower
when four or five campaigns gate at once on 8 cores (load average about 7).

Campaigns run three or four full gates even with zero failures: SPEC-0301 ran 4 x 800 s, SPEC-0304
ran 3 x about 1000 s. That is about an hour of tests per campaign and 1.5-2.5 h of wall time per
merge. The [SPEC-0298](./0298-agentic-mvp-cheap-durable-quiet-conductor.md) exit window needs
at least 20 consecutive merges, so gate time sets how fast it fills.

Where the full runs come from today:

1. `queue._do_gate` (`src/wt/agentic/queue.py:1723`) calls `gates.run_all` for every GATING step.
   That includes every re-gate after a fix loop, which re-runs the whole suite to check a
   one-test fix.
2. `close.prepare_close` step 5 (`src/wt/agentic/close.py:564`) re-gates after every rebase, even
   when the rebase was a no-op and the tree is the one that just went green.
3. Nothing bounds how many gates run at once on the host. Profile `slots` (`balanced.yaml`: 2)
   bounds campaigns per runner, not CPU. One pytest process uses one core, so parallelism
   inside a gate is the free lever, and it is unsafe without a host-wide cap.

The test suite is parallel-safe by design: SPEC-0269 forbids fixed sleeps in favour of
`tests/waits.py`, and SPEC-0253 gives each gate run a private `TMPDIR`. The flake rate under
xdist still has to be measured, not assumed.

## Goals / Non-goals

**Goals**
- A zero-failure campaign runs at most two full-suite gates (the first gate and the close gate),
  and a campaign whose rebase was a no-op after a green gate runs one.
- A full pytest gate takes at most half its serial wall time on an idle host.
- Concurrent gates never oversubscribe the CPU: a host-wide pool bounds how many gate runs
  execute at once.
- Correctness is unchanged. Nothing merges that was not proven green on the exact tree that
  merges, as far as the gates can say. The close gate stays the safety net for every shortcut.
- Per-campaign gate time falls from about 60 min to about 15 min (the idea's estimate; the
  verification below reports what was measured).

**Non-goals**
- Test impact analysis from coverage data (testmon-style source-to-test maps). The affected set
  is the campaign's own changed tests plus the previous failing ids, which needs no map.
- Changing the retry ladder (SPEC-0289), the triager, or the flaky-test record (SPEC-0293 is a
  frozen area).
- Splitting the suite into shards across machines.
- Skipping gates when the tree changed, or any `--docs-only` change (SPEC-0234 is untouched).

## Decision

Four changes, all in the gate path, each switchable:

1. **Parallel pytest.** Add `pytest-xdist` to the dev extras and run wt's own pytest gate as
   `uv run pytest -q -x -n 4`, with a serial `retry_cmd` for the targeted rerun.
2. **Gate-slot pool.** A host-wide pool of `agentic.gates.slots` (default 2) slots, taken with
   `flock` for the duration of a whole `run_all`. Two concurrent `-n 4` gates fill 8 cores; a
   third waits.
3. **Affected scope in fix loops.** A gate may declare `affected_cmd` and `affected_tests`. A
   GATING step that has a previous gate in the same campaign runs `affected_cmd` instead of
   `cmd`. The close gate and the first gate of a campaign always run the full `cmd`.
4. **Reuse a green tree at close.** A full-scope gate run that passed on a clean tree records the
   tree hash. When the close step's rebased and regenerated tree has the same hash and the gate
   definitions are unchanged, the close does not re-run the gates. It records the reuse.

Tree equality, not "base unchanged since the last gate", is the reuse test. It covers the
no-op rebase as a special case and stays right when the base moved but the rebased tree is
identical. Anything else re-gates in full.

## Design

### 1. Parallel pytest

- `pyproject.toml`: add `pytest-xdist>=3` to the `dev` extra (next to `pytest>=8`, line 30).
- `.wt/project.yaml`, gate `pytest`: `cmd: uv run pytest -q -x -n 4`, and
  `retry_cmd: uv run pytest -q -n 0 {failing}` so a targeted rerun of one to ten ids does not
  pay worker start-up. `results: {format: pytest}` stays: `gate_results.parse_pytest` reads the
  last summary line, which xdist keeps in the same format.
- Tests that are not parallel-safe (a fixed port, a fixed path outside `tmp_path`, a shared
  module-level file) are fixed in place. Where a fix is not cheap, mark the test with
  `@pytest.mark.xdist_group("name")` and run with `--dist loadgroup` in the gate command. The
  implementation records which tests needed that in *As shipped*.
- The worker count lives in the project's gate `cmd`, since only the project knows its suite.
  wt's default of 4 comes from 8 cores divided by 2 pool slots.

### 2. Gate-slot pool

New module `src/wt/agentic/gate_slots.py`:

```python
@contextmanager
def acquire(cfg, campaign_id, *, stop=None, poll_s=1.0) -> Iterator[SlotHold]: ...
def settings(cfg) -> dict          # {"slots": 2}
def occupancy(cfg) -> dict         # {"slots": 2, "held": [{"slot": 0, "campaign": ..., "since": ...}]}
```

- Slot files are `<agentic_dir>/gate-slots/slot-<n>.lock`, `n` in `0..slots-1`. A holder takes
  `flock(LOCK_EX | LOCK_NB)` on a free file and writes `{campaign, pid, since}` into it. The
  flock dies with the process, so a crashed runner never leaks a slot (the approach of
  `lease.py`). The pool is host-wide because the files sit under the shared data directory.
- Acquire polls every `poll_s` and checks `stop` and `gates._stopping` on each poll, like
  `_stop_requested`. A stop during the wait returns an interrupted run that starts no gate
  (SPEC-0229 semantics).
- `gates.run_all` wraps its whole body in `acquire`: one slot per run, held across every gate
  of the run and its retries. The `deadline` (`gate_budget_s`) and each gate's `timeout_s`
  count from the moment the slot is held, not from the wait.
- Settings sit under `agentic.gates.*` in the user config, with code-held defaults. A value that
  is not an integer from 1 to 16 falls back to its default, as `events.settings` does.
- `run_all` records `slot_wait_s` (rounded, 0.0 when no wait) in the gate payload
  (`gates.as_payload`). When the wait passes 30 s it first appends a `gate_waiting` ledger event
  `{slots, held, waited_s}`. `gate_waiting` is `hold` in `quiet.RULES` (informational), and the
  `/wt-conduct` skill's *Quiet stream* paragraph lists it with the other held types. A
  campaign waiting for a gate slot is working, not silent: the stuck and silent detectors in
  `events.py` do not raise `campaign_silent` or `state_stuck` for a GATING campaign while a
  gate-slot wait is in progress.
- `wt gate slots` (or `wt queue gates` if that is where `gates` is attached) prints
  `occupancy()` as text and as `--json`.

### 3. Affected scope in fix loops

Two new optional keys on a gate in `.wt/project.yaml`, validated in `project.py`
(`_check_mapping` allowed keys, `_check_gate_retry` neighbour):

```yaml
- name: pytest
  cmd: uv run pytest -q -x -n 4
  affected_cmd: uv run pytest -q -x -n 0 {affected}
  affected_tests: ["tests/test_*.py", "tests/**/test_*.py"]
```

- `affected_cmd` is a string containing `{affected}`; `affected_tests` is a non-empty list of
  repo-relative globs. Each requires the other. `affected_cmd` requires
  `results: {format: pytest}`, since the failing ids come from the parsed summary.
- `gates.affected_set(state, worktree, gate) -> list[str] | None` returns the ordered,
  de-duplicated union of:
  1. the failing ids of the campaign's `last_gate` result for this gate (pytest summary
     `failing`), only when that list is complete (the `_targeted_ids` rule: 1 to `MAX_FAILING` ids,
     accounting for every failure and error);
  2. the files changed since the merge base with the base branch
     (`git diff --name-only <merge-base>...HEAD` plus uncommitted changes) that match
     `affected_tests`.
  It returns `None`, meaning "run the full `cmd`", when the union is empty, when the previous
  failing list was incomplete, or when git fails.
- Scope is chosen in `run_all(..., scope="auto")`:
  - `auto` picks `affected` for a gate that declares `affected_cmd` only when the campaign
    already has a recorded gate (`state["last_gate"]` set) and the caller is the GATING step.
    Otherwise it picks `full`.
  - `queue._do_gate` passes `scope="auto"`. `close.prepare_close` and `wt gate run` pass
    `scope="full"`. A gate without `affected_cmd` always runs in full.
- Every gate result and the run payload carry `scope: "full" | "affected"`; for an affected
  gate the sidecar also records the `affected` list. A run with any affected gate has run
  `scope: "affected"`.
- The retry ladder is unchanged: a failing affected gate with complete failing ids still
  re-runs them via `retry_cmd`, and its full rerun is the gate's `affected_cmd`, not `cmd`.
- The last gate of a fix loop may be green on an affected scope only. That is safe because
  reviewers read the diff, and the close gate in step 4 runs in full whenever the tree is not one
  already proven by a full gate.

### 4. Reuse of a green tree at close

- `run_all` computes `tree` before its first gate: `git rev-parse HEAD^{tree}` in the worktree,
  but only when the worktree is clean including untracked files. It re-checks that tracked files
  are unchanged after the last gate (`git diff --quiet HEAD`). If a gate rewrote tracked files
  (the `spec-lint --write-ledger` gate does when the ledger is stale), `tree` is `None` for the
  run: the gates did not prove the tree they started from. The run payload carries `tree` and
  `gates_hash`, the sha256 of the canonical JSON of the project's `gates` list (name, cmd,
  cwd, required, results, retries, on_fail, retry_cmd, affected_cmd, affected_tests).
- When a run has `scope: "full"`, `ok` true, no interrupted gate and a `tree`, the queue stores
  `state["last_green_gate"] = {gate_seq, tree, gates_hash, finished_at}` in the same apply that
  sets `last_gate` (`queue.py` GATING branch and `_absorb_close_events`).
- `close.prepare_close` step 5, after the rebase and `_regenerate_after_rebase`, runs
  `gates.reusable_green(state, project_cfg, worktree, head_tree)`. It returns the recorded run
  when all of these hold, else `None` with a reason:
  - the project's `merge.regate` is `auto` (the default; `always` disables reuse);
  - `last_green_gate` exists and its `tree` equals `git rev-parse HEAD^{tree}` of the rebased
    worktree, and the worktree is clean;
  - its `gates_hash` equals the hash of the current `project_cfg["gates"]`;
  - the request is not a `--docs-only` merge (that path never gates).
- On reuse, `prepare_close` does not call `run_all`. It appends a `gate_reused` ledger event
  `{gate_seq, tree, target_sha, saved_s}` (`saved_s` is the source run's `duration_s`) and builds
  the `PreparedClose.gate_run` from the recorded payload, so `finish_close`, the merged
  `close` record (`gates_seq` is the source seq, plus `gates_reused: true`) and `pr_body`
  all see a green run. The PR table notes `reused from gate run <n>`. `gate_reused` is `hold` in
  `quiet.RULES`. When reuse is refused for a campaign that had a `last_green_gate`,
  `prepare_close` adds an `info` finding `regate: full (<reason>)` where the reason is one of
  `tree_changed`, `gates_changed`, `worktree_dirty`, `policy_always`, so the full re-gate is
  explained.
- `finish_close` is unchanged. It already requires the merged tree to equal the prepared head,
  so reuse cannot merge a tree other than the proven one.

### Config

- User config: `agentic.gates.slots` (int 1-16, default 2).
- `.wt/project.yaml`: gate keys `affected_cmd`, `affected_tests`; `merge.regate: auto | always`
  (default `auto`). `project.validate` rejects anything else with the key path.

## Alternatives considered

- **Raise profile `slots` or add CPU quotas per campaign.** `slots` bounds campaigns, and an
  idle campaign (implementing, reviewing) does not use the CPU, so a campaign cap is the wrong
  unit. A separate gate pool matches what saturates the host.
- **A bare `-n auto`.** It sizes workers to the host's cores, so N concurrent gates oversubscribe
  N times over. A fixed `-n 4` under a pool is predictable.
- **A coverage-based source-to-test map (pytest-testmon).** Better selection, but it needs a
  persistent per-worktree database and has its own invalidation failure modes. The changed
  tests plus the previous failures cover the fix-loop case, and the close gate backstops it.
- **"Base unchanged since the last green gate" as the reuse test.** It misses a rebased tree
  that is identical after the base moved, and it trusts that nothing else touched the branch.
  Hashing the tree is exact and costs one git call.
- **Skip the close re-gate whenever the last full gate was green.** Unsound when the base moved:
  the merge result was never tested.
- **Wait for a slot in the queue (a WAITING-like phase).** It would add a campaign state and
  reopen SPEC-0200's slot accounting. A blocking wait inside the gate step, with a ledger event,
  is smaller and keeps the state machine as it is.

## Acceptance criteria

- [ ] `pytest-xdist` is in the `dev` extra, `.wt/project.yaml`'s pytest gate uses `-n 4` with a
      serial `retry_cmd`, and `uv run pytest -q -n 4` passes on the full suite.
- [ ] Parallel-safety evidence: three consecutive full `-n 4` runs on an idle host all pass with
      no test failing in one run and passing in another. The three outcomes are recorded in *As
      shipped*.
- [ ] On an idle host the full `-n 4` suite takes at most 50% of the wall time of the serial run
      on the same tree. Both durations are recorded in *As shipped*.
- [ ] `gate_slots.acquire` never lets more than `agentic.gates.slots` holders in at once
      across processes; a holder killed with SIGKILL frees its slot; a waiter returns promptly on
      stop without starting a gate.
- [ ] `gates.run_all` holds one slot for the whole run. The gate payload has `slot_wait_s`, and
      `gate_waiting` is appended once the wait exceeds 30 s. `quiet.RULES` classifies
      `gate_waiting` and `gate_reused` as `hold`, and the `/wt-conduct` skill lists both.
- [ ] A GATING campaign waiting for a slot raises no `campaign_silent` or `state_stuck`.
- [ ] `project.validate` accepts `affected_cmd`/`affected_tests` and `merge.regate`, and rejects:
      `affected_cmd` without `{affected}`, `affected_cmd` without `affected_tests` or the
      reverse, `affected_cmd` without `results: {format: pytest}`, empty or absolute or `..`
      globs, and a `merge.regate` value other than `auto` or `always`.
- [ ] The first gate of a campaign, every `wt gate run`, and the close re-gate run the gate's full
      `cmd`. A later GATING step runs `affected_cmd` with the previous failing ids plus the
      campaign's changed test files. With an empty affected set it runs the full `cmd`.
- [ ] Gate results and the run payload carry `scope`; an affected gate's sidecar lists the
      affected set.
- [ ] A full-scope green run on a clean tree stores `last_green_gate`. An affected-scope run, a
      red run, an interrupted run, or a run whose gates rewrote tracked files does not.
- [ ] At close, a rebased tree equal to `last_green_gate.tree` with unchanged gate definitions
      skips `run_all`, appends `gate_reused`, merges, and records `gates_reused: true`. A
      different tree, changed gate definitions, a dirty worktree or `merge.regate: always`
      re-gates in full and records the reason.
- [ ] A zero-failure campaign run end to end by the fake runner executes exactly two full gate
      runs when the base moved (first gate, close gate) and exactly one when it did not.
- [ ] `uv run pytest` passes, and `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated: `tests/test_gate_slots.py`.** Uses `tests/waits.py`, never a fixed sleep before a
  positive assertion. Spawns three processes against `slots: 2` and asserts at most two hold at
  once and the third enters after one releases. A holder killed with SIGKILL frees its slot. A
  waiter with `stop` set returns an interrupted run, with no gate started. A bad
  `agentic.gates.slots` (`0`, `"x"`, `true`, `99`) falls back to the default. `occupancy` lists
  the holder.
- **Automated: `tests/test_gate_scope.py`.** Fake project with a git repo and a pytest gate whose
  `cmd`/`affected_cmd` are `echo` scripts, so the chosen command is visible in the log.
  Asserts the first gate and `wt gate run` use `cmd`; a second GATING step uses `affected_cmd`
  with the previous failing ids plus changed test files, de-duplicated and shell-quoted; an
  incomplete failing list or an empty set falls back to `cmd`; `scope` shows in the payload and
  sidecar; the retry ladder still works on an affected gate.
- **Automated: `tests/test_close_regate_reuse.py`.** Temp git repos and the fake runner. A:
  green full gate, no-op rebase, close skips the gates, emits `gate_reused`, merges, and the
  close record has `gates_reused: true`. B: base moved with an unrelated file, so the rebased
  tree differs and the close runs in full. C: base moved to the same resulting tree (the branch
  change already on base), reuse. D: `gates` list edited between the gate and the close, full
  re-gate with the reason `gates_changed`. E: a gate that rewrites a tracked file leaves
  `tree: None`, so no `last_green_gate` and a full close re-gate. F: `merge.regate: always`. G: an
  affected-scope last run and a red last run both leave `last_green_gate` unset. H: `pr_body`
  shows `reused from gate run <n>`.
- **Automated: `tests/test_agentic_project.py` (extend).** The `project.validate` cases in the
  acceptance list.
- **Automated: `tests/test_agentic_events.py` / `tests/test_quiet.py` (extend).** `gate_waiting` and
  `gate_reused` are `hold`, the skill's *Quiet stream* paragraph lists them (the existing
  comparison test), and a GATING campaign inside a slot wait raises no silent or stuck event.
- **Manual verification.**
  1. On an idle host, `time uv run pytest -q` then `time uv run pytest -q -n 4` on the same tree;
     record both durations and the ratio.
  2. Run `uv run pytest -q -n 4` three times; record the results. Any test that fails once and
     passes later is fixed or grouped before the spec is `done`.
  3. Queue two real campaigns together with `agentic.gates.slots: 1`. Check with
     `wt gate slots` that one gate runs while the other waits, and that the waiter's
     `gate_waiting` and `slot_wait_s` show in `wt queue events` and `wt campaign show`.
  4. Over the first 5 campaigns that merge after this ships, run
     `wt campaign export --since <ship date>` and report GATING minutes and full gate runs per
     campaign next to the 2026-10-30 baseline (about 60 min of gate time and 3-4 full gates).
     Record it in *As shipped*.
- **Regression guard.** Full `uv run pytest`. The existing gate tests (`tests/test_agentic_gates*.py`,
  the close and queue tests) pass unchanged. A project with none of the new keys behaves as
  before, apart from the slot pool, which defaults to 2 and bounds only concurrent gate runs.

## Rollout / migration

1. Merge the code with the new `project.yaml` keys unused. Reuse and slots go live, scope and
   `-n 4` stay off, so the first merge isolates their effect.
2. Fix any parallel-unsafe tests found by the three-run check, then switch wt's own gate to
   `-n 4` with the `retry_cmd`.
3. Add `affected_cmd` and `affected_tests` to wt's pytest gate.
4. Compare gate minutes per campaign over the next five merges. If a shortcut shows a
   regression (a campaign merged with a test red on main, or a gate flake rate above 1 in 20
   runs), set `merge.regate: always` or drop `affected_cmd` for that project. No data move is
   needed. New fields are only added to payloads and state, and older campaigns without
   `last_green_gate` re-gate in full.
5. The pool applies to every project the runner serves. For comfort-assistant, set its gate
   `cmd` worker count to its own suite.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed and
      the measurements are in *As shipped*.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

- None. The pool default (2) and worker count (4) come from 8 cores and the measured load; they
  are tuned from the first five merges' gate times, not escalated.
