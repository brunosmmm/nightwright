---
id: SPEC-0587
title: SPEC-0583 review follow-ups - own-ref exclusion no longer hides real collisions, full-ref own names, test gaps
status: accepted
owner: user
created: 2026-11-12
updated: 2026-11-12
kind: feature
source_idea: IDEA-1339
depends_on: [SPEC-0583]
tags: [spec-numbers, review-followup]
---

## Context

[SPEC-0583](./0583-nwfug-idea-1074-20261112-7400-spec-0569-review.md) made `collisions()` in
`src/nightwright/spec_numbers.py` stop reporting a slug rename on the tree's own branch. It did so
by building `own = {worktree:<tree>} | _own_refs(tree)` and dropping every location in `own`
from the filename comparison (`spec_numbers.py:323`, `:337`). `_own_refs` returns the tree's
local branch and its `@{upstream}` as `git rev-parse --abbrev-ref` short names (`:299`). The
review of that change (IDEA-1325, filed as IDEA-1339) left six findings:

1. **Upstream hides a real remote collision** (major). The local branch is the tree's own state,
   so ignoring it is sound. The upstream is not: it is the shared remote ref that another clone
   pushes to. Another clone pushing `0001-r.md` to `origin/main` while this tree holds
   `0001-x.md` is a real collision, and it is dropped because `origin/main` is "own".
2. **A duplicate inside the tree's own tree is hidden** (minor). `worktree:<tree>` and the own
   branch are both in `own`. If the tree holds `0005-a.md` and `0005-b.md`, or a committed file
   plus a newly added second file with the same number, each file's other filename sits only in
   `own` locations and is dropped.
3. **Own-ref exclusion is untested beyond the primary checkout** (minor). The rename test runs
   only on the primary checkout with `main` and `origin/main`. Detached HEAD (where
   `rev-parse --abbrev-ref HEAD` prints `HEAD`), a linked worktree, and `nw/<id>` branches are
   never exercised.
4. **Read-only test misses pruning** (minor). `test_collisions_is_read_only` covers an absent
   `data_dir` and a held lock, but not a store holding a record `_prune` would drop (number
   committed on a ref, or abandoned campaign). A regression that saved the pruned store from
   `collisions()` would pass it.
5. **Release path scan count unchecked** (minor). `test_prune_without_records_runs_no_ls_tree`
   checks `list_reservations` with a live record (1 `for-each-ref`, 1 `ls-tree`) but never
   `release` with a live record.
6. **`--abbrev-ref` names can differ from the scan's location names** (minor). `git rev-parse
   --abbrev-ref` returns a disambiguated name such as `heads/main` when the short name is
   ambiguous (a tag or remote with the same name). The scan's location is
   `ref.split("/", 2)[2]` of the full refname (`:146`), which is `main`. The own-ref exclusion then
   silently never matches and the rename is reported again.

## Goals / Non-goals

**Goals**
- Report a different filename for the same number that the upstream carries when that filename is
  new since the tree's history diverged from it, while still ignoring the tree's own unpushed
  rename.
- Report two filenames with the same number inside the tree's own tree (working tree and own
  branch), while still ignoring the tree's own rename.
- Derive own-ref names from full refnames, with the same short-name rule the scan uses, so the
  exclusion cannot miss on an ambiguous short name.
- Close the three test gaps (own-ref shapes, read-only with a prunable store, release scan count).

**Non-goals**
- Changing the store schema, allocation order, `nw spec reserve|release|check-numbers`, the JSON
  shape `nw.spec.collisions.v1`, or `number_taken`.
- Ignoring more refs: other local branches and other remotes with the same number under another
  slug stay collisions.
- Resolving two refs that share one short name (a local branch literally named `origin/main`);
  that ambiguity is in the scan's location names themselves and is out of scope.
- Treating a detached-HEAD tree as owning any ref (see Decision).

## Decision

Keep the own-branch exclusion but narrow it to what is provably the tree's own state:

- The tree's own **local branch** location is always ignored for other filenames (a rename there is
  the tree's own change).
- The tree's own **worktree** location is no longer ignored: another filename with the same number
  that is present in the tree's own tree is a real duplicate and is reported against
  `worktree:<tree>`. A filename that exists only on the own branch (the old name of a renamed or
  deleted file) is not present in the tree and is ignored.
- The tree's **upstream** is ignored only for filenames that already existed at
  `git merge-base HEAD @{upstream}`, the shared history that the tree's own commits renamed.
  A filename that appeared on the upstream after that point comes from elsewhere and is reported.
- Own-ref names come from full refnames (`git symbolic-ref -q HEAD`,
  `git rev-parse --symbolic-full-name @{upstream}`), mapped to the scan's short location name by
  one shared helper.
- A detached HEAD has no own branch and no upstream, so every ref is compared; this is the
  documented SPEC-0569 behaviour and is now pinned by a test.

## Design

**Short-name helper.** Extract `_short_ref(ref)` returning `ref.split("/", 2)[2]` in
`spec_numbers.py` and use it in `_number_files` (`:146`) and in `_own_refs`, so the scan and the
own-ref names cannot diverge.

**`_own_refs(tree)`** returns `(branch, upstream)`, each a short location name or `None`:
- `branch`: `git -C tree symbolic-ref -q HEAD` (exit 1 when detached gives `None`), mapped with
  `_short_ref` when it starts with `refs/heads/`.
- `upstream`: `git -C tree rev-parse --symbolic-full-name @{upstream}` (fails without an
  upstream gives `None`), mapped with `_short_ref` when it starts with `refs/remotes/` or
  `refs/heads/`.
No `--abbrev-ref` anywhere. `_tree_campaign` keeps its own `rev-parse --abbrev-ref HEAD` call
unchanged: it only tests the `nw/` prefix of a branch the tree is on, where a `heads/` prefix
would be a separate pre-existing question outside this spec.

**`collisions()` (`spec_numbers.py:310`).**
- Compute `branch, upstream = _own_refs(tree)` and, only when `upstream` is set, `base_names`: the
  filenames under `rel_dir` at `git -C tree merge-base HEAD @{upstream}` (via the existing
  `_ref_files(top, rel_dir, sha, cache)` so the per-commit memo is shared; empty set when there is
  no merge-base).
- For each file `p` and each other filename `f` of its number, filter `f`'s locations: drop
  `branch`; drop `upstream` when `f in base_names`; keep everything else, including
  `worktree:<tree>`. A filename with no locations left is not reported.
- Reported shape, sort order and `reservation` handling are unchanged.
- Git cost: `merge-base` adds one subprocess (plus at most one `ls-tree` for a commit the scan has
  not already listed) only when an upstream is set.

**Tests** (all in the existing `tests/test_spec_numbers.py`, which keeps its one
`pytestmark = pytest.mark.fast`; no new module and no second tier marker):
- `test_collisions_upstream_other_filename_is_a_collision`: a clone pushes `0001-r.md` to
  `origin/main`, fetch, tree holds `0001-x.md` with `main` tracking `origin/main`; expect one
  `filename` collision with `other == ["origin/main"]` and `other_files == ["0001-r.md"]`.
  Counter-cases in the same test or a sibling: tree renamed `0001-x.md` uncommitted (upstream
  holds the old name only) gives `[]`; tree committed the rename and is ahead of the unpushed
  upstream gives `[]`; upstream renamed by another clone (`0001-y.md`) while the tree is behind
  gives a collision.
- `test_collisions_duplicate_number_in_own_tree_is_reported`: `0005-a.md` and `0005-b.md` both in
  the tree, committed: both files collide, `other` includes `worktree:<realpath>`; committed
  `0005-a.md` plus an untracked `0005-b.md` also collides; `git mv 0005-a.md 0005-c.md` (old name
  only on the branch) gives `[]`.
- `test_own_refs_shapes`: detached HEAD gives `(None, None)` and the tree's uncommitted rename is
  reported against every ref (`main` included); a linked worktree on `nw/<id>` with an upstream
  returns `("nw/<id>", "origin/nw/<id>")` and its uncommitted rename is not a collision; a branch
  without an upstream returns `(branch, None)`; a primary checkout on `main` with
  `origin/main` as before.
- `test_own_refs_ambiguous_short_name`: create tag `main` (`git tag main`) so `rev-parse
  --abbrev-ref HEAD` prints `heads/main`; assert `_own_refs` returns `("main", ...)` and that the
  uncommitted rename on `main` is still not a collision.
- Extend `test_collisions_is_read_only`: seed the store with a record whose number is committed on
  `main` and a record whose campaign is `ABANDONED` (via `store`), keep the lock held in a second
  fd, call `collisions()`, assert the store bytes and mtime are unchanged and `data` still holds
  only the lock file and the store.
- Extend `test_prune_without_records_runs_no_ls_tree`: after `allocate`, count git calls around
  `release(cfg, rec["number"])`: exactly one `for-each-ref` and one `ls-tree`, and the record is
  gone from the store.

## Alternatives considered

- **Keep ignoring the upstream entirely** - leaves the major finding; the upstream is where a
  concurrent clone's number appears.
- **Never ignore the upstream** - reports the tree's own unpushed rename as a collision, which
  SPEC-0583 explicitly removed.
- **Ignore the upstream only when it is an ancestor of HEAD** - misses the common diverged case
  where the tree renamed a file and another clone pushed an unrelated commit; the merge-base name
  set covers both.
- **Keep `--abbrev-ref` and add `--no-abbrev` fallbacks** - still two sources for the same name;
  one shared `_short_ref` removes the divergence.
- **Treat a detached HEAD's commit as the own state** - needs commit-to-ref matching with no
  stable rule; campaign worktrees are always on `nw/<id>` branches.

## Acceptance criteria

- [ ] With `main` tracking `origin/main`, a different filename with the tree's number that another clone pushed to `origin/main` is reported as a `filename` collision with `other == ["origin/main"]`.
- [ ] The tree's own uncommitted rename, and its own committed-but-unpushed rename, are not reported against `main` or `origin/main`.
- [ ] A filename that appeared on the upstream after the merge-base is reported even when the tree is also ahead of the upstream.
- [ ] Two same-number filenames in the tree's own tree (both committed, or committed plus untracked) are each reported with `worktree:<tree>` among the locations; a rename leaves no report.
- [ ] `_own_refs` derives its names from full refnames through the shared `_short_ref`; with a tag named `main`, the own branch is `main` and the rename on `main` is not a collision.
- [ ] A detached-HEAD tree owns no ref (`_own_refs == (None, None)`) and compares every ref; a branch without an upstream returns `(branch, None)`; a linked worktree on `nw/<id>` with an upstream is handled.
- [ ] `collisions()` on a store holding a record committed on a ref and a record for an ABANDONED campaign, while another process holds the lock, leaves the store file's bytes and mtime unchanged and creates no other file in `data_dir`.
- [ ] `release()` of a live record runs exactly one `for-each-ref` and one `ls-tree` and removes the record.
- [ ] The `check-numbers` CLI output and the `nw.spec.collisions.v1` JSON shape are unchanged; all other tests in `tests/test_spec_numbers.py`, `tests/test_agentic_spec_writer.py` and `tests/test_specs*.py` pass without edits.

## Test plan

- **Automated tests** (extend `tests/test_spec_numbers.py`; it already carries its single module-level `pytestmark = pytest.mark.fast`, so no new test module and no second tier marker is added; helpers `_make_repo`, `_with_origin`, `_clone_push`, `_commit_on`, `_count_git` are reused):
  - `test_collisions_upstream_other_filename_is_a_collision` - AC 1-3.
  - `test_collisions_duplicate_number_in_own_tree_is_reported` - AC 4.
  - `test_own_refs_shapes` and `test_own_refs_ambiguous_short_name` - AC 5-6 (detached HEAD, linked worktree on `nw/<id>` with an upstream, no-upstream branch, ambiguous short name via `git tag main`).
  - Extended `test_collisions_is_read_only` - AC 7.
  - Extended `test_prune_without_records_runs_no_ls_tree` - AC 8.
  - Existing `test_collisions_own_branch_slug_rename_is_not_a_collision` stays as is and must keep passing (AC 2, 9).
  - Run `python3 tools/affected_tests.py --run uv run pytest -q` plus `tests/test_spec_numbers.py` and `tests/test_agentic_spec_writer.py`.
- **Tier check:** when `tools/test_tiers.py` exists, `python3 tools/test_tiers.py --check` exits 0.
- **Manual verification:** in a scratch repo with a bare `origin`, set `main` to track `origin/main`, push `docs/specs/0001-r.md` from a second clone, fetch, and run `nw spec check-numbers` in the first clone holding `0001-x.md`: it exits 1 naming `origin/main`. Rename `0001-x.md` there without a push and with no other change on the remote: `nw spec check-numbers` prints `no spec-number collisions`.
- **Regression guard:** `python3 tools/spec_lint.py` exits 0; the SPEC-0569 and SPEC-0583 collision tests pass unchanged.

## Rollout / migration

No data migration; the store schema is unchanged. Single change in `spec_numbers.py` plus tests; the only user-visible effect is that `nw spec check-numbers` and the queue's collision check now report cases they previously hid.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (affected tests, per AGENTS.md); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

None.
