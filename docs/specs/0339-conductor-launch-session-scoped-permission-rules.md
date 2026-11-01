---
id: SPEC-0339
title: "`nw queue conductor-launch` passes session-scoped permission rules (allowedTools / disallowedTools)"
status: done
owner: user
created: 2026-11-01
updated: 2026-11-01
kind: feature
tags: [agentic, conductor, permissions]
depends_on: [SPEC-0319]
---

## Context

The conductor needs to push `nw/*` / `wt/*` branches and write PR metadata without a prompt
each time, but the human does not want those rules in their global or project settings
(where every other session would inherit them), and never wants the conductor to push to
`main`/`master`, force-push without lease, or merge PRs. `claude` accepts
`--allowedTools` / `--disallowedTools` ("comma or space-separated list", variadic;
Claude Code 2.1.316 `claude --help`), which apply to that session and its sub-agents only.
[SPEC-0319](./0319-wt-queue-conductor-launch-steward-launch-start.md) (done, so not rewritten)
already builds the launch command line from `agentic.conductor.launch.*`.

## Goals / Non-goals

**Goals**
- New keys `allowed_tools` and `disallowed_tools` (lists of rule strings) under
  `agentic.conductor.launch`, with safe code-held defaults, turned into the two flags.
- Visible in `--dry-run` (text and `--json`) and applied under `--resume`.

**Non-goals**
- No `--dangerously-skip-permissions`, no `--permission-mode` change.
- Read-only `gh pr view/list/checks/diff` rules are deliberately not defaults (the human
  puts them in their global settings).

## Decision

Add the two list keys to `launch.DEFAULTS`. `build_plan` emits `--allowedTools r1 r2 …` and
`--disallowedTools r1 r2 …` (one argv element per rule); an empty list emits no flag.

## Design

- Rule syntax: Claude Code's current form is a space-wildcard, `Bash(git push *)`
  (matching the `claude --help` example `Bash(git *)`); the older `:*` prefix form is
  deprecated, so defaults use ` *`.
- Defaults, allowed: `Bash(git push --force-with-lease origin nw/*)`,
  `Bash(git push origin nw/*)`, the same two for `wt/*`, `Bash(gh pr create *)`,
  `Bash(gh pr edit *)`, `Bash(gh pr comment *)`.
- Defaults, disallowed: `Bash(git push * main)`, `Bash(git push * master)`,
  `Bash(git push * HEAD:main)`, `Bash(git push * HEAD:master)`, `Bash(git push --force *)`,
  `Bash(git push -f *)`, `Bash(gh pr merge *)`. Deny rules take precedence over allow rules
  in Claude Code, so `--force-with-lease` (a different flag) stays allowed.
- A configured list replaces the default wholesale (so `[]` removes it). A non-list or a
  list with a non-string/empty entry is a bad value and falls back to the default (same
  rule as the other launch keys). Steward launches use the same keys, and a `steward`
  sub-mapping can override them.
- The flags are variadic, so they are placed before `--model` (never directly before the
  positional prompt, which they would swallow). The prompt stays the last argv element.
- `plan` gains `allowed_tools` / `disallowed_tools`; the text dry-run prints them on two lines
  before the command; `--json` carries them as fields. `--resume` goes through the same
  `build_plan`, so it applies them.
- Config documentation: the launch keys are documented only in SPEC-0319's table; this
  spec is the reference for the two new keys.

## Alternatives considered

- **Global/project `settings.json` rules** — leak into the human's other sessions; rejected.
- **`--permission-mode` / skip-permissions** — explicitly out of scope.

## Acceptance criteria

- [x] Default plan includes `--allowedTools` and `--disallowedTools` with the defaults above.
- [x] Config overrides replace the defaults; empty lists add no flags; bad values fall back.
- [x] `--dry-run` text and `--json` show the rules; `--resume` applies them.
- [x] The prompt remains the last argv element; no other flags changed.

## Test plan

- **Automated tests:** `tests/test_agentic_launch.py`: default flags, JSON/text dry-run,
  config override (conductor and steward), empty lists, bad values, resume, prompt last.
- **Manual verification:** `uv run nw queue conductor-launch --dry-run`; the first live
  launch must test one `nw/*` push.
- **Regression guard:** full `uv run pytest` and `python3 tools/spec_lint.py`.

## As shipped

As designed. Flags go before `--model`. `uv run pytest` green; dry-run checked manually.

## Rollout / migration

None; existing configs gain the defaults on next launch.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

- Unverified: whether `--allowedTools` overrides Claude Code's auto-mode classifier (it may
  still prompt or block). The first live use must test one `nw/*` push and record the result
  here.
