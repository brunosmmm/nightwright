---
id: SPEC-0355
title: "Faster code gating: narrow affected-test fallbacks, close reuse of a green loop gate, failed-first reruns"
status: accepted
owner: user
created: 2026-11-01
updated: 2026-11-01
milestone: "M9: agentic workflow"
kind: feature
source_idea: IDEA-649
tags: [agentic, gates, close, merge, tests]
depends_on: [SPEC-0289, SPEC-0328, SPEC-0350]
---

## Context

[SPEC-0328](./0328-tiered-gates-affected-test-selection.md) split gates into a loop tier
(`pytest-affected`: `tools/affected_tests.py` picks the tests a branch affects) and a close tier
(the full `pytest` suite once, on the rebased tree). [SPEC-0350](./0350-per-gate-path-sensitivity.md)
skips close gates whose `paths` the branch does not touch. Only docs-only merges got faster
(24 s instead of about 600 s). Code campaigns still spend most of their wall time in gates. The
2026-11-01 ledgers and gate logs show:

- **SPEC-0338**: 8001 s in gates. 7 or more `pytest-affected` rounds fell back to the **full** suite
  (11-24 min each). `affected_tests.py` logged `full suite: .nw/project.yaml changed (static
  selection unsafe)` and `test data changed: tests/data/remote_gates_golden.json`.
- **SPEC-0354**: 1028 s on a fallback `no test reaches src/nightwright/agentic/cli_workers.py`.
  This was a new CLI-wiring module. It is a hub (`agentic.cli_*`), so the walk never goes past it.
- **Close and merge tier**: the full `pytest` gate takes 566-822 s for any `src/**` or `tools/**`
  change. It runs even when the loop tier just proved the same branch green and the rebase only
  brought in unrelated main commits.
- **Failures**: a red loop round is followed by a fix and then a whole new round. The round
  re-runs the full selection, or the full suite on a fallback, before it gets back to the test
  that failed. The SPEC-0289 ladder only retries *within* one round.
- **Slot waits** of 317-671 s add to all of this. Shorter gate runs make these waits shorter too.

## Goals / Non-goals

**Goals**
- G1. `affected_tests.py` falls back to the full suite only for changes it cannot map:
  `pyproject.toml`, `uv.lock`, any `conftest.py`, `tests/waits.py`, a removed module, an unreadable
  diff, or a selection above `MAX_FRACTION`. Config, test data, tools, package data and
  unreached src modules map to targeted sets.
- G2. A close-tier gate may reuse the last green loop-tier result instead of running, when the
  rebase is *clean* (defined below). Each reuse is recorded as a `gate.reused` ledger event with
  its evidence. Each decline is recorded as a `gate.reuse_declined` event.
- G3. A periodic **full-suite sentinel** on the base branch catches what reuse lets through. A red
  sentinel turns reuse off until main is green again.
- G4. After a red loop round, the next round runs the previously failing tests first. If they
  still fail, the round ends without running the rest.

**Non-goals**
- Coverage-based or predictive selection (SPEC-0328 phase 3).
- Batched close trains or bisecting (SPEC-0328 phase 2).
- Changing `merge.docs_only_paths` or SPEC-0350 `paths`.
- Mapping a *removed* module's old importers (still a full fallback).
- Reuse for `nw queue merge` campaigns. They have no loop tier, so they always run the close gates.

## Decision

1. **Narrowed mapping in `tools/affected_tests.py`.** It uses a reference scan (string constants
   and import names) next to the import graph. A changed non-Python file under `tests/`,
   `src/nightwright/`, `.nw/` or `tools/`, or a changed `tools/*.py`, selects the test modules that
   reference it, plus the nearest tests of src modules that reference it. A src module that no test
   reaches selects the test files named after it. The configured smoke set (`--include`) is always
   added. A change that maps to nothing still falls back to the full suite. The full-suite triggers
   shrink to `pyproject.toml`, `uv.lock`, `conftest.py`, `tests/waits.py` and removed modules.
2. **Close reuse.** A close gate with `reuse_from: <loop gate name>` is skipped when the rebase is
   clean by rules R1-R7 below. The loop gate's green result then stands for it. Every rule fails
   closed. This is opt-in per gate (the generic default is no reuse), and the escape hatches force
   the full run.
3. **Sentinel.** The queue runner runs the project's close-tier gates on the base HEAD, in a
   dedicated detached worktree, whenever reuses have accumulated (see Design). A red sentinel sets
   `force_full_close` on the project and escalates to the conductor with the list of suspect merges.
4. **Failed-first.** A gate with `failed_first: true` begins a round by running the failing ids
   recorded in the campaign's previous red result for that gate, through the gate's `retry_cmd`.
   This gives pytest `--lf` semantics, driven by the ledger rather than by `.pytest_cache`.
   If that prelude fails, the round is red and stops there. If it passes, the normal selected run
   follows.

## Design

### 1. Narrowed fallbacks (`tools/affected_tests.py`, stdlib only)

**Full triggers** (`FULL_TRIGGERS`) become `pyproject.toml`, `uv.lock`, `tests/waits.py` and any
`conftest.py`. The triggers for removed modules, an unknown base and `MAX_FRACTION` stay.
`tools/` and `.nw/` are no longer prefix triggers. `--strict` (or env `NW_AFFECTED_STRICT=1`)
restores the SPEC-0328 triggers exactly. This is the rollback switch.

**Reference index.** `_scan` already parses every module under `src/nightwright` and `tests`. It
also collects, per module, (a) every `str` `ast.Constant` and (b) every imported top-level name.
For a changed path `rel`, the *needles* are:
- the basename (for example `remote_gates_golden.json`, `project.yaml`, `affected_tests.py`);
- for `tools/X.py`, also the stem `X` (tests load tools with `importlib`/`sys.path` or
  `subprocess [... "tools/X.py"]`, and an import of `X` counts);
- the name of the parent directory, when the file sits below the top level of `tests/`,
  `src/nightwright/` or `.nw/` (for example `data` for `tests/data/*.json`, `personas` for
  `src/nightwright/agentic/personas/*.md`, `.nw` for `.nw/project.yaml`). This catches
  `Path(__file__).parent / "data"` and glob-style readers.

A module *references* `rel` when one of its string constants contains a needle (for a directory
needle, the constant is equal to the needle or ends with `/<needle>` or `<needle>/`), or when the
needle is the stem of a module it imports. A referencing **test** module is selected, and so are the
test modules importing it (the existing free hops through helpers). A referencing **src** module
selects its `nearest_tests` (this covers package data read by src code). Matching on basenames
errs toward selecting more, and `MAX_FRACTION` caps the result.

**Rules by path class** (applied per changed file, results unioned):

| Changed path | Selection | If empty |
|---|---|---|
| `pyproject.toml`, `uv.lock`, `conftest.py`, `tests/waits.py` | full suite | n/a |
| `.nw/**` | reference scan (finds `test_agentic_dogfood_config`, `test_agentic_gate_paths`, …) | full |
| `tests/**` non-`.py` (test data) | reference scan | full |
| `src/nightwright/**` non-`.py` (package data) | reference scan, then nearest tests | full |
| `tools/*.py` and other `tools/**` | reference scan | full |
| `src/nightwright/**.py` | `nearest_tests` (unchanged), then the **name rule** if empty | full |
| `tests/**.py` | unchanged (itself and importers) | n/a |
| anything else (docs, specs, …) | nothing (unchanged) | n/a |

**Name rule** (an unreached src module, for example a new `cli_workers.py`). Take the module's leaf
name, and also the leaf with the prefixes `cli_` / `_` stripped (`cli_workers` gives `cli_workers`
and `workers`). Select the test files under `tests/` whose stem contains either token as a whole
`_`-delimited run (`test_agentic_workers.py`, `test_agentic_workers_bootstrap.py`, but not
`test_coworkers.py`). Also select the test modules whose string constants contain the
module's dotted name, or a CLI command name equal to the token with `_` replaced by `-`. An empty
result is still the full fallback. The reason string says which rule fired:
`affected: N/M test files (name rule for src/.../cli_workers.py; reference scan for .nw/project.yaml)`.

**Why these needles.** They are a grep of real readers: 20 test modules mention `project.yaml`
(about 9 % of 216 test files), and the golden data file is read by exactly its golden test. The
needles reach every test that names the file, and the cap stops a needle that is too broad from
costing more than SPEC-0328's fallback already did.

**Report.** When env `NW_GATE_REPORT` is set, `affected_tests.py` writes a JSON report there:
`{full, reason, tests, include, changed, base_sha, head_sha, head_tree}`. Writing it is best
effort: a write error is ignored.

### 2. Close reuse of a green loop result

**Recording.** `run_gate` sets `NW_GATE_REPORT=<campaign>/gates/<seq:03>-<name>.report.json` for
every gate. A gate result may carry `report` (the parsed JSON, under 64 KiB; anything larger is
dropped). The `gate` ledger payload and `last_gate` gain optional `head` and `tree`: the
worktree's `HEAD` and `HEAD^{tree}` when the run started. The campaign state keeps
`last_green_loop`, a copy of the last loop-tier payload with `ok` true and no `interrupted`.
Any later red loop run clears it.

**Config.** A gate may set `reuse_from: <name>` (validated: the gate is `when: close`, and the name
is a `when: loop` gate of the same config). The project may set `close.force_full: true` (the
escape hatch in the config), and env `NW_CLOSE_FULL=1` does the same for one runner. Both turn
reuse off. The project schema is strict (unknown keys fail), so `project.py` gains two optional
top-level sections, validated like `merge` with defaults in `PROJECT_DEFAULTS`: `close`
(`force_full: false`, `reuse_max_age_s: 86400`, `reuse_max_delta: 200`) and `sentinel`
(`enabled: true`, `after_reuses: 3`, `max_age_s: 86400`).

**Clean rebase, decided in `close._reuse_loop_green` before `G.run_all(..., tier="close")`.** Let
`G` be `last_green_loop.head`, `H` the rebased HEAD, `T` the target sha, `B` =
`changed_files(H vs T)` (the branch's own changes after the rebase) and `D` =
`git diff --name-only G H` (everything that differs between the gated tree and the close tree).
Reuse requires all of the following:

- **R1 enabled.** The gate has `reuse_from`. `close.force_full`, `NW_CLOSE_FULL` and the
  project's sentinel `force_full_close` are all off.
- **R2 green source.** `last_green_loop` exists. Its `reuse_from` gate result is `ok`, has a
  `report`, and that report's `head_sha` equals `G`.
- **R3 branch unchanged.** `D ∩ B = ∅`: every file the branch changes has the same blob in `H` as
  in `G`. So no fix commits came after the green round, and the rebase changed no branch file
  (a conflict resolution or a regenerated file in `B` breaks this).
- **R4 main delta mappable.** No path in `D` is a full trigger (pyproject, lockfile, conftest,
  waits, removed module) and none is a `.nw/**` gate-definition change. The selection for `D`,
  computed on `H`, is not full.
- **R5 selection did not grow.** `sel_H(B)`, the branch's selection recomputed on `H`'s import
  graph, is a subset of the report's `tests`, or the report was full.
- **R6 no interaction.** The forward static closure of `sel_H(B)` (imports plus reference scan) on
  `H` contains no path in `D`. The forward closure of `sel_H(D)` contains no path in `B`. In
  words: the tests that proved the branch read nothing main changed, and the tests main's
  changes affect read nothing the branch changed.
- **R7 bounded age.** `G` was gated less than `close.reuse_max_age_s` ago (default 86400) and
  `D` has at most `close.reuse_max_delta` paths (default 200). A large or old delta is not worth
  the static reasoning.

When every rule holds, the gate is removed from the close run (like a SPEC-0350 skip) and a
`gate.reused` event is appended. Required keys: `gate`, `from_gate`, `from_gate_seq`, `gated_head`,
`close_head`, `target_sha`, `delta_count`, `selection_count`, `rules`. `rules` maps R1-R7 to `true`.
Optional keys are `selection_reason` and `full_source` (whether the source run was full). A reused
required gate counts as satisfied. When a rule fails, `gate.reuse_declined` is appended (required
keys `gate`, `rule`, `detail`) and the gate runs as today. Both events go into
`schemas.EVENT_KEYS`.

**Why this definition.** R3 and R5 make sure the branch's tested content is byte-identical and its
selection has not grown. R6 is the only new trust. It extends to the main-plus-branch combination
the same static reasoning the loop tier already trusts for the branch alone, and it fails closed
whenever the two change sets touch shared test dependencies. The failures that remain possible
come from dynamic coupling, covered under Risks, and the sentinel backs those up. The rule uses
the static graph, so the decision takes seconds, not minutes.

### 3. Sentinel (periodic full suite on base)

**Where it runs.** The queue runner gets a new step, `sentinel`, on a worker thread. It is
scheduled only when no campaign is waiting for a gate slot. It runs `gates_for(cfg, "close")`
with no `paths` skip and no reuse, on a detached worktree at `<agentic>/sentinel/<project>`,
reset to the base HEAD before each run (`worktrees.setup` runs on creation). It takes a normal
host gate slot (SPEC-0309).

**When it runs.** The project file `<agentic>/projects/<project>/gate_reuse.json` holds
`{reuses_since_green, last_green_sha, last_green_at, force_full_close, suspects}`. Every
`gate.reused` increments `reuses_since_green` and appends `{campaign, merge_sha}` to `suspects`.
The sentinel is due when `reuses_since_green >= sentinel.after_reuses` (default 3), or when
`reuses_since_green >= 1` and `last_green_at` is older than `sentinel.max_age_s` (default 86400).
That gives at least one full run per day while reuse is in use, and none when nothing was reused.

**Outcome.** On green: reset the counter and suspects, record `last_green_sha`, and append the
queue event `sentinel` (`ok: true`). On red: set `force_full_close` (all later closes of the
project run full), append `sentinel` (`ok: false`, failing ids, suspects), and file an escalation
to the conductor naming the suspect merges. A green sentinel or `nw queue sentinel clear --project
P` resets the flag. `nw queue sentinel run --project P` runs it on demand.

### 4. Failed-first rounds

`run_gate_with_retries` gets the previous result for the same gate name from
`state["last_gate"]`, passed in by `run_all` for the loop tier. When the gate sets `failed_first`,
the previous result was red, and `_targeted_ids` gives a complete id list, attempt `p` (kind
`failed-first`) runs `targeted_cmd(gate, ...)` with those ids before the normal run:
- prelude red (and not a code in `TARGETED_UNRESOLVED_EXITS`): the gate result is that attempt,
  red. There is no main run and no SPEC-0289 ladder, since the failure is already reproduced.
- prelude green, or inconclusive (ids gone, usage error): the normal run and the ladder follow
  unchanged. The prelude is listed in `attempts`, and its duration counts in `duration_s`.

A pytest gate with `-x` and a fallback selection used to spend up to 24 min on a round whose first
failing test was still broken. Now that round ends in the prelude's seconds.

### Dogfood config (`.nw/project.yaml`)

- `pytest-affected`: `failed_first: true`. The command passes `--include` for the smoke set as now.
- `pytest` (close): `reuse_from: pytest-affected`.
- No `close.force_full`. Sentinel defaults.

## Alternatives considered

- **Re-run only the selection at close instead of reusing.** This still costs one selected run per
  close and gives nothing that R6 does not. Reuse at zero cost is the point.
- **Reuse on equal tree hash only (`H^{tree} == G^{tree}`).** This is never true after a rebase
  onto a moved main, which is the common case. It would almost never fire.
- **Periodic full run inside the next campaign's close.** It would blame and triage an unrelated
  campaign for a regression an earlier reuse let in. A separate sentinel on base keeps the
  attribution right.
- **pytest `--lf` with `.pytest_cache`.** The cache lives in the worktree and is not reliable under
  xdist or after `git clean`. The ledger already holds the failing ids (SPEC-0284).
- **Coverage-based mapping for data files.** It needs an instrumented seeding run, which costs
  what we are trying to save (SPEC-0328 analysis).

## Acceptance criteria

- [ ] `affected_tests.py` selects a targeted, non-full set for a `.nw/project.yaml` change, a
      `tests/data/*.json` change, a `tools/*.py` change, a package-data change under
      `src/nightwright/`, and a src module reached by no test but matched by the name rule. Each
      reason names the rule that fired.
- [ ] `pyproject.toml`, `uv.lock`, `conftest.py`, `tests/waits.py`, a removed module, an unmapped
      data file, an unmatched unreached module, and a selection above `MAX_FRACTION` still give the
      full suite. `--strict` / `NW_AFFECTED_STRICT=1` reproduce SPEC-0328 selection exactly.
- [ ] The smoke set (`--include`) is added to every non-full selection.
- [ ] `affected_tests.py` writes the JSON report to `$NW_GATE_REPORT`. Gate results carry it, and
      `gate` payloads carry `head`/`tree`.
- [ ] A close gate with `reuse_from` is skipped and a `gate.reused` event with all required keys is
      recorded only when R1-R7 hold. Each failing rule appends `gate.reuse_declined` naming that
      rule, and the gate runs.
- [ ] `close.force_full`, `NW_CLOSE_FULL=1` and a red sentinel each force the full close run.
- [ ] `reuse_from` is validated (it names a loop gate and sits on a close gate).
- [ ] The sentinel runs on a detached base worktree when due by reuse count or age, never while a
      campaign waits for a slot. Red sets `force_full_close` and escalates with suspects. Green
      clears the counter. `nw queue sentinel run|clear` work.
- [ ] With `failed_first: true`, a round after a red round runs the previous failing ids first. A
      reproduced failure ends the round red without the main run, and a passing or inconclusive
      prelude falls through to the normal run.
- [ ] The dogfood `.nw/project.yaml` sets `failed_first` and `reuse_from`.
      `docs/product/capabilities/agentic.md` documents `reuse_from`, `failed_first`,
      `close.force_full` and the sentinel.

## Test plan

- **Automated: mapping rules** (`tests/test_affected_tests.py`, existing temp-git-repo fixture,
  extended with `.nw/project.yaml`, `tests/data/golden.json`, `tools/tool_x.py`,
  `src/nightwright/pkg/data/x.md` and a `cli_widgets.py` hub module):
  - one test per row of the rules table: expected selected files, `full` false, and the reason
    names the rule;
  - the needle forms: a basename constant, `Path(...)/"data"`, a `tools` stem import, a subprocess
    path, a dotted module name and a CLI command name;
  - the empty-mapping fallbacks (an unreferenced data file, a name-rule miss) give the full suite;
  - `--strict` and the env give the SPEC-0328 results; the smoke set is always appended; the
    report JSON has its keys.
- **Automated: replay** (`tests/test_affected_tests_replay.py`, fixture
  `tests/data/affected_fallback_replay.json`). This file holds the recorded 2026-11-01 fallbacks:
  `.nw/project.yaml` (SPEC-0338), `tests/data/remote_gates_golden.json` (SPEC-0338), and a new
  `src/nightwright/agentic/cli_workers.py` (SPEC-0354), plus controls `pyproject.toml` and
  `uv.lock`. Run against the **real repository tree**, with missing files staged as temporary
  copies, the three recorded reasons must now give a non-full selection under `MAX_FRACTION`
  that includes `test_agentic_dogfood_config.py`, the golden test and a `*workers*` test
  respectively. The controls stay full.
- **Automated: reuse** (`tests/test_agentic_close_reuse.py`, close fixtures as in
  `test_agentic_close.py` / `test_agentic_gate_paths.py`): a clean rebase onto unrelated main
  commits gives `gate.reused` with every key, and the pytest gate does not run. Then one test per
  rule for the decline: a reuse flag that is off or forced (R1), no or stale green (R2), a fix
  commit after green or a conflict resolution in a branch file (R3), a main delta with
  `pyproject.toml` or `.nw/project.yaml` (R4), a grown selection (R5), main changing a module a
  branch test imports, or a test reading a branch data file (R6), old or large (R7). Also a
  schema test for both events, and `reuse_from` validation in `test_agentic_project.py`.
- **Automated: sentinel** (`tests/test_agentic_sentinel.py`): due and not-due by count and age; no
  scheduling while a slot waiter exists; green resets; red sets `force_full_close`, escalates and
  makes the next close decline with R1; the CLI `run`/`clear`.
- **Automated: failed-first** (`tests/test_agentic_gates.py`, using the fake gate commands of the
  SPEC-0289 ladder tests): a reproduced prelude gives a red result with one attempt and no main
  run; a green prelude runs the main command; an exit code 4/5 prelude falls through; no
  prelude without the flag, without a prior red, or with an incomplete id list.
- **Manual verification:** (a) `git checkout <SPEC-0338 branch tip>` in a scratch worktree, run
  `python3 tools/affected_tests.py --base <its base>` and record the selection size against the
  old full fallback in *As shipped*; the same for the SPEC-0354 tip. (b) Run one dogfood code
  campaign through the queue and record close-gate wall time, and whether a `gate.reused` or
  `gate.reuse_declined` event appeared and with which rule. (c) `nw queue sentinel run --project
  Meta-Tools` on main: green, with the state file reset.
- **Regression guard:** `python3 tools/affected_tests.py --run uv run pytest -q` plus
  `test_agentic_close*`, `test_agentic_gate*`, `test_agentic_queue*`, `test_agentic_project`,
  `test_agentic_dogfood_config`; `python3 tools/spec_lint.py` exits 0; the full suite runs at
  the queue's close gate (this change edits `.nw/project.yaml`, so R4 declines reuse for itself).

## Risks

- **Dynamic coupling the static graph misses** (a plugin registry, `importlib` by computed name,
  env or config read through a helper, test ordering or shared global state). A reused close can
  merge a regression. *Mitigation:* R6 fails closed on any shared static dependency. The sentinel
  runs the full suite at most 3 reuses or 24 h later and names the suspect merges. A red sentinel
  turns reuse off project-wide until main is green.
- **Basename needles too narrow** (a file opened by a computed name that contains no needle). A
  data change can then select a set that misses its reader. *Mitigation:* the parent-directory
  needle catches directory-level readers. The loop tier was already static-only, and a
  close-tier full run still happens whenever reuse declines or is off. Misses show up as sentinel
  reds, which are a signal to add a rule.
- **Name rule misses** (an unreached module tested only through the CLI under another name). The
  close tier or the sentinel catches it. A new module with no tests is a test-coverage finding in
  any case.
- **Flaky tests** let a reused green hide a flake that the full run would have hit. This is
  accepted: SPEC-0293 already tracks flakes, and the sentinel retries through the normal ladder.
- **State loss** (`gate_reuse.json` deleted) loses the counter. It is rebuilt as zero with
  `last_green_at` unknown, which makes the sentinel due as soon as one reuse happens (it fails
  toward running).

## Clock Log

CLOCK-IN: n/a
CLOCK-OUT: n/a

## Rollout / migration

1. Ship the mapping (default **on**; `--strict` / `NW_AFFECTED_STRICT=1` is the rollback). It only
   affects the loop tier, and the close tier still runs the full suite.
2. Ship the report/`head`/`tree` recording, `failed_first` (default **off**: absent means no
   prelude), reuse (default **off**: no `reuse_from` means no reuse) and the sentinel. The
   sentinel is idle until the first reuse.
3. In the same change, turn on `failed_first` and `reuse_from` in the dogfood `.nw/project.yaml`.
   Campaigns already in flight pick it up at their next GATING or close, because main's config is
   read. Their first close declines with R2, since earlier loop runs have no report.
4. Watch `gate.reused` / `gate.reuse_declined` counts and sentinel results for a week. Tune
   `after_reuses` and `reuse_max_delta` from evidence and record the result in *As shipped*.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; affected tests pass; manual steps performed and recorded.
- [ ] No regressions (queue close gate full suite).
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated.

## Open questions

None.
