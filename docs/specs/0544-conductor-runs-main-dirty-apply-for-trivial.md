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

IDEA-1159 asks that the conductor itself commit a trivial stray edit (frontmatter, docs prose,
comments) that blocks closes, and escalate only what needs the human. The human said yes to the
conductor clearing `main_dirty`, and approved doing it through one audited verb,
`nw queue main-dirty-apply`, instead of raw git allow-rules (campaign
`IDEA-1159-20261110-02ed`, resume note seq 437). At ESC-0067 (2026-11-11) the human chose to
split [SPEC-0537](./0537-conductor-must-clear-main-dirty-itself-a-stray.md); this is part C.
[SPEC-0542](./0542-main-dirty-plan-and-escalate.md) gives the conductor the plan and the
escalation; [SPEC-0543](./0543-main-dirty-apply-audited-verb.md) ships the verb for the human
and the steward and denies it to the conductor.

This spec lets the conductor run the verb for `action: commit` only, and closes the raw git
write paths that the branch's review found open (`worktree add *` and `switch --create`
unrestricted, `commit -m *` allowing `-a` / `--amend` on the base, a project base interpolated
into an allow glob, a deny rule per project directory, the steward losing its swallowed-`rm`
deny, and skill text telling the conductor and its sub-agents to run `git add` / `git commit`
that the new deny list blocks).

## Goals / Non-goals

**Goals**

- On a `main_dirty` park whose live plan is `commit`, the conductor runs
  `uv run nw queue main-dirty-apply --trivial-only` itself, mails the owner, and resumes.
- Every other class still escalates exactly as SPEC-0542 says.
- The conductor and its sub-agents hold no raw git write rule; the deny list is the branch's
  full conductor-only list.
- The skill text matches the permissions: nothing in it tells the conductor or a sub-agent to
  run a denied command.

**Non-goals**

- The conductor never runs the verb for `save_branch`, `branch_and_escalate`, `hold`, or
  `escalate`; the human or the steward does (SPEC-0543).
- No change to the steward's allow or deny lists.
- No `git switch <base>` allow for the conductor (the trivial path never switches).

## Decision

Add `--trivial-only` to `main-dirty-apply`: after all SPEC-0543 guards, it refuses with no
write unless the live action is `commit`. Replace SPEC-0543's blanket conductor deny of the verb
with denies of every form that does not start with `--trivial-only`, and allow the
`--trivial-only` form. Move the branch's `_CONDUCTOR_WRITE_DENY` (plus `cp`, `git add -A`,
`git add --all`) into a conductor-only deny list and drop `_CONDUCTOR_WIP_VERBS` from the
conductor allow-list. Update `CONDUCTOR_CURSOR_POLICY` and `skills/nw-conduct/SKILL.md`.

The steward directed the full deny list. Its consequence, recorded here on purpose: the
conductor's worktree sub-agents (SPEC-0496) can no longer `git add` / `git commit`; they report
their diff and the steward commits it, as the branch's skill text already said. Narrowing the
deny so worktree commits stay allowed is a later spec, if evidence shows the cost.

## Design

### The flag

`apply_clear(root, plan=None, *, trivial_only=False)`; CLI `--trivial-only`. With it, after the
lock, re-plan, in-progress, path, and index guards, a live action other than `commit` returns
`ok: false`, `error: not trivial: <action>`, `escalate` as the class implies, and writes
nothing. Without the flag the verb is unchanged.

### Launch (`src/nightwright/agentic/launch.py`)

- **Allow:** `Bash(uv run nw queue main-dirty-apply --trivial-only *)` added to
  `conductor_allowed_tools`. `_CONDUCTOR_WIP_VERBS` is removed from it; `worktree list` stays.
  The read-only `_CONDUCTOR_GIT_VERBS` stay for `git` and `git -C *`.
- **Verb deny (conductor-only):** SPEC-0543's four rules are replaced by, for each prefix
  `uv run nw` and `nw`, and each of `queue main-dirty-apply` and `* queue main-dirty-apply`:
  the bare form and the tails ` --root*`, ` --plan*`, ` --json*`, ` --help*`, ` -h*`. None of
  them matches `uv run nw queue main-dirty-apply --trivial-only …`.
- **Raw git deny (conductor-only, `_conductor_extra_deny`):** each entry of the branch's
  `_CONDUCTOR_WRITE_DENY` as `Bash(git X)` and `Bash(git * X)`: `checkout HEAD -- *`,
  `checkout -- *`, `rm -f -- *`, `rm -r*`, `rm -rf*`, `add -- *`, `hash-object -w -- *`,
  `commit -m *`, `commit *`, `worktree add *`, `worktree add -B *`, `switch --create *`,
  `-C */.nw-worktrees/* add *`, `-C */.nw-worktrees/* rm *`, `-C */.nw-worktrees/* commit *`,
  `-C */.nw-worktrees/* update-index *`, `update-index *`; plus `Bash(cp *)`,
  `Bash(git add -A)`, `Bash(git add --all)`. Static strings only: no project path or base name
  is interpolated into any rule.
- **Shared list unchanged:** `DEFAULTS["disallowed_tools"]` (push deny, `_steward_guard`,
  `_conductor_guard`, `gh pr merge`) is byte-identical to before, so the steward keeps its
  `git * rm *` swallow deny and its `git -C <dir> add|commit` and
  `git worktree add ../.nw-worktrees/*` allows.
- **`CONDUCTOR_CURSOR_POLICY`:** "A main_dirty clear of the trivial class runs
  `uv run nw queue main-dirty-apply --trivial-only`; never run it otherwise. No raw git
  checkout, add, commit, rm, worktree add, update-index, or hash-object; no reset --hard,
  clean, branch -D, switch -f, cp, or push. Otherwise write only under `.nw-worktrees/`."

### Skill (`skills/nw-conduct/SKILL.md`)

- **`parked.main_dirty` row** (SPEC-0542's row, one step added before its escalation step):
  if the live plan's `action` is `commit`, run
  `uv run nw queue main-dirty-apply --trivial-only --root <root> --json` in the conductor's own
  context (one command, no sub-agent, one per root at a time). `ok: true` → mail
  `nw mailbox send --from conductor --to steward:<Project> --kind info` when
  `nw mailbox list --to steward:<Project>` shows that reader, else `--to human`, with the `sha`
  and the paths; then resume each `main_dirty` campaign of the root with
  `--decision "cleared main (commit, <sha>)" --rule parked.main_dirty`. `ok: false` → no retry;
  SPEC-0542's escalation with an extra `--fact "main-dirty-apply: <error>"`. Every other action
  goes straight to SPEC-0542's escalation. The row never runs the verb without
  `--trivial-only`.
- **Dispatch paragraph and permission-denial paragraph** (SPEC-0496 text): replace with the
  branch's wording — neither the conductor nor its sub-agents run raw `git add` / `git commit` /
  `git checkout` / `git rm` / `git worktree add`; a sub-agent reports its diff and the steward
  commits it; never run the blocked command yourself.
- **`parked.main_not_on_base` row:** the conductor can no longer move dirt or switch branches:
  run `main-dirty-plan` and escalate `human_checkout` (no campaign, `--yes`) with the class and
  `recommended`, as SPEC-0542 step 4.
- **`wt_broken` row:** mail the steward where it lives (or escalate `human_checkout`) to save
  the tracked dirt to a WIP branch and restore main; use `uv run nw` from a worktree meanwhile;
  no raw git write.

### Files the implementer may cherry-pick from `nw/IDEA-1159-20261110-02ed`

- `src/nightwright/agentic/launch.py` — `_CONDUCTOR_WRITE_DENY`, `_conductor_write_guard`,
  `_conductor_extra_deny`, `_CONDUCTOR_WIP_MISC`, `_conductor_wip_allow_rules`, the
  `build_plan` conductor merge, the `conductor_disallowed_tools` `add -A` entries. **Not**
  `_branch_ref_ok`, `_base_from_project_doc`, `_conductor_project_bases`, or the
  `switch <base>` part of `_conductor_extra_allow`.
- `tests/test_agentic_launch.py` — the conductor deny cases,
  `test_conductor_denies_cp_by_rule_steward_does_not`, the steward swallow-family tests,
  `test_push_guard_rule_count`, `git branch -D` cases; drop the `switch <base>` cases.
- `skills/nw-conduct/SKILL.md` — lines 45 and 47 (dispatch / permission-denial) and the
  `wt_broken` row; the `parked.main_dirty` row only as a source of phrases.
- `tests/test_main_dirty_clear.py` — `test_conductor_cursor_policy_allows_clear`,
  `test_skill_row_phrases` (rewritten to this row).

## Alternatives considered

- **Let the conductor run the verb for every class.** The human approved escalating
  substantive edits in their checkout; non-trivial saves move their work to a branch and stay
  with the human or the steward.
- **Enforce trivial-only by the conductor's identity** (an env var the verb reads). The cursor
  runner launches with no env, and an env check is invisible in the permission list; a flag
  plus deny globs is visible and testable with the real binary.
- **Keep `_CONDUCTOR_WIP_VERBS` for worktree commits.** The steward chose the full branch deny
  list; this spec records the cost (Decision) instead.
- **A second verb (`main-dirty-commit`).** The human approved a single audited verb.

## Acceptance criteria

- [ ] AC1 `main-dirty-apply --trivial-only` on each non-`commit` live action (`save_branch`,
      `branch_and_escalate`, `hold`, `escalate`, `resume`) writes nothing (HEAD, index bytes,
      worktree bytes, branch list unchanged) and returns `ok: false`,
      `error: not trivial: <action>`; on a trivial diff it commits exactly as SPEC-0543 AC3.
- [ ] AC2 With the conductor's effective rules from `build_plan(cfg, "conductor")`, checked by
      the launch tests' rule matcher: `uv run nw queue main-dirty-apply --trivial-only --root /r
      --json` is permitted; denied: `uv run nw queue main-dirty-apply --root /r --json`, the
      bare form, `--plan f`, `--json`, `-h`, the same through `nw`, and
      `uv run nw --x queue main-dirty-apply --root /r`.
- [ ] AC3 Denied for the conductor: `git checkout -- .`, `git checkout HEAD -- a`, `cp a b`,
      `git rm -r a`, `git rm -f -- a`, `git -C /w/.nw-worktrees/p/x rm -f -- a`, `git clean -fd`,
      `git reset --hard`, `git switch -f main`, `git switch --discard-changes main`,
      `git switch --create x`, `git switch -C x`, `git add -- a`, `git add -A`, `git commit -m x`,
      `git commit -am x`, `git commit --amend`, `git worktree add ../x`,
      `git hash-object -w -- a`, `git update-index --add --cacheinfo 100644,0,a`,
      `git branch -D x`, and the push forms. Still permitted: `git status`, `git log -1`,
      `git -C /w diff`, `git worktree list`.
- [ ] AC4 No conductor rule contains a project root path or a base branch name (launch with two
      projects whose roots and bases differ yields identical rule lists).
- [ ] AC5 `DEFAULTS["disallowed_tools"]` and the steward's effective rules equal a snapshot
      taken before this spec; the steward still permits `git -C /w add a`,
      `git -C /w commit -m x`, `git worktree add ../.nw-worktrees/p/x`, and still denies
      `git -C /w rm -f --ignore-unmatch -- f "x log y"`.
- [ ] AC6 `CONDUCTOR_CURSOR_POLICY` names `main-dirty-apply --trivial-only`, says never run it
      otherwise, and names `reset --hard`, `clean`, `branch -D`, `switch -f`, `cp`, `push` as
      forbidden.
- [ ] AC7 The `parked.main_dirty` row (extracted by rule id) contains
      `main-dirty-apply --trivial-only`, `--from conductor`, `steward:<Project>`, `--to human`,
      `cleared main (commit`, `--rule parked.main_dirty`, and the `main-dirty-apply: <error>`
      fact; every occurrence of `main-dirty-apply` in the whole skill is followed by
      `--trivial-only`; it contains none of `git add`, `git commit`, `git checkout`, `git rm`,
      `git worktree add`.
- [ ] AC8 No sentence in `skills/nw-conduct/SKILL.md` tells the conductor or a sub-agent to run
      `git add`, `git commit`, `git checkout --`, `git rm`, or `git worktree add` (the old
      "`git add` / `git commit` are allowed as single commands" and "`git add <paths>` then
      `git commit -m`" phrases are gone; the sub-agent-reports-its-diff sentence is present);
      the `parked.main_not_on_base` and `wt_broken` rows name `human_checkout` or the steward and
      no raw git write.
- [ ] AC9 SPEC-0542's escalation rules are unchanged for every non-`commit` class (its skill
      tests still pass).

## Test plan

- **Automated tests:** `tests/test_agentic_launch.py` (marker already present) — AC2–AC6,
  parametrized over the command strings above, using the existing rule-matching helper, plus a
  snapshot of the steward rules (AC5) and a two-project launch (AC4). From the branch take the
  conductor deny cases, `test_conductor_denies_cp_by_rule_steward_does_not`, the steward
  swallow-family tests, and `test_push_guard_rule_count`. `tests/test_main_dirty_apply.py`
  (SPEC-0543) — AC1, one case per action with the before/after snapshot.
  `tests/test_main_dirty_plan.py` (SPEC-0542) — extend the skill-row test for AC7, AC8, AC9.
- **Regression guard:** `python3 tools/affected_tests.py --run uv run pytest -q`,
  `python3 tools/test_tiers.py --check`, `python3 tools/spec_lint.py`.
- **Manual verification (real binary):** from a nightwright worktree, print the conductor plan
  (`uv run nw queue conductor-launch --dry-run --json`) and run the real `claude -p` once with its
  `--allowedTools` / `--disallowedTools` and `--permission-mode dontAsk`, cwd the worktree,
  against a scratch repo under the scratchpad with a frontmatter-only edit. Ask it to run, in
  order, `git -C <scratch> commit -am x` (expect denied),
  `uv run nw queue main-dirty-apply --root <scratch> --json` (expect denied), and
  `uv run nw queue main-dirty-apply --trivial-only --root <scratch> --json` (expect `ok: true`
  and a new commit in the scratch repo). Record the three results in As shipped.

## Rollout / migration

Ships after SPEC-0543. A conductor launched after the deploy gets the new rules; a running one
keeps its old flags until restarted (`nw queue conductor-launch` compares `launch_hash`). Tell
stewards that conductor worktree sub-agents now hand diffs to them to commit.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed, including the real-binary check; affected tests pass.
- [ ] No regressions in launch, skills, and main-dirty tests.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` regenerated; `python3 tools/spec_lint.py` exits 0.

## Open questions

None.
