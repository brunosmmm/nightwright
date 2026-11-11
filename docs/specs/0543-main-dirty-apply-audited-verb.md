---
id: SPEC-0543
title: nw queue main-dirty-apply, the audited main-checkout clear (human or steward run)
status: accepted
owner: user
created: 2026-11-11
updated: 2026-11-11
kind: feature
tags: [agentic, queue, git]
depends_on: [SPEC-0542]
source_idea: IDEA-1159
---

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
  on the base (only when on the base); anything else is saved to a WIP branch under
  `.nw-worktrees` before the tracked paths are restored.
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
lock, re-plans with `plan_clear`, refuses on any mismatch or unsafe state, then performs the
live plan's action. SPEC-0542's `recommended` becomes `nw queue main-dirty-apply --root <R>`
for `commit`, `save_branch`, `branch_and_escalate`, and `hold`. The conductor launch denies the
verb.

## Design

### Interface

Output schema `nw.main_dirty_apply.v1`: `schema`, `root`, `action`, `ok`, and when set `sha`,
`wip_branch`, `wip_path`, `wip_sha`, `escalate`, `replanned` (`{on_base, action, class}`),
`error`. Exit 0 whenever the verb ran (including `ok: false`); exit 2 when `--root` is not the
top of a git worktree or `--plan` is not a `nw.main_dirty_plan.v1` object whose `paths` is a
list of strings; exit 3 when a git read fails before the verb starts. Text mode prints
`main-dirty-apply ok|failed: <action>` and the error. Every refusal or failed step is
`ok: false` JSON, never a traceback (`GitError`, `OSError`, `ValueError`, `TypeError`,
`AttributeError` are caught; a malformed `.nw/project.yaml` included).

### Guards, in order, before any write

1. **Lock:** a non-blocking `flock` on `<git-dir>/nw-main-dirty.lock`; a second concurrent run
   returns `ok: false`, `error: clear already in progress`.
2. **Re-plan:** `plan_clear(root)` on the live checkout. With `--plan`, its `action` and sorted
   `paths` must equal the live plan's, else `error: plan is stale`. `base` always comes from the
   project, never from the plan file.
3. **Operation in progress** (live `class: merge`, any `in_progress`): no write of any kind,
   `ok: false`, `escalate: true`, `error: <in_progress> in progress`.
4. **Paths:** each path is relative, has no `..` segment, is not a directory, and is not a
   symlink and has no symlink parent (worktree or HEAD). A path with glob characters is
   accepted only as that literal path. Else `error: bad path <p>`.
5. **Index:** a path whose index blob differs from both HEAD and the worktree (partially
   staged) is refused, `error: partially staged <p>`.
6. **Base:** for `hold`, `base` must pass `git check-ref-format --branch` and not start with
   `-`; a detached HEAD that no branch contains is refused (`error: detached HEAD not on any
   branch`).

Every git call passes `--literal-pathspecs` and puts paths after `--`.

### Actions

- **`commit`** (only reached when the live action is `commit`, so on the base): stage each
  planned path with `git add --`, commit with subject
  `chore: land stray trivial main-checkout edits` and one `-m` line per path, return `sha`.
  A failed commit unstages what it staged and returns git's stderr (hooks, identity) in
  `error`. Succeeds only when main is tracked-clean afterwards. Never `commit -a`, `add -A`.
- **`save_branch`, `branch_and_escalate`, `hold` with dirt:** allocate
  `wip/main-dirty-<YYYYMMDDThhmmssZ>` and
  `<repo>/../.nw-worktrees/<project>/main-dirty-<ts>` (the `project.py` `worktree_root` parent),
  suffix `-2`, `-3`, … when either exists. `git worktree add -b <wip> <path> HEAD`. Copy each
  text path's bytes and mode; a binary path through `git hash-object -w` +
  `git update-index --add --cacheinfo`; a deletion through `git rm -f --` in the WIP tree.
  Commit there with subject `WIP: stray main-checkout edits (<class>)` and one `-m` line per
  path. Verify each path's blob and mode **from the WIP commit** against main's worktree (absent
  for a deletion). Only then restore main path by path, re-verifying that path just before:
  `git checkout HEAD -- <p>` when in HEAD, else `git rm -f -- <p>`. A failure before the WIP
  commit removes the WIP worktree and its empty branch (`git branch -d`) and leaves main
  byte-identical; a failure after it keeps the branch and returns `wip_branch`/`wip_sha` in the
  error result. Success requires main tracked-clean and the WIP commit present.
  `branch_and_escalate` returns `escalate: true`.
- **`hold`:** the save above (if dirty), then `git switch <base>` only when tracked-clean
  (never `-f`, `--discard-changes`, `-C`), re-plan, return `replanned`. A failed switch still
  returns `wip_*` and `escalate` (substantive). `base` is read again after the restore.
- **`resume`:** `ok: true` when tracked-clean, no write. **`escalate`:** see guard 3.

Never run, asserted on recorded argv: `reset`, `clean`, `checkout -- .`, `checkout .`,
`rm -r`, `stash`, `push`, `branch -D`, `switch -f`, and any non-`git` command (no `nw`).

### Callers and permissions

- `recommended` in `plan_clear` (SPEC-0542) for `commit`, `save_branch`,
  `branch_and_escalate`, `hold` becomes `["nw queue main-dirty-apply --root <R>"]` (quoted).
  The SPEC-0542 escalation then tells the human to run that one command.
- The steward already holds `Bash(nw *)` / `Bash(uv run nw *)`; `skills/nw-steward/SKILL.md`
  gains one sentence: on the human's instruction, or to clear a `human_checkout` escalation
  the human hands it, run `nw queue main-dirty-apply --root <root> --json` and report the
  result to the human.
- The conductor gets **no** new permission: `DEFAULTS["conductor_disallowed_tools"]` gains
  `Bash(uv run nw queue main-dirty-apply*)`, `Bash(nw queue main-dirty-apply*)`,
  `Bash(uv run nw * main-dirty-apply*)`, `Bash(nw * main-dirty-apply*)` (conductor-only; the
  steward's list is unchanged). `CONDUCTOR_CURSOR_POLICY` gains "Do not run
  `nw queue main-dirty-apply`." `skills/nw-conduct/SKILL.md` is unchanged.

### Files the implementer may cherry-pick from `nw/IDEA-1159-20261110-02ed`

- `src/nightwright/agentic/main_dirty.py` — `NotAWorktree`, `MainDirtyApplyError`, and the
  apply half from `_PATH_BAD` (line 512) to the end: `_validate_repo_path`, `_wip_parent`,
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
      `REVERT_HEAD`, `rebase-merge/`), with a `--plan` claiming `commit` or `save_branch`: HEAD,
      index bytes, worktree bytes, and `git branch --list` are unchanged; `ok: false`,
      `escalate: true`, `error` names the operation.
- [ ] AC2 A `--plan` whose `action` or `paths` differ from the live plan returns
      `error: plan is stale` and writes nothing; a plan `base` different from the project's is
      ignored.
- [ ] AC3 `commit`: the new commit's subject is exactly
      `chore: land stray trivial main-checkout edits`, its body lists each path on its own line,
      the returned `sha` equals `git rev-parse HEAD`, only the planned paths are in it, untracked
      files and an unplanned literal-`*` sibling are untouched, and main is tracked-clean.
- [ ] AC4 Save: for text, binary, deleted, newly added, and exec-bit paths, the WIP commit's
      subject is `WIP: stray main-checkout edits (<class>)`, each WIP blob and mode equals the
      pre-clear main bytes and mode, main is tracked-clean afterwards, and the WIP worktree is
      under `.nw-worktrees/<project>/main-dirty-*` on branch `wip/main-dirty-*`.
- [ ] AC5 Ordering (recorded argv): the WIP commit and the blob check precede every main
      `checkout HEAD --` / `rm -f --`; every main path call carries `--literal-pathspecs`
      (the test fails if the flag is removed).
- [ ] AC6 A forced copy mismatch, a failed `worktree add`, a failed WIP commit (hook), and a
      per-path re-verify failure each leave main byte-identical for every not-yet-restored path,
      return `ok: false` with `error` (git stderr included for a hook), remove the WIP worktree,
      and delete an empty WIP branch; a failure after the WIP commit returns `wip_branch` and
      `wip_sha`.
- [ ] AC7 A name clash yields `-2`, then `-3`, for both branch and directory.
- [ ] AC8 Refusals with no write: absolute path, `..`, a directory, a nonexistent glob path, a
      symlink or a path under a symlinked directory, a partially staged path, malformed
      `worktrees` in `.nw/project.yaml` (`ok: false` JSON), a second concurrent run (lock).
- [ ] AC9 Typechange and mode-only dirt are saved to the WIP branch, never committed on the
      base (they are `other` per SPEC-0542).
- [ ] AC10 `hold`: substantive dirt off the base is saved, the switch to `base` happens only when
      tracked-clean, `replanned` is returned and `escalate` is true; a failed switch keeps
      `wip_*` and `escalate` and reports `replanned.action: hold`; a failed save does not
      switch; an option-shaped or invalid `base` and a detached HEAD on no branch are refused
      before any write.
- [ ] AC11 Untracked files are byte-identical after every action. The recorded argv never
      contains `reset`, `clean`, `checkout -- .`, `rm -r`, `stash`, `push`, `branch -D`,
      `switch -f`, and every subprocess is `git` (never `nw`).
- [ ] AC12 CLI via `CliRunner`: `--json` exit 0 on success and on a refusal; exit 2 on a non-git
      root, a subdirectory root, and a `--plan` that is not a plan (including `paths` of `5`,
      `{"a": 1}`, `[null]`, a string); exit 3 on a failed git read; text mode prints
      `main-dirty-apply ok|failed: <action>`.
- [ ] AC13 SPEC-0542 `recommended` is `["nw queue main-dirty-apply --root <R>"]` for `commit`,
      `save_branch`, `branch_and_escalate`, `hold`, and unchanged for `resume` / `escalate`.
- [ ] AC14 Conductor launch (`build_plan(cfg, "conductor")`) denies the four verb rules and the
      steward launch does not; `CONDUCTOR_CURSOR_POLICY` contains the new sentence; no other
      `launch.py` rule changes (existing `tests/test_agentic_launch.py` cases pass unchanged).
- [ ] AC15 `skills/nw-steward/SKILL.md` has the one sentence naming
      `nw queue main-dirty-apply --root`; `skills/nw-conduct/SKILL.md` still never names
      `main-dirty-apply`.

## Test plan

- **Automated tests:** new `tests/test_main_dirty_apply.py`, one marker
  `pytestmark = pytest.mark.fast`, temp git repos with real before/after snapshots (HEAD,
  index bytes, worktree bytes, branch list) and a git-argv recorder. Take from the branch's
  `tests/test_main_dirty_clear.py` every `test_apply_*` test (lines 753–1700), plus
  `test_commit_trivial_guard_when_replan_not_commit`, `test_commit_trivial_failure_unstages`,
  `test_commit_trivial_refuses_symlink`, `test_backslash_name_is_kept_and_saved`,
  `test_commit_failure_carries_git_stderr`, `test_hold_reads_base_after_the_restore`, the apply
  half of `test_cli_text_mode_plan_and_apply` / `test_cli_git_failures_have_messages_and_distinct_codes`
  / `test_root_must_be_checkout_top`. Tighten them to the ACs: AC3 asserts the full message;
  AC5's literal-pathspecs check is mutation-tested once by hand; new tests for AC1's
  `rebase-merge`/`REVERT_HEAD`, AC9, AC13, AC14, AC15.
- **Existing modules:** `tests/test_agentic_launch.py` gains AC14 cases;
  `tests/test_main_dirty_plan.py` (SPEC-0542) updates its `recommended` expectations (AC13).
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
the verb. SPEC-0544 builds on this.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed, including the scratch-repo check; affected tests pass.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` regenerated; `python3 tools/spec_lint.py` exits 0.

## Open questions

None.
