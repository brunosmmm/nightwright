---
id: SPEC-0469
title: Release the red-base hold once the base moves past the red sha
status: done
owner: user
created: 2026-11-08
updated: 2026-11-08
tags: [agentic]
depends_on: [SPEC-0420]
---

## Context

A red episode holds every close until a later full-suite sentinel is green. A fix that
already landed moves the base past `last_red_sha`, but the episode stays open until that
sentinel finishes. Finished campaigns then sit in `CLOSING` and the runner restarts the
held close on every tick. On 2026-11-08 the Meta-Tools episode stayed open after the
failing tests were fixed and a full merge gate had passed, so nothing could land.

## Decision

`holding()` returns None when the project's base tip is not `last_red_sha`. The episode
stays open for the sentinel. If the new tip is red, the next red sentinel sets
`last_red_sha` to it and the hold applies again. A base that cannot be read stays held.

## Design

`_base_ahead_of_red` reads `project_cfg["_root"]` and the merge base (default `main`).
No root, no marked sha, or a git error means the hold stays. `close_hold` already passes
the project config, so a live close sees the current tip.

## Acceptance criteria

- [x] An open episode holds a normal campaign while the base tip equals `last_red_sha`.
- [x] The same episode does not hold that campaign once a later commit is the base tip.
- [x] A campaign that fixes the red base, a docs-only merge, and a closed episode stay unheld.

## Test plan

- [x] `tests/test_redmain.py::test_holding_releases_once_the_base_moves_past_the_red_sha`
- [x] `tests/test_redmain.py::test_holding_per_kind`
- [x] `tests/test_redmain.py::test_prepare_close_holds_a_spec_campaign_without_touching_anything`
- [x] `uv run pytest -q tests/test_redmain.py` — 23 passed.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; the three named tests pass.
- [x] No regressions in `tests/test_redmain.py` (23 passed).
- [x] Spec body matches what shipped.
- [x] `docs/LEDGER.md` regenerated.

## As shipped

`holding()` releases when `git rev-parse` of the project base is not `last_red_sha`.
The episode record is left open for the sentinel.
