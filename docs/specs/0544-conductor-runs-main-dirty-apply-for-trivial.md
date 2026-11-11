---
id: SPEC-0544
title: Conductor runs main-dirty-apply for the trivial class only
status: accepted
owner: user
created: 2026-11-11
updated: 2026-11-11
kind: feature
tags: [agentic, conductor, launch]
depends_on: [SPEC-0543]
source_idea: IDEA-1159
---

## Context

IDEA-1159 asks the conductor to commit a trivial stray edit (frontmatter, docs prose,
comments) that blocks closes, and to escalate only what needs the human. The human said yes
to the conductor clearing `main_dirty`. They also approved doing it through one audited verb,
`nw queue main-dirty-apply`, instead of raw git allow-rules (campaign
`IDEA-1159-20261110-02ed`, resume note seq 437). At ESC-0067 (2026-11-11) the human chose to
split [SPEC-0537](./0537-conductor-must-clear-main-dirty-itself-a-stray.md); this is part C.
[SPEC-0542](./0542-main-dirty-plan-and-escalate.md) gives the conductor the plan and the
escalation. [SPEC-0543](./0543-main-dirty-apply-audited-verb.md) ships the verb for the human
and the steward and denies it to the conductor.

This spec lets the conductor run the verb for `action: commit` only. It also closes the raw
git write paths into the **main checkout** that the branch's review found open:
`worktree add *` and `switch --create` were unrestricted, `commit -m *` allowed `-a` /
`--amend` on the base, a project base was interpolated into an allow glob, and the launch
emitted a deny rule per project directory. It must not repeat two other branch mistakes. The
steward lost its swallowed-`rm` deny. And a blanket `commit *` / `add -- *` deny blocked the
conductor's worktree sub-agents from committing.
[SPEC-0496](./0496-subagent-worktree-commit-allowlist.md) (human-requested) allows those
commits: a sub-agent spawned with `isolation: worktree` runs plain `git add <paths>` /
`git commit -m …` in its own worktree. When a sub-agent reports a denial, the conductor runs
the blocked command in that worktree.

Permission rule strings match the command text only; they cannot see which checkout a plain
`git commit` runs in. So a main-checkout-only deny cannot be a `--disallowedTools` glob. It
has to be a check that knows the working directory.

## Goals / Non-goals

**Goals**

- On a `main_dirty` park whose live plan is `commit`, the conductor runs
  `uv run nw queue main-dirty-apply --trivial-only` itself, mails the owner, and resumes.
- Every other class still escalates as SPEC-0542 says.
- In a conductor session (the conductor and its sub-agents), the git-guard hook denies every
  raw git command that targets a **primary worktree** (a main checkout) unless it is in a small
  named read-only set, denies `cp` into one, and denies anything it cannot parse that could do
  either. This covers `add`, `commit`, `rm`, `checkout`, `switch`, `restore`, `merge`,
  `rebase`, `reset`, `mv`, `cherry-pick`, `revert`, `stash`, `apply`, `am`, `hash-object`,
  `update-index`, and `worktree add` outside `.nw-worktrees/`.
- Worktree commits stay permitted as the conductor allow-list (`_CONDUCTOR_WIP_VERBS`, prefix
  `git` and `git -C *`) and SPEC-0496 (plain commands, via `.claude/settings.json`) already
  define them; the hook adds no denial in a linked worktree.

**Non-goals**

- The conductor never runs the verb for `save_branch`, `branch_and_escalate`, `hold`,
  `escalate`, or `resume`. The human or the steward does (SPEC-0543). IDEA-1159's "commit via a
  sub-agent" and "else save to a branch" are replaced on purpose: the human approved the single
  audited verb (resume note seq 437) and, at ESC-0067, the split that leaves WIP saves with the
  human or the steward. No follow-up idea is needed for it.
- No change to the steward's allow or deny lists, to `.claude/settings.json` allow rules
  (SPEC-0496), or to what the SPEC-0496 paragraphs tell sub-agents to run.
- No `git switch <base>` allow for the conductor; the trivial path never switches.
- **Threat model.** The hook guards a cooperative agent against mistakes, not a deliberately
  adversarial one. Escapes through another interpreter (`uv run python -c`, `bash -c`,
  `sh -c`, `eval`, `xargs git`, a script file) are out of scope; the conductor's `dontAsk`
  allow-list does not permit them, which is the control for those.

## Decision

1. Add `--trivial-only` to `main-dirty-apply`. After all SPEC-0543 guards, the verb refuses
   with no write unless the live action is `commit`.
2. Replace SPEC-0543's two blanket conductor denies of the verb with static denies of every
   form that does not put `--trivial-only` first, and allow the `--trivial-only` form. The hook
   repeats this check on the parsed command, so a form the static tails miss is still denied.
3. Enforce the main-checkout write ban with a new PreToolUse hook,
   `nw queue conductor-git-guard`, matcher `Bash`. It acts only for a registered conductor
   session id. It parses the command with a quote-aware shell tokenizer, tracks the directory
   each `git` / `cp` segment runs in, and on a primary worktree allows only a named read-only
   set. **Anything outside the grammar it models fails closed** (deny), with one narrow
   exception that keeps SPEC-0496 worktree commits working (hook step 2).
4. `nw queue conductor-launch` (claude runner) refuses to start a conductor while the hook is
   not installed, so the ban is never silently absent.
5. The conductor allow-list keeps `_CONDUCTOR_WIP_VERBS`, and the static deny lists stay as
   they are except for the verb rules in step 2.

## Design

### The flag

`apply_clear(root, plan=None, *, trivial_only=False)` and CLI `--trivial-only`. With the flag,
the verb first runs every SPEC-0543 guard (lock, re-plan, in-progress, plan check, paths, index).
Then a live action other than `commit` returns `ok: false`, `error: not trivial: <action>`, and
SPEC-0543's `escalate` for the live class, and writes nothing. Without the flag the verb is
unchanged.

### Launch (`src/nightwright/agentic/launch.py`), static rules

- **Allow:** add `Bash(uv run nw queue main-dirty-apply --trivial-only *)` to
  `conductor_allowed_tools`. `_CONDUCTOR_WIP_VERBS` and `_CONDUCTOR_GIT_VERBS` are unchanged.
- **Verb deny (conductor-only):** SPEC-0543's `Bash(uv run nw * main-dirty-apply*)` and
  `Bash(nw * main-dirty-apply*)` are removed (they would also deny the allowed form: deny wins).
  For each prefix `uv run nw` and `nw`, and each of `queue main-dirty-apply` and
  `* queue main-dirty-apply`, deny the bare form and one tail per option of the click command
  other than `--trivial-only`: today ` --root*`, ` --plan*`, ` --json*`, ` --help*`, ` -h*`.
  None of these matches `uv run nw queue main-dirty-apply --trivial-only …`. A launch test
  introspects the click command and fails when an option has no deny tail.
- **Unchanged:** `DEFAULTS["disallowed_tools"]` (push deny, `_steward_guard`,
  `_conductor_guard`, `gh pr merge`) and the steward's rules stay byte-identical. No rule
  contains a project path or a base name.
- **`CONDUCTOR_CURSOR_POLICY`** gets this text: "A main_dirty clear of the trivial class runs
  `uv run nw queue main-dirty-apply --trivial-only`; never run it otherwise. Never write the
  main checkout with raw git (add, commit, rm, checkout, switch, restore, merge, rebase, reset,
  mv, cherry-pick, revert, stash, apply, am, hash-object, update-index, worktree add outside
  `.nw-worktrees/`) or cp; in a worktree under `.nw-worktrees/` add and commit as usual. No
  reset --hard, clean, branch -D, or push to the base." The cursor runner has no hooks, so this
  prose is its guard.

### The hook: `nw queue conductor-git-guard`

A new command in `cli_conductor.py`, beside `conductor-guard`. It always exits 0, denies through
the PreToolUse JSON decision (`permissionDecision: deny`, reason below), and is silent
otherwise. Order of work, so a non-conductor call stays cheap: read stdin, look up `session_id`
in the conductor session registry (`CC.sessions(cfg, "conductor")`), and return silently unless
it matches, before tokenizing or running any git. Unreadable JSON and non-conductor sessions are
silent (fail open), as SPEC-0382 requires, so the hook can never block a human's session.

The deny reason is `conductor: no raw git writes in the main checkout <toplevel>; trivial:
main-dirty-apply --trivial-only, else escalate (SPEC-0544)`, or for a fail-closed deny
`conductor-git-guard: cannot verify <what> (SPEC-0544); use a plain single git command in the
worktree`.

**1. Tokenize (quote-aware).** `shlex.shlex(command, posix=True,
punctuation_chars="();<>|&\n")` with `whitespace = " \t\r"` and `whitespace_split = True`, so
`;`, `&&`, `||`, `|`, `&`, `(`, `)`, `<<`, `<`, `>` and an unquoted newline are operator tokens
only outside quotes, and `"fix: a; b && c"` (or a quoted message spanning lines) stays one word.
Checked on Python 3.14: `cd M` newline `git commit -m "a; b && c"` gives `cd M`, `\n`, then the
commit with its message as one word; the `$(cat <<'EOF' …)` commit message is one word.
Single-quoted text is literal. Words lose their quoting, so the checks below run on words: a
single-quoted `'$(x)'` is treated like an unquoted one (stricter, never looser). The command is **supported** when all of these hold; otherwise it is
**unsupported** (step 2):

- `shlex` raises no error (no unbalanced quote);
- no `(`, `)`, `{`, `}` grouping token, no `<<` heredoc operator outside quotes, no `&`
  background operator, no `pushd`, `popd`;
- no backtick and no `$(` in any word, except the one form agents use for commit messages: a
  word that is exactly `$(cat <<'EOF'` newline … newline `EOF` newline `)` (or `<<EOF`, any
  delimiter word), whose body lines contain no backtick and no `$(`. That word is treated as
  text. (An unquoted `$(` cannot reach this check: its `(` is an operator token, which is
  grouping.)

Segments are the token runs between `;`, `&&`, `||`, `|` and newline. In each segment, leading
`NAME=value` words, `env` (and its `NAME=value` words; any `env` option makes it unsupported),
`command` and `exec` are skipped; a word whose basename is `git` or `cp` (`/usr/bin/git`) is
that program.

**2. Unsupported commands fail closed.** An unsupported command is denied when its raw text
contains the word `git` or `cp` (regex `\b(git|cp)\b`). The one exception keeps SPEC-0496
commits that use a form the grammar does not model (for example `git commit -F - <<EOF`): it is
silent when the payload `cwd` is a linked worktree (step 4) **and**, splitting the raw text on
whitespace and the characters ``;&|(){}<>$\`"'`` (shell punctuation, backslash, backtick and
quotes), no word is `cp`, `cd`, `pushd`, `-C` or `-c`, none
starts with `--chdir`, `--git-d`, `--wo` or `--exec-path`, and the text contains none of
`core.worktree`, `GIT_DIR`, `GIT_WORK_TREE`, `GIT_INDEX_FILE`, `GIT_COMMON_DIR`. With none of
those, nothing in the command can point git anywhere but the linked worktree. The check is
deliberately coarse: a commit message that happens to contain one of those words is denied, and
the reason tells the agent to use a plain single command. Commands that
mention neither `git` nor `cp` are silent.

**3. Directory tracking.** The directory starts at the payload's `cwd` (an absolute string;
when it is missing or not a string, the start is **unknown**). A segment `cd <dir>` changes the
directory for the following segments only when it is followed by `;`, `&&` or a newline; a `cd`
before `|` or `||`, or a `cd` whose argument is missing, `-`, or contains `$` or `~`, makes the
directory **unknown** from there on. `<dir>` is resolved against the current directory and
`os.path.realpath`. For a `git` segment, global options before the subcommand are read in
order: `-C <p>` (cumulative, relative to the previous directory), `--literal-pathspecs`,
`--no-pager`, `-P`, `--no-replace-objects`, `--no-optional-locks`. Denied outright, whatever the
directory: any abbreviation of `--git-dir` or `--work-tree` (prefixes `--git-d`, `--wo`), `-c`
(already statically denied), `--bare`, `--exec-path`, any other option before the subcommand,
and a `NAME=value` prefix that sets `GIT_DIR`, `GIT_WORK_TREE`, `GIT_INDEX_FILE` or
`GIT_COMMON_DIR`. A `git` or `cp` segment whose directory is unknown is denied.

**4. Primary or linked.** For a `git` segment's directory, run
`git -C <dir> rev-parse --path-format=absolute --git-dir --git-common-dir` (timeout 2 s,
`GIT_OPTIONAL_LOCKS=0`) and compare the two lines after `os.path.realpath`. Equal: the
directory is in a **primary worktree** (a main checkout, from any subdirectory of it). Different:
a **linked worktree** (every `.nw-worktrees/**` campaign worktree and every harness
`isolation: worktree` tree): the segment is silent. Exit 128 with `not a git repository` or
`cannot change to`: silent (git would fail the same way, and no `--git-dir` / `GIT_DIR` can be
in play after step 3). Any other non-zero exit, a timeout, or output that is not two absolute
paths: deny (fail closed).

**5. The primary-worktree allow set.** On a primary worktree a `git` segment is silent only when
its subcommand and arguments are one of:

- `status`, `log`, `diff`, `show`, `rev-parse`, `rev-list`, `merge-base`, `ls-files`,
  `ls-tree`, `cat-file`, `describe`, `blame`, `shortlog`, `show-ref`, `for-each-ref`, `grep`,
  `version`, `help`;
- `branch` with no argument or only `--list` (and its patterns), `-a`, `-r`, `-v`, `-vv`,
  `--show-current`;
- `worktree list`; `worktree add` whose target path resolves under a `.nw-worktrees` directory;
- `fetch`, `push` (they move refs and remotes, not the checkout; the static push rules govern
  them).

Every other subcommand is denied there, with no textual "pathspec or not" decision: `checkout`
and `switch` in any form (so `checkout -b` and `switch -c` get the same answer, deny: both move
main's HEAD off the base), `restore` (`--staged` included: it rewrites main's index), `add`,
`commit`, `rm`, `mv`, `merge`, `rebase`, `reset`, `cherry-pick`, `revert`, `stash`, `apply`,
`am`, `hash-object`, `update-index`, `tag`, `notes`, `gc`, and any subcommand not listed.

**6. `cp`.** A `cp` segment is denied when its destination (the last argument, or the `-t`
/ `--target-directory` value) resolves inside a primary worktree's toplevel, or when the
destination cannot be determined. `cp` is not in the conductor allow-list, so `dontAsk`
already refuses it; the hook check stays because a user-level `Bash(cp *)` allow in
`~/.claude/settings.json` would reopen it, and the hook is the only cwd-aware control.

**7. The verb.** A segment that runs `nw` (or `uv run nw`) with `main-dirty-apply` is denied
unless the word right after `main-dirty-apply` is `--trivial-only`. This backs the static tails.

**Install.** `agent_permissions.sync` adds a second PreToolUse entry,
`{"matcher": "Bash", "hooks": [{"type": "command", "command": "nw queue conductor-git-guard || true"}]}`,
idempotently, beside the unchanged `AskUserQuestion` entry. `sync()` returns
`{"added", "removed", "hook", "git_guard_hook"}`: `hook` keeps its meaning (the
conductor-guard entry was added now), `git_guard_hook` is true when the git-guard entry was
added now. `git_guard_hook_missing(path)` mirrors `hook_missing`. `nw skills install` prints
`conductor-git-guard hook  added to <path>` when it adds it; `nw skills list` prints a
`hooks  present` or `hooks  <n> missing (run nw skills install)` line.

**Launch check.** `nw queue conductor-launch` with the claude runner refuses to start (exit 1,
message naming `nw skills install`) while `git_guard_hook_missing()` is true for
`agent_permissions.default_settings_path()`; `--dry-run` starts nothing, exits 0, and reports
`git_guard_hook: false` in its JSON and a warning line in text. The cursor runner has no hooks
and is not checked (its guard is `CONDUCTOR_CURSOR_POLICY`).

**Latency.** The hook runs on every Bash call of every session on the machine. The
non-conductor path does the registry lookup before importing the tokenizer or running git. As
shipped records its wall time for a non-conductor payload; the budget is 300 ms median over 20
runs. If it is over budget, the hook entry points at a console script
`nw-conductor-git-guard` whose module imports only `json`, `os`, `shlex`, `subprocess` and the
registry reader (same logic, same tests), and `sync` installs that command instead.

### Skill (`skills/nw-conduct/SKILL.md`)

- **`parked.main_dirty` row:** SPEC-0542's row gains one step **after step 2 and before step 3**.
  If the live plan's `action` is `commit`:
  - Run `uv run nw queue main-dirty-apply --trivial-only --root <root> --json` in the
    conductor's own context: one command, no sub-agent, one per root at a time. This runs
    even when an open `human_checkout` record exists for the root; on `ok: true` withdraw that
    record (`nw escalations withdraw <id> --reason "cleared by main-dirty-apply" --yes`).
  - On `ok: true`, mail `nw mailbox send --from conductor --to steward:<Project> --kind info`
    with the `sha` and the paths, or `--to human` when the `stewards` list of
    `nw queue conduct-status --json` has no `address` equal to `steward:<Project>` (a steward
    is the owner only while its mailbox follower is held). Then resume
    each `main_dirty` campaign of the root with `--decision "cleared main (commit, <sha>)"
    --rule parked.main_dirty`.
  - On `error: not trivial: resume`, the checkout became clean meanwhile: go to step 2.
  - On any other `ok: false`, do not retry. Go on to step 3 / 4 with an extra
    `--fact "main-dirty-apply: <error>"`.

  Every other action goes straight to SPEC-0542's step 3.
- **`parked.main_not_on_base` row:** the conductor does not move dirt or switch branches in
  the main checkout. It runs `main-dirty-plan` and escalates `human_checkout` (no campaign,
  `--yes`) with the class and `recommended`, as in SPEC-0542 step 4.
- **`wt_broken` row:** mail the steward (or escalate `human_checkout`) to save the tracked
  dirt to a WIP branch and restore main. Use `uv run nw` from a worktree meanwhile. No raw git
  write in the main checkout.
- **SPEC-0496 paragraphs, amended (not kept as is).** The dispatch paragraph keeps telling
  sub-agents to run plain single `git add <paths>` / `git commit -m …` in their worktree. The
  permission-denial paragraph today says "run the blocked plain command yourself in that
  worktree"; the conductor's cwd is not that worktree, so it becomes: "run it yourself as
  `git -C <worktree> add -- <paths>` / `git -C <worktree> commit -m …` (the forms the
  conductor allow-list permits), never in the main checkout: the conductor-git-guard hook denies
  raw git writes there".

### Files the implementer may cherry-pick from `nw/IDEA-1159-20261110-02ed`

- `src/nightwright/agentic/launch.py`: only the `CONDUCTOR_CURSOR_POLICY` wording, as a
  source. **Not** `_CONDUCTOR_WRITE_DENY`, `_conductor_write_guard`, the removal of
  `_CONDUCTOR_WIP_VERBS`, `_branch_ref_ok`, `_conductor_project_bases`, or the
  `switch <base>` allow.
- `tests/test_agentic_launch.py`: `test_push_guard_rule_count` and the steward swallow-family
  tests, as the steward-unchanged regression guard.
- `tests/test_main_dirty_clear.py`: `test_conductor_cursor_policy_allows_clear` and
  `test_skill_row_phrases`, rewritten to this row.

## Alternatives considered

- **Static deny globs (`Bash(git commit *)`, …), as on the branch.** These rules cannot see
  the working directory. They block SPEC-0496's in-worktree commits, which the human asked
  for. Rejected.
- **Per-project `Bash(git -C <root> commit *)` denies.** They miss the plain cwd-relative form,
  which is exactly how the conductor would hit main. They also interpolate paths into rules,
  a review major. Rejected.
- **Let the conductor run the verb for every class.** The human approved escalating
  substantive edits in their checkout. Non-trivial saves move their work to a branch, and that
  stays with the human or the steward.
- **Enforce trivial-only by an env var the verb reads.** The cursor runner launches with no
  env, and the check would be invisible in the permission list. A flag plus deny globs is
  visible and testable with the real binary.
- **A second verb (`main-dirty-commit`).** The human approved a single audited verb.

## Acceptance criteria

Fixtures for AC3–AC6: a temp primary checkout `M` at `<parent>/repo` and a linked worktree `W`
at `<parent>/.nw-worktrees/P/x`; a conductor session registered in a temp cfg; the hook fed a
PreToolUse payload (`session_id`, `cwd`, `tool_input.command`) through `CliRunner`.
"Permitted" in AC2/AC3 means the helper `tests/_permission_rules.py::decide(command, allow,
deny)` (Claude Code semantics: `*` matches any characters including spaces, the whole command
must match, deny beats allow) returns `allow` on the effective rules of
`build_plan(cfg, "conductor")`; the real-binary run is the arbiter.

- [ ] AC1 `main-dirty-apply --trivial-only` on each non-`commit` live action (`save_branch`,
      `branch_and_escalate`, `hold`, `escalate`, `resume`) writes nothing: HEAD, index bytes,
      worktree bytes, and the branch list are unchanged. It returns `ok: false`,
      `error: not trivial: <action>` and SPEC-0543's `escalate` for the class. On a trivial
      diff it commits exactly as SPEC-0543 AC3.
- [ ] AC2 Static rules: `decide` permits
      `uv run nw queue main-dirty-apply --trivial-only --root /r --json` and denies
      `uv run nw queue main-dirty-apply --root /r --json`, the bare form, `--plan f`, `--json`,
      `-h`, the same forms through `nw`, and `uv run nw --x queue main-dirty-apply --root /r`.
      A test introspects the click command and fails when an option other than
      `--trivial-only` has no deny tail.
- [ ] AC3 **Worktree commits stay permitted.** Hook silent, parametrized:
      `git add a`, `git commit -m x`, `git commit -m "fix: a; b && c | d"`, the heredoc form
      `git commit -m "$(cat <<'EOF'` / `x; y` / `EOF` / `)"`, and `git commit -F - <<EOF` (the
      step-2 exception), each with `cwd: W`; `git -C W add -- a`, `git -C W commit -m x` and
      `git -C W rm -f -- a` with `cwd: M`; `git status`, `git log -1 | head` with `cwd: M`.
      Statically, `decide` permits `git -C W add -- a` and `git -C W commit -m x` (conductor
      allow-list) and denies `git -C W rm -f -- a` (`Bash(git * rm *)`, unchanged: the hook's
      silence grants nothing). `.claude/settings.json` permits only the plain `git add a` and
      `git commit -m x` forms (SPEC-0496; `tests/test_claude_settings_allowlist.py` unchanged).
- [ ] AC4 **The same commands against main are denied** with the SPEC-0544 reason: `git add a`,
      `git commit -m x`, `git commit -am x`, `git add -A`, `git commit -m "a; b"` with `cwd: M`;
      the same in a subdirectory of `M`; `git -C M commit -m x` and `cd <parent>/repo && git
      commit -m x` with `cwd: W`; `git -C W -C ../../../repo commit -m x` (resolves to `M`).
- [ ] AC5 Denied against `M`: `git rm -f -- a`, `git checkout -- .`, `git checkout HEAD -- a`,
      `git checkout HEAD a`, `git checkout other`, `git checkout -b x`, `git switch -c x`,
      `git switch -f main`, `git switch --discard-changes main`, `git restore a`,
      `git restore --staged a`, `git merge --ff-only x`, `git rebase x`, `git reset --soft
      HEAD~1`, `git mv a b`, `git stash`, `git apply p`, `git hash-object -w -- a`,
      `git update-index --add --cacheinfo 100644,0,a`, `git worktree add ../x`,
      `git --work-tree=M add a`, `git --wo=M add a`, `GIT_DIR=M/.git git add a` (from `W`),
      `env GIT_WORK_TREE=M git add a`, `/usr/bin/git add a`, `command git commit -m x`,
      `(cd M && git commit -m x)` and `{ cd M; git commit -m x; }` (from `W`), `cd $HOME && git
      add a`, `cd ~ && git add a`, `cd - && git add a`, `cd M | git add a`, an unbalanced
      quote `git commit -m "x`, `` git commit -m `x` ``, `git commit -m "$(git -C M add .)"`,
      `cp a M/b`, `cp -t M a`, and `uv run nw queue main-dirty-apply --root M` (step 7).
      Silent against `M` or `W`: `git branch --list`, `git worktree list`,
      `git worktree add ../.nw-worktrees/P/y`, `cp a W/b`, `ls; echo "git add a"` (supported,
      no `git` segment), and an unsupported command with neither word (`(ls)`).
- [ ] AC6 Fail-open and fail-closed cases, parametrized: silent for a non-conductor session id,
      unreadable JSON, and an empty command, for every AC4 command; silent for a write in a
      directory that is not a git repository and in one that does not exist; denied when the
      `rev-parse` exits non-zero for another reason (monkeypatched), when it times out
      (monkeypatched), and when the payload has no `cwd` and the command is `git add a`. The
      hook exits 0 in every case.
- [ ] AC7 `agent_permissions.sync` adds the `Bash` matcher entry once and is idempotent; it
      leaves the `AskUserQuestion` entry and the other keys untouched; its return has
      `git_guard_hook`; `nw skills list` reports the missing hook and then `present`.
      `nw queue conductor-launch --dry-run --json` reports `git_guard_hook: false` on a
      settings file without it, and a real launch (start stubbed) exits 1 naming
      `nw skills install`; with the entry present both proceed.
- [ ] AC8 No conductor static rule contains a project root path or a base branch name: a
      launch with two projects whose roots and bases differ yields identical rule lists.
      `DEFAULTS["disallowed_tools"]`, `_CONDUCTOR_WIP_VERBS`, and the steward's effective
      rules equal a snapshot taken before this spec. The steward still denies
      `git -C /w rm -f --ignore-unmatch -- f "x log y"`.
- [ ] AC9 `CONDUCTOR_CURSOR_POLICY` names `main-dirty-apply --trivial-only` and the main
      checkout write ban. It says worktree add/commit is allowed.
- [ ] AC10 The `parked.main_dirty` row, extracted by rule id, contains
      `main-dirty-apply --trivial-only`, `--from conductor`, `steward:<Project>`,
      `conduct-status`, `--to human`, `cleared main (commit`, `not trivial: resume`,
      `escalations withdraw`, `--rule parked.main_dirty`, and the `main-dirty-apply: <error>`
      fact, with the new step between steps 2 and 3. Every `main-dirty-apply` command in the
      skill is followed by `--trivial-only` (regex `main-dirty-apply(?!:)(?! --trivial-only)`
      finds nothing; the fact label `main-dirty-apply: <error>` is exempt). The row contains
      none of `git add`, `git commit`, `git checkout`, `git rm`, `git worktree add`. The
      `parked.main_not_on_base` and `wt_broken` rows name `human_checkout` or the steward and
      no raw git write. The permission-denial paragraph contains
      `git -C <worktree> add -- <paths>` and `never in the main checkout`; the dispatch
      paragraph keeps its plain `git add <paths>` / `git commit -m` wording.
- [ ] AC11 Inherited assertions this spec edits, and only these: SPEC-0542 AC13's forbidden
      list loses `main-dirty-apply` (the row may name the `--trivial-only` form); SPEC-0543
      AC14's two blanket deny rules are replaced by AC2's rule set; SPEC-0543 AC15's
      "`nw-conduct` never names `main-dirty-apply`" is replaced by AC10's regex. SPEC-0542's
      steps 3–5, its question, options and `--fact` text are byte-identical for every
      non-`commit` class.

## Test plan

- **Automated tests:**
  - New `tests/test_conductor_git_guard.py` (`pytestmark = pytest.mark.fast`) covers
    AC3–AC6 with the fixtures above, parametrized per command; the tokenizer also gets a
    direct unit table (supported vs unsupported, segments, directory per segment).
  - New helper `tests/_permission_rules.py::decide(command, allow, deny)`, used by AC2/AC3 in
    `tests/test_agentic_launch.py`.
  - `tests/test_agentic_launch.py` covers AC2, AC8 and AC9, including the snapshot, the
    two-project launch, and the option-introspection test; it replaces SPEC-0543's AC14 test.
  - `tests/test_agent_permissions.py` covers AC7's `sync` cases; `tests/test_agentic_launch_cli.py`
    (or the module holding the `conductor-launch` CLI tests) covers the launch check.
  - `tests/test_main_dirty_apply.py` (SPEC-0543) covers AC1.
  - `tests/test_main_dirty_plan.py` (SPEC-0542): its skill-row test drops `main-dirty-apply`
    from the forbidden list and gains AC10; SPEC-0543's AC15 conduct-skill assertion is
    replaced by AC10's regex (AC11).
- **Regression guard:** `tests/test_claude_settings_allowlist.py` (SPEC-0496) passes
  unchanged. Also run `python3 tools/affected_tests.py --run uv run pytest -q`,
  `python3 tools/test_tiers.py --check`, and `python3 tools/spec_lint.py`.
- **Manual verification (real binary):**
  1. Install the hook (`nw skills install` or `agent_permissions.sync` on a temp settings
     file passed through `--settings`).
  2. Print the conductor plan with `uv run nw queue conductor-launch --dry-run --json`
     (`git_guard_hook: true`).
  3. Run the real `claude -p` once as a registered conductor session, with the plan's
     `--allowedTools` / `--disallowedTools`, `--permission-mode dontAsk`, and cwd a
     nightwright worktree. Use a scratch repo under the scratchpad with a linked worktree
     under `.nw-worktrees/` and a frontmatter-only edit on main.
  4. Have it run `git -C <main> commit -am x` (expect denied by the hook) and
     `git -C <wt> commit --allow-empty -m x` (expect a new commit in the worktree).
  5. Have a sub-agent spawned with `isolation: worktree` (a) run
     `git -C <main> commit --allow-empty -m x`: expect **denied by the hook**, which proves the
     sub-agent's payload carries the conductor's session id; (b) run a plain `git add` +
     `git commit -m x` in its worktree: expect committed, which proves its payload `cwd` is its
     own tree. Record the observed payload fields (log them once from the hook).
  6. Have it run `uv run nw queue main-dirty-apply --root <main> --json` (expect denied), then
     `uv run nw queue main-dirty-apply --trivial-only --root <main> --json` (expect
     `ok: true`).
  7. Time 20 non-conductor hook calls; record the median against the 300 ms budget.

  Record each result in As shipped. If 5(a) is not denied or 5(b) is not committed, a premise of
  the Decision is false: the spec is not done, and the finding goes back to Design.

## Rollout / migration

Ships after SPEC-0543. The hook applies once `nw skills install` has added it; until then
`nw queue conductor-launch` refuses to start a claude conductor, so the ban is never silently
absent for a launched conductor. A conductor started by hand before the install is not
guarded; `nw doctor`'s existing conductor checks are unchanged. A conductor launched after the
deploy gets the new verb rules; a running one keeps its old flags (SPEC-0543's blanket deny)
until it is relaunched (`launch_hash` changes). Worktree sub-agent commits are unaffected (AC3,
manual step 5).

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed, including the real-binary check; affected tests pass.
- [ ] No regressions in launch, settings-allowlist, skills, and main-dirty tests.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` regenerated; `python3 tools/spec_lint.py` exits 0.

## Review notes

Review of 2026-11-11 (`nw spec review SPEC-0544`, verdict `split`):

- **AC3 claims permissions the rules do not grant (must_fix): fixed.** AC3 is now a matrix that
  separates hook silence from static permission: `git -C W add -- a` / `commit -m` are allowed
  by `_CONDUCTOR_WIP_VERBS`, `git -C W rm` stays statically denied, settings.json covers only
  the plain forms, checked by a named `decide` helper.
- **Quote-unaware splitting (major): fixed.** `shlex` with `punctuation_chars`; operators inside
  quotes and the `$(cat <<'EOF' …)` commit form are text; grouping, backticks, other `$(`,
  heredocs outside quotes and unbalanced quotes are unsupported and fail closed, with the one
  linked-worktree exception; AC3/AC5 test both sides.
- **Non-repo silent vs rev-parse failure denies (major): fixed.** Exit 128 "not a git
  repository" / "cannot change to" is silent; any other error or a timeout denies;
  `--path-format=absolute` plus `realpath`; AC6 has each case.
- **Write set is a denylist (major): fixed.** Inverted to a named allow set on a primary
  worktree; `checkout -b` and `switch -c` are both denied; `merge --ff-only`, `rebase`,
  `restore --staged` and the rest are in AC5.
- **Unverified premises (major): fixed.** Manual step 5 now has a denied sub-agent write to main
  (proves the shared session id) and an allowed worktree commit (proves `cwd`); a missing `cwd`
  is an unknown start and fails closed; a false premise blocks `done`.
- **Install nothing checks (major): fixed.** `conductor-launch` refuses without the hook,
  `--dry-run` reports it, `sync` / `skills install` / `skills list` shapes are defined (AC7).
- **AC11 contradicts inherited criteria (major): fixed.** AC11 names the three inherited
  assertions it edits and nothing else.
- **Overclaims "cannot write" (major): fixed.** Threat model stated as a Non-goal; env prefixes,
  `env`/`command`/path-qualified git, `GIT_DIR`-style variables, subshells, groups and
  unresolvable `cd` are denied (AC5); Goals say "denies", not "cannot".
- **Split the hook from the verb permission (major): rebutted.** The scope is the human's
  ESC-0067 part C: the conductor gains a write (the trivial clear) only together with the guard
  that keeps every other main-checkout write away from it. Shipping the verb permission first
  would grant the write without the guard; shipping the guard first is possible but buys
  nothing the human asked for, and the reviewer's own split makes the verb spec depend on the
  hook spec, the same order this single spec enforces. The flag, rules and skill rows are small
  (AC1, AC2, AC8–AC11); the hook is the bulk and now has an exact contract.
- **Minors and nit: fixed.** Latency path and budget; `decide` helper; AC10 regex exempting the
  fact label; insertion point, `not trivial: resume` branch and a concrete steward check;
  option-introspection test for the deny tails; SPEC-0496 paragraph amended to the
  `-C <worktree> add --` form; AC4's `-C` example resolves to `M`; the idea deviation is a
  Non-goal; `cp` kept with its reason (a user-level `cp` allow would reopen it).

## Open questions

None.
