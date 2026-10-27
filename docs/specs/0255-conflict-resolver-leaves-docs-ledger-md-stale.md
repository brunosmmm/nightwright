---
id: SPEC-0255
title: "Close regenerates declared generated files after a rebase and commits them before re-gating"
status: accepted
owner: user
created: 2026-10-26
updated: 2026-10-26
kind: feature
source_idea: IDEA-306
tags: [agentic, close, bug]
---

## Context

IDEA-306. After a `conflict_resolver` run (SPEC-0221) resolves a rebase, `docs/LEDGER.md` can be
stale: the resolver merged text, not the generated block. `.wt/project.yaml` already has
`merge.regenerate` (SPEC-0191), but `gitops.rebase(regenerate=...)` only uses it for conflicts
made solely of declared paths. The gates now run `python3 tools/spec_lint.py --write-ledger`
first, which hides the staleness from the gate, but it rewrites the working tree only:
`close.prepare_close` takes `head = git.rev_parse(worktree)` before the gate, `finish_close`
requires the merged tree to equal that gated head, and nothing commits the regenerated file. So
a stale `docs/LEDGER.md` can still land on main and the next campaign's pytest sees drift.

## Decision

In `close.prepare_close` (src/wt/agentic/close.py), right after a successful rebase (step 4, after
the `rb.regenerated` finding) and before `head = git.rev_parse(worktree)`, run every
`merge.regenerate` command in the worktree and, if any declared path changed, commit exactly those
paths with the message `regenerate generated files after rebase` (record a `commit` event and an
info `finding`). This is unconditional on whether a resolver ran (cheap, idempotent, and the only
sound place); a command failure is a close park `regenerate_failed` with the output tail, resume
to CLOSING. Scope is only paths declared in `merge.regenerate`; nothing else is added.

## Design

`close.py`: new helper `_regenerate_after_rebase(worktree, regen, events) -> str | None` using
`git._run(["sh","-c",cmd])` like `gitops.rebase`, then `git.git(worktree,"add","--",*paths)` and a
commit only when `diff --cached --quiet` is non-zero (not `commit_all`, which would add the whole
tree). The docs-only merge path is covered too since the helper runs before the branch. `head`
is read after it so the gated and merged heads stay equal. No project.yaml change.

## Alternatives considered

- **Only run after a resolver** - misses any other stale-generated case; running always is
  idempotent.
- **Rely on the spec-lint gate** - it does not commit, as above.
- **Resolver prompt tells it to regenerate** - the persona verdict is not trusted (SPEC-0221).

## Acceptance criteria

- [ ] After a successful rebase with a stale declared generated file, close commits the
      regenerated file before capturing `head` and gating.
- [ ] The merged tree equals the gated tree and contains the regenerated file.
- [ ] No commit when nothing changed; undeclared dirty paths are never committed.
- [ ] A failing regenerate command parks `regenerate_failed` without merging.

## Test plan

Automated (`tests/test_agentic_close_regenerate.py`, git fixtures from `test_agentic_close.py`):
- Campaign branch whose rebased tree has a stale generated file (regenerate cmd rewrites it from a
  source file): `prepare_close` returns a head one commit past the rebase, `git show HEAD` touches
  only the declared path, and an unrelated untracked file is not committed.
- Already-fresh file: head unchanged, no extra commit.
- Regenerate cmd exiting 1: outcome parks `regenerate_failed`.
- A resolver-style rebase (conflict resolved by the fake resolver, then re-close) ends with the
  regenerated file in the merged tree.
- **Sabotage check:** remove the helper call; the stale-file test must fail.
Regression: `tests/test_agentic_close.py`, merge tests, full `uv run pytest`.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`).
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` regenerated.
