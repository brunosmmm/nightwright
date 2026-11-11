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

## Decision

1. A PreToolUse hook, console script `nw-conductor-git-guard`, matcher `Bash`. It acts only for
   a registered conductor session id. It tokenizes with a quote-aware `shlex`, tracks the
   directory each `git` / `cp` segment runs in, and on a primary worktree allows only a named
   set. **Anything outside the grammar it models fails closed** (deny), with one narrow
   exception that keeps SPEC-0496 worktree commits working (step 2). It also denies any
   `main-dirty-apply` form without `--trivial-only` (step 7).
2. `agent_permissions.sync` installs it; `nw queue conductor-launch` (claude runner) refuses to
   start a conductor while it is not installed, so the ban is never silently absent.
3. No static allow or deny rule changes, and no skill change: SPEC-0551 does those.

## Design

### Premise check (first implementation step)

Before writing the hook, log one PreToolUse payload from a real `claude -p` conductor session
and one from an `isolation: worktree` sub-agent it spawns (fields `session_id`, `cwd`,
`agent_id` if present), and record them here under **Observed payloads**. The design assumes
the sub-agent's payload carries the conductor's `session_id` and its own worktree as `cwd`. If
either is false, stop and revise this Design before building the rest.

### The hook

`src/nightwright/agentic/git_guard.py`, entry `main()`, installed as the console script
`nw-conductor-git-guard` in `pyproject.toml`. It always exits 0, denies through the PreToolUse
JSON decision (`permissionDecision: deny`, reason below), and is silent otherwise. Order of
work, so a non-conductor call stays cheap: read stdin, look up `session_id` in the conductor
session registry (`CC.sessions(cfg, "conductor")`), and return silently unless it matches,
before importing the tokenizer or running any git. `git_guard.py` imports only `json`, `os`,
`sys` and the registry reader at top level, never `click`; the tokenizer, directory tracking and
git calls live in `nightwright.agentic.git_guard_parse`, imported only after the match. Unreadable JSON, a registry error and non-conductor sessions are silent (fail open),
as SPEC-0382 requires, so the hook can never block a human's session. **Once the session is a
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
`\n`, then the commit with its message as one word. The command is **supported** when all of
these hold; otherwise it is **unsupported** (step 2):

- `shlex` raises no error (no unbalanced quote); the raw text has no backslash-newline;
- no `(`, `)`, `{`, `}` grouping token, no `<<` heredoc operator outside quotes, no `&`
  background operator, no `pushd`, `popd`;
- no backtick and no `$(` in any word, except the one form agents use for commit messages: a
  word that is exactly `$(cat <<'EOF'` newline … newline `EOF` newline `)` (or `<<EOF`, any
  delimiter word), whose body lines contain no backtick and no `$(`. That word is treated as
  text. No other word contains a newline.

Segments are the token runs between `;`, `&&`, `||`, `|` and newline. In each segment, leading
`NAME=value` words, `env` (and its `NAME=value` words; any `env` option makes it unsupported),
`command` and `exec` are skipped; a word whose basename is `git` or `cp` (`/usr/bin/git`) is
that program. **Redirects:** `<`, `>`, `>>`, `>&` and the word after each are removed from the
segment's arguments, and a digit-only word right before one is removed with it (`2>&1`). An
output redirect (`>`, `>>`, or `>&` followed by a non-digit word) has its target checked as a
`cp` destination (step 6).

**2. Unsupported commands fail closed.** An unsupported command is denied when its raw text
contains the word `git` or `cp` (regex `\b(git|cp)\b`). The one exception keeps SPEC-0496
commits that use a form the grammar does not model (for example `git commit -F - <<EOF`): it is
silent when the payload `cwd` is a linked worktree (step 4) **and**, splitting the raw text on
whitespace and the characters ``;&|(){}<>$\`"'`` (shell punctuation, backslash, backtick and
quotes), no word is `cp`, `cd`, `pushd`, `-C` or `-c`, none starts with `--chdir`, `--git-d`,
`--wo` or `--exec-path`, and the text contains none of `core.worktree`, `GIT_DIR`,
`GIT_WORK_TREE`, `GIT_INDEX_FILE`, `GIT_COMMON_DIR`. With none of those, nothing in the command
can point git anywhere but the linked worktree. The check is deliberately coarse: a commit
message that happens to contain one of those words is denied, and the reason tells the agent to
use a plain single command. Commands that mention neither `git` nor `cp` are silent.

**3. Directory tracking.** The directory starts at the payload's `cwd` (an absolute string;
when it is missing or not a string, the start is **unknown**). A segment `cd <dir>` changes the
directory for the following segments only when it is followed by `;`, `&&` or a newline; a `cd`
before `|` or `||`, or a `cd` whose argument is missing or `-`, makes the directory **unknown**
from there on. **Every path word the hook resolves** (the `cd` argument, each `-C` value, each
`cp` source, destination and `-t` value, each redirect target, the `worktree add` target) that
contains `$`, `~`, `*`, `?`, `[`, `{` or a backtick is unknown: the shell would expand it, so the
hook cannot know where it points. Paths are resolved against the current directory with
`os.path.realpath`. For a `git` segment, global options before the subcommand are read in order:
`-C <p>` (cumulative, relative to the previous directory), `--literal-pathspecs`, `--no-pager`,
`-P`, `--no-replace-objects`, `--no-optional-locks`. Denied outright, whatever the directory:
any abbreviation of `--git-dir` or `--work-tree` (prefixes `--git-d`, `--wo`), `-c`, `--bare`,
`--exec-path`, any other option before the subcommand, and a `NAME=value` prefix that sets
`GIT_DIR`, `GIT_WORK_TREE`, `GIT_INDEX_FILE` or `GIT_COMMON_DIR`. A `git` or `cp` segment whose
directory is unknown is denied.

**4. Primary or linked.** For a directory, run
`git -C <dir> rev-parse --path-format=absolute --git-dir --git-common-dir` (timeout 2 s,
`GIT_OPTIONAL_LOCKS=0`) and compare the two lines after `os.path.realpath`. Equal: the
directory is in a **primary worktree** (a main checkout, from any subdirectory of it).
Different: a **linked worktree** (every `.nw-worktrees/**` campaign worktree and every harness
`isolation: worktree` tree, including `<root>/.claude/worktrees/*`): the segment is silent. Exit
128 with `not a git repository` or `cannot change to` on a fully literal path: silent (git would
fail the same way). Any other non-zero exit, a timeout, or output that is not two absolute
paths: deny (fail closed).

**5. The primary-worktree allow set.** On a primary worktree a `git` segment is silent only when
its subcommand and arguments are one of:

- read-only: `status`, `log`, `diff`, `show`, `rev-parse`, `rev-list`, `merge-base`,
  `ls-files`, `ls-tree`, `cat-file`, `describe`, `blame`, `shortlog`, `show-ref`,
  `for-each-ref`, `grep`, `version`, `help`;
- `branch` with no argument or only `--list` (and its patterns), `-a`, `-r`, `-v`, `-vv`,
  `--show-current`;
- `worktree list`; `worktree add` whose target resolves under a `.nw-worktrees` directory. The
  target is the first positional after the options `-b <name>`, `-B <name>`, `--detach`, `-f`,
  `--force`, `--lock`, `--no-checkout`; any other option denies;
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

**7. The verb.** A segment that runs `nw` (or `uv run nw`) with `main-dirty-apply` is denied
unless the word right after `main-dirty-apply` is `--trivial-only`. Until SPEC-0551 the static
deny of SPEC-0548 also blocks every form; this step backs SPEC-0551's static tails.

### Install and launch check

`agent_permissions.sync` adds a second PreToolUse entry,
`{"matcher": "Bash", "hooks": [{"type": "command", "command": "nw-conductor-git-guard || true"}]}`,
idempotently, beside the unchanged `AskUserQuestion` entry. `sync()` returns
`{"added", "removed", "hook", "git_guard_hook"}` on every path, the no-op early return included
(`git_guard_hook: False`): `hook` keeps its meaning, `git_guard_hook` is true when the git-guard
entry was added now. `git_guard_hook_missing(path)` mirrors `hook_missing` and matches that exact
command string. `nw skills install` prints `conductor-git-guard hook  added to <path>` when it
adds it; `nw skills list` prints `hooks  present` or `hooks  <n> missing (run nw skills
install)`.

`nw queue conductor-launch` with the claude runner refuses to start (exit 1, message naming
`nw skills install`) while `git_guard_hook_missing()` is true for
`agent_permissions.default_settings_path()`; `--dry-run` starts nothing, exits 0, and reports
`git_guard_hook: false` in its JSON and a warning line in text. The cursor runner has no hooks
and is not checked (its guard is `CONDUCTOR_CURSOR_POLICY`, SPEC-0551). Tests point both at a
temp file by monkeypatching `agent_permissions.default_settings_path`.

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

Fixtures: a temp primary checkout `M` at `<parent>/repo` and a linked worktree `W` at
`<parent>/.nw-worktrees/P/x`; a conductor session registered in a temp cfg; the hook's `main`
fed a PreToolUse payload (`session_id`, `cwd`, `tool_input.command`) on stdin. "Permitted" in
AC1 means the new helper `tests/_permission_rules.py::decide(command, allow, deny)` (Claude Code
semantics: `*` matches any characters including spaces, the whole command must match, deny
beats allow) returns `allow` on the effective rules of `build_plan(cfg, "conductor")`; the
real-binary run is the arbiter.

- [ ] AC1 (SPEC-0544 AC3) **Worktree commits stay permitted.** Hook silent, parametrized:
      `git add a`, `git commit -m x`, `git commit -m "fix: a; b && c | d"`, `git add a#b`, the
      heredoc form `git commit -m "$(cat <<'EOF'` / `x; y` / `EOF` / `)"`, and
      `git commit -F - <<EOF` (the step-2 exception), each with `cwd: W`; `git -C W add -- a`,
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
      `W` (unsupported, has `-C`).
- [ ] AC3 (SPEC-0544 AC5) Denied against `M`: `git rm -f -- a`, `git checkout -- .`,
      `git checkout HEAD -- a`, `git checkout HEAD a`, `git checkout other`, `git checkout -b x`,
      `git switch -c x`, `git switch -f main`, `git switch --discard-changes main`,
      `git restore a`, `git restore --staged a`, `git merge --ff-only x`, `git rebase x`,
      `git reset --soft HEAD~1`, `git mv a b`, `git stash`, `git apply p`,
      `git hash-object -w -- a`, `git update-index --add --cacheinfo 100644,0,a`,
      `git worktree add ../x`, `git --work-tree=M add a`, `git --wo=M add a`,
      `GIT_DIR=M/.git git add a` (from `W`), `env GIT_WORK_TREE=M git add a`,
      `/usr/bin/git add a`, `command git commit -m x`, `(cd M && git commit -m x)` and
      `{ cd M; git commit -m x; }` (from `W`), `cd $HOME && git add a`, `cd ~ && git add a`,
      `cd - && git add a`, `cd M | git add a`, `git -C "$MAIN" commit -m x`,
      `git -C ~/repo add a`, `git -C /par* add a` (all from `W`), an unbalanced quote
      `git commit -m "x`, `` git commit -m `x` ``, `git commit -m "$(git -C M add .)"`,
      `cp a M/b`, `cp -t M a`, `cp -rt M a`, `cp a $M/b`, `git show HEAD:a > M/a`, and
      `uv run nw queue main-dirty-apply --root M` (step 7). Silent against `M` or `W`:
      `git branch --list`, `git worktree list`, `git worktree add ../.nw-worktrees/P/y`,
      `git worktree add -b x ../.nw-worktrees/P/y`, `cp a W/b`,
      `cp a M/.claude/worktrees/w/b` (a linked tree), `ls; echo "git add a"` (supported, no
      `git` segment), and an unsupported command with neither word (`(ls)`).
- [ ] AC4 (SPEC-0544 AC6) Fail-open and fail-closed cases, parametrized: silent for a
      non-conductor session id, unreadable JSON, and an empty command, for every AC2 command;
      silent for a write in a directory that is not a git repository and in one that does not
      exist; denied when the `rev-parse` exits non-zero for another reason (monkeypatched), when
      it times out (monkeypatched), when the payload has no `cwd` and the command is
      `git add a`, and when the tokenizer is monkeypatched to raise (reason `cannot verify
      internal error`), while a non-conductor session with the same raising tokenizer stays
      silent. The hook exits 0 in every case.
- [ ] AC5 (SPEC-0544 AC7) `agent_permissions.sync` adds the `Bash` matcher entry with the
      command string exactly `nw-conductor-git-guard || true`, once, idempotently; it leaves the
      `AskUserQuestion` entry and the other keys untouched; every return (no-op included) has
      the four keys `added`, `removed`, `hook`, `git_guard_hook`; `nw skills list` reports the
      missing hook and then `present`. With `agent_permissions.default_settings_path`
      monkeypatched to a temp file without the entry, `nw queue conductor-launch --dry-run
      --json` reports `git_guard_hook: false` and a real launch (start stubbed) exits 1 naming
      `nw skills install`; with the entry present both proceed.
- [ ] AC6 (new) Entry point: `pyproject.toml` declares the console script
      `nw-conductor-git-guard = "nightwright.agentic.git_guard:main"`; a subprocess run of the
      hook with a non-conductor payload leaves `click` and `nightwright.agentic.git_guard_parse`
      absent from `sys.modules` (the test runs `python -c` that calls `main()` with the payload on
      stdin, then prints `sorted(sys.modules)`).

## Test plan

- **Automated tests:** new `tests/test_conductor_git_guard.py` (`pytestmark = pytest.mark.fast`)
  covers AC1–AC4 and AC6 with the fixtures above, parametrized per command; the tokenizer also
  gets a direct unit table (supported vs unsupported, merged punctuation tokens such as `&&\n`,
  `;\n`, `\n\n`, redirects, segments, directory per segment). New helper
  `tests/_permission_rules.py::decide`, used by AC1 here and by SPEC-0551.
  `tests/test_agent_permissions.py` covers AC5's `sync` cases; the module holding the
  `conductor-launch` CLI tests covers the launch check.
- **Regression guard:** `tests/test_claude_settings_allowlist.py` (SPEC-0496) and
  `tests/test_agentic_launch.py` pass unchanged. Also `python3 tools/affected_tests.py --run uv
  run pytest -q`, `python3 tools/test_tiers.py --check`, `python3 tools/spec_lint.py`.
- **Manual verification (real binary):**
  0. The premise check (Design): log the two payloads and record them under Observed payloads.
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

## Out of scope

- The `--trivial-only` flag, any static allow or deny rule change, `CONDUCTOR_CURSOR_POLICY`,
  and every `skills/nw-conduct/SKILL.md` change (SPEC-0551).
- The steward's allow or deny lists, `.claude/settings.json` allow rules (SPEC-0496), and what
  the SPEC-0496 paragraphs tell sub-agents to run.
- **Threat model.** The hook guards a cooperative agent against mistakes, not a deliberately
  adversarial one. Escapes through another interpreter (`uv run python -c`, `bash -c`,
  `sh -c`, `eval`, `xargs git`, a script file) are out of scope; the conductor's `dontAsk`
  allow-list does not permit them, which is the control for those.

## Rollout / migration

Ships after SPEC-0549. The hook applies once `nw skills install` has added it; until then
`nw queue conductor-launch` refuses to start a claude conductor, so the ban is never silently
absent for a launched conductor. A conductor started by hand before the install is not guarded;
`nw doctor`'s existing conductor checks are unchanged. Worktree sub-agent commits are unaffected
(AC1, manual step 5). Queued under project Meta-Tools; SPEC-0551 follows.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed, including the premise check and the real-binary run; affected tests
      pass.
- [ ] No regressions in launch, settings-allowlist, skills tests.
- [ ] Spec body updated to match what shipped (Observed payloads and the latency median
      recorded).
- [ ] `docs/LEDGER.md` regenerated; `python3 tools/spec_lint.py` exits 0.

## Open questions

None.
