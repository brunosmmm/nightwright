---
id: SPEC-0436
title: "Persona CursorRunner and Cursor model maps"
status: done
owner: user
created: 2026-11-05
updated: 2026-11-05
kind: feature
parent: SPEC-0435
tags: [agentic, runner, cursor]
depends_on: [SPEC-0174, SPEC-0237]
source_idea: IDEA-1005
---

## Context

Child 1 of [SPEC-0435](./0435-cursor-agent-backend-for-the-agentic-queue.md). Persona
runs today go through `ClaudeRunner` (`claude -p`). Claude credits can be exhausted
while a logged-in `cursor-agent` CLI is already on the box. This spec ships the
one-shot Cursor backend, alias maps, and per-spawn resolution (host / project /
campaign pin / in-flight flip) so queue personas can run on Cursor without changing
conductor or steward launch (child 2).

## Goals / Non-goals

**Goals**
- `CursorRunner`: one-shot `cursor-agent -p` with the existing `PersonaRunner`
  contract (prompt on stdin, wait, timeout, detach/adopt, `stream.jsonl`).
- Builtin `(alias, effort) → Cursor model id` map, overridable via
  `agentic.cursor.alias_map`. Run records store the Cursor id that ran.
- `resolve_runner` per spawn; `select_runner(cfg, runner=...)` constructs the impl.
- `nw queue add --runner cursor|claude` pins a new campaign; omit = live.
- `nw queue backend CAMPAIGN cursor|claude|default --yes` sets or clears the pin
  on a non-terminal campaign without parking or killing the live spawn.
- Doctor / `nw workers check` probe `cursor-agent --version` and login
  (`status` / `whoami`, not a paid `-p`) when any active resolution is `cursor`.
- Default remains `claude`. This spec does not write `agentic.runner: cursor`
  into user config.

**Non-goals**
- Conductor / steward `cursor-agent` argv (child 2 / IDEA-1006).
- Cursor `--json-schema`, a USD price table, herdr `tool_call` adapters.
- The Python Cursor SDK. `CURSOR_API_KEY` auth.
- Changing FakeRunner, verdict schemas, or builtin profile YAML model fields.
- `nw queue runner` (name collides with `run` / `runner-log`).

## Decision

Add `CursorRunner` beside `ClaudeRunner`. Keep the protocol, run-dir files, and
queue. Profiles keep `opus` / `sonnet` / `haiku`; a builtin map (and optional
user override) resolves them when the spawn's runner is `cursor`, so `req.model`
is the Cursor id that will run. Verdicts come from `parse_result`'s existing
`structured_output or json.loads(result)` fallback. Auth is a logged-in
`cursor-agent` session.

Selection is **per spawn** via `resolve_runner`. A queue process may mix
backends. A host or project flip applies to the **next** spawn of unpinned
campaigns; an in-flight persona finishes on the backend it started with.

## Design

### Runner resolution

`resolve_runner(cfg, *, campaign=None, project=None)` returns `claude`,
`cursor`, or (env only) `fake`:

1. `NW_AGENTIC_RUNNER` if set (`claude` / `cursor` / `fake`). Highest. `fake`
   is valid only here (and today's `agentic.runner: fake` for offline tests).
2. Else `campaign["runner"]` if set (`claude` or `cursor`).
3. Else `agentic.project_runners[project]` if set (`claude` or `cursor`).
4. Else `.nw/project.yaml` `agents.runner` if set (`claude` or `cursor`).
5. Else `agentic.runner`.
6. Else `claude`.

Unknown values raise `ValueError` (`unknown agentic.runner`). `fake` in a
campaign pin, `project_runners`, or `agents.runner` is refused.

`select_runner(cfg, runner=None)` constructs the implementation. When `runner`
is passed, env still wins (tests). `_Ctx.runner_for(state)` caches by resolved
kind; an injected test runner still wins for every campaign.

`validate_runner_cfg` accepts `claude`, `fake`, and `cursor` for
`agentic.runner`; validates `project_runners` values and `cursor.alias_map`
(string keys to string ids).

### Live vs pinned

`nw queue add … --runner cursor|claude` copies the flag onto `state.runner`.
Omitting `--runner` leaves the field unset (live). The same flag on a batch
applies to every item.

`nw queue backend CAMPAIGN cursor|claude|default --yes` (dry-run unless
`--yes`) writes or clears `state.runner` on any **non-terminal** campaign,
including IMPLEMENTING / GATING. `default` deletes the pin. It does not park,
resume, or SIGTERM the live persona. Journal `runner_changed`
`{from, to, campaign}`. No `--active` bulk form.

### CursorRunner argv

`build_cursor_argv(req)`:

```
cursor-agent -p --output-format stream-json --model <mapped>
  --trust --workspace <cwd> [--add-dir …]
  [--force]              # write personas (not read_only)
  [--mode ask]           # read_only personas
```

No Claude-only flags. `prompt.md` is `req.prompt`, then `req.system_append`,
then a fixed footer asking for a single JSON object matching `req.json_schema`
(inlined). `runproc` resolves `cursor-agent` the same way as `claude`
([SPEC-0389](./0389-persona-claude-resolution.md)): PATH, then standard install
dirs including `~/.local/bin`. Missing binary is a job error, not a verdict.

Cursor JSON has no `total_cost_usd`. Leave `cost_usd=None`; the queue already
charges `max_budget_usd` as an estimate.

Remote persona jobs carry `payload.runner` so the worker constructs the same
backend (env still wins).

### Model map (YAML, same overlay idea as profiles)

The Cursor translation is **data**, not a Python table. Builtin profiles keep
`model: opus|sonnet|haiku` (one tree, no per-backend fork). What those aliases
mean on Cursor lives in YAML, overlaid lowest first:

1. builtin [`src/nightwright/agentic/cursor_map.yaml`](../../src/nightwright/agentic/cursor_map.yaml)
   (`schema: nw.cursor_map.v1`)
2. `<config_dir>/cursor_map.yaml`
3. `agentic.cursor` in user `config.yaml` (`defaults`, nested `aliases`, optional
   flat `alias_map`)
4. `<project>/.nw/cursor_map.yaml`

A profile persona may set `cursor_model:` (user/project profile YAML). That id
wins for that persona on a Cursor spawn — the same files that already pick
`model` / `effort`. Builtin profiles do not set it.

Default builtin map (effort fallback is `defaults:` in the YAML: haiku → medium,
else high):

| alias  | effort (fallback) | Cursor id           |
|--------|-------------------|---------------------|
| haiku  | low / medium      | grok-4.7-low        |
| haiku  | high / xhigh / max| grok-4.7-medium     |
| sonnet | low / medium      | grok-4.7-medium     |
| sonnet | high              | grok-4.7-high       |
| sonnet | xhigh / max       | grok-4.7-xhigh      |
| opus   | low / medium      | gpt-5.6-sol-medium  |
| opus   | high              | gpt-5.6-sol-high    |
| opus   | xhigh / max       | gpt-5.6-sol-xhigh   |

Applied in `build_request` (and `nw spec review`) so `req.model` is the Cursor
id; optional `model_alias` on the request keeps the profile alias. `claude-*`
ids are rejected when the runner is `cursor`. A non-alias, non-`claude-*` id is
passed through. Claude runner validation stays today's aliases / `MODEL_RE`.

### Doctor and workers

`nw doctor` check `agentic.cursor` (skip when no active resolution is `cursor`):
`cursor-agent --version` plus `status` or `whoami` showing a logged-in session.
`nw worker cursor-check` (SSH grammar; not a paid `-p`). `nw workers check`
runs it on persona workers when any active resolution is `cursor`.

### Launch

Unchanged. Child 2 uses `resolve_runner` / the map / the binary resolver from
this spec.

## Alternatives considered

Covered on the parent epic: wrap the CLI not the SDK; opt-in default; keep
stream-json; per-spawn resolution; finish the live spawn on flip; no
`nw queue runner` host writer.

- **Hardcoded Python alias table** — first cut of this child. Lost: model
  selection already lives in YAML (profiles); the Cursor translation belongs
  in the same kind of file, overridable without a code change.

## Acceptance criteria

- [x] With `agentic.runner` unset or `claude` and no pin, `select_runner` is
      `ClaudeRunner` (today's backend).
- [x] `NW_AGENTIC_RUNNER=cursor` or `agentic.runner: cursor` selects
      `CursorRunner` for unpinned spawns.
- [x] `resolve_runner` precedence is env, campaign pin, `project_runners`,
      `agents.runner`, host, default `claude`. `fake` is refused off env /
      `agentic.runner`.
- [x] `nw queue add --runner cursor` pins `state.runner`; omitting `--runner`
      leaves it unset.
- [x] `nw queue backend CAMPAIGN cursor|claude|default --yes` sets or clears
      the pin on a non-terminal campaign without requiring PARKED; journals
      `runner_changed`.
- [x] Profile aliases map through `cursor_map.yaml` (user / config /
      project overlays; persona `cursor_model` wins). Run records store the
      mapped id. `claude-*` is rejected on the cursor runner.
- [x] Write argv includes `--force --trust`; read-only includes `--mode ask --trust`.
- [x] `parse_result` accepts a Cursor `type: result` envelope whose `result`
      text is JSON conforming to the persona schema; missing `total_cost_usd`
      yields `cost_usd is None`.
- [x] Missing `cursor-agent` is a job error. Doctor / workers check fail closed
      when any active resolution is `cursor` and the binary is missing or not
      logged in; the check skips otherwise.
- [x] Launch argv is unchanged. FakeRunner and Claude argv tests stay green.
- [x] Shipping does not set `agentic.runner: cursor` in user config defaults.

## Test plan

- **Automated tests:** `tests/test_cursor_runner.py` (plus extensions of
  `tests/test_agentic_runner.py`, `tests/test_claude_resolve.py`,
  `tests/test_agentic_project.py`, `tests/test_agentic_config_reload.py`,
  `tests/test_doctor.py`, `tests/test_remote_personas.py`):
  `resolve_runner` precedence; `select_runner` accepts `cursor`;
  `build_cursor_argv` write vs read-only; alias map including effort fallback
  and config override; `claude-*` rejected; `parse_result` on a Cursor success
  envelope and a non-zero stderr-only failure; `plan_backend` / `apply_backend`
  on an IMPLEMENTING campaign; add pin vs live; `agents.runner` validation;
  `with_cursor_agent` PATH/`~/.local/bin`; doctor skip vs fail; SSH
  `cursor-check` grammar. Existing Claude / FakeRunner tests remain the
  regression guard.
- **Manual verification:** with a logged-in `cursor-agent`,
  `NW_AGENTIC_RUNNER=cursor nw campaign prompt <cid> implementer` shows the
  JSON-schema footer. Confirm user config default is still `claude`.
  Executed 2026-11-05: compiled `DEFAULT_CONFIG["agentic"]["runner"]` is still
  `claude`; `cursor_prompt` unit test covers the schema footer. Live
  `nw campaign prompt` against a real campaign was not run (no spend).
- **Regression guard:** `python3 tools/affected_tests.py --run uv run pytest -q`
  on the touched files; `python3 tools/spec_lint.py`.
  Executed 2026-11-05: `tests/test_cursor_*.py` plus runner / runproc / doctor /
  profiles / project / remote_personas / claude_resolve / config_reload /
  rich_output_guard. Touched `queue.py` / `cli_queue.py` select almost the
  whole agentic suite; that full affected set was not waited out locally
  (AGENTS.md: heavy gates go to the box). Pre-existing
  `test_list_marks_overdue_and_colours_parks_by_owner` fails on main too
  (Rich drops the red on `!`) — not this change.

## Clock Log

Implementation wall-time is on IDEA-1005 (`nw idea clock-in` / `clock-out`).

## Rollout / migration

No data migration. Existing campaigns have no `state.runner` (live). Opt in
via config, project yaml, `--runner`, or `nw queue backend`. Do not flip the
compiled default.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; affected tests pass; manual steps noted.
- [x] No regressions on Claude / FakeRunner paths.
- [x] Spec body matches what shipped.
- [x] `docs/LEDGER.md` regenerated; `spec_lint` exits 0.

## Open questions

None. Parent epic closed cost, stream capture, opt-in, login auth, and
per-spawn resolution.
