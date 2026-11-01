---
id: SPEC-0358
title: "Failed-first loop rounds: rerun the previously failing tests before the rest of a gate"
status: accepted
owner: user
created: 2026-11-01
kind: feature
source_idea: IDEA-649
tags: [agentic, gates, tests]
depends_on: ["SPEC-0357"]
supersedes: ["SPEC-0355"]
---

## Context

[SPEC-0328](./0328-tiered-gates-affected-test-selection.md) split gates into a loop tier
(`pytest-affected`: `tools/affected_tests.py` picks the tests a branch affects) and a close tier
(the full `pytest` suite once, on the rebased tree). Code campaigns still spend most of their wall
time in gates. This spec is part 2 of 3 of the split of SPEC-0355 (which it supersedes together with
the other two parts). It covers the cost of a *red loop round*.

The 2026-11-01 ledgers and gate logs show:

- **Failures**: a red loop round is followed by a fix and then a whole new round. The round
  re-runs the full selection, or the full suite on a fallback (11-24 min), before it gets back to
  the test that failed. The [SPEC-0289](./0289-gate-ladder-v2.md) ladder only retries *within* one
  round, so nothing carries the knowledge "these ids failed last round" into the next one.
- **SPEC-0338**: 8001 s in gates, most of it full-suite fallback rounds. The `pytest-affected`
  command passes `-x`, so a round whose first failing test is still broken still pays for every
  test that runs before it.
- **Slot waits** of 317-671 s add to all of this. Shorter gate runs make these waits shorter too.

What exists today: the ledger already holds the failing ids of every red pytest gate
([SPEC-0284](./0284-gate-result-parsing-read-structured-results-not.md) `summary.failing`), `last_gate` in the campaign state keeps the
last run's per-gate results, and `gates.targeted_cmd` / `gates._targeted_ids` already build the
command that runs exactly a failing-id list through the gate's `retry_cmd`
(`src/nightwright/agentic/gates.py`). Only the first retry *inside* a round uses them.

## Goals / Non-goals

**Goals**
- G4. After a red loop round, the next round runs the previously failing tests first. If they
  still fail, the round ends without running the rest. If they pass, or cannot be run, the round
  continues exactly as today.

**Non-goals**
- Part 1 of the split of SPEC-0355, *Narrow affected_tests.py fallbacks and write the gate report*:
  G1 plus the `NW_GATE_REPORT` JSON. It shrinks `FULL_TRIGGERS`, adds the reference index and
  needles, the name rule, `--strict` / `NW_AFFECTED_STRICT` as the rollback, the always-included
  smoke set, the report file, the replay fixture test and a JSON CLI mode exposing the selection
  and forward closure for later reuse. It affects the loop tier only; the close tier still runs the
  full suite. It is SPEC-0357, and this spec depends on it only for the shorter rounds it brings.
- Part 3 of the split: *close-tier reuse of a green loop gate with a full-suite sentinel* (G2+G3).
  That covers `head`/`tree`/`report` on gate payloads, `last_green_loop`, `reuse_from` validation,
  the `close` / `sentinel` project sections, rules R1-R7 in `close.py` (including declining when the
  sentinel is overdue), the `gate.reused` / `gate.reuse_declined` events, `gate_reuse.json`, the
  sentinel runner step on a detached base worktree, `nw queue sentinel run|clear`, escalation, the
  dogfood `reuse_from` and its docs. If that is still too large, the sentinel ships first and reuse
  after it.
- Coverage-based or predictive selection (SPEC-0328 phase 3), batched close trains or bisecting
  (SPEC-0328 phase 2).
- Changing the SPEC-0289 retry ladder itself, or the close tier (`failed_first` is a loop-tier
  mechanism; the close tier never gets a previous result).

## Decision

A gate with `failed_first: true` begins a round by running the failing ids recorded in the
campaign's previous red result *for that gate name*, through the gate's `retry_cmd`. This gives
pytest `--lf` semantics, driven by the ledger rather than by `.pytest_cache`. If that prelude
reproduces the failure, the round is red and stops there: no main run and no SPEC-0289 ladder, since
the failure is already reproduced. If the prelude passes, or is inconclusive, the normal selected run
follows unchanged. The feature is opt-in per gate (default off) and is turned on for the dogfood
`pytest-affected` gate.

## Design

### Config and validation (`src/nightwright/agentic/project.py`)

A gate may set `failed_first: <bool>`. `failed_first` joins the known gate keys in `validate` (the
project schema is strict: unknown keys fail). Validation follows `_check_gate_retry`:
- it must be a boolean;
- it is only valid with `results: {format: pytest}`, because the ids come from the pytest summary
  (the same rule as `retry_cmd`). `failed_first: true` on a gate without it is a validation
  problem naming `gates[i].failed_first`;
- it is only valid on a `when: loop` gate. A close gate gets no previous result (see below), so the
  key would be a silent no-op. Gates with `when: always` are rejected for the same reason.

`failed_first` does not require `retry_cmd`: like the SPEC-0289 targeted rerun, `targeted_cmd` falls
back to the expanded `cmd` followed by the ids, and `_targeted_ids` refuses that fallback when the
command has shell operators.

### Plumbing in `run_all` and `run_gate_with_retries` (`src/nightwright/agentic/gates.py`)

`run_all` (loop tier only, `tier == "loop"`) looks up, for each gate with `failed_first`, the
same-name entry in `campaign_state["last_gate"]["results"]` and passes it as `previous=` to
`run_gate_with_retries`. Every other tier and every gate without the flag passes `previous=None`,
so their behavior is byte-for-byte as before. The previous entry qualifies only when it is red
(`ok` false), neither `interrupted` nor `timed_out`, and carries a pytest `summary`.

The failing ids come from `_targeted_ids`, refactored to take the previous entry's `cmd` and
`summary` (the existing `GateResult` call site passes its own). The same completeness rule holds:
1 to `gate_results.MAX_FAILING` ids, and `failed + errors == len(failing)`. An incomplete list means
no prelude. A pass of a partial list could not stand for anything, and the prelude can only end a
round red, so an incomplete list is simply skipped.

The prelude is attempt kind `failed-first`. It runs `run_gate` with `cmd_override =
targeted_cmd(gate, previous["cmd"], ids, placeholders)` and its own log
`gates/<seq:03>-<name>.ff.log` (so it never overwrites the main run's log or the `.r<n>.log`
retry logs). Its attempt record uses `_attempt` with `attempt: -1` and `kind: "failed-first"`;
the main run keeps `attempt: 0`, so the SPEC-0289 numbering and log names stay unchanged. It runs
before the main run, only when no stop is requested.

Outcome of the prelude:
- **Red and not a code in `TARGETED_UNRESOLVED_EXITS`** (`4`, `5`: usage error or no tests, so the
  ids never ran): the failure is reproduced. The gate result is the prelude's: `ok` false, its
  `exit_code`, `tail`, `summary` (the still-failing ids), `log`, `cmd`, with `attempts =
  (prelude,)` and `duration_s` the prelude's. There is no main run and no ladder. The gate is
  red for the round's verdict and triage exactly like any other red gate.
- **Interrupted** (a stop during the prelude): the gate result is the prelude's with `interrupted`
  true. No main run starts (SPEC-0229: a cut is not a failure).
- **Green, timed out, or an exit code in `TARGETED_UNRESOLVED_EXITS`** (the ids are gone, renamed,
  or the command did not accept them): inconclusive. The normal run and the SPEC-0289 ladder follow
  unchanged. A timed-out prelude gives no information about the rest and the main run has its own
  timeout, so it falls through too.

On a fall-through the final result is the main run's (with the ladder's verdict, if one ran) and
`attempts` lists the prelude first, then the main run and any ladder attempts, even when no ladder
retry started. `duration_s` is the sum of every attempt's duration, the prelude included. The
sidecar JSON of the main run is rewritten with the final values like in the ladder (the existing
`_rewrite_sidecar`), including the prelude in `attempts`. The `deadline` budget check for ladder
full reruns counts the main run's own duration only, as today.

A pytest gate with `-x` and a fallback selection used to spend up to 24 min on a round whose first
failing test was still broken. Now that round ends in the prelude's seconds.

### Accounting

- `GateResult.attempts` and the `gate` ledger event's `attempts` already carry per-attempt
  `kind`, `ok`, `exit_code`, `duration_s`, `log` and `failing`; the new kind `failed-first` is added
  to the documented kinds. No new ledger event keys are needed, so `schemas.EVENT_KEYS` is unchanged.
- A reproduced prelude counts as a red gate for every existing consumer (counters, review/triage
  rendering via `personas.render_gate_results`, queue failure handling), since they read the same
  `ok`/`summary` fields.
- `flaky` is only set by the ladder, so a green prelude never marks the ids flaky.

### Dogfood config (`.nw/project.yaml`)

`pytest-affected` gets `failed_first: true`. Its command, `retry_cmd: uv run pytest -q -n 0
{failing}`, `results: {format: pytest}` and `timeout_s` stay as they are. No other gate changes in
this spec.

### Documentation

`docs/product/capabilities/agentic.md` documents `failed_first` (what it does, loop tier only, the
three outcomes of the prelude, the `failed-first` attempt kind and the `.ff.log` log).

## Alternatives considered

- **pytest `--lf` with `.pytest_cache`.** The cache lives in the worktree and is not reliable
  under xdist or after `git clean`. The ledger already holds the failing ids (SPEC-0284).
- **Make the main run order the failing tests first** (`-p` plugin or `--ff`). The ordering
  needs a plugin or the cache again, and a `-x` run would still collect the whole selection and
  pay the fallback's collection cost; a separate short run is simpler and independent of how the
  gate command is composed.
- **Always run the prelude, even for a green previous round.** There is nothing to run then. The
  prelude is skipped without a prior red.
- **Skip the SPEC-0289 ladder after a green prelude.** A green prelude proves only the previously
  failing ids; the main run and its ladder stay authoritative.

## Acceptance criteria

- [ ] `failed_first` is a validated gate key: a boolean, only with `results: {format: pytest}`
      and only on a `when: loop` gate; other cases fail `validate` naming `gates[i].failed_first`.
- [ ] `run_all(..., tier="loop")` passes the previous same-name result from `last_gate` to
      `run_gate_with_retries`. The close tier and gates without the flag pass none and run exactly
      as before.
- [ ] With `failed_first: true`, a round after a red round (previous entry red, not interrupted or
      timed out, with a complete failing-id list) runs the previous failing ids first, through
      `targeted_cmd` (the gate's `retry_cmd`), in a log `<seq>-<name>.ff.log`.
- [ ] A reproduced failure (prelude red, exit code not in `TARGETED_UNRESOLVED_EXITS`) ends the
      round red without the main run or the ladder: one attempt, of kind `failed-first`, and the
      result's `summary` names the still-failing ids.
- [ ] A passing prelude, a timed-out prelude and a prelude exiting 4 or 5 fall through to the
      normal run and ladder; the prelude is listed first in `attempts` and its duration is in
      `duration_s`. An interrupted prelude yields an interrupted result and no main run.
- [ ] No prelude runs without the flag, without a prior red result for that gate name, after a
      green or interrupted previous result, or with an incomplete id list.
- [ ] The dogfood `.nw/project.yaml` sets `failed_first: true` on `pytest-affected`, and
      `docs/product/capabilities/agentic.md` documents `failed_first`.

## Test plan

- **Automated: gates** (`tests/test_agentic_gates.py`, using the fake gate commands of the
  SPEC-0289 ladder tests):
  - a reproduced prelude gives a red result with exactly one attempt of kind `failed-first`, no
    main run (the main command leaves a marker file that must be absent), `duration_s` equal to the
    prelude's, and the `.ff.log` file present;
  - a green prelude runs the main command; the result's `attempts[0]` is the prelude, the durations
    are summed, and a red main run still goes through the ladder;
  - an exit code 4 and an exit code 5 prelude, a timed-out prelude fall through to the main run;
  - an interrupted prelude (stop event set) gives an interrupted result with no main run;
  - no prelude without the flag, without a prior red, with a previous result that is green,
    interrupted or timed out, with a different gate name, and with an incomplete id list
    (`failed + errors != len(failing)`, or more than `MAX_FAILING`);
  - `run_all(tier="loop")` hands the previous result to the gate (a `last_gate` in the fixture
    state) and `tier="close"` / `tier=None` does not.
- **Automated: project config** (`tests/test_agentic_project.py`): `failed_first` accepted on a
  loop pytest gate; rejected when not a boolean, without `results: {format: pytest}`, and on a
  close or `always` gate.
- **Automated: dogfood config** (`tests/test_agentic_dogfood_config.py`): `.nw/project.yaml`
  validates and `pytest-affected` has `failed_first: true`.
- **Manual verification:** in a scratch worktree with the dogfood config, make one test fail, run
  `nw gate run` (loop) to get a red round, then run it again with the test still broken and
  confirm the second round ends after the prelude (one `failed-first` attempt, seconds of wall
  time). Fix the test and confirm the following round runs the prelude green and then the normal
  selection. Record the wall times in *As shipped*.
- **Regression guard:** `python3 tools/affected_tests.py --run uv run pytest -q` plus
  `test_agentic_gate*`, `test_agentic_queue*`, `test_agentic_project`,
  `test_agentic_dogfood_config`; `python3 tools/spec_lint.py` exits 0; the full suite runs at the
  queue's close gate.

## Clock Log

CLOCK-IN: n/a
CLOCK-OUT: n/a

## Rollout / migration

1. Ship the validation and the gates plumbing with `failed_first` default **off**: absent means no
   prelude, so nothing changes for any project until it sets the key.
2. In the same change, turn on `failed_first` in the dogfood `.nw/project.yaml`. Campaigns already
   in flight pick it up at their next GATING, because main's config is read. Their first round has
   no prior result of the new form and simply runs the normal selection.
3. Watch the share of red rounds that end at the prelude and the wall time saved, and record the
   result in *As shipped*. Rollback is removing the key from the config.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; affected tests pass; manual steps performed and recorded.
- [ ] No regressions (queue close gate full suite).
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated.

## Open questions

None.
