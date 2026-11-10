---
id: SPEC-0525
title: "Implementer and spec-writer prompts require one speed-tier marker"
status: accepted
owner: user
created: 2026-11-09
updated: 2026-11-09
milestone: "M9: agentic workflow"
kind: feature
tags: [agentic, personas, testing]
depends_on: [SPEC-0490]
source_idea: IDEA-1129
---

## Context

[SPEC-0490](./0490-tier-markers-and-fast-only-gates.md) requires every `tests/test_*.py` module
to carry exactly one module-level speed-tier marker (`fast`, `integration`, `tui`, `remote`,
or `perf`). `tests/test_test_tiers.py::test_every_real_test_module_has_exactly_one_tier` runs
`python3 tools/test_tiers.py --check` and is a `tree_guard`, so the loop gate and the close
gate both run it. The first gate of IDEA-1020, IDEA-1070, and IDEA-1040 failed that guard:
each campaign added an unmarked `tests/test_*.py`. Each failure cost a triage-and-fix round
trip of about five minutes.

The implementer procedure (`src/nightwright/agentic/personas/implementer.md`) tells the
implementer to write the tests named in the Test plan and to iterate with targeted tests
([SPEC-0288](./0288-implementer-prompt-run-targeted-tests-while.md)). It never mentions a tier
marker or `tools/test_tiers.py`. The spec-writer procedure
(`src/nightwright/agentic/personas/spec_writer.md`) never tells the writer to put that
requirement into a Test plan that introduces a new module. The red-base paragraph tells the
implementer to skip steps 2, 4, 6, and 7, so a rule that lives only inside step 4 would not
apply when a red-base fix adds a test module.

## Goals / Non-goals

**Goals**
- The assembled implementer prompt states that every new `tests/test_*.py` needs exactly one
  module-level speed-tier marker, names the five tiers, and says how to choose the tier.
- That prompt tells the implementer to run `python3 tools/test_tiers.py --check` in their own
  loop and to fix every module it lists until it exits 0, before they return and before the
  campaign gate.
- The same marker rule stays in force for a red-base fix, which skips procedure steps 2, 4, 6,
  and 7.
- The assembled spec-writer prompt states the same rule for a Test plan that introduces a new
  `tests/test_*.py`, including which tier to name and that the Test plan requires the check to
  exit 0.

**Non-goals**
- Changing `tools/test_tiers.py`, the tier taxonomy, `pyproject.toml` markers, gate exclusion,
  or `test_every_real_test_module_has_exactly_one_tier`.
- A queue step or a new gate that runs `--check` and bounces the campaign. The check runs
  inside the implementer's loop so the file is marked before the gate starts.
- Rewriting tier markers on modules that already have one.
- Editing `AGENTS.md` or any persona other than `implementer` and `spec_writer`.

## Decision

Add the tier rule to the two persona templates and pin it with tests. The implementer runs
`python3 tools/test_tiers.py --check` as a step of their own loop, after they add a
`tests/test_*.py` and before they return, which is before the campaign gate. The spec writer
writes that same requirement into any Test plan that introduces a new `tests/test_*.py`. The
rule is an unnumbered paragraph, so the red-base instruction to skip steps 2, 4, 6, and 7
leaves it in force. Prompt text only: no queue, gate, or tool change.

## Design

Both templates are rendered by `personas.build_prompt`. New sentences contain no
`{{placeholders}}`. Tests compare the whitespace-unwrapped prompt (`" ".join(prompt.split())`),
the same way `test_implementer_prompt_targeted_tests` does. Procedure steps stay numbered 1
through 8. Step 5 keeps its single "for nightwright itself: spec-lint" clause
([SPEC-0288](./0288-implementer-prompt-run-targeted-tests-while.md)).

**Implementer** (`src/nightwright/agentic/personas/implementer.md`):

- At the end of procedure step 5, add: `On each iteration that adds a tests/test_*.py, run the tier check in **Test tier marker** below before you move on.`
  That sentence does not contain `spec-lint`.
- After step 8 and before "Do not widen scope", add this paragraph (line wrapping may match
  the file; the unwrapped text is what the test pins):

  **Test tier marker (SPEC-0490).** Every new `tests/test_*.py` needs exactly one module-level speed-tier marker, `pytestmark = pytest.mark.<tier>` with `<tier>` one of `fast`, `integration`, `tui`, `remote`, or `perf`. `slow` is not a tier. Non-tier marks such as `skipif` and `tree_guard` may share that `pytestmark` list. A module that is not named `test_*.py` needs no tier. A single test may carry a heavier function-level mark; the module `pytestmark` still carries exactly one tier. Choose the tier with the SPEC-0490 taxonomy, first match: `test_tui_budget` is `perf`; `test_gate_timeout_tree` is `integration`; `test_tui_*`, or a module that imports `tui_harness` or `tui_budget` or calls `run_test`, is `tui`; `test_remote_*`, `test_pull_worker*`, or a module that imports `remote_loopback`, `remote_support`, or `remote_workers_golden`, is `remote`; a name ending in `_e2e`, `_integration`, or `_race`, or a module whose tests sum past 20 seconds, is `integration`; otherwise `fast`. In your loop, before you return and before the campaign gate, run `python3 tools/test_tiers.py --check` when `tools/test_tiers.py` exists, and fix every module it lists until it exits 0. When that file is absent, skip the command.

- At the end of the **Red-base fix (no spec)** paragraph, add: `The **Test tier marker** rule still applies to any tests/test_*.py you add.`

**Spec writer** (`src/nightwright/agentic/personas/spec_writer.md`), one new bullet under
"Always:":

- A new `tests/test_*.py` that the Test plan introduces needs exactly one module-level speed-tier marker written into that plan: `pytestmark = pytest.mark.<tier>` with `<tier>` one of `fast`, `integration`, `tui`, `remote`, or `perf`. `slow` is not a tier. Name the tier from the SPEC-0490 taxonomy (first match): `test_tui_budget` is `perf`; `test_gate_timeout_tree` is `integration`; `test_tui_*`, or a module that imports `tui_harness` or `tui_budget` or calls `run_test`, is `tui`; `test_remote_*`, `test_pull_worker*`, or a module that imports `remote_loopback`, `remote_support`, or `remote_workers_golden`, is `remote`; a name ending in `_e2e`, `_integration`, or `_race` is `integration`; otherwise `fast`. The Test plan must say that `python3 tools/test_tiers.py --check` exits 0 when `tools/test_tiers.py` exists. Extending a module that already has its one marker adds no second tier.

The spec writer does not create the test file and does not run the check. The 20-second sum
stays on the implementer prompt: the writer cannot measure it, and `--check` does not measure
it either. A module the writer marks `fast` that later sums past 20 seconds is re-tiered by
the implementer under the paragraph above.

`tests/test_agentic_personas.py` already has `pytestmark = pytest.mark.fast`. The new tests
extend that module. They do not add a `tests/test_*.py` file and they do not add a second tier
mark.

## Alternatives considered

- **A loop gate that runs `--check` before pytest.** A red check would still enter triage, so
  the round trip this idea removes would remain. The implementer fixes the marker before
  returning, and the existing guard then passes on the first gate.
- **Defaulting an unmarked module to `fast` inside the gate.** A heavy module would run in the
  fast tier, or a skipped mark would hide the new tests. The marker stays explicit.
- **Putting the rule only in `AGENTS.md`.** The procedure the implementer follows step by step
  is the persona prompt. `AGENTS.md` is already in `agents.context_files` and did not state
  this rule when the three gates failed.

## Acceptance criteria

- [ ] The assembled implementer prompt (`build_prompt("implementer", ctx)`) says every new
      `tests/test_*.py` needs exactly one module-level speed-tier marker
      `pytestmark = pytest.mark.<tier>`, names `fast`, `integration`, `tui`, `remote`, and
      `perf`, and says `slow` is not a tier.
- [ ] That prompt states the taxonomy in Design: `test_tui_budget` is `perf`;
      `test_gate_timeout_tree` is `integration`; `test_tui_*`, or a module that imports
      `tui_harness` or `tui_budget` or calls `run_test`, is `tui`; `test_remote_*`,
      `test_pull_worker*`, or a module that imports `remote_loopback`, `remote_support`, or
      `remote_workers_golden`, is `remote`; a name ending in `_e2e`, `_integration`, or
      `_race`, or a module whose tests sum past 20 seconds, is `integration`; otherwise
      `fast`. Each of those structural triggers is enough on its own. It says a
      function-level mark may be heavier and the module `pytestmark` still carries exactly
      one tier, and that a module not named `test_*.py` needs no tier.
- [ ] That prompt tells the implementer to run `python3 tools/test_tiers.py --check` in their
      loop, before they return and before the campaign gate, when `tools/test_tiers.py`
      exists, and to fix every module it lists until it exits 0. Step 5 points at the
      **Test tier marker** paragraph. The red-base paragraph says that rule still applies to
      any `tests/test_*.py` they add.
- [ ] The assembled spec-writer prompt says a Test plan that introduces a new
      `tests/test_*.py` names exactly one of those five tiers with
      `pytestmark = pytest.mark.<tier>`, says `slow` is not a tier, states the structural
      taxonomy in Design (without the 20-second clause), and says the Test plan requires
      `python3 tools/test_tiers.py --check` to exit 0 when `tools/test_tiers.py` exists.
      Extending a module that already has its one marker adds no second tier.
- [ ] Procedure steps stay 1 through 8. Step 5 still contains exactly one `spec-lint`, inside
      "for nightwright itself: spec-lint". The spec-change guard sentences pinned by
      `test_personas_state_spec_change_guard_rules` stay. `tests/test_agentic_personas.py`
      still has exactly one module-level tier mark, `fast`.

## Test plan

- **Automated tests:** extend `tests/test_agentic_personas.py` (existing module mark
  `pytestmark = pytest.mark.fast`; do not add a new `tests/test_*.py`).
  - `test_implementer_prompt_requires_one_tier_marker` builds `ctx` with
    `PS.build_context(_cfg(tmp_path), _state(tmp_path), _project_cfg())` and unwraps
    `PS.build_prompt("implementer", ctx)`. It asserts each phrase in the Design paragraph and
    the step-5 and red-base sentences, including `before the campaign gate`,
    `python3 tools/test_tiers.py --check`, `until it exits 0`, the five tier names, `slow` is
    not a tier, and the taxonomy names listed in the acceptance criteria. It asserts the
    substrings `` `test_tui_*`, or a module that imports `` and `or a module whose tests sum past 20 seconds`, so the test fails if `and` joins `test_tui_*` to the import rule or joins a name suffix to the 20-second sum. It asserts the
    step-5 slice (`split("5. ")` then `split(" 6. ")`, as
    `test_implementer_prompt_targeted_tests`) contains `Test tier marker` and does not add a
    second `spec-lint`.
  - `test_spec_writer_prompt_requires_one_tier_marker` unwraps
    `PS.build_prompt("spec_writer", ctx)` with the same context (static bullet; missing
    placeholders render as `(none)`) and asserts the Design bullet: exactly one marker, the
    five tiers, `slow` is not a tier, the structural taxonomy, `--check` exits 0 when
    `tools/test_tiers.py` exists, and no second tier on a module that already has one. It
    asserts the substrings `` `test_tui_*`, or a module that imports `` and `` or a module that imports `remote_loopback` ``, so the test fails if `and` joins `test_tui_*` to the import rule or joins a remote name to the `remote_loopback` import. It
    asserts that bullet sits in the "Always:" section (after `Always:` and before
    `Pending brief`).
  - Sabotage: delete the new implementer paragraph and the new spec-writer bullet, confirm
    the two new tests fail, then restore them.
- **Manual verification:** `uv run nw campaign prompt <campaign-id> implementer` prints the
  **Test tier marker** paragraph, the step-5 pointer, and the red-base sentence.
  `uv run nw campaign prompt <campaign-id> spec_writer` prints the new Always bullet. The
  hidden `prompt` command renders the template; the bullet is static, so it is present even
  when `write_rules` renders as `(none)`.
- **Regression guard:** `uv run pytest -q tests/test_agentic_personas.py` (includes
  `test_implementer_prompt_targeted_tests` and
  `test_personas_state_spec_change_guard_rules`) and `python3 tools/spec_lint.py`. Then
  `python3 tools/test_tiers.py --check` exits 0. The full suite stays on the close gate and
  the sentinel ([SPEC-0378](./0378-affected-only-close-gate.md),
  [SPEC-0379](./0379-time-based-sentinel-on-worker-slots.md)).

## Clock Log

Implementation sessions clock in and out on IDEA-1129 with `nw idea clock-in` and
`nw idea clock-out`.

## Rollout / migration

The next implementer run and the next spec-writer run after merge receive the new text.
Revert is the two persona templates plus the two tests.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

None.
