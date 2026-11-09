---
id: SPEC-0496
title: Sub-agents in a worktree can commit; allowed command forms are documented
status: accepted
owner: user
created: 2026-11-09
kind: feature
tags: [agentic, permissions, conductor]
source_idea: IDEA-1071
---

## Context

Background sub-agents working in a git worktree could not commit. The committed
`.claude/settings.json` allowed only Bash commands that START with `nw`, `uv run`,
`python3 tools/`, `git worktree`, `git status`, `git diff`, `git log`, `git add`,
`git commit` and `git merge --ff-only`. Claude Code matches allow rules against the
command prefix, so `cd <wt> && git commit`, `git -C <wt> commit`, pipes and compound
commands fall through to the auto-mode classifier and are denied. A background
sub-agent cannot answer the prompt, the denial was never escalated or handled, and
spec-fix work stalled at commit.

Root causes: (1) the allowlist lacked ordinary worktree-local git forms (`git rebase`,
branch creation, `git restore --staged`, read-only `git show` / `rev-parse`); (2) the
briefs conductors write (nw-conduct / nw-orient skills under `skills/`) never told
sub-agents to run in an isolated worktree with their cwd already the worktree and to use
plain single commands; (3) the conductor had no rule for a denied commit.

## Goals / Non-goals

**Goals**
- Extend the committed allowlist with safe worktree-local git forms.
- Document the brief contract and a conductor rule for denied commands.
- Guard the allowlist with a test.

**Non-goals**
- Allowing `git -C` (cannot be restricted to sibling worktrees by a portable pattern),
  `git push`, `git reset`, `git branch -D`, or bare `git stash` (the stash stack is shared
  across worktrees).
- Editing user-level or local-only settings.

## Decision

Add to `.claude/settings.json`: `git rebase *`, `git switch -c *`, `git checkout -b *`,
`git branch --show-current`, `git branch --list *`, `git restore --staged *`,
`git show *`, `git rev-parse *`, `git ls-files *`. Update `skills/nw-orient/SKILL.md` and
`skills/nw-conduct/SKILL.md`: briefs must say to spawn with `isolation: worktree` (cwd is
the worktree, so never `cd` or `git -C`), to run every git command as a plain single
command, and the conductor treats a sub-agent reporting a permission denial as its own
work item: it does not wait, it runs the blocked plain command itself in that worktree
(or dispatches a fresh sub-agent with the corrected brief).

## Design

Config and skill text only; no code. `tests/test_claude_settings_allowlist.py` loads the
committed file.

## Alternatives considered

- **Allow `Bash(git *)`** - too broad (push, reset --hard, branch -D).
- **Allow `git -C *`** - lets an agent touch any repo, including the main checkout.

## Acceptance criteria

- [ ] `.claude/settings.json` is valid JSON and allows the forms listed in Decision.
- [ ] It does not allow `git push`, `git reset`, `git -C`, bare `git stash`, or `git *`.
- [ ] nw-orient and nw-conduct skills state the brief contract and the denied-command rule.
- [ ] The guard test passes.

## Test plan

- **Automated tests:** `tests/test_claude_settings_allowlist.py` asserts valid JSON,
  every entry is `Bash(...)`, required prefixes present, forbidden ones absent; and that the
  skills mention `isolation: worktree`.
- **Manual verification:** run `git commit` as a plain command in a worktree sub-agent.
- **Regression guard:** `tests/test_no_settings_writes.py` and spec lint still pass.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; affected tests pass.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated.
