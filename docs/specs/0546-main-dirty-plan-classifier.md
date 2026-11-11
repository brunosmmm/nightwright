---
id: SPEC-0546
title: nw queue main-dirty-plan, the read-only main_dirty classifier
status: done
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
own: a human can paste its `recommended` lines. Because SPEC-0551 may commit a `trivial` plan to
main unattended, every rule below errs toward `branch`: a miss only costs a branch, a false
`trivial` lands unreviewed code on main.

**Carries SPEC-0542 AC5, AC6, AC7, AC8, AC9, AC10, AC11, AC12** (AC6 and AC7 merged into AC2
below). It also folds in the SPEC-0542 review's classifier findings: the base rule now matches
`finish_close` (its `load_project_config` defaults), the git invocations are named, the suffix
tables agree, blank lines no longer make non-comment files trivial, and `--root` compares real
paths.

## Goals / Non-goals

**Goals**

- A read-only classifier `plan_clear` and `nw queue main-dirty-plan` whose `recommended` lines a
  human can paste.
- No behaviour change anywhere else.

**Non-goals**

- Any change to `PARK_OWNERS`, the dashboard, herdr, E5, or `skills/nw-conduct/SKILL.md`
  (SPEC-0547).
- Any git write: the verb `nw queue main-dirty-apply` (SPEC-0548, SPEC-0549).
- `launch.py`, `CONDUCTOR_CURSOR_POLICY` and every permission list (SPEC-0548, SPEC-0550,
  SPEC-0551).
- Untracked files: they never park `main_dirty` and are never listed.
- `finish_close` is unchanged (including its `repo.base` fallback, which no registered project
  can reach; see Base).

## Decision

Add `src/nightwright/agentic/main_dirty.py` `plan_clear(root, base=None) -> dict` (read-only)
and `nw queue main-dirty-plan --root PATH [--base BRANCH] [--json]`. The plan carries `class`,
`action`, the paths, and `recommended`: the shell commands a human runs to clear the checkout.
Nothing calls it yet; SPEC-0547 wires it into the conductor.

## Design

`plan_clear` writes nothing (no index refresh either: every git call runs with
`GIT_OPTIONAL_LOCKS=0`). Every git child runs with `LC_ALL=C` and with `GIT_DIR`,
`GIT_WORK_TREE` and `GIT_INDEX_FILE` removed from its environment. CLI: registered in
`cli_queue.py` and named under `nw queue` in `docs/product/reference/index.md`. It takes no
campaign id and looks none up. `--root` must be the top of a git worktree:
`os.path.realpath(root)` equals `os.path.realpath` of `git -C root rev-parse --show-toplevel`;
otherwise exit 2 with a stderr message. Exit 2 also covers a `rev-parse --show-toplevel` failure
whose stderr says `not a git repository` or that the operation must run in a work tree (a bare
repo or a `.git` dir). Every other failure exits 3 with a stderr message: any other git failure
on a real worktree, and a `.nw/project.yaml` that `load_project_config` rejects (the loader's
message is printed). Every class exits 0, a clean tree included. Text mode: one line
`<action> <class> <n> paths`, one path per line, then one line per `recommended` command.

### Inputs

Every `git diff` call passes `--no-ext-diff --no-textconv --no-color --no-renames`, and
`git status` passes `--renames`, so user config (`diff.external`, textconv drivers,
`diff.renames`, `status.renames`) cannot change what is read. No patch text is read or parsed:
the file rules use only the `old` and `new` bytes below, so prefix and quoting config
(`diff.noprefix`, `diff.mnemonicPrefix`, `diff.srcPrefix`, `core.quotePath`) cannot matter. `-z`
is used only with `status --porcelain` and `diff --raw`, where it changes the framing.

- Tracked dirt: `git status --porcelain=v1 -z --renames --untracked-files=no`; its `XY` columns
  are kept per path. A rename record is `XY new\0old\0`: the next NUL field after `new` is the
  old name. A rename is detected from porcelain `R` only. An unstaged move (`mv` without `git
  mv`) is a delete (` D`) plus an untracked file, so only the delete is listed.
- Modes: `git diff HEAD --raw --no-renames -z` for a path that is not partially staged; for a
  partially staged path `git diff --cached --raw --no-renames -z` (staged half) and `git diff
  --raw --no-renames -z` (unstaged half). Modes are looked up per name, so a staged rename
  appears as its `D` entry (old name) and its `A` entry (new name). Mode `120000` is a symlink,
  `160000` a gitlink.
- `old` and `new` bytes, per path. Blobs are read with `git cat-file blob <rev>:<path>` (no
  filters, no textconv). A path is **partially staged** when both porcelain columns `X` and `Y`
  are non-blank and it is not unmerged; any other path is classified once, as the **combined**
  pair.

  | classified as | `old` | `new` |
  |---|---|---|
  | combined (any path not partially staged; an unmerged path too) | `HEAD:<path>` | worktree bytes |
  | staged half (partially staged) | `HEAD:<path>` | `:<path>` (the index blob) |
  | unstaged half (partially staged) | `:<path>` | worktree bytes |

  A side is absent when its blob is missing (a new file has no `old`; a deletion has no `new`).
  A rename's `old` is read at the old name and its `new` at the new name. A symlink is read as
  its link target, never followed. For a gitlink or a worktree directory no side is read: not
  the HEAD blob, not the index blob, not the worktree.
- Bytes are decoded with `errors="surrogateescape"`, so a non-UTF-8 file never raises. A NUL
  byte on either side sets `binary`. Lines split on `\n` only, never `splitlines()`.
- A rename classifies both names, and both appear in `paths`. Paths are repo-relative with
  forward slashes.

### File rules

**Code path:** starts with `src/`, `tests/` or `tools/`, or its suffix is one of `.py .pyi .sh
.bash .js .mjs .cjs .jsx .ts .tsx .go .rs .c .h .cc .cpp .cxx .hpp .rb .pl .java .sql .lua`.
Any source language not listed is not a code path; it can be `other`, never `substantive` (a
conservative miss: `other` still goes to a branch, never to a trivial commit).

**Docs path:** `README.md`, `CHANGELOG.md`, or a path under `docs/` ending `.md`; never
`docs/LEDGER.md`. Any other `.md` (`.claude/agents/*.md`, `.claude/commands/*.md`,
`AGENTS.md`, `CLAUDE.md`, `skills/**`) is not a docs path: it can carry tool permissions or
prompt text, so frontmatter-only, docs-prose and the `.md` comment rules never apply to it and
it falls to rule 6.

**Line scan.** Each side (`old`, `new`) is scanned once, top to bottom, with state carried
between lines. Per line, one trailing `\r` is dropped and `s` is the rest stripped with Python
`str.strip()`. A line gets a kind: `blank`, `comment`, `text` (anything else), and for `.md
.html .xml` also `fenced`. The function is `_line_kinds(path, side_text) -> (kinds, hazard)`.

*Hazard.* A line whose `s` is non-empty and which (after dropping one trailing `\r`) still
contains any of `\r \x0b \x0c \x1c \x1d \x1e \x85    ` sets `hazard` for the whole
side: such a character ends a line for CPython's tokenizer or for JavaScript, so a code line can
hide behind a comment-looking prefix (`# note\rimport os`). A line that is blank after strip,
`\x0c` alone included, is `blank`, not a hazard. These suffix hazards also set `hazard`:

- `.sh .bash .rb .pl`: any line containing `<<` (a heredoc body is data, not comment).
- `.yml .yaml`: any line whose `s` ends with `|`, `|-`, `|+`, `>`, `>-` or `>+` (a block scalar).
- `.js .mjs .cjs .jsx .ts .tsx .go`: any backtick (a template or raw string).
- C-like and `.css`: a `text` line containing `/*` or `*/`.
- `.md .html .xml`: a `text` line containing `<!--` or `-->`.

*Kinds by suffix.*

| suffixes | kinds |
|---|---|
| `.py .pyi .toml` | while inside a triple-quoted string every line is `text`. Strings open at the first `"""` or `'''` on a line and close at the next occurrence of the same token (that line is `text` too). Otherwise: `s` empty → `blank`; starts with `#` and not `#!` → `comment`; else `text` |
| `.sh .bash .yml .yaml .rb .pl` | `s` empty → `blank`; starts with `#` and not `#!` → `comment`; else `text` |
| `.js .mjs .cjs .jsx .ts .tsx .go .rs .c .h .cc .cpp .cxx .hpp .java` | in a block comment: `comment`. Otherwise `s` empty → `blank`; starts with `//` → `comment`; starts with `/*`: `comment` and, when it has no `*/`, enters a block comment; a `/*` line that has `*/` is `comment` only when `s` ends with that single `*/`, else `text`; anything else (`*ptr = 1;`, `*out`, `*=`, `* b;`, a lone `*/`) → `text`. A block comment ends at the first line containing `*/`; that line must end with it, else hazard |
| `.css` | as the C-like row, with no `//` comment |
| `.md .html .xml` | inside a backtick fence → `fenced`. A line whose `s` starts with three or more backticks toggles the fence and is itself `fenced` (a `~~~` line never toggles; an unclosed fence runs to end of file). Otherwise, in an HTML comment → `comment`, which ends at the first line containing `-->`, that line must end with it, else hazard. Else `s` empty → `blank`; a line whose `s` starts with `<!--` and ends with a single `-->` → `comment`; one starting `<!--` without `-->` → `comment` and enters an HTML comment; else `text` |
| any other suffix | `blank` and `comment` do not exist: every line is `text` (a blank line in `x.json` is not a comment) |

A Markdown `#` heading is `text`. A line is a comment only in the position the scan puts it:
`*`-led lines are comments only inside a block comment that this side's own scan opened, so a
`* b;` that continues a multiplication stays `text`.

**Comment-only:** not binary, no `hazard` on either side, `old` and `new` both present, and the
sequence of `text` and `fenced` lines of `old` equals that of `new` (exact text, in order). A
changed, added or removed `text` line, any edit inside a string, a fence or a block
scalar, and an indent change therefore all fail it. Edits to `blank` and `comment` lines pass it,
as does moving a code line across them. Wrapping existing code in `/*` … `*/` or `<!--` …
`-->` removes those lines from the new `text` sequence, so it fails.

**Fence edit** (`.md .html .xml`, no hazard needed): the sequence of `fenced` lines of `old`
differs from that of `new`. Blank lines inside a fence are `fenced`, so a blank-line edit in a
fence is a fence edit.

**Frontmatter-only:** a docs path; `old` and `new` are both present text; each begins with a
`---` line and has a later line whose stripped text is `---` (the closing fence, required: an
unclosed block is not frontmatter), found before any backtick fence; and the bytes after the
closing fence are identical.

**Docs-prose:** a docs path; text on both sides; `old` and `new` differ; no fence edit; and
neither side begins with a `---` line that has no closing `---` (a half-written frontmatter
block is not prose).

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
5. A fence edit (docs path `.md`, or `.html`/`.xml`) → `other`. Then, comment-only → `comment`
   (an `.md` only when it is a docs path); frontmatter-only → `frontmatter`; docs-prose →
   `docs`.
6. Anything else (config, a non-docs `.md`, an unclosed frontmatter block, a blank-line edit in
   `x.json`) → `other`.

A partially staged path is classified on the staged half and on the unstaged half, each with its
own `old`/`new` and its own raw mode, and takes the worse class (`code` > `other` > the trivial
three). A rename's half uses the old name for `old` and the new name for `new`.

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
When more than one marker exists, `in_progress` is the first of that list in that order
(`MERGE_HEAD` beats `rebase-merge/`). File rows stay in `files`. A `cherry-pick -n` leaves no
`CHERRY_PICK_HEAD`, so it is not an operation in progress.

**Dirty gitlink → `action: escalate`, `recommended: []`**, `class` stays as computed. A gitlink
path whose worktree column `Y` is non-blank may have dirty submodule content with an unchanged
submodule HEAD; `git add -- <submodule>` then stages nothing, `git commit` exits 1 and the
checkout stays dirty, so no command list can clear it. The escalation names the submodule path.
A gitlink with a blank `Y` (a staged pointer change) is already in the index and takes the
normal list.

**Base**, the value `finish_close` compares to the current branch. `finish_close` receives
`ctx.project(state)` (`queue.py`, `project`), which is `P.project_config(...)` and so ends in
`P.load_project_config` / `_with_defaults`: there `base` defaults to `main` and `merge.base`
defaults to the top-level `base`. So `_close_cfg` (`close.py`) yields, for every project that has
a config, `merge.base`, else `base`, else `main`; its `repo.base` fallback is unreachable
there. `plan_clear` applies the same loader: when `<root>/.nw/project.yaml` is a file, base is
`P.load_project_config(path)["merge"]["base"]` (never the raw YAML, never `--base`); a
`ValueError` from the loader (invalid YAML, scalar `merge`, a non-string `base`) exits 3 with
its message, as `finish_close` parks `ProjectConfigError`. `plan_clear` skips only
`project_config`'s project-name check, which does not affect the base. When there is no
`.nw/project.yaml` (a scratch or unregistered repo) base is `--base`, else `main`. `--base` is
documented as "used only when `--root` has no `.nw/project.yaml`". `on_base` is true only when
`git symbolic-ref --short HEAD` equals the base; a detached HEAD is `on_base: false`. Off the
base, `action` is `hold` and `class` stays as computed, unless `class` is `merge`. Precedence of
`action`: operation in progress, then dirty gitlink, then off-base `hold`, then the rollup.

JSON schema `nw.main_dirty_plan.v1`, keys: `schema`, `root`, `base`, `on_base`, `class`
(`clean|trivial|branch|substantive|merge`), `action`
(`resume|commit|save_branch|branch_and_escalate|hold|escalate`), `in_progress`, `paths` (every
tracked dirty path, both names of a rename), `substantive_paths` (the paths of `code` files, both
names of a rename), `files`, `recommended` (list of strings).

`files` has one row per porcelain record: `{path, orig_path, status, file_class}`. `status` is
the two-character porcelain `XY` string, kept verbatim (space included). `orig_path` is the old
name for a rename (`path` is then the new name) and `null` otherwise; a rename is one row whose
`file_class` is the rename's class. Examples:

```json
{"path": "docs/a.md", "orig_path": null, "status": " M", "file_class": "docs"}
{"path": "docs/new.md", "orig_path": "src/old.py", "status": "R ", "file_class": "code"}
```

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
| `escalate` (`merge`, or a dirty gitlink) | `[]`; the escalation option names the operation to finish or the submodule path |

None of these discards work: the dirt is committed, on the base or on `<wip>`. No `checkout --`,
`reset`, `clean`, `stash drop`, `rm`, or push ever appears. SPEC-0548 and SPEC-0549 replace the
non-empty lists with the verb, action by action, as each ships.

### Files the implementer may cherry-pick from `nw/IDEA-1159-20261110-02ed`

Read-only with `git show nw/IDEA-1159-20261110-02ed:<path>` (branch tip `61876af9`); an
optional source, never the contract: where the branch and this spec differ, this spec wins.

- `src/nightwright/agentic/main_dirty.py`: the plan half, `_is_git_repo` … `_merge_state`,
  `plan_clear`, `NotAWorktree`. Not `apply_clear` or any `_wip_*` / `_commit_*` / `_restore_*`
  helper. Not its per-line `_is_comment_line` or its hunk walk: the Line scan above replaces
  them. Add `in_progress`, the sequencer markers via `--git-path`, `--base`, the diff flags,
  the loader-based base, the `old`/`new` table and `recommended`.
- `src/nightwright/agentic/cli_queue.py`: the `main-dirty-plan` command only.
- `docs/product/reference/index.md`: the `main-dirty-plan` entry only.

## Alternatives considered

- **Keep classifier and ownership in one spec** (SPEC-0542's rebuttal). Rejected on re-review:
  15 ACs, and the classifier needs its own convergence budget; the CLI is usable by a human
  without the conductor row.
- **Read `.nw/project.yaml` raw and let `--base` beat the defaults.** `finish_close` never does:
  through `load_project_config` it always sees `main` or the project's own `base`. The plan would
  say `on_base: true` on `develop` where `finish_close` compares `main`, and the conductor only
  plans for a `main_dirty` park, which `finish_close` raises when the checkout is on its base.
  The plan follows `finish_close`; `--base` serves repos with no project config.
- **Classify hunk lines one by one.** A bare `/*`, `*/`, `<!--` or `-->` line, or a `#` or blank
  line inside a triple-quoted string, heredoc or block scalar, would each look like a comment
  while changing behaviour. Comparing the `text` sequence of the whole old and new file catches
  all of them with one rule, and needs no hunk line numbers.
- **Recommend `git stash push`** for non-trivial dirt. A stash is easy to lose and names no
  branch; a `wip/main-dirty-*` commit is visible in `git branch` and matches what the verb
  (SPEC-0549) produces.

## Acceptance criteria

- [x] AC1 (SPEC-0542 AC5) Every row C01–C42 of the case table in the Test plan passes: one test
      per row, each first asserting its fixture produced the named diff, and each ending on the
      row's class, action and, where named, `file_class`.
- [x] AC2 (SPEC-0542 AC6 + AC7) Every row S01–S27 of the case table passes. `_line_kinds` is
      pinned by the parametrized rows S01–S10 (every suffix in the Kinds table, `#!`, `*x`,
      `* x`, `/*`, `*/`, `<!--`, `-->`, a Markdown `#` heading, a `.css` `//` line, a blank line
      in `.json`, and the disqualifying characters). The byte fixtures `+# note\rimport os` in a
      `.py` and `+// note\x0cfoo();` in a `.js` are each `code`, and `+\x0c` alone is a blank
      line. A repo with `diff.external`, a textconv driver, `diff.noprefix=true`,
      `diff.mnemonicPrefix=true`, `diff.renames=false` and `status.renames=false` configured
      classifies the same as without. The `old`/`new` bytes of the combined, staged and unstaged
      pairs are exactly the table in Inputs (asserted by S16–S19).
- [x] AC3 (SPEC-0542 AC8) Operation in progress: `MERGE_HEAD`, each of the seven unmerged
      codes, `CHERRY_PICK_HEAD`, `REVERT_HEAD`, `rebase-merge/`, `rebase-apply/`, and a merge in
      a linked worktree each give `merge`/`escalate` with the matching `in_progress`, also when
      off the base; a `cherry-pick -n` with no `CHERRY_PICK_HEAD` is not `merge`; with both
      `MERGE_HEAD` and `rebase-merge/` present `in_progress` is `merge`.
- [x] AC4 (SPEC-0542 AC9) Base (rows B01–B08 of the table in the Test plan): `merge.base` wins
      over `base`, `base` over the default `main`; with a `.nw/project.yaml` that has neither,
      `--base develop` is ignored and base is `main`, so on `develop` `on_base` is false and
      `action` is `hold`, the same base `_close_cfg` returns for the same config and a state
      whose `repo.base` is `develop` (asserted by calling `_close_cfg`); with no
      `.nw/project.yaml`, `--base develop` gives `on_base: true` on `develop` and no flag gives
      `main`; an invalid YAML file and a scalar `merge` exit 3 with the loader's message. On a
      non-`main` base a trivial diff is `commit` on it and `hold` (class still `trivial`)
      elsewhere; detached HEAD gives `on_base: false` and `hold`. A dirty gitlink off the base is
      `escalate`, not `hold`.
- [x] AC5 (SPEC-0542 AC10) `recommended` matches the table exactly (asserted as whole lists) for
      `commit`, `save_branch`, `branch_and_escalate`, `hold` dirty, `hold` clean, `resume`,
      `escalate` (`merge`), and a dirty gitlink (`escalate`, `[]`); a path with a space or a
      literal `*` is quoted; the `<wip>` name takes `-2` when the branch exists; the `add` line
      names only the `U…` paths and is absent when there are none. The lists for `commit`,
      `save_branch`, `branch_and_escalate` and `hold` dirty are then **run** in their temp repos,
      each of which also holds an untracked file; `resume`, `hold` clean and `escalate` are
      whole-list assertions only. Run fixtures: a trivial edit (`commit`); a staged `git rm`, a
      staged `git mv`, and an unstaged delete (`substantive`, `save_branch` list and, with a
      logic edit, `branch_and_escalate`); a dirty tracked `a*b.md` beside an untracked `axb.md`;
      a trivial edit on a feature branch (`hold` dirty, ending back on the base). After each run
      every command exited 0, `git status --porcelain --untracked-files=no` is empty, the edits
      (both rename sides and the deletes) are on the base commit or on `<wip>`, and every
      untracked file (`axb.md` included) is still untracked and absent from the new commit's
      tree. Removing `--literal-pathspecs` from the lists makes the `a*b.md` case fail.
- [x] AC6 (SPEC-0542 AC11) `plan_clear` writes nothing: HEAD, `git branch --list`, the index
      file's bytes, and the worktree files are identical before and after, for every class, on a
      repo whose index has stale stat data.
- [x] AC7 (SPEC-0542 AC12) CLI (through `CliRunner`): `--json` prints `nw.main_dirty_plan.v1`
      and exits 0 on each class, with `files` rows `{path, orig_path, status, file_class}`
      matching the Design examples for a plain edit (` M`) and a staged rename (`R `);
      `--base` reaches `plan_clear`; a non-git `--root`, a bare repo and a subdirectory of a
      checkout exit 2 with a message, also with `GIT_DIR` set in the environment; a symlinked
      path to a checkout top and a root with a trailing slash exit 0; a git failure exits 3; text
      mode prints the summary line, the paths, and the commands; and
      `docs/product/reference/index.md` contains `main-dirty-plan` in its `nw queue` list, asserted
      by one added test in `tests/test_main_dirty_plan.py` that fails when the entry is removed.

## Test plan

- **Automated tests:** new `tests/test_main_dirty_plan.py`, one module marker
  `pytestmark = pytest.mark.fast` (tier per SPEC-0490: no TUI, remote or `_e2e` name), temp git
  repos. `python3 tools/test_tiers.py --check` exits 0 when `tools/test_tiers.py` exists. From
  the branch's `tests/test_main_dirty_clear.py` (tip `61876af9`) take the plan tests: everything
  before `test_apply_commit_trivial`, plus `test_is_comment_line_suffix_table` (re-pointed at
  `_line_kinds`, rows S01–S10),
  `test_typechange_and_mode_only_are_other_not_trivial`,
  `test_docs_prose_requires_a_visible_diff`, `test_retargeted_symlink_is_not_trivial`,
  `test_split_fm_keeps_eof_newline_and_star_tab_comment` (the star case re-pointed at C06),
  `test_cherry_pick_no_commit_not_merge` (unchanged), the plan half of
  `test_cli_text_mode_plan_and_apply` and `test_cli_git_failures_have_messages_and_distinct_codes`,
  and the plan part of `test_root_must_be_checkout_top`. New: every case-table row the branch
  lacks, AC3 sequencer, two-marker and linked-worktree cases, AC4 rows B01–B08 plus the
  `_close_cfg` parity assertion, AC5 whole-list cases and running them, the AC6 snapshot test,
  AC7 symlinked root, bare repo, `GIT_DIR`, the JSON `files` rows and the reference-index
  assertion.
- **Case table (AC1, AC2, AC4).** Paths are repo-relative; each fixture is one commit on the
  base plus the edit named. Expected is `class/action` unless a `file_class` is named.

  | id | fixture | expected |
  |---|---|---|
  | C01 | frontmatter-only edit in `docs/specs/0001-x.md` | `trivial/commit`, `frontmatter` |
  | C02 | prose edit outside fences in `docs/a.md` | `trivial/commit`, `docs` |
  | C03 | prose edit in `README.md` | `trivial/commit` |
  | C04 | prose edit in `CHANGELOG.md` | `trivial/commit` |
  | C05 | `#` comment line changed in `src/x.py` | `trivial/commit`, `comment` |
  | C06 | `src/star_comment.c`: `/*`, ` * old note`, ` */`, then code; the ` * old note` line changed to ` * kept comment` | `trivial/commit`, `comment` |
  | C07 | blank line added between two functions in `src/x.py` | `trivial/commit`, `comment` |
  | C08 | blank line added to `docs/LEDGER.md` | `branch/save_branch` |
  | C09 | line changed inside a backtick fence in `docs/a.md` | `branch/save_branch` |
  | C10 | blank line added inside a backtick fence in `docs/a.md` | `branch/save_branch` |
  | C11 | `docs/a.md` with an unclosed fence; a line after it edited | `branch/save_branch` |
  | C12 | `docs/a.md`: a `~~~` line inside a backtick fence, then an edit after it (still inside) | `branch/save_branch` |
  | C13 | `docs/specs/x.md` with a `---` block and no closing `---`; a line in it edited | `branch/save_branch`, `other` |
  | C14 | blank line added to `config/x.json` | `branch/save_branch` |
  | C15 | prose edit in `skills/x.md` | `branch/save_branch` |
  | C16 | `docs/a.md` deleted | `branch/save_branch` |
  | C17 | binary (NUL) file `assets/x.bin` changed | `branch/save_branch` |
  | C18 | a NUL byte added to `docs/a.md` | `branch/save_branch` |
  | C19 | frontmatter edit in `.claude/agents/x.md` | `branch/save_branch`, `other` |
  | C20 | blank line added to `AGENTS.md` | `branch/save_branch`, `other` |
  | C21 | `src/x.c`: `+/*` added before an existing code line and `+*/` after it | `substantive/branch_and_escalate` (not `trivial`) |
  | C22 | `web/x.css`: `+/*` added before existing rules and `+*/` after them | `branch/save_branch` |
  | C23 | `site/x.html`: `+<!--` before existing markup and `+-->` after it | `branch/save_branch` |
  | C24 | `src/x.py`: `+# x` added inside a `"""` docstring (rejected as trivial) | `substantive/branch_and_escalate` |
  | C25 | `src/x.py`: blank line added inside a `"""` string | `substantive/branch_and_escalate` |
  | C26 | `src/x.sh`: `# x` added inside a `<<EOF` body | `substantive/branch_and_escalate` |
  | C27 | `.github/ci.yml`: `# x` added inside a `run: |` block scalar | `branch/save_branch` |
  | C28 | `src/star_deref.c`: `*ptr = 1;` added | `substantive/branch_and_escalate` |
  | C29 | `src/mul.c`: `    * b;` added under `x = a` | `substantive/branch_and_escalate` |
  | C30 | logic edit in `src/x.py` plus prose edit in `docs/a.md` | `substantive/branch_and_escalate` |
  | C31 | `src/x.py` deleted | `substantive/branch_and_escalate` |
  | C32 | binary file `src/x.bin` changed | `substantive/branch_and_escalate` |
  | C33 | logic edit in root `x.py` | `substantive/branch_and_escalate` |
  | C34 | logic edit in `lib/x.rb` | `substantive/branch_and_escalate` |
  | C35 | logic edit in `tests/test_x.py` | `substantive/branch_and_escalate` |
  | C36 | logic edit in `skills/x/run.py` | `substantive/branch_and_escalate` |
  | C37 | rename `src/a.py` → `docs/a.md`; both names in `paths` | `substantive/branch_and_escalate` |
  | C38 | rename `docs/a.md` → `src/a.py`; both names in `paths` | `substantive/branch_and_escalate` |
  | C39 | shebang line edited in `bin/x.py` | `substantive/branch_and_escalate` |
  | C40 | no tracked dirt | `clean/resume` |
  | C41 | an untracked file only | `clean/resume`; the file is not in `paths` |
  | C42 | tracked edit plus an untracked file | the file is not in `paths` or `files` |
  | S01 | `_line_kinds`, `.py .pyi .sh .bash .yml .yaml .toml .rb .pl`: `# c`, `#!/x`, `  ` | `comment`, `text`, `blank` |
  | S02 | `_line_kinds`, every C-like suffix: `// c`, `*ptr = 1;`, `*out`, `*=`, `* b;`, `*/` in normal state | `comment`, then `text` ×5 |
  | S03 | C-like `/*`, ` * x`, ` */` in sequence; the same with a code line between | `comment` ×3; the code line between is `comment` too (inside the block) and the `*/` after it is the closer |
  | S04 | `_line_kinds`, `.css`: `// x`, `/* x */`, blank | `text`, `comment`, `blank` |
  | S05 | `_line_kinds`, `.md`: `<!-- x -->`; `<!--`, `t`, `-->`; `# Heading` | `comment`; `comment` ×3; `text` |
  | S06 | `_line_kinds`, `.json`: blank, `// x` | `text`, `text` |
  | S07 | `.py`: `"""`, `# x`, ``, `"""`; `.toml`: `'''`, `# x`, `'''` | all `text` |
  | S08 | `.md`: fence open, `# x`, `~~~`, `y`, fence close | all `fenced`; `~~~` does not close |
  | S09 | `.py` `# note\rimport os`; `.js` `// note\x0cfoo();`; `.py` `\x0c` alone; `.py` `# x\r` (CRLF) | hazard; hazard; `blank`; `comment` |
  | S10 | `.sh` with `<<EOF`; `.yml` with `k: |`; `.js` with a backtick; `.c` `x = "a/*b";` | hazard each |
  | S11 | `chmod +x` on the non-code file `docs/tool.txt` | `branch/save_branch`, `other` |
  | S12 | `chmod +x` on `src/x.py` | `substantive/branch_and_escalate`, `code` |
  | S13 | typechange (file → symlink) on `docs/a.md` | `branch/save_branch` |
  | S14 | symlink `docs/link` retargeted | `branch/save_branch` |
  | S15 | dirty submodule (gitlink, content dirty, `Y` non-blank); also staged-only pointer bump | first: `branch/escalate`, `recommended == []`, no raise; second: `branch/save_branch` |
  | S16 | staged-only comment edit in `src/x.py` (combined pair) | `trivial/commit` |
  | S17 | staged fence edit in `docs/a.md` (staged half: `HEAD:` vs `:`) | `branch/save_branch` |
  | S18 | `src/x.py`: staged comment edit plus unstaged logic edit | `substantive/branch_and_escalate` |
  | S19 | `docs/a.md`: staged prose edit plus unstaged fence edit (unstaged half: `:` vs worktree) | `branch/save_branch` |
  | S20 | `+# note\rimport os` added to `src/x.py` | `substantive/branch_and_escalate`, `code` |
  | S21 | `+// note\x0cfoo();` added to `src/x.js` | `substantive/branch_and_escalate`, `code` |
  | S22 | `+\x0c` alone added to `src/x.py` | `trivial/commit`, `comment` |
  | S23 | non-UTF-8 byte in a `docs/a.md` prose edit; in a `src/x.py` code line | `trivial/commit`; `substantive/branch_and_escalate`; no raise |
  | S24 | config `diff.external`, textconv driver on `*.md`, `diff.noprefix=true`, `diff.mnemonicPrefix=true`, `diff.renames=false`, `status.renames=false`; run C01, C37 and S26 | same `class`, `action`, `paths` and `files` as without |
  | S25 | staged `git mv` of `docs/a.md` → `docs/b.md` | one `files` row, `status` `R `, `orig_path` `docs/a.md`; `paths` has both names |
  | S26 | staged `git mv` plus an unstaged edit on the new name | row `status` `RM`; both halves classified; worse class wins |
  | S27 | unstaged `mv docs/a.md docs/b.md` | only the delete (` D`) listed; `docs/b.md` untracked and absent |
  | B01 | `merge.base: x`, `base: y` | base `x` |
  | B02 | `base: y`, no `merge` | base `y` |
  | B03 | config with neither, no `--base` | base `main` |
  | B04 | config with neither, `--base develop`, checkout on `develop`, trivial edit | base `main`, `on_base: false`, `hold`, class `trivial`; equals `_close_cfg` with `repo.base: develop` |
  | B05 | no `.nw/project.yaml`, `--base develop`, on `develop` | base `develop`, `on_base: true`, `commit` |
  | B06 | no `.nw/project.yaml`, no flag | base `main` |
  | B07 | invalid YAML in `.nw/project.yaml` | exit 3, loader message on stderr |
  | B08 | `merge: not-a-mapping` | exit 3, loader message on stderr |
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

### Test record (2026-11-11)

- `uv run pytest -q tests/test_main_dirty_plan.py`: 141 passed. It holds one parametrized test
  per case-table row (C01-C42, S01-S27, B01-B08), the AC3 sequencer / unmerged-code / two-marker /
  linked-worktree cases, the AC5 whole-list assertions and runs, the AC6 stale-index snapshot test,
  the AC7 CLI cases and the reference-index assertion.
- `python3 tools/test_tiers.py --check`: exit 0. `python3 tools/spec_lint.py`: exit 0.
- `python3 tools/affected_tests.py --run uv run pytest -q`: `--guards --exclude-marks integration,tui,remote,perf -n 3`: 1634 passed, 1 skipped (it first
  caught `tests/test_rich_output_guard.py`: the command prints through `console`/`R.warn`, not `click.echo`).
- Mutation check (by hand): removed `--literal-pathspecs` from `_recommended`;
  `test_literal_pathspecs_protect_a_star_name` failed (the glob staged the untracked `axb.md`);
  restored.
- Manual scratch repo: frontmatter-only edit → `trivial commit`; pasting the `recommended` lines
  gave a clean tracked tree and one `chore: land stray trivial main-checkout edits` commit. A logic
  edit to `src/x.py` plus an untracked file → `substantive branch_and_escalate`; its lines left the
  checkout on `main`, tracked-clean, the edit on `wip/main-dirty-<ts>`, the untracked file still there.
- Review loop 2: `uv run pytest -q tests/test_main_dirty_plan.py` passes (309 tests): whole-list `recommended`
  assertions for commit, `save_branch`, `branch_and_escalate` (rm, mv, unstaged delete, logic and a mixed
  staged-only plus unstaged set), hold dirty, hold clean, resume, merge and gitlink; each run list checks
  the edits on `<wip>` or HEAD (`ls-tree`, `show`, `diff --name-status`), untracked files still untracked
  and absent from the tree, and the wip branch descending from the feature branch for hold; parametrized
  hazard table (every line-break character, every suffix hazard, closer and backslash hazards); file-level
  indent, move-across-comments, docs/html comment-only, frontmatter-after-fence and unclosed-frontmatter
  cases; stderr-separated CLI checks of the loader messages (B07, B08, non-string base), exit 2 and 3;
  real AA/DU/UD/UU conflicts; the AC6 snapshot over merge, hold, staged, rename and gitlink; a
  no-wrap-on-a-terminal test. Mutation re-run: removing `--literal-pathspecs` fails 9 tests.
  `python3 tools/test_tiers.py --check` and `python3 tools/spec_lint.py` exit 0.
- Review loop 3: `uv run pytest -q tests/test_main_dirty_plan.py` passes (354 tests). New false-`trivial`
  guards: a staged delete (`git rm --cached`) whose same-name worktree file differs (code and docs twin),
  newly added `A ` and `AM` files (an absent `old` is never empty), whole `recommended` lists and runs on a
  `develop` base and on a `merge.base` / `base` from the config, a relative `--root`, the `-3` wip suffix,
  non-ASCII paths with and without `core.quotePath`, frontmatter plus body edits and the EOF newline, a
  dirty gitlink with a merge marker, EOF-newline-only and unknown-suffix edits, the extra hazards and a
  non-UTF-8 path. Mutations re-run by hand: reading the worktree for a staged delete, hardcoding `main` in
  `switch <base>`, and treating an absent `old` as empty each fail a test.
  `python3 tools/test_tiers.py --check` and `python3 tools/spec_lint.py` exit 0. The full suite was left
  to gating.
- Review loop 4 (test and adversarial review fixes): `uv run pytest -q tests/test_main_dirty_plan.py`
  passes (452 tests). New rows make mode and symlink checks load-bearing: `chmod +x` alone on a docs file
  with frontmatter, prose edit plus `chmod +x`, comment-only edit plus `chmod +x` on `src/x.py`, and a
  retargeted `docs/link.md` symlink, each unstaged and staged; an MM case with a staged fence edit reverted
  in the worktree; non-docs `.md` rows; the `"""`/`'''` pin; adjacent-marker `in_progress` precedence; a
  code-path table; the `LC_ALL=C` / `GIT_OPTIONAL_LOCKS=0` child env; `--root` as a file (exit 2) and an
  `OSError` in the loader (exit 3); multi-line quoted literals, pragma comments, Unicode-only whitespace
  lines, option-like base and control-character paths. Mutations re-run by hand: deleting the `om != nm`
  block fails 6 tests, dropping `120000` from the mode check fails the 2 symlink tests.
  `python3 tools/test_tiers.py --check` and `python3 tools/spec_lint.py` exit 0. The full suite was left to
  gating.
- The full suite was left to gating.

### As shipped (2026-11-11)

- Deviation from Inputs: the `--raw` mode lookups use the plumbing commands `git diff-index HEAD`,
  `git diff-index --cached HEAD` and `git diff-files` (same `-z` raw framing, same flags) instead of
  `git diff … --raw`. `git diff` rewrote a stale index even with `GIT_OPTIONAL_LOCKS=0` (git 2.55),
  which broke AC6; the plumbing commands write nothing.
- A staged delete (`X` = `D`) is read with `new` absent, even if an untracked file of that name sits
  in the worktree, so a delete is never classified as an edit.
- Comment-only also requires `old != new`, so an identical pair is never `comment`.
- A backtick fence line inside an open HTML comment (`.md .html .xml`) sets `hazard`: the two states
  cannot be told apart from lines.
- The Python triple-quote scan opens a string at the first triple quote on a line even inside a `#`
  comment or another string literal. That only makes more lines `text` (stricter), except when a
  fake open string later swallows a real opener; that stays an accepted residual risk.
- `plan_clear` returns the schema's keys only; a dirty gitlink is derived from the rows.
- Review loop 2 hardening (all strictly toward `branch`, nothing in the spec-mandated lists removed):
  a block-comment or HTML-comment closing line sets `hazard` unless it holds exactly one closer and
  ends with it (`*/ f(); /* y */` is a hazard); a line ending in `\\` sets `hazard` for `.py .pyi
  .sh .bash .rb .pl` and the C-like suffixes (a backslash splices the next line, so a comment or
  blank edit beside it can change behaviour). `plan_clear` makes `root` absolute after the worktree
  check, so `recommended` never holds `git -C .`; `OSError` while reading a worktree file or the
  project config becomes a `PlanError` (exit 3); text mode prints paths and commands with
  `soft_wrap=True` so a terminal never folds a pasteable line.
- Review loop 3 hardening (again only toward `branch`): comment-only is false when the two sides differ
  in the final newline (`_split` drops it) and for a suffix with no kinds table (so `x.json` or `x.txt`
  is never `comment`); an unterminated Python triple-quoted string, a Ruby/Perl `__END__` / `__DATA__`
  line, a `"""` in `.java` and a YAML block scalar header with indent/chomp indicators or a comment
  (`|2`, `|  # x`) set `hazard`; `root` is `abspath` only when that has the same realpath, else
  `realpath`; the command exits 3 before printing when a path is not valid UTF-8.
- Review loop 4 hardening (only toward `branch`; no spec-mandated list changed): `blank` is ASCII-whitespace
  only (`" \t\x0b\x0c"`), so a NBSP/U+3000/U+2000-U+200A line is `text`; a `text` line with an odd count
  of unescaped `"` (and `'` for sh, rb, pl, yaml) in `.sh .bash .rb .pl .yml .yaml .rs .jsx .tsx`, a Rust
  raw string, JSX markup in `.jsx`/`.tsx` and a Go `import "C"` set `hazard`; pragma comments (Go `//go:`,
  `// +build`, `//export`; Python coding cookie; Ruby `frozen_string_literal`/`encoding`; TS/JS `@ts-`,
  `/// <reference`, `@flow`, `@jsx`, source maps) are `text`, not `comment`; a base starting with `-` or
  empty exits 3; text mode exits 3 when a path has a control character (`--json` still works). Skipped: a
  size cap on blobs.
- Code: `src/nightwright/agentic/main_dirty.py`, the `main-dirty-plan` command in `cli_queue.py`,
  the reference-index entry, `tests/test_main_dirty_plan.py`.

## Rollout / migration

No data move, no caller yet: the command is additive. Queued under project Meta-Tools; next in
the chain is SPEC-0547.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed, including the scratch-repo check; affected tests pass.
- [x] No regressions.
- [x] `docs/product/reference/index.md` names `nw queue main-dirty-plan` (asserted by AC7).
- [x] As shipped records the AC5 `--literal-pathspecs` mutation run (flag removed, test fails).
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` regenerated; `python3 tools/spec_lint.py` exits 0.

## Open questions

None.
