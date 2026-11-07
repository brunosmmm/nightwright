---
id: SPEC-0458
title: The run tail renders a triage and an implementer result as cards
status: done
owner: user
created: 2026-11-07
updated: 2026-11-07
kind: feature
tags: [agentic, herdr]
depends_on: [SPEC-0452]
---

## Context

A gate triager finishes by speaking `GATE_TRIAGER_SCHEMA`: `disposition`, `confidence`, `must_fix`, and `fix_brief`. An implementer finishes by speaking its result: `status`, `summary`, `changed_files`, `tests_run`, and `ac_status`. [SPEC-0452](./0452-tail-renders-spoken-verdicts.md) turns a spoken object into a card only when it has a string `verdict`. These two objects have no `verdict`, so the pane printed them as JSON under the speech marker.

## Goals / Non-goals

**Goals**

- A spoken gate triage renders as a card: disposition, confidence, each must-fix, the brief.
- A spoken implementer result renders as a card: status, summary, files, tests, acceptance.

**Non-goals**

- The stream file stays one JSON event per token. The queue still validates the schema.
- A review triager (`waive` and `note`) is unchanged.
- An object that is not one of these shapes still prints as speech.

## Decision

The tail recognizes the two schemas by their outcome field. `disposition` of `fix`, `retry_gate`, `waive`, or `escalate` is a triage. `status` of `done`, `partial`, or `blocked`, together with one of the implementer fields, is an implementer result. `verdict` still wins when it is present.

## Design

`_triage_lines` draws `▸ {disposition} · {confidence}`, adds `flaky` only when `suspected_flaky` is true, then each `must_fix` (gate, title, detail, files), each waiver, the `fix_brief`, and any follow-up ideas. Empty lists are omitted. `fix` and `escalate` are red, `waive` is green, `retry_gate` is yellow.

`_status_lines` draws `▸ {status}` (`done` green, `partial` yellow, `blocked` red), the summary, changed files, each test command, acceptance as `met` or `unmet`, blockers, and follow-up ideas. Empty blockers and follow-ups are omitted.

## Alternatives considered

- **Pretty-print the JSON in place.** The keys would still be what a person has to read.

## Acceptance criteria

- [x] A spoken gate triage with `disposition: fix` renders the disposition, confidence, must-fix title, detail, files, and brief, and does not print the JSON keys.
- [x] `suspected_flaky: true` adds `flaky` to the triage header. `false` does not.
- [x] A spoken implementer result with `status: done` renders the summary, files, test command, and each acceptance line as met or unmet, and does not print the JSON keys.
- [x] Empty `blockers` and `followup_ideas` are omitted. A non-empty blocker is a line.
- [x] A spoken object with a `verdict` string still renders as a review.

## Test plan

- **Automated tests:** `tests/test_agentic_stream.py` feeds chunked JSON for a triage and an implementer result and asserts the card lines. The existing verdict test still passes.
- **Manual verification:** the next tail process shows the card. A tail already open keeps the old rendering until it restarts.
- **Regression guard:** the verdict review test is unchanged and passes.

## Rollout / migration

None. Open run panes pick this up when their tail process restarts.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass; manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

None.
