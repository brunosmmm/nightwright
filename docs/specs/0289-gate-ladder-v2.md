---
id: SPEC-0289
title: "Gate ladder v2: per-gate retry policy and targeted reruns of failing pytest ids"
status: done
owner: user
created: 2026-10-30
updated: 2026-10-30
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
- For a gate with `results: {format: pytest}` and a complete list of failing ids, the first
  retry runs only those ids. If they fail again, the failure is reproduced and no further
  retry runs. If they pass, the remaining retries are full reruns, because a targeted pass
  alone never proves the suite is green.
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

1. Run the gate (attempt 0, kind `full`).
2. Retries are skipped when attempt 0 passed, was interrupted, or timed out (`timed_out`), or
   when `retries == 0`. A timed-out gate is **not** retried: a rerun would burn up to
   `timeout_s` again (1200 s for pytest) and a timeout is not a flake signal, so it goes to
   triage. Otherwise at most `retries` more attempts run, each checked against the stop
   conditions below before it starts.
3. **Targeted rung** (pytest results with 1–50 failing ids, and `failed + errors ==
   len(failing)`, so the list is complete). The first retry is the targeted command.
   - It fails: the failure is reproduced. The ladder ends and the gate stays failed.
   - It passes: this proves nothing about the rest of the suite. Why: the failure may depend on
     test order or leaked state, pytest may have stopped early (`-x`, or a collection error
     that aborts the session), and the targeted ids ran alone. So the ladder continues with the
     full rung.
4. **Full rung** (any other gate, or after a targeted pass): re-run the full command, one
   attempt per remaining retry, and stop at the first pass. If no retry is left after a
   targeted pass (`retries: 1`), the gate **stays failed**. The targeted pass is recorded in
   `attempts` as evidence, not as a verdict.
5. Any retry attempt that times out or is interrupted ends the ladder.

There is no early-stop special case and no log sniffing: because only a full pass can turn a
failed gate green, `-x`, collection errors (`Interrupted: N errors during collection`) and
order-dependent failures are all handled by the same rule.

`flaky` holds the ids of a passing targeted attempt **only when the gate's final `ok` is
true**, that is, a later full rerun confirmed it. When the targeted rerun passes but a full
rerun then fails, and the gate ends failed, `flaky` stays empty: the failure may be real and
order-dependent, and SPEC-0293 must not count it as flake evidence. The per-attempt data
(targeted `ok: true`, full `ok: false`) is still in `attempts`, and the triage line says so.

The targeted command is `retry_cmd` with `{failing}` replaced by the shell-quoted,
space-joined ids when the gate sets `retry_cmd`. Otherwise it is the gate's expanded `cmd`,
followed by a space and the quoted ids. The explicit key covers wrappers (`make test`) where
appending ids does not work.

**The result of the ladder.** The returned `GateResult` is attempt 0's, with these fields set
explicitly (all others, `name`, `cmd` and `log`, stay attempt 0's):

| field | value |
|---|---|
| `ok`, `exit_code`, `tail`, `summary` | from the *verdict attempt*: the last attempt of kind `full` (attempt 0 counts). A targeted attempt is never the verdict, so a targeted pass cannot make `ok` true, and the gate's `summary` keeps full-suite counts. |
| `interrupted`, `timed_out` | from the last attempt of any kind, so a stop that lands during a retry makes the gate interrupted, and `timed_out: true` never sits next to `ok: true`. |
| `duration_s` | the total over all attempts. Per-attempt durations are in `attempts`. |
| `attempts`, `flaky` | as defined above. |

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
    "interrupted", "duration_s", "log", "failing"}`. Attempt 0 is included once any retry ran.
  - New `run_gate_with_retries(gate, worktree, env, log_path, ph, *, log_rel, stop)`:
    - It calls `run_gate` for attempt 0, then applies the ladder from the Decision and builds
      the final result with the field table from the Decision.
    - A retry's log goes to `gates/NNN-<name>.r<n>.log`, with its own `wt.gate.v1` sidecar
      (from `_write_sidecar`) carrying `"attempt": n`. Each retry's sidecar holds that
      attempt's own `ok`, `exit_code` and `summary`.
    - Before each retry it checks `stop.is_set()` (the `threading.Event` that `run_all`
      passes) **or** the module flag `gates._stopping`; if either is true, the ladder ends.
      The flag and the event are equivalent for the ladder, so a test may use either.
      A retry that `run_gate` marks interrupted is recorded as an attempt with
      `interrupted: true` and ends the ladder.
    - When at least one retry ran, attempt 0's sidecar is **rewritten** after the ladder
      finishes (attempt 0's own sidecar was written by `run_gate` before any retry). The
      rewritten sidecar is the gate's final result, the same values as the `gate` ledger
      event: final `ok`, `exit_code`, `timed_out`, `interrupted`, `duration_s`, plus `attempts`
      and `flaky`. Attempt 0's raw values remain in `attempts[0]`. A gate with no retry is not
      rewritten, so its sidecar is byte-identical to today's.
  - New `targeted_cmd(gate, cmd, failing)`: builds the command with `shlex.quote` for each id.
  - `run_all` calls `run_gate_with_retries` instead of `run_gate`, so `GateRun.interrupted`
    (SPEC-0229) is true when the stop landed in a retry, and the queue treats the run as cut
    by a stop, not as a failure to triage.
  - `as_payload` drops `attempts` and `flaky` when they are empty, the same way it drops a
    None `summary`, so old readers and old ledgers are unaffected.
  - `_write_sidecar` adds `attempts` and `flaky` to a sidecar when they are non-empty.
- `src/wt/agentic/personas.py:render_gate_results`: when a result has attempts, add a line:
  `retries: targeted rerun of 2 ids reproduced the failure`, or `targeted rerun of 2 ids passed,
  full rerun 2/2 passed (flaky: a::b)`, or `targeted rerun passed but full rerun 2/2 failed
  (order-dependent or other failure)`, or `full rerun 1/2 failed`. This line goes after the
  `results:` line from SPEC-0284.
- `src/wt/agentic/cli_queue.py:_gate_text`: append ` ↻N` (the number of retries) to a gate
  that has attempts, and ` (flaky)` when `flaky` is non-empty and the gate is ok.
- `src/wt/agentic/schemas.py`: no new required keys (all of this is optional). Update the
  `wt.gate.v1` doc comment, including the optional `interrupted` key of an attempt.
- `.wt/project.yaml` (this repo): the `pytest` gate gets `retries: 2`. That is one targeted
  rerun (seconds) plus one full rerun. A real failure costs only the targeted rerun before
  triage. A flaky first failure costs the targeted rerun plus one full rerun, instead of a
  triager run plus a full re-gate. An order-dependent failure passes the targeted rerun, fails
  the full rerun, and goes to triage with the gate still failed.

## Alternatives considered

- **Retry in the queue (a new GATING sub-state per retry).** Rejected. It adds states and
  ledger transitions for something that is one gate's internal business. It would also skip
  `wt queue gates` and the close re-gate.
- **Treat a passing targeted rerun as green, or as green unless the log shows an early stop.**
  Rejected. The rest of the suite never ran in the targeted rerun, so green would be a lie
  for early stops (`-x`), collection errors, and order-dependent failures. Detecting only
  `stopping after N failures` misses the collection-error abort, so the ladder does not
  sniff logs at all: a full pass is always required.
- **Accept the order-dependent risk and record it.** Rejected. It turns a deterministic failure
  green, including at the close re-gate before a merge. The cost of the extra full rerun is
  the price of a trustworthy green.
- **Retry timed-out gates.** Rejected. It multiplies a 1200 s timeout by up to four, and
  timeouts are not flakes.
- **Always append ids to `cmd`, with no `retry_cmd`.** Rejected. It breaks wrapped commands.
  SPEC-0284 already chose explicit opt-in over sniffing.
- **An `on_fail: escalate` rung.** Deferred. Triage already escalates, and no gate has needed
  it yet.

## Acceptance criteria

- [x] `.wt/project.yaml` validation accepts `retries` (int 0–3), `on_fail` (`block`|`warn`)
      and `retry_cmd` (string containing `{failing}`, only with `results.format: pytest`).
      It reports out-of-range values, unknown enums, `retry_cmd` without `{failing}` or
      without pytest results, and an `on_fail` that contradicts `required`.
- [x] `on_fail: warn` behaves exactly like `required: false`: the gate's failure leaves
      `GateRun.ok` true and the campaign goes to REVIEWING, not TRIAGING.
- [x] A failing gate with `retries: N` and no usable failing ids is re-run in full up to N
      times. It stops at the first pass, and its final `ok`, `exit_code`, `tail` and `summary`
      come from the last full attempt. No retry runs after a stop (`stop` event or
      `gates._stopping`), for an interrupted gate, or for a gate whose attempt 0 timed out.
- [x] A pytest-results gate with a complete failing-id list first re-runs only those ids
      (`retry_cmd` or `cmd` + quoted ids). A reproduced failure ends the ladder. A passing
      targeted rerun never makes the gate ok on its own: the remaining retries are full
      reruns and the first full pass decides. With no retry left after the targeted pass, the
      gate stays failed. This holds with or without `-x` and after a collection error.
- [x] `flaky` lists the targeted ids only when the gate ends ok. It is empty when the gate
      ends failed, including a targeted pass followed by failing full reruns.
- [x] The returned result takes `interrupted` and `timed_out` from the last attempt (never
      `timed_out: true` with `ok: true`), `duration_s` as the sum over all attempts, and
      `summary` from the last full attempt, not from a targeted one. A stop that lands during a
      retry gives `interrupted: true` and `GateRun.interrupted` true. An attempt that times out
      ends the ladder.
- [x] Every attempt has its own `NNN-<name>.r<n>.log` and sidecar. When retries ran, attempt
      0's sidecar is rewritten with the final result and carries `attempts` and `flaky`, as
      does the `gate` ledger event, and all pass `schemas` validation. A gate without retries
      has an unchanged payload and sidecar. The triage prompt and the `wt queue watch` gate
      cell show the retries.
- [x] This repo's `pytest` gate sets `retries: 2`.

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
  - `test_targeted_rerun_flaky_ok`: `retries: 2`, no `stopping after` line. The targeted run
    passes, then the full run passes. Two attempts (`targeted`, `full`), `ok` True and
    `flaky == ("tests/a.py::t1",)`.
  - `test_targeted_then_full_after_early_stop`: the log has `stopping after 1 failures`. The
    targeted run passes, then the full run passes: two attempts and `ok`. With `retries: 1`
    instead, the gate stays failed, `flaky == ()`, `exit_code` is attempt 0's, and the only
    retry attempt is the passing `targeted` one.
  - `test_targeted_then_full_fails`: `retries: 2`, the targeted run passes and the full run
    fails (an order-dependent failure). `ok` False, `flaky == ()`, two attempts, with the
    targeted one `ok: true`. With `retries: 3` and a third run that passes, `ok` is True and
    `flaky == ("tests/a.py::t1",)`.
  - `test_targeted_after_collection_error_runs_full`: attempt 0's log ends with
    `!!! Interrupted: 1 error during collection !!!` and its summary is `errors: 1`,
    `failing: ["tests/x.py"]`. The targeted run passes, but a full run still follows, and the
    gate is ok only if that full run passes. With the full run failing, the gate stays failed.
  - `test_retry_cmd_template_used`: `{failing}` is replaced with the quoted ids. An id
    containing a space is quoted.
  - `test_incomplete_failing_list_falls_back_to_full`: `failed: 60` with 50 ids gives a full
    retry.
  - `test_no_retry_when_stopping`: parametrised over a set `stop` event and `gates._stopping`
    set after attempt 0. Both give no attempts.
  - `test_stop_during_retry_is_interrupted`: the stop lands while a retry runs (the retry's
    script sets the flag, or `run_gate` is patched to return an interrupted result). The
    returned result has `interrupted` True, `ok` False, no attempt after it, and
    `run_all`'s `GateRun.interrupted` is True.
  - `test_timed_out_attempt_zero_not_retried`: attempt 0 times out with `retries: 2`: no
    attempts, `timed_out` True.
  - `test_retry_timeout_ends_ladder`: attempt 0 fails, the first retry times out. No third
    attempt even with `retries: 3`, `timed_out` True and `ok` False.
  - `test_result_fields_after_ladder`: a fail-then-pass full retry gives `timed_out` False,
    `interrupted` False, `duration_s` equal to the sum of the attempts' durations, `log`
    attempt 0's, `exit_code` 0. After a targeted pass with `retries: 1`, `summary` is attempt
    0's full-suite counts, not the targeted run's `{passed: 1, failed: 0}`.
  - `test_attempt0_sidecar_rewritten`: after a ladder, attempt 0's `.json` has the final `ok`
    and `exit_code`, plus `attempts` (whose first entry holds attempt 0's raw values) and
    `flaky`, and passes `schemas`.
  - `test_payload_omits_empty_attempts`: a gate without retries has no `attempts` or `flaky`
    key in `as_payload` or in its sidecar, and the sidecar is not rewritten.
  - `test_retry_sidecars_validate`: every `.r<n>.json` passes `schemas` and has `attempt`.
- `tests/test_agentic_queue.py::test_on_fail_warn_goes_to_reviewing`: a fake failing
  `on_fail: warn` gate. The campaign moves GATING → REVIEWING.
  `tests/test_agentic_queue.py::test_stop_during_gate_retry_not_triaged`: a stop landing in a
  retry leaves the campaign out of TRIAGING (the run counts as interrupted, SPEC-0229).
- `tests/test_agentic_personas.py::test_render_gate_results_retries_line` (the reproduced,
  flaky-confirmed, targeted-pass-then-full-fail and full-rerun wordings) and
  `tests/test_agentic_dashboard.py::test_gate_text_retry_marker` (`↻1`, `(flaky)` only when ok).
- `tests/test_agentic_project.py::test_repo_pytest_gate_retries`: this repo's `.wt/project.yaml`
  has `retries: 2` on `pytest`.
- **Sabotage check:**
  - Make the ladder treat a passing targeted rerun as ok. `test_targeted_then_full_fails`,
    `test_targeted_after_collection_error_runs_full` and
    `test_targeted_then_full_after_early_stop` (the `retries: 1` case) must fail.
  - Drop the "reproduced ⇒ stop" branch. `test_targeted_rerun_reproduces` must fail, because
    a second attempt appears.
  - Take `interrupted` from attempt 0 instead of the last attempt.
    `test_stop_during_retry_is_interrupted` must fail.
- **Regression guard:** full `uv run pytest`. Gates without the new keys produce exactly the
  old payload and sidecar (`test_payload_omits_empty_attempts`).

### Test record (2026-10-30)

Ran `uv run pytest -q tests/test_agentic_gates.py tests/test_agentic_project.py
tests/test_agentic_personas.py tests/test_agentic_dashboard.py tests/test_agentic_queue.py
tests/test_agentic_dogfood_config.py`: 333 passed. All tests named in the plan exist and pass
(the project, gates, queue, personas and dashboard ones; `test_stop_during_retry_is_interrupted`
patches `run_gate` to stop on attempt 1). Sabotage checks run by hand, each failing the named
tests: treating a passing targeted rerun as the verdict (targeted-then-full, collection-error,
early-stop and result-field tests failed), taking `interrupted` from attempt 0
(`test_stop_during_retry_is_interrupted` failed), and dropping the "reproduced stops" branch
(`test_targeted_rerun_reproduces` failed). `python3 tools/spec_lint.py` passes. The full
`uv run pytest` was left to gating (it did not finish inside the implementer's time window).

### Test record, review round 1 (2026-10-30)

Added tests for the review findings: `tail`/`summary` from the last full attempt (three
cases), retry-sidecar contents, `_targeted_ids` eligibility (JUnit, `error`, `failed+errors`
vs `len(failing)`, shell operators, `retry_cmd` exempt), exit 4/5 targeted reruns not counted
as reproduced, the retry deadline, `_rewrite_sidecar` (error propagation, clearing, an
unreadable sidecar), an interrupted attempt 0 not retried, and the persona wordings (full-only
pass, timed-out/interrupted/unresolved targeted rerun). The timeout tests now patch
`run_gate` instead of sleeping. Ran `uv run pytest -q` on the gates, project, personas,
dashboard, queue, dogfood-config, close-train, loop-state, watchdog, lease and events test
files: 430 passed. `python3 tools/spec_lint.py` passes. The full suite is left to gating.

### Test record, review round 2 (2026-10-30)

Added `test_stop_after_completed_retry_is_interrupted` and
`test_stop_after_completed_retry_run_is_interrupted` (event and flag; a stop landing after a
retry completed normally gives `interrupted` on the result, the rewritten sidecar and
`GateRun`) and `test_targeted_retry_timeout_ends_ladder`. Sabotage by hand: dropping
`or stopped` from the final `replace(...)` fails the four stop-after-retry cases. `uv run pytest
-q tests/test_agentic_gates.py`: 52 passed. The full suite is left to gating.

### As shipped

Matches the Design. Small additions: `run_gate` gained keyword-only `cmd_override` and
`attempt` parameters (the targeted command and a retry sidecar's `attempt`); `targeted_cmd`
takes an optional `placeholders` argument so `retry_cmd` is expanded before the ids are
substituted. When a stop lands before any retry starts, `run_gate_with_retries` returns
attempt 0's result unchanged (no `attempts`). The persona line for a targeted pass with no full
retry left reads "targeted rerun of N ids passed, no full rerun left (the gate stays failed)".

Review-round additions (all outside the Design text, none changes an acceptance criterion):
- **Inconclusive targeted rerun.** A targeted rerun that exits 4 or 5 (pytest usage error or no
  tests collected, `gates.TARGETED_UNRESOLVED_EXITS`) never ran the ids, so it is not
  "reproduced": the ladder goes on with full reruns and `flaky` stays empty. The persona line
  reads "targeted rerun of N ids could not run (pytest exit 4), ...". A targeted rerun that
  timed out or was interrupted reads "timed out" / "was interrupted", not "reproduced".
- **No ids appended to a shell pipeline.** Without `retry_cmd`, a `cmd` containing a shell
  operator (`| & ; < > ( ) \``, `$(`, a newline) gets no targeted rung (the ids would go to
  the wrong program); the ladder runs full reruns. `retry_cmd` is not subject to this.
- **Retry time budget.** `gate_budget_s` (the runner's busy and stuck budget, default raised
  from 1800 to 3600 s) now bounds the ladder: `run_all` passes `run_gate_with_retries` a
  `deadline` (start of the run + `gate_budget_s`), and a *full* rerun is not started when
  `now + attempt 0's duration` would pass it. The targeted rung is never budgeted.
- **A stop that skips the retries marks the gate `interrupted`** (also when it lands mid-ladder
  after a completed retry), so `GateRun.interrupted` is true and the campaign is not triaged
  (SPEC-0229). The gate's sidecar is rewritten with `interrupted: true` and the stop error.
- The rewritten sidecar's `error` comes from the verdict attempt, or from a later retry that
  was cut short; the rewrite is best effort (an unreadable sidecar is left as it is) and goes
  through a temp file plus `os.replace`.
- Wording: the full-rerun phrase is "full rerun K/N passed" (K passes among N full reruns) or
  "full rerun N/N failed"; the Design's "full rerun 1/2 failed" is an illustration of the
  shape, not a literal string.

## Rollout / migration

Additive and opt-in per gate. Old ledgers and sidecars have no `attempts` key, and readers
treat a missing key as "no retries".

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

(none)
