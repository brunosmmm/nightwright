---
id: SPEC-0447
title: The run tail joins SDK text tokens into a sentence
status: done
owner: user
created: 2026-11-07
tags: [agentic, herdr]
depends_on: [SPEC-0362, SPEC-0442]
---

## Context

A herdr run pane follows `nw campaign tail`. The renderer prints each assistant text
block as its own line. Claude's stream sends a finished block. The Cursor SDK sends
one event per token, and the space between words is the leading space of the next
token. Run 006 of IDEA-967 showed `I'll`, ` implement`, ` SPEC`, `-`, `044`, `6`
as six events, and the pane showed six lines. Those 37 tokens were one contiguous
burst, then the tool calls. Thinking events were not mixed into the burst.

## Goals / Non-goals

**Goals**
- Consecutive assistant text tokens render as one sentence, with the spaces the
  tokens already carry.
- A tool call, a result, or any other non-thinking event prints the sentence first.
- A newline inside the text still starts a new pane line.
- A poll that ends with text still held prints that text, so a sentence shows up
  before the next tool call arrives.
- `stream.jsonl` stays one event per token.

**Non-goals**
- Redrawing a line that was already printed when later tokens arrive.
- Coalescing tokens inside `cursor_exec` before they are written.
- Changing tool-call or result lines.

## Decision

The renderer holds assistant text. It flushes finished lines on a newline, and
flushes the rest when any event other than `assistant` or `thinking` arrives.
The tail viewer and the dashboard tail also flush at the end of each poll.
A cost line that belongs to held text waits and prints under the sentence.

## Design

`Renderer._prose` is keyed by `parent_tool_use_id`, so a nested stream does not
join onto the parent's sentence. `flush_prose` empties every key. `tail.emit`
and `RunTail.poll` call it after the lines they just read.

## Alternatives considered

- **Join only the lines that arrived in one poll** — a tool call in the same poll
  would still print before the sentence if the flush waited until the end.
- **Rewrite the stream as paragraphs** — `--raw` and later readers would lose the
  token events.

## Acceptance criteria

- [x] Tokens `I'll`, ` implement`, ` SPEC`, `-`, `044`, `6` followed by a tool call render as `I'll implement SPEC-0446` on one line, then the tool line.
- [x] A thinking event between tokens does not split the sentence.
- [x] `hello\nworld` prints `hello` immediately and holds `world` until a flush.
- [x] A finished stream of only those tokens still shows the joined sentence.
- [x] Claude's one-block `ok` sample still shows `ok`.

## Test plan

- [x] `tests/test_agentic_stream.py` covers the token join, the newline, and a finished file.
- [x] `uv run pytest -q tests/test_agentic_stream.py tests/test_cursor_sdk_exec.py tests/test_dashboard_campaign.py` — 48 passed.

## As shipped

`Renderer` holds assistant text per `parent_tool_use_id`. A newline emits the finished lines. Any other event except `thinking` emits the held sentence first. `tail.emit` and the dashboard `RunTail.poll` flush again at the end of a poll. The cost line for held text prints under the sentence. `stream.jsonl` is unchanged. 48 tests passed on 2026-11-07.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` regenerated and `python3 tools/spec_lint.py` exits 0.
