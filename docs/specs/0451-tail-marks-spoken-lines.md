---
id: SPEC-0451
title: The run tail marks the agent's spoken lines
status: done
owner: user
created: 2026-11-07
tags: [agentic, herdr]
depends_on: [SPEC-0447]
---

## Context

A herdr run pane shows tool calls, results, shell output, and the agent's own
words in one scroll. Spoken lines are indented plain text. A sentence such as
"The public request store is specified on the prerequisite" looks like another
line of output, so a response is easy to miss while scanning.

SPEC-0450 is left for the campaign that is writing it.

## Goals / Non-goals

**Goals**
- Each spoken line starts with a marker in a color that is not used for tools or shell output.
- The words on that line use the same color.
- Tool, result, shell, and thinking lines stay as they are.

**Non-goals**
- Changing `stream.jsonl`.
- Coloring the dashboard's plain-text copy beyond the marker character it already receives.

## Decision

Spoken lines render as `▸` plus the text, both in bold yellow.

## Design

`Renderer._speech_line` builds the line. `_append_prose` and `_flush_key` use it,
so a newline and a later flush look the same. Nested speech keeps its `┆` indent
and then the marker.

## Alternatives considered

- **Color the text and leave the prefix off** — a long paragraph is a block of color, and a one-line reply is still easy to skip.
- **A bold white line with no marker** — it collides with the session and result lines.

## Acceptance criteria

- [x] Joined speech renders as `▸ I'll implement SPEC-0446`.
- [x] `hello` and the held `world` each carry the marker, and the marker's style is bold yellow.
- [x] Claude's one-block `ok` sample shows `▸ ok`.

## Test plan

- [x] `tests/test_agentic_stream.py` covers the marker on a joined sentence, a newline, and the `ok` sample.
- [x] `uv run pytest -q tests/test_agentic_stream.py tests/test_cursor_sdk_exec.py tests/test_dashboard_campaign.py` — 50 passed.

## As shipped

Each spoken line is `▸` plus the text, both bold yellow. Tool, result, and shell lines are unchanged. 50 tests passed on 2026-11-07.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` regenerated and `python3 tools/spec_lint.py` exits 0.
