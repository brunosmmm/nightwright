---
id: SPEC-0452
title: The run tail renders a spoken verdict as a review
status: done
owner: user
created: 2026-11-07
tags: [agentic, herdr]
depends_on: [SPEC-0451]
---

## Context

A reviewer finishes by speaking one JSON object. The pane printed that object
line by line, braces and keys included, so a spec review read as a JSON dump.
The words were already joined. The shape was the problem.

## Goals / Non-goals

**Goals**
- A spoken JSON object with a `verdict` string renders as a review: the verdict, the rationale, each finding, and any splits.
- The object is held until its braces close, including across a poll, so a half-written verdict is not printed.
- A sentence before the object stays a spoken line.
- An unfinished object is printed as text when a later event needs the space.

**Non-goals**
- Changing `stream.jsonl`.
- Rendering a JSON object that has no `verdict`.
- Wrapping lines to the pane width.

## Decision

Hold text that opens with `{` or `[` until the value closes. If it parses as an
object with a `verdict` string, print a review. Otherwise print it as speech.

## Design

`Renderer._drain` emits finished sentences immediately and keeps an open JSON
value. `flush_prose` still emits a held sentence, and it leaves an open verdict
held. `_review_lines` prints the header, rationale or summary, `fix_brief`,
findings, suggested splits, and acceptance checks. `must_fix` is red, `major`
is yellow, and `approve` or `pass` is green.

## Alternatives considered

- **Pretty-print the JSON in yellow** — it stays a dump.
- **Read the verdict only from the result event** — the pane would stay empty until the run ended, and a revised draft the model spoke earlier would disappear.

## Acceptance criteria

- [x] A pretty-printed `split` verdict streamed in small pieces renders as `▸ split · xl · critical`, the rationale, the `must_fix` finding, and the suggested split, with no `"verdict"` key.
- [x] `{"verdict": "spl` survives `flush_prose` unprinted, and the closing `it"}` renders `▸ split`.
- [x] `Checking the spec.` before an `approve` object stays a spoken line, and the object renders as `▸ approve` plus its rationale.

## Test plan

- [x] `tests/test_agentic_stream.py` covers the streamed verdict, the held fragment, and prose before the object.
- [x] `uv run pytest -q tests/test_agentic_stream.py tests/test_cursor_sdk_exec.py tests/test_dashboard_campaign.py` — 52 passed.

## As shipped

A spoken verdict is held until the JSON value closes, then shown as a review. Other speech is unchanged. 52 tests passed on 2026-11-07.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` regenerated and `python3 tools/spec_lint.py` exits 0.
