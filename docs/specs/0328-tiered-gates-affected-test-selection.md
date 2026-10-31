---
id: SPEC-0328
title: "Tiered gates (when: loop | close | always) + static affected-test selection for fix loops"
status: done
owner: user
created: 2026-10-31
updated: 2026-10-31
milestone: "M9: agentic workflow"
kind: feature
tags: [agentic, gates, testing, performance]
depends_on: [SPEC-0170, SPEC-0177, SPEC-0289, SPEC-0309]
source_idea: IDEA-554
---

## Context

Every campaign gate ran the whole of `.wt/project.yaml`: for wt that is spec-lint plus
`uv run pytest -q -x -n 4`, the full ~4300-test suite (20-35 min on the loaded host). The gates
run after the implementer, after every fix loop and again in the close re-gate on the rebased
tree, so a campaign paid for the full suite about 4 times, and fix loops (the step that should
be fastest) were the slowest. On 2026-10-31 the human asked to "fix this horrific testing
situation FAST". IDEA-554 holds the researched practice: test impact analysis (pytest-testmon,
pytest-impact), tiered presubmit/postsubmit testing (Google TAP), merge-queue batching with
bisect, flaky quarantine, predictive selection.

This spec is phase 1: tiers plus a static affected-test selection. Phases 2-3 are follow-ups
(below), not built here.

## Goals / Non-goals

**Goals**
- A gate can be declared for the fix loop only, the close only, or both.
- wt's own fix-loop gate runs only the tests a branch affects; the full suite runs once, at close.
- Deterministic, cheap selection with no state to seed or keep in sync.
- Every existing project config (comfort-assistant included) behaves exactly as before.

**Non-goals**
- Batching close-train full runs, bisect, periodic main runs, flaky quarantine (phase 2).
- Coverage-based or predictive selection (phase 3).

## Decision

1. **Gate tiers.** A gate may set `when: loop | close | always` (absent ⇒ `always`). The GATING
   state (after implement / after each fix) runs `loop` + `always` gates; the close re-gate on
   the rebased tree runs `close` + `always` gates. A debug `wt queue gate <campaign>` run runs
   every gate.
2. **Static affected-test selection**, `tools/affected_tests.py` (stdlib only): changed files
   from `git diff --name-only merge-base(base, HEAD)...HEAD` plus uncommitted and untracked
   files, mapped to test files through a static AST import graph over `src/wt` and `tests`.
   The gate command calls the tool itself (`--run CMD...` appends the selection), so the gate
   runner stays generic: no wt-specific `{affected}` placeholder.
3. **wt's `.wt/project.yaml`**: `spec-lint` stays `always`; a new `pytest-affected` gate
   (`when: loop`) runs the selection with `-n 2` plus a ~1 s config smoke set; the full `pytest`
   gate becomes `when: close`. Both keep `retry_cmd` / `retries` (SPEC-0289).

## Design

**Schema** (`src/wt/agentic/project.py`): `when` joins the allowed gate keys, validated against
`GATE_WHEN = (loop, close, always)`. It is *not* added to `GATE_DEFAULTS`, so resolved gate dicts
of existing configs are byte-identical; `gate_when(g)` reads it with the `always` default.
`gates_for(project_cfg, tier)` returns the tier's gates plus `always` ones in config order
(`tier=None` ⇒ all; an unknown tier raises). The `STARTER` template documents the key.

**Runner** (`src/wt/agentic/gates.py`): `run_all(..., tier=None)` and `run_and_log(..., tier=None)`
filter with `gates_for` before taking the gate slot. Call sites: `queue._do_gate` passes
`tier="loop"`; `close.prepare_close` (the re-gate) passes `tier="close"`;
`queue._close_regates` (SPEC-0259/0263: does the close re-gate count as a gate?) now asks
whether any required *close-tier* gate exists. `wt queue gate` passes no tier.

**Selection** (`tools/affected_tests.py`):
- Graph: every `.py` under `src/wt` and `tests` is a node (`tests/x.py` ⇒ module `x`, as tests
  import each other by bare name). Edges come from every `import` / `from … import` anywhere in
  the file (function-level lazy imports included), relative imports resolved, `from pkg import
  sub` resolving to the submodule.
- Changed test module ⇒ itself plus the test modules importing it (shared helpers such as
  `test_agentic_e2e` select their importers).
- Changed src module ⇒ its **nearest tests**: a breadth-first walk of the reverse graph that
  stops at the first src level holding any test file (`--depth N` always walks N src levels
  first; default 0). Hops through test helper modules are free. The CLI hubs (`wt.cli`,
  `wt.agentic.cli`, `wt.agentic.cli_*`) are never walked past.
- Full-suite fallback (the tool prints `tests` / `--run` runs the bare command) when: a
  `conftest.py`, `tests/waits.py`, `pyproject.toml`, `uv.lock`, anything under `tools/` or `.wt/`,
  a non-Python file under `src/wt` or under `tests/` (fixtures, data) changed; a changed src
  module reaches no test; a src/test module was removed (its importers are unknown); the diff
  against the base fails; or the selection exceeds 40 % of test files (`--max-fraction`).
- Changes with no mapping and no trigger (docs, specs) select nothing; `--include PATH` (the
  smoke set) is always added; with an empty selection `--run` prints `no affected tests` and
  exits 0. `--base` defaults to `$WT_BASE` (set by `gate_env`), else `main`.

**Why nearest tests rather than the full transitive closure** (measured on this repo, 199 test
files): wt's modules form one dense import mesh (queue ⇄ gates ⇄ events ⇄ close through lazy
imports) and 88 test files import `wt.cli`, which imports everything. The full transitive
closure of `gates.py` selects 164 test files (82 %); with the CLI hubs cut, 96 (48 %); with
module-level imports only, 79 (40 %); `store.py`, `specs.py`, `org_write.py` select 92-175. Any
of those means the 40 % fallback, i.e. the full suite, on almost every campaign, which is the
problem this spec exists to fix. The nearest-tests rule selects 12 files for `gates.py`,
11 for `project.py`, 15 for `close.py`, 75 for `queue.py`, 1 for `gate_results.py`. What it can
miss (a behavior change seen only by tests of a module two levels up) is caught by the close
gate's full run, which is the safety net by design (presubmit-lite + postsubmit-full).

**pytest-testmon, evaluated.** Testmon keeps a per-checkout coverage database (`.testmondata`)
and selects tests whose recorded coverage touches changed blocks. It is more precise, but: each
campaign worktree would need a full instrumented seeding run (the very cost we are cutting) or
a copied DB from main that goes stale as main moves; it runs under coverage tracing (slower
runs); and a rebase rewrites the base under the DB, so selection after `git rebase` is not
reliable without reseeding. Static selection needs no state, is deterministic and runs in
well under a second. Testmon (or a predictive model) is phase 3.

## Alternatives considered

- **`{affected}` placeholder expanded by the gate runner** — puts a wt-specific Python import
  analysis inside the generic runner and still needs an "empty selection" convention; the tool
  calling pytest itself keeps `gates.py` generic and works for any project's own selector.
- **Full transitive closure** (pytest-impact style) — measured above: falls back to the full
  suite for nearly every wt change.
- **pytest-testmon** — see Design.
- **Adding `when` to `GATE_DEFAULTS`** — changes every resolved gate dict and the tests that pin
  them; reading with a default is equivalent.

## Acceptance criteria

- [x] `when: loop | close | always` validated per gate; bad values rejected by key path; absent ⇒ `always`; documented in `STARTER`.
- [x] The GATING state runs only `loop` + `always` gates; the close re-gate only `close` + `always` gates.
- [x] A config with no `when` keys runs every gate in both states, as before.
- [x] `tools/affected_tests.py` selects changed tests, the nearest importing tests of changed src modules (transitively through helpers and src levels when needed), and falls back to the full suite on every listed trigger.
- [x] wt's `.wt/project.yaml`: `spec-lint` always, `pytest-affected` loop, `pytest` close; `retry_cmd` kept on both pytest gates.
- [x] Phases 2-3 recorded as follow-ups.

## Test plan

- **Automated tests:**
  - `tests/test_affected_tests.py` (temp git repo shaped like wt): changed-file discovery
    (committed, uncommitted, untracked; main moving on is ignored); changed src module ⇒ nearest
    importing tests; nearest transitive tests when no direct importer (including an in-function
    import); transitive through a test helper; the hub is not walked past; `--depth`; changed
    test ⇒ itself; changed helper ⇒ its importers; docs-only ⇒ nothing; every fallback trigger
    (conftest, nested conftest, waits.py, pyproject, uv.lock, tools/, .wt/, non-Python under
    src/wt, test data, unreachable src module, removed module, >fraction, unknown base) ⇒ full;
    the CLI's print and `--run` modes (selection appended, bare full run, empty ⇒ no run).
  - `tests/test_gate_tiers.py`: `gates_for` filtering and order, default `always`, unknown tier,
    validation of good/bad values, STARTER validates; comfort-assistant style config unchanged;
    integration through `wt queue run --until-idle`: the ledger's GATING gate event names
    `[always, loop]` and the close event `[always, close]`; an untiered config runs both gates
    in both.
  - `tests/test_agentic_dogfood_config.py` updated for the new gate set and tiers.
- **Manual verification:** timed the loop gate's selection for a sample `gates.py` change vs.
  the full suite (As shipped).
- **Regression guard:** full suite once before landing; gates/project/close tests re-run after
  the rebase onto main.

## Rollout / migration

None for other projects: no `when` ⇒ unchanged. wt's own config takes effect on main at once:
the runner reloads the project config for each gate run (`ctx.project(state)`), so campaigns
already in flight use the loop gate at their next GATING and the full suite at their next close.

## Follow-ups

- **Phase 2:** batch the close train's full runs across several campaigns (rebase N together, one
  full run, bisect on red, ~log2 N extra runs); a periodic full run on main with culprit bisect
  to catch what selection missed; the flaky quarantine (SPEC-0293 record) so cross-campaign
  flakes leave the critical path and file a bug.
- **Phase 3:** coverage-based (pytest-testmon) or predictive test selection (Meta-style model),
  tied to IDEA-423; tune `--depth` / `--max-fraction` from close-gate misses.

## As shipped

Landed directly on main at the human's request (2026-10-31), without a queue campaign.

Measured on the loaded host, branch tree, `-n 2`:
- loop gate for a sample change to `src/wt/agentic/gates.py`: 12 selected test files + the
  2-file smoke set, 538 tests, **103 s** wall;
- full suite (same tree, `-n 2`, under the shared full-suite lock): 4363 passed, 3 skipped,
  2 failed in **970 s** (16 min 10 s). The two failures
  (`test_agentic_nonblocking::test_heartbeat_spans_many_ticks`,
  `test_agentic_resolver::test_interrupted_resolver_not_counted`) pass in isolation: load-timing
  flakes outside the changed code, in the set another agent is deflaking.

So a fix loop's test gate drops from ~16 min (35 min under concurrent gates) to under 2 min for
a typical agentic-module change; the full suite runs once per campaign, at close.

Landing order: the code commit first, then the `.wt/project.yaml` switch in its own commit, so
a runner still on the pre-SPEC-0328 code never reads a `when:` key it would reject
(`config_invalid` park, self-healing but noisy).

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

- None.
