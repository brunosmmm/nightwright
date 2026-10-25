---
id: SPEC-0210
title: "In-campaign spec review and revision loop with automatic acceptance"
status: accepted
owner: user
created: 2026-10-25
updated: 2026-10-25
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0169
tags: [agentic, queue, specs, review]
source_idea: IDEA-063
depends_on: [SPEC-0209]
---

## Context

Child of epic [SPEC-0169](./0169-mini-agentic-workflow-queue-campaigns-headless.md), the
third of four specs from `IDEA-063`. It also absorbs `IDEA-077`, the review store. After
SPEC-0209 an idea campaign writes its spec and parks `spec_ready` for a human. This spec
closes the loop: the SPEC-0188 spec reviewer reviews the written spec inside the campaign. A
fresh spec writer revises it on `revise`, up to a profile limit. An approving review accepts
it, and the campaign continues into IMPLEMENTING on the same branch.

Today `spec_review.review_spec` (`src/wt/agentic/spec_review.py`) does three things at once.
It resolves the spec on the live checkout, runs the reviewer with `cwd` = the Meta-Tools root,
and writes the verdict next to the spec (`docs/specs/reviews/<SPEC>.json`). Those files dirty
`main` (the conductor untracked and gitignored them in `2f82375`), are lost on clone, and
cannot work for a spec that exists only on a campaign branch.

## Goals / Non-goals

**Goals**
- A data-dir review store, `<data_dir>/agentic/reviews/<SPEC-ID>/<spec_sha256>.json`
  (`wt.spec.review.v1`), used by `wt spec review`, by the SPEC-0188 queue-add gate and by
  the campaign loop.
- An idea campaign runs SPEC_WRITING → SPEC_REVIEW → (SPEC_REVISION → SPEC_REVIEW)* → accept
  → IMPLEMENTING by itself, and parks only for a split, an exhausted loop or a failure.

**Non-goals**
- Acting on `split` automatically (IDEA-065; this spec parks).
- A structured `suggested_fix` (IDEA-076): it is passed to the reviser when present, but this
  spec does not require it.
- Watch, dashboard and event-stream views (SPEC-0211).

## Decision

Split `review_spec` into a pure runner (`run_spec_review(cfg, spec_path, spec_id, *, root,
idea_text, profile, run_dir)` returns the payload) and a store (`save_review` /
`load_review`). Add the states SPEC_REVIEW and SPEC_REVISION. Approval is a deterministic
step: set `accepted`, commit, write `spec_at_open.md`, then go to IMPLEMENTING.

## Design

- **Review store:**
  - `spec_review.store_path(cfg, spec_id, sha)`;
  - `save_review(cfg, payload)` (atomic);
  - `load_review(cfg, spec_id, sha) -> dict | None`.
  - `check_admissible` (the `wt queue add SPEC` gate) reads the store for the spec's current
    sha, and falls back to a legacy `docs/specs/reviews/<SPEC>.json` with a matching
    `spec_sha256` so existing approvals keep counting.
  - `wt spec review` writes to the store and no longer writes into the checkout.
  - The root is resolved from the spec's project (the repo containing the spec path) instead
    of a hardcoded Meta-Tools (fixes IDEA-073).
- **States:** `SPEC_REVIEW` and `SPEC_REVISION` join `STATES`, `ACTIVE_STATES` and
  `RESUMABLE_STATES`. After a valid write (SPEC-0209), the campaign transitions to
  SPEC_REVIEW instead of parking `spec_ready`. The `spec_ready` park stays available only for
  `--no-auto-accept` (below).
- **SPEC_REVIEW step:** `Step("spec_review", persona="spec_reviewer")` runs in the worktree,
  read-only, with the idea text from `state.idea`.
  - It is recorded as a normal persona run (`run.start`/`run.end`, `runs/NNN-spec_reviewer`,
    cost charged to the campaign).
  - The payload is saved to the store and logged as ledger event `spec_review {verdict,
    spec_sha256, round, counts: {must_fix, major, minor, nit}}` (added to
    `schemas.EVENT_KEYS`).
  - `state.spec_review = {round, verdict, sha}`, and `counters.spec_rounds` counts the
    reviews.
- **Routing:**
  - `approve` (no must_fix, enforced by `_check_verdict`): **accept**, with no persona.
    1. `specs.set_spec_status(<worktree spec>, "accepted")`;
    2. commit `wt(<cid>): accept SPEC-NNNN (review round R)`;
    3. copy the text to `spec_at_open.md`, and set `spec.sha256_at_open`;
    4. ledger `transition … reason spec_accepted` → IMPLEMENTING.
  - `revise` with `spec_rounds < profile.spec_review_max` (default 3, in every builtin
    profile): → SPEC_REVISION. Otherwise park `spec_review_exhausted` (resume_to
    SPEC_REVISION; `wt queue resume --reset-loops` resets `spec_rounds`).
  - `split`: park `spec_split`, detail = the `suggested_splits` titles, resume_to
    SPEC_REVISION (a human note can tell the writer to narrow the scope).
  - A reviewer run error or non-conforming verdict: one retry, then park
    `spec_review_failed`.
- **SPEC_REVISION step:** `Step("revise_spec", persona="spec_writer")`, the same persona as
  SPEC-0209 in **revise mode**.
  - The prompt adds the current spec text, the latest review's findings (with `suggested_fix`
    when present), any human notes from `pending_brief`, and the rule: edit only
    `<rel_path>`, keep the id, number, `source_idea` and `status: proposed`, and address every
    must_fix and major finding.
  - The SPEC-0209 validation applies, with "modified" instead of "new".
  - Commit `wt(<cid>): spec revision round R — SPEC-NNNN` → SPEC_REVIEW.
- **`wt queue add IDEA-NNN --no-auto-accept`** keeps the SPEC-0209 behaviour: after an
  approving review it parks `spec_ready` instead of accepting, so a human can read it first.
  It is recorded as `state.auto_accept = false`.
- **PARK_REASONS** gains `spec_review_exhausted`, `spec_split` and `spec_review_failed`.

## Alternatives considered

- **Review on main after merging the spec** — rejected by the user (one campaign end to end).
- **A separate reviser persona** — the writer already knows the template and rules; a revise
  mode keeps one prompt contract and one profile entry.
- **Keep sidecars in the repo, untracked** — invisible to campaign branches, lost on clone,
  and racy with closes.

## Acceptance criteria

- [ ] `wt spec review SPEC-X` writes `<data_dir>/agentic/reviews/SPEC-X/<sha>.json` and nothing
      under `docs/specs/`. `wt queue add SPEC-X` accepts an approving stored review for the
      current text, still accepts a legacy sidecar with a matching sha, and refuses a stale
      one.
- [ ] `wt spec review` on a spec in a second project's repo runs with that repo as root.
- [ ] Idea campaign, FakeRunner script writer → reviewer(approve): SPEC_WRITING → SPEC_REVIEW
      → IMPLEMENTING. The worktree spec is `accepted` and committed, `spec_at_open.md` equals
      the accepted text, and the run continues to MERGED with no human step.
- [ ] Script writer → reviewer(revise) → writer(revision) → reviewer(approve): passes through
      SPEC_REVISION once, `counters.spec_rounds == 2`, the reviser prompt contains the
      findings, and the result is accepted.
- [ ] `revise` on every round until `spec_review_max` parks `spec_review_exhausted`; `split`
      parks `spec_split` with the split titles; reviewer failures retry once, then park
      `spec_review_failed`.
- [ ] `--no-auto-accept`: an approving review parks `spec_ready`, and resuming accepts.
- [ ] Each review is a `spec_review` ledger event with counts, and its payload is in the
      store; reviewer runs are charged to the campaign's cost.
- [ ] `uv run pytest` passes; `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests:**
  - `tests/test_spec_review_store.py`: store paths; `wt spec review` with a FakeRunner
    reviewer writes only the store; queue-add gate cases (stored, legacy, stale); the
    second-project root.
  - `tests/test_agentic_idea_review_loop.py`, using the e2e harness and the SPEC-0209 helpers:
    the approve path to MERGED; one revise round (assert the reviser prompt via `wt campaign
    prompt` or the saved `prompt.md`); exhaustion; split; failure + retry; `--no-auto-accept`;
    ledger and cost assertions.
- **Regression guard:** full `uv run pytest`; the SPEC-0188 tests keep passing after their
  fixtures move from the sidecar to the store; the e2e `write_review` helper writes the store.

## Rollout / migration

Ships through the agentic queue after SPEC-0209. Existing legacy sidecars keep working
through the fallback. Once this lands, ideas go into the queue directly
(`wt queue add IDEA-NNN`).

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

(none)
