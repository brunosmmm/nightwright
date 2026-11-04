---
id: SPEC-0426
title: Deflake the load-sensitive remote-gate, heartbeat and job-lease tests
status: done
owner: user
created: 2026-11-04
updated: 2026-11-04
kind: feature
source_idea: IDEA-957
tags: [tests, flake, remote-gates]
---

## Context

Three tests fail intermittently when the suite runs under xdist load on a busy gate box. They have
blocked the gates of IDEA-909 and IDEA-409, both unrelated to them:

1. `tests/test_remote_gates.py::test_transport_failures_fall_back_with_cooldown[drop-ssh]`: a fake
   `ssh` (`Remote.variants["drop"]`, driven by `_DROP_AFTER_GATE`, `tests/test_remote_gates.py:181`)
   `os._exit(255)`s as soon as the first `gate` frame has been relayed. The test then expects a
   fall-back at stage `ssh`, a cooldown, exactly three local results, an exact gate-file listing,
   no leftover child tmp dir and no `refs/nw/gate-jobs/` ref (`_assert_fell_back`,
   `tests/test_remote_gates.py:539`). It runs a real `nw worker ssh-command` per ssh call with
   the dispatch loop polling every `_POLL_S` (0.2 s), so under load the outcome depends on how
   the dropped connection, the still-running worker job and the local re-run interleave.
2. `tests/test_agentic_nonblocking.py::test_heartbeat_spans_many_ticks`: runs a 7 s agent step with
   `lease_ttl_s = 8` (heartbeat every ttl/4 = 2 s) and asserts no lease sample is `stale`,
   `max(heartbeat_age_s) < 8`, more than 10 samples (taken every 0.2 s by a side thread) and more
   than 10 ticks (poll 0.05 s). A starved beat thread or sampler lands the assertions on the edge.
3. `tests/test_worker_jobs.py::test_a_long_silent_gate_is_never_cancelled_while_attached`: a 5 s
   silent gate with `job_lease_s = 2` attached in-process; asserts the lease never lapsed
   (`lease_until < time.time()` seen by a 20 ms watcher) and that the lease rewrites are at most
   `2/3 + 2 * WJ.FOLLOW_TICK_S` (= 1.67 s) apart. With a 2 s lease and a 0.5 s follow tick a
   scheduling stall of about a second fails either assertion, with nothing wrong in the code.

All three assert time-derived bounds that are tight relative to the stalls an xdist-loaded box
produces (a stall of 1-2 s is routine there). The code under test is correct; the tests' time
margins are not. The same class of failure was fixed in `f14d0da8` for
`test_an_upload_longer_than_the_slot_wait_stays_remote` (slot wait 1 s -> 4 s, upload 2 s -> 5 s,
with a comment saying why) and earlier for the lease-expiry test in `test_worker_jobs.py` (a long
first lease so a slow checkout cannot spend the short one). SPEC-0269 (`tests/waits.py`) already
supplies `until`/`join` and `WAIT_S` (`NW_TEST_WAIT_S`, default 60 s) for positive waits.

I could not reproduce the failures on a quiet 8-CPU box, nor with 16 CPU-burner processes or
pinning the run to one burner-shared CPU (all three pass, 7 test cases). The fix therefore rests on
reading the assertions' margins, and the acceptance below proves load tolerance with injected stalls
instead of waiting for the flake.

## Goals / Non-goals

**Goals**
- The three tests pass when any single wait, thread wake-up or subprocess start in the test world
  is delayed by several seconds, without weakening what each test proves (fall back at the `ssh`
  stage with cooldown and clean worker residue; the heartbeat thread beats for the whole run
  across many ticks; an attached job's lease is renewed throttled and never lapses).
- Time margins in the three tests are derived from named constants and the code's own
  ratios, with a comment saying why (as in `f14d0da8`), rather than literals.
- The fix is test-only.

**Non-goals**
- Changing any production code, default (`DEFAULT_SLOT_WAIT_S`, `FOLLOW_TICK_S`, lease ttl or
  heartbeat ratio), or the worker protocol.
- Auditing every timing-sensitive test in the suite. Only the three named tests, plus any
  sibling case parametrized into the same function, are in scope; other flakes get their own idea.
- Making the suite faster. A deflaked test may run a few seconds longer if that buys margin.

## Decision

Keep each test's observable claim, and widen the margin it is judged against so that a multi-second
stall cannot change the verdict: make time bounds relative to a lease/ttl/slot wait that is larger
than any plausible stall, and make every "positive eventually" check a `tests/waits.py` wait
instead of a snapshot at a fixed moment. Where an assertion pins a duration (the heartbeat and lease
gaps), replace the absolute upper bound by a bound that scales with the configured interval plus a
named load slack, and keep a lower bound that still proves throttling. Each test gets a
deterministic load-simulation companion that injects the stall, so the property "survives a
stall" is checked on every run rather than only when the box happens to be busy.

## Design

All changes are in `tests/` (the three files above, plus `tests/waits.py` only if a helper is
shared). No file under `src/` changes.

### 1. `[drop-ssh]` (`tests/test_remote_gates.py`)

First establish which interleaving flakes, by running the case in a loop under the stall injection
below and reading the assertion that fails. The single stage that may legitimately vary is none:
the stage is `ssh` whatever the worker side was doing (`line is None` -> `RemoteFailure("ssh")`),
so the stage assertion is never loosened. Candidate sources, each handled by a fix that keeps
the claim:

- The `child_tmp` dir and the `refs/nw/gate-jobs/` refs in `world.repo` are local state cleaned
  up before `run_all` returns (`store.clear_child_tmp` in the `finally` of `run_all`,
  `src/nightwright/agentic/gates.py:803`; the `update-ref -d` in the `finally` of the bundle
  step, `src/nightwright/agentic/remote_gates.py:355`, which runs before ssh is even started).
  A still-running `nw worker ssh-command` cannot change either, so they stay direct asserts
  and are not wrapped in `until`: doing so would hide a regression where `run_all` stops
  cleaning up before it returns. `until` is used only for worker-side state that a
  still-running worker job can change after the drop, if the diagnosis shows one is asserted.
- Frame ordering under load: `_DROP_AFTER_GATE` already flushes after every relayed line and
  exits only after a whole line (`tests/test_remote_gates.py:181-187`), so a half-read line
  from the script itself is not a source. The test must not depend on how many frames the
  dispatcher saw before the drop.
- Any literal `slot_wait_s`/timeout set for the case (none today: the default 300 s applies) is
  left alone; do not add a short one.

If the injected stalls do not reproduce a failing assertion, the implementer makes the test
report which assertion failed and with what values (an assertion message in `_assert_fell_back`
carrying `world.ledger(["worker_failed"])`, the cooldown record and the gate-file listing). Each
tight or snapshot assertion is then judged against the drop: one that depends on worker-side
timing (named in the As shipped note) is widened; the rest stay. The diagnosis outcome,
including "not reproduced", is recorded in the spec's As shipped note.

The companion test `test_drop_ssh_survives_a_slow_worker_side` registers a variant of the drop
script that sleeps 3 s after relaying the `gate` frame before `os._exit(255)` (the stall), and
runs the same assertions via a shared `_run_drop(world, remote, script)` helper, so the case
and the stalled case share `_assert_fell_back`.

### 2. `test_heartbeat_spans_many_ticks` (`tests/test_agentic_nonblocking.py`)

The claim is "one heartbeat thread beats across a run that spans many ticks and the lease is
never stale". A module constant `LOAD_STALL_S = 2.0` is the one stall budget: the longest delay a
single beat, sampler wake-up or poll must survive. `_heartbeat_while` beats every
`max(1.0, ttl/4)` s (`src/nightwright/agentic/queue.py:5589`); the implementer derives the
constants from that code and keeps these ratios. Use `lease_ttl_s = 20` (>= 10 x
`LOAD_STALL_S`, so a lease is only stale after a beat lags by 20 s) and a step of
`3 * (ttl/4 + LOAD_STALL_S)` = 21 s, so at least 3 distinct `heartbeat_at` values (the acquire
stamp and two beats) still appear when every beat lags by `LOAD_STALL_S`. "Many beats" is
proved directly: assert at least 3 distinct `heartbeat_at` values in the samples, rather than
inferring it from `max(heartbeat_age_s) < ttl`. The sample-count assertions
(`len(samples) > 10`, `ticks > 10`) become `len(samples) >= 5` and `ticks >= 5`, which a
21 s run still meets with the sampler or poll stalled `LOAD_STALL_S` on every wake-up: they
prove "many" without assuming the sampler was never starved. The `beaters` set is read from
within the sampling loop as today.

The companion test `test_heartbeat_survives_a_stalled_beat` monkeypatches
`Lease.heartbeat` (`src/nightwright/agentic/lease.py:59`) to delay one beat by `LOAD_STALL_S` (2 s) the first
time it runs after the first tick, and asserts the lease stayed non-stale and a single beat thread
was used.

### 3. `test_a_long_silent_gate_is_never_cancelled_while_attached` (`tests/test_worker_jobs.py`)

The claim is "attach renews the lease every `min(30, lease/3)` s while a gate is silent, so the job
is never cancelled". Use `job_lease_s = 6` (renewal every 2 s) and a 7 s silent gate (>= 3
renewals): the lease has a 6 s margin where it had 2 s. Both the `_conf` call and the
`WJ.attach(_cfg(...))` call take the same value, held in one local constant. Gap bounds:
`min(gaps) >= every - 0.05` stays (it proves throttling), and the upper bound becomes
`every + 2 * WJ.FOLLOW_TICK_S + LOAD_SLACK_S` where `LOAD_SLACK_S = 1.0`, a named constant
with a comment: the hard guarantee is `not lapsed`, the gap bound only catches a renewal that
never throttles back up. The `lapsed` check compares against `time.time()`, which is what the code
uses (`rec["lease_until"]` is wall time), so it stays.

The companion `test_attach_renewal_survives_a_stalled_follow_tick` monkeypatches
`WJ.time.sleep`-based follow tick (the `_Clock` helper already in the file,
`tests/test_worker_jobs.py:315`, fakes monotonic time; reuse it) to advance 3 s in one tick and
asserts the lease is rewritten on the next tick, so a stall is covered deterministically without
real time.

### Shared conventions

- Constants for margins are module-level with a one-line comment, in the style of the comment
  `f14d0da8` added.
- No fixed `time.sleep` precedes a positive assertion; use `until`/`join` (SPEC-0269).
  `tests/test_test_waits.py` must still pass.
- The load simulation uses injected delays, not real CPU burners, so it is deterministic and
  cheap and runs in CI.

## Alternatives considered

- **Mark the three tests `@pytest.mark.flaky` / rerun on failure** — hides the margin problem,
  masks a real regression that fails 1 run in 3, and the repo has no rerun plugin.
- **Raise only the xdist worker count limits or run these tests serially** — shifts the
  problem onto the gate queue's wall time and the next test to be loaded; the root cause is the
  test margins.
- **Retry the production code paths longer (bigger `FOLLOW_TICK_S` / slot waits)** — changes
  product behavior to satisfy a test world; out of scope and wrong direction.
- **Scale all waits by a global load factor from `os.getloadavg()`** — makes the suite
  nondeterministic and the margin invisible in the test source.

## Acceptance criteria

- [x] `git diff main...HEAD --name-only` for the implementing change lists only files under
      `tests/` (plus this spec and the generated ledger); nothing under `src/`.
- [x] `test_transport_failures_fall_back_with_cooldown[drop-ssh]` and the stalled-drop companion
      both pass; with the drop delayed 3 s the stage is still `ssh`, cooldown set, three local
      results, no leftover `child_tmp` or `refs/nw/gate-jobs/` at return of `run_all`
      (direct asserts; neither is wrapped in `until`).
- [x] If the stall injection does not reproduce a failing assertion, `_assert_fell_back`
      carries assertion messages with the ledger event, cooldown record and gate-file listing,
      and the As shipped note records the diagnosis outcome (including "not reproduced").
- [x] `test_heartbeat_spans_many_ticks` uses `lease_ttl_s = 20` (>= 10 x `LOAD_STALL_S` = 2.0), a
      21 s step (3 beats at interval + `LOAD_STALL_S`), asserts at least 3 distinct
      `heartbeat_at` values, `len(samples) >= 5` and `ticks >= 5`; the distinct-beat minimum
      still holds when every beat lags by `LOAD_STALL_S`.
- [x] `test_heartbeat_survives_a_stalled_beat` delays one beat by `LOAD_STALL_S` (2 s) and the lease is never
      `stale` and exactly one `nw-lease-heartbeat` thread ran.
- [x] `test_a_long_silent_gate_is_never_cancelled_while_attached` uses `job_lease_s = 6` with
      a >= 7 s gate, `not lapsed`, `min(gaps) >= every - 0.05` and
      `max(gaps) <= every + 2 * FOLLOW_TICK_S + LOAD_SLACK_S`.
- [x] `test_attach_renewal_survives_a_stalled_follow_tick` passes: a 3 s stall in one follow
      tick still yields a renewal on the next tick.
- [x] Each widened margin sits in a named constant with a comment explaining the stall it
      tolerates; no new bare time literal is introduced without one.
- [x] The three tests (and companions) pass 10 consecutive runs each while the box carries
      CPU-burner load (`nproc * 2` busy loops) and when run pinned to one CPU
      (`taskset -c 0`).
- [x] `tests/test_test_waits.py` and `python3 tools/spec_lint.py` pass.

## Test plan

- **Automated tests:** the three named tests, kept in place and widened per Design 1-3, plus the
  three load-simulation companions (`test_drop_ssh_survives_a_slow_worker_side`,
  `test_heartbeat_survives_a_stalled_beat`, `test_attach_renewal_survives_a_stalled_follow_tick`)
  in the same files. They assert the stage/cooldown/residue outcome under a 3 s worker-side
  stall; no stale lease with one beat delayed 2 s; renewal on the next tick after a 3 s stalled
  follow tick. `tests/test_test_waits.py` guards the no-fixed-sleep convention.
- **Manual verification:** with the box loaded, run each of the following 10 times and expect
  10 passes:
  `for i in $(seq 1 $(( $(nproc) * 2 ))); do (timeout 600 sh -c 'while :; do :; done' &); done`
  then
  `for r in $(seq 10); do uv run pytest -q -p no:cacheprovider tests/test_remote_gates.py::test_transport_failures_fall_back_with_cooldown tests/test_agentic_nonblocking.py::test_heartbeat_spans_many_ticks tests/test_worker_jobs.py::test_a_long_silent_gate_is_never_cancelled_while_attached; done`
  and again prefixed with `taskset -c 0`. Then `pkill -f 'while :; do :; done'`. Record the
  pass counts in the spec's As shipped note. If a diagnosed flake interleaving was found
  (for `[drop-ssh]`), note which assertion it was.
- **Regression guard:** `python3 tools/affected_tests.py --run uv run pytest -q` and the full
  files `tests/test_remote_gates.py`, `tests/test_agentic_nonblocking.py`,
  `tests/test_worker_jobs.py`; the `git diff --stat` shows no `src/` change, so product
  behavior is unchanged.

### Test record (2026-11-04)

- `uv run pytest -q -p no:cacheprovider -n 16 tests/test_remote_gates.py tests/test_agentic_nonblocking.py tests/test_worker_jobs.py tests/test_test_waits.py`: 222 passed (once; the 10x xdist repeat of the review's suggestion was not run).
- Loaded-box loop (16 `while :; do :; done` burners = nproc x 2) over the six tests (the three named plus the three companions), 10 runs: 10/10 passed. Same set pinned with `taskset -c 0` under the same burners, 10 runs: 10/10 passed. These runs pass before the change too (the flake was never reproduced on this box), so they are a regression guard; the load-tolerance proof is the three injected-stall companions.
- `python3 tools/spec_lint.py` passes; the ledger was regenerated with `--write-ledger`. The full suite was left to gating.

### As shipped (2026-11-04)

- No `src/` change; only the three test files.
- **`[drop-ssh]` diagnosis: reproduced, via the stalled-drop companion.** With the drop delayed 3 s
  the gate-file listing assertion failed (`001-flaky.r1.*` missing). The world's `flaky` gate
  kept one counter file shared by the worker and the local run; the worker job keeps running
  after the connection drops, so it can spend the counter's first failure before the local
  re-run reaches it, and then the local run passes first try and writes no `.r1` files. Which side
  got there first is exactly what a loaded box shuffles. The fix is in the test world, not a
  widened assertion: `World.split_counter()` gives the `flaky` gate one counter per side
  (`$NW_DATA_DIR` containing `wdata` is the worker) and `_run_drop` applies it. Only the drop cases
  use it: the other cases' gate text is pinned by the golden record. The stage stays `ssh` and no
  assertion is loosened. `child_tmp` and `refs/nw/gate-jobs/` stay direct asserts.
  `_assert_fell_back` now carries the ledger events, cooldown record and gate listing in its
  messages. The shared `_assert_fell_back` is called through `_run_drop` for both `drop` and
  the new `slowdrop` variant (`SLOW_DROP_S = 3.0`), which delays `os._exit(255)` inside the same
  script (`DROP_DELAY_S` placeholder, 0 for the plain variant).
- **Heartbeat:** `LOAD_STALL_S = 2.0`, `SAMPLE_S = 0.2`; `lease_ttl_s = 20`, step
  `3 * (ttl/4 + LOAD_STALL_S)` = 21 s; asserts `ticks >= 5`, `len(samples) >= 5`, at least 3
  distinct `heartbeat_at`, never stale, one beat thread. Both heartbeat tests share
  `_run_sampled`. The companion deviates from the Design's example numbers: `lease_ttl_s = 8`
  (interval 2 s) and the first `Lease.heartbeat` call delayed by ttl/2 = 4 s, so the lease is at
  worst 6 s old, 2 s (`LOAD_STALL_S`) short of stale (the Design's ttl 4 left a 1 s margin, thinner than
  the stall budget it is meant to survive). It asserts at least 3 beat calls (the stalled one plus
  later ones, so a beat thread dying after the stall fails), never stale and one thread.
- **Job lease:** `ATTACH_LEASE_S = 6` (every 2 s), 7 s gate, `LOAD_SLACK_S = 1.0`; gap bounds as
  designed. The companion `_StalledClock(_Clock)` makes the second follow `sleep` advance 3 s
  and asserts the next `_renew` happens at that tick's fake-clock end, inside the 6 s term.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; affected tests pass; the manual loaded-box loop performed and its
      result noted in the spec.
- [x] No regressions (affected tests pass at the queue close gate).
- [x] Spec body updated to match what shipped (the final constants, and the diagnosed
      interleaving for `[drop-ssh]`).
- [x] `docs/LEDGER.md` regenerated (`python3 tools/spec_lint.py --write-ledger`).

## Open questions

None. Margins are decided above and are tuned from loaded-box evidence if a later flake shows
them too small.
