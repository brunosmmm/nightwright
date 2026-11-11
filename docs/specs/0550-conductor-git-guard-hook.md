---
id: SPEC-0550
title: Conductor git-guard, a cwd-aware PreToolUse hook that denies raw git writes in a main checkout
status: accepted
owner: user
created: 2026-11-11
updated: 2026-11-11
kind: feature
tags: [agentic, conductor, launch]
depends_on: [SPEC-0549]
supersedes: [SPEC-0544]
source_idea: IDEA-1159
---

## Context

The human said yes to the conductor clearing `main_dirty` through one audited verb (campaign
`IDEA-1159-20261110-02ed`, resume note seq 437), and at ESC-0067 chose to split
[SPEC-0537](./0537-conductor-must-clear-main-dirty-itself-a-stray.md). The steward's part C,
[SPEC-0544](./0544-conductor-runs-main-dirty-apply-for-trivial.md), was sized `xl` by spec
review (verdict `split`): the hook is the bulk and independent of the verb flag. It is split
into this guard spec and [SPEC-0551](./0551-conductor-runs-main-dirty-apply-trivial-only.md),
which grants the conductor the trivial clear and `depends_on` this one, so **the conductor never
gets the write before the guard is merged**.

The chain, in queue order: [SPEC-0546](./0546-main-dirty-plan-classifier.md),
[SPEC-0547](./0547-main-dirty-conductor-owned-plan-and-escalate.md),
[SPEC-0548](./0548-main-dirty-apply-core-commit.md),
[SPEC-0549](./0549-main-dirty-apply-wip-save-and-hold.md), **SPEC-0550 (this spec)**,
[SPEC-0551](./0551-conductor-runs-main-dirty-apply-trivial-only.md). The hook needs nothing
from SPEC-0546–0549; it is queued after them to keep the human's risk order (plan, human-run
apply, then conductor guard and grant).

The branch's review found raw git write paths into the **main checkout** open to the conductor:
`worktree add *` and `switch --create` unrestricted, `commit -m *` allowing `-a` / `--amend` on
the base, a project base interpolated into an allow glob, a deny rule per project directory.
The branch's blanket `commit *` / `add -- *` deny then blocked the conductor's worktree
sub-agents from committing, which [SPEC-0496](./0496-subagent-worktree-commit-allowlist.md)
(human-requested) allows: a sub-agent spawned with `isolation: worktree` runs plain
`git add <paths>` / `git commit -m …` in its own worktree. Permission rule strings match the
command text only; they cannot see which checkout a plain `git commit` runs in. So the
main-checkout ban has to be a check that knows the working directory.

**Carries SPEC-0544 AC3 (AC1), AC4 (AC2), AC5 (AC3), AC6 (AC4), AC7 (AC5).** AC6 is new, from
the review (the entry point is decided up front). It folds in the SPEC-0544 review's hook
findings: merged-punctuation tokens, `commenters`, backslash-newline and redirects; unexpanded
`$`, `~`, globs and braces in any resolved path; `cp` and `worktree add` parsing; internal
errors fail closed; the payload premise is checked first; the launch check's settings path.

## Goals / Non-goals

**Goals**
- A conductor session cannot write the main checkout with raw `git` or `cp`; worktree commits
  (SPEC-0496) keep working; the guard is installed and checked at launch.
- The hook costs a non-conductor session almost nothing: a light script, no click, yaml, rich
  or store import.
- Every form the hook cannot model fails closed for a conductor session and never blocks a
  human's session.

**Non-goals**
- The `--trivial-only` flag, any static allow or deny rule change, `CONDUCTOR_CURSOR_POLICY`,
  and every `skills/nw-conduct/SKILL.md` change (all SPEC-0551).
- The steward's allow or deny lists, `.claude/settings.json` allow rules (SPEC-0496), and what
  the SPEC-0496 paragraphs tell sub-agents to run.
- **Defence against a deliberately adversarial agent (threat model).** The hook guards a
  cooperative agent against mistakes. Escapes through another interpreter (`uv run python -c`,
  `bash -c`, `sh -c`, `eval`, `xargs git`, a script file) are out of scope; the conductor's
  `dontAsk` allow-list does not permit them, which is the control for those. Writes by programs
  other than `git` and `cp` (including their redirects, step 1) are governed by that allow-list
  too.

## Decision

1. A PreToolUse hook, console script `nw-conductor-git-guard`, matcher `Bash`. It acts only for
   a registered conductor session id. It tokenizes with a quote-aware `shlex`, tracks the
   directory each `git` / `cp` segment runs in, and on a primary worktree allows only a named
   set. **Anything outside the grammar it models fails closed** (deny), with one narrow
   exception that keeps SPEC-0496 worktree commits working (step 2). It also denies any
   `main-dirty-apply` form without `--trivial-only` (step 7), unsupported commands included.
2. `agent_permissions.sync` installs it; `nw queue conductor-launch` (resolved claude runner)
   refuses to start a conductor while the settings entry is missing **or** the
   `nw-conductor-git-guard` executable is not on the launch PATH, so the ban is never silently
   absent.
3. No static allow or deny rule changes, and no skill change: SPEC-0551 does those.

## Design

### Premise check (first implementation step)

Before writing the hook, log three PreToolUse payloads from a real `claude -p` conductor
session (fields `session_id`, `cwd`, `agent_id` if present) and record them under **Observed
payloads** below:

1. the first Bash call of the conductor session;
2. a Bash call from an `isolation: worktree` sub-agent it spawns;
3. a Bash call made right after an earlier call ran `cd <other dir>` (a directory different from
   the session's start directory), to record whether `cwd` reflects the shell's persisted
   directory or stays at the session's original one.

The design assumes the sub-agent's payload carries the conductor's `session_id` and its own
worktree as `cwd`, and that the payload `cwd` tracks the shell's current directory (payload 3).
If either of the first two is false, stop and revise this Design before building the rest. If
payload 3 shows `cwd` does **not** reflect the earlier `cd`, the **persisted-cd fallback**
applies: the hook also denies every `cd` segment, in any command, whose target is unknown or
resolves into a primary worktree (step 4's test), so the conductor can never persist a shell
directory in the main checkout; the AC2 case `cd M` alone from `W` is then denied and the
conductor skill is told not to `cd` across calls. If `cwd` does reflect it, the fallback is not
built and AC2's case stays out.

### Observed payloads

Filled by the implementer at implementation time (the three payloads above, the post-`cd`
`cwd`, and whether the fallback was needed).

### The hook

`src/nightwright/agentic/git_guard.py`, entry `main()`, installed as the console script
`nw-conductor-git-guard` in `pyproject.toml`. It always exits 0, denies through the PreToolUse
JSON decision (`permissionDecision: deny`, reason below), and is silent otherwise. Order of
work, so a non-conductor call stays cheap: read stdin, look up `session_id` in the conductor
session registry, and return silently unless it matches, before importing the tokenizer or
running any git. The lookup is a small **local reader** in `git_guard.py`: it computes
`Path(paths.data_dir()) / "agentic" / "conductor" / "sessions.jsonl"` (`nightwright.paths`
imports only `os`) and reads it the way `conductor_cost.sessions` does (a line counts when it
is a JSON object with a truthy `session_id` and `reader == "conductor"`; unparseable lines and
a final line without a newline are skipped; a missing or unreadable file means no match). It
must **not** import `conductor_cost`, `store`, `config`, `render`, yaml or rich: `store` pulls
`export`, `render` (rich), `rules` (yaml) and `codewatch`, and `config.load_config()` pulls yaml
and calls `sys.exit(1)` when yaml is missing. `git_guard.py` imports only `json`, `os`, `sys`,
`pathlib` and `nightwright.paths` at top level, never `click` (the `nightwright` and
`nightwright.agentic` package `__init__` files stay import-light); the tokenizer, directory
tracking and git calls live in `nightwright.agentic.git_guard_parse`, imported only after the
match. Unreadable JSON, a registry error, `SystemExit` and any other exception from the lookup,
and non-conductor sessions are silent (fail open), as SPEC-0382 requires, so the hook can never
block a human's session. **Once the session is a
registered conductor's, any exception** in parsing or resolution denies with
`conductor-git-guard: cannot verify internal error (SPEC-0550)`.

The deny reason is `conductor: no raw git writes in the main checkout <toplevel>; escalate it
(SPEC-0550)`, or for a fail-closed deny `conductor-git-guard: cannot verify <what> (SPEC-0550);
use a plain single git command in the worktree`.

**1. Tokenize (quote-aware).** `shlex.shlex(command, posix=True,
punctuation_chars="();<>|&\n")` with `whitespace = " \t\r"`, `whitespace_split = True` and
`commenters = ""` (`#` stays in words, as in bash). Words lose their quoting, so the checks below
run on words; a single-quoted `'$(x)'` is treated like an unquoted one (stricter, never looser).
`shlex` merges adjacent punctuation into one token (`&&\n`, `;\n`, `\n\n`); each
punctuation-only token is split with the regex `&&|\|\||>>|>&|<<|[;|\n()<>&]` into operators,
and a token that does not split fully into those (`;;`, `|&`, `&>`) makes the command
unsupported. Checked on Python 3.14: `cd M` newline `git commit -m "a; b && c"` gives `cd M`,
`\n`, then the commit with its message as one word. The punctuation merging is the
version-sensitive part, so the tokenizer unit table must pass on the oldest supported
interpreter (Python 3.12, `requires-python = ">=3.12"`) as well as 3.14. The command is
**supported** when all of these hold; otherwise it is **unsupported** (step 2):

- `shlex` raises no error (no unbalanced quote); the raw text has no backslash-newline;
- no `(`, `)`, `{`, `}` grouping token, no `<<` heredoc operator outside quotes, no `&`
  background operator, no `pushd`, `popd`, and no word equal to `case`, `select`, `function` or
  `[[` (constructs the segment model does not cover);
- no backtick and no `$(` in any word, except the one form agents use for commit messages: a
  word that is exactly `$(cat <<'EOF'` newline … newline `EOF` newline `)` (or `<<EOF`, any
  delimiter word), whose body lines contain no backtick and no `$(`. That word is treated as
  text. No other word contains a newline.

Segments are the token runs between `;`, `&&`, `||`, `|` and newline. In each segment, leading
`NAME=value` words, `env` (and its `NAME=value` words; any `env` option makes it unsupported),
`command`, `exec`, the shell reserved words `if`, `then`, `else`, `elif`, `do`, `while`,
`until`, `!` and `time` (any `time` option makes it unsupported) are skipped, so the word after
them is the program (`then git commit -m x` runs the program `git`); a word whose basename is
`git` or `cp` (`/usr/bin/git`) is that program. **Redirects:** `<`, `>`, `>>`, `>&` and the
word after each are removed from the segment's arguments, and a digit-only word right before
one is removed with it (`2>&1`). **Redirect targets are checked for `git` and `cp` segments
only:** an output redirect (`>`, `>>`, or `>&` followed by a non-digit word) on such a segment
has its target checked as a `cp` destination (step 6). A redirect on any other program
(`echo x > M/f`) is not checked; those programs are governed by the conductor's `dontAsk`
allow-list (Non-goals).

**2. Unsupported commands fail closed.** An unsupported command is denied when its raw text
matches `\b(git|cp)\b|main-dirty-apply`, so the verb (step 7) cannot slip through a form the
grammar does not model. The one exception keeps SPEC-0496
commits that use a form the grammar does not model (for example `git commit -F - <<EOF`): it is
silent when the text does not contain `main-dirty-apply`, the payload `cwd` is a linked
worktree (step 4) **and**, splitting the raw text on
whitespace and the characters ``;&|(){}<>$\`"'`` (shell punctuation, backslash, backtick and
quotes), no word is `cp`, `cd`, `pushd`, `-C` or `-c`, none starts with `--chdir`, `--git-d`,
`--wo` or `--exec-path`, and the text contains none of `core.worktree`, `GIT_DIR`,
`GIT_WORK_TREE`, `GIT_INDEX_FILE`, `GIT_COMMON_DIR`. With none of those, nothing in the command
can point git anywhere but the linked worktree. The check is deliberately coarse: a commit
message that happens to contain one of those words is denied, and the reason tells the agent to
use a plain single command. Commands that mention none of `git`, `cp` and `main-dirty-apply`
are silent.

**3. Directory tracking.** The directory starts at the payload's `cwd` (an absolute string;
when it is missing or not a string, the start is **unknown**). A segment `cd <dir>` changes the
directory for the following segments only when it is followed by `;`, `&&` or a newline; a `cd`
before `|` or `||`, or a `cd` whose argument is missing or `-`, makes the directory **unknown**
from there on. A `cd` whose literal target does not exist or is not a directory also makes the
directory **unknown** from there on: followed by `;` or a newline the shell stays in the old
directory and a later `git` runs there, possibly in the main checkout. The one exception is
`cd X &&`: the segments chained after it by `&&` (up to the next `;`, newline or `||`) do not
run, so they are silent; after that point the directory is unknown. **Every path word the hook resolves** (the `cd` argument, each `-C` value, each
`cp` source, destination and `-t` value, each redirect target, the `worktree add` target) that
contains `$`, `~`, `*`, `?`, `[`, `{` or a backtick is unknown: the shell would expand it, so the
hook cannot know where it points. Paths are resolved against the current directory with
`os.path.realpath`. For a `git` segment, global options before the subcommand are read in order:
`-C <p>` (cumulative, relative to the previous directory), `--literal-pathspecs`, `--no-pager`,
`-P`, `--no-replace-objects`, `--no-optional-locks`. Denied outright, whatever the directory:
any option beginning `--git-d` or `--wo` (stricter than git, which does not abbreviate global
options), `-c`, `--bare`,
`--exec-path`, any other option before the subcommand, and a `NAME=value` prefix that sets
`GIT_DIR`, `GIT_WORK_TREE`, `GIT_INDEX_FILE` or `GIT_COMMON_DIR`. A `git` or `cp` segment whose
directory is unknown is denied.

**4. Primary or linked.** For a directory, run
`git -C <dir> rev-parse --path-format=absolute --git-dir --git-common-dir` (timeout 2 s,
`GIT_OPTIONAL_LOCKS=0`) and compare the two lines after `os.path.realpath`. Equal: the
directory is in a **primary worktree** (a main checkout, from any subdirectory of it).
Different: a **linked worktree** (every `.nw-worktrees/**` campaign worktree and every harness
`isolation: worktree` tree, including `<root>/.claude/worktrees/*`, which may be nested inside the primary's own directory): the
segment is silent. The test is this `rev-parse` comparison, never path containment. Exit 128
with `not a git repository` or `cannot change to` is silent (git would fail the same way) only
for a `-C` value that is a fully literal path, or for a `git` segment chained after a failed
`cd X &&` (step 3); a missing `cd` target before `;` or a newline is unknown (step 3), not
silent. Any other non-zero exit, a timeout, or output that is not two absolute
paths: deny (fail closed).

**5. The primary-worktree allow set.** On a primary worktree a `git` segment is silent only when
its subcommand and arguments are one of:

- read-only: `status`, `log`, `diff`, `show`, `rev-parse`, `rev-list`, `merge-base`,
  `ls-files`, `ls-tree`, `cat-file`, `describe`, `blame`, `shortlog`, `show-ref`,
  `for-each-ref`, `grep`, `version`, `help`;
- `branch` with no argument or only `--list` (and its patterns), `-a`, `-r`, `-v`, `-vv`,
  `--show-current`;
- `worktree list`; `worktree add` whose target resolves under a `.nw-worktrees` directory. The
  target is the first positional after the options `-b <name>`, `--detach`, `--lock`,
  `--no-checkout`; any other option denies, in particular `-B <name>` and `-f` / `--force`
  (`git worktree add -f -B main <path>` would force-reset a branch the primary has checked
  out, leaving the main checkout looking dirty);
- `fetch`, `push` (they move refs and remotes, not the checkout; the static push rules govern
  them).

Every other subcommand is denied there: `checkout` and `switch` in any form, `restore`
(`--staged` included), `add`, `commit`, `rm`, `mv`, `merge`, `rebase`, `reset`, `cherry-pick`,
`revert`, `stash`, `apply`, `am`, `hash-object`, `update-index`, `tag`, `notes`, `gc`, and any
subcommand not listed.

**6. `cp`.** The destination is the `-t X` / `--target-directory=X` value, else the last
positional (after `--`, everything is positional; `-T` is accepted). Short-option clusters made
only of the letters `aflnPpRrsuvx` are skipped; a cluster ending in `t` takes the next word as
the target; any other option denies. The segment is denied when the destination's nearest
existing ancestor directory is in a primary worktree by step 4's test (so a destination in a
linked tree inside the primary, such as `.claude/worktrees/w/`, is silent), or when the
destination cannot be determined. `cp` is not in the conductor allow-list, so `dontAsk` already
refuses it; the hook check stays because a user-level `Bash(cp *)` allow in
`~/.claude/settings.json` would reopen it.

**7. The verb.** The program is detected like `git`, after the same prefix skipping (step 1): a
word whose basename is `nw` (`/home/u/.local/bin/nw`), or a word whose basename is `uv`
followed by `run`, then options (words starting with `-`; `--project`, `--directory`, `--with`,
`--python`, `-p`, `--package`, `--env-file`, `--extra`, `--group` take the next word as their
value), then a word whose basename is `nw`. A segment of that program that contains the word
`main-dirty-apply` is denied unless the word right after `main-dirty-apply` is
`--trivial-only`. Unsupported commands that contain `main-dirty-apply` are denied by step 2, so
the verb is covered for a form the static tails miss. Until SPEC-0551 the static deny of
SPEC-0548 also blocks every form; this step backs SPEC-0551's static tails.

### Install and launch check

`agent_permissions.sync` adds a second PreToolUse entry,
`{"matcher": "Bash", "hooks": [{"type": "command", "command": "nw-conductor-git-guard || true"}]}`,
idempotently, beside the unchanged `AskUserQuestion` entry. `sync()` returns
`{"added", "removed", "hook", "git_guard_hook"}` on every path, the no-op early return included
(`git_guard_hook: False`): `hook` keeps its meaning, `git_guard_hook` is true when the git-guard
entry was added now. `git_guard_hook_missing(path)` mirrors `hook_missing` and matches that exact
command string. `nw skills install` prints `conductor-git-guard hook  added to <path>` when it
adds it. `nw skills list` prints one `hooks` line (today it reports only `allow rules`):
`hooks` counts the missing entries among {conductor-guard (`hook_missing`), conductor-git-guard
(`git_guard_hook_missing`)}, and prints `hooks  present` for 0 missing and `hooks  <n> missing
(run nw skills install)` otherwise, so an install from before this spec prints
`hooks  1 missing (run nw skills install)`.

**Launch check.** The check applies when `plan["runner"]` is claude. That is the **resolved**
runner from `launch.build_plan` (`--runner`, else the stored runner on `--resume`, else the
host default from `resolve_runner(cfg)`), not the raw `--runner` option, which is None on a
default launch. The check result `git_guard_hook` is true only when **both** hold: the settings
entry is present (`git_guard_hook_missing(agent_permissions.default_settings_path())` is false)
**and** `agent_permissions.git_guard_executable(env)` resolves, which is
`shutil.which("nw-conductor-git-guard", path=env.get("PATH"))` on the launch environment's
PATH (the settings command `nw-conductor-git-guard || true` would otherwise swallow a shell
exit 127 and the hook would silently never run). `nw queue conductor-launch` refuses to start
(exit 1) while it is false, with a message naming the missing settings entry and/or the missing
executable `nw-conductor-git-guard`, and `nw skills install` or reinstalling nightwright;
`--dry-run` starts nothing, exits 0, and reports `git_guard_hook: false` in its JSON and a
warning line in text. For a resolved cursor runner nothing is checked and the JSON reports
`git_guard_hook: null` (the cursor runner has no hooks; its guard is
`CONDUCTOR_CURSOR_POLICY`, SPEC-0551).

Tests never read the real `~/.claude/settings.json`: see the autouse fixture in the Test plan.

**Latency.** The hook runs on every Bash call of every session on the machine. As shipped
records the wall time of 20 non-conductor calls; the budget is a 300 ms median.

## Alternatives considered

- **Static deny globs (`Bash(git commit *)`, …), as on the branch.** They cannot see the working
  directory and block SPEC-0496's in-worktree commits, which the human asked for.
- **Per-project `Bash(git -C <root> commit *)` denies.** They miss the plain cwd-relative form
  and interpolate paths into rules, a review major.
- **Ship the hook with the verb permission** (SPEC-0544's rebuttal). Rejected on re-review: 11
  ACs over independent subsystems. A `depends_on` from SPEC-0551 to this spec enforces the same
  order (guard before grant) with each part converging on its own.
- **`nw queue conductor-git-guard` through the click CLI, swapped for a light script if slow.**
  The installed command string is what `git_guard_hook_missing` matches, so a later swap would
  strand existing installs; the light script is chosen up front.

## Acceptance criteria

Fixtures: a temp primary checkout `M` at `<parent>/repo` and three linked worktrees made with
`git worktree add`: `W` at `<parent>/.nw-worktrees/P/x` (a sibling of `M`), `W2` at
`M/.nw-worktrees/y` and `W3` at `M/.claude/worktrees/w` (both **nested inside** `M`, the real
layout of this repo); a conductor session registered in a temp cfg; the hook's `main` fed a
PreToolUse payload (`session_id`, `cwd`, `tool_input.command`) on stdin. "Permitted" in
AC1 means the new helper `tests/_permission_rules.py::decide(command, allow, deny)` (Claude Code
semantics: `*` matches any characters including spaces, the whole command must match, deny
beats allow) returns `allow` on the effective rules of `build_plan(cfg, "conductor")`; the
real-binary run is the arbiter.

- [ ] AC1 (SPEC-0544 AC3) **Worktree commits stay permitted.** Hook silent, parametrized:
      `git add a`, `git commit -m x`, `git commit -m "fix: a; b && c | d"`, `git add a#b`, the
      heredoc form `git commit -m "$(cat <<'EOF'` / `x; y` / `EOF` / `)"`, and
      `git commit -F - <<EOF` (the step-2 exception), each with `cwd: W` and again with
      `cwd: W2` (nested in `M`); `git -C W add -- a`, `git -C W2 add -- a`,
      `git -C W commit -m x` and `git -C W rm -f -- a` with `cwd: M`; `git status`,
      `git log -1 | head` and `git log 2>&1 | head` with `cwd: M`. Statically, `decide` permits
      `git -C W add -- a` and `git -C W commit -m x` (conductor allow-list) and denies
      `git -C W rm -f -- a` (`Bash(git * rm *)`, unchanged: the hook's silence grants nothing).
      `.claude/settings.json` permits only the plain `git add a` and `git commit -m x` forms
      (SPEC-0496; `tests/test_claude_settings_allowlist.py` unchanged).
- [ ] AC2 (SPEC-0544 AC4) **The same commands against main are denied** with the main-checkout
      reason: `git add a`, `git commit -m x`, `git commit -am x`, `git add -A`,
      `git commit -m "a; b"` with `cwd: M`; the same in a subdirectory of `M`; `git -C M commit
      -m x` and `cd <parent>/repo && git commit -m x` with `cwd: W`;
      `git -C W -C ../../../repo commit -m x` (resolves to `M`); `cd M` + blank line +
      `git commit -m x` from `W`; `git status &&` + newline + `git add a` from `M`;
      `git add a#b; git -C M commit -m x` from `W`; `git \` + newline + `-C M commit -m x` from
      `W` (unsupported, has `-C`); and, with `cwd: W2`, `git -C M commit -m x`,
      `git -C ../.. commit -m x` (resolves to `M`) and `cd M && git commit -m x`. If payload 3
      of the premise check shows `cwd` does not track `cd`, also `cd M` alone from `W` (the
      persisted-cd fallback).
- [ ] AC3 (SPEC-0544 AC5) Denied against `M`: `git rm -f -- a`, `git checkout -- .`,
      `git checkout HEAD -- a`, `git checkout HEAD a`, `git checkout other`, `git checkout -b x`,
      `git switch -c x`, `git switch -f main`, `git switch --discard-changes main`,
      `git restore a`, `git restore --staged a`, `git merge --ff-only x`, `git rebase x`,
      `git reset --soft HEAD~1`, `git mv a b`, `git stash`, `git apply p`,
      `git hash-object -w -- a`, `git update-index --add --cacheinfo 100644,0,a`,
      `git worktree add ../x`, `git worktree add -B x ../.nw-worktrees/P/y`,
      `git worktree add -f ../.nw-worktrees/P/y`, `git --work-tree=M add a`, `git --wo=M add a`,
      `GIT_DIR=M/.git git add a` (from `W`), `env GIT_WORK_TREE=M git add a`,
      `/usr/bin/git add a`, `command git commit -m x`, `(cd M && git commit -m x)` and
      `{ cd M; git commit -m x; }` (from `W`), `cd $HOME && git add a`, `cd ~ && git add a`,
      `cd - && git add a`, `cd M | git add a`, `git -C "$MAIN" commit -m x`,
      `git -C ~/repo add a`, `git -C /par* add a` (all from `W`), an unbalanced quote
      `git commit -m "x`, `` git commit -m `x` ``, `git commit -m "$(git -C M add .)"`,
      `cp a M/b`, `cp -t M a`, `cp -rt M a`, `cp a $M/b`, `git show HEAD:a > M/a`, and
      the same checkout-writing forms (`git add a`, `cp a M/b`) with `cwd: W2` targeting `M`;
      the keyword forms, each from `M`: `if true; then git commit -m x; fi`,
      `for f in a; do git add $f; done`, `! git add a`, `time git add a`; and the verb
      (step 7): `uv run nw queue main-dirty-apply --root M`,
      `uv run --project M nw queue main-dirty-apply --root M`,
      `/home/u/.local/bin/nw queue main-dirty-apply --root M` and the **unsupported** form
      `(uv run nw queue main-dirty-apply --root M)` (denied by step 2 even from `W`, since the
      linked-worktree exception does not apply to `main-dirty-apply`). Silent against `M` or
      `W`: `git branch --list`, `git worktree list`, `git worktree add ../.nw-worktrees/P/y`,
      `git worktree add -b x ../.nw-worktrees/P/y`, `cp a W/b`, `cp a M/.nw-worktrees/y/b` (a
      linked tree nested in `M`), `cp a M/.claude/worktrees/w/b` (`W3`, a linked tree),
      `echo x > M/f` from `W` (a redirect on a non-`git`/`cp` program is not checked; the
      allow-list governs it), `uv run nw queue main-dirty-apply --trivial-only --root M`
      (step 7 allows it; the static rules decide), `ls; echo "git add a"` (supported, no `git`
      segment), and an unsupported command with none of the words (`(ls)`).
- [ ] AC4 (SPEC-0544 AC6) Fail-open and fail-closed cases, parametrized: silent for a
      non-conductor session id, unreadable JSON, and an empty command, for every AC2 command;
      silent for a write in a directory that is not a git repository (`git -C <non-repo> add a`)
      and in one that does not exist (`git -C /missing add a`, and `cd /missing && git add a`);
      denied for `cd /missing; git add a` and the same with a newline instead of `;`, with `cwd:
      M` (the failed `cd` leaves the shell, and git, in `M`); denied when the `rev-parse` exits non-zero for another reason (monkeypatched), when
      it times out (monkeypatched), when the payload has no `cwd` and the command is
      `git add a`, and when the tokenizer is monkeypatched to raise (reason `cannot verify
      internal error`), while a non-conductor session with the same raising tokenizer stays
      silent. The hook exits 0 in every case.
- [ ] AC5 (SPEC-0544 AC7) `agent_permissions.sync` adds the `Bash` matcher entry with the
      command string exactly `nw-conductor-git-guard || true`, once, idempotently; it leaves the
      `AskUserQuestion` entry and the other keys untouched; every return (no-op included) has
      the four keys `added`, `removed`, `hook`, `git_guard_hook`. `nw skills list` prints the
      exact line `hooks  1 missing (run nw skills install)` for a settings file that has only
      the `AskUserQuestion` conductor-guard entry (and for one with only the git-guard entry),
      `hooks  2 missing (run nw skills install)` for a file with neither, and `hooks  present`
      for a file with both. Launch check (monkeypatching
      `agent_permissions.default_settings_path` and `agent_permissions.git_guard_executable`;
      `conductor-launch --dry-run --json` and a real launch with `os.execvpe` stubbed):
      entry missing: `git_guard_hook: false` and exit 1 naming `nw skills install`; entry
      present but `git_guard_executable` returns None (`shutil.which` finds nothing): the same,
      with a message naming `nw-conductor-git-guard`; entry present and executable resolved:
      `git_guard_hook: true` and the launch proceeds. The check follows the **resolved**
      runner: with no `--runner` and a cfg/host default of claude, a missing entry exits 1; with
      `--runner cursor`, or a cursor default, it is not checked (`git_guard_hook: null`, exit
      0); `--resume` of a campaign whose stored runner is claude is checked, and one whose
      stored runner is cursor is not.
- [ ] AC6 (new) Entry point: `pyproject.toml` declares the console script
      `nw-conductor-git-guard = "nightwright.agentic.git_guard:main"`; a subprocess run of the
      hook with a non-conductor payload leaves `click`, `yaml`, `rich`,
      `nightwright.agentic.git_guard_parse`, `nightwright.agentic.conductor_cost`,
      `nightwright.agentic.store` and `nightwright.config` absent from `sys.modules` (the test
      runs `python -c` that calls `main()` with the payload on stdin, then prints
      `sorted(sys.modules)`), and the same run with a registered **conductor** session id and a
      silent command (`git status` from `W`) imports `git_guard_parse` but still not `click`,
      `yaml`, `rich`, `conductor_cost` or `store`. The test fails if the hook module imports any
      of them for a non-conductor payload. A registry file that is missing, unreadable or has a
      bad line leaves the hook silent.

## Test plan

- **Automated tests:** new `tests/test_conductor_git_guard.py` (`pytestmark = pytest.mark.fast`)
  covers AC1–AC4 and AC6 with the fixtures above, parametrized per command; the tokenizer also
  gets a direct unit table (supported vs unsupported, merged punctuation tokens such as `&&\n`,
  `;\n`, `\n\n`, redirects, segments, directory per segment, the skipped reserved words `if
  then else elif do while until ! time` and the unsupported `case select function [[`). The
  tokenizer unit table runs on the oldest supported interpreter (3.12) in CI or by hand as well
  as 3.14, and the result is recorded in As shipped. New helper
  `tests/_permission_rules.py::decide`, used by AC1 here and by SPEC-0551.
  `tests/test_agent_permissions.py` covers AC5's `sync` and `skills list` cases; the module
  holding the `conductor-launch` CLI tests (`tests/test_agentic_launch.py`) covers the launch
  check. The module `tests/test_conductor_git_guard.py` is the only new `tests/test_*.py`; it
  carries exactly one module-level marker, `pytestmark = pytest.mark.fast` (not `slow`), and the
  extended modules keep their own single marker.
- **Autouse fixture (launch tests).** The launch check reads the real
  `agent_permissions.default_settings_path()` (`~/.claude/settings.json`) and PATH, and
  `tests/test_agentic_launch.py` (lines calling `invoke("conductor-launch")` without
  `--dry-run`, with `os.execvpe` stubbed), `tests/test_cursor_launch.py` and
  `tests/test_pi_runner.py` start a claude conductor and expect exit 0. An autouse fixture in
  `tests/conftest.py` therefore monkeypatches `agent_permissions.default_settings_path` to a
  tmp settings file that already contains the git-guard entry and
  `agent_permissions.git_guard_executable` to a fake executable path. The new launch cases
  override both with their own monkeypatch. No test may read the real settings file.
- **Regression guard:** `tests/test_claude_settings_allowlist.py` (SPEC-0496) passes
  unchanged; `tests/test_agentic_launch.py`, `tests/test_cursor_launch.py` and
  `tests/test_pi_runner.py` pass with only that fixture added, and also with `HOME` pointed at
  an empty directory (`HOME=$(mktemp -d) uv run pytest -q tests/test_agentic_launch.py
  tests/test_cursor_launch.py tests/test_pi_runner.py`). Also `python3 tools/affected_tests.py
  --run uv run pytest -q`, `python3 tools/test_tiers.py --check` (exits 0 when
  `tools/test_tiers.py` exists), `python3 tools/spec_lint.py`.
- **Manual verification (real binary):**
  0. The premise check (Design): log the three payloads (first call, worktree sub-agent, call
     after a `cd`) and record them under Observed payloads, with the post-`cd` `cwd`.
  1. `nw skills install` (adds the hook to `~/.claude/settings.json`).
  2. `uv run nw queue conductor-launch --dry-run --json` → `git_guard_hook: true`.
  3. Run the real `claude -p` once as a registered conductor session, with the plan's
     `--allowedTools` / `--disallowedTools`, `--permission-mode dontAsk`, cwd a nightwright
     worktree, on a scratch repo under the scratchpad with a linked worktree under
     `.nw-worktrees/`.
  4. Have it run `git -C <main> commit -am x` (expect denied by the hook) and
     `git -C <wt> commit --allow-empty -m x` (expect a new commit in the worktree).
  5. Have a sub-agent spawned with `isolation: worktree` (a) run
     `git -C <main> commit --allow-empty -m x`: expect **denied by the hook**; (b) run a plain
     `git add` + `git commit -m x` in its worktree: expect committed.
  6. Have it run `uv run nw queue main-dirty-apply --root <main> --json`: expect denied.
  7. Time 20 non-conductor hook calls; record the median against the 300 ms budget.

  Record each result in As shipped. If 5(a) is not denied or 5(b) is not committed, a premise of
  the Decision is false: the spec is not done, and the finding goes back to Design.

## Rollout / migration

Ships after SPEC-0549. The hook applies once `nw skills install` has added it and the
`nw-conductor-git-guard` console script is on the PATH (a reinstall of nightwright creates it);
until then `nw queue conductor-launch` refuses to start a claude conductor, so the ban is
never silently absent for a launched conductor. A conductor started by hand before the install is not guarded;
`nw doctor`'s existing conductor checks are unchanged. Worktree sub-agent commits are unaffected
(AC1, manual step 5). Queued under project Meta-Tools; SPEC-0551 follows.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed, including the premise check and the real-binary run; affected tests
      pass.
- [ ] No regressions in launch, settings-allowlist, skills tests.
- [ ] Spec body updated to match what shipped (Observed payloads, As shipped with the manual
      results, the latency median and the 3.12 / 3.14 tokenizer results recorded).
- [ ] `docs/LEDGER.md` regenerated; `python3 tools/spec_lint.py` exits 0.

## Open questions

None.

## As shipped

Filled by the implementer at implementation time: each manual-verification result (steps
0–7), the latency median of 20 non-conductor calls against the 300 ms budget, and the tokenizer
unit table result on Python 3.12 and 3.14.
