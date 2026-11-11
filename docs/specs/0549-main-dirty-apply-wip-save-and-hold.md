---
id: SPEC-0549
title: main-dirty-apply saves non-trivial dirt to a WIP branch, restores main, and holds
status: done
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
(verdict `split`): [SPEC-0548](./0548-main-dirty-apply-core-commit.md) carries the first half
(guards and `commit`) and SPEC-0549 carries the second half (the save actions), so both name
SPEC-0543 and the ledger shows both successors; SPEC-0549's `supersedes: [SPEC-0543]` is
deliberate. It replaces part of [SPEC-0537](./0537-conductor-must-clear-main-dirty-itself-a-stray.md)
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
SPEC-0548's guards 1–6 (the base checks, always):

- `base` must pass `git check-ref-format --branch`, must not start with `-`, and
  `refs/heads/<base>` must exist (else `error: bad base <base>`).
- A detached HEAD is accepted only when `git for-each-ref --contains HEAD refs/heads` is
  non-empty; otherwise `error: detached HEAD not on any branch`. (This spec wins over the
  branch's `_head_on_a_branch` where they differ.)

The WIP-location checks apply **only when the plan has tracked dirt to save**; a `hold` with no
dirt needs only the base checks above:

- `.nw/project.yaml` must exist and carry a `project:` key, else
  `error: no project config to place the WIP worktree`.
- `project` is a non-empty string, not `.` or `..`, with no `/`, `\` or NUL, and unchanged by
  `strip()`, else `error: bad project name`.
- A malformed `worktrees` value is `ok: false` JSON.

`commit` and `resume` need none of this. `verb_refusal` runs guard 7 for these actions.

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
- `git worktree add -b <wip> <path> HEAD`. Copy each non-deleted path's bytes and mode into the
  WIP tree, then stage it by `git hash-object -w --no-filters -- <p>` +
  `git update-index --add --cacheinfo <mode>,<oid>,<p>` (text and binary alike; `git add` is
  never used, because it applies clean filters and EOL conversion); a deletion goes through
  `git --literal-pathspecs rm -f -- <p>` in the WIP tree. Every path call, in main and in the
  WIP tree, carries `--literal-pathspecs`. Commit there with subject
  `WIP: stray main-checkout edits (<class>)` and one `-m` line per path.
- **Comparator.** WIP blobs are the raw worktree bytes (`--no-filters`), so a repo with
  `.gitattributes` `text=auto eol=crlf` or `filter=lfs` saves exactly what main's file holds.
  Verify compares each WIP blob against `git hash-object --no-filters -- <p>` of main's file
  (absent for a deletion), never against a filtered hash.
- Verify, **from the WIP commit**: each planned path's blob and mode equals the comparator
  above, and `git diff-tree -r --name-only -z HEAD <wip_sha>` equals the planned paths exactly
  (an unplanned path in the WIP commit fails phase (d)).
- Only then restore main path by path, re-verifying that path just before:
  `git checkout HEAD -- <p>` when in HEAD, else `git rm -f -- <p>`.
- Success requires, after every path is restored, main tracked-clean and the WIP commit
  present (phase (f)).

**`hold`:** the save above (if dirty), then `git switch --no-guess <base>` only when
tracked-clean (never `-f`, `--discard-changes`, `-C`), re-plan, return `replanned`, which is
always the real re-plan result `{on_base, action, class}` (never inferred from the switch's exit
status). `base` is read again after the restore (HEAD's `.nw/project.yaml` may differ from the
stray dirty one); the re-read value passes guard 7's base checks again, else `ok: false`, the
save kept, and no switch runs. A failed switch keeps `wip_*`. `hold` returns `ok: true` only
when the switch exited 0, the re-plan reports `on_base: true`, and main is tracked-clean;
otherwise `ok: false` with `wip_*` kept (when a save ran). A switch that exits non-zero after
HEAD moved (a `post-checkout` hook's status is `git switch`'s) reports the re-plan as it is
(for example `resume` / `clean`) with `ok: false`.

**Failure phases of a save.** Main is byte-identical for every path not yet restored, in every
row. In rows (a)–(c) the WIP worktree is removed first, then the branch is deleted. Every step
of the save maps to exactly one row.

| phase that fails | WIP worktree | WIP branch | result keys |
|---|---|---|---|
| (a) `git worktree add` exits non-zero | if the WIP path is registered (`git worktree list --porcelain`, e.g. a failing `post-checkout` hook, which runs after git registered the worktree), run `git worktree remove --force <wip_path>` first; then remove any remaining partial directory the verb just created | `git branch -d <wip>` if the branch exists (it has no commit) | `ok: false`, `error` carries git's stderr |
| (b) copy, `git rm -f --`, `hash-object` or `update-index` in the WIP tree | removed (`git worktree remove --force` on the WIP tree only) | deleted (`git branch -d`, no commit yet) | `ok: false`, `error` |
| (c) WIP commit (hook) | removed | deleted (`git branch -d`, no commit) | `ok: false`, `error` with the hook's stderr |
| (d) verify from the WIP commit: blob/mode mismatch, or `diff-tree` path set differs from the planned paths | kept | kept (it holds the commit; `branch -D` is never run) | `ok: false`, `error`, `wip_branch`, `wip_path`, `wip_sha` |
| (e) per-path re-verify or restore of path k | kept | kept | `ok: false`, `error` naming path k, `wip_branch`, `wip_path`, `wip_sha`; paths before k are restored (their content is on the WIP commit), k and later are untouched |
| (f) post-restore check: main not tracked-clean or the WIP commit missing | kept | kept | `ok: false`, `error` names the leftover paths, `wip_branch`, `wip_path`, `wip_sha` |

If cleanup in (a)–(c) itself fails (for example `worktree remove --force` fails, so `branch -d`
cannot run), keep what remains, return `ok: false` with an `error` naming both the original and
the cleanup failure, and set `wip_*` only when a WIP commit exists.

**Never run**, asserted on recorded argv; this list replaces SPEC-0548's (which banned
`checkout`, `restore` and `switch` outright because the core never needed them): `reset`,
`restore`, `clean`, `checkout -- .`, `checkout .`, `checkout` without `HEAD --`, `rm -r`,
`stash`, `push`, `branch -D`, `switch -f`, `switch --discard-changes`, `switch -C`, and any
non-`git` command (no `nw`). The only `worktree remove --force` names the WIP tree the verb just
created.

### `recommended`

For `save_branch`, `branch_and_escalate` and `hold`, `recommended` in `plan_clear` becomes
`["nw queue main-dirty-apply --root <R>"]` when `verb_refusal(root, plan)` is `None` (guards
5–7); otherwise SPEC-0546's raw list stays. When guard 7's base check fails (option-shaped or
invalid `base`, missing `refs/heads/<base>`), the raw `hold` list omits its `switch <base>`
line, so it never contains an invalid base. `resume` and `escalate` are unchanged.

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

- [x] AC1 (SPEC-0543 AC4, + AC8's `worktrees` case) Save: for text, binary, deleted, newly
      added, mode-only (`chmod +x` of a tracked file), a newly added executable file, and an
      executable file with a content edit, the WIP commit's subject is
      `WIP: stray main-checkout edits (<class>)`, each WIP blob and mode equals the pre-clear
      main bytes and mode, main is tracked-clean afterwards, none of it is committed on the base,
      untracked files are byte-identical, and the WIP worktree is under the configured worktree
      root (default `.nw-worktrees/<project>/main-dirty-*`; one case with a custom
      `worktrees.root`) on branch `wip/main-dirty-*`. Without `.nw/project.yaml` or its
      `project:` key a save returns `error: no project config to place the WIP worktree` with no
      write, while `commit` still works; `project: ../x` returns `error: bad project name` with no
      write; a malformed `worktrees` value returns `ok: false` JSON with no write. The `bad
      project name` cases are `../x`, `.`, `..`, a name with a backslash, a NUL, or surrounding
      whitespace, and an empty string, each with no write. One fixture has `.gitattributes`
      `* text=auto eol=crlf` and a CRLF-sensitive edited text file: the WIP blob equals the raw
      worktree bytes (the `--no-filters` comparator) and the save succeeds.
- [x] AC2 (SPEC-0543 AC5) Ordering (recorded argv): the WIP commit and the blob check precede
      every main `checkout HEAD --` / `rm -f --`; every path call, in main and in the WIP tree,
      carries `--literal-pathspecs` (the test fails if the flag is removed from either); a
      fixture with tracked paths `a*b`, `axb` and a deletion of `a*b` shows that, with the flag,
      only `a*b` is removed and the WIP commit holds exactly the planned paths (without the
      flag an extra path would appear and phase (d) would fire); the argv never contains an
      entry of the never-run list.
- [x] AC3 (SPEC-0543 AC6) One test per row of the failure-phase table, each forced by a fault
      injected at that phase, with a real git fault where possible (pre-commit hook, unwritable
      parent directory, colliding branch) and a monkeypatched helper otherwise, the method named
      in the test: (a) failed `worktree add`, in two cases: an unwritable parent directory, and
      a failing `post-checkout` hook (worktree registered, then non-zero), each asserting no
      worktree entry (`git worktree list --porcelain`) and no `wip/main-dirty-*` branch remain;
      (b) failed copy (monkeypatched helper); (c) failing WIP-commit pre-commit hook; (d) a
      blob mismatch at verify (monkeypatched helper), and a second case where the WIP commit
      holds an unplanned path; (e) a re-verify failure on the second of three paths; (f) a
      post-restore check failure (a tracked file dirtied by a monkeypatched restore helper
      after the last restore) asserting worktree and branch kept and `error` naming the leftover
      path; plus a cleanup-failure case (`worktree remove --force` forced to fail in (c))
      asserting `ok: false`, an `error` naming both failures, and no `wip_*`. Each asserts the
      table's WIP-worktree and WIP-branch state and result keys exactly, that main is
      byte-identical for every not-yet-restored path, that (b) and (c) run `worktree remove`
      before `branch -d`, that (c) carries the hook's stderr, and that (e) has the first path
      restored and its content on `wip_sha`.
- [x] AC4 (SPEC-0543 AC7) A name clash yields `-2`, then `-3`, for both branch and directory.
- [x] AC5 (SPEC-0543 AC9) `escalate` is asserted by a parametrised test over every (action,
      class) pair the plan can produce, success and refusal, plus the two no-plan returns
      (`action: "none"`): `true` exactly for `substantive` and `merge`.
- [x] AC6 (SPEC-0543 AC10) `hold`: substantive dirt off the base is saved,
      `git switch --no-guess <base>` runs only when tracked-clean, `ok: true`, `replanned` is
      returned with exactly the keys `on_base`, `action`, `class` (after a successful switch
      `{on_base: true, action: resume, class: clean}`) and `escalate` is true. A failed switch,
      forced by an untracked file that collides with a base file, keeps `wip_*`, is `ok: false`
      and reports `replanned.action: hold`; a non-zero switch whose HEAD did move (a failing
      `post-checkout` hook) is `ok: false` with `replanned` reported as the re-plan is. A failed
      save does not switch. An option-shaped or invalid `base`, a `base` with no
      `refs/heads/<base>`, and a detached HEAD where `git for-each-ref --contains HEAD
      refs/heads` is empty (`error: detached HEAD not on any branch`; a detached HEAD on a
      branch tip is accepted) are refused before any write. A base that is valid in the dirty
      `.nw/project.yaml` but option-shaped in HEAD's copy keeps the save and runs no `switch`.
      A `hold` with no tracked dirt and no `.nw/project.yaml` switches, creates no worktree and
      writes no `wip_*`.
- [x] AC7 (SPEC-0543 AC13) `recommended` is `["nw queue main-dirty-apply --root <R>"]` for
      `commit`, `save_branch`, `branch_and_escalate`, `hold` when `verb_refusal` is `None`; the
      raw list for a symlink, a typechange, a partially staged path, a gitlink, a path under a
      symlinked directory, and a save with no `.nw/project.yaml`; unchanged for `resume` /
      `escalate`. SPEC-0546's AC5 save-action fixtures gain a committed `.nw/project.yaml`
      (`project: Scratch`); the test asserts the new `recommended` is the verb command before it
      runs it through the CLI and asserts the same end state; the raw-list fixtures still run
      the raw list. Raw-list fixtures for an invalid (option-shaped) `base` and for a missing
      `refs/heads/<base>` assert the raw `hold` list has no `switch <base>` line.

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

### Test record (2026-11-11)

- `uv run pytest -q tests/test_main_dirty_apply.py tests/test_main_dirty_plan.py`: 224 + 465 passed
  (the apply file gained the save, AC2 ordering, AC3 phase, AC4 clash, AC5 `escalate`, AC6 `hold`
  tests; the plan file gained the AC7 verb / raw-list tests and runs the verb through the CLI).
- Targeted: `uv run pytest -q -n 4` over `tests/test_main_dirty_apply.py`, `test_main_dirty_plan.py`,
  `test_docs_product_reference.py`, `test_docs_product_guides.py`, `test_specs_dir.py`,
  `test_test_tiers.py`, `test_guard_registry.py` and the spec-lint / conductor-guard modules: 729
  passed. `python3 tools/test_tiers.py --check` exits 0; `python3 tools/spec_lint.py` exits 0 with
  the ledger regenerated. The wide affected-tests run (a TUI / dashboard-heavy set) and the full
  suite are left to gating.
- Mutation (by hand, once): `_restore_path` switched from `_lg` to `_git` (no `--literal-pathspecs`
  on the restore calls) made `test_save_orders_commit_and_check_before_any_restore_and_stays_literal`
  fail; reverted.
- Manual (scratch repo, `project: Scratch`): (1) `src/x.py` edit + a newly added file + `chmod +x` +
  an untracked file on `main`, `nw queue main-dirty-apply --root <scratch> --json` returned
  `ok: true`, `escalate: true`, `wip_branch`/`wip_path`/`wip_sha`; `git status --porcelain
  --untracked-files=no` was empty, the untracked file remained, the WIP commit held the three paths.
  (2) On branch `feature` with a `.nw/project.yaml` edit, `hold` returned `ok: true`,
  `replanned: {on_base: true, action: resume, class: clean}`; `HEAD` was `main` afterwards.

- Review loop 1 (tests, plus text mode): `tests/test_main_dirty_apply.py` gained the AC2
  `rm -f --` restore test (a newly added `a*b` beside a tracked `axb`; recorded argv carries
  `--literal-pathspecs` and `--`; mutation of `_restore_path`'s `rm` branch to `_git` made it fail,
  reverted), the AC7 raw-list-for-save-actions tests (symlink, typechange, symlink-to-file,
  symlinked parent, gitlink, partial staging, all with a committed `.nw/project.yaml`),
  `test_backslash_name_is_kept_and_saved` and a parametrised save over awkward names, the
  planned-path-equals-HEAD / empty-WIP-commit tests, the tightened AC3 / AC4 / AC6 assertions and the
  missing sub-branches of rows (d), (e), (f). The text mode of `nw queue main-dirty-apply` now also
  prints `wip_branch`, `wip_path`, `wip_sha` and a one-line `replanned` (tested). Affected tests
  (`test_main_dirty_apply.py`, `test_main_dirty_plan.py`) pass; `python3 tools/test_tiers.py --check`
  and `python3 tools/spec_lint.py` exit 0; the full suite is left to gating.
- Review loop 2 (tests only): `test_main_dirty_apply.py` gained the `hold` ok:true conditions
  (a `post-checkout` hook, exit 0, that dirties a tracked file after the switch; a switch that
  exits 0 but leaves the re-plan off the base; mutating `_hold_switch`'s `elif` to `False` made both
  fail, reverted), row (a)'s partial-directory removal, row (b) faults in `update-index`,
  `hash-object` and `rm` in the WIP tree, row (e) mode-change and reappeared-deletion re-verifies,
  `_reverify_path` branch tests, and a clean `hold` with a missing `refs/heads/<base>` or a detached
  HEAD on no branch. `test_main_dirty_plan.py` runs SPEC-0546's AC5 save fixtures (rm, mv,
  unstaged_delete, logic, mixed) through the verb with a committed `.nw/project.yaml` and the same
  end-state checks (shared helper with the raw-list run). `uv run pytest -q -n 4` over the two files:
  742 passed; `python3 tools/test_tiers.py --check` exits 0; the full suite is left to gating.

### As shipped (2026-11-11)

Code in `src/nightwright/agentic/main_dirty.py` (written fresh on `main`'s base, not cherry-picked
from the branch): `_guard7` / `_base_refusal` / `_wip_config` (in `verb_refusal`), `_save_to_wip`
and its phase helpers (`_copy_to_wip`, `_stage_in_wip`, `_wip_commit`, `_verify_wip`,
`_reverify_path`, `_restore_main`, `_discard_wip`), `_hold_switch`, `_save_clear`. Deviations and
choices the Design left open:

- **Expected path set.** The `diff-tree` check compares against the planned paths that really differ
  from HEAD (a planned path whose bytes and mode equal HEAD, e.g. a staged edit reverted in the
  worktree, or `AD`, leaves no change and is not expected). An empty WIP commit is allowed
  (`commit --allow-empty`), so such a plan still restores main.
- **Error text.** Guard 7 refusals are `bad base <base>`, `detached HEAD not on any branch`,
  `no project config to place the WIP worktree`, `bad project name`, `bad worktrees config`.
  Phase (e) is `<path>: <reason>`; phase (f) is `main not restored: <paths>`.
- **Option-shaped base.** `plan_clear` already raised `PlanError` for an option-shaped base, so that
  case is an `ok: false` JSON without a plan (no raw list); the raw `hold` list omits `switch
  <base>` for an invalid ref name or a missing `refs/heads/<base>`, and for every list (not only
  `hold`) the final `switch <base>` line is dropped when the base is invalid.
- **post-checkout hook.** A failing `post-checkout` hook also fails `git checkout HEAD -- <p>` (its
  status is the command's), so such a hook turns the restore into a phase (e) failure with the WIP
  kept; the AC6 hook test therefore fails only on the branch-switch flag in main.
- `_head_map` gained a `rev` argument (verify reads the WIP commit); `_assert_safe_argv` in the tests
  now carries this spec's never-run list.
- **Empty `project:`.** The config loader rejects it first (`invalid project config:\n  - project:
  must be a non-empty string`), so guard 7's own `bad project name` never sees an empty string; the
  result is still `ok: false` with no write (tested).
- **Re-read base.** A base that is option-shaped in HEAD's copy is rejected by `_base` before guard
  7's check, so that `hold` error reads `invalid base branch '-x'` (save kept, no `switch`); a missing
  `refs/heads/<base>` reads `bad base <base>`.

## Goals / Non-goals

**Goals**

- A human or steward can save non-trivial main-checkout dirt to a WIP branch and restore main
  without losing it.

**Non-goals**

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

- [x] Acceptance criteria all met.
- [x] Test plan executed, including the scratch-repo check; affected tests pass.
- [x] No regressions.
- [x] `docs/product/reference/index.md` and `docs/product/loop/operate.md` say the verb saves
      non-trivial dirt to a `wip/main-dirty-*` branch.
- [x] Spec body updated to match what shipped; As shipped records the AC2 mutation run.
- [x] `docs/LEDGER.md` regenerated; `python3 tools/spec_lint.py` exits 0.

## Open questions

None.
