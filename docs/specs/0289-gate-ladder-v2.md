---
id: SPEC-0289
title: "Gate ladder v2: per-gate retry policy and targeted reruns of failing pytest ids"
status: accepted
owner: user
created: 2026-10-30
milestone: "M9: agentic workflow"
kind: feature
source_idea: IDEA-027
parent: SPEC-0169
depends_on: [SPEC-0284]
tags: [agentic, gates, triage]
---

## Context

Promoted from idea `IDEA-027` (gate ladder v2, epic SPEC-0169). Part 1 of 2; the
cross-campaign flaky-test record is [SPEC-0293](./0293-gate-ladder-v2-cross-campaign-flaky-test-record.md),
which builds on the data this spec produces.

Today the gate ladder has one retry rung, and it is owned by the triager:

- `src/wt/agentic/gates.py:run_all` runs each gate once through `run_gate`. `ok` is "every
  `required` gate ok". There is no per-gate retry.
- `src/wt/agentic/queue.py:decide_triage` honors a triager `retry_gate` disposition at most
  `GATE_RETRY_MAX` (1) times per loop, and only when the triager says `suspected_flaky`.
  That retry re-runs **every** gate and costs a full triager run first.
- SPEC-0284 (`gate_results.parse`) gives a pytest gate a `summary` with
  `{passed, failed, errors, skipped, failing}`, where `failing` holds up to 50 node ids. Nothing
  uses the ids yet.
- `src/wt/agentic/project.py` accepts the gate keys `name, cmd, cwd, timeout_s, required,
  results`. `required: false` already makes a failure non-blocking.

A flaky pytest failure therefore costs a triager run plus a full re-gate (this repo's pytest
gate takes minutes), and the triager has to guess whether the failure is flaky.

## Goals / Non-goals

**Goals**
- Two optional per-gate keys in `.wt/project.yaml`: `retries` (0–3, default 0) and `on_fail`
  (`block` | `warn`). `on_fail: warn` is the spelled-out form of `required: false`.
- A failing gate with `retries > 0` is re-run inside the same gate step, before triage.
- For a gate with `results: {format: pytest}` and parsed failing ids, the first retry runs
  only those ids. If they fail again, the failure is reproduced and no further retry runs. If
  they pass, the ids are recorded as `flaky`, and the gate finishes green (or with one full
  re-run when pytest stopped early).
- Every attempt is recorded on the gate result, so the triager, `wt queue watch` and
  SPEC-0293 can see it.

**Non-goals**
- The cross-campaign flaky record and showing it to the triager (SPEC-0293).
- Changing the triager's `retry_gate` rung or `GATE_RETRY_MAX`. It stays as the last rung.
- An `escalate` rung per gate (`on_fail: escalate`). A failing blocking gate already reaches
  triage, and triage can escalate.
- Targeted reruns for JUnit or other formats.

## Decision

Retries live in `gates.run_all`. That is the one place every gate run goes through: the loop's
GATING step, `wt queue gates`, and the re-gate in close. The ladder for one gate is:

1. Run the gate (attempt 0).
2. If it failed, was not interrupted by a stop, and `retries > 0`:
   - **Targeted** (pytest results with 1–50 failing ids, and `failed + errors ==
     len(failing)`, so the list is complete): run the targeted command. If it fails, stop:
     the failure is real. If it passes, mark those ids `flaky`. Then, if the log of attempt 0
     shows pytest stopped early (`stopping after N failures`), and a retry is left, run the
     full command once and take its result. Otherwise the gate is ok.
   - **Full** (any other gate): re-run the full command, up to `retries` times, and stop at
     the first pass.
3. The gate's final `ok` is the outcome of its last attempt (a passing targeted attempt counts
   as ok only under the early-stop rule above).

The targeted command is `retry_cmd` with `{failing}` replaced by the shell-quoted,
space-joined ids when the gate sets `retry_cmd`. Otherwise it is the gate's expanded `cmd`,
followed by a space and the quoted ids. The explicit key covers wrappers (`make test`) where
appending ids does not work.

## Design

- `src/wt/agentic/project.py`, in the gate validation loop (the `_check_mapping` call with the
  gate keys): accept `retries`, `on_fail` and `retry_cmd`.
  - `retries` must be an int from 0 to 3.
  - `on_fail` must be `block` or `warn` (use `_check_enum`).
  - `retry_cmd` must be a non-empty string that contains `{failing}`. It is allowed only when
    `results.format` is `pytest`.
  - `on_fail: warn` together with `required: true` is a problem: "contradicts `required`".
    So is `on_fail: block` together with `required: false`.
  - `GATE_DEFAULTS` gains `retries: 0`. `on_fail: warn` is normalised to `required: False`
    where the defaults are applied, so every reader of `required` stays unchanged.
- `src/wt/agentic/gates.py`:
  - `GateResult` gains `attempts: tuple = ()` and `flaky: tuple = ()`. Each attempt is a dict:
    `{"attempt": n, "kind": "targeted"|"full", "ok", "exit_code", "timed_out",
    "duration_s", "log", "failing"}`.
  - New `run_gate_with_retries(gate, worktree, env, log_path, ph, *, log_rel, stop)`:
    - It calls `run_gate` for attempt 0, then applies the ladder from the Decision.
    - A retry's log goes to `gates/NNN-<name>.r<n>.log`, with its own `wt.gate.v1` sidecar
      (from `_write_sidecar`) carrying `"attempt": n`.
    - Retries are skipped when `stop` is set, or when the gate was interrupted.
    - The function returns attempt 0's `GateResult` with `ok`, `exit_code`, `tail` and
      `summary` replaced by the last attempt's values, plus `attempts` and `flaky`.
  - New `targeted_cmd(gate, cmd, failing)`: builds the command with `shlex.quote` for each id.
  - New `stopped_early(log_path)`: a regex for pytest's `stopping after \d+ failures?` line.
  - `run_all` calls `run_gate_with_retries` instead of `run_gate`.
  - `as_payload` drops `attempts` and `flaky` when they are empty, the same way it drops a
    None `summary`, so old readers and old ledgers are unaffected.
  - `_write_sidecar` adds `attempts` and `flaky` to attempt 0's sidecar when they are
    non-empty.
- `src/wt/agentic/personas.py:render_gate_results`: when a result has attempts, add a line:
  `retries: targeted rerun of 2 ids reproduced the failure`, or `… passed (flaky: a::b)`,
  or `full rerun 1/2 failed`. This line goes after the `results:` line from SPEC-0284.
- `src/wt/agentic/cli_queue.py:_gate_text`: append ` ↻N` (the number of retries) to a gate
  that has attempts, and ` (flaky)` when `flaky` is non-empty and the gate is ok.
- `src/wt/agentic/schemas.py`: no new required keys (all of this is optional). Update the
  `wt.gate.v1` doc comment.
- `.wt/project.yaml` (this repo): the `pytest` gate gets `retries: 2`. Its `-x` makes the
  early-stop rule apply, so a flaky first failure costs one targeted rerun (seconds) plus one
  full rerun, instead of a triager run plus a full re-gate. A real failure costs only the
  targeted rerun before triage.

## Alternatives considered

- **Retry in the queue (a new GATING sub-state per retry).** Rejected. It adds states and
  ledger transitions for something that is one gate's internal business. It would also skip
  `wt queue gates` and the close re-gate.
- **Treat a passing targeted rerun as green even after `-x`.** Rejected. The tests after the
  first failure never ran, so green would be a lie.
- **Always append ids to `cmd`, with no `retry_cmd`.** Rejected. It breaks wrapped commands.
  SPEC-0284 already chose explicit opt-in over sniffing.
- **An `on_fail: escalate` rung.** Deferred. Triage already escalates, and no gate has needed
  it yet.

## Acceptance criteria

- [ ] `.wt/project.yaml` validation accepts `retries` (int 0–3), `on_fail` (`block`|`warn`)
      and `retry_cmd` (string containing `{failing}`, only with `results.format: pytest`).
      It reports out-of-range values, unknown enums, `retry_cmd` without `{failing}` or
      without pytest results, and an `on_fail` that contradicts `required`.
- [ ] `on_fail: warn` behaves exactly like `required: false`: the gate's failure leaves
      `GateRun.ok` true and the campaign goes to REVIEWING, not TRIAGING.
- [ ] A failing gate with `retries: N` and no usable failing ids is re-run in full up to N
      times. It stops at the first pass, and its final `ok` and `exit_code` come from the last
      attempt. No retry runs after a stop or for an interrupted gate.
- [ ] A pytest-results gate with a complete failing-id list first re-runs only those ids
      (`retry_cmd` or `cmd` + quoted ids). A reproduced failure ends the ladder. A pass
      records the ids in `flaky` and is ok, unless the log shows pytest stopped early, in which
      case one full re-run decides (if a retry is left; otherwise the gate stays failed).
- [ ] Every attempt has its own `NNN-<name>.r<n>.log` and sidecar. The gate result, attempt 0's
      sidecar and the `gate` ledger event carry `attempts` and `flaky`, and pass `schemas`
      validation. The triage prompt and the `wt queue watch` gate cell show the retries.
- [ ] This repo's `pytest` gate sets `retries: 2`.

## Test plan

- `tests/test_agentic_project.py`: `test_gate_retry_keys_valid`, `test_gate_retries_out_of_range`,
  `test_gate_on_fail_bad_enum`, `test_retry_cmd_needs_failing_placeholder`,
  `test_retry_cmd_needs_pytest_results` and `test_on_fail_contradicts_required`. Also
  `test_on_fail_warn_normalises_required` (the loaded config has `required: False`).
- `tests/test_agentic_gates.py`. The fake gates are shell scripts that read a counter file in
  `tmp_path`, so each attempt can behave differently.
  - `test_full_retry_passes_second_time`: fails, then passes. `ok`, two attempts, `.r1.log`
    exists.
  - `test_full_retry_exhausted`: `retries: 2`, always fails. Three attempts, `ok` False, and
    `exit_code` from the last attempt.
  - `test_targeted_rerun_reproduces`: the first run prints a pytest summary with
    `FAILED tests/a.py::t1`. The targeted command echoes its args and fails. It has one
    attempt of kind `targeted`, no further attempt even with `retries: 3`, and its argv
    contains `tests/a.py::t1`.
  - `test_targeted_rerun_flaky_ok`: the targeted run passes and no `stopping after` line
    appears. `ok` is True and `flaky == ("tests/a.py::t1",)`.
  - `test_targeted_then_full_after_early_stop`: the log has `stopping after 1 failures`. The
    targeted run passes, then the full run passes. That gives two attempts (`targeted`,
    `full`) and `ok`. With `retries: 1` instead, the gate stays failed and `flaky` is still
    recorded.
  - `test_retry_cmd_template_used`: `{failing}` is replaced with the quoted ids. An id
    containing a space is quoted.
  - `test_incomplete_failing_list_falls_back_to_full`: `failed: 60` with 50 ids gives a full
    retry.
  - `test_no_retry_when_stopping`: `gates._stopping` is set after attempt 0, so there are no
    attempts.
  - `test_payload_omits_empty_attempts`: a gate without retries has no `attempts` or `flaky`
    key in `as_payload` or in its sidecar.
  - `test_retry_sidecars_validate`: every `.r<n>.json` passes `schemas` and has `attempt`.
- `tests/test_agentic_queue.py::test_on_fail_warn_goes_to_reviewing`: a fake failing
  `on_fail: warn` gate. The campaign moves GATING → REVIEWING.
- `tests/test_agentic_personas.py::test_render_gate_results_retries_line` and
  `tests/test_agentic_dashboard.py::test_gate_text_retry_marker` (`↻1`, `(flaky)`).
- `tests/test_agentic_project.py::test_repo_pytest_gate_retries`: this repo's `.wt/project.yaml`
  has `retries: 2` on `pytest`.
- **Sabotage check:**
  - Make the ladder treat a passing targeted rerun as ok even when the log shows an early
    stop. `test_targeted_then_full_after_early_stop` must fail.
  - Drop the "reproduced ⇒ stop" branch. `test_targeted_rerun_reproduces` must fail, because
    a second attempt appears.
- **Regression guard:** full `uv run pytest`. Gates without the new keys produce exactly the
  old payload (`test_payload_omits_empty_attempts`).

## Rollout / migration

Additive and opt-in per gate. Old ledgers and sidecars have no `attempts` key, and readers
treat a missing key as "no retries".

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

(none)
