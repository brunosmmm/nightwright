---
id: SPEC-0442
title: Cursor SDK persona runs
status: done
owner: user
created: 2026-11-07
updated: 2026-11-07
kind: feature
tags: [agentic, runner, cursor]
depends_on: [SPEC-0436, SPEC-0362]
---

## Context

[SPEC-0436](./0436-persona-cursorrunner-and-cursor-model-maps.md) runs a Cursor persona
as `cursor-agent -p --output-format stream-json`. That print mode does not return a
dollar cost, and its event stream is a CLI shape the tail only partly understands.
The Cursor Python SDK (`cursor-sdk`) runs the agent loop locally against the campaign
worktree, streams typed events, and can ask the account for billed cents
(`Agent.get_usage`). Conductor and steward sessions stay on the interactive
`cursor-agent` CLI. [SPEC-0435](./0435-cursor-agent-backend-for-the-agentic-queue.md)
named the SDK as a non-goal; this spec is the decision that reverses that for
**persona** runs only. SPEC-0435 and SPEC-0436 stay `done`: the model map,
`resolve_runner`, and conductor/steward launch still govern.

## Goals / Non-goals

**Goals**
- When a persona spawn resolves to `cursor`, the detached wrapper starts
  `python -m nightwright.agentic.cursor_exec` instead of `cursor-agent -p`.
- The child persists each SDK message as one JSON line, then a `type: result`
  line `extract_result` / `parse_result` already collect.
- `get_usage()` supplies `total_cost_usd` from `raw_cost_cents` (falling back to
  `charged_cents` when raw is 0). Both cent fields are stored. A
  `feature_unavailable` lookup leaves `total_cost_usd` null so the queue keeps
  charging the budget as an estimate.
- Read-only personas pass `tools=[]`. Write personas keep the SDK's default
  toolset, with sandbox and auto-review left off.
- `nw doctor` and the remote persona preflight require `cursor-sdk` importable
  and `CURSOR_API_KEY` set. They do not require `cursor-agent login`.
- The live tail prints SDK tool calls (one line in, one short line out) and does
  not print thinking text. Stream lines split on `\n` only, so a U+2028 inside a
  JSON string is not an `[unparseable line]`.

**Non-goals**
- Replacing conductor or steward `cursor-agent` launch, `create-chat`, or `--resume`.
- Cloud agents, ACP, or calling the SDK inside the queue process.
- A JSON Schema constraint on the SDK call. The schema footer and `parse_result` stay.
- Adding `cursor-sdk` to the core install. It is an optional extra.
- Following tool-call output files, or rendering thinking text.
- Cancelling or rewriting the IDEA-1012 campaign, which adapts the CLI stream.

## Decision

Persona Cursor runs go through the local Python SDK inside the existing `runproc`
child. Claude stays `claude -p`. Auth for personas is `CURSOR_API_KEY` from the
environment (never written into `launch.json`). The child is lazy: a missing
package or a missing key writes an `is_error` result and exits non-zero instead
of hanging. Dollar cost is whatever `get_usage` returns; this account's local
`feature_unavailable` is a lookup failure, not a reason to skip the call.
`total_cost_usd` is `raw_cost_cents / 100` when raw is non-zero, else
`charged_cents / 100`, including a reported `0`. A missing cost object or a
failed lookup stays null. Persona instructions stay in the user message because
Python `AgentOptions` has no `systemPrompt`. `setting_sources` is `["project"]`
so the worktree's rules load and user/global settings do not. The model id is
`request.json`'s `model` (already mapped). The session id on the result is the
SDK `agent_id`.

## Design

`CursorRunner._launch_argv` returns

```text
<sys.executable> -m nightwright.agentic.cursor_exec <run_dir>
    [--read-only] [--add-dir DIR ...]
```

`runproc` already treats an argv whose first token is not `cursor-agent` as a
literal command, so timeout, process group, `stream.jsonl`, and adopt are
unchanged. The child reads `request.json` and `prompt.md` (the prompt already
includes the schema footer). It calls `Agent.create` with `LocalAgentOptions`
(`cwd`, `dirs` from `--add-dir`, `setting_sources=["project"]`), `tools=[]` when
the run is read-only, and `api_key` from `CURSOR_API_KEY`. It iterates
`run.messages()`, writes one JSON object per line, calls `run.wait()`, then
`agent.get_usage()`. SIGTERM best-effort calls `run.cancel()`; the wrapper still
SIGKILLs the group after the grace period.

The result line:

```json
{"type":"result","subtype":"success","is_error":false,"result":"<final text>",
 "session_id":"<agent_id>","sdk_run_id":"<run_id>","duration_ms":0,
 "usage":{"input_tokens":0,"output_tokens":0,"cache_read_tokens":0,
          "cache_write_tokens":0,"cache_read_input_tokens":0,
          "cache_creation_input_tokens":0},"total_cost_usd":null,
 "cost":{"raw_cost_cents":0,"charged_cents":0}}
```

`usage` keeps the SDK names and the Claude aliases `cache_read_input_tokens` /
`cache_creation_input_tokens` so existing token readers see cache counts.
`total_cost_usd` is null when `get_usage` raises or `cost` is null. Startup
failures (no key, no package, SDK error before a run) write the same shape with
`is_error: true` and exit 1. The API key is scrubbed from error text.

`Renderer.feed` handles top-level `tool_call` (`running` → one-line args summary;
`completed` / `error` → first line, not a file body) and `usage` (a dim token
line). Top-level `thinking` is persisted and not printed. A `system`/`init`
event prints a tool count only when `tools` is a list. `read_lines` and
`runproc._result_line` split on `\n` only.

`probe_cursor_sdk` is the persona health check (`import cursor_sdk` plus a
non-empty `CURSOR_API_KEY`). `probe_cursor_agent` stays for conductor launch.
`nw worker cursor-check` and `remote_personas.run_job` use the SDK probe for a
`CursorRunner`. The optional extra is `cursor = ["cursor-sdk>=1.3.1"]`.

## Alternatives considered

- **Keep `cursor-agent -p` and only fix the tail.** Leaves print mode, which
  cannot return billed cents and does not stream the SDK's typed events.
- **Call the SDK inside the queue process.** A hung bridge would stall the
  runner. The wrapper already owns timeout and adopt.
- **Cloud runtime.** It clones a repo on a Cursor VM and does not drive the
  campaign worktree.
- **`tools` allowlist matching Claude's names.** SDK tool names are not Claude's.
  Empty means read-only; unset means the local default, which is the same
  posture as `--force`.
- **Treat `charged_cents == 0` as the budget number.** Plan-included runs report
  charged 0 while raw still prices the model. The cap stands in for model cost,
  so raw wins when it is non-zero.

## Acceptance criteria

- [x] A Cursor persona launch argv is `python -m nightwright.agentic.cursor_exec`
      with the run dir, not `cursor-agent -p`. The API key is not in the argv
      or in `launch.json` via that argv.
- [x] The child writes each SDK message and a final `type: result` line.
      `parse_result` reads the verdict, `session_id`, usage, and
      `total_cost_usd` from that line.
- [x] When `get_usage` returns cents, `total_cost_usd` is raw/100, or charged/100
      when raw is 0, and both cent fields are on the result. A
      `feature_unavailable` error leaves `total_cost_usd` null and the run
      successful if the agent finished.
- [x] Read-only runs pass `tools=[]` (via `read_only` on the create options).
      Write runs do not. `setting_sources` is `["project"]`. Sandbox and
      auto-review are not enabled.
- [x] Missing `CURSOR_API_KEY` or a missing `cursor_sdk` import writes an
      `is_error` result and returns non-zero without calling the agent.
- [x] The tail renders an SDK `tool_call` as a one-line summary and does not
      render a top-level `thinking` event. A JSON line containing U+2028 stays
      one event. A system event with no `tools` list does not say `0 tools`.
- [x] `check_cursor_agent` / `cursor_check` / the remote Cursor preflight fail
      on a missing package or key, and pass when the import works and the key
      is set, without printing the key. `probe_cursor_agent` still exists for
      the CLI.
- [x] Claude launch argv is unchanged.

## Test plan

- **Automated tests:** `tests/test_cursor_sdk_exec.py` (fake agent, no network):
  argv, stream + `parse_result` cost, `feature_unavailable`, raw-0/charged,
  read-only options, missing key, missing import, key scrubbing, tail tool-call
  and thinking, U+2028 `read_lines` / `_result_line`, doctor and
  `ensure_cursor_persona`. `tests/test_cursor_runner.py` argv and doctor
  expectations updated. Claude stream fixtures in `tests/test_agentic_stream.py`
  stay green.
- **Manual verification:** with `uv sync --extra cursor` and `CURSOR_API_KEY`
  set, one local persona run shows tool lines in `nw campaign tail` and a
  result cost when `get_usage` is enabled for the account. Not run in this
  change: the key is supplied by the operator at live time, and unit tests
  must not call the network.
- **Regression guard:** affected tests via `tools/affected_tests.py`. Claude
  argv tests and `probe_cursor_agent` login test stay.

## Rollout / migration

Install the extra on hosts and workers that run Cursor personas
(`uv sync --extra cursor`) and export `CURSOR_API_KEY` there. `nw doctor`
reports a missing package or key. In-flight `cursor-agent -p` runs keep the
argv they launched with. The next Cursor spawn uses the SDK. Conductor and
steward hosts still need the `cursor-agent` binary and `cursor-agent login`.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; affected tests pass; manual live prompt not required
      for this change (no key in the test environment; fake agent covers the
      contract).
- [x] No regressions in the affected set.
- [x] Spec body matches what shipped.
- [x] `docs/LEDGER.md` regenerated.

## Open questions

None.
