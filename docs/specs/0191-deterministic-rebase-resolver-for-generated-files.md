---
id: SPEC-0191
title: "Deterministic rebase resolver for generated files in the close step"
status: done
owner: user
created: 2026-10-25
updated: 2026-10-25
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0169
tags: [agentic, close, git]
source_idea: IDEA-043
depends_on: [SPEC-0175]
---

## Context

Child of epic [SPEC-0169](./0169-mini-agentic-workflow-queue-campaigns-headless.md)
(promoted from `IDEA-043`).

Every campaign on this repo regenerates `docs/LEDGER.md`, a block generated from spec
frontmatter. With two queue slots, the second campaign to close rebases onto a base that already
carries the first one's ledger and hits a conflict on that file. The close step (SPEC-0175)
parks it as `rebase_conflict`, and resuming repeats the same conflict, so a human must resolve a
file whose correct content is a pure function of the rest of the tree. Found by the SPEC-0175
builder on 2026-10-25. A persona-based general resolver is a separate idea (IDEA-034); this spec
is the deterministic, no-model case.

## Goals / Non-goals

**Goals**
- A project declares generated files and the command that regenerates each
  (`merge.regenerate: [{path, cmd}]` in `.wt/project.yaml`).
- When a rebase conflicts only on declared files, the close step resolves them by regenerating,
  continues the rebase, and proceeds. Any other conflict parks exactly as before.
- The auto-resolution is visible in the ledger.

**Non-goals**
- Resolving non-generated conflicts (IDEA-034).
- Trusting the regeneration: the gates still re-run on the rebased tree.

## Decision

`gitops.rebase` takes an optional `regenerate` map (path to shell command). On each conflicted
step whose conflicted paths are all in the map, it takes one side of each file, runs the
file's command in the worktree, stages the result and continues the rebase, looping per
commit. Anything else (undeclared path, failing command, step bound exceeded) aborts the rebase
and restores HEAD as today. `close_campaign` passes the project's `merge.regenerate` and records
an `info` finding naming the regenerated paths. Step 5 (re-gate) still follows the rebase.

## Design

- **Config** (`project.py`): `merge.regenerate`, default `[]`; a list of mappings with exactly
  the keys `path` and `cmd`, both non-empty strings; unknown keys and other shapes are rejected
  with the key path (`merge.regenerate[i].cmd: ...`). This repo declares
  `{path: docs/LEDGER.md, cmd: "python3 tools/spec_lint.py --write-ledger"}`.
- **gitops** (`rebase(worktree, onto, *, regenerate=None)`): after a failed `git rebase`, list
  unmerged paths. If non-empty and all in `regenerate` (and fewer than 50 steps done): for each,
  `git checkout --theirs -- <path>` (content is about to be replaced), run `sh -c <cmd>` in the
  worktree (timeout 300 s, nonzero exit means failure), `git add -- <path>`; then
  `GIT_EDITOR=true git rebase --continue`; repeat while the rebase stops on conflicts. On any
  failure fall through to `git rebase --abort`, check HEAD equals the pre-rebase HEAD, and
  return `ok=False` with the conflicts of the step that failed. `RebaseResult` gains
  `regenerated: tuple[str, ...] = ()`, the paths auto-resolved (deduplicated, in order).
- **close** (`close_campaign` step 4): `regen = {r["path"]: r["cmd"] for r in merge.regenerate}`;
  when `rb.regenerated` is non-empty, append `finding(persona close, severity info, gate rebase,
  title "generated files regenerated during rebase", detail "regenerated: <paths>")`. Order is
  unchanged: rebase, then re-run all gates, then merge preconditions.

## Alternatives considered

- **Merge driver / `.gitattributes`** - rejected; it needs per-clone git config and cannot run
  project commands in the campaign worktree.
- **Name `merge.regenerate_on_conflict`** (the idea's wording) - shortened to `regenerate`.
- **Let a persona resolve** - rejected here; non-deterministic and slow for a mechanical case.

## Acceptance criteria

- [x] `merge.regenerate` validates (list of `{path, cmd}`, non-empty strings, unknown keys
      rejected) and defaults to `[]`; this repo's `.wt/project.yaml` declares the ledger.
- [x] `rebase(..., regenerate=...)` with a conflict only in declared files succeeds, returns
      `regenerated` listing them, and the file equals the command's output.
- [x] A conflict that includes an undeclared file, or a failing command, aborts the rebase with
      HEAD and tree restored and `ok=False`; without `regenerate` behavior is unchanged.
- [x] `close_campaign` resolves a ledger-style conflict, records the `info` finding, re-runs the
      gates and merges.
- [x] Full `uv run pytest` green; `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests:** `tests/test_agentic_gitops.py` (regenerated conflict, no-regenerate
  abort, mixed conflict abort and restore, failing command abort); `tests/test_agentic_close.py`
  (ledger-style conflict merges with gate re-run and info finding; undeclared conflict still
  parks); `tests/test_agentic_project.py` (default and validation of the new key).
- **Manual verification:** none beyond the suite (the next two-slot run exercises it).
- **Regression guard:** full `uv run pytest`.

### Test record (2026-10-25)

New tests in the three files above pass; full suite: 1579 passed, 1 skipped; spec_lint exits 0.

## Rollout / migration

Additive; projects without `merge.regenerate` behave as before. The runner must be restarted to
pick up the new close code.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`).
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).
