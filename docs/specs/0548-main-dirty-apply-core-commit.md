---
id: SPEC-0548
title: nw queue main-dirty-apply core, guards and the trivial commit (human or steward run)
status: done
owner: user
created: 2026-11-11
updated: 2026-11-11
kind: feature
tags: [agentic, queue, git]
depends_on: [SPEC-0547]
supersedes: [SPEC-0543]
source_idea: IDEA-1159
---

## Context

The human approved replacing raw git allow-rules with **one audited verb,
`nw queue main-dirty-apply`** (campaign `IDEA-1159-20261110-02ed`, resume note seq 437: "the
verb itself must refuse a merge in progress, escalate substantive diffs, and save non-trivial
diffs to a WIP branch before restoring"). At ESC-0067 the human chose to split
[SPEC-0537](./0537-conductor-must-clear-main-dirty-itself-a-stray.md); the steward's part B,
[SPEC-0543](./0543-main-dirty-apply-audited-verb.md), was then sized `xl` by spec review
(verdict `split`, 15 ACs, critical tone) and is split here into two.

The chain, in queue order: [SPEC-0546](./0546-main-dirty-plan-classifier.md) (classifier),
[SPEC-0547](./0547-main-dirty-conductor-owned-plan-and-escalate.md) (owner and escalation),
**SPEC-0548 (this spec: verb core)**, [SPEC-0549](./0549-main-dirty-apply-wip-save-and-hold.md)
(verb WIP save and `hold`), [SPEC-0550](./0550-conductor-git-guard-hook.md) (git-guard hook),
[SPEC-0551](./0551-conductor-runs-main-dirty-apply-trivial-only.md) (conductor trivial-only).

This spec ships the verb with its guards, `commit`, `resume` and `escalate`, its CLI, and the
conductor deny. The save actions refuse with no write until SPEC-0549. `recommended` names the
verb only for `commit`, so a human is never sent to a mode that does not exist yet. The verb is
run by the human or a steward; the conductor is denied it.

Review majors on SPEC-0537's branch this spec pins: the verb trusted the caller's plan file, did
not guard a sequencer in progress, had no CLI test, and its tests passed with a wrong commit
message and without `--literal-pathspecs`.

**Carries SPEC-0543 AC1, AC2 (merged as AC1), AC3 (AC2), AC8 (AC3; its malformed-`worktrees`
case moves to SPEC-0549 AC1, the only place `worktrees` is read), AC11 (AC4), AC12 (AC5), AC14
and AC15 (merged as AC7).** AC6 is new, from the review's split: `recommended` for `commit`
only. It folds in the SPEC-0543 review findings that touch the core: a named index-restore
mechanism for a failed commit, a staged-content check before the commit, `recommended` gated
by a shared refusal predicate, the exact existing launch tests that change, `action`/`escalate`
when no live plan exists, and text-mode exit 1.

## Decision

`apply_clear(root, plan=None, base=None) -> dict` in `src/nightwright/agentic/main_dirty.py` and
`nw queue main-dirty-apply --root PATH [--base BRANCH] [--plan FILE] [--json]` in
`cli_queue.py` (`--base` as in SPEC-0546's `main-dirty-plan`; the CLI passes it through as
`apply_clear(..., base=B)`, which forwards it to `plan_clear`). It takes a
lock, re-plans with `plan_clear`, refuses an operation in progress before anything else, then a
stale `--plan`, unsafe paths and partial staging, and only then performs the live plan's action.
`commit`, `resume` and `escalate` are implemented; `save_branch`, `branch_and_escalate` and
`hold` return `ok: false`, `error: save not supported yet: <action>` with no write. `escalate`
is set from the live class alone. The conductor launch denies the verb.

## Design

### Interface

Output schema `nw.main_dirty_apply.v1`: `schema`, `root`, `action`, `ok`, `escalate` (both
always present), and when set `sha`, `error`. SPEC-0549 adds `wip_branch`, `wip_path`,
`wip_sha`, `replanned`. Text mode prints `main-dirty-apply ok|failed: <action>` and the error.

Exit codes: with `--json`, 0 whenever `apply_clear` ran (including `ok: false`); in text mode, 0
when `ok` is true and 1 when it is false, so `… && next` stops on a refusal. Exit 2 when `--root`
is not the top of a git worktree (real paths compared, as SPEC-0546), or `--plan` is
unreadable (missing file), not JSON, not a `nw.main_dirty_plan.v1` object, lacks a string
`action`, or has `paths` that is not a list of strings; exit 3 only when the CLI's own
`git rev-parse --show-toplevel` of the root fails with something other than "not a git
repository". Each exit-2 case prints a one-line message, never a traceback. The CLI runs its own
git calls with `LC_ALL=C`, so the "not a git repository" test is locale-independent. Once `apply_clear` runs, every refusal or failed step is `ok: false` JSON, never a
traceback (`GitError`, `OSError`, `ValueError`, `TypeError`, `AttributeError` are caught; a
malformed `.nw/project.yaml` included).

**`escalate`** is a function of the live class only: `true` when the live `class` is
`substantive` or `merge`; `false` for `trivial`, `branch` and `clean`. When no live plan exists
(lock failure, or `plan_clear` raised), `action` is `"none"` and `escalate` is `false`.

### Guards, in order, before any write

Every git call before the first write runs with `GIT_OPTIONAL_LOCKS=0`, so no guard refreshes
the index. The lock file is the only file created before the first write; "no write" means no
change to HEAD, the index, the worktree or any ref.

1. **Lock:** a non-blocking `flock` on `<git-dir>/nw-main-dirty.lock`; a second concurrent run
   returns `ok: false`, `error: clear already in progress`.
2. **Re-plan:** `plan_clear(root, base=base)` on the live checkout (the `base` argument is the
   CLI's `--base`, `None` when not given). Everything below uses the live plan; `base` never comes from a plan file.
3. **Operation in progress** (live `class: merge`): no write, `ok: false`, `escalate: true`,
   `error: <in_progress> in progress`. This runs before the `--plan` comparison, whatever the
   plan file says (a merge that started after the plan was written is the main reason the verb
   re-plans).
4. **Plan check:** with `--plan`, its `action` and sorted `paths` must equal the live plan's,
   else `error: plan is stale`.
5. **Paths:** each live path is relative, has no `..` segment, and is not a directory (a gitlink
   is one); neither the path nor any parent directory is a symlink, in the worktree or in HEAD
   (mode `120000`). A path with glob characters is accepted only as that literal path; a path
   that is in neither HEAD nor the worktree nor the index is refused. Else `error: bad path
   <p>`. Symlink and typechange dirt is refused: the verb never writes a symlink.
6. **Index:** a path whose index blob differs from both HEAD and the worktree (partially staged)
   is refused, `error: partially staged <p>`.

Guards 5 and 6 are one read-only function, `verb_refusal(root, plan) -> str | None` (the error
text, or `None`), used by `apply_clear` and by `recommended` (below). SPEC-0549 adds guard 7 to
it.

Every git call passes `--literal-pathspecs` and puts paths after `--`.

### Actions

- **`commit`** (only reached when the live action is `commit`, so on the base): copy the bytes
  of `.git/index` (resolved with `git rev-parse --git-path index`) to memory; record
  `git hash-object -- <p>` for each planned worktree path (no `-w`: writes nothing). Stage each
  planned path with `git add --literal-pathspecs -- <p>`. Before committing, check that
  `git diff --cached --name-only -z` equals the planned paths and that each staged blob
  (`git ls-files -s -z -- <p>`) equals the recorded hash (a trivial plan has no deletion: a
  delete of a non-code path is `other`, so the action is `save_branch`); on a mismatch,
  restore the index and return `error: changed since plan <p>`. Commit with subject
  `chore: land stray trivial main-checkout edits` and one `-m` line per path; return `sha`. A
  failed commit (hook, identity) restores the index and returns git's stderr in `error`.
  **Restoring the index** creates `<index>.lock` with `O_EXCL`, writes the saved bytes with the
  index's mode (not `mkstemp`'s 0600), and `os.replace`s the lock over the index, so the index is
  byte-identical to before; no `reset` or `restore` runs. If the lock exists (a concurrent git
  process), return `ok: false`, `error: index locked, not restored`. The restore runs only when
  `git rev-parse HEAD` equals the pre-add HEAD; if HEAD moved (a commit landed although `git
  commit` exited non-zero), return `ok: false`, `sha` = the new HEAD, and leave the index alone.
  A commit that lands but leaves main not tracked-clean (a hook rewrote a file) is `ok: false`
  with `sha` and `error: main not tracked-clean after commit`; the index is not restored.
  Succeeds only when main is tracked-clean afterwards. Never `commit -a`, `add -A`.
- **`resume`:** `ok: true` when tracked-clean, no write.
- **`escalate`:** see guard 3.
- **`save_branch`, `branch_and_escalate`, `hold`:** after the guards, `ok: false`,
  `error: save not supported yet: <action>`, `escalate` per class, no write (SPEC-0549).

Never run, asserted on recorded argv: `reset`, `restore`, `clean`, `checkout`, `rm -r`,
`stash`, `push`, `branch -D`, `switch`, and any non-`git` command (no `nw`).

### `recommended` (in SPEC-0546's `plan_clear`)

For `action: commit`, `recommended` becomes `["nw queue main-dirty-apply --root <R>"]` (root
quoted with `shlex.quote`; ` --base <B>` appended when the plan was made with `--base`) when `verb_refusal(root, plan)` is `None`; otherwise it stays
SPEC-0546's raw list, so the human is never told to run a command that refuses. Every other
action keeps SPEC-0546's list. SPEC-0547's escalation then tells the human to run that one
command.

Guard 5 cannot refuse a real `commit` plan (symlink, typechange and gitlink dirt is never
`trivial`, so the action is never `commit`); only guard 6 can, so the real-fixture test is the
partially staged path. Guard 5's refusals are reachable through the save actions, whose
`recommended` stays raw in this spec, and through a monkeypatched plan.

### Callers and permissions

- The steward already holds `Bash(nw *)` / `Bash(uv run nw *)`; `skills/nw-steward/SKILL.md`
  gains one sentence: on the human's instruction, or to clear a `human_checkout` escalation the
  human hands it, run `nw queue main-dirty-apply --root <root> --json` and report the result to
  the human.
- The conductor gets **no** new permission: `DEFAULTS["conductor_disallowed_tools"]` becomes
  `["AskUserQuestion", "Bash(uv run nw queue main-dirty-apply*)", "Bash(nw queue main-dirty-apply*)"]`
  (conductor-only, the steward's list is unchanged). The verb token is anchored at the
  subcommand position: the repo's matcher lets `*` match any characters including spaces, so an
  unanchored `* main-dirty-apply*` would also deny SPEC-0547's `nw queue escalate … --fact 'nw
  queue main-dirty-apply --root <R>' …` command. `nw` takes no global option before `queue`;
  forms the anchored glob misses (a wrapper, a changed word order) are SPEC-0550's hook backstop. `CONDUCTOR_CURSOR_POLICY` gains "Do not run `nw queue main-dirty-apply`."
  `skills/nw-conduct/SKILL.md` is unchanged; SPEC-0551 adds the `--trivial-only` form.
- Existing tests in `tests/test_agentic_launch.py` that encode the old conductor default change
  to the new list and nothing else: the module constant `CONDUCTOR_DENY`,
  `test_default_plan_argv_and_env` (the conductor's exact `--disallowedTools` argv),
  `test_permission_config_replaces_defaults_and_steward_inherits` (the conductor branch's
  `["Bash(rm *)", "AskUserQuestion"]` expectation), and
  `test_conductor_guard_settings_override_and_bad_value` (the non-list fallback to the
  default). Steward assertions stay byte-identical.

### Files the implementer may cherry-pick from `nw/IDEA-1159-20261110-02ed`

Optional source (tip `61876af9`), never the contract; where it differs, this spec wins (guard
order, `escalate`, the index restore).

- `src/nightwright/agentic/main_dirty.py`: `NotAWorktree`, `MainDirtyApplyError`,
  `_validate_repo_path`, `_git_literal`, `_precheck_index`, `_commit_trivial`, `_refuse`,
  `apply_clear`, `_clear_lock`, `_apply_locked`. Not the `_wip_*`, `_save_*`, `_restore_*`,
  `_valid_base`, `_head_on_a_branch` helpers (SPEC-0549).
- `src/nightwright/agentic/cli_queue.py`: the `main-dirty-apply` command.
- Not the branch's `launch.py` allow/deny changes and not its `SKILL.md` text.

## Alternatives considered

- **Raw git allow-rules for the clear** (SPEC-0537's first design). Globs over `checkout HEAD
  --`, `rm -f --`, `update-index` could be bent (literal `*`, `-C`, `..`); the human approved the
  single verb instead.
- **Ship the whole verb in one spec** (SPEC-0543's rebuttal). Rejected on re-review: 15 ACs.
  The rebuttal's constraints still hold here: the deny lands with the verb, and `recommended`
  names only the mode that exists.
- **Trust the `--plan` file.** A plan written minutes earlier can be wrong after another edit or
  a merge started; the verb re-plans and treats the file as a check only.
- **Unstage with `git restore --staged`** after a failed commit. It rewrites index stat data, so
  the index cannot be byte-identical; the saved-bytes restore can.

## Acceptance criteria

- [x] AC1 (SPEC-0543 AC1 + AC2) Merge or sequencer in progress (`MERGE_HEAD`, each of
      `UU AA DD AU UA DU UD` as a status code (one run is enough for the code→merge mapping, one
      for `DD`), `CHERRY_PICK_HEAD`, `REVERT_HEAD`, `rebase-merge/`, `rebase-apply/`, and a merge
      in a linked worktree), each run once with no `--plan` and once
      with a `--plan` claiming `commit` or `save_branch`: HEAD, index bytes, worktree bytes, and
      `git branch --list` are unchanged; `ok: false`, `escalate: true`, `error` names the
      operation, never `plan is stale`. A `--plan` whose `action` or `paths` differ from the live
      plan returns `error: plan is stale` and writes nothing; a plan `base` different from the
      project's is ignored.
- [x] AC2 (SPEC-0543 AC3) `commit`: the new commit's subject is exactly
      `chore: land stray trivial main-checkout edits`, its body lists each path on its own line,
      the returned `sha` equals `git rev-parse HEAD`, only the planned paths are in it, untracked
      files and an unplanned literal-`*` sibling are untouched, and main is tracked-clean. A
      failing pre-commit hook leaves the index bytes as before, `ok: false`, and `error` includes
      the hook's stderr. A planned file modified between the re-plan and the `add`
      (monkeypatched hook point) returns `error: changed since plan <p>`, no commit, index bytes
      as before. Index-restore outcomes, one test each: a held `<index>.lock` when a failed
      commit needs the restore → `ok: false`, `error: index locked, not restored`, the lock file
      and its holder's bytes untouched; a `git commit` that lands a commit but exits non-zero
      (post-commit-style fault, monkeypatched) → `ok: false`, `sha` equals the new
      `git rev-parse HEAD`, the index not rewritten; a commit that lands and a hook then
      rewrites a tracked file → `ok: false`, `sha` set, `error: main not tracked-clean after
      commit`. A restored index has the original file mode.
- [x] AC3 (SPEC-0543 AC8) Refusals with no write, in five lettered groups, each its own
      parametrized test (each case names its fixture and exact `error`/`verb_refusal`):
      - **3a bad paths:** a symlink path, a file→symlink and a symlink→file typechange, and a
        path under a symlinked directory (real repos) → `bad path <p>`; a directory (a
        gitlink) → `bad path <p>`; absolute path, `..`, and a path in neither HEAD, index nor
        worktree, by monkeypatching `plan_clear` to return it → `bad path <p>`; a deleted
        tracked path named `a*b.md` passes guards 5–6 (`verb_refusal` is `None`) and
        `apply_clear` returns `save not supported yet: save_branch`, not `bad path`.
      - **3b partial staging:** a partially staged path → `partially staged <p>`.
      - **3c config and lock:** an unparseable `.nw/project.yaml` → `ok: false` JSON with
        `action: "none"`, `escalate: false`; a second concurrent run →
        `error: clear already in progress`, `action: "none"`, `escalate: false`.
      - **3d save refusals:** `save_branch`, `branch_and_escalate` and `hold` →
        `save not supported yet: <action>` with `escalate` per the live class (true only for
        `substantive` or `merge`, false for `trivial`, `branch` and `clean`).
      - **3e index bytes:** every refusal in 3a–3d leaves the index bytes identical on a repo
        with stale stat data (the guards run with `GIT_OPTIONAL_LOCKS=0`).
- [x] AC4 (SPEC-0543 AC11) Untracked files are byte-identical after every action. `resume` on a
      clean checkout is `ok: true` and writes nothing. The recorded argv never contains `reset`,
      `restore`, `clean`, `checkout`, `rm -r`, `stash`, `push`, `branch -D`, `switch`, and every
      subprocess is `git` (never `nw`). Every main path call carries `--literal-pathspecs` (the
      test fails if the flag is removed).
- [x] AC5 (SPEC-0543 AC12) CLI via `CliRunner`: `--json` exit 0 on success and on a refusal;
      text mode exit 0 on success and exit 1 on a refusal, printing
      `main-dirty-apply ok|failed: <action>`; exit 2 with a one-line message (no traceback) on a
      non-git root, a subdirectory root, and a `--plan` that is not a plan: `paths` of `5`,
      `{"a": 1}`, `[null]`, a string; invalid JSON; a missing file; `{"action": 5, "paths": []}`;
      exit 3 when the root's `rev-parse --show-toplevel` fails for another reason
      (monkeypatched), and the non-git-root exit 2 holds with `LANG` set to a non-English locale
      (the CLI's git calls use `LC_ALL=C`). `--base develop` reaches `plan_clear` (project config
      without `base`, checkout on `develop`, trivial edit → `ok: true`); without `--base` the
      same checkout gives `ok: false`, `error: save not supported yet: hold`. A test fails if
      `base` is not forwarded to `plan_clear`.
- [x] AC6 (new) `recommended` for `commit` is `["nw queue main-dirty-apply --root <R>"]` when
      `verb_refusal` is `None`, and SPEC-0546's raw list for a trivial fixture with a partially
      staged path (real git, guard 6) and one where `plan_clear`'s plan is monkeypatched to
      `action: commit` with a path under a symlinked directory (guard 5; a real symlink or
      gitlink fixture is never `commit`). `recommended` carries ` --base <B>` exactly when
      `plan_clear` was called with `base`, and the root is quoted with `shlex.quote` (a fixture
      root containing a space). Every other action's `recommended` equals SPEC-0546's
      list. SPEC-0546's AC5 run-the-list test for `commit` now runs the verb command through the
      CLI and asserts the same end state.
- [x] AC7 (SPEC-0543 AC14 + AC15) Conductor launch (`build_plan(cfg, "conductor")`) denies the
      two verb rules and the steward launch does not. With `_permitted(cmd, allow, deny)` on the
      conductor plan, `uv run nw queue main-dirty-apply --root /x --json` and
      `nw queue main-dirty-apply --root /x` are denied, while `uv run nw queue escalate --topic
      human_checkout --question q --fact 'nw queue main-dirty-apply --root /x' --yes` and
      `uv run nw queue main-dirty-plan --root /x --json` are permitted. `CONDUCTOR_CURSOR_POLICY` contains the new
      sentence; only the four named `tests/test_agentic_launch.py` sites change and every other
      case passes unchanged. `skills/nw-steward/SKILL.md` has the one sentence naming
      `nw queue main-dirty-apply --root`; `skills/nw-conduct/SKILL.md` does not name
      `main-dirty-apply` (SPEC-0551 replaces this assertion).

## Test plan

- **Automated tests:** new `tests/test_main_dirty_apply.py`, one marker
  `pytestmark = pytest.mark.fast`, temp git repos with real before/after snapshots (HEAD, index
  bytes, worktree bytes, branch list) and a git-argv recorder. Take from the branch's
  `tests/test_main_dirty_clear.py` (tip `61876af9`) the non-save `test_apply_*` tests,
  `test_commit_trivial_guard_when_replan_not_commit`, `test_commit_trivial_failure_unstages`
  (now asserting index bytes), `test_commit_trivial_refuses_symlink`,
  `test_commit_failure_carries_git_stderr`, and the apply half of
  `test_cli_text_mode_plan_and_apply` / `test_cli_git_failures_have_messages_and_distinct_codes`
  / `test_root_must_be_checkout_top`. Tighten to the ACs: AC2 asserts the full message; new
  tests for AC1's `--plan` precedence and each operation marker (including `rebase-apply/`, the
  seven `U` codes and the linked worktree), AC2's changed-since-plan case and its three
  index-restore tests (`test_restore_index_locked`, `test_restore_skipped_when_head_moved`,
  `test_commit_lands_but_main_dirty`), AC5's text-mode exit 1, the malformed `--plan` forms, the
  `LC_ALL=C` non-English-locale case and `test_cli_base_forwarded_to_plan_clear`, and AC6
  (`test_recommended_base_suffix_and_quoting`, `test_recommended_raw_when_partially_staged`,
  `test_recommended_raw_when_monkeypatched_symlink_parent`). AC3 is one parametrized test per
  letter: `test_refusal_bad_paths` (3a), `test_refusal_partially_staged` (3b),
  `test_refusal_config_and_lock` (3c), `test_refusal_save_actions_not_supported` (3d),
  `test_refusals_leave_index_bytes` (3e).
- **Conductor deny:** `tests/test_agentic_launch.py` adds `test_conductor_deny_anchored_verb`,
  asserting the four `_permitted` outcomes of AC7.
- **Speed tier:** `tests/test_main_dirty_apply.py` carries the module marker
  `pytestmark = pytest.mark.fast` (no TUI, remote or `_e2e` name, so `fast`);
  `python3 tools/test_tiers.py --check` exits 0.
- **Existing modules:** `tests/test_agentic_launch.py` gains AC7 cases and edits the four named
  sites; `tests/test_main_dirty_plan.py` (SPEC-0546) updates its `commit` `recommended`
  expectation and its run-the-list test (AC6).
- **Mutation check (by hand, once):** remove `--literal-pathspecs` from the commit path; AC4
  fails. Recorded in As shipped.
- **Regression guard:** `python3 tools/affected_tests.py --run uv run pytest -q`,
  `python3 tools/test_tiers.py --check`, `python3 tools/spec_lint.py`.
- **Manual verification (real check):** scratch repo under the scratchpad with
  `.nw/project.yaml` (`project: Scratch`, `base: main`). (1) Frontmatter-only edit plus an
  untracked file → `uv run nw queue main-dirty-apply --root <scratch> --json` gives `ok: true`,
  `sha` = HEAD, subject as AC2, the untracked file still there. (2) Edit `src/x.py` logic → `ok:
  false`, `save not supported yet: branch_and_escalate`, `escalate: true`, nothing changed.
  (3) Start a conflicting merge → `ok: false`, nothing changed. Record the outputs in As shipped.

### Test record (2026-11-11)

- `uv run pytest -q tests/test_main_dirty_apply.py` (new, `pytest.mark.fast`): 91 cases, all
  pass. They cover AC1 (every operation marker, the seven `U` codes, a real conflict, a linked
  worktree, `--plan` precedence), AC2 (message, body, untracked/sibling untouched, hook failure,
  changed-since-plan, the three index-restore tests, file mode), AC3a-3e, AC4 (argv recorder),
  AC5 (CLI, malformed `--plan` forms, locale, exit 3, `--base`) and AC6.
- `tests/test_agentic_launch.py` (four named sites edited, `test_conductor_deny_anchored_verb`
  added), `tests/test_main_dirty_plan.py` (commit `recommended`, run-the-list through the CLI,
  and the docs assertion), plus the related launch, queue, dashboard, skills and rich-output
  modules: 1453 passed, 2 skipped. `python3 tools/test_tiers.py --check` exits 0;
  `python3 tools/spec_lint.py` exits 0.
- Mutation (by hand, once): `--literal-pathspecs` removed from `_lg` (the verb's git wrapper);
  `test_recorded_git_argv` and `test_commit_trivial` failed; restored.
- Manual scratch repo (`.nw/project.yaml` `project: Scratch`, `base: main`): (1) frontmatter edit
  plus an untracked file: `ok: true`, `sha` = HEAD, subject `chore: land stray trivial
  main-checkout edits`, `untracked.txt` still there. (2) `src/x.py` logic edit: `ok: false`,
  `save not supported yet: branch_and_escalate`, `escalate: true`, tree unchanged. (3) conflicting
  merge: `ok: false`, `error: merge in progress`, `escalate: true`, tree unchanged.
- The full suite was left to gating; `tools/affected_tests.py --run` selected too many modules to
  finish in the run's time limit, so the related modules above were run instead.

### As shipped (2026-11-11)

- Code: `apply_clear`, `verb_refusal`, `_commit_trivial`, `_restore_index` and the flock in
  `src/nightwright/agentic/main_dirty.py` (written fresh to Design, not cherry-picked from the
  branch); `main-dirty-apply` in `cli_queue.py`; the anchored deny, cursor policy sentence in
  `launch.py`; one steward-skill sentence; docs lines in `reference/index.md`, `loop/operate.md`
  and `capabilities/agentic.md`.
- The git calls go through `_git` (`LC_ALL=C`, `GIT_OPTIONAL_LOCKS=0`) and `_lg`, which adds
  `--literal-pathspecs`. `plan_clear`'s own read-only calls keep SPEC-0546's argv.
- `_stage` is the hook point for the changed-since-plan test (hashes are recorded just before it).
- `--base` is forwarded as written, but `plan_clear` still ignores it when `--root` has a
  `.nw/project.yaml` (SPEC-0546: the project's base wins). AC5's `--base develop` case therefore
  uses a repo with no `.nw/project.yaml`; a config that lacks `base` still defaults to `main`.
- A dirty gitlink gives action `escalate` with class `branch` or `substantive`: guard 5 refuses it
  as `bad path`, so it never reaches the verb's fallback `nothing to apply: escalate`.
- `tests/test_main_dirty_plan.py`: every commit `recommended` expectation is now the verb command;
  `_run_list` runs a `nw ...` command through the CLI of this tree. SPEC-0547's docs assertion
  (`main-dirty-apply` absent from operate.md / agentic.md) is inverted, as this spec's Definition
  of done requires; the conductor skill still does not name the verb.

## Goals / Non-goals

**Goals**

- The human or a steward clears a trivial dirty main checkout with one audited command,
  `nw queue main-dirty-apply`, whose guards refuse every unsafe case without writing.
- The conductor cannot run that command, yet can still escalate with its text as a `--fact`.

**Non-goals**

- WIP save, restore, `hold`, guard 7 (base and WIP location) and the failure-phase table
  (SPEC-0549); `recommended` for the save actions stays SPEC-0546's raw list until then.
- Any conductor permission other than the explicit deny; no raw-git allow/deny change for
  anyone. The conductor's skill row is not changed (SPEC-0551).
- The verb sends no escalation, mail, or resume. Untracked files are never touched. No push.

## Rollout / migration

Ships after SPEC-0547. No data move. SPEC-0543 is superseded by this spec alone (its one
`superseded_by`); SPEC-0549 lists only `depends_on: [SPEC-0548]` and names SPEC-0543 in its
Context text, not in `supersedes`. Existing escalations keep their raw commands; new trivial
ones name the verb. A running conductor keeps its old deny list until
`nw queue conductor-launch` relaunches it (the rule change moves its `launch_hash`). A
launch-settings override of `conductor_disallowed_tools` replaces the default list, this deny
included, as it already does for `AskUserQuestion`; this spec does not change that. Queued under
project Meta-Tools.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed, including the scratch-repo check; affected tests pass.
- [x] No regressions.
- [x] `docs/product/reference/index.md`, `docs/product/loop/operate.md` and
      `docs/product/capabilities/agentic.md` name `nw queue main-dirty-apply` (human or steward
      run; the conductor does not run it).
- [x] Spec body updated to match what shipped; As shipped records the AC4 mutation run.
- [x] `docs/LEDGER.md` regenerated; `python3 tools/spec_lint.py` exits 0.

## Open questions

None.
