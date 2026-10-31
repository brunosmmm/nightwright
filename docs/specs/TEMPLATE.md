---
id: SPEC-NNNN
title: <short title>
status: draft
owner: user
created: YYYY-MM-DD
# updated: YYYY-MM-DD
# milestone: "M?: <phase>"
# tags: []
# depends_on: []
# supersedes: []
# superseded_by: SPEC-NNNN
---

## Context

<Why are we doing this? What exists today? What's the problem or opportunity?>

## Goals / Non-goals

**Goals**
- …

**Non-goals**
- …

## Decision

<The chosen approach in a few sentences. State it as a decision, not a discussion.>

## Design

<The how: modules, data shapes, interfaces, algorithms, migrations. Reference
file_path:line where useful.>

## Alternatives considered

- **<option>** — <why it lost>.

## Acceptance criteria

<What must be TRUE for this to be correct. Each item must be objectively checkable.>

- [ ] …
- [ ] …

## Test plan

<HOW we prove the acceptance criteria. Required for feature/behavior specs; for a pure
policy/doc spec write "N/A — <reason>". Cover:>

- **Automated tests:** which tests, where (`tests/…`), what they assert; fixtures needed.
- **Manual verification:** exact commands/steps to run and the expected result.
- **Regression guard:** how we confirm existing behavior is unchanged.

## Clock Log

<Supplemental wall-time record for implementation sessions (SPEC-0060/0062). For internal
work, prefer `nw idea clock-in`/`nw idea clock-out` on the linked idea instead — this section
is mainly for outbound/foreign-repo implementation, where no org access exists. One
CLOCK-IN/CLOCK-OUT pair per session:>

CLOCK-IN: [timestamp]
CLOCK-OUT: [timestamp]

## Rollout / migration

<Ordered steps. Data moves. How to verify no regression.>

## Definition of done

<Loop-closure checklist — see AGENTS.md. Do not mark the spec `done` until all are true.>

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

- <resolve these before moving to `accepted`>
