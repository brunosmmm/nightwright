---
id: SPEC-0449
title: The run tail shows shell output while the command is running
status: done
owner: user
created: 2026-11-07
tags: [agentic, herdr]
depends_on: [SPEC-0442, SPEC-0448]
---

## Context

`cursor_exec` iterates `run.messages()`. That iterator keeps only `sdk_message`
frames. A shell's stdout and stderr while the process is still running arrive
as `interactionUpdate` frames of type `shell-output-delta`, and those frames
are dropped. The pane then sits on the last edit while pytest is still
printing. The same channel also carries `text-delta`, `thinking-delta`,
`tool-call-started`, `partial-tool-call`, `step-started`, and `step-completed`.
Those repeat the message stream or carry only a step id.

A delta's `event` is `{type, data}` for `stdout` and `stderr`, `{type}` for
`stdout_trimmed` and `stderr_trimmed`, and `{type, code}` for `exit`.

## Goals / Non-goals

**Goals**
- A `shell-output-delta` is written to the stream and shown in the tail as it arrives.
- Stdout is dim, stderr is red, and a trimmed or exit notice is one line.
- After those lines have been shown, the tool's completed result shows `exit N` and does not print the same output again.
- A shell that finishes with no deltas still shows its stdout or stderr on the result, as SPEC-0448 does.
- `text-delta`, `thinking-delta`, and step frames are not written.

**Non-goals**
- Showing partial tool arguments or step durations.
- Pairing a delta with a call id. One shell is in progress at a time; the next completed shell consumes the "already shown" flag.

## Decision

Iterate `run.events()` when the run has it. Write `shell-output-delta` as
`{type: shell_output, event}`. The renderer prints the chunk. Other
interaction updates are ignored. `messages()` remains the fallback for a run
that has no `events()`.

## Design

`frame_dicts` selects the lines for one frame. `Renderer._shell_delta` prints
up to 8 non-empty lines of one chunk. `_shell_live` remembers that live output
was shown, and `_shell_result` then returns only the exit line.

## Alternatives considered

- **Keep `messages()` and poll the OS process** — the pane would learn that pytest exists, and it would not show the command's own output.
- **Write every interaction update** — assistant text and thinking would appear twice.

## Acceptance criteria

- [x] A `shell-output-delta` stdout chunk is written as `shell_output` and renders as `│ collected 3 items`.
- [x] A `text-delta` frame is not written.
- [x] After that chunk, the completed shell result renders as `← exit 0` and does not repeat the chunk.
- [x] A completed shell with no preceding delta still renders `← exit 1` and its stderr.

## Test plan

- [x] `tests/test_cursor_sdk_exec.py` covers the frame filter, the written stream, and both renderer paths.
- [x] `uv run pytest -q tests/test_cursor_sdk_exec.py tests/test_agentic_stream.py` — 41 passed.

## As shipped

`cursor_exec` reads `run.events()` and writes `shell-output-delta` as `shell_output`. Other interaction updates are dropped. The tail prints up to 8 lines of each chunk, stderr in red, and a trim or exit notice as one line. Once live output was shown, the completed shell line is only `← exit N`. A shell with no deltas still prints its output from the result. 41 tests passed on 2026-11-07.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` regenerated and `python3 tools/spec_lint.py` exits 0.
