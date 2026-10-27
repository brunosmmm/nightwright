---
id: SPEC-0284
title: "Gate result parsing: counts and failing test ids from pytest and JUnit XML"
status: done
kind: feature
owner: user
created: 2026-10-27
source_idea: IDEA-069
parent: SPEC-0169
depends_on: [SPEC-0173, SPEC-0192]
tags: [agentic, gates, triage]
---

## Context

Promoted from idea `IDEA-069`, which comes from item 1b of the seed plan's project config
("what results to read"). Today a gate gives only an exit code and a log tail:

- `src/wt/agentic/gates.py:run_gate` builds a `GateResult(name, required, ok, exit_code,
  timed_out, duration_s, log, tail, cmd, cwd, interrupted)`. `tail` holds the last
  `TAIL_LINES` (60) lines of the log.
- `gates._write_sidecar` writes `gates/NNN-name.json` (`wt.gate.v1`, SPEC-0192), with the
  keys listed in `schemas.GATE_KEYS`.
- `personas.render_gate_results` passes only the tail to the triager and the implementer.
  `cli_queue._gate_text` shows a ✓ or ✗ per gate.
- `project.py` accepts only these `gates[]` keys: `name, cmd, cwd, timeout_s, required`.

As a result, triage, the dashboard and `wt campaign export` cannot report something like "3
failed: test_x, test_y". If pytest's summary has scrolled out of the 60-line tail, the triager
has to guess. Tracking flaky tests across campaigns (IDEA-027) has no per-test data to work
from.

## Goals / Non-goals

**Goals**
- Each gate in `.wt/project.yaml` can set an optional `results:`. It is either
  `{format: pytest}`, which parses the gate log, or `{format: junit, path: <file>}`, where the
  path is relative to the gate's cwd.
- The parsed summary is `{format, passed, failed, errors, skipped, failing}`, where `failing`
  holds at most 50 test ids. It is stored in `GateResult.summary`, in the sidecar and in the
  `gate` ledger event.
- The triage prompt shows the summary above the tail. `wt queue watch` and
  `wt campaign show` show "N failed".
- A parse failure never changes `ok`. It is recorded as `summary: {format, error}`.

**Non-goals**
- ruff/eslint JSON and other formats. The parser registry leaves room to add them later.
- Flaky detection and per-gate retries. Both belong to IDEA-027 (gate ladder v2); this spec
  only provides the data.
- Detecting pytest automatically when a gate has no `results:`.

## Decision

Add a small parser module, `src/wt/agentic/gate_results.py`, with
`parse(results_cfg, log_path, cwd) -> dict | None` and two parsers. `run_gate` calls it after
the gate process ends.

The summary is purely additive: `summary` is an optional field on `GateResult`, in the sidecar
and in the ledger. Old campaigns and old readers are not affected.

## Design

- `src/wt/agentic/project.py`, in the gate validation loop: accept a `results` key. It must
  be a mapping whose `format` is `pytest` or `junit`. `junit` also needs a non-empty relative
  `path`; absolute paths and paths containing `..` are refused. Problems are reported the
  same way as for the other gate keys.
- `src/wt/agentic/gate_results.py` (new):
  - `parse_pytest(text)`:
    - Counts come from the last line that matches pytest's summary: either `=+ … in N.NNs =+`
      or the `-q` form `N failed, M passed … in N.NNs`. It reads `passed`, `failed`,
      `errors` (from both `error` and `errors`) and `skipped`.
    - `failing` holds the node ids from `^(FAILED|ERROR) (\S+)` lines in the short test
      summary. The ids are de-duplicated, kept in order, and capped at 50.
    - If there is no summary line, it raises `ValueError("no pytest summary line")`.
  - `parse_junit(path)`:
    - It reads `<testsuite>` or `<testsuites>` with `xml.etree.ElementTree`.
    - Counts come from the `<testcase>` children and their failure, error and skipped
      elements.
    - Failing ids are written as `classname::name`, capped at 50.
    - Files larger than 20 MB are refused.
  - `parse(results_cfg, log_path, cwd)` dispatches on `format`:
    - On success it returns `{"format", …}`.
    - On any exception it returns `{"format", "error": str}`.
    - When `results_cfg` is None it returns None.
- `src/wt/agentic/gates.py`:
  - `GateResult` gains `summary: dict | None = None`.
  - `run_gate` computes the summary after the process ends and before `_write_sidecar`. It
    skips gates that were never started or were interrupted.
  - `_write_sidecar` adds `"summary"` only when it is not None.
  - The `gate` ledger event already carries the results through the existing `asdict` path.
- `src/wt/agentic/schemas.py`: no change to the required keys, because `summary` is optional
  and extra keys are allowed. The `wt.gate.v1` doc comment lists the new optional key.
- `src/wt/agentic/personas.py:render_gate_results`: when `r.get("summary")` has counts, add a
  line such as `results: 3 failed, 120 passed, 2 skipped; failing: a::b, c::d` before the
  tail. An error summary gets its own form of the line.
- `src/wt/agentic/cli_queue.py:_gate_text`: for a failed gate that has a summary, append
  ` (N failed)`.
- `.wt/project.yaml` (this repo): give the `pytest` gate `results: {format: pytest}`, so that
  dogfooding produces data.

## Alternatives considered

- **Always parse pytest by sniffing the command.** Rejected as fragile, because commands are
  often wrapped (`uv run`, `make test`). The explicit opt-in costs one line.
- **Keep a longer tail.** Rejected because it costs prompt tokens and still gives machines no
  counts or ids.
- **Let the triager persona parse the output.** Rejected because the result would not be
  deterministic, and the data would not reach the ledger for flaky tracking.

## Acceptance criteria

- [x] `.wt/project.yaml` accepts `results: {format: pytest}` and `{format: junit, path: …}`.
      These are reported as config problems:
      - unknown formats;
      - junit without a path;
      - absolute paths;
      - paths containing `..`.
- [x] A gate with `results` records `summary` (counts and at most 50 failing ids) in three
      places: its `GateResult`, its `wt.gate.v1` sidecar and the `gate` ledger event. Both
      the sidecar and the ledger pass `schemas` validation.
- [x] A parse failure records `summary.error` and leaves `ok` and `exit_code` unchanged. A
      gate without `results` produces no `summary` key.
- [x] `render_gate_results` shows the counts and failing ids in the triage and implementer
      prompts. The gate cell in `wt queue watch` shows "(N failed)".
- [x] This repo's `pytest` gate opts in with `format: pytest`.

## Test plan

- New `tests/test_agentic_gate_results.py`:
  - pytest summary forms: the full `===` line, the `-q` line, `1 error` and `2 errors`, and
    a log with no summary, which raises;
  - FAILED and ERROR id extraction, with de-duplication and the 50-id cap;
  - JUnit files using `testsuites` and `testsuite`, with failure, error and skipped cases;
  - the JUnit size cap;
  - `parse` returning the error form on garbage input.
- `tests/test_agentic_gates.py`:
  - `test_run_gate_records_summary`: a fake gate prints a pytest summary and exits 1. The
    sidecar has `summary.failed == 1` and `ok` is False.
  - `test_run_gate_parse_error_keeps_ok`: the gate exits 0 with garbage output. `ok` is True
    and `summary.error` is set.
  - `test_no_results_no_summary_key`.
- `tests/test_agentic_project.py`: validation of `results` for a valid config, a bad format,
  a missing path, an absolute path and a `..` path.
- `tests/test_agentic_personas.py`: `render_gate_results` includes the `results:` line.
- `tests/test_agentic_dashboard.py`: `_gate_text` shows `(2 failed)`.
- **Sabotage check:**
  - Make `run_gate` set `ok = summary.failed == 0`, so the parser decides `ok`.
    `test_run_gate_parse_error_keeps_ok` must fail.
  - Remove the 50-id cap. The cap test must fail.
- **Regression guard:** the full `uv run pytest`. `tests/test_agentic_logs.py`, which checks
  the sidecar schema, is unchanged.
- **Manual:** run `wt queue gates <campaign>` on a campaign worktree with a failing test,
  then open `gates/NNN-pytest.json` and confirm the counts and ids.

### Test record (2026-10-27)

- `uv run pytest -q -x`: 3520 passed, 3 skipped (full suite, no regressions;
  `tests/test_agentic_logs.py` unchanged). New and extended: `tests/test_agentic_gate_results.py`
  (new), `test_agentic_gates.py` (summary, parse-error-keeps-ok, junit cwd, no-summary-key,
  ledger/sidecar schema), `test_agentic_project.py` (`results:` validation),
  `test_agentic_personas.py` (`results:` line), `test_agentic_dashboard.py` (`(2 failed)`).
- Review round 1 (2026-10-27): focused suites (gates, gate_results, dashboard, project,
  personas) pass; full `uv run pytest -q -x`: 3527 passed, 3 skipped.
- Sabotage: with `run_gate` setting `ok` from `summary.failed == 0`,
  `test_run_gate_parse_error_keeps_ok` failed; with the 50-id cap removed, the pytest and JUnit
  cap tests failed. Both reverted.
- Manual `wt queue gates <campaign>` on a live campaign worktree was not run; the same path
  (`run_gate` -> sidecar -> `as_payload`) is exercised end to end by the gate tests, which
  read `gates/NNN-name.json` back and validate it against `schemas`.

### As shipped (2026-10-27)

- Matches the Design. `wt campaign show` has no gate rendering of its own; the "(N failed)"
  suffix lives in `cli_queue._gate_text`, the shared gate cell of `wt queue watch`.
- `results.path` is also refused on `format: pytest`; an error summary renders in the prompt as
  `results: could not parse <format> results (<error>)`.
- Review round 1 fixes (deviations/additions beyond Design): `queue._gate_summary` passes a
  gate's `summary` through to `dashboard_payload`, so the "(N failed)" suffix reaches
  `wt queue watch` (before, it was dropped there). A JUnit file whose mtime is older than the
  gate's start is refused as `summary.error` ("stale"), so a timed-out or early-failing gate
  never reports the previous round's failing ids. Extra tests pin the repo's own `pytest` gate
  opt-in, the stopped-gate (no summary) rule, the dashboard path, stale JUnit, and
  xfailed/xpassed/warnings tokens.

## Rollout / migration

Additive. Old sidecars and ledger events have no `summary`, and every reader treats a missing
summary as "no data".

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

(none)
