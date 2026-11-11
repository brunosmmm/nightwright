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
- The conductor and its sub-agents cannot write the main checkout with raw git (`add`,
  `commit`, `rm`, `checkout --`, `hash-object`, `update-index`, `worktree add` outside
  `.nw-worktrees/`, forced switch, `add -A`) or `cp`.
- In-worktree `git add` / `git commit` (plain, cwd the worktree) and
  `git -C <path under .nw-worktrees/> add|commit` stay permitted exactly as SPEC-0496 defines.

**Non-goals**

- The conductor never runs the verb for `save_branch`, `branch_and_escalate`, `hold`,
  `escalate`, or `resume`. The human or the steward does (SPEC-0543).
- No change to the steward's allow or deny lists, to `.claude/settings.json` allow rules
  (SPEC-0496), or to the SPEC-0496 skill paragraphs.
- No `git switch <base>` allow for the conductor; the trivial path never switches.

## Decision

1. Add `--trivial-only` to `main-dirty-apply`. After all SPEC-0543 guards, the verb refuses
   with no write unless the live action is `commit`.
2. Replace SPEC-0543's blanket conductor deny of the verb with static denies of every form
   that does not start with `--trivial-only`, and allow the `--trivial-only` form.
3. Enforce the main-checkout write ban with a new PreToolUse hook,
   `nw queue conductor-git-guard`, matcher `Bash`. It acts only for a registered conductor
   session, which covers the conductor's sub-agents because they share its session id. It
   resolves the checkout each git command targets: `-C <path>` when given, else the hook
   payload's `cwd`. It denies a listed write verb only when that checkout is a **primary
   worktree**, meaning `git rev-parse --git-dir` equals `--git-common-dir`. That is a main
   checkout. Linked worktrees are allowed: every `.nw-worktrees/**` campaign worktree and
   every harness `isolation: worktree` tree.
4. The conductor allow-list keeps `_CONDUCTOR_WIP_VERBS`, and the static deny lists stay as
   they are except for the verb rules in step 2.

## Design

### The flag

`apply_clear(root, plan=None, *, trivial_only=False)` and CLI `--trivial-only`. With the flag,
the verb first runs the lock, re-plan, in-progress, path, and index guards. Then a live action
other than `commit` returns `ok: false`, `error: not trivial: <action>`, and the `escalate`
value the class implies, and writes nothing. Without the flag the verb is unchanged.

### Launch (`src/nightwright/agentic/launch.py`), static rules

- **Allow:** add `Bash(uv run nw queue main-dirty-apply --trivial-only *)` to
  `conductor_allowed_tools`. `_CONDUCTOR_WIP_VERBS` and `_CONDUCTOR_GIT_VERBS` are unchanged.
- **Verb deny (conductor-only):** SPEC-0543's four rules are replaced. For each prefix
  `uv run nw` and `nw`, and each of `queue main-dirty-apply` and `* queue main-dirty-apply`,
  deny the bare form and the tails ` --root*`, ` --plan*`, ` --json*`, ` --help*`, ` -h*`.
  None of these matches `uv run nw queue main-dirty-apply --trivial-only …`.
- **Unchanged:** `DEFAULTS["disallowed_tools"]` (push deny, `_steward_guard`,
  `_conductor_guard`, `gh pr merge`) and the steward's rules stay byte-identical. No rule
  contains a project path or a base name.
- **`CONDUCTOR_CURSOR_POLICY`** gets this text: "A main_dirty clear of the trivial class runs
  `uv run nw queue main-dirty-apply --trivial-only`; never run it otherwise. Never write the
  main checkout with raw git (add, commit, rm, checkout --, hash-object, update-index,
  worktree add, switch -f / -C / --discard-changes, add -A) or cp; in a worktree under
  `.nw-worktrees/` add and commit as usual. No reset --hard, clean, branch -D, or push." The
  cursor runner has no hooks, so this prose is its guard.

### The hook: `nw queue conductor-git-guard`

The hook is a new command in `cli_conductor.py`, beside `conductor-guard`. It always exits 0,
denies through the PreToolUse JSON decision, and is silent otherwise.
`agent_permissions.sync` adds a second PreToolUse entry,
`{"matcher": "Bash", "hooks": [{"type": "command", "command": "nw queue conductor-git-guard || true"}]}`,
and a `hook_missing`-style check for it. The existing `AskUserQuestion` entry is unchanged.

Logic, for a payload whose `session_id` is a registered conductor session (otherwise silent):

1. Split `tool_input.command` into segments on `&&`, `||`, `;`, `|`, and newlines, then
   `shlex.split` each segment. If a segment does not parse, deny only when it contains
   `git` or `cp`.
2. A `cd <dir>` segment changes the directory for the segments after it. Start from the
   payload's `cwd`.
3. For a `git` segment, skip the global options (`-C <p>` applies cumulatively,
   `--literal-pathspecs`, `-c k=v`, `--git-dir`, `--work-tree`) to find the subcommand.
   `--git-dir` or `--work-tree` is denied outright. The write set is:
   - `add` (any form, including `-A` / `--all`), `commit`, `rm`, `hash-object -w`, and
     `update-index`;
   - `checkout` with a `--` or with a pathspec, but not `checkout -b`;
   - `restore` without `--staged`;
   - `switch` with `-f`, `--force`, `--discard-changes`, `-C`, `--create`, or `-c`;
   - `worktree add`, when its target path does not resolve under a `.nw-worktrees`
     directory.
4. For a write-set segment, resolve the target directory. If
   `git -C <dir> rev-parse --git-dir --git-common-dir` gives equal absolute paths, the target
   is a primary worktree: deny, with the reason
   `conductor: no raw git writes in the main checkout <toplevel>; trivial: main-dirty-apply --trivial-only, else escalate (SPEC-0544)`.
   A linked worktree is allowed. If the directory is not a git repo, the check is silent.
5. For a `cp` segment, deny when the destination (the last argument, or `-t <dir>`) resolves
   inside a primary worktree's toplevel.
6. If `git rev-parse` fails or times out for a write-set segment of a conductor session, deny
   (fail closed). Unreadable JSON and non-conductor sessions stay silent (fail open), as
   SPEC-0382 requires, so the hook can never block a human's session.

### Skill (`skills/nw-conduct/SKILL.md`)

- **`parked.main_dirty` row:** SPEC-0542's row gains one step before its escalation step. If
  the live plan's `action` is `commit`:
  - Run `uv run nw queue main-dirty-apply --trivial-only --root <root> --json` in the
    conductor's own context: one command, no sub-agent, one per root at a time.
  - On `ok: true`, mail `nw mailbox send --from conductor --to steward:<Project> --kind info`
    with the `sha` and the paths. Use `--to human` instead when
    `nw mailbox list --to steward:<Project>` does not show that reader. Then resume each
    `main_dirty` campaign of the root with `--decision "cleared main (commit, <sha>)"
    --rule parked.main_dirty`.
  - On `ok: false`, do not retry. Send SPEC-0542's escalation with an extra
    `--fact "main-dirty-apply: <error>"`.

  Every other action goes straight to SPEC-0542's escalation.
- **`parked.main_not_on_base` row:** the conductor does not move dirt or switch branches in
  the main checkout. It runs `main-dirty-plan` and escalates `human_checkout` (no campaign,
  `--yes`) with the class and `recommended`, as in SPEC-0542 step 4.
- **`wt_broken` row:** mail the steward (or escalate `human_checkout`) to save the tracked
  dirt to a WIP branch and restore main. Use `uv run nw` from a worktree meanwhile. No raw git
  write in the main checkout.
- **Kept as is:** the SPEC-0496 dispatch paragraph and permission-denial paragraph. Sub-agents
  commit in their worktree with plain `git add <paths>` / `git commit -m …`. On a denial, the
  conductor runs the blocked command in that worktree with `git -C <worktree>`. Add one
  sentence: "never in the main checkout; the conductor-git-guard hook denies raw git writes
  there".

### Files the implementer may cherry-pick from `nw/IDEA-1159-20261110-02ed`

- `src/nightwright/agentic/launch.py`: only the `CONDUCTOR_CURSOR_POLICY` wording, as a
  source. **Not** `_CONDUCTOR_WRITE_DENY`, `_conductor_write_guard`, the removal of
  `_CONDUCTOR_WIP_VERBS`, `_branch_ref_ok`, `_conductor_project_bases`, or the
  `switch <base>` allow.
- `src/nightwright/agentic/main_dirty.py` (via SPEC-0543): the source of `_valid_base`-style
  checks, if the hook needs a ref check.
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

- [ ] AC1 `main-dirty-apply --trivial-only` on each non-`commit` live action (`save_branch`,
      `branch_and_escalate`, `hold`, `escalate`, `resume`) writes nothing: HEAD, index bytes,
      worktree bytes, and the branch list are unchanged. It returns `ok: false`,
      `error: not trivial: <action>`. On a trivial diff it commits exactly as SPEC-0543 AC3.
- [ ] AC2 The launch tests' rule matcher, run on the effective rules from
      `build_plan(cfg, "conductor")`, permits
      `uv run nw queue main-dirty-apply --trivial-only --root /r --json`. It denies
      `uv run nw queue main-dirty-apply --root /r --json`, the bare form, `--plan f`,
      `--json`, `-h`, the same forms through `nw`, and
      `uv run nw --x queue main-dirty-apply --root /r`.
- [ ] AC3 **Worktree commits stay permitted.** In a temp repo with a primary checkout `M` and
      a linked worktree `W` under `<parent>/.nw-worktrees/P/x`, the hook is fed a
      conductor-session payload. These are silent (allowed): `git add a` and
      `git commit -m x` with `cwd: W`; `git -C W add a` and `git -C W commit -m x` with
      `cwd: M`; `git -C W rm -f -- a`. They are also permitted by the static conductor rules,
      and by `.claude/settings.json` as `tests/test_claude_settings_allowlist.py` already
      checks.
- [ ] AC4 **The same commands against main are denied.** `git add a`, `git commit -m x`,
      `git commit -am x`, `git add -A` with `cwd: M`, and `git -C M commit -m x` with
      `cwd: W`, are each denied with the SPEC-0544 reason. So are `cd M && git commit -m x`
      and `git -C W -C ../../.. commit -m x` when that resolves to `M`.
- [ ] AC5 The hook denies these against `M`: `git rm -f -- a`, `git checkout -- .`,
      `git checkout HEAD -- a`, `git restore a`, `git hash-object -w -- a`,
      `git update-index --add --cacheinfo 100644,0,a`, `git switch -f main`,
      `git switch -C x`, `git switch --discard-changes main`, `git worktree add ../x`,
      `git --work-tree=M add a`, and `cp a M/b`. It allows `git status`, `git log -1`,
      `git checkout -b x`, `git restore --staged a`, `git worktree add ../.nw-worktrees/P/y`,
      and `cp a W/b`.
- [ ] AC6 The hook is silent for a non-conductor session id and for unreadable JSON, for
      every AC4 command. It denies when `rev-parse` fails for a conductor write segment. It
      always exits 0.
- [ ] AC7 `agent_permissions.sync` adds the `Bash` matcher entry once and is idempotent. It
      leaves the `AskUserQuestion` entry and the other keys untouched.
- [ ] AC8 No conductor static rule contains a project root path or a base branch name: a
      launch with two projects whose roots and bases differ yields identical rule lists.
      `DEFAULTS["disallowed_tools"]`, `_CONDUCTOR_WIP_VERBS`, and the steward's effective
      rules equal a snapshot taken before this spec. The steward still denies
      `git -C /w rm -f --ignore-unmatch -- f "x log y"`.
- [ ] AC9 `CONDUCTOR_CURSOR_POLICY` names `main-dirty-apply --trivial-only` and the main
      checkout write ban. It says worktree add/commit is allowed.
- [ ] AC10 The `parked.main_dirty` row, extracted by rule id, contains
      `main-dirty-apply --trivial-only`, `--from conductor`, `steward:<Project>`,
      `--to human`, `cleared main (commit`, `--rule parked.main_dirty`, and the
      `main-dirty-apply: <error>` fact. Every `main-dirty-apply` in the skill is followed by
      `--trivial-only`. The row contains none of `git add`, `git commit`, `git checkout`,
      `git rm`, `git worktree add`. The `parked.main_not_on_base` and `wt_broken` rows name
      `human_checkout` or the steward and no raw git write. The SPEC-0496 paragraphs keep
      their in-worktree `git add` / `git commit` wording and gain the main-checkout sentence.
- [ ] AC11 SPEC-0542's escalation is unchanged for every non-`commit` class: its skill tests
      still pass.

## Test plan

- **Automated tests:**
  - New `tests/test_conductor_git_guard.py` (`pytestmark = pytest.mark.fast`) covers
    AC3–AC6. It builds a temp primary checkout plus a linked worktree under `.nw-worktrees/`,
    registers a conductor session in a temp cfg, and invokes `conductor-git-guard` through
    `CliRunner` with hook JSON (`session_id`, `cwd`, `tool_input.command`). It asserts the
    decision for each command, parametrized.
  - `tests/test_agentic_launch.py` covers AC2, AC8, and AC9, including the snapshot and the
    two-project launch.
  - `tests/test_agent_permissions.py` (or the existing permissions test module) covers AC7.
  - `tests/test_main_dirty_apply.py` (SPEC-0543) covers AC1.
  - `tests/test_main_dirty_plan.py` (SPEC-0542) extends its skill-row test for AC10 and AC11.
- **Regression guard:** `tests/test_claude_settings_allowlist.py` (SPEC-0496) passes
  unchanged. Also run `python3 tools/affected_tests.py --run uv run pytest -q`,
  `python3 tools/test_tiers.py --check`, and `python3 tools/spec_lint.py`.
- **Manual verification (real binary):**
  1. Install the hook (`nw skills install` or `agent_permissions.sync` on a temp settings
     file passed through `--settings`).
  2. Print the conductor plan with `uv run nw queue conductor-launch --dry-run --json`.
  3. Run the real `claude -p` once as a registered conductor session, with the plan's
     `--allowedTools` / `--disallowedTools`, `--permission-mode dontAsk`, and cwd a
     nightwright worktree. Use a scratch repo under the scratchpad with a linked worktree
     under `.nw-worktrees/` and a frontmatter-only edit on main.
  4. Have it run `git -C <main> commit -am x` (expect denied by the hook) and
     `git -C <wt> commit --allow-empty -m x` (expect a new commit in the worktree).
  5. Have a sub-agent spawned with `isolation: worktree` run a plain `git add` +
     `git commit -m x` (expect committed). This confirms that the sub-agent's hook payload
     carries the conductor's session id and its own worktree `cwd`.
  6. Have it run `uv run nw queue main-dirty-apply --root <main> --json` (expect denied), then
     `uv run nw queue main-dirty-apply --trivial-only --root <main> --json` (expect
     `ok: true`).

  Record each result in As shipped.

## Rollout / migration

Ships after SPEC-0543. `nw skills install` (or the next `agent_permissions.sync`) adds the
`Bash` hook entry. A conductor launched after the deploy gets the new verb rules. A running
conductor keeps its old flags until it is restarted (`nw queue conductor-launch` compares
`launch_hash`), but the hook applies at once. Worktree sub-agent commits are unaffected.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed, including the real-binary check; affected tests pass.
- [ ] No regressions in launch, settings-allowlist, skills, and main-dirty tests.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` regenerated; `python3 tools/spec_lint.py` exits 0.

## Open questions

None.
