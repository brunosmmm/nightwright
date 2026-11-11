---
id: SPEC-0543
title: nw queue main-dirty-apply, the audited main-checkout clear (human or steward run)
status: superseded
owner: user
created: 2026-11-11
updated: 2026-11-11
kind: feature
tags: [agentic, queue, git]
depends_on: [SPEC-0542]
source_idea: IDEA-1159
superseded_by: SPEC-0548
---

> **Superseded** (2026-11-11, spec review verdict `split`, size xl): split into [SPEC-0548](./0548-main-dirty-apply-core-commit.md) (verb core, guards, `commit`, conductor deny) and [SPEC-0549](./0549-main-dirty-apply-wip-save-and-hold.md) (WIP save, restore and `hold`); nothing below shipped.

## Context

[SPEC-0542](./0542-main-dirty-plan-and-escalate.md) makes `main_dirty` conductor-owned and
gives the human an escalation with the class and the raw git commands that clear the checkout.
This spec adds the write half that
[SPEC-0537](./0537-conductor-must-clear-main-dirty-itself-a-stray.md) attempted. The human
approved replacing raw git allow-rules with **one audited verb, `nw queue main-dirty-apply`**
(campaign `IDEA-1159-20261110-02ed`, resume note seq 437: "the verb itself must refuse a merge
in progress, escalate substantive diffs, and save non-trivial diffs to a WIP branch before
restoring"). At ESC-0067 the human chose to split SPEC-0537; this is part B of the steward's
split. The verb is run by the human or a steward. The conductor gets no new permission here;
[SPEC-0544](./0544-conductor-runs-main-dirty-apply-for-trivial.md) lets it run the trivial
class.

Most review majors on SPEC-0537's branch were about this verb: it trusted the caller's plan
file, did not guard a sequencer in progress, wrote through symlinks, dropped `wip_*` from a
failed `hold`, had no CLI test, and its tests passed with a wrong commit message or WIP
content and without `--literal-pathspecs`. The criteria below pin each of those.

## Goals / Non-goals

**Goals**

- One command clears a `main_dirty` checkout without losing work: a trivial diff is committed
  on the base (only when on the base); anything else is saved to a WIP branch in a worktree
  under the project's worktree root before the tracked paths are restored. Symlink and
  typechange dirt is refused (it keeps SPEC-0542's raw commands).
- It re-plans on the live checkout and refuses anything it cannot do safely, with no write.
- SPEC-0542's `recommended` names this verb.

**Non-goals**

- No conductor permission change other than an explicit deny of this verb. No raw-git
  allow/deny change for anyone. The conductor's skill row is not changed beyond SPEC-0542.
- The verb sends no escalation, mail, or resume. It runs only `git`.
- Untracked files are never touched. No push, ever.

## Decision

`apply_clear(root, plan=None) -> dict` in `src/nightwright/agentic/main_dirty.py` and
`nw queue main-dirty-apply --root PATH [--plan FILE] [--json]` in `cli_queue.py`. It takes a
lock, re-plans with `plan_clear`, refuses an operation in progress before anything else, then
a stale `--plan`, unsafe paths (symlinks and typechanges included), partial staging, and an
unusable base or WIP location, and only then performs the live plan's action. `escalate` is set
from the live class alone. SPEC-0542's `recommended` becomes `nw queue main-dirty-apply --root
<R>` for `commit`, `save_branch`, `branch_and_escalate`, and `hold` (raw list kept for symlink
dirt). The conductor launch denies the verb.

## Design

### Interface

Output schema `nw.main_dirty_apply.v1`: `schema`, `root`, `action`, `ok`, `escalate` (always
present), and when set `sha`, `wip_branch`, `wip_path`, `wip_sha`, `replanned`
(`{on_base, action, class}`), `error`. Text mode prints `main-dirty-apply ok|failed: <action>`
and the error.

Exit codes: 0 whenever `apply_clear` ran (including `ok: false`); 2 when `--root` is not the top
of a git worktree, or `--plan` is not a `nw.main_dirty_plan.v1` object whose `paths` is a list of
strings; 3 only when the CLI's own `git rev-parse --show-toplevel` of the root fails with
something other than "not a git repository". Once `apply_clear` runs, every refusal or failed
step is `ok: false` JSON, never a traceback (`GitError`, `OSError`, `ValueError`, `TypeError`,
`AttributeError` are caught; a malformed `.nw/project.yaml` included).

**`escalate`** is a function of the live class only, so a caller never has to infer it:
`true` when the live `class` is `substantive` or `merge`; `false` for `trivial`, `branch` and
`clean`. That covers every action and every return, success or error: `commit` and `resume`
are always `false`; `save_branch` is `false`; `branch_and_escalate` and `escalate` are `true`;
`hold` follows the class of the dirt it holds (`false` with no dirt). An error after a
substantive save stays `true`.

### Guards, in order, before any write

Every git call before the first write runs with `GIT_OPTIONAL_LOCKS=0`, so no guard refreshes
the index. The lock file is the only file created before the first write; "no write" below
means no change to HEAD, the index, the worktree or any ref.

1. **Lock:** a non-blocking `flock` on `<git-dir>/nw-main-dirty.lock`; a second concurrent run
   returns `ok: false`, `error: clear already in progress`.
2. **Re-plan:** `plan_clear(root)` on the live checkout. Everything below uses the live plan.
   `base` always comes from the project, never from a plan file.
3. **Operation in progress** (live `class: merge`, any `in_progress`): no write, `ok: false`,
   `escalate: true`, `error: <in_progress> in progress`. This runs before the `--plan`
   comparison: a live `in_progress` takes precedence over `plan is stale`, whatever the plan
   file says (a merge that started after the plan was written is the main reason the verb
   re-plans).
4. **Plan check:** with `--plan`, its `action` and sorted `paths` must equal the live plan's,
   else `error: plan is stale`.
5. **Paths:** each live path is relative, has no `..` segment, and is not a directory (a gitlink
   is one); neither the path nor any parent directory is a symlink, in the worktree or in HEAD
   (mode `120000`). A path with glob characters is accepted only as that literal path; a path
   that is in neither HEAD nor the worktree nor the index is refused. Else
   `error: bad path <p>`. **Symlink and typechange dirt is refused** (Decision below): the verb
   never saves or restores a symlink.
6. **Index:** a path whose index blob differs from both HEAD and the worktree (partially
   staged) is refused, `error: partially staged <p>`.
7. **Base and WIP location**, for actions that save or switch (`save_branch`,
   `branch_and_escalate`, `hold`): `base` must pass `git check-ref-format --branch`, must not
   start with `-`, and `refs/heads/<base>` must exist; a detached HEAD that no branch contains is
   refused (`error: detached HEAD not on any branch`). `.nw/project.yaml` must exist and carry a
   `project:` key, else `error: no project config to place the WIP worktree`. `commit` and
   `resume` need neither.

Every git call passes `--literal-pathspecs` and puts paths after `--`.

**Why symlinks are refused, not saved.** A file↔symlink typechange or a retargeted link is
`other` (SPEC-0542), so it is `save_branch`. Copying a link into the WIP tree means writing a
mode-`120000` entry by hand, and restoring it means writing through, or replacing, a link in the
human's checkout: the exact class of bug the branch's review found. The rare case keeps the raw
commands instead: SPEC-0542's `recommended` stays the raw git list whenever any planned path is
a symlink or typechange on either side (`git add` records a link without following it), so the
human is never told to run a command that always refuses.

### Actions

- **`commit`** (only reached when the live action is `commit`, so on the base): stage each
  planned path with `git add --`, commit with subject
  `chore: land stray trivial main-checkout edits` and one `-m` line per path, return `sha`.
  A failed commit (hook, identity) unstages what it staged, leaves the index as before, and
  returns git's stderr in `error`. Succeeds only when main is tracked-clean afterwards. Never
  `commit -a`, `add -A`.
- **`save_branch`, `branch_and_escalate`, `hold` with dirt:** allocate
  `wip/main-dirty-<YYYYMMDDThhmmssZ>` and the directory
  `P.worktree_root(doc, root, project) / f"main-dirty-{ts}"` (`doc` is `.nw/project.yaml`,
  `project` its `project:` key; default `<root>/../.nw-worktrees/<project>`, or the configured
  `worktrees.root`), suffix `-2`, `-3`, … when either exists.
  `git worktree add -b <wip> <path> HEAD`. Copy each text path's bytes and mode; a binary path
  through `git hash-object -w` + `git update-index --add --cacheinfo`; a deletion through
  `git rm -f --` in the WIP tree. Commit there with subject
  `WIP: stray main-checkout edits (<class>)` and one `-m` line per path. Verify each path's blob
  and mode **from the WIP commit** against main's worktree (absent for a deletion). Only then
  restore main path by path, re-verifying that path just before: `git checkout HEAD -- <p>` when
  in HEAD, else `git rm -f -- <p>`. Success requires main tracked-clean and the WIP commit
  present.
- **`hold`:** the save above (if dirty), then `git switch --no-guess <base>` only when
  tracked-clean (never `-f`, `--discard-changes`, `-C`), re-plan, return `replanned`. `base` is
  read again after the restore (HEAD's `.nw/project.yaml` may differ from the stray dirty one);
  the re-read value passes guard 7's checks again, else `ok: false`, the save kept, and no switch
  runs. A failed switch keeps `wip_*` and reports `replanned.action: hold`.
- **`resume`:** `ok: true` when tracked-clean, no write. **`escalate`:** see guard 3.

**Failure phases of a save.** Main is byte-identical for every path not yet restored, in every
row.

| phase that fails | WIP worktree | WIP branch | result keys |
|---|---|---|---|
| (a) `git worktree add` | not created (any partial dir removed) | deleted if created (`git branch -d`, it has no commit) | `ok: false`, `error` |
| (b) copy, `hash-object` or `update-index` in the WIP tree | removed (`git worktree remove --force` on the WIP tree only) | deleted (`git branch -d`, no commit yet) | `ok: false`, `error` |
| (c) WIP commit (hook) | removed | deleted (`git branch -d`, no commit) | `ok: false`, `error` with the hook's stderr |
| (d) blob/mode verify from the WIP commit | kept | kept (it holds the commit; `branch -D` is never run) | `ok: false`, `error`, `wip_branch`, `wip_path`, `wip_sha` |
| (e) per-path re-verify or restore of path k | kept | kept | `ok: false`, `error` naming path k, `wip_branch`, `wip_path`, `wip_sha`; paths before k are restored (their content is on the WIP commit), k and later are untouched |

Never run, asserted on recorded argv: `reset`, `clean`, `checkout -- .`, `checkout .`,
`rm -r`, `stash`, `push`, `branch -D`, `switch -f`, and any non-`git` command (no `nw`). The
only `worktree remove --force` names the WIP tree the verb just created.

### Callers and permissions

- `recommended` in `plan_clear` (SPEC-0542) for `commit`, `save_branch`,
  `branch_and_escalate`, `hold` becomes `["nw queue main-dirty-apply --root <R>"]` (quoted),
  except when a planned path is a symlink or typechange (guard 5), where the raw list stays. The
  SPEC-0542 escalation then tells the human to run that one command.
- The steward already holds `Bash(nw *)` / `Bash(uv run nw *)`; `skills/nw-steward/SKILL.md`
  gains one sentence: on the human's instruction, or to clear a `human_checkout` escalation
  the human hands it, run `nw queue main-dirty-apply --root <root> --json` and report the
  result to the human.
- The conductor gets **no** new permission: `DEFAULTS["conductor_disallowed_tools"]` gains
  `Bash(uv run nw * main-dirty-apply*)` and `Bash(nw * main-dirty-apply*)` (`*` matches
  `queue`, so these two cover every form; conductor-only, the steward's list is unchanged).
  `CONDUCTOR_CURSOR_POLICY` gains "Do not run `nw queue main-dirty-apply`."
  `skills/nw-conduct/SKILL.md` is unchanged as of this spec; SPEC-0544 later adds the
  `--trivial-only` form and owns that assertion from then on.

### Files the implementer may cherry-pick from `nw/IDEA-1159-20261110-02ed`

An optional source at branch tip `61876af9`, never the contract: where it and this spec differ,
this spec wins (guard order, the failure table, `escalate`, the WIP location).

- `src/nightwright/agentic/main_dirty.py` — `NotAWorktree`, `MainDirtyApplyError`, and the
  apply half from `_PATH_BAD` to the end: `_validate_repo_path`, `_wip_parent`,
  `_git_literal`, `_git_wip_literal`, `_precheck_index`, `_precheck_save`,
  `_allocate_wip_slot`, `_stage_binary_on_wip`, `_copy_paths_to_wip`, `_verify_wip_matches`,
  `_restore_paths_on_main`, `_wip_commit`, `_commit_trivial`, `_save_to_wip`, `_valid_base`,
  `_head_on_a_branch`, `_refuse`, `apply_clear`, `_clear_lock`, `_apply_locked`. Align the
  refusal for a sequencer with SPEC-0542's `in_progress` (one source of truth).
- `src/nightwright/agentic/cli_queue.py` — the `main-dirty-apply` command.
- `docs/product/reference/index.md` — the `main-dirty-apply` mention.
- Not the branch's `launch.py` allow/deny changes and not its `SKILL.md` text (SPEC-0544).

## Alternatives considered

- **Raw git allow-rules for the clear** (SPEC-0537's first design). Globs over `checkout HEAD
  --`, `rm -f --`, `update-index` could be bent (literal `*`, `-C`, `..`) and nothing ordered
  save-before-restore; the human approved the single verb instead.
- **Trust the `--plan` file.** A plan written minutes earlier can be wrong after another edit
  or a merge started; the verb re-plans and treats the file as a check only.
- **`git stash` instead of a WIP worktree.** A stash is not a branch and is easy to drop; the
  WIP branch is visible and matches SPEC-0542's `<wip>` name.

## Acceptance criteria

- [ ] AC1 Merge or sequencer in progress (`MERGE_HEAD`, a `U` status, `CHERRY_PICK_HEAD`,
      `REVERT_HEAD`, `rebase-merge/`), each run once with no `--plan` and once with a `--plan`
      claiming `commit` or `save_branch`: HEAD, index bytes, worktree bytes, and
      `git branch --list` are unchanged; `ok: false`, `escalate: true`, `error` names the
      operation, never `plan is stale`.
- [ ] AC2 A `--plan` whose `action` or `paths` differ from the live plan returns
      `error: plan is stale` and writes nothing; a plan `base` different from the project's is
      ignored.
- [ ] AC3 `commit`: the new commit's subject is exactly
      `chore: land stray trivial main-checkout edits`, its body lists each path on its own line,
      the returned `sha` equals `git rev-parse HEAD`, only the planned paths are in it, untracked
      files and an unplanned literal-`*` sibling are untouched, and main is tracked-clean. A
      failing pre-commit hook leaves the index bytes as before, `ok: false`, and `error`
      includes the hook's stderr.
- [ ] AC4 Save: for text, binary, deleted, newly added, mode-only (`chmod +x`) and exec-bit
      paths, the WIP commit's subject is `WIP: stray main-checkout edits (<class>)`, each WIP
      blob and mode equals the pre-clear main bytes and mode, main is tracked-clean afterwards,
      none of it is committed on the base, and the WIP worktree is under the configured worktree
      root (default `.nw-worktrees/<project>/main-dirty-*`; one case with a custom
      `worktrees.root`) on branch `wip/main-dirty-*`. Without `.nw/project.yaml` or its
      `project:` key a save returns `error: no project config to place the WIP worktree` with no
      write, while `commit` still works.
- [ ] AC5 Ordering (recorded argv): the WIP commit and the blob check precede every main
      `checkout HEAD --` / `rm -f --`; every main path call carries `--literal-pathspecs`
      (the test fails if the flag is removed).
- [ ] AC6 One test per row of the failure-phase table, each forced by a fault injected at that
      phase: (a) failed `worktree add`, (b) failed copy, (c) failing WIP-commit hook, (d) a blob
      mismatch at verify, (e) a re-verify failure on the second of three paths. Each asserts the
      table's WIP-worktree and WIP-branch state and result keys exactly, that main is
      byte-identical for every not-yet-restored path, that (c) carries the hook's stderr, and
      that (e) has the first path restored and its content on `wip_sha`.
- [ ] AC7 A name clash yields `-2`, then `-3`, for both branch and directory.
- [ ] AC8 Refusals with no write, each naming its fixture and `error`: a symlink path, a
      file→symlink and a symlink→file typechange, and a path under a symlinked directory (real
      repos) → `bad path <p>`; a directory (a gitlink) → `bad path <p>`; absolute path, `..`,
      and a path in neither HEAD, index nor worktree, by monkeypatching `plan_clear` to return
      it (defence in depth) → `bad path <p>`; a deleted tracked path with a literal `*` is
      accepted; a partially staged path → `partially staged <p>`; malformed `worktrees` in
      `.nw/project.yaml` → `ok: false` JSON; a second concurrent run → `clear already in
      progress`. Each refusal leaves the index bytes identical on a repo with stale stat data.
- [ ] AC9 `escalate` is asserted by a parametrised test over every (action, class) pair the plan
      can produce, success and refusal: `true` exactly for `substantive` and `merge`.
- [ ] AC10 `hold`: substantive dirt off the base is saved, `git switch --no-guess <base>` runs
      only when tracked-clean, `replanned` is returned and `escalate` is true; a failed switch
      keeps `wip_*` and reports `replanned.action: hold`; a failed save does not switch; an
      option-shaped or invalid `base`, a `base` with no `refs/heads/<base>`, and a detached HEAD
      on no branch are refused before any write; a base that is valid in the dirty
      `.nw/project.yaml` but option-shaped in HEAD's copy keeps the save and runs no `switch`.
- [ ] AC11 Untracked files are byte-identical after every action. `resume` on a clean checkout
      is `ok: true` and writes nothing. The recorded argv never contains `reset`, `clean`,
      `checkout -- .`, `rm -r`, `stash`, `push`, `branch -D`, `switch -f`, and every subprocess
      is `git` (never `nw`).
- [ ] AC12 CLI via `CliRunner`: `--json` exit 0 on success and on a refusal; exit 2 on a non-git
      root, a subdirectory root, and a `--plan` that is not a plan (including `paths` of `5`,
      `{"a": 1}`, `[null]`, a string); exit 3 when the root's `rev-parse --show-toplevel` fails
      for another reason (monkeypatched); text mode prints `main-dirty-apply ok|failed: <action>`.
- [ ] AC13 SPEC-0542 `recommended` is `["nw queue main-dirty-apply --root <R>"]` for `commit`,
      `save_branch`, `branch_and_escalate`, `hold`; the raw list when a planned path is a
      symlink or typechange; unchanged for `resume` / `escalate`. SPEC-0542's AC10 run-the-list
      tests now run the verb command through the CLI and assert the same end state.
- [ ] AC14 Conductor launch (`build_plan(cfg, "conductor")`) denies the two verb rules and the
      steward launch does not; `CONDUCTOR_CURSOR_POLICY` contains the new sentence; no other
      `launch.py` rule changes (existing `tests/test_agentic_launch.py` cases pass unchanged).
- [ ] AC15 `skills/nw-steward/SKILL.md` has the one sentence naming
      `nw queue main-dirty-apply --root`; as of this spec `skills/nw-conduct/SKILL.md` does not
      name `main-dirty-apply` (SPEC-0544 relaxes this to `--trivial-only` forms and replaces the
      assertion).

## Test plan

- **Automated tests:** new `tests/test_main_dirty_apply.py`, one marker
  `pytestmark = pytest.mark.fast`, temp git repos with real before/after snapshots (HEAD,
  index bytes, worktree bytes, branch list) and a git-argv recorder. Take from the branch's
  `tests/test_main_dirty_clear.py` (branch tip `61876af9`) every `test_apply_*` test, plus
  `test_commit_trivial_guard_when_replan_not_commit`, `test_commit_trivial_failure_unstages`,
  `test_commit_trivial_refuses_symlink`, `test_backslash_name_is_kept_and_saved`,
  `test_commit_failure_carries_git_stderr`, `test_hold_reads_base_after_the_restore`, the apply
  half of `test_cli_text_mode_plan_and_apply` / `test_cli_git_failures_have_messages_and_distinct_codes`
  / `test_root_must_be_checkout_top`. Tighten them to the ACs: AC3 asserts the full message;
  AC5's literal-pathspecs check is mutation-tested once by hand (recorded in As shipped); new
  tests for AC1's `--plan` precedence and `rebase-merge`/`REVERT_HEAD`, the AC6 phase table,
  AC8's symlink and typechange cases, AC9's `escalate` table, AC10's re-read base, AC13, AC14,
  AC15.
- **Existing modules:** `tests/test_agentic_launch.py` gains AC14 cases;
  `tests/test_main_dirty_plan.py` (SPEC-0542) updates its `recommended` expectations (AC13),
  and its AC10 tests that run the raw lists now run the `recommended` verb command via the CLI
  and assert the same end state (tracked-clean, edit on the base or on a `wip/main-dirty-*`
  branch, untracked file in place); the symlink case still runs the raw list.
- **Regression guard:** `python3 tools/affected_tests.py --run uv run pytest -q`,
  `python3 tools/test_tiers.py --check`, `python3 tools/spec_lint.py`.
- **Manual verification (real check):** scratch repo under the scratchpad with `.nw/project.yaml`
  (`project: Scratch`, `base: main`). (1) Edit `src/x.py` logic, add an untracked file, run
  `uv run nw queue main-dirty-apply --root <scratch> --json` → `ok: true`, `escalate: true`;
  `git -C <scratch> status --porcelain --untracked-files=no` empty; `git log -1
  wip/main-dirty-*` shows the edit; the untracked file is still there. (2) Frontmatter-only edit
  → `ok: true`, `sha` = HEAD, subject as AC3. (3) Start a conflicting merge → `ok: false`,
  nothing changed. Record the outputs in As shipped.

## Rollout / migration

Ships after SPEC-0542. No data move. Existing escalations keep their raw commands; new ones name
the verb. SPEC-0544 builds on this. The WIP worktree and branch are left for the human to inspect
and remove by hand (`git worktree remove`, `git branch -d`); `nw queue gc` lists the worktree as
`no campaign` and keeps it. A running conductor keeps its old deny list until
`nw queue conductor-launch` relaunches it (the rule change moves its `launch_hash`). A
launch-settings override of `conductor_disallowed_tools` replaces the default list, this deny
included, as it already does for `AskUserQuestion`; this spec does not change that.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed, including the scratch-repo check; affected tests pass.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped; As shipped records the AC5 mutation run (flag
      removed, test fails).
- [ ] `docs/LEDGER.md` regenerated; `python3 tools/spec_lint.py` exits 0.

## Review notes

Review of 2026-11-11 (`nw spec review SPEC-0543`, verdict `split`):

- **Guard order makes AC1 unsatisfiable (must_fix): fixed.** The in-progress refusal is now
  guard 3, before the `--plan` comparison; AC1 runs with and without a stale plan.
- **AC6 contradicts the save phases (must_fix): fixed.** A failure-phase table states the WIP
  worktree, branch and result keys per phase; AC6 has one test per row and no "empty branch"
  claim after the WIP commit.
- **Guard 4 refuses symlinks that SPEC-0542 routes to save (must_fix): fixed (option B).** The
  verb refuses symlink and typechange paths (path and every parent, worktree and HEAD), and
  `recommended` keeps the raw list for them, so no human is sent to a command that always
  refuses. Option A (writing mode-`120000` entries by hand) was rejected as the riskiest write
  in the verb for the rarest dirt.
- **WIP location undefined without a project config (major): fixed.** It is
  `P.worktree_root(doc, root, project)`; a save without the config refuses before any write.
- **`escalate` not defined for every action (major): fixed.** It is a function of the live
  class, stated once; AC9 tables it.
- **Split into three specs (major): rebutted.** The scope is the human's ESC-0067 split (part B,
  the human-run verb). The callers part cannot ship apart from the verb: the conductor deny must
  land in the same change that adds the verb, or `Bash(uv run nw *)` hands the conductor the
  verb, and `recommended` must not name a verb that is not there. Splitting save/restore from
  commit would ship a verb that refuses most of the classes it is recommended for. The three
  must_fix items were contradictions, now gone; with the phase table and the `escalate` rule
  the remaining ACs are test groups over one module.
- **Minors and nits: fixed.** AC8 names its fixtures and the monkeypatched defence-in-depth
  cases; `GIT_OPTIONAL_LOCKS=0` and "no write" defined; base re-read re-validated,
  `--no-guess`, `refs/heads/<base>` checked; failed-commit and `resume` ACs, exit-3 boundary;
  AC15 hands its skill assertion to SPEC-0544; SPEC-0542 AC10's successor named; deny rules
  reduced to the two covering globs, override and WIP-lifecycle notes in Rollout; branch tip
  pinned, line numbers dropped.

## Open questions

None.
