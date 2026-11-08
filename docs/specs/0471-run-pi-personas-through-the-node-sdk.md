---
id: SPEC-0471
title: Pi test requires the task text in the user message
status: done
owner: user
created: 2026-11-08
updated: 2026-11-08
kind: feature
tags: [agentic, runner, pi]
source_idea: IDEA-1023
---

## Context

[SPEC-0465](./0465-run-pi-personas-through-the-node-sdk.md) is the Pi persona
child: `node` runs `pi_exec.mjs`, one in-memory session, one `prompt`, then
`dispose`. That spec was numbered SPEC-0451 on the campaign branch.
[SPEC-0451](./0451-tail-marks-spoken-lines.md) on main is the tail's spoken-line
marker, so the Pi work was renumbered to SPEC-0465. The implementation
comments, `pi_map.yaml`, and the Pi tests still name SPEC-0451.

`execute` builds the user message as `prompt.md` plus the schema footer and
passes that string to `session.prompt`. `appendSystemPrompt` is
`system_append` (the invariant). `tests/js/pi_exec.test.js` writes
`task text\n\nINVARIANT` to `prompt.md` and checks that the prompt argument
contains the invariant once and the schema footer once. It never checks
`task text`. A user message that drops the task body still passes.

The abandoned IDEA-1023 campaign spent three implement loops on SPEC-0465.
This replacement campaign does not reopen that design. Its job is the missing
assertion, and to stop citing SPEC-0451 for the Pi runner before close.

## Goals / Non-goals

**Goals**

- The Node test that calls `execute` requires the task body from `prompt.md`
  in the `session.prompt` argument, and fails when that body is absent from
  the argument.
- Comments, docstrings, and the `pi_map.yaml` header that attribute the Pi
  runner to SPEC-0451 attribute it to SPEC-0465.

**Non-goals**

- A new Pi runner, map, doctor check, cost rule, tool list, or prompt
  composition. Those stay as SPEC-0465 shipped them.
- A startup error when `prompt.md` has no task body. Missing `prompt.md` is
  already an `is_error` result.
- RPC mode, replacing the conductor or steward, a Python port of Pi, and
  loading user extensions or skills.
- Editing SPEC-0451, SPEC-0452, or SPEC-0465.

## Decision

`tests/js/pi_exec.test.js` asserts that the task body written to `prompt.md`
appears in the string passed to `session.prompt`, earlier than the schema
footer. The assertion reads that argument. `appendSystemPrompt` holding the
same words does not satisfy it. When the argument lacks the task body, that
test fails.

Pi sources that still say the runner is SPEC-0451 say SPEC-0465. SPEC-0451
remains the tail spec.

## Design

### User-message assertion

`layout()` in `tests/js/pi_exec.test.js` writes `prompt.md`. The task body is
the leading `task text` in that file (the fixture string, shared with the
assertion so the two cannot drift). The test
`execute: in-memory session, settings, loader flags, appendSystemPrompt`
already records `over.promptText` from the stub `session.prompt`.

That test also asserts:

- `over.promptText` contains the task body.
- The task body occurs before `Reply with a single JSON object`.
- The invariant still occurs once in `over.promptText`, and the schema body
  still occurs once.
- `over.loaderOpts.appendSystemPrompt` is still `["INVARIANT"]`.

`execute` already sets `userMessage` to `prompt.md` plus the footer. This spec
adds the assertion. It does not change that composition.

### Renumber

Replace the contiguous id `SPEC-0451` with `SPEC-0465` where it names the Pi
runner, in:

- `src/nightwright/agentic/pi_exec.mjs`
- `src/nightwright/agentic/pi_map.py`
- `src/nightwright/agentic/pi_map.yaml`
- `src/nightwright/agentic/runner.py`
- `src/nightwright/agentic/runproc.py`
- `src/nightwright/agentic/cli_worker.py`
- `src/nightwright/agentic/worker.py`
- `src/nightwright/agentic/remote_personas.py`
- `src/nightwright/doctor.py`
- `tests/test_pi_exec_js.py`
- `tests/test_rich_output_guard.py` (the `worker_pi_check` reason)

`tests/test_pi_runner.py` `test_remote_header_runner_pi` uses `SPEC-0451` as a
stand-in campaign id. Change that `id` and `spec_id` to `c1`. The file then
has no contiguous `SPEC-0451`. The new regression test in that file builds
the forbidden id from two string parts (`"SPEC-"` and `"0451"`) so its source
is not a hit.

`docs/specs/0451-tail-marks-spoken-lines.md` and SPEC-0452's `depends_on` stay.

## Alternatives considered

- **Supersede SPEC-0465 with a full restatement of the Pi child.** The child,
  map, and doctor already exist. The abandoned loops left this assertion and
  the stale id.
- **Fail `execute` when the task body is missing.** The conductor asked the
  test to fail when the user message lacks the task text. Missing `prompt.md`
  is already a job error.
- **Assert the task body only on `appendSystemPrompt`.** That channel is the
  invariant. The task body is the user message.

## Acceptance criteria

- [x] `node --test tests/js/pi_exec.test.js` asserts that `over.promptText`
      contains the `prompt.md` task body `task text`, and that this substring
      occurs before `Reply with a single JSON object`. Removing the task body
      from the `session.prompt` argument fails that test.
- [x] That same test still requires the invariant once in `over.promptText`,
      the schema body once, and `appendSystemPrompt` equal to `["INVARIANT"]`.
- [x] The Pi files listed in Design contain no contiguous `SPEC-0451`. Each
      former citation is `SPEC-0465`. `test_remote_header_runner_pi` uses
      campaign id `c1`.
- [x] SPEC-0451's title and body stay the spoken-line tail spec. SPEC-0452
      still `depends_on` SPEC-0451.
- [x] `execute`'s user message stays `prompt.md` plus one schema footer.
      Claude and Cursor launch argv stay as they are.

## Test plan

- **Automated tests:** `node --test tests/js/pi_exec.test.js` covers the
  prompt argument (task body before the footer, invariant once, schema once,
  `appendSystemPrompt`). `tests/test_pi_runner.py` gains
  `test_pi_sources_cite_spec_0465`: it builds the forbidden id from `"SPEC-"`
  and `"0451"`, reads every path in Design (including itself), and fails if
  that contiguous id appears. `tests/test_pi_exec_js.py` still runs the Node
  file under pytest. No network and no live Pi package.
- **Manual verification:** none. The stub session records the prompt argument.
- **Regression guard:** `python3 tools/affected_tests.py --run uv run pytest -q`
  on the touched tests, plus the Node file. `tests/test_cursor_runner.py` and
  the Claude argv tests in `tests/test_agentic_runner.py` stay green.
  `docs/specs/0451-tail-marks-spoken-lines.md` is not edited.

### Test record (2026-11-08)

- `node --test tests/js/pi_exec.test.js`: 63 passed.
- `uv run pytest -q tests/test_pi_runner.py tests/test_pi_exec_js.py tests/test_rich_output_guard.py tests/test_agentic_runner.py tests/test_cursor_runner.py`: 87 passed.
- Full suite left to gating.

## Rollout / migration

No config, map, or process change. The next `node --test` of
`tests/js/pi_exec.test.js` enforces the task body. In-flight persona runs keep
the argv they launched with. Spec comments that move from SPEC-0451 to
SPEC-0465 do not change runtime behavior.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest` on the
      affected set, plus `node --test tests/js/pi_exec.test.js`).
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

None.
