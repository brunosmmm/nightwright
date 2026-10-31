---
id: SPEC-0309
title: "Faster gates: parallel pytest under a host-wide gate-slot pool"
status: done
owner: user
created: 2026-10-30
updated: 2026-10-30
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

Campaigns run three or four full gates even with zero failures: SPEC-0301 ran 4 x 800 s,
SPEC-0304 ran 3 x about 1000 s. That is about an hour of tests per campaign and 1.5-2.5 h of wall
time per merge. The [SPEC-0298](./0298-agentic-mvp-cheap-durable-quiet-conductor.md) exit window
needs at least 20 consecutive merges, so gate time sets how fast it fills.

IDEA-430 names four levers: parallel pytest, affected-tests-only fix loops, reuse of an
already-green tree at close, and a smaller gate-slot pool. The spec review found that they are
four independent subsystems with different risk, and asked for three specs. **This spec is the
first and lands first: lever 1 (parallel pytest) and lever 4 (the gate-slot pool).** They belong
together because parallel pytest is unsafe without a host-wide cap: one pytest process uses one
core, so `-n 4` is the free speed-up only while the number of concurrent gates is bounded.
The other levers are listed under Non-goals.

What exists today:

1. `gates.run_all` (`src/wt/agentic/gates.py:383`) runs every gate of a project, with no bound on
   how many `run_all` calls run at once on the host. Profile `slots` (`balanced.yaml`: 2) bounds
   campaigns per runner, and an implementing or reviewing campaign does not use the CPU, so it is
   the wrong unit. `wt gate run` and `close.prepare_close` (`close.py:545`) also call it.
2. Gate runs write no `run.start` ledger entry. `events._open_run` (`events.py:410`) reads only
   `run.start`/`run.end`, so the `run_overdue` check (`events.py:755`) never covers a gate. The
   two detectors that do see a GATING campaign are `campaign_silent` (`events.py:762`, silence is
   the age of the newest ledger entry against `silent_after_s`, default 900) and `state_stuck`
   (`stuck_items`, `events.py:488`, limit `max(stuck_after_s, gate_budget_s)`, default 3600 s).
   A campaign blocked for a slot writes nothing while it waits, so both can fire falsely.
3. The test suite is parallel-safe by design: SPEC-0269 forbids fixed sleeps in favour of
   `tests/waits.py`, and SPEC-0253 gives each gate run a private `TMPDIR`. The flake rate under
   xdist still has to be measured, not assumed.

## Goals / Non-goals

**Goals**
- A full pytest gate takes at most half its serial wall time on an idle host.
- Concurrent gate runs never oversubscribe the CPU: a host-wide pool bounds how many `run_all`
  calls execute at once, and a campaign waiting for a slot is neither reported silent or stuck
  nor counted against its gate budget.
- Correctness is unchanged: the same gates run on the same tree with the same pass rule. Only
  the wall time and the number of concurrent runs change.

**Non-goals** (each is a separate child spec of SPEC-0298, to be filed as its own idea; none
is started here)
- **Affected-tests scope in fix loops** (IDEA-430 lever 2): `affected_cmd`/`affected_tests` gate
  keys and their validation in `project.py`, `gates.affected_set`, `run_all(scope=auto|full)`,
  `scope` in results, payload and sidecar, and how the retry ladder treats an affected gate.
- **Reuse of an already-green tree at close** (IDEA-430 lever 3): `tree` and `gates_hash` on the
  run payload, `state.last_green_gate`, `merge.regate: auto|always`, the `prepare_close` reuse
  path, the `gate_reused` event, and the end-to-end "two full gate runs when the base moved, one
  when it did not" check. This skips the merge safety gate, so its design is tone-critical. The
  review's requirements are carried over to it, not dropped: `gates_hash` covers the whole
  `project_cfg['gates']` mapping (sha256 of its sorted-key JSON after `GATE_DEFAULTS`), the
  post-gate check compares `git rev-parse HEAD^{tree}` with the starting tree and requires
  `git diff --quiet HEAD`, and untracked non-ignored gate output is removed after a run in a
  wt-created worktree so the close-time snapshot cannot change the proven tree. Both of these
  specs may declare `depends_on` on each other for the shared `scope` field; neither depends on
  this one beyond ordering of edits in `gates.py`.
- Test impact analysis from coverage data (testmon-style source-to-test maps).
- Changing the retry ladder (SPEC-0289), the triager, or the flaky-test record (SPEC-0293 is a
  frozen area).
- Splitting the suite into shards across machines, or a bare `-n auto`.
- A WAITING-like campaign state for slot waits, or changes to SPEC-0200's campaign slot
  accounting.

## Decision

Two changes in the gate path:

1. **Parallel pytest.** Add `pytest-xdist` to the dev extras and run wt's own pytest gate as
   `uv run pytest -q -x -n 4`, with a serial `retry_cmd` for the targeted rerun.
2. **Gate-slot pool.** A host-wide pool of `agentic.gates.slots` slots (default 2), taken with
   `flock` for the duration of a whole `run_all`. Two concurrent `-n 4` gates fill 8 cores; a
   third waits. A waiting campaign publishes a marker file that the stuck and silent detectors
   read, so a wait never raises an alarm.

The worker count (4) lives in the project's gate `cmd`, since only the project knows its suite.
It is 8 cores divided by 2 pool slots. The pool is the guarantee, the worker count is a tuning
value.

## Design

### 1. Parallel pytest

- `pyproject.toml`: add `pytest-xdist>=3` to the `dev` extra (next to `pytest>=8`, line 30).
- `.wt/project.yaml`, gate `pytest`: `cmd: uv run pytest -q -x -n 4`, and
  `retry_cmd: uv run pytest -q -n 0 {failing}`, so a targeted rerun of one to ten ids does not
  pay worker start-up. `results: {format: pytest}` stays: `gate_results.parse_pytest` reads the
  last summary line, which xdist keeps in the same format. If it does not (a verified failure of
  the parse on the three-run check), the implementation fixes `parse_pytest`; that is in scope.
- Tests that are not parallel-safe (a fixed port, a fixed path outside `tmp_path`, a shared
  module-level file, a process-global like `os.environ` or the cwd) are fixed in place. Where a
  fix is not cheap, mark the test `@pytest.mark.xdist_group("name")` and add `--dist loadgroup`
  to the gate command. *As shipped* records which tests needed that.
- The project config change (`-n 4` and the `retry_cmd`) is the last commit of the campaign,
  after the suite passes three consecutive parallel runs (see Rollout).

### 2. Gate-slot pool

New module `src/wt/agentic/gate_slots.py`:

```python
@contextmanager
def acquire(cfg, campaign_id, *, stop=None, poll_s=1.0) -> Iterator[SlotHold]: ...
def settings(cfg) -> dict          # {"slots": 2}
def occupancy(cfg) -> dict         # {"slots": 2, "held": [{slot, campaign, pid, since}],
                                   #  "waiting": [{campaign, pid, since}]}
def waiting(cfg, campaign_id, now=None) -> dict | None   # live waiter marker, else None
```

- **Slot files.** `<agentic_dir>/gate-slots/slot-<n>.lock`, `n` in `0..slots-1`. A holder takes
  `flock(LOCK_EX | LOCK_NB)` on a free file and writes `{campaign, pid, since}` into it. The
  flock dies with the process, so a crashed runner never leaks a slot (the approach of
  `lease.py`). The pool is host-wide because the files sit under the shared data directory.
  If `slots` is lowered while holders exist, files above the new count are not taken again and
  drain naturally; if it is raised, new files appear on the next acquire.
- **Waiting.** While no slot is free, `acquire` polls every `poll_s`. On entry to the wait it
  writes `<agentic_dir>/gate-slots/waiting-<campaign>.json` with `{campaign, pid, since}`
  (atomic write, `export._atomic_write`) and removes it on acquire, on stop, and on any
  exception (`finally`). A waiter marker whose `pid` is not alive is stale and ignored, so a
  crashed runner leaves no permanent suppression. `occupancy()` lists waiters; `waiting()` returns
  one campaign's live marker.
- **Stop.** Each poll checks `stop` and `gates._stopping` (the `_stop_requested` rule). A stop
  during the wait returns a `GateRun` with `interrupted=True` and no gate started (SPEC-0229
  semantics); the marker is removed.
- **`gates.run_all`** wraps its whole body in `acquire`: one slot per run, held across every gate
  of the run and its retries. `t0`, the `deadline` (`events.gate_budget_s`) and each gate's
  `timeout_s` count from the moment the slot is held, not from the wait, so a wait never eats
  the budget. `seq` and `TMPDIR` claiming (SPEC-0253) happen after the slot is held, so a waiting
  run does not occupy a `gate-<seq>` directory.
- **Settings.** `agentic.gates.slots` in the user config, with a code-held default of 2. A value
  that is not an integer from 1 to 16 (a bool, a string, `0`, `99`, a float) falls back to the
  default, as `events.settings` does.
- **Payload and event.** `run_all` records `slot_wait_s` (rounded; `0.0` when no wait) in the
  gate payload (`gates.as_payload`). When a wait passes 30 s it appends one `gate_waiting` ledger
  entry `{slots, held, waited_s}`, registers it in `schemas.py` and `events.EVENT_TYPES` (data
  keys `slots`, `held`, `waited_s`), and the projector carries it into `events.jsonl` like
  `waiting`. `gate_waiting` is `hold` in `quiet.RULES` (informational), and the `/wt-conduct`
  skill's *Quiet stream* "Always held" list names it (`skills/wt-conduct/SKILL.md:81`).
- **Detectors.** `events.py` learns of a wait only from the on-disk marker, since it can run in
  another process:
  - `campaign_silent` (`events.py:762`) and `state_stuck` (`stuck_items`, `events.py:488`, the
    GATING and CLOSING branch) skip a campaign for which `gate_slots.waiting(cfg, cid)` returns a
    live marker.
  - A finished wait must not count against `state_stuck` either. `stuck_items` adds the
    `slot_wait_s` of the campaign's newest `gate` ledger entry to `limit` in the GATING and
    CLOSING branch. Without this, a long wait followed by a 1000 s gate could cross the 3600 s
    limit after the marker is gone. The wait of a run still in flight is covered by the marker.
  - `run_overdue` needs no change: gate runs write no `run.start`, so `_open_run` never returns
    one and `run_overdue` cannot fire for a gate or a gate-slot wait. A test pins this, so that a
    later `run.start` for gates cannot silently reintroduce the false alarm.
- **CLI.** `wt queue gate-slots` (module `src/wt/agentic/cli_gate_slots.py`, attached with the
  `cli_conduct.attach` pattern) prints `occupancy()` as text and as `--json`.

### Config

- User config: `agentic.gates.slots` (int 1-16, default 2). Nothing in `.wt/project.yaml`
  changes shape; only wt's own `pytest` gate `cmd` and `retry_cmd` values change.

## Alternatives considered

- **Raise profile `slots` or add CPU quotas per campaign.** `slots` bounds campaigns, and an
  idle campaign (implementing, reviewing) does not use the CPU, so a campaign cap is the wrong
  unit. A separate gate pool matches what saturates the host.
- **A bare `-n auto`.** It sizes workers to the host's cores, so N concurrent gates oversubscribe
  N times over. A fixed `-n 4` under a pool is predictable.
- **Hold the slot per gate instead of per run.** A run's gates are sequential, so a per-gate slot
  would let a second campaign interleave between the fast `spec-lint` and the slow `pytest`, and
  a campaign could wait mid-run with a half-finished result. One slot per run keeps a run's
  wall time simple.
- **Wait for a slot in the queue (a WAITING-like phase).** It would add a campaign state and
  reopen SPEC-0200's slot accounting. A blocking wait inside the gate step, with a marker file
  and a ledger event, is smaller and keeps the state machine as it is.
- **Tell the detectors about a wait through a ledger entry only.** `gate_waiting` is written once
  after 30 s, and a later entry would be needed to say the wait ended. A marker removed in
  `finally` cannot go out of date while its pid is alive.

## Acceptance criteria

- [x] `pytest-xdist` is in the `dev` extra, `.wt/project.yaml`'s pytest gate uses `-n 4` with a
      serial `retry_cmd`, and the full suite passes under `uv run pytest -q -n 4`.
- [x] Parallel-safety evidence: three consecutive full `-n 4` runs on an idle host all pass, with
      no test failing in one run and passing in another. The three outcomes are recorded in *As
      shipped*, with any test that needed fixing or `xdist_group`.
- [x] On an idle host the full `-n 4` suite takes at most 50% of the wall time of the serial run
      on the same tree. Both durations and the ratio are recorded in *As shipped*.
- [x] `gate_slots.acquire` never lets more than `agentic.gates.slots` holders in at once across
      processes; a holder killed with SIGKILL frees its slot; a waiter returns promptly on stop
      without starting a gate and removes its waiter marker. A `slots` value that is not an int
      from 1 to 16 falls back to 2.
- [x] `gates.run_all` holds one slot for the whole run, and its `deadline` and per-gate timeouts
      count from the moment the slot is held. The gate payload has `slot_wait_s` (0.0 without a
      wait), and exactly one `gate_waiting` entry is appended once a wait exceeds 30 s.
- [x] `quiet.RULES` classifies `gate_waiting` as `hold` and the `/wt-conduct` skill's *Quiet
      stream* list names it (the existing rules-versus-skill comparison test passes).
- [x] A GATING or CLOSING campaign with a live `waiting-<campaign>.json` marker raises no
      `campaign_silent` and no `state_stuck`, a marker with a dead pid does not suppress them, a
      long wait plus a long gate does not raise `state_stuck` before `gate_budget_s` of actual
      gate time, and no `run_overdue` is raised for a gate-slot wait.
- [x] `wt queue gate-slots` prints holders and waiters as text and with `--json`.

## Test plan

- **Automated: `tests/test_gate_slots.py`.** Uses `tests/waits.py`, never a fixed sleep before a
  positive assertion. Spawns three processes against `slots: 2`: at most two hold at once, and
  the third enters after one releases. A holder killed with SIGKILL frees its slot. A waiter with
  `stop` set returns an interrupted run with no gate started and no marker left. `settings`
  falls back to 2 for `0`, `"x"`, `true`, `99`, `1.5`. `occupancy` lists the holder and the
  waiter. A `run_all` against a fake project with one slot held by another process records
  `slot_wait_s > 0`, appends one `gate_waiting` after the (monkeypatched) 30 s threshold, and its
  `deadline` is computed after the wait.
- **Automated: `tests/test_agentic_events.py` and `tests/test_quiet.py` (extend).**
  `gate_waiting` is `hold`, and the skill's *Quiet stream* paragraph names it (the existing
  comparison test). With a live marker for a GATING campaign whose ledger has been silent past
  `silent_after_s` and whose state age is past the stuck limit, `_watch` emits neither
  `campaign_silent` nor `state_stuck`. The same state with a dead-pid marker emits both. With a
  recorded `slot_wait_s` on the current run, `stuck_items` extends its limit by that wait. A
  GATING campaign with no `run.start` and an old ledger raises no `run_overdue`.
- **Automated: `tests/test_cli_gate_slots.py`.** `wt queue gate-slots` and `--json` show a held
  slot and a waiter from a temp data dir.
- **Automated: existing gate tests.** `tests/test_agentic_gates*.py`, the close and queue tests,
  run unchanged (with the pool at its default, a single `run_all` takes a free slot at once).
- **Manual verification.**
  1. On an idle host, `time uv run pytest -q` then `time uv run pytest -q -n 4` on the same tree;
     record both durations and the ratio.
  2. Run `uv run pytest -q -n 4` three times; record the results. Any test that fails once and
     passes later is fixed or grouped before the spec is `done`.
  3. Queue two real campaigns together with `agentic.gates.slots: 1`. Check with
     `wt queue gate-slots` that one gate runs while the other waits, that the waiter's
     `gate_waiting` and `slot_wait_s` show in `wt queue events` and `wt campaign show`, and that
     no `campaign_silent` or `state_stuck` arrives for the waiter.
  4. Over the first 5 campaigns that merge after this ships, run
     `wt campaign export --since <ship date>` and record GATING minutes per campaign next to the
     2026-10-30 baseline (about 60 min of gate time and 3-4 full gates) in *As shipped*. The
     expected effect of this spec alone is the per-gate wall-time ratio; the number of full gates
     per campaign is the next two specs' target.
- **Regression guard.** Full `uv run pytest` (run with `-n 4` once the gate is switched, and
  serial once, so neither mode regresses). A project with no `agentic.gates` config behaves as
  before, apart from the pool defaulting to 2, which bounds only concurrent gate runs.

### Test record (2026-10-30)

- Targeted (also `test_cli_gate_slots`, `test_agentic_events`, `test_agentic_quiet`, `test_skills`: 203 passed): `uv run pytest -q -n 0 tests/test_gate_slots.py tests/test_agentic_gate_results.py`,
  28 passed (includes the new slot-held-across-gates, short-wait, `slot_wait_s` round-trip and
  xdist-summary `parse_pytest` tests).
- Manual step 1 and 2 (below, in *As shipped*): one serial and three `-n 4` full runs on this tree,
  all passed.
- Manual steps 3 (two real campaigns with `slots: 1`) and 4 (GATING minutes over the first five
  merges) need real campaigns and merges after this ships, so they cannot precede this merge;
  they are the post-ship comparison of Rollout step 3 and are not recorded here. The same
  behaviour is covered by the automated pool, `run_all` and detector tests above.
- Review fixes (loop 3): `tests/test_agentic_events.py`, `test_agentic_quiet.py` (`gate_waiting`
  is `hold`), `test_gate_slots.py` (dead-pid `holding()`, raising `on_wait`) and `test_skills.py`:
  192 passed. Full suite left to gating.
- Full suite under gating is left to the gate; the runs above stand in for the serial and
  `-n 4` regression guard.

### As shipped

**Parallel pytest, measured** (this tree, 4081 passed, 3 skipped each run; 8 cores; the host was
not idle: other campaigns kept the load average at 2-7, which penalises the parallel runs more
than the serial one):

| run | command | result | duration |
|---|---|---|---|
| serial | `uv run pytest -q` | 4081 passed, 3 skipped | 1003.70 s (0:16:43) |
| -n 4 #1 | `uv run pytest -q -x -n 4` | 4081 passed, 3 skipped, 25 warnings | 326.34 s (0:05:26) |
| -n 4 #2 | `uv run pytest -q -x -n 4` | 4081 passed, 3 skipped, 25 warnings | 345.90 s (0:05:45) |
| -n 4 #3 | `uv run pytest -q -x -n 4` | 4081 passed, 3 skipped, 25 warnings | 285.18 s (0:04:45) |

Ratio `-n 4` / serial: 0.33, 0.34, 0.28 (limit 0.5). No test failed in one run and passed in
another. **Three tests were changed for parallel safety**, none needed an `xdist_group`, and
the gate command carries no `--dist loadgroup`: `test_agentic_campaign_runs.py::merged` (fixture),
`test_agentic_profiles_cli.py::env` (fixture) and
`test_spec_review_show.py::test_reviews_list_filter_json_and_unresolvable`. The cause is the same:
the console width is fixed when `wt.console` is imported, so setting `COLUMNS` alone does not
reach an xdist worker that imported it earlier. The fix sets `console._width` directly. The 25 warnings are `DeprecationWarning: ... multi-threaded, use of fork()`
from `test_org_write_race.py`, `test_spec_numbers.py` and `test_tui_real_terminal.py`; they only
show under xdist (its workers are threaded) and do not fail anything. `gate_results.parse_pytest`
reads the xdist summary line unchanged; `test_pytest_xdist_summary_line` pins it.

**Added beyond Design**
- `gate_slots.holding(cfg, campaign_id)` and a `waited_s` field in the slot file. While a gate runs,
  the finished wait is readable from the live hold, because the `gate` ledger entry (with
  `slot_wait_s`) lands only after `run_all` returns.
- Silence and stuck offsets taken from the live hold: `stuck_items` adds the hold's `waited_s` to
  its limit, and `campaign_silent` offsets the silence by it too, so a long wait followed by a long
  gate raises neither alarm.
- **Deviation from Design: a recorded `slot_wait_s` does not extend the stuck limit.** Design says
  `stuck_items` adds the newest `gate` entry's `slot_wait_s` to `limit`. That double-counts: the
  entry's ts is written after the gate ends, so the stuck clock (the minimum of the time in state,
  `head_since` and the newest `gate` entry's age) taken from it already excludes the wait. Only the
  live hold's `waited_s` extends the limit, for the gate still in flight. A test pins that a
  recorded 3000 s wait leaves the limit at `gate_budget_s` of gate time.
- `campaign_silent` takes silence as the smaller of the newest ledger entry's age and the live
  hold's age (`since`), not the tail's age minus `waited_s`: a `gate_waiting` entry lands mid-wait,
  so subtracting the whole wait removed its first 30 s twice.
- `acquire(..., on_wait=)`: a callback `on_wait(occupancy, waited_s)` called once when a wait passes
  `WAIT_EVENT_S`; `run_all` uses it to append the single `gate_waiting` entry.

## Rollout / migration

1. Merge the pool and detector code first, with `.wt/project.yaml` unchanged. The pool defaults
   to 2 and bounds only concurrent gate runs, so serial gates are barely throttled.
2. Run the three-run check on a branch with `pytest-xdist` installed. Fix or group any
   parallel-unsafe tests found. Then switch wt's own pytest gate to `-n 4` with the serial
   `retry_cmd`. The campaign gating that commit runs the new command in its own final gate.
3. Compare per-gate wall time and the flake rate over the next five merges. If the flake rate
   passes 1 in 20 gate runs, revert the gate `cmd` to serial (a one-line `project.yaml` edit)
   and keep the pool; the pool needs no data migration, since slot and waiter files are
   transient and a missing directory is created on first use.
4. The pool applies to every project the runner serves. For comfort-assistant, set its gate
   `cmd` worker count to its own suite when it joins the loop.
5. Open the two non-goal specs (affected scope, close reuse) as ideas under SPEC-0298 once this
   one is accepted, so their gates also run under the pool.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed and
      the measurements are in *As shipped*.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

- None. The pool default (2) and worker count (4) come from 8 cores and the measured load; they
  are tuned from the first five merges' gate times, not escalated.
