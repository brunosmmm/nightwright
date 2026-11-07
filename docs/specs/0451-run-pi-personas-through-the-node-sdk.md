---
id: SPEC-0451
title: Run Pi personas through the Node SDK
status: proposed
owner: user
created: 2026-11-07
kind: feature
tags: [agentic, runner, pi]
depends_on: [SPEC-0442, SPEC-0436]
source_idea: IDEA-1023
---

## Context

Persona runs that resolve to Cursor go through a supervised child,
`python -m nightwright.agentic.cursor_exec`, as decided in
[SPEC-0442](./0442-cursor-sdk-persona-runs.md). `runproc` still owns timeout,
kill, and `stream.jsonl`. `extract_result` and `parse_result` still read the
last `type: result` line. Claude stays `claude -p`. Conductor and steward stay
on their interactive CLIs.

Pi (`@earendil-works/pi-coding-agent`, <https://pi.dev>) has no Python SDK. The
API is `createAgentSession()` in Node or Bun. Print mode returns only the final
text. `pi --mode json` streams one run as JSONL framed on LF, and it still
loads discovered extensions, and a failed agent turn does not by itself exit
nonzero. RPC is a long-lived controller. A persona run is one shot. The queue
process must not import or call Pi.

## Goals / Non-goals

**Goals**

- When a persona spawn resolves to `pi`, the detached wrapper starts a Node
  child that runs one in-memory Pi session, one prompt, then `dispose`.
- `runproc` supervises that child the way it supervises `cursor_exec`: timeout,
  process group, `stream.jsonl`, adopt. Stdout is the event stream. The child
  writes a final `type: result` line so `extract_result` and `parse_result`
  stay the collectors. Records split on `\n` only. `cwd` is the campaign
  worktree. `SessionManager.inMemory()` writes no session tree into the repo.
- Read-only personas get the tools `read`, `grep`, `find`, and `ls`. Writers
  get Pi's defaults `read`, `bash`, `edit`, and `write`. The allowlist is the
  boundary. There is no permission popup.
- Discovered extensions and skills stay off, so `~/.pi` does not enter the
  run. Project `AGENTS.md` from the worktree still loads.
- Persona instructions go in the system prompt. The schema footer stays in the
  user message. The verdict is parsed from the final assistant text. No JSON
  schema is sent to the model.
- A missing `pi` package and a missing credential for the selected provider
  are job errors, not a hang. Credentials are per provider. The doctor check
  is: the package resolves, and the provider for the mapped model has a key.
- The queue model id maps to a Pi `provider` / `id` plus a thinking level
  through a map parallel to `cursor_map.yaml`. A Cursor model slug is not a
  Pi model.
- Assistant usage includes token counts and `cost.total` in USD. When that
  cost is present and the model has a configured price, store it as
  `total_cost_usd`. Missing usage keeps the existing budget estimate
  (`cost_usd` null, the queue charges the cap). A reported `0` is a free run
  only after a model with no price is distinguished from a real zero.

**Non-goals**

- RPC mode, print mode, and `pi --mode json` as the persona child.
- Replacing the conductor or the steward, or adding `pi` to their `--runner`
  choices.
- A Python port of Pi, and importing or calling Pi from the queue process.
- Loading user or project extensions, skills, prompt templates, or themes
  into a persona.
- Changing `parse_result`, Claude's argv, or `cursor_exec`.

## Decision

`PiRunner` is a `ClaudeRunner` subclass in the same slot as `CursorRunner`.
Its argv is `node` plus the package script `pi_exec.mjs` and the run dir.
`runproc.with_agent` leaves a leading `node` untouched, so timeout, kill, and
`stream.jsonl` stay. The child dynamic-imports `@earendil-works/pi-coding-agent`,
opens one `SessionManager.inMemory(cwd)` session, calls `prompt` once, writes
the result line, and `dispose`s. Startup failures and a failed agent turn
write `is_error: true` and exit 1. The Python process never imports the
package; doctor and the worker preflight run the script's `--probe`.

Profile aliases stay `opus` / `sonnet` / `haiku`. A `pi_map` resolves
`(alias, effort)` to `{provider, id, thinking}`. `req.model` stored on the run
is `provider/id`. Thinking is a separate field. Persona instructions are
`appendSystemPrompt`. The user message is the prompt plus the existing schema
footer. Dollar cost is the sum of assistant `usage.cost.total` when every
message has that number and the model is priced; otherwise `total_cost_usd`
is null and the queue charges `max_budget_usd`.

## Design

### Runner kind

`pi` joins `LIVE_RUNNERS`. `resolve_runner`, `select_runner`,
`validate_runner_cfg`, `agentic.project_runners`, and `.nw/project.yaml`
`agents.runner` accept it beside `claude` and `cursor`. `fake` stays
env-only. `select_runner` returns `PiRunner` for `pi`.

`nw queue add --runner` and `nw queue backend` accept `pi` (and `default`
still clears a pin). Conductor and steward launch in `cli_launch.py` stay
`claude` | `cursor`.

`pi_needed` mirrors `cursor_needed`. Doctor check id `agentic.pi` skips when
no campaign resolves to `pi`.

`PiRunner` subclasses `ClaudeRunner`, so detach and adopt stay. The remote
persona preflight that treats a `ClaudeRunner` as "need the `claude` binary"
excludes `PiRunner` the same way it excludes `CursorRunner`. A `PiRunner` job
calls `ensure_pi_persona` instead. `nw workers check` runs that probe when
`pi_needed`, beside the existing Cursor probe.

### Model map

`src/nightwright/agentic/pi_map.py` and builtin `pi_map.yaml`
(`schema: nw.pi_map.v1`) follow the `cursor_map` overlay order, lowest first:

1. builtin `pi_map.yaml`
2. `<config_dir>/pi_map.yaml`
3. `agentic.pi` in config.yaml
4. `<project>/.nw/pi_map.yaml`

Keys are `schema`, `defaults`, and `aliases`. There is no flat `alias_map`.
`defaults` maps an alias to an effort in the profile effort set, used when
the persona effort is missing. `aliases.<alias>.<effort>` is an object with
the three strings `provider`, `id`, and `thinking`. A string value is invalid.
`thinking` is one of `off`, `minimal`, `low`, `medium`, `high`, `xhigh`,
`max`. `provider` matches `^[a-z0-9][a-z0-9_-]*$`. `id` is non-empty and has
no whitespace. Each layer is validated before merge; an effort object in a
file carries all three fields. `validate_runner_cfg` loads the map the way it
loads `agentic.cursor`.

`map_pi_model` returns `{provider, id, thinking, model}` where `model` is
`provider + "/" + id`. Unknown alias or effort with no row raises
`ValueError` (`no Pi mapping for …`). A model string that is not an alias is
refused, including a Cursor slug such as `grok-4.7-high`.

Builtin rows use one id per alias and set `thinking` to the effort word.
Missing effort uses `defaults` (`haiku: medium`, `sonnet: high`, `opus: high`):

| alias | provider | id | thinking |
|---|---|---|---|
| haiku | anthropic | claude-haiku-4-5 | the effort word |
| sonnet | anthropic | claude-sonnet-4-6 | the effort word |
| opus | anthropic | claude-opus-4-7 | the effort word |

`mapped_model` still returns `(model, model_alias)`. For `pi`, `model` is
`provider/id` and `model_alias` is the profile alias. The RunRequest builders
in `personas.py` and `spec_review.py` also store `pi_provider`, `pi_id`, and
`pi_thinking` from the same row. Mapping happens once, on the machine that
builds the request. A remote job copies those three fields;
`request_from_job` restores them. The worker does not apply its own map.

`_request_doc` writes `pi_provider`, `pi_id`, `pi_thinking`, and
`system_append` when they are set, and omits them for Claude and Cursor
requests. `model` on the run record is `provider/id`.

### Child

`PiRunner._launch_argv` returns:

```text
node <package>/nightwright/agentic/pi_exec.mjs <run_dir>
```

The script path is the file beside `runner.py`. No API key is an argument or
a `launch.json` field. `launch.json` `cwd` is `req.cwd` (the campaign
worktree). `add_dirs` today is that same worktree; the child does not grow a
second workspace. `--read-only` is not required: `request.json` `read_only`
selects the tool list.

`pi_exec.mjs` exports `execute(runDir, deps)`. `main` dynamic-imports
`@earendil-works/pi-coding-agent` and calls `execute`. A thrown import writes
the error result and exits 1. `--probe` (below) never calls `prompt`.

`execute` reads `request.json` and `prompt.md`. It builds:

- `SessionManager.inMemory(cwd)` with `cwd` from `request.json`
- `SettingsManager.inMemory()` so `~/.pi` settings do not change tools,
  model, or thinking
- `DefaultResourceLoader` with `noExtensions`, `noSkills`,
  `noPromptTemplates`, and `noThemes` true, `noContextFiles` false, and
  `appendSystemPrompt` set to `system_append` when that string is non-empty.
  Context-file discovery from `cwd` still loads the worktree `AGENTS.md`
- `tools`: `["read", "grep", "find", "ls"]` when `read_only` is true, otherwise
  `["read", "bash", "edit", "write"]`. The list is passed explicitly. No
  approval callback reads stdin
- `model` resolved as that `provider` / `id` from the package registry, and
  `thinkingLevel` from `pi_thinking`. Pi clamps the level to what the model
  supports

Credential resolution is Pi's own for that provider: the provider's
environment variable, then a stored API key or OAuth credential. The child
checks it before `prompt`. A miss writes `is_error` and exits 1. Unknown
provider or unknown model id is the same startup error. The key text is
scrubbed from the result line and from stderr.

`prompt.md` is the user message: `req.prompt`, then the schema footer already
used by `cursor_prompt` (`CURSOR_SCHEMA_FOOTER` with `req.json_schema`).
`system_append` is not copied into that file. Nothing on the session call
sends a JSON schema. `prompt` is called once. `finally` calls `dispose`.
SIGTERM best-effort calls `session.abort()`; `runproc` still SIGKILLs the
group after the grace period.

Stdout is one JSON object per `\n`. The child maps session events into the
shapes `Renderer.feed` already prints: an `assistant` line with `text` and
`tool_use` blocks, and a `user` line with `tool_result` blocks. Thinking text
is not written. A tool argument that contains U+2028 stays inside that one
JSON line.

The last line is:

```json
{"type":"result","subtype":"success","is_error":false,
 "result":"<final assistant text>","session_id":"<in-memory session id>",
 "usage":{"input_tokens":0,"output_tokens":0,"cache_read_tokens":0,
          "cache_write_tokens":0,"cache_read_input_tokens":0,
          "cache_creation_input_tokens":0},"total_cost_usd":null}
```

`usage` sums assistant messages. Pi's `input`, `output`, `cacheRead`, and
`cacheWrite` become `input_tokens`, `output_tokens`, `cache_read_tokens`, and
`cache_write_tokens`. The Claude aliases `cache_read_input_tokens` and
`cache_creation_input_tokens` are copies of the cache counts, as in
`cursor_exec.usage_dict`. `session_id` is the in-memory session id. `result`
is the final assistant text. `parse_result` reads the verdict from that text.

An agent failure (a thrown `prompt`, or a final assistant message with an
error) sets `subtype` to `error`, `is_error` to true, and exits 1. Success
exits 0. A missing `node` binary is the wrapper's existing `OSError` finish
(`exit.json` written, the run does not hang).

### Cost

Across assistant messages:

- If there is no assistant message, any message lacks `usage`, or any
  `usage` lacks a numeric `cost.total`, `total_cost_usd` is null. Token
  counts that are present are still summed into `usage`.
- Otherwise the model has a configured price when the session model's catalog
  `cost` has a non-zero `input`, `output`, `cacheRead`, or `cacheWrite`
  (including a tier rate), or any assistant message has
  `usage.cost.totalFromProvider === true`.
- When the model has a configured price, `total_cost_usd` is the sum of
  `cost.total`, and a sum of `0` is stored as `0`.
- When the model has no configured price, `total_cost_usd` is null. A
  reported `0` from an unpriced catalog is not a free run.

`parse_result` already turns a null `total_cost_usd` into `cost_usd: null`.
The queue then charges `max_budget_usd` and sets `cost_estimated`. A stored
`0` is a real zero and is charged as `0`.

### Doctor and probe

`node pi_exec.mjs --probe <provider> ...` dynamic-imports the package, checks
each named provider's credential, and prints one JSON object on stdout. `ok`
is true only when the import works and every provider has a credential.
`missing` lists providers without one. `error` names a failed import. The
output never contains key text. `--probe` does not call the model.

`probe_pi` runs that command for the distinct providers in the loaded map and
returns `{ok, error, missing}`. `ensure_pi_persona` raises `FileNotFoundError`
when `ok` is false. Doctor `agentic.pi` uses it when `pi_needed`, and skips
otherwise. A missing `node` fails the check with that fact.

## Alternatives considered

- **`pi --mode json` as the child.** It already emits tool events and
  `usage.cost.total`, and it frames records on LF only. It still loads
  discovered extensions, and a failed agent turn does not by itself exit
  nonzero. The SDK child writes the result line and the exit code.
- **RPC mode.** RPC steers a running agent. A persona run is one shot.
  Conductor and steward stay on the CLIs they already use.
- **Call the SDK inside the queue process.** Pi has no Python SDK. A hung
  bridge would stall the runner. The wrapper already owns timeout and adopt.
- **Pass a Cursor model slug through.** Cursor ids are not Pi `provider/id`
  values, and thinking would be baked into the slug. The map carries both.
- **Treat `cost.total === 0` as a free run.** Unpriced models also report `0`.
  The dollar field stays null until a catalog rate or
  `totalFromProvider` shows the zero is real.

## Acceptance criteria

- [ ] A Pi persona launch argv is `node` plus `pi_exec.mjs` plus the run dir.
      The provider credential is not in the argv or in `launch.json`.
      `runproc` leaves a leading `node` unchanged and records stdout as
      `stream.jsonl` when the launch is a stream.
- [ ] The queue process does not import `@earendil-works/pi-coding-agent`.
      The child dynamic-imports it, creates one in-memory session, calls
      `prompt` once, and `dispose`s. No session file is written under the
      worktree `cwd`.
- [ ] The child writes assistant and tool events as JSON lines `Renderer.feed`
      already renders, then a final `type: result` line. `extract_result` /
      `parse_result` read the verdict from the final assistant text,
      `session_id`, usage, and `total_cost_usd`. A JSON line containing
      U+2028 stays one event.
- [ ] Read-only runs pass tools `read`, `grep`, `find`, `ls`. Write runs pass
      `read`, `bash`, `edit`, `write`. The resource loader disables
      extensions, skills, prompt templates, and themes, and leaves context
      files on. `appendSystemPrompt` is the persona `system_append`. The user
      message is the prompt plus the schema footer and does not include
      `system_append`. No JSON schema is passed to the session.
- [ ] A missing package, a missing credential for the selected provider, or
      an unknown `provider` / `id` writes `is_error: true` and exits 1
      without calling `prompt`. An agent failure exits 1. Key text is absent
      from the result line and stderr.
- [ ] `map_pi_model` turns `(alias, effort)` into `provider`, `id`, and
      `thinking`. `req.model` is `provider/id`. A Cursor slug and any other
      non-alias are refused. Overlay order matches `cursor_map`. A remote job
      carries `pi_provider`, `pi_id`, and `pi_thinking` and the worker does
      not remap. Claude and Cursor `request.json` files gain none of these
      keys.
- [ ] When every assistant message has numeric `cost.total` and the model has
      a configured price, `total_cost_usd` is the sum, including a sum of `0`.
      A configured price is a non-zero catalog rate (or tier) or
      `cost.totalFromProvider === true`. Missing usage, a missing `cost.total`,
      or an unpriced model leaves `total_cost_usd` null so the queue charges
      `max_budget_usd`.
- [ ] `agentic.pi` doctor and `ensure_pi_persona` fail when `node` is missing,
      the package does not import, or a mapped provider has no credential,
      and they pass when the import works and every mapped provider has one,
      without printing a key. The check skips when no campaign resolves to
      `pi`. A remote `PiRunner` is not required to have the `claude` binary.
- [ ] `resolve_runner` / `select_runner` / `nw queue add --runner pi` /
      `nw queue backend … pi` accept `pi`. Claude launch argv and Cursor
      launch argv are unchanged. Conductor and steward `--runner` choices
      stay `claude` and `cursor`.

## Test plan

- **Automated tests:** `tests/test_pi_runner.py` (no network): argv has no
  credential; `with_agent` leaves `node` unchanged; `map_pi_model` builtin
  and overlay rows; a Cursor slug and a string alias value fail validation;
  `mapped_model` for `pi` returns `provider/id` and the alias; `personas` and
  `spec_review` request builders set `pi_provider`, `pi_id`, `pi_thinking`;
  `request_to_job` / `request_from_job` round-trip those fields; Claude and
  Cursor request docs omit them; `select_runner("pi")` is a `PiRunner`;
  `resolve_runner` accepts `pi` in the existing precedence; doctor skip,
  fail, and pass via a fake `--probe` subprocess; `ensure_pi_persona`;
  the remote preflight calls it for `PiRunner` and does not require `claude`.
  `tests/js/pi_exec.test.js` (`node --test`, stub SDK passed into `execute`):
  one `inMemory` session, one `prompt`, `dispose`; tool lists; loader flags
  and `appendSystemPrompt`; user text is prompt plus footer; startup errors
  (thrown import, no credential, unknown model) write `is_error` and do not
  call `prompt`; agent error exits 1; success exits 0; key scrubbing; cost
  cases (priced sum, priced `0`, `totalFromProvider` `0`, unpriced `0`,
  missing `usage`, missing `cost.total`); a U+2028 tool argument is one
  line; `parse_result` on that result line yields the verdict and
  `cost_usd`. `tests/test_cursor_runner.py` and the Claude argv tests in
  `tests/test_agentic_runner.py` stay green.
- **Manual verification:** with `node` on `PATH`, the package resolvable from
  `pi_exec.mjs`, and the mapped provider's key set, `nw doctor` reports
  `agentic.pi` ok and one local persona run shows tool lines in
  `nw campaign tail` and a `total_cost_usd` when the model is priced. Not
  required for the unit tests; they must not call the network.
- **Regression guard:** affected tests via `tools/affected_tests.py`. Claude
  argv, Cursor argv, and conductor `--runner` choices stay.

## Rollout / migration

Hosts and workers that run Pi personas need `node` on `PATH` and
`@earendil-works/pi-coding-agent` resolvable by a dynamic `import` from
`pi_exec.mjs` (a `node_modules` walk from that file, or `NODE_PATH`). Export
the provider's own key there (`ANTHROPIC_API_KEY` for the builtin map).
`nw doctor` reports a missing binary, package, or key. In-flight runs keep
the argv they launched with. The next spawn whose resolution is `pi` uses
the Node child. Conductor and steward hosts are unchanged. An overlay map
that names another provider requires that provider's credential before
doctor passes.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest` on the
      affected set, plus `node --test tests/js/pi_exec.test.js`); manual
      live prompt not required when the stub covers the contract.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

None.
