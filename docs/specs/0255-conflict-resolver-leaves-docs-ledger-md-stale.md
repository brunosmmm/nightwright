---
id: SPEC-0255
title: "Close regenerates declared generated files after a rebase and commits them before re-gating"
status: done
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

## Goals / Non-goals

**Goals:** a rebased close never gates or merges a stale declared generated file; the regenerated
file is committed so gated head == merged head.
**Non-goals:** new `project.yaml` keys; changing how `gitops.rebase` resolves conflicts; changing
the resolver persona or prompt; dropping the `--write-ledger` step from the spec-lint gate;
repairing stale ledgers already on main outside a close.

## Decision

In `close.prepare_close` (src/wt/agentic/close.py), right after a successful rebase (step 4, after
the `rb.regenerated` finding) and before `head = git.rev_parse(worktree)`, run every
`merge.regenerate` command in the worktree and, if any declared path changed, commit exactly those
paths with the message `wt(<campaign id>): regenerate generated files after rebase` (the existing
`wt(<id>): ...` convention of the close-time snapshot; record a `commit` event and an
info `finding`). This is unconditional on whether a resolver ran (cheap, idempotent, and the only
sound place); a command failure is a close park `regenerate_failed` with the output tail, resume
to CLOSING. `regenerate_failed` is added to `queue.PARK_REASONS` (queue.py:62) with the default
conductor owner (`PARK_OWNERS` unchanged; it is a tooling failure, not a human decision); resume
goes to CLOSING. After the commit the helper checks the worktree is clean (ignoring the gate's own
output); if the regenerate commands left undeclared files modified or untracked that were not
already dirty before they ran, close parks `regenerate_failed` naming them rather than gating a
dirty tree. For merge-request campaigns (`own_worktree` false) the new commit is wt-generated, not
the requester's work, so SPEC-0233's "close never commits the requester's work" is not violated.
Docs-only merges: option (a) - paths declared in `merge.regenerate` are excluded from
`docs_only_violations` (in both `close.py:412` and `queue.py:3692` callers), because
`docs/LEDGER.md` is not in `merge.docs_only_paths` and a stale-ledger repair is exactly the case
that would otherwise park `not_docs_only` for a requester who touched only allowlisted docs. Scope is only paths declared in `merge.regenerate`; nothing else is added.

## Design

`close.py`: new helper `_regenerate_after_rebase(worktree, regen, events) -> str | None` using
a new small public `gitops.run_regenerate(worktree, cmd)` (extracted from the code `gitops.rebase`
already uses, shared by both; close.py does not call private `git._run`), then `git.git(worktree,"add","--",*paths)` and a
commit only when `diff --cached --quiet` is non-zero (not `commit_all`, which would add the whole
tree). The helper runs before the docs-only branch, with the exclusion above. `head`
is read after it so the gated and merged heads stay equal. No project.yaml change.

## Alternatives considered

- **Only run after a resolver** - misses any other stale-generated case; running always is
  idempotent.
- **Rely on the spec-lint gate** - it does not commit, as above.
- **Resolver prompt tells it to regenerate** - the persona verdict is not trusted (SPEC-0221).

## Acceptance criteria

- [x] After a successful rebase with a stale declared generated file, close commits the
      regenerated file before capturing `head` and gating.
- [x] The merged tree equals the gated tree and contains the regenerated file.
- [x] No commit when nothing changed; undeclared dirty paths are never committed.
- [x] A failing regenerate command parks `regenerate_failed` without merging; the reason is in
      `PARK_REASONS` (conductor-owned) and resume goes to CLOSING.
- [x] A regenerate command that dirties an undeclared file parks `regenerate_failed`.
- [x] A `--docs-only` merge whose only extra change is a regenerated declared path does not park
      `not_docs_only`.
- [x] The commit emits a `commit` event and an info `finding` in `PreparedClose.events`.

## Test plan

Automated (`tests/test_agentic_close_regenerate.py`, git fixtures from `test_agentic_close.py`):
- Campaign branch whose rebased tree has a stale generated file (regenerate cmd rewrites it from a
  source file): `prepare_close` returns a head one commit past the rebase, `git show HEAD` touches
  only the declared path, and an unrelated untracked file is not committed.
- Already-fresh file: head unchanged, no extra commit.
- Stale-file test also asserts the `commit` event and info `finding` in `PreparedClose.events` and
  the `wt(<id>): ...` message.
- Regenerate cmd exiting 1: outcome parks `regenerate_failed`; `park`/`resume` accept the reason.
- Regenerate cmd that also writes an undeclared file: parks `regenerate_failed`.
- Docs-only close with a stale `docs/LEDGER.md`: regenerated, committed, no `not_docs_only` park.
- Merge-request (adopted branch, `own_worktree` false) close: the regenerate commit is added and
  the requester's own commits are untouched.
- A resolver-style rebase (conflict resolved by the fake resolver, then re-close) ends with the
  regenerated file in the merged tree.
- **Sabotage check:** remove the helper call; the stale-file test must fail.
Regression: `tests/test_agentic_close.py`, merge tests, full `uv run pytest`.

### Test record (2026-10-26)

`uv run pytest -q tests/test_agentic_close_regenerate.py` (10 passed: stale-file commit + `commit`
event + info `finding` + message, merged==gated tree, fresh file no commit, undeclared dirt not
committed, failing command and undeclared-file park `regenerate_failed`, docs-only close, adopted
merge-request branch, resolver-style rebase, `run_regenerate`); `tests/test_agentic_queue_merge.py`
(park resume to CLOSING; `plan_merge --docs-only` ignores declared regenerate paths). Sabotage check:
neutralising the helper call failed 7 of the 10 new tests. Full `uv run pytest -q -x` and
`python3 tools/spec_lint.py` passed.

Review-loop hardening: four more tests (adopted worktree keeps a requester's file/edit on a failing
or stray-writing command; a missing declared path parks and the resume snapshots nothing; a refused
docs-only merge never runs the commands); `tests/test_agentic_close*.py`, `test_agentic_queue_merge.py`
and the full `uv run pytest -q -x` pass.

Loop-2 review fix: `test_docs_only_close_accepts_a_branch_that_itself_carries_a_stale_declared_path`
covers the close-side docs-only exclusion with a branch that carries a stale `docs/LEDGER.md`;
removing `+ list(regen)` from close.py fails it. `tests/test_agentic_close_regenerate.py` 15 passed.

### As shipped

- `gitops.run_regenerate(worktree, cmd)` is the shared public helper (`gitops.rebase` uses it too).
- On a `regenerate_failed` park the worktree is put back to HEAD (`reset --hard` + `clean -fd`) when
  it was clean before the commands ran, so a resume does not snapshot or park on the leftovers.
  Only for a wt-owned worktree: an adopted (merge-request) worktree is the requester's, so nothing
  is reset or deleted there (our staging is undone; the resume parks `worktree_dirty`).
- The `git add` / `git commit` steps are inside the same error handling as the commands, so a
  declared path that is missing or ignored, or a rejecting hook, parks `regenerate_failed` (tree put
  back) instead of erroring every tick and letting the close-time snapshot commit the leftovers.
- The `--docs-only` allowlist check now runs before the regenerate commands, so a refused merge
  never runs branch-modified scripts. Not done: excluding wt's own regenerate commit from
  `commits_since_request` on retries.
- `docs/product/loop/operate.md` gained the `regenerate_failed` row (a docs test requires every
  park reason there).

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`).
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` regenerated.
