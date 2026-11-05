---
id: SPEC-0437
title: "Conductor and steward launch via cursor-agent"
status: done
owner: user
created: 2026-11-05
updated: 2026-11-05
kind: feature
parent: SPEC-0435
tags: [agentic, launch, cursor]
depends_on: [SPEC-0436, SPEC-0319]
source_idea: IDEA-1006
---

## Context

Child 2 of [SPEC-0435](./0435-cursor-agent-backend-for-the-agentic-queue.md). Child 1
([SPEC-0436](./0436-persona-cursorrunner-and-cursor-model-maps.md)) shipped `CursorRunner`
for one-shot persona jobs, YAML maps, and `resolve_runner`. Conductor and steward sessions
still start with `claude` via [SPEC-0319](./0319-wt-queue-conductor-launch-steward-launch-start.md)
`build_plan` / `nw queue conductor-launch` / `steward-launch`. When Claude credits are
exhausted, those long-lived sessions have to move too.

Installed `cursor-agent` (2026.11.05): interactive (no `-p`) accepts `--model`, `--force`
/`--yolo`, `--trust`, `--workspace`, `--approve-mcps`, `--resume [chatId]`. `create-chat`
prints a UUID. There is no `--session-id`, `-n`, `--permission-mode`, `--allowedTools` /
`--disallowedTools`, `--effort`, or `--autocompact`.

## Goals / Non-goals

**Goals**
- `build_plan` argv starts with `cursor-agent` when that session's `resolve_runner` is
  `cursor` (conductor = host default; steward = `project=<P>`).
- Drop Claude-only flags and `CLAUDE_CODE_*` env on a Cursor launch.
- Map `dontAsk` + allow/deny lists to `--force --trust` plus a short policy footer in the
  prompt (Cursor has no tool-rule CLI flags).
- `--model` through the SPEC-0436 YAML map. `claude-sonnet-5-5` (the compiled launch
  default) is the sonnet family: map as `sonnet` with the YAML default effort (`high`).
- Fresh Cursor session: `cursor-agent create-chat` for the id, register it, exec
  `--resume <id>`. `--resume` on the nw command reuses the newest Cursor launch record
  (no Claude transcript required).
- Default remains Claude. Live sessions are not restarted on a host/project flip.

**Non-goals**
- Changing `/nw-conduct` or `/nw-steward` skill prose.
- Auto-restarting a live session when config flips.
- Cursor USD prices. Skip the SPEC-0299 price refusal when the session runner is `cursor`.
- Translating the glob allow/deny lists into a Cursor deny file.
- Persona `-p` jobs (child 1).

## Decision

When `resolve_runner` for the session is `cursor`, `build_plan` emits a Cursor argv and
empty Claude env; `run` resolves `cursor-agent` like personas, creates or reuses a Cursor
chat id, registers it, and execs. Claude argv, env, price check, and transcript resume
stay as they are when the session resolves to `claude`.

## Design

### Resolution

`resolve_runner(cfg)` for conductor (no campaign, no project).
`resolve_runner(cfg, project=P)` for steward. Env still wins. A host or project flip does
not signal a live session; the next `conductor-launch` / `steward-launch` uses the new
value. Restart those sessions after a credit flip if the session itself must move.

`plan["runner"]` is `claude` or `cursor` so dry-run, JSON, and tests can see it.

### Cursor argv (pinned against this CLI)

Fresh and resume both use `--resume <chatId>` (Cursor has no `--session-id`):

```
cursor-agent --resume <chatId> --force --trust --approve-mcps
  --workspace <cwd> --model <mapped>
  <skill prompt + policy footer>
```

`--force` is the `dontAsk` stand-in (always on for these roles unless permission mode is
`plan`, which becomes `--mode plan --trust` without `--force`). `--approve-mcps` avoids an
interactive MCP prompt that would stall a dontAsk session. No `-p`. No Claude flags.

`run()` for a **fresh** Cursor launch: `cursor-agent create-chat` (stdout = UUID; observed
2026-11-05), write that id onto `plan["session_id"]` and the `--resume` value, then
register and exec. Dry-run does not call `create-chat`; it shows a placeholder uuid.
`run()` for **`--resume`**: skip `create-chat`; use the newest launch record. Require
that record's `runner` extra to be `cursor`. Skip `find_transcript` (Cursor chats are not
Claude jsonl). A previous Claude launch cannot be resumed as Cursor — start fresh.

`register` stores `runner` on the session row next to `launch_hash`.

### Model

`map_launch_model(model, effort, cfg)`:

- `claude-opus*`, `claude-sonnet*`, `claude-haiku*` → alias `opus` / `sonnet` / `haiku`
  with `effort=None` so YAML `defaults:` apply (sonnet/opus high, haiku medium). That is
  how `claude-sonnet-5-5` becomes `grok-4.7-high`.
- profile aliases pass through `map_model` with the launch effort.
- any other non-`claude-*` id is passed through.

`check_price` / `missing_prices` no-op when the session (host, for `missing_prices`)
resolves to `cursor`.

### Policy footer

Cursor cannot enforce SPEC-0353/0382/0424 globs. The prompt still starts with the skill
line, then a short MUST-follow block: no AskUserQuestion; no push to main/master; no
`gh pr merge`; no edits on the main checkout (only `.nw-worktrees/`); no
`reset --hard` / `clean` / `branch -D` / `switch -f`. Steward adds fetch/rebase/commit
in worktrees. Dry-run still prints the Claude allow/deny lists (documentation); they are
not on the Cursor argv. The glob explosion stays out of the prompt.

### Binary and env

`runproc.resolve_cursor_agent` / `with_cursor_agent`. Missing binary is a ClickException.
Cursor env is empty (no `CLAUDE_CODE_*`); `env_remove` still drops
`CLAUDE_CODE_EFFORT_LEVEL`.

### Doctor

`cursor_needed` is also true when any `agentic.project_runners` value is `cursor`, so a
steward-only overlay still probes login.

## Alternatives considered

- **Dump every `--disallowedTools` glob into the prompt** — tens of thousands of tokens
  of matcher noise. Lost; the policy is a dozen lines, the globs exist for Claude's
  matcher.
- **`--session-id` analogue without create-chat** — Cursor would mint an id we never
  see, so `--resume` could not work. `create-chat` then `--resume` is the registry.
- **Keep the price refusal on Cursor ids** — there is no table; it would block the
  launch. Skip.
- **Kill/restart the live session on flip** — epic already rejected this for personas;
  same for launch.

## Acceptance criteria

- [x] With host `agentic.runner` unset/`claude` and no env pin, conductor and steward
      argv still start with `claude` and keep today's flags, env, and price check.
- [x] `NW_AGENTIC_RUNNER=cursor` or host `agentic.runner: cursor` makes
      `conductor-launch` argv start with `cursor-agent` and include `--force --trust
      --workspace --model <mapped>` and the skill prompt; no `--permission-mode`,
      `--allowedTools`, `--effort`, `--autocompact`.
- [x] Steward uses `resolve_runner(cfg, project=P)` (project_runners / project.yaml /
      host). Conductor ignores project overlays.
- [x] `claude-sonnet-5-5` maps to the YAML sonnet default (`grok-4.7-high` builtin).
- [x] Fresh Cursor `run` calls `create-chat`, registers that UUID, execs `--resume` with
      it. `--resume` reuses a Cursor launch record and does not require a Claude
      transcript. Resuming a Claude launch as Cursor is refused.
- [x] `check_price` does not refuse a Cursor launch. Shipping does not write
      `agentic.runner: cursor` into user config.
- [x] A host flip does not SIGTERM a live session; the next launch picks up the new
      backend (existing freshness/stale mail still applies because `launch_hash` changes).
- [x] Claude launch tests stay green.

## Test plan

- **Automated tests:** `tests/test_cursor_launch.py` plus existing
  `tests/test_agentic_launch.py` on the Claude default: Cursor vs Claude argv; steward
  project resolution; model map for `claude-sonnet-5-5`; policy footer present; no
  Claude flags; `create-chat` mocked in `run`; resume without transcript; resume from a
  Claude record refused; `missing_prices` empty when host is cursor; `cursor_needed`
  true for `project_runners`.
- **Manual verification:** with logged-in `cursor-agent`,
  `NW_AGENTIC_RUNNER=cursor nw queue conductor-launch --dry-run` shows `cursor-agent`
  argv and `grok-4.7-high`. Confirm user config default is still `claude`.
  Executed 2026-11-05: dry-run printed `runner: cursor`, `--model grok-4.7-high`,
  `--force --trust --approve-mcps --workspace`, skill prompt + policy footer.
  Compiled default remains `claude`. Live exec of conductor-launch was not run
  (would replace this session).
- **Regression guard:** `tests/test_agentic_launch.py`; `python3 tools/spec_lint.py`.

## Clock Log

Implementation wall-time is on IDEA-1006 (`nw idea clock-in` / `clock-out`).

## Rollout / migration

No data migration. Existing Claude launch records stay Claude; after a host flip, start
fresh (no `--resume`) to mint a Cursor chat. Opt in the same ways as child 1.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; affected tests pass; manual steps noted.
- [x] No regressions on Claude launch.
- [x] Spec body matches what shipped.
- [x] `docs/LEDGER.md` regenerated; `spec_lint` exits 0.

## Open questions

None. Parent epic pinned resolution, permission mapping, and resume.
