---
id: SPEC-0192
title: "Schema'd agentic logs: validated ledger, run.json, gate sidecars, runner journal"
status: done
owner: user
created: 2026-10-25
updated: 2026-10-25
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0169
tags: [agentic, logs, schema]
source_idea: IDEA-045
---

## Context

Child of epic [SPEC-0169](./0169-mini-agentic-workflow-queue-campaigns-headless.md),
promoted from `IDEA-045` (All saved agentic logs must be machine-readable and consistent
(JSONL with a schema), not free text). User requirement, 2026-10-25.

What the agentic layer saved before this spec:

- `campaigns/<id>/ledger.jsonl` — JSONL, but payload keys varied by event type with no
  declared schema and no validation, so a producer could drop a key silently.
- `<agentic>/runner.log` — free-text tick lines from `wt queue run`'s stdout, captured by the
  conductor's `nohup … >> runner.log`. They were hard-wrapped at 80 columns, so one record
  spanned two lines (IDEA-058).
- `runs/<NNN-persona>/result.json` — claude's raw stdout (the vendor's shape); `stderr.log`
  raw.
- `gates/<NNN-name>.log` — raw command output with a wt-written `# name: cmd (cwd …)` header
  and `# timed out …` / `# failed to start …` lines mixed in.

## Goals / Non-goals

**Goals**
- A versioned, declared shape for every record wt writes, validated where it is written.
- Raw third-party output stays raw, with a wt-written JSON record beside it.
- The runner gets a structured journal that the conductor can read (`wt queue runner-log`).

**Non-goals**
- Rewriting existing ledgers (old lines without `schema` read as v1).
- `events.jsonl` and the mailbox (already structured: `wt.events.v1` / `wt.message.v1`).
- Rotating `runner.jsonl`; a `wt campaign runs` view (IDEA-050 — now trivial over
  `run.json`).

## Decision

New module `wt.agentic.schemas` declares four schemas and their validators:
`wt.ledger.event.v1`, `wt.run.v1`, `wt.gate.v1` and `wt.runner.record.v1`.
`store.append_event` validates every ledger record and refuses to write invalid ones. Both
persona runners write `run.json`. `run_gate` writes a `.json` sidecar and keeps the `.log`
raw. `queue.run` appends to `runner.jsonl`.

## Design

- **Ledger (`wt.ledger.event.v1`).** Envelope `{schema, ts, seq, campaign, type, by}` plus
  `EVENT_KEYS[type]` (required keys, values may be null; extra keys allowed for additive
  evolution): open, transition, run.start, run.end, cost, commit, gate, park, resume, close,
  note, backlog, finding, priority. `append_event` adds `schema` and raises `ValueError`
  (nothing written) on an unknown type or a missing key. `ledger_problems(rec)` treats a
  record without `schema` as v1. `schemas.with_defaults(type, payload)` fills required keys
  with None for fixtures/tools. Human renderers skip `schema`.
- **Run record (`wt.run.v1`).** `runner.write_run_record(req, result, started_at)` writes
  `runs/<run_id>/run.json` with campaign, run_id, persona, model, effort, budget, timeout,
  permission mode, started_at, duration, exit/timed_out/interrupted/is_error, cost, usage,
  session_id, verdict_ok, verdict and `files` {prompt, raw, stderr}. It is called by both
  `ClaudeRunner` and `FakeRunner`. `interrupted` is the runner's view; the ledger's `run.end`
  keeps the step's view.
- **Gate sidecar (`wt.gate.v1`).** `gates/<NNN-name>.json`: name, cmd, cwd, required, ok,
  exit_code, timed_out, timeout_s, started_at, duration_s, log (file name), error ("failed to
  start: …" / "timed out after Ns; process group killed" / null). The `.log` now holds only
  the command's output. `GateResult` gains `cmd` and `cwd`, so the ledger's `gate.results`
  carry them. The gate triager prompt shows `command: \`…\`` under each gate heading, which
  replaces the lost header line.
- **Runner journal (`wt.runner.record.v1`).** `queue.journal(cfg, kind, …)` makes a single
  O_APPEND write to `<agentic>/runner.jsonl` and never raises. `queue.run` writes `start`
  {argv, interval_s}; `tick` {tick, steps, admitted, errors}, only for ticks that did
  something; `stop` {ticks, reason: once|max_ticks|idle|signal}; or `crash` {ticks, error,
  traceback} instead of `stop` when the loop raises. Every record carries `ts`, `pid` and
  `kind`. `wt queue runner-log [--tail N] [--json]` renders it (Rich) or emits
  `wt.runner.log.v1`. Stdout is unchanged, so the conductor's `runner.log` capture still
  catches tracebacks from before the loop starts.
- **Conductor skill:** the `runner_dead` row now reads `wt queue runner-log --tail 30`
  instead of the `runner.log` tail.

## Alternatives considered

- **JSON Schema files + `jsonschema` at runtime** — a new dependency for a handful of
  required-key checks; the declared tuples are enough and are themselves testable.
- **Drop `runner.log` and make `wt queue run` write JSON to stdout** — stdout is the human
  view and the crash catch-all; a separate journal keeps both.
- **Wrap gate output in JSONL** — commands write arbitrary bytes; a sidecar keeps the log
  byte-exact.

## Acceptance criteria

- [x] `append_event` writes `schema: wt.ledger.event.v1` and refuses an unknown type or a
      missing per-type key (file unchanged).
- [x] An end-to-end campaign (happy path and gate-fail → triage → fix) leaves a ledger whose
      every line validates.
- [x] Every persona run dir has a valid `run.json` agreeing with its `run.end` event.
- [x] Every gate log has a valid `.json` sidecar; the `.log` has no wt-written lines;
      timeouts and start failures are recorded as `error` in the sidecar.
- [x] `runner.jsonl` gets start / tick / stop (reason) records, or a crash record with the
      error and traceback; `wt queue runner-log` shows them, `--json` emits them.
- [x] Records written before this spec still read and validate as v1.
- [x] `uv run pytest` passes; `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests:** `tests/test_agentic_logs.py` — e2e happy path validates the ledger,
  every `run.json` (agrees with `run.end`, files exist), every gate sidecar, and
  `runner.jsonl` (start…tick…stop/idle, a CLOSING→MERGED step); a fail→triage→fix e2e
  validates again with 3 runs and failing + passing sidecars; append rejects bad records
  without writing; pre-schema records read as v1; a monkeypatched exploding tick journals
  `crash` (no `stop`); `wt queue runner-log` human / `--json` / `--tail`.
  `tests/test_agentic_gates.py` asserts the raw log and the sidecar (incl. timeout and
  start-failure errors). Fixtures that appended partial events now use
  `schemas.with_defaults`.
- **Regression guard:** full `uv run pytest`.

### Executed (2026-10-25)

- `tests/test_agentic_logs.py`: 6 passed; `tests/test_agentic_gates.py`: 10 passed.
- Full `uv run pytest`: 1580 passed, 1 skipped; spec_lint exits 0.
- 13 existing tests failed on the strict ledger at first. All were fixtures appending partial
  events (no production producer was missing a key, so the e2e suites passed unchanged).
  They were fixed with `with_defaults` or by completing the payload.

## Rollout / migration

Built on branch `idea-045-jsonl-logs` (worktree `/home/bruno/work/wt-idea-045`), merged
through the conductor. Code under `src/wt/agentic/` changed, so the runner must restart. New
files appear from the next campaign onward; existing ledgers and logs are untouched.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

(none)
