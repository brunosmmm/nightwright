---
id: SPEC-0448
title: The run tail renders SDK tool results instead of their JSON
status: done
owner: user
created: 2026-11-07
tags: [agentic, herdr]
depends_on: [SPEC-0447]
---

## Context

A completed Cursor tool call stores `{status, value}`. The run tail does not
open that wrapper, so the pane shows one clipped JSON line. An edit's value is
`linesAdded`, `linesRemoved`, and `diffString`. A shell's value is `exitCode`,
`stdout`, and `stderr`. A read carries `totalLines` and `fileSize`. A glob or
grep carries the files. `updateTodos` carries the todo list, and its running
line dumps the args JSON.

## Goals / Non-goals

**Goals**
- An edit shows `+N −M` and the changed lines, without the `--- a/` and `+++ b/`
  path headers.
- A diff longer than 20 body lines shows the first 20 and how many remain.
- A shell shows `exit N` and up to 8 lines of stderr, or stdout when stderr is empty.
- A read shows the line count and size, and does not print file content.
- A glob or grep shows the file list.
- A todo update shows the count on the way in and the items on the way out.

**Non-goals**
- Printing a whole new file into the pane.
- Changing Claude tool-result lines that are not `{status, value}`.
- Pairing a result with an earlier running line when the stream delivers them apart.

## Decision

When a completed tool result has `status` and `value`, render `value` by the
fields it carries. Anything else keeps the one-line summary.

## Design

`Renderer._sdk_rich_result` dispatches on the value. Diff lines keep their
leading character and are colored green or red. Shell output is red when it
comes from stderr. File paths drop a leading `./`. `DIFF_LINES` is 20 and
`OUTPUT_LINES` is 8.

## Alternatives considered

- **Pretty-print the whole JSON** — a 400-line diff and a source dump still
  flood the pane, and the path headers repeat the tool line.
- **Show only the counts** — the pane would still hide the change the agent made.

## Acceptance criteria

- [x] An edit result `+1 −1` shows those counts and the `+`/`-` lines, and does not show `diffString` or the `--- a/` path.
- [x] A 40-line diff shows a "more lines" trailer.
- [x] A shell result with exit 1 shows `exit 1` and the stderr text, not `exitCode`.
- [x] A read result shows `347 lines · 20 bytes` and does not show the file content.
- [x] A glob result lists the file. A grep result lists the match file without `./`.
- [x] A running `updateTodos` shows the item count instead of the args JSON.
- [x] A Claude-shaped `{success: {content}}` result still renders as `ok` and does not print the content.

## Test plan

- [x] `tests/test_cursor_sdk_exec.py` covers the rows above, including the existing content and error cases.
- [x] `uv run pytest -q tests/test_cursor_sdk_exec.py tests/test_agentic_stream.py` — 40 passed.

## As shipped

`_sdk_rich_result` renders `{status, value}`. Edits show `+N −M` and up to 20 diff body lines. Shells show `exit N` and up to 8 lines of stderr or stdout. Reads show the line count and size. Globs and greps list files. Todo updates show the count, then the items. Other shapes stay on the one-line summary. 40 tests passed on 2026-11-07.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` regenerated and `python3 tools/spec_lint.py` exits 0.
