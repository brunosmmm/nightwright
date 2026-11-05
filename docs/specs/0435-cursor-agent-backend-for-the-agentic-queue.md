---
id: SPEC-0435
title: "Cursor-agent backend for the agentic queue"
status: accepted
owner: user
created: 2026-11-05
updated: 2026-11-05
kind: epic
parent: SPEC-0169
tags: [agentic, runner, cursor]
depends_on: [SPEC-0174, SPEC-0237, SPEC-0319]
source_idea: IDEA-1004
---

## Context

Promoted from IDEA-1004. Claude credits are exhausted. The agentic queue has one
real backend: `claude` — persona runs are `claude -p` ([SPEC-0174](./0174-headless-persona-runner-prompt-assembly-and.md)),
conductor and steward sessions are `claude` ([SPEC-0319](./0319-wt-queue-conductor-launch-steward-launch-start.md)).
`select_runner` and `validate_runner_cfg` accept only `claude` or `fake`. Profiles lock
`model` to aliases `opus` / `sonnet` / `haiku` or a `claude-*` id
([SPEC-0237](./0237-workflow-profiles-as-yaml-files-builtin-user-project.md)).

This machine already has `cursor-agent` at `~/.local/bin/cursor-agent` (2026.11.05-86047df;
`agent` is an alias). Headless print mode (`-p` / `--print`) can do the same one-shot job
as `claude -p`: write a prompt, wait, read a JSON envelope. It does not offer
`--json-schema`, `--max-budget-usd`, Claude `--permission-mode`, `--allowedTools`, or a
separate `--effort` (effort is encoded in the model id). JSON success is
`type=result` with `result` (assistant text) and `session_id`; failures are stderr plus a
non-zero exit, with no JSON. Stream-json events are not Claude's
([SPEC-0362](./0362-stream-json-capture-of-persona-runs-and-nw.md)).

Human decisions 2026-11-05: the backend is **opt-in** (default stays `claude`); auth is the
existing `cursor-agent login` session, not `CURSOR_API_KEY`. Same day: in-flight credit
flips and project / work-item scope (see Runner resolution); mixing backends in one queue
process is required for that.

## Goals / Non-goals

**Goals**
- An opt-in Cursor backend that runs persona jobs through headless `cursor-agent -p`
  with the same `PersonaRunner` contract (one-shot prompt on stdin, wait, timeout,
  detach/adopt).
- Backend-relative model maps so existing profile aliases (`opus` / `sonnet` / `haiku`)
  resolve to Cursor-available non-Claude model ids; explicit Cursor ids valid when the
  resolved runner is `cursor`.
- Conductor and steward launch through `cursor-agent` when *that session's* resolved
  runner is `cursor`.
- Doctor / `nw workers check` probe `cursor-agent` version and login status (not a paid
  `-p`) when any active campaign or the host default resolves to `cursor`.
- Default remains `claude`. Shipping does not write `agentic.runner: cursor` into user
  config.
- Per-spawn resolution: host default, per-project, and per-campaign pin, with a live
  in-flight flip that does not kill running persona processes.

**Non-goals**
- Replacing Claude as the compiled-in default backend.
- Cursor `--json-schema` (the CLI does not have it).
- A Cursor USD price table. Missing `total_cost_usd` uses the existing estimate path
  (charge `max_budget_usd`, `cost_estimated: true`).
- Live herdr translation of Cursor `tool_call` events. Child A still captures
  `stream.jsonl` and collects the last `type: result` event; pane-level tool viz of
  Cursor events is a later idea.
- The Python Cursor SDK (`cursor-sdk`). This epic wraps the CLI.
- Changing FakeRunner, verdict schemas, or persona prompt templates except a Cursor-only
  footer that asks for JSON matching the existing schema.

## Decision

Add a second real `PersonaRunner` implementation, `CursorRunner`. Keep the protocol,
run-dir files, detach/adopt wrapper, and queue. Profiles keep today's aliases; a builtin
`(alias, effort) → cursor model id` table resolves them at argv-build time. Verdicts
come from parsing the assistant `result` text as JSON against the existing schema
(`parse_result` already falls back that way). Auth is a logged-in `cursor-agent` session.

**Amendment 2026-11-05:** the first cut said one runner setting covered the whole
process. That blocked credit-exhaustion flips and project / work-item scope. Selection
is **per spawn** via `resolve_runner(...)` (below). A queue process may run Claude
personas on one campaign and Cursor personas on another; the conductor may stay Claude
while a project's steward or campaigns use Cursor.

Ship as two children: persona runner plus maps and resolution first (campaigns can run
and flip), then launch (permission allow/deny lists have no Cursor equivalent).

## Architecture / cross-cutting design

### Invariants

- Default `agentic.runner` remains `claude`. Unknown values still error.
- Persona I/O stays one-shot: `prompt.md` on the child's stdin, wrapper stdin `DEVNULL`,
  no second prompt, timeout via SIGTERM then KILL. Same as [SPEC-0174](./0174-headless-persona-runner-prompt-assembly-and.md)
  / [SPEC-0225](./0225-detached-persona-runs-adopted-across-runner-restarts.md).
- Python still decides transitions; the agent still returns one structured verdict.
- FakeRunner and offline tests are unchanged.
- Builtin profile YAML does not gain Cursor-specific model ids; aliases stay.

### Runner resolution (per spawn)

The `(alias, effort) → Cursor id` table ships as YAML (`cursor_map.yaml`) with
the same overlay stack as profiles; a persona may set `cursor_model` in profile
YAML. Child 1 owns that. The default ids below are the builtin YAML contents.

`select_runner(cfg, runner=None)` still constructs the implementation.
`resolve_runner(cfg, *, campaign=None, project=None)` picks the kind **for this spawn**:

1. `NW_AGENTIC_RUNNER` if set (`claude` / `cursor` / `fake`). Highest. Tests and
   emergency host pin. `fake` is valid only here (never from project yaml or a campaign).
2. Else `campaign.state.runner` if set (`claude` or `cursor`). Work-item pin for that
   campaign's lifecycle.
3. Else user config `agentic.project_runners[project]` if set. Local overlay; not
   committed. Lets one project move to Cursor without editing that repo.
4. Else `.nw/project.yaml` `agents.runner` if set. Committed project default.
5. Else user config `agentic.runner`.
6. Else `claude`.

Unknown values error, same as today. `validate_runner_cfg` accepts `claude`, `fake`,
`cursor`.

**Live vs pinned.** Admit copies `--runner` onto `state.runner` only when the flag is
passed (`nw queue add SPEC --runner cursor`, same shape as `--profile`). Omitting it
leaves `state.runner` unset, so later host/project flips apply to the *next* spawn.

**No new command for host or project flips.** `agentic.runner` in user config already
reloads before each tick ([SPEC-0226](./0226-runner-reloads-user-and-project-config-on-change.md)).
`.nw/project.yaml` is already re-read every tick. `agentic.project_runners` lives in the
same user file. Do not add `nw queue runner`: it collides with `nw queue run` /
`runner-log` (the queue process), and it would only write a file the operator can edit.

**Changing an already-working campaign.** One mutator, same family as `nw queue after` /
`prioritize` (dry-run unless `--yes`):

```
nw queue backend CAMPAIGN cursor|claude|default --yes
```

It writes or clears `state.runner` on any non-terminal campaign, including IMPLEMENTING /
GATING (not QUEUED/PARKED-only like `prioritize`). `default` deletes the pin so the item
follows host/project again. It does **not** park, resume, or SIGTERM the live persona:
that spawn finishes on the backend it started with; the next spawn uses the new
resolution. Journal `runner_changed` `{from, to, campaign}`. A pin is an opt-out of live
host flips; to move a pinned item you name it. No `--active` bulk form.

**Launch (child 2).** Conductor sessions resolve with no campaign (host default, step 5).
Steward sessions resolve with `project=<P>` (steps 3–5). Editing host/project config does
not restart a live conductor/steward; the next `conductor-launch` / `steward-launch` uses
the new value. Restart those sessions after a credit flip if the session itself must move.

**CursorRunner** is detachable and uses the same `runproc` wrapper. `runproc.with_claude`
gains a sibling (or a generalized `resolve_agent_binary`) so `argv[0] == "cursor-agent"`
resolves PATH then `~/.local/bin` the way `claude` does ([SPEC-0389](./0389-persona-claude-resolution.md)).
Missing binary is a job error, not a verdict failure.

### Cursor argv (persona)

`build_cursor_argv(req)` (name as shipped) roughly:

```
cursor-agent -p --output-format stream-json --model <mapped>
  --trust --workspace <cwd> [--add-dir …]
  [--force]              # write personas (not read_only)
  [--mode ask]           # read_only personas
```

No `--json-schema`, `--max-budget-usd`, `--permission-mode`, `--permission-prompts`,
`--no-session-persistence`, `--allowedTools`, `--restricted`, `--effort`,
`--append-system-prompt`. The prompt file concatenates `req.prompt`, `req.system_append`,
and a fixed footer: reply with a single JSON object matching `req.json_schema` (the schema
is inlined). `--force` maps from write personas (`bypassPermissions` / not `read_only`).
`--mode ask` maps from `read_only`. `--trust` is always on for headless.

### Model map

**Amendment (child 1):** the table is YAML (`cursor_map.yaml`), overlaid like
profiles (builtin → `<config_dir>/cursor_map.yaml` → `agentic.cursor` →
`<project>/.nw/cursor_map.yaml`). A profile persona may set `cursor_model`.
`agentic.cursor.alias_map` remains a valid flat overlay. Defaults, non-Claude
because the motive is credits:

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

Unknown effort uses the alias's `high` row except haiku (medium). A profile `model` that
is already a Cursor id (not an alias and not `claude-*`) is passed through only when the
runner is `cursor`; Claude runner validation stays today's `MODEL_ALIASES` / `MODEL_RE`.
When the runner is `cursor`, aliases remain valid and `claude-*` ids are rejected (they
would burn the exhausted credits). Launch `agentic.conductor.launch.model` goes through
the same resolver (`claude-sonnet-5-5` is treated as the sonnet/high tier).

### Verdicts and cost

Cursor JSON has no `structured_output`. `parse_result` already does
`verdict = structured_output or json.loads(result)`. Child A relies on that plus
`_conforms`. Unparseable `result` is `verdict_ok=False`, `is_error=True`, same as today.

Cursor JSON has no `total_cost_usd`. Leave `cost_usd=None`. The queue already charges
`max_budget_usd` and sets `cost_estimated: true` ([queue.py](../../src/nightwright/agentic/queue.py)
`_record_run`). Campaign caps stay pessimistic. No new price table in this epic.

### Stream capture

Keep `--output-format stream-json` and `stream.jsonl`. Collect via the last
`type: result` event (Cursor's result shape is compatible enough: `is_error`, `result`,
`session_id`). Live herdr mapping of Cursor `tool_call` payloads is out of this epic.

### Auth and doctor

No `CURSOR_API_KEY` in the first ship. `nw doctor` / `nw workers check` when runner is
`cursor`: `cursor-agent --version` and `cursor-agent status` (or `whoami`) must show a
logged-in session. A failed login is a check failure, not a persona verdict. Remote
workers use the same resolver and check; a box without a login cannot take cursor
persona jobs.

### Launch (child B)

`build_plan` argv starts with `cursor-agent` instead of `claude` when
`resolve_runner(cfg, project=...)` is `cursor`. Drop Claude-only flags
(`--permission-mode`, `--effort`, `--autocompact`, `--allowedTools` /
`--disallowedTools`, `CLAUDE_CODE_*` env). Map:

- conductor/steward `dontAsk` + allow-lists → `--force --trust` plus prompt text that
  restates the deny rules (no Cursor equivalent of the allow/deny CLI flags)
- `--model` through the alias map
- session resume via `cursor-agent --resume <chatId>` if a launch record exists

Exact flag list is child B's to pin against the installed CLI. Child A must not change
launch.

### Modules

| area | change |
|---|---|
| `runner.py` | `CursorRunner`, `build_cursor_argv`, `select_runner` / `resolve_runner` |
| `runproc.py` | resolve `cursor-agent` like `claude` |
| `cursor_map.yaml` / `cursor_map.py` / `profiles.py` | YAML alias map + optional persona `cursor_model` |
| `project.py` | `agents.runner`: `claude` or `cursor` |
| `queue.py` / `cli_queue.py` | `validate_runner_cfg`; `--runner` on add; `nw queue backend`; `state.runner` pin |
| `personas.py` | apply the YAML map when building the `RunRequest` so `req.model` is the Cursor id that will run. Optional `model_alias` on `request.json` keeps the profile alias |
| `doctor.py` / `worker.py` / `remote_personas.py` | cursor binary + login probe when any active resolution is `cursor` |
| `launch.py` | child B only; uses `resolve_runner` |
| config | `agentic.runner`; `agentic.project_runners`; optional `agentic.cursor.alias_map` |

Child A records the chosen `RunRequest.model` behaviour in its Design (alias vs mapped id
on the request). Epic constraint: ledger/run records must show the **Cursor id that ran**,
not only the alias.

## Alternatives considered

- **Cursor Python SDK** — extra dependency and a different runtime than the installed
  CLI the human already logs into. Lost; wrap `cursor-agent`.
- **Silently default to cursor** — rejected; human asked for opt-in.
- **`--output-format json` without a stream** — simpler collect, but drops
  `stream.jsonl` and the last-result-event path already works. Keep stream-json.
- **Estimate USD from duration** — magical. Reuse the existing budget-as-estimate path.
- **Per-persona Cursor ids in builtin profiles** — forks the profile files per backend.
  Aliases plus a map keep one profile tree.
- **One runner for the whole process** — first Decision; rejected on 2026-11-05 because
  credit exhaustion and per-project / per-campaign scope need per-spawn resolution.
- **Kill in-flight `claude -p` on flip** — loses a verdict already in progress. Finish
  the spawn; switch on the next one.
- **Always pin at admit** — would freeze campaigns onto Claude when the host later
  flips. Default is live resolution; pin only with `--runner` or `nw queue backend`.
- **`nw queue runner` to write host config** — name collides with the queue process
  (`run`, `runner-log`); SPEC-0226 already applies a config edit at the next tick.
- **Require park before changing a working item** — the live persona cannot migrate
  anyway; parking just interrupts the loop. Mutate `state.runner`, let the current spawn
  finish.

## Breakdown / sub-specs

- [ ] Persona CursorRunner and Cursor model maps (project: Meta-Tools) — one-shot cursor-agent -p runner, alias maps, resolve_runner (host / project / campaign pin / in-flight flip), select_runner, profile validation, doctor and worker login check
- [ ] Conductor and steward launch via cursor-agent (project: Meta-Tools) (depends_on child 1) — conductor-launch and steward-launch argv, permission mapping, session registry, resolve_runner for that session

Sequencing: child 1 first so campaigns can run, pin, and flip without Claude. Child 2
needs the binary resolver, model map, and `resolve_runner` from child 1. Nothing in
parallel.

## Acceptance criteria

- [ ] With `agentic.runner` unset or `claude` and no pin, behaviour is today's Claude backend.
- [ ] Host `agentic.runner: cursor` (or `NW_AGENTIC_RUNNER=cursor`) runs unpinned personas
      via `cursor-agent -p`; launch uses `resolve_runner` once child 2 is done.
- [ ] `.nw/project.yaml` `agents.runner` and user `agentic.project_runners[project]`
      select the backend for that project's unpinned campaigns (and its steward).
- [ ] `nw queue add --runner cursor` pins that campaign for its lifecycle; omitting
      `--runner` stays live.
- [ ] Changing the host default in user config applies to the next spawn of unpinned
      campaigns and does not kill an in-flight persona.
- [ ] `nw queue backend CAMPAIGN cursor|claude|default --yes` sets or clears the pin on a
      non-terminal campaign (including in-progress) without parking; the current persona
      spawn is left running.
- [ ] Profile aliases `opus` / `sonnet` / `haiku` resolve to the default Cursor map
      (overridable). Run records store the Cursor model id that ran.
- [ ] Write personas pass `--force --trust`; read-only personas pass `--mode ask --trust`.
- [ ] A structured verdict is accepted when the assistant `result` text is JSON that
      conforms to the persona schema.
- [ ] Missing `total_cost_usd` charges `max_budget_usd` as an estimate (`cost_estimated`).
- [ ] Doctor / workers check fail closed when `cursor-agent` is missing or not logged in
      and any active resolution is `cursor`.
- [ ] Shipping this epic does not set `agentic.runner: cursor` in user config.
- [ ] FakeRunner and Claude paths keep their existing tests green.

## Test plan

Unit-level tests live on the children. Epic integration, once both children are `done`:

- **Automated tests:** `tests/test_agentic_runner.py` (and child files) assert
  `select_runner` / `validate_runner_cfg` accept `cursor`; `build_cursor_argv` for a write
  and a read-only request; alias map including effort fallbacks and config override;
  `parse_result` on a Cursor success envelope whose `result` is a conforming JSON object,
  and on a non-zero exit with stderr only; Claude argv and FakeRunner unchanged.
  `resolve_runner` precedence (env, pin, project_runners, project.yaml, host, default);
  live vs pinned; a host flip does not attach a kill to an in-flight hook. Launch
  tests assert `cursor-agent` argv when that session resolves to cursor.
- **Manual verification:** with a logged-in `cursor-agent`,
  `NW_AGENTIC_RUNNER=cursor nw campaign prompt <cid> implementer` (or equivalent) shows the
  JSON-schema footer; one dry persona run against a scratch worktree returns a conforming
  verdict without using Claude. Confirm shipping did not rewrite user config. Flip the
  host default and confirm the next spawn (not the live one) uses Cursor.
- **Regression guard:** existing `tests/test_agentic_runner.py`, `tests/test_agentic_profiles.py`,
  `tests/test_agentic_launch.py` stay green on the Claude default.

## Rollout / sequencing

1. Accept this epic; `nw spec seed-children SPEC-0435`.
2. Child 1 (persona runner + maps + resolve_runner) to `accepted` then implement in a
   worktree. After it ships, unpinned campaigns follow the host/project default; `--runner`
   pins a work item. Conductor/steward still launch Claude until child 2.
3. Child 2 (launch) after child 1 is `done`.
4. Do not flip the compiled default. Opt in via config, project yaml, `--runner`, or
   `nw queue backend`.

Partial delivery: child 1 alone is useful (queue personas on Cursor, launch still Claude).
Child 2 without child 1 is invalid (`depends_on child 1`).

## Definition of done

- [ ] All child specs `done` or `superseded` (the linter gates this).
- [ ] Integration test plan executed; affected tests pass.
- [ ] Acceptance criteria met; epic body reflects what shipped.
- [ ] `docs/LEDGER.md` regenerated.

## Open questions

None. Cost uses the existing estimate path. Stream capture keeps `stream.jsonl` without a
herdr adapter in this epic. Opt-in, login auth, and per-spawn resolution (host / project /
campaign pin / in-flight flip) are decided.
