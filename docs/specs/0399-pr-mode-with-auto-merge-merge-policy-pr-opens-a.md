---
id: SPEC-0399
title: "Implicit docs/LEDGER.md regeneration on every nw rebase (first slice of PR auto-merge)"
status: accepted
owner: user
created: 2026-11-02
updated: 2026-11-02
milestone: "M9: agentic workflow"
kind: feature
tags: [agentic, close, git, pr]
source_idea: IDEA-647
depends_on: [SPEC-0191, SPEC-0255]
---

## Context

IDEA-647 asks for a PR auto-merge lane under `merge.policy: pr` and for `docs/LEDGER.md`
conflicts to become structurally impossible. On comfort-assistant on 2026-11-01, PRs #3-#12 were
open at once; each merge moved the base and invalidated the siblings, and six manual rebase +
force-push rounds were needed. Most of the conflicts were in `docs/LEDGER.md`, a file whose
correct content is a pure function of the rest of the tree.

The review of the first draft of this spec found it covered three independent subsystems (the
ledger fix, the auto-merge lane, the sibling/tick scheduling) with 13 acceptance criteria, and
that the lane force-pushes and runs `gh pr merge` against real repos, so it must be split. This
spec is the **first split**: the ledger half, which has no dependency on PR mode, removes the
dominant conflict source for every merge policy, and is the prerequisite of the lane.

What exists today:

- `close.prepare_close` (`src/nightwright/agentic/close.py`, section 4 "rebase") builds
  `regen = {r["path"]: r["cmd"] for r in (mcfg.get("regenerate") or [])}` from the project's
  `merge.regenerate` and passes it to `gitops.rebase(worktree, target_sha, regenerate=regen)`
  (SPEC-0191), then to `_regenerate_after_rebase` (SPEC-0255). It is the only caller of
  `gitops.rebase`, and it serves all three policies (`merge`, `pr`, `draft-pr`).
- `gitops.rebase` resolves a conflict only when **every** conflicted path of a step is declared
  in `regen`: it checks the path out from the replayed commit, runs the command, stages and
  continues. Any other conflict aborts and `prepare_close` parks `rebase_conflict`.
- `merge.regenerate` is opt-in per project. nightwright's own `.nw/project.yaml` declares
  `docs/LEDGER.md -> python3 tools/spec_lint.py --write-ledger`; a project that forgot to declare
  it (comfort-assistant) still conflicts on it.
- `tools/spec_lint.py --write-ledger` rewrites only the block between the
  `<!-- specs:begin -->` / `<!-- specs:end -->` markers of `docs/LEDGER.md` from spec frontmatter
  (`docs/specs/SCHEMA.md`, "The ledger is generated"); it exits 1 when the markers are missing.

## Goals / Non-goals

**Goals**
- A conflict on `docs/LEDGER.md` never parks a nw rebase: the ledger is regenerated from the
  rebased tree, never textually merged, whether or not the project declared it in
  `merge.regenerate`.
- The implicit behaviour is additive: it never overrides a project's explicit entry, never
  applies to a project that has no ledger generator, and can only turn a `rebase_conflict` park
  into a resolved rebase (or add a regenerate commit that SPEC-0255 would already make for a
  declared entry).

**Non-goals**
- The PR auto-merge lane itself (`merge.pr.auto_merge`, `close.pr_auto`, `nw queue pr-merge`,
  `gh_pr_status` / `gh_pr_merge` / `push_branch(force_with_lease=)`, the rebase / gate /
  force-push-with-lease / wait / `gh pr merge` / `MERGED` loop). That is **split 2**, a separate
  spec that depends on this one. When it is written it must carry these review requirements:
  - a lane step "sync" that fetches the branch and parks `pr_head_moved` unless the local tip
    equals the recorded `pr_head`; `--resume` adopts the current remote head as `pr_head`
    (recorded as an info finding) and the next tick hard-resets the worktree branch to it before
    rebasing, so a human's fix is rebased on, never force-pushed away (test: the human commit is
    an ancestor of the pushed head);
  - a merge-gate on `mergeStateStatus` (`CLEAN`/`HAS_HOOKS` only; `BLOCKED`/`UNSTABLE`/`UNKNOWN`
    -> waiting; `BEHIND`/`DIRTY` -> rebase) and no merge on `checks: none` right after a push
    (tests: BLOCKED waits, none-immediately-after-push makes no merge call);
  - a table mapping every `prepare_close` outcome to a named `pr_auto` park reason with the
    campaign staying `HANDED_OFF` (spec_guard / spec_dod / gates -> `gates_failed`;
    `rebase_conflict`, `regenerate_failed`, `open_failed`, `worktree_dirty` -> same reason), and
    `prepare_close(snapshot=False)` so a dirty PR worktree parks instead of being snapshotted;
  - `gh pr merge` run with cwd outside any repository and without `--delete-branch`, the remote
    branch deleted by `git push <remote> --delete` and the local one by `_cleanup`;
  - a source scan that allows `launch.py`'s existing deny strings (`_FORCE_FORMS`, `_PUSH_DENY`)
    and asserts `"pr", "merge"` argv construction and `git push ... --force-with-lease` occur
    only in `gitops.py`, plus a check that `DEFAULTS["disallowed_tools"]` still contains
    `Bash(gh pr merge *)` and every `_PUSH_DENY` force form.
- Sibling refresh after a merge, per-(project, base) lane locks, the `agentic.pr_merge`
  interval / budget tick hook, the gc keep rule for active lane worktrees, the three-PR real-`gh`
  verification on a scratch repo and enabling the lane on comfort-assistant. That is **split 3**,
  depending on split 2.
- Changing `merge.regenerate` semantics, the SPEC-0191 `gitops.rebase` algorithm, or the
  SPEC-0221 conflict resolver. The spec writer / accept regenerate paths
  (`queue.py` calls of `SW.regenerate` at the SPEC-0215 / SPEC-0214 steps) keep reading only the
  *declared* `merge.regenerate`: they are not rebases.
- A custom git merge driver or merging the ledger by row id (see Alternatives).

## Decision

nw gets a built-in **implicit `merge.regenerate` entry** for the ledger: `docs/LEDGER.md ->
python3 tools/spec_lint.py --write-ledger`. A new helper `effective_regenerate(merge_cfg,
worktree)` in `close.py` returns the project's declared mapping plus that entry when the
worktree has a generator for it and the project did not declare the path itself. `prepare_close`
uses it in place of the inline `regen` mapping, so every rebase nw performs (close under any
merge policy) resolves ledger conflicts by regeneration and `_regenerate_after_rebase` keeps the
gated head equal to the merged head. Regenerating was chosen over merging by row id because the
block is already generated, so regeneration is simpler and the only result that can never be
wrong.

## Design

### `effective_regenerate(merge_cfg, worktree) -> dict[str, str]`

In `src/nightwright/agentic/close.py`, next to `_regenerate_after_rebase`:

- Start from `{r["path"]: r["cmd"] for r in (merge_cfg.get("regenerate") or [])}` (exactly the
  current expression).
- Constants `IMPLICIT_LEDGER_PATH = "docs/LEDGER.md"` and
  `IMPLICIT_LEDGER_CMD = "python3 tools/spec_lint.py --write-ledger"`.
- Add `IMPLICIT_LEDGER_PATH -> IMPLICIT_LEDGER_CMD` only when **all** hold:
  1. no declared `path` equals `docs/LEDGER.md` after normalising (`posixpath.normpath`, leading
     `./` stripped), so a declared entry, including its command, always wins and is never
     duplicated;
  2. `<worktree>/tools/spec_lint.py` is a file;
  3. `<worktree>/docs/LEDGER.md` is a file containing both `<!-- specs:begin -->` and
     `<!-- specs:end -->` (without them `--write-ledger` exits 1, which would turn a clean
     rebase into a `regenerate_failed` park).
- It is evaluated once per `prepare_close`, on the campaign branch's checkout before the rebase.
  A branch that predates `tools/spec_lint.py` or the ledger markers therefore gets no implicit
  entry and behaves as today (a ledger conflict parks `rebase_conflict`). This is accepted: such a
  branch has no generated ledger to regenerate.

### Wiring in `prepare_close`

`close.py` section 4 replaces the inline `regen = {...}` with `regen = effective_regenerate(mcfg,
worktree)`. That one mapping already feeds, unchanged: `git.rebase(..., regenerate=regen)`, the
`--docs-only` allowlist (`list(mcfg.get("docs_only_paths")) + list(regen)`, SPEC-0255) and
`_regenerate_after_rebase`. Consequences, all intended and the same as for a declared entry:

- A ledger-only conflict is resolved during the rebase and reported by the existing info finding
  "generated files regenerated during rebase".
- After every rebase the command runs once more; if the ledger is stale it is committed as
  "regenerate generated files after rebase" (so a branch whose author forgot
  `--write-ledger` is corrected before gating). An up-to-date ledger yields no commit.
- For `--docs-only` merges, `docs/LEDGER.md` joins the allowlist when the implicit entry applies,
  because it is regenerated after the allowlist check, exactly as a declared path is.
- A failing command still parks `regenerate_failed` and a ledger conflict that comes with a
  non-generated conflict still parks `rebase_conflict` listing every conflicted path (the
  SPEC-0191 all-paths-declared rule is unchanged).

No config key is added: the implicit entry cannot be turned off, but any explicit
`merge.regenerate` entry for the path replaces it. No change to `project.py`.

## Alternatives considered

- **Merge `docs/LEDGER.md` by row id with a custom git merge driver** -- drivers are local git
  config (absent on GitHub's side and on other clones) and the block is already derived data;
  regenerating is simpler and always correct.
- **Make the ledger not committed (generate at read time)** -- changes the ledger contract that
  `spec_lint`, pytest and every project's CI rely on; far larger than the need.
- **Require every project to declare the ledger in `merge.regenerate`** -- the status quo; it is
  exactly what comfort-assistant missed.
- **Apply the implicit entry only when a conflict occurs** -- would need a second code path in
  `gitops.rebase`; running the command after every rebase is the existing SPEC-0255 behaviour and
  also corrects stale ledgers.
- **Ship the whole PR auto-merge idea in one spec** -- rejected by review: three independent
  subsystems and a real-repo force-push / merge surface; split into the three specs named in
  Non-goals.

## Acceptance criteria

- [ ] `effective_regenerate` returns the declared mapping unchanged when `tools/spec_lint.py` or
      the ledger markers are absent, and adds `docs/LEDGER.md -> python3 tools/spec_lint.py
      --write-ledger` when `tools/spec_lint.py` exists and `docs/LEDGER.md` has both markers and
      is not declared.
- [ ] A declared entry for the ledger path, spelled `docs/LEDGER.md` or `./docs/LEDGER.md`, keeps
      its own command, and no second entry is added.
- [ ] A `merge.policy: merge` close whose rebase conflicts only on `docs/LEDGER.md`, with an empty
      `merge.regenerate`, rebases, regenerates the ledger, records the "generated files
      regenerated during rebase" finding and does not park.
- [ ] The same ledger-only conflict resolves through `prepare_close` for `merge.policy: pr` and
      `draft-pr` (target `<remote>/<base>`).
- [ ] With no `tools/spec_lint.py` in the worktree, or a ledger without markers, a ledger
      conflict still parks `rebase_conflict` listing `docs/LEDGER.md`.
- [ ] A conflict on `docs/LEDGER.md` plus a code file parks `rebase_conflict` listing both paths
      and leaves the worktree at its pre-rebase HEAD.
- [ ] A rebase without conflicts on a branch with a stale ledger gets one "regenerate generated
      files after rebase" commit; with an up-to-date ledger no extra commit is made.
- [ ] A failing ledger command (stub generator exiting 1) parks `regenerate_failed` and restores
      the worktree.
- [ ] The ledger produced by the resolution passes the real `python3 tools/spec_lint.py`
      (exit 0); the spec writer / accept regenerate calls still read only the declared
      `merge.regenerate`.
- [ ] `python3 tools/spec_lint.py` exits 0 for this repo.

## Test plan

- **Automated tests:** in `tests/test_agentic_close_regenerate.py` (reuses the SPEC-0255
  fixtures imported from `tests/test_agentic_close.py`; the `tests/test_agentic_close.py`
  policy fixtures `_merge_world` / `world` supply the `pr` and `merge` worlds):
  - `effective_regenerate` unit cases on a tmp worktree: no spec_lint -> unchanged; no markers ->
    unchanged; both present -> implicit entry added; declared `docs/LEDGER.md` and
    `./docs/LEDGER.md` -> declared command wins, one entry; other declared entries are kept
    alongside the implicit one.
  - Close cases with real `git` and an empty `merge.regenerate`, using a stub
    `tools/spec_lint.py` that deterministically rewrites the marker block from the files in
    `docs/specs/`: ledger-only conflict under `merge`, `pr` and `draft-pr` resolves with the
    finding; ledger + code conflict parks with both paths and HEAD restored; no spec_lint -> still
    `rebase_conflict`; stale ledger without a conflict -> one regenerate commit, up-to-date -> none;
    stub exiting 1 -> `regenerate_failed` and clean worktree; `--docs-only` allowlist includes the
    ledger when the implicit entry applies and not otherwise.
  - One case with the repository's real `tools/spec_lint.py` (plus the minimal `docs/specs`
    tree it needs, copied into the fixture repo): two branches each add a spec, rebase one onto
    the other, assert `python3 tools/spec_lint.py` exits 0 in the result.
  - Guard that `SW.regenerate` call sites in `queue.py` still pass only the declared
    `merge.regenerate` (existing spec-writer tests stay green unchanged).
- **Manual verification (real tools, required before `done`):** in a scratch clone of this
  repository whose `.nw/project.yaml` has `merge.policy: merge` and **no** `merge.regenerate`,
  create two branches that each add a different spec (each therefore conflicts on
  `docs/LEDGER.md`), merge the first into the base, then run `uv run nw queue merge <second
  branch> --yes`. Expected: the second branch merges with no `rebase_conflict` park, the ledger
  finding is present in the campaign ledger, and `python3 tools/spec_lint.py` exits 0 on the
  base. Note the commands and the result in an "As shipped" note.
- **Regression guard:** `python3 tools/affected_tests.py --run uv run pytest -q` plus
  `tests/test_agentic_close.py`, `tests/test_agentic_close_regenerate.py`,
  `tests/test_agentic_queue_merge.py` and the SPEC-0191 / SPEC-0255 tests unchanged and green; a
  `merge.policy: merge` close on a project that declares the ledger behaves exactly as before.

## Rollout / migration

1. Land `effective_regenerate` and the `prepare_close` wiring with the tests. No config change
   and no data migration; projects that already declare the ledger are unaffected, projects
   without `tools/spec_lint.py` are unaffected.
2. Run the manual verification on a scratch clone.
3. Write split 2 (PR auto-merge lane core) and split 3 (sibling refresh, multi-lane scheduling,
   tick hook), each depending on this spec, carrying the requirements listed under Non-goals.
   IDEA-647 stays open until those land.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; affected tests pass; the manual run is performed and noted.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

- None. The marker requirement and the "evaluate on the pre-rebase branch checkout" rule are
  decided above; revisit only if a real project's branch lacks `tools/spec_lint.py` at close
  time often enough to matter.
