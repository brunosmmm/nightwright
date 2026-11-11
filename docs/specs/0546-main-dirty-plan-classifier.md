---
id: SPEC-0546
title: nw queue main-dirty-plan, the read-only main_dirty classifier
status: accepted
owner: user
created: 2026-11-11
updated: 2026-11-11
kind: feature
tags: [agentic, queue, git]
supersedes: [SPEC-0542]
source_idea: IDEA-1159
---

## Context

`finish_close` parks `main_dirty` when the main checkout is on the base branch and
`git status --porcelain --untracked-files=no` is non-empty (`src/nightwright/agentic/close.py`).
[SPEC-0537](./0537-conductor-must-clear-main-dirty-itself-a-stray.md) tried to make the
conductor clear it in one spec and stopped converging (85 runs, review score 92 → 79). At
ESC-0067 (2026-11-11) the human chose "rework the spec (split / narrow)", and approved one
audited verb (`nw queue main-dirty-apply`) instead of raw git allow-rules (campaign
`IDEA-1159-20261110-02ed`, resume note seq 437). The steward's first split,
[SPEC-0542](./0542-main-dirty-plan-and-escalate.md) / 0543 / 0544, was again sized `xl` by spec
review (verdict `split`), so each was split once more.

This spec is the first of six, in queue order:

1. **SPEC-0546 (this spec)**: the read-only classifier `plan_clear` and `nw queue
   main-dirty-plan`. No behaviour change anywhere else.
2. [SPEC-0547](./0547-main-dirty-conductor-owned-plan-and-escalate.md): `main_dirty` becomes
   conductor-owned; the skill row plans and escalates.
3. [SPEC-0548](./0548-main-dirty-apply-core-commit.md): the audited verb's guards, `commit`,
   CLI, conductor deny (human or steward run).
4. [SPEC-0549](./0549-main-dirty-apply-wip-save-and-hold.md): the verb's WIP save, restore and
   `hold`.
5. [SPEC-0550](./0550-conductor-git-guard-hook.md): the cwd-aware conductor git-guard hook.
6. [SPEC-0551](./0551-conductor-runs-main-dirty-apply-trivial-only.md): the conductor runs
   the verb with `--trivial-only`.

Spec review found the classifier is where the abandoned branch churned most (form feed, `\r`,
`~~~`, symlinks, staged paths), so it gets its own convergence budget. The CLI is usable on its
own: a human can paste its `recommended` lines.

**Carries SPEC-0542 AC5, AC6, AC7, AC8, AC9, AC10, AC11, AC12** (AC6 and AC7 merged into AC2
below). It also folds in the SPEC-0542 review's classifier findings: the base rule now matches
`finish_close` (`--base`), the git invocations are named, the suffix tables agree, blank lines
no longer make non-comment files trivial, and `--root` compares real paths.

## Decision

Add `src/nightwright/agentic/main_dirty.py` `plan_clear(root, base=None) -> dict` (read-only)
and `nw queue main-dirty-plan --root PATH [--base BRANCH] [--json]`. The plan carries `class`,
`action`, the paths, and `recommended`: the shell commands a human runs to clear the checkout.
Nothing calls it yet; SPEC-0547 wires it into the conductor.

## Design

`plan_clear` writes nothing (no index refresh either: every git call runs with
`GIT_OPTIONAL_LOCKS=0`). CLI: registered in `cli_queue.py` and named under `nw queue` in
`docs/product/reference/index.md`. It takes no campaign id and looks none up. `--root` must be
the top of a git worktree: `os.path.realpath(root)` equals `os.path.realpath` of
`git -C root rev-parse --show-toplevel`; otherwise exit 2 with a stderr message. A git failure
on a real worktree exits 3 with a stderr message. Every class exits 0, a clean tree included.
Text mode: one line `<action> <class> <n> paths`, one path per line, then one line per
`recommended` command.

### Inputs

Every diff call passes `--no-ext-diff --no-textconv --no-color`, so user config cannot change
the lines read.

- Tracked dirt: `git status --porcelain=v1 -z --untracked-files=no`; its `XY` columns are kept
  per path.
- Modes and statuses: `git diff HEAD --raw --no-renames -z` (and `git diff --cached --raw` /
  `git diff --raw` for a partially staged path). Mode `120000` is a symlink, `160000` a gitlink.
- Hunks: `git diff HEAD -z` (staged and unstaged together).
- Per path, `old` is `git show HEAD:<path>` when that blob exists, else absent; `new` is the
  worktree bytes when the file is there, else absent (a deletion). A symlink is read as its
  link target, never followed. A gitlink or a worktree directory is never read.
- Bytes are decoded with `errors="surrogateescape"`, so a non-UTF-8 diff never raises. A NUL
  byte on either side, or git's "Binary files … differ" marker, sets `binary`. Lines split on
  `\n` only, never `splitlines()`, so a form feed or a bare `\r` does not hide a code line.
- A rename classifies both the old and the new path, and both appear in `paths`. Paths are
  repo-relative with forward slashes.

### File rules

**Code path:** starts with `src/`, `tests/` or `tools/`, or its suffix is one of `.py .pyi .sh
.bash .js .mjs .cjs .jsx .ts .tsx .go .rs .c .h .cc .cpp .cxx .hpp .rb .pl .java .sql .lua`.
Any source language not listed is not a code path; it can be `other`, never `substantive` (a
conservative miss: `other` still goes to a branch, never to a trivial commit).

**Comment line** for a path, by suffix (a suffix not in this table has **no** comment lines,
blank ones included):

| suffixes | comment when the stripped line |
|---|---|
| `.py .pyi .sh .bash .yml .yaml .toml .rb .pl` | is empty, or starts with `#` and not with `#!` |
| `.js .mjs .cjs .jsx .ts .tsx .go .rs .c .h .cc .cpp .cxx .hpp .java` | is empty; starts with `//`; or is `/*`, `*/` or `*`; or is `*` followed by whitespace (`* text`). `*out`, `*=`, `*ptr = 1;` are not |
| `.css` | is empty; or is `/*`, `*/` or `*`; or is `*` followed by whitespace. `//` is not a comment |
| `.md .html .xml` | is empty, starts with `<!--`, or is `-->` |

A Markdown `#` heading is not a comment line.

**Comment-only:** not binary, status is not a delete or rename, at least one added or removed
text line, and every added and removed line is a comment line.

**Frontmatter-only:** the path ends with `.md` and is not `docs/LEDGER.md`; `old` and `new` are
both present text; each begins with a `---` line and has a later line whose stripped text is
`---` (the closing fence, required: an unclosed block is not frontmatter), found before any
backtick fence; and the bytes after the closing fence are identical.

**Fence edit** (`.md .html .xml`): fence state toggles only on a line whose stripped text starts
with three or more backticks; a `~~~` line never toggles it (so it does not close a backtick
fence). An unclosed fence runs to end of file. An added line whose number in `new`, or a removed
line whose number in `old`, sits inside a fence is a fence edit, blank lines included.

**Docs-prose:** the path is `README.md` or `CHANGELOG.md`, or is under `docs/` and ends with
`.md`, and is not `docs/LEDGER.md`; text on both sides, at least one added or removed line, and
no fence edit.

**File class**, decided per path in this order (first match wins). A path is on the **code
side** when it, or for a rename either of its two names, is a code path:

1. `binary`, a symlink on either side, a typechange `T`, a mode change (`chmod +x`), a gitlink
   (`160000`) or a directory in the worktree → `code` when on the code side, else `other`.
   These have no text lines, so they are never `comment`, `frontmatter` or `docs`.
2. On the code side (this includes a code-suffix file under `skills/`, e.g. `skills/x/run.py`):
   a delete or rename → `code`; comment-only → `comment`; anything else (a logic or shebang
   edit) → `code`.
3. Any other path under `skills/`, or `docs/LEDGER.md` (even a blank line) → `other`.
4. A delete or rename of a non-code path → `other`.
5. A fence edit → `other`. Then comment-only → `comment`; frontmatter-only → `frontmatter`;
   docs-prose → `docs`.
6. Anything else (config, an unclosed frontmatter block, a blank-line edit in `x.json`) →
   `other`.

A partially staged path (index differs from both HEAD and the worktree) is classified on both
the staged and the unstaged diff and takes the worse class (`code` > `other` > the trivial
three).

### Rollup, in progress, base

**Rollup, worst file wins:** any `code` → `class: substantive`, `action: branch_and_escalate`;
else any `other` → `class: branch`, `action: save_branch`; else one or more `comment`,
`frontmatter` or `docs` files → `class: trivial`, `action: commit`; no tracked dirt →
`class: clean`, `action: resume`.

**Operation in progress → `class: merge`, `action: escalate`**, whatever the files and the
branch. The only merge rule: any of `MERGE_HEAD`, `CHERRY_PICK_HEAD`, `REVERT_HEAD`,
`rebase-merge/`, `rebase-apply/` exists under the worktree's git dir (each resolved with
`git rev-parse --git-path <name>`, so a linked worktree is covered), or any porcelain status is
one of the seven unmerged codes `UU AA DD AU UA DU UD`. Key `in_progress` is `merge`,
`cherry-pick`, `revert`, `rebase`, `conflict` (unmerged codes with no marker file) or `null`.
File rows stay in `files`. A `cherry-pick -n` leaves no `CHERRY_PICK_HEAD`, so it is not an
operation in progress.

**Base**, the value `finish_close` compares to the current branch (`_close_cfg` in
`close.py`): `.nw/project.yaml` `merge.base` when it is a non-empty string, else the project
`base`, else `--base` (the campaign's `repo.base`, passed by the caller), else `main`. A missing
file, or a missing or scalar `merge`, falls back. `on_base` is true only when
`git symbolic-ref --short HEAD` equals the base; a detached HEAD is `on_base: false`. Off the
base, `action` is `hold` and `class` stays as computed, unless `class` is `merge`.

JSON schema `nw.main_dirty_plan.v1`, keys: `schema`, `root`, `base`, `on_base`, `class`
(`clean|trivial|branch|substantive|merge`), `action`
(`resume|commit|save_branch|branch_and_escalate|hold|escalate`), `in_progress`, `paths` (every
tracked dirty path, both sides of a rename), `substantive_paths` (the `code` ones), `files`
(`{path, status, file_class}`), `recommended` (list of strings).

### `recommended`

Commands for a human in a shell, each argument quoted with `shlex.quote`, every path command
with `git -C <root> --literal-pathspecs`, paths after `--`. `<wip>` is
`wip/main-dirty-<YYYYMMDDThhmmssZ>` (UTC, plan time), with `-2`, `-3`, … appended while
`git branch --list` shows it. `<wip>` is created from the checkout's current HEAD; for `hold`
that is the off-base branch (or detached commit), so the WIP branch carries its history.

`U…` is the paths whose worktree column (`Y` of `XY`) is not blank: the edits not yet in the
index, a rename's new path when it has further unstaged edits. Staged-only paths (a staged
`git rm`, a staged `git mv`, a staged edit) are already in the index and are not named: `git add
--` on a path that is in neither the index nor the worktree stops with `pathspec did not match`
(checked against git 2.55). When `U…` is empty the `add` line is omitted. Each `commit` then
records the whole index, which holds only tracked dirt.

| action | `recommended` |
|---|---|
| `resume` | `[]` |
| `commit` | `git -C R --literal-pathspecs add -- U…`, `git -C R commit -m 'chore: land stray trivial main-checkout edits'` |
| `save_branch`, `branch_and_escalate` | `git -C R switch -c <wip>`, `git -C R --literal-pathspecs add -- U…`, `git -C R commit -m 'WIP: stray main-checkout edits (<class>)'`, `git -C R switch <base>` |
| `hold` with tracked dirt | the `save_branch` list (it ends on `git -C R switch <base>`) |
| `hold`, no tracked dirt | `git -C R switch <base>` |
| `escalate` (`merge`) | `[]`; the escalation option names the operation to finish |

None of these discards work: the dirt is committed, on the base or on `<wip>`. No `checkout --`,
`reset`, `clean`, `stash drop`, `rm`, or push ever appears. SPEC-0548 and SPEC-0549 replace the
non-empty lists with the verb, action by action, as each ships.

### Files the implementer may cherry-pick from `nw/IDEA-1159-20261110-02ed`

Read-only with `git show nw/IDEA-1159-20261110-02ed:<path>` (branch tip `61876af9`); an
optional source, never the contract: where the branch and this spec differ, this spec wins.

- `src/nightwright/agentic/main_dirty.py`: the plan half, `_is_git_repo` … `_merge_state`,
  `plan_clear`, `NotAWorktree`. Not `apply_clear` or any `_wip_*` / `_commit_*` / `_restore_*`
  helper. Add `in_progress`, the sequencer markers via `--git-path`, `--base`, the diff flags,
  and `recommended`.
- `src/nightwright/agentic/cli_queue.py`: the `main-dirty-plan` command only.
- `docs/product/reference/index.md`: the `main-dirty-plan` entry only.

## Alternatives considered

- **Keep classifier and ownership in one spec** (SPEC-0542's rebuttal). Rejected on re-review:
  15 ACs, and the classifier needs its own convergence budget; the CLI is usable by a human
  without the conductor row.
- **Read the base from `.nw/project.yaml` only.** A campaign queued with `--base develop` and no
  project `base` would plan against `main` and tell the human to switch off a correct branch.
- **Recommend `git stash push`** for non-trivial dirt. A stash is easy to lose and names no
  branch; a `wip/main-dirty-*` commit is visible in `git branch` and matches what the verb
  (SPEC-0549) produces.

## Acceptance criteria

- [ ] AC1 (SPEC-0542 AC5) Classes on temp git repos, one test per row, each test first asserting
      its fixture produced the named diff: frontmatter-only spec, docs prose outside fences,
      `README.md`, `CHANGELOG.md`, comment-only `.py` → `trivial`/`commit`; `docs/LEDGER.md`
      (even a blank line), a fence edit, a blank-line addition inside a backtick fence in
      `docs/a.md`, an unclosed fence, a `~~~` line inside a backtick fence followed by an edit
      (still a fence edit), a `---` block with no closing `---`, a blank-line edit in `x.json`,
      `skills/x.md`, a doc delete, a binary outside code paths, a NUL in a `.md` →
      `branch`/`save_branch`; a logic edit beside a docs edit, a `src/` delete, a binary under
      `src/`, a root `.py`, a logic edit in `lib/x.rb`, `tests/`, `skills/x/run.py`, a rename
      between `src/` and `docs/` in either direction (both names in `paths`) →
      `substantive`/`branch_and_escalate`; `src/star_deref.c` with `*ptr = 1;` →
      `substantive`; `src/star_comment.c` with ` * kept comment` → `trivial`; a shebang edit in
      `.py` → not trivial; no tracked dirt → `clean`/`resume`; an untracked file never appears
      in `paths`.
- [ ] AC2 (SPEC-0542 AC6 + AC7) `_is_comment_line` is pinned by a parametrized table covering
      every suffix in the comment-line table, `#!`, `*x`, `* x`, `/*`, `*/`, `<!--`, `-->`, a
      Markdown `#` heading (not a comment), a `.css` `//` line (not a comment), and a blank line
      in `.json` (not a comment). Mode-only (`chmod +x`) outside code paths, typechange, and a
      symlink retarget outside code paths are `other`; `chmod +x` on `src/x.py` is `code`; a
      dirty submodule (gitlink) path is `other` without raising; a staged-only comment edit is
      `trivial`; a staged fence edit is `branch`; a staged comment edit plus an unstaged logic
      edit on one path is `code`; a staged docs edit plus an unstaged fence edit is `branch`; a
      code line hidden behind a form feed and one behind a bare `\r` are still `code`; a
      non-UTF-8 diff is classified without raising; a repo with `diff.external` and a textconv
      driver configured classifies the same as without.
- [ ] AC3 (SPEC-0542 AC8) Operation in progress: `MERGE_HEAD`, each of the seven unmerged
      codes, `CHERRY_PICK_HEAD`, `REVERT_HEAD`, `rebase-merge/`, `rebase-apply/`, and a merge in
      a linked worktree each give `merge`/`escalate` with the matching `in_progress`, also when
      off the base; a `cherry-pick -n` with no `CHERRY_PICK_HEAD` is not `merge`.
- [ ] AC4 (SPEC-0542 AC9) Base: `merge.base` wins over `base`, `base` over `--base`, `--base`
      over the default, a missing or scalar `merge` falls back, and no `.nw/project.yaml` means
      `main`; a project config without `base` plus `--base develop` gives `on_base: true` on
      `develop`. On a non-`main` base a trivial diff is `commit` on it and `hold` (class still
      `trivial`) elsewhere; detached HEAD gives `on_base: false` and `hold`.
- [ ] AC5 (SPEC-0542 AC10) `recommended` matches the table exactly (asserted as whole lists) for
      `commit`, `save_branch`, `branch_and_escalate`, `hold` dirty, `hold` clean, `resume`,
      `escalate`; a path with a space or a literal `*` is quoted; the `<wip>` name takes `-2`
      when the branch exists; the `add` line names only the `U…` paths and is absent when there
      are none. Each list is then run in its temp repo, which also holds an untracked file.
      Fixtures: a trivial edit (`commit`); a staged `git rm`, a staged `git mv`, and an unstaged
      delete (`substantive`, `save_branch` list); a dirty tracked `a*b.md` beside an untracked
      `axb.md`. After each run every command exited 0,
      `git status --porcelain --untracked-files=no` is empty, the edits (both rename sides and
      the deletes) are on the base commit or on `<wip>`, and every untracked file (`axb.md`
      included) is still untracked and absent from the new commit's tree. Removing
      `--literal-pathspecs` from the lists makes the `a*b.md` case fail.
- [ ] AC6 (SPEC-0542 AC11) `plan_clear` writes nothing: HEAD, `git branch --list`, the index
      file's bytes, and the worktree files are identical before and after, for every class, on a
      repo whose index has stale stat data.
- [ ] AC7 (SPEC-0542 AC12) CLI (through `CliRunner`): `--json` prints `nw.main_dirty_plan.v1`
      and exits 0 on each class; `--base` reaches `plan_clear`; a non-git `--root` and a
      subdirectory of a checkout exit 2 with a message; a symlinked path to a checkout top and a
      root with a trailing slash exit 0; a git failure exits 3; text mode prints the summary
      line, the paths, and the commands.

## Test plan

- **Automated tests:** new `tests/test_main_dirty_plan.py`, one module marker
  `pytestmark = pytest.mark.fast`, temp git repos. From the branch's
  `tests/test_main_dirty_clear.py` (tip `61876af9`) take the plan tests: everything before
  `test_apply_commit_trivial`, plus `test_is_comment_line_suffix_table`,
  `test_typechange_and_mode_only_are_other_not_trivial`,
  `test_docs_prose_requires_a_visible_diff`, `test_retargeted_symlink_is_not_trivial`,
  `test_split_fm_keeps_eof_newline_and_star_tab_comment`,
  `test_cherry_pick_no_commit_not_merge` (unchanged), the plan half of
  `test_cli_text_mode_plan_and_apply` and `test_cli_git_failures_have_messages_and_distinct_codes`,
  and the plan part of `test_root_must_be_checkout_top`. New: every AC1/AC2 case the branch lacks
  (blank lines in a fence and in `.json`, `.css` `//`, `.rb`, gitlink, partial-staging pairs,
  ext-diff config, `~~~` inside a fence, unclosed frontmatter, bare `\r`, rename `paths`), AC3
  sequencer and linked-worktree cases, AC4 `--base`, AC5 whole-list cases and running them,
  AC6 snapshot test, AC7 symlinked root.
- **Mutation check (by hand, once):** remove `--literal-pathspecs` from the lists; the AC5
  `a*b.md` case fails. Recorded in As shipped.
- **Regression guard:** no existing module changes behaviour; `python3 tools/affected_tests.py
  --run uv run pytest -q`, `python3 tools/test_tiers.py --check`, `python3 tools/spec_lint.py`.
- **Manual verification (real check):** in a scratch repo under the scratchpad, `git init`,
  commit `docs/a.md` with frontmatter and `src/x.py`; edit only the frontmatter; run
  `uv run nw queue main-dirty-plan --root <scratch> --json` → `trivial`/`commit`; paste the
  `recommended` commands into a shell → tracked-clean, one new commit with the subject. Then
  edit `src/x.py` logic and add an untracked file → `substantive`; run its commands → back on
  the base, tracked-clean, the edit on `wip/main-dirty-*`, the untracked file still there.
  Record the output in As shipped.

## Out of scope

- Any change to `PARK_OWNERS`, the dashboard, herdr, E5, or `skills/nw-conduct/SKILL.md`
  (SPEC-0547).
- Any git write: the verb `nw queue main-dirty-apply` (SPEC-0548, SPEC-0549).
- `launch.py`, `CONDUCTOR_CURSOR_POLICY` and every permission list (SPEC-0548, SPEC-0550,
  SPEC-0551).
- Untracked files: they never park `main_dirty` and are never listed.
- `finish_close` is unchanged.

## Rollout / migration

No data move, no caller yet: the command is additive. Queued under project Meta-Tools; next in
the chain is SPEC-0547.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed, including the scratch-repo check; affected tests pass.
- [ ] No regressions.
- [ ] `docs/product/reference/index.md` names `nw queue main-dirty-plan`.
- [ ] As shipped records the AC5 `--literal-pathspecs` mutation run (flag removed, test fails).
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` regenerated; `python3 tools/spec_lint.py` exits 0.

## Open questions

None.
