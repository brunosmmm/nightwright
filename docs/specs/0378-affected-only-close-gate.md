---
id: SPEC-0378
title: "Affected-only close gate: the close tier runs the affected selection, with narrowed full-suite fallbacks"
status: done
owner: user
created: 2026-11-02
updated: 2026-11-02
milestone: "M9: agentic workflow"
kind: feature
tags: [agentic, gates, close, tests]
depends_on: [SPEC-0328, SPEC-0357, SPEC-0359]
---

## Context

nightwright's own close-tier `pytest` gate (`.nw/project.yaml`) ran `uv run pytest -q -x -n 4`,
the full suite (about 5800 tests, about 28 minutes on the gate box), whenever SPEC-0359's
`reuse_from: pytest-affected` declined. Reuse declines often: a dirty rebase, a sentinel that is
due, red or overdue, a `.nw/**` delta, a reuse-tooling change, an R5/R6 closure hit. Then every
campaign close pays the full suite again. The human wants minimal testing everywhere: the queue
must never run the full suite per campaign. The loop tier already runs
`tools/affected_tests.py` (SPEC-0328, SPEC-0357), but its full-suite fallbacks fire on plenty of
ordinary changes.

The last 200 first-parent commits on main were replayed through the SPEC-0357 selector, using
today's import graph and the per-commit diff. 36 of them would have selected the full suite:

- 29 because of the 40% fraction cap;
- 4 because of `tests/conftest.py`;
- 3 because of `pyproject.toml`.

The other 164 selected a median of 2 test files (p90 39) out of 230.

This spec changes one decision of SPEC-0328: "the close tier runs the full suite". It also
narrows SPEC-0357's default-mode fallbacks. Neither spec is superseded: their selection model,
the `--strict` mode, the report and the loop gate all stand, and this spec extends them.

## Goals / Non-goals

**Goals**
- A campaign close runs only the tests its branch affects. The selection is computed by
  `tools/affected_tests.py` against `merge-base($NW_BASE, HEAD)`.
- Every remaining full-suite fallback is as narrow as is safe, and each one is documented.
- The retry and targeted-rerun path (SPEC-0289) keeps working, and so do the `paths` filter
  (SPEC-0350) and `reuse_from` (SPEC-0359).
- The full suite still runs, but only as the sentinel on main. The sentinel forces it through
  the environment, and [SPEC-0379](./0379-time-based-sentinel-on-worker-slots.md) adds its clock
  trigger.

**Non-goals**
- Coverage-based selection.
- Changing the loop gate's command.
- Changing `--strict`. `--strict` keeps the SPEC-0328 rules and the 40% cap.

## Decision

- **The close gate's command.** The close `pytest` gate runs
  `python3 tools/affected_tests.py --include tests/test_agentic_dogfood_config.py --include tests/test_agentic_project.py --run uv run pytest -q -x -n 4`.
  - `retry_cmd: uv run pytest -q -n 0 {failing}` and `retries: 2` stay. A failing run reruns its
    failing ids, then reruns the selection once.
  - `paths`, `reuse_from`, `timeout_s` and `required` stay.
- **Default mode drops the fraction cap.** A large selection runs as itself. Only `--strict`, or
  an explicit `--max-fraction`, still caps.
- **Narrowed fallbacks.** In default (non-strict) mode, these changes no longer select the full
  suite:
  - a nested conftest;
  - `tests/waits.py`;
  - a metadata-only `pyproject.toml` change;
  - a `uv.lock` change that only bumps the project's own version;
  - a removed module;
  - a src module reached only through a hub;
  - a file outside the mapped roots.
- **Forcing the full suite.** `affected_tests.py` selects the full suite when one of these is set:
  - `NW_SENTINEL` is non-empty (the local sentinel run);
  - `NW_CAMPAIGN` starts with `sentinel-` (the worker sentinel run; a worker forwards only
    `NW_CAMPAIGN` / `NW_SPEC_ID` / `NW_BASE`);
  - `NW_AFFECTED_FULL` is true.
- **`close.force_full`, `NW_CLOSE_FULL` and a red sentinel's `force_full_close` only turn reuse
  off.** The close gate then runs its own command, the affected selection. They no longer mean
  "the full suite". A red main is already escalated and rerun by the sentinel. Making every close
  run the full suite until it is fixed is exactly the per-campaign full suite this spec removes.

## Design

### `tools/affected_tests.py` (default mode)

Each fallback is listed with what it does now.

| Change | Before (SPEC-0357) | Now (SPEC-0378) | Why it is safe |
|---|---|---|---|
| Root `tests/conftest.py` | full | **full (kept)** | Its fixtures reach every test. |
| Nested `tests/<dir>/conftest.py` | full | The test modules under `tests/<dir>/` | A conftest applies only below its directory. |
| `tests/waits.py` | full | Its importers, like any tests helper module | It is an ordinary module; 42 of 230 test files import it. |
| `pyproject.toml` | full | Nothing, when the parsed diff against the merge base touches only `[project]` metadata keys (`SAFE_PROJECT_KEYS`: version, description, readme, authors, maintainers, keywords, classifiers, urls, license, license-files) or `[tool.<x>]` tables in `SAFE_TOOL_KEYS` (ruff, mypy, black, isort, coverage, codespell). Otherwise **full**. | Dependencies, scripts, entry points, build-system, `[tool.pytest]`, `[tool.uv]` and unknown tables can change any test, so they stay full. |
| `uv.lock` | full | Nothing, when the only difference is the own package's `version` (read from `[project].name`). Otherwise **full**. | Any other lock change moves a dependency. |
| `pyproject.toml` / `uv.lock` with no base (`--changed`), added, removed or unparsable | full | **full (kept)** | Nothing can be compared. |
| Removed `src`/`tests`/`tools` module | full | For every module whose text still names the removed leaf as a word, the tests that reach it: a test referrer gives its nearest tests, a tools referrer gives the tests that reference that tool, a src referrer gives `_reaching` (nearest tests, then the name rule, then through the hubs). Nothing names it: nothing. A src referrer that reaches no test: **full**. | A stale import or string fails in the modules that still name the removed module, and those modules are selected. |
| Src module reached by no test except through a CLI hub | full | The tests reaching it through the hubs | They are the tests that exercise it. Before, the hubs were never walked, so this fell back to the full suite. |
| Src module no test reaches, even through the hubs, and no name-rule match | full | **full (kept)** | Untested code: nothing narrower is known. |
| Package-data file whose reader no test reaches | full | **full (kept)** | As above. |
| `.nw/**`, `tools/**`, or data under `tests/` / `src/nightwright/` that no test references | full | **full (kept)** | These are consumed at runtime by paths the static graph cannot see. |
| Any other file (AGENTS.md, README.md, `skills/**`, `docs/**`) | nothing | The test modules that reference it (the SPEC-0357 reference scan, test referrers only); otherwise nothing | Tests that read such a file (contract, doc and skill checks) still run. Prose that no test reads selects nothing, as before. |
| More than 40% of test files selected | full | The selection | It is still a subset. The cap was the largest source of full runs (29 of 36). |
| Base cannot be resolved | full | **full (kept)** | Nothing can be diffed. |
| Sentinel or `NW_AFFECTED_FULL` environment | (none) | **full** | The sentinel runs at the base itself, where the diff is empty. |

Sampled selections on today's tree (`--changed`, out of 230 test files):

| Change | Test files selected |
|---|---|
| AGENTS.md | 4 |
| README.md | 12 |
| docs/WORKFLOW.md | 7 |
| skills/nw-orient/SKILL.md | 32 |
| tests/waits.py | 42 |
| src/nightwright/agentic/queue.py | 92 |
| docs/specs/0003-*.md | 98 (common needles; still not the full suite) |

`select(repo, changed, max_fraction=None, depth=0, strict=False, base=None)` takes `base`, the
merge-base sha, so that it can read the old `pyproject.toml` / `uv.lock`. `main()` passes it on
the git path. `--changed` has no base, so a config file given there is full. `forced_full()` is
checked before anything else.

### Remaining full fallbacks

1. The root conftest.
2. A dependency, build or pytest change in `pyproject.toml` / `uv.lock`, or a no-base, added,
   removed or unparsable config file.
3. A removed module named by a src module that no test reaches.
4. A src module, or a package-data reader, that no test reaches even through the hubs.
5. An unreferenced `.nw/**`, `tools/**`, or data file under `tests/` / `src/`.
6. An unresolvable base.
7. The sentinel environment.

In the 200-commit replay, cases 1 and 2 account for at most 7 of the 200 commits.

### `.nw/project.yaml`

- The close `pytest` gate runs the command given under Decision.
- `timeout_s` stays 2400, because a remaining full fallback and the sentinel still need it.
- The comments name SPEC-0378 and SPEC-0379.

### Docs

- The AGENTS.md DoD item says: the affected tests at the close gate, the full suite only as the
  sentinel on main.
- `docs/product/capabilities/agentic.md` describes both specs.

## Alternatives considered

- **Keep the full close gate and only make reuse decline less.** Rejected: every decline would
  still cost 28 minutes, and the human asked that the queue never run the full suite per
  campaign.
- **Run `--strict` selection at close.** Rejected: strict falls back on more files and keeps the
  cap. That is the full suite most of the time.
- **Pass a `NW_SENTINEL` env key to the worker.** Rejected: the worker env allowlist and old
  workers' `validate_header` refuse unknown keys. A job campaign id that starts with `sentinel-`
  needs no worker change. Campaign ids look like `PROJECT-NNNN-date-hex` or `IDEA-...`, so they
  never start with lowercase `sentinel-`.
- **A red sentinel forces a full selection on every close.** Rejected: that brings back the
  per-campaign full suite while main is red. The red sentinel already escalates and reruns the
  full suite on every base move.

## Acceptance criteria

- [x] The close `pytest` gate runs `tools/affected_tests.py ... --run uv run pytest -q -x -n 4`.
  `paths`, `reuse_from`, `retry_cmd {failing}` and `retries: 2` are unchanged.
- [x] Default mode has no fraction cap. `--strict` keeps `MAX_FRACTION`, and an explicit
  `max_fraction` still caps.
- [x] A nested conftest selects the tests below it.
- [x] `tests/waits.py` selects its importers.
- [x] A metadata or lint-only `pyproject.toml` change selects nothing. A dependency,
  `[tool.pytest]`, build-system or unparsable change is full.
- [x] A `uv.lock` change that only bumps the own version selects nothing. A dependency move is
  full.
- [x] A removed module selects the tests of what still names it. Nothing naming it selects
  nothing. An untested src referrer is full.
- [x] A hub-only module selects the tests through the hub.
- [x] An unmapped root file selects the tests that reference it.
- [x] `NW_SENTINEL`, `NW_CAMPAIGN=sentinel-*` and `NW_AFFECTED_FULL=1` force the full suite. A
  campaign's `NW_CAMPAIGN` does not.
- [x] `--strict` behaviour is unchanged.
- [x] The AGENTS.md DoD wording matches the new behaviour.

## Test plan

- `tests/test_affected_tests.py`:
  - Updated: the old fallback tests (hub-only, removed module, removed tool, unmappable,
    full-report cases) now assert the narrowed selections. Strict mode still asserts full.
  - New: no cap by default; nested conftest; `waits.py` importers; a parametrized pyproject
    metadata/deps/pytest/build-system/unparsable table; uv.lock own-version vs dependency; no
    base is full; a removed module nothing names; a removed module named by an untested src
    module is full; the unmapped root-file reference scan; forced-full and not-forced
    environments through the CLI.
  - The file's autouse fixture clears `NW_SENTINEL` / `NW_CAMPAIGN` / `NW_AFFECTED_FULL` /
    `NW_AFFECTED_STRICT` / `NW_BASE`, so that the sentinel running this file is not forced full.
- `tests/test_agentic_dogfood_config.py`: the close gate's command is the affected selection,
  and the retry knobs stay.
- `tests/test_affected_tests_replay.py` is unchanged and passes. It calls the selector with an
  explicit cap.
- Run: `python3 tools/affected_tests.py --run uv run pytest -q` and the touched files. Not the
  full suite (this spec's point; the queue's sentinel runs it).

## Rollout / migration

The change takes effect when it is merged to main: gate commands come from main's
`.nw/project.yaml`. A worktree branched before this spec still has `tools/affected_tests.py`
(SPEC-0328), and the sentinel-forcing env check is in the merged tool, which the sentinel runs at
main HEAD.

## Definition of done

- [x] Acceptance criteria true, and the test plan executed (see *As shipped*).
- [x] Ledger regenerated; `spec_lint` exits 0.

## As shipped

- Executed (after rebasing onto 2109611d): `tests/test_affected_tests.py` (113),
  `tests/test_affected_tests_replay.py`, `tests/test_agentic_dogfood_config.py`,
  `tests/test_agentic_sentinel.py`, `tests/test_agentic_close_reuse.py`: 215 passed; and
  `tests/test_remote_gates.py`: 115 passed. The branch's own affected selection (121 of 230
  files) was started but stopped on the human's request to expedite; the queue's close gate
  runs it.
- Not verified here: a real queue close running the new command. The live queue was not touched;
  the gate takes effect on merge.
- The reference scan for files outside the mapped roots matches basenames, so a common basename
  (for example `SKILL.md`) or a spec number selects broadly: 32 to 98 files. That is still a
  subset; tighten the needles if it shows up in gate times.

## Open questions

None.
