---
id: SPEC-0320
title: "`wt skills install` provisions the conductor and steward allow rules for wt commands"
status: done
owner: user
created: 2026-10-31
updated: 2026-10-31
source_idea: IDEA-510
milestone: "M10: agentic MVP"
tags: [agentic, conductor, permissions, install, mvp]
---

## Context

On 2026-10-31 the conductor ran `wt queue add IDEA-008 --yes` to queue the comfort-assistant
canary (SPEC-0298 exit criterion E5). Claude Code's auto-mode classifier denied the Bash call
because the campaign opens a GitHub PR, and the conductor rightly refused a steward-relayed
approval. Any queue action the classifier sees as outward-facing therefore stalls until the
human approves in person, which contradicts E5 ("the human is only pulled in for scope
decisions"). An explicit `permissions.allow` rule takes precedence over the classifier (the
settings schema: `autoMode.classifyAllShell`, off by default, is what would suspend allow rules),
but Claude Code refuses to let an agent write its own permission settings. The rules must come
from a human-run step, and new installs need them too.

## Goals / Non-goals

**Goals**
- The human-run install step (`wt skills install`) provisions the allow rules, so a fresh
  install lets the conductor and stewards run their own `wt` commands.
- Merge, never replace: every other settings key and allow entry survives; re-running is a no-op.
- `wt skills list` reports whether the rules are present.

**Non-goals**
- Allowing `gh`, `git push` or any non-`wt` command. PR creation stays inside the queue's close
  step, which runs under the runner, not as a conductor Bash call.
- The session launcher (IDEA-509), which may later pass the same list with `--settings`.
- Removing rules (uninstall); not needed yet.

## Decision

A new module `wt.agent_permissions` holds one code-defined `ALLOW_RULES` list and merges it into
`permissions.allow` of the Claude Code **user** settings file (`~/.claude/settings.json`). User
scope, because stewards run in other projects' repositories (comfort-assistant). `wt skills
install` calls it by default.

## Design

- `ALLOW_RULES`: `Bash(wt <group> *)` for queue, mailbox, campaign, escalations, idea, ideas,
  spec, hub, next, projects; plus `Bash(WT_MAILBOX_ADDR=* wt <group> *)` for queue, mailbox,
  campaign, escalations, idea, spec (the addressed form the conductor and stewards use).
- `missing(path)` lists the rules not yet present. `install(path)` appends the missing ones in
  order and returns them, or returns `[]` and leaves the file untouched. It creates a missing
  file. The write is atomic (temp file plus `os.replace`) and keeps the file's mode. Invalid
  JSON, a non-object document or a non-list `permissions.allow` raises `ValueError`, and the
  file is never rewritten.
- CLI: `wt skills install` gains `--permissions/--no-permissions` (default on) and `--settings
  PATH`. With `--dest` (a non-default install, and the form tests use), rules are written only
  when `--settings` is also given, so tests never touch the real settings. `wt skills list`
  prints an `allow rules  present | N missing` line under the same rule.

- **Plain `wt` commands in the skills.** Claude Code strips only certain known-safe env
  assignments before matching an allow rule, and `WT_MAILBOX_ADDR` is not one of them; a
  compound command needs every part allowed. The first install still saw denials because the
  conductor ran `WT_MAILBOX_ADDR=conductor wt …`. `wt-conduct` and `wt-steward` now pass
  `--from` / `--by` instead and never prefix or `cd … &&` a `wt` command. The prefixed rules
  stay in the list as a fallback; whether Claude Code matches a wildcard in that position is
  unverified.
- **`wt queue add --by`.** The other queue writes already take `--by` (SPEC-0273); `queue add`
  recorded every campaign as `human` and ignored even `$WT_MAILBOX_ADDR`. It now takes `--by`
  (default `$WT_MAILBOX_ADDR`, then `human`, validated like the others) and passes it to
  `apply_add` / `apply_add_idea` / `apply_add_batch`, so a plain `wt queue add … --by conductor`
  is both allowed and correctly attributed.

## Alternatives considered

- **Project `.claude/settings.local.json` per repo:** covers only one checkout; every project a
  steward runs in would need its own copy. Rejected for user scope.
- **Dry-run by default with `--yes`:** the install step is already a write; the human asked to
  re-run the install step and have it work.
- **Only the launcher's `--settings`:** sessions started without the launcher would still be
  blocked. Kept as a later addition (IDEA-509), not instead.

## Acceptance criteria

- [x] `wt skills install` (default destination) merges `ALLOW_RULES` into
      `~/.claude/settings.json` and prints what it added, or "up-to-date".
- [x] Existing keys, allow entries, deny rules and the file mode are preserved; no duplicates.
- [x] Re-running adds nothing and leaves the file byte-identical.
- [x] Invalid settings JSON is refused with an error and left unchanged.
- [x] With `--dest` and no `--settings`, neither `install` nor `list` reads or writes settings.
- [x] `--no-permissions` skips the rules; `wt skills list` reports presence.
- [x] No rule allows `gh` or `git`.
- [x] The `wt-conduct` and `wt-steward` skills name `--from` / `--by` and forbid the
      `WT_MAILBOX_ADDR=` prefix.
- [x] `wt queue add --by` records the actor on the campaign's events (single and batch).

## Test plan

- **Unit/CLI (`tests/test_agent_permissions.py`, all on tmp paths):** rule coverage and no
  `gh`/`git`; create a missing file; merge keeping other keys, deny rules, order and mode;
  idempotent byte-identical rerun; invalid JSON refused; CLI install with `--settings` writes
  and `list` reports present; `--dest` alone never calls the module; `--no-permissions`.
- **`tests/test_agentic_queue_by.py`:** `queue add --by conductor`, the `$WT_MAILBOX_ADDR`
  default, and a rejected bad address.
- **Regression:** `tests/test_skills.py` and the full `uv run pytest`.
- **Manual:** the human runs `wt skills install` and checks the conductor can run
  `wt queue add` for a PR-policy campaign without a classifier denial.

## Clock Log

## Rollout / migration

Takes effect when the human re-runs `wt skills install`. Claude Code reloads the settings file,
so running sessions pick the rules up without a restart.

## Definition of done

- [x] Acceptance criteria checked.
- [x] Test plan executed: `tests/test_agent_permissions.py` and `tests/test_skills.py` pass
      (85 passed); full suite run before commit.
- [x] Ledger regenerated; `python3 tools/spec_lint.py` exits 0.

## As shipped

Shipped directly on `main` at the human's request (2026-10-31: "change directly in main", no
queue campaign). The manual check is the human's re-run of `wt skills install`.

## Open questions

None.
