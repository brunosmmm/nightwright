---
id: SPEC-0549
title: main-dirty-apply saves non-trivial dirt to a WIP branch, restores main, and holds
status: accepted
owner: user
created: 2026-11-11
updated: 2026-11-11
kind: feature
tags: [agentic, queue, git]
depends_on: [SPEC-0548]
supersedes: [SPEC-0543]
source_idea: IDEA-1159
---

## Context

[SPEC-0548](./0548-main-dirty-apply-core-commit.md) ships `nw queue main-dirty-apply` with its
guards and the trivial `commit`; its save actions refuse with `save not supported yet`. This
spec adds the write the human asked for in resume note seq 437 (campaign
`IDEA-1159-20261110-02ed`): "save non-trivial diffs to a WIP branch before restoring". It is the
second half of [SPEC-0543](./0543-main-dirty-apply-audited-verb.md), split per its spec review
(verdict `split`). It replaces part of [SPEC-0537](./0537-conductor-must-clear-main-dirty-itself-a-stray.md)
(human chose "split / narrow" at ESC-0067).

The chain, in queue order: [SPEC-0546](./0546-main-dirty-plan-classifier.md),
[SPEC-0547](./0547-main-dirty-conductor-owned-plan-and-escalate.md),
[SPEC-0548](./0548-main-dirty-apply-core-commit.md), **SPEC-0549 (this spec)**,
[SPEC-0550](./0550-conductor-git-guard-hook.md),
[SPEC-0551](./0551-conductor-runs-main-dirty-apply-trivial-only.md).

SPEC-0537 stalled at 85 runs on exactly this write half. Review majors on its branch: symlinks
written through, `wip_*` dropped from a failed `hold`, WIP content not checked, restore before
save. The failure-phase table and the ordering criteria below pin each. Run by the human or a
steward only; the conductor stays denied (SPEC-0548) and never runs a save (SPEC-0551).

**Carries SPEC-0543 AC4 (AC1; plus SPEC-0543 AC8's malformed-`worktrees` case), AC5 (AC2), AC6
(AC3), AC7 (AC4), AC9 (AC5), AC10 (AC6), AC13 (AC7).** It folds in the SPEC-0543 review's save
findings: guard 7 rejects a `project:` that is not a single path component, rows (a)–(c) remove
the worktree before deleting the branch, AC1 lists three distinct mode fixtures, and
`recommended` uses the shared refusal predicate for the save actions.

## Decision

Implement `save_branch`, `branch_and_escalate` and `hold` in `apply_clear`, replacing SPEC-0548's
`save not supported yet` refusal. Each saves the tracked dirt to a `wip/main-dirty-*` branch in
a worktree under the project's worktree root, verifies it from the WIP commit, and only then
restores main path by path; `hold` then switches to the base when tracked-clean. Guard 7 (base
and WIP location) joins `verb_refusal`. `recommended` names the verb for the save actions.

## Design

### Guard 7, base and WIP location

For actions that save or switch (`save_branch`, `branch_and_escalate`, `hold`), after
SPEC-0548's guards 1–6: `base` must pass `git check-ref-format --branch`, must not start with
`-`, and `refs/heads/<base>` must exist; a detached HEAD that no branch contains is refused
(`error: detached HEAD not on any branch`). `.nw/project.yaml` must exist and carry a `project:`
key, else `error: no project config to place the WIP worktree`; `project` must be a non-empty
string and a single path component (no `/`, `\`, `..`), else `error: bad project name`. A
malformed `worktrees` value is `ok: false` JSON. `commit` and `resume` need none of this.
`verb_refusal` runs guard 7 for these actions.

### Interface additions

Result keys, when set: `wip_branch`, `wip_path`, `wip_sha`, `replanned`
(`{on_base, action, class}`). `escalate` follows SPEC-0548's rule: `save_branch` is `false`,
`branch_and_escalate` is `true`, `hold` follows the class of the dirt it holds (`false` with no
dirt); an error after a substantive save stays `true`.

### The save

- Allocate `wip/main-dirty-<YYYYMMDDThhmmssZ>` and the directory
  `P.worktree_root(doc, root, project) / f"main-dirty-{ts}"` (`doc` is `.nw/project.yaml`,
  `project` its `project:` key; default `<root>/../.nw-worktrees/<project>`, or the configured
  `worktrees.root`), suffix `-2`, `-3`, … when either exists.
- `git worktree add -b <wip> <path> HEAD`. Copy each text path's bytes and mode; a binary path
  through `git hash-object -w` + `git update-index --add --cacheinfo`; a deletion through
  `git rm -f --` in the WIP tree. Commit there with subject
  `WIP: stray main-checkout edits (<class>)` and one `-m` line per path.
- Verify each path's blob and mode **from the WIP commit** against main's worktree (absent for a
  deletion).
- Only then restore main path by path, re-verifying that path just before:
  `git checkout HEAD -- <p>` when in HEAD, else `git rm -f -- <p>`. Success requires main
  tracked-clean and the WIP commit present.

**`hold`:** the save above (if dirty), then `git switch --no-guess <base>` only when
tracked-clean (never `-f`, `--discard-changes`, `-C`), re-plan, return `replanned`. `base` is
read again after the restore (HEAD's `.nw/project.yaml` may differ from the stray dirty one);
the re-read value passes guard 7's base checks again, else `ok: false`, the save kept, and no
switch runs. A failed switch keeps `wip_*` and reports `replanned.action: hold`.

**Failure phases of a save.** Main is byte-identical for every path not yet restored, in every
row. In rows (a)–(c) the WIP worktree is removed first, then the branch is deleted.

| phase that fails | WIP worktree | WIP branch | result keys |
|---|---|---|---|
| (a) `git worktree add` | not created (any partial dir removed) | deleted if created (`git branch -d`, it has no commit) | `ok: false`, `error` |
| (b) copy, `hash-object` or `update-index` in the WIP tree | removed (`git worktree remove --force` on the WIP tree only) | deleted (`git branch -d`, no commit yet) | `ok: false`, `error` |
| (c) WIP commit (hook) | removed | deleted (`git branch -d`, no commit) | `ok: false`, `error` with the hook's stderr |
| (d) blob/mode verify from the WIP commit | kept | kept (it holds the commit; `branch -D` is never run) | `ok: false`, `error`, `wip_branch`, `wip_path`, `wip_sha` |
| (e) per-path re-verify or restore of path k | kept | kept | `ok: false`, `error` naming path k, `wip_branch`, `wip_path`, `wip_sha`; paths before k are restored (their content is on the WIP commit), k and later are untouched |

**Never run**, asserted on recorded argv; this list replaces SPEC-0548's (which banned
`checkout`, `restore` and `switch` outright because the core never needed them): `reset`,
`restore`, `clean`, `checkout -- .`, `checkout .`, `checkout` without `HEAD --`, `rm -r`,
`stash`, `push`, `branch -D`, `switch -f`, `switch --discard-changes`, `switch -C`, and any
non-`git` command (no `nw`). The only `worktree remove --force` names the WIP tree the verb just
created.

### `recommended`

For `save_branch`, `branch_and_escalate` and `hold`, `recommended` in `plan_clear` becomes
`["nw queue main-dirty-apply --root <R>"]` when `verb_refusal(root, plan)` is `None` (guards
5–7); otherwise SPEC-0546's raw list stays. `resume` and `escalate` are unchanged.

### Files the implementer may cherry-pick from `nw/IDEA-1159-20261110-02ed`

Optional source (tip `61876af9`), never the contract; where it differs, this spec wins (the
failure table, the WIP location, guard 7).

- `src/nightwright/agentic/main_dirty.py`: `_wip_parent`, `_git_wip_literal`, `_precheck_save`,
  `_allocate_wip_slot`, `_stage_binary_on_wip`, `_copy_paths_to_wip`, `_verify_wip_matches`,
  `_restore_paths_on_main`, `_wip_commit`, `_save_to_wip`, `_valid_base`, `_head_on_a_branch`.

## Alternatives considered

- **Write symlinks into the WIP tree** (mode `120000` entries by hand). The riskiest write in
  the verb for the rarest dirt; SPEC-0548 refuses symlink and typechange dirt and `recommended`
  keeps the raw list for it.
- **`git stash` instead of a WIP worktree.** A stash is not a branch and is easy to drop; the WIP
  branch is visible and matches SPEC-0546's `<wip>` name.
- **Restore main first, then save.** A failed save would lose the work; save, verify from the
  commit, then restore.

## Acceptance criteria

- [ ] AC1 (SPEC-0543 AC4, + AC8's `worktrees` case) Save: for text, binary, deleted, newly
      added, mode-only (`chmod +x` of a tracked file), a newly added executable file, and an
      executable file with a content edit, the WIP commit's subject is
      `WIP: stray main-checkout edits (<class>)`, each WIP blob and mode equals the pre-clear
      main bytes and mode, main is tracked-clean afterwards, none of it is committed on the base,
      untracked files are byte-identical, and the WIP worktree is under the configured worktree
      root (default `.nw-worktrees/<project>/main-dirty-*`; one case with a custom
      `worktrees.root`) on branch `wip/main-dirty-*`. Without `.nw/project.yaml` or its
      `project:` key a save returns `error: no project config to place the WIP worktree` with no
      write, while `commit` still works; `project: ../x` returns `error: bad project name` with no
      write; a malformed `worktrees` value returns `ok: false` JSON with no write.
- [ ] AC2 (SPEC-0543 AC5) Ordering (recorded argv): the WIP commit and the blob check precede
      every main `checkout HEAD --` / `rm -f --`; every main path call carries
      `--literal-pathspecs` (the test fails if the flag is removed); the argv never contains an
      entry of the never-run list.
- [ ] AC3 (SPEC-0543 AC6) One test per row of the failure-phase table, each forced by a fault
      injected at that phase: (a) failed `worktree add`, (b) failed copy, (c) failing WIP-commit
      hook, (d) a blob mismatch at verify, (e) a re-verify failure on the second of three paths.
      Each asserts the table's WIP-worktree and WIP-branch state and result keys exactly, that
      main is byte-identical for every not-yet-restored path, that (b) and (c) run
      `worktree remove` before `branch -d`, that (c) carries the hook's stderr, and that (e) has
      the first path restored and its content on `wip_sha`.
- [ ] AC4 (SPEC-0543 AC7) A name clash yields `-2`, then `-3`, for both branch and directory.
- [ ] AC5 (SPEC-0543 AC9) `escalate` is asserted by a parametrised test over every (action,
      class) pair the plan can produce, success and refusal, plus the two no-plan returns
      (`action: "none"`): `true` exactly for `substantive` and `merge`.
- [ ] AC6 (SPEC-0543 AC10) `hold`: substantive dirt off the base is saved,
      `git switch --no-guess <base>` runs only when tracked-clean, `replanned` is returned and
      `escalate` is true; a failed switch keeps `wip_*` and reports `replanned.action: hold`; a
      failed save does not switch; an option-shaped or invalid `base`, a `base` with no
      `refs/heads/<base>`, and a detached HEAD on no branch are refused before any write; a base
      that is valid in the dirty `.nw/project.yaml` but option-shaped in HEAD's copy keeps the
      save and runs no `switch`.
- [ ] AC7 (SPEC-0543 AC13) `recommended` is `["nw queue main-dirty-apply --root <R>"]` for
      `commit`, `save_branch`, `branch_and_escalate`, `hold` when `verb_refusal` is `None`; the
      raw list for a symlink, a typechange, a partially staged path, a gitlink, a path under a
      symlinked directory, and a save with no `.nw/project.yaml`; unchanged for `resume` /
      `escalate`. SPEC-0546's AC5 run-the-list tests now run the verb command through the CLI
      for the save actions and assert the same end state; the raw-list fixtures still run the
      raw list.

## Test plan

- **Automated tests:** extend `tests/test_main_dirty_apply.py` (SPEC-0548). Take from the
  branch's `tests/test_main_dirty_clear.py` (tip `61876af9`) the save `test_apply_*` tests,
  `test_backslash_name_is_kept_and_saved`, `test_hold_reads_base_after_the_restore`. New: the
  AC1 mode fixtures and config refusals, the AC3 phase table, AC5's `escalate` table, AC6's
  re-read base, AC7's refusal fixtures. SPEC-0548's never-run assertion is replaced by AC2's
  list (the only inherited assertion this spec edits).
- **Existing modules:** `tests/test_main_dirty_plan.py` updates its save-action `recommended`
  expectations and run-the-list tests (AC7).
- **Mutation check (by hand, once):** remove `--literal-pathspecs` from the restore calls; AC2
  fails. Recorded in As shipped.
- **Regression guard:** `python3 tools/affected_tests.py --run uv run pytest -q`,
  `python3 tools/test_tiers.py --check`, `python3 tools/spec_lint.py`.
- **Manual verification (real check):** scratch repo under the scratchpad with
  `.nw/project.yaml` (`project: Scratch`, `base: main`). (1) Edit `src/x.py` logic, add an
  untracked file, run `uv run nw queue main-dirty-apply --root <scratch> --json` → `ok: true`,
  `escalate: true`; `git -C <scratch> status --porcelain --untracked-files=no` empty;
  `git log -1 wip/main-dirty-*` shows the edit; the untracked file is still there. (2) On a
  branch `feature` with a config edit → `hold` saves and switches to `main`. Record the outputs in
  As shipped.

## Out of scope

- Symlink and typechange dirt (refused by SPEC-0548; raw list kept).
- Removing the WIP worktree or branch after a successful save: left for the human.
- Any conductor permission; the conductor never runs a save (SPEC-0551).
- The verb sends no escalation, mail, or resume. Untracked files are never touched. No push.

## Rollout / migration

Ships after SPEC-0548. No data move. The WIP worktree and branch are left for the human to
inspect and remove by hand (`git worktree remove`, `git branch -d`); `nw queue gc` lists the
worktree as `no campaign` and keeps it. Existing escalations keep their commands; new save ones
name the verb. Queued under project Meta-Tools.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed, including the scratch-repo check; affected tests pass.
- [ ] No regressions.
- [ ] `docs/product/reference/index.md` and `docs/product/loop/operate.md` say the verb saves
      non-trivial dirt to a `wip/main-dirty-*` branch.
- [ ] Spec body updated to match what shipped; As shipped records the AC2 mutation run.
- [ ] `docs/LEDGER.md` regenerated; `python3 tools/spec_lint.py` exits 0.

## Open questions

None.
