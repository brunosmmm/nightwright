---
id: SPEC-0383
title: Make `nw gate run` discoverable by agents (skill, AGENTS.md, CLI help)
status: done
owner: user
created: 2026-11-02
tags: [docs, agents, gate]
---

## Context

SPEC-0381 shipped `nw gate run` (standalone remote gate jobs on a worker box) but no agent-facing
doc mentions it, so agents default to a local full pytest run and humans paste instructions.
The reference index already lists `nw gate`.

## Goals / Non-goals

**Goals**
- A short, consistent `nw gate run` section in `skills/nw-orient/SKILL.md`, a rule in `AGENTS.md`,
  and an example in `nw gate run --help`.

**Non-goals**
- No behavior change; related to SPEC-0381 (not a child).

## Decision

Document the same facts in three agent-facing places and guard them with a test.

## Design

- nw-orient: section "Gating on the worker box (nw gate run)" under Working rules.
- AGENTS.md: one bullet under "While implementing" pointing to it and SPEC-0381.
- `gate_run` docstring: exit codes (already) plus one example.
- `docs/product/reference/index.md` already has the `gate` row; left as is.
- `tests/test_gate_docs.py` asserts all of the above.

## As shipped

As designed. `nw --help` and the reference index already listed `gate`, so only the `gate run`
docstring example was added. Tests run: `tests/test_gate_docs.py`, `tests/test_skills.py`,
`tests/test_gate_jobs.py` (108 passed, `-n 4`); `nw gate run --help` checked by hand.

## Alternatives considered

- **Only the skill** -- AGENTS.md is read first by every agent, so it needs the pointer too.

## Acceptance criteria

- [x] nw-orient SKILL.md has the gate section with commands, exit codes, constraints, known limit.
- [x] AGENTS.md "While implementing" has the `nw gate run` rule; existing testing rules untouched.
- [x] `nw --help` lists `gate`; `nw gate run --help` shows exit codes and an example.
- [x] Reference index has the `nw gate` entry.
- [x] `tests/test_gate_docs.py` passes.

## Test plan

- **Automated tests:** `tests/test_gate_docs.py` (docs mention `nw gate run`; help lists exit codes
  and example; `nw --help` lists gate); `tests/test_skills.py` (thinness guard still holds).
- **Manual verification:** `uv run nw gate run --help`.
- **Regression guard:** `tests/test_gate_jobs.py`; `spec_lint` exit 0.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; affected tests pass.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated.

## Open questions

- None.
