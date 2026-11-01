---
id: SPEC-0347
title: Queue backlog resolves outbound `depends_on` (PROJ-NNNN files, merged-campaign fallback)
status: done
owner: user
created: 2026-11-01
updated: 2026-11-01
kind: feature
tags: [agentic, queue, outbound]
source_idea: IDEA-597
---

## Context

IDEA-597: `COMFORTASSISTANT-0002` (campaign `COMFORTASSISTANT-0002-20261031-9005`, project
comfort-assistant) has `depends_on: [COMFORTASSISTANT-0003]`. `nw queue list` reports
`blocked_by: ["COMFORTASSISTANT-0003 not found"]` though the target repo holds
`docs/specs/COMFORTASSISTANT-0003-mode-aware-unit-drift-and-heating-overshoot-stop.md`.

Cause: `backlog_readiness` (and `_dangling`, the `queue add` preview) resolve a dependency with
`specs._find_spec`, which strips the prefix and globs `NNNN-*.md`. Exported outbound specs are
filed as `PROJ-NNNN-slug.md` (plus a `PROJ-NNNN-slug.contract.md` sidecar), so the glob never
matches.

A second gap shows once the file is found: the readiness check reads the dependency's status
from the project's base checkout (`repo.root`). An outbound campaign closes by opening a PR
(`HANDED_OFF`); the sweep moves it to `MERGED` when the PR merges, but nothing fast-forwards the
human's local checkout — comfort-assistant's local `main` still shows `COMFORTASSISTANT-0001` as
`accepted` although its campaign is `MERGED` and `origin/main` has it `done`.

## Goals / Non-goals

**Goals**
- An outbound dependency id resolves to its `PROJ-NNNN-*.md` file in the target repo (the
  `.contract.md` sidecar never counts as the spec).
- The blocker clears only when the dependency is actually done: `done`/`superseded` in the base
  checkout, **or** its newest campaign in the project is `MERGED`. `HANDED_OFF` (PR open) stays
  blocked, with a reason that says so.

**Non-goals**
- Fetching / fast-forwarding the target repo's checkout.
- Superseded-chain walking for outbound ids (`dependency_met` keeps scanning `[0-9]*.md`).

## Decision

Add `queue._find_dep_spec(specs_dir, dep)`: exact `<dep>-*.md` (non-`.contract.md`) first, then
the existing `_find_spec` number glob. Use it in `backlog_readiness` and `_dangling`. For an
outbound dependency that is not met by its file, consult the dependency's newest campaign in the
same project: `MERGED` → met; `HANDED_OFF` → reason `waiting on X (<status>; PR open, campaign
<id> handed off)`. A `MERGED` campaign is also met when the file is not found (base checkout
predates the export).

## Design

- `queue.py` `_find_dep_spec(specs_dir, dep) -> Path` (raises ValueError like `_find_spec`).
- `queue.py` `_dep_campaign(cfg, dep, project) -> dict | None`: newest campaign (terminal
  included) for the outbound `dep` in `project`; `None` for internal ids, or when `cfg` has no
  `data_dir` (unit callers pass `{}`).
- `backlog_readiness`: missing file → met if `_dep_campaign` is `MERGED`, else `X not found`.
  Unmet file status → met if `MERGED`; if `HANDED_OFF`, the reason names the open PR.
- `_dangling`: file via `_find_dep_spec`; a dependency whose campaign is `HANDED_OFF`/`MERGED`
  is not dangling (it resolves on merge).
- Only outbound dependency ids consult campaigns: internal campaigns fast-forward the local base
  on merge, so the file is authoritative there and behaviour is unchanged.

## Alternatives considered

- **Fix `_find_spec` globally** — many callers rely on its number-glob for internal ids;
  a queue-local helper keeps the blast radius to dependency resolution.
- **Read status from `origin/<base>`** — needs a fetch per readiness check (called every tick)
  and git plumbing; the campaign state already records the merge.
- **Treat `HANDED_OFF` as done** — the code is not on the base yet; a dependent built on top
  would miss it. Rejected per the idea.

## Acceptance criteria

- [x] An outbound spec whose `depends_on` names a `PROJ-NNNN` spec filed as `PROJ-NNNN-slug.md`
      (with a `.contract.md` sidecar) is no longer reported `not found`; its status is read from
      the spec file, not the sidecar.
- [x] With the dependency `accepted` in the base checkout and its campaign `HANDED_OFF`, the
      campaign stays blocked: `waiting on PROJ-NNNN (accepted; PR open, campaign <id> handed off)`.
- [x] With the dependency's campaign `MERGED` (base checkout still `accepted`), the campaign is
      ready. With the file `done`, ready regardless of campaigns.
- [x] Internal `SPEC-NNNN` dependency behaviour is unchanged (existing tests pass).
- [x] `_dangling` finds `PROJ-NNNN-*.md` files and does not flag a handed-off dependency.

## Test plan

- **Automated tests:** `tests/test_agentic_queue.py::test_backlog_readiness_outbound_dependency`
  — a tmp target repo with `ACME-0002` (depends on `ACME-0003`), `ACME-0003-x.md` +
  `ACME-0003-x.contract.md`; cfg with a `data_dir`; asserts not-found is gone, `HANDED_OFF`
  blocks with the PR reason, `MERGED` admits, file `done` admits, unknown id still `not found`.
  `tests/test_agentic_queue_cli.py::test_dangling_outbound_prefixed_file_and_handed_off_dep`
  — `_dangling` reads the prefixed file and skips a handed-off dep.
- **Manual verification:** `Q.backlog_readiness(cfg, <state of COMFORTASSISTANT-0002-20261031-9005>)`
  from the worktree (`uv run python`, read-only, live config) returns COMFORTASSISTANT-0002 `blocked_by: ["waiting on COMFORTASSISTANT-0003 (accepted; PR
  open, campaign COMFORTASSISTANT-0003-20261031-07cf handed off)"]`.
- **Regression guard:** `python3 tools/affected_tests.py --run uv run pytest -q`.

## Rollout / migration

None: readiness is recomputed every tick; the next tick rewrites the campaign's `backlog`.

## As shipped

- `src/nightwright/agentic/queue.py`: `_find_dep_spec`, `_dep_campaign`; `backlog_readiness`
  and `_dangling` use them as designed. The `.contract.md` sidecar sorts before the spec in a
  plain `PROJ-NNNN-*.md` glob, hence the explicit exclusion.
- Tests: `test_backlog_readiness_outbound_dependency` (also checks a `MERGED` campaign of the
  same id in another project does not count, and that a `MERGED` campaign covers a dependency
  the base checkout lacks), `test_dangling_outbound_prefixed_file_and_handed_off_dep`.
- Manual (2026-11-01, live data, read-only): COMFORTASSISTANT-0002 →
  `(False, ['waiting on COMFORTASSISTANT-0003 (accepted; PR open, campaign
  COMFORTASSISTANT-0003-20261031-07cf handed off)'])`. Once PR #4 merges the sweep moves 07cf to
  `MERGED` and the next tick admits 0002 even though the local comfort-assistant `main` lags.
- Affected tests (`tools/affected_tests.py --run uv run pytest -q`): 2301 passed, 2 skipped; the worktree venv needed `uv sync --all-extras` (textual) for the
  TUI tests to collect.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; affected tests pass; manual step performed.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` regenerated; `spec_lint` exits 0.
