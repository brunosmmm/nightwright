---
id: SPEC-0457
title: Read-only Cursor tool names are lowercase
status: done
owner: user
created: 2026-11-07
updated: 2026-11-07
kind: feature
tags: [agentic, cursor]
depends_on: [SPEC-0456]
---

## Context

[SPEC-0456](./0456-readonly-cursor-personas-keep-read-tools.md) gave a read-only Cursor persona `Read`, `Grep`, `Glob`, and `LS`. The API rejects those names. On IDEA-1019 the gate triager exited in about two seconds with "unknown names Read, Grep, Glob, LS" and the valid names given as `read`, `grep`, `glob`, `ls`. Two such runs parked the campaign.

## Goals / Non-goals

**Goals**

- A read-only Cursor persona sends the lowercase tool names the API accepts.

**Non-goals**

- No change to which tools a writer gets.
- No change to the tail's rendering of a tool-call event that already happened.

## Decision

`READ_ONLY_TOOLS` is `read`, `grep`, `glob`, `ls`.

## Design

`agent_tools(True)` returns that list. `agent_tools(False)` still returns `None`.

## Alternatives considered

- **Resume the campaign on the capitalized names.** The API rejects the spawn, so the triager parks again.

## Acceptance criteria

- [x] A read-only create passes `read`, `grep`, `glob`, and `ls`, and no other tool.
- [x] A writable create still passes no tool list.

## Test plan

- **Automated tests:** `tests/test_cursor_sdk_exec.py` asserts `agent_tools(True)` and the create call use the lowercase names.
- **Manual verification:** the next read-only spawn after the queue re-execs sends those names. A process already running keeps the old list.
- **Regression guard:** `agent_tools(False)` is still `None`.

## Rollout / migration

Land the names, let the queue re-exec, then resume a campaign parked on the unknown-tool error to GATING with the triage count reset.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass; manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

None.
