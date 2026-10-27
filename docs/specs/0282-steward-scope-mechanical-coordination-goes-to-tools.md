---
id: SPEC-0282
title: "Steward scope: mechanical coordination goes to tools or the conductor"
status: done
kind: feature
owner: user
created: 2026-10-27
source_idea: IDEA-082
parent: SPEC-0169
depends_on: [SPEC-0184, SPEC-0208]
tags: [agentic, skills, steward, conductor]
---

## Context

Promoted from idea `IDEA-082` (kind: bug). On 2026-10-25 the conductor kept mailing the
Meta-Tools steward to reserve spec numbers (SPEC-0201..0203, 0205..0207, 0212, 0213). The
steward did it each time, so it ended up as a manual number registrar inside the mailbox loop.
The human's reaction: "that is not the steward job".

SPEC-0184 gives the steward project judgment: it answers questions about a project's
campaigns, reworks specs, and relays the human's queue requests. Neither skill says this:

- `skills/wt-steward/SKILL.md` says what the steward does ("When to use", "Conventions /
  guardrails") but never what it must decline.
- `skills/wt-conduct/SKILL.md` ("Conventions / guardrails") does not forbid sending
  mechanical work to a steward. It also does not name the tool that now owns spec numbers:
  `wt spec reserve` (SPEC-0208, `src/wt/cli.py:spec_reserve_cmd`, done). `wt spec new`
  already calls the same allocator through `spec_numbers.allocate` (`src/wt/specs.py`).
- `tests/test_skills.py` pins other routing rules but not this one. An example is
  `test_conduct_and_steward_route_merges_through_the_queue_spec_0234`.

## Goals / Non-goals

**Goals**
- The steward skill states its scope:
  - It answers project-judgment questions and routes the human's requests.
  - It declines mechanical coordination (spec numbers, bookkeeping, merges, runner
    restarts) and points at the tool or the conductor.
- The conductor skill says:
  - Never ask a steward for mechanical work.
  - Spec numbers come from `wt spec reserve` or `wt spec new`.
- `tests/test_skills.py` pins both rules.

**Non-goals**
- A new mailbox kind, or a decision-table rule that tags questions as "judgment" (item 3 of
  IDEA-082). Skill text plus a test is enough for now. A mailbox schema change can follow if
  the rule is broken again.
- Any code change to the mailbox, the allocator or the queue.

## Decision

Add a short **Scope** section to `skills/wt-steward/SKILL.md`, right after "When to use".
Add one guardrail bullet to "Conventions / guardrails" in `skills/wt-conduct/SKILL.md`. Pin
the exact sentences in a new `tests/test_skills.py` test, in the style of the SPEC-0234 test.
The quoted phrases below are the contract the test checks.

## Design

- `skills/wt-steward/SKILL.md` gets a new `## Scope` section with these bullets:
  - "You answer project-judgment questions": a wrong or ambiguous spec, a triage brief, an
    exhausted loop, a blocked implementer, a spec rework.
  - "You route the human's requests" for this project to the conductor (`--kind request`).
  - "Decline mechanical coordination". This covers:
    - spec numbers (`wt spec reserve`);
    - bookkeeping (`wt spec reconcile`, idea links);
    - merges (`wt queue merge`, through the conductor);
    - runner restarts and queue state changes.

    The steward replies with `--kind answer`, naming the tool or saying "the conductor owns
    this", and does not do the work itself.
- `skills/wt-conduct/SKILL.md` gets this new guardrail bullet: "**Never ask a steward for
  mechanical work.** Spec numbers come from `wt spec reserve` (or `wt spec new`, which
  reserves); merges, bookkeeping and restarts are yours or a tool's. Mail a steward only for
  project judgment."
- `tests/test_skills.py` gets a new `test_steward_scope_and_no_mechanical_requests_spec_0282`.
  It reads both files through `REPO_SKILLS_DIR` and asserts that:
  - the steward file contains `## Scope`, "Decline mechanical coordination" and
    `wt spec reserve`;
  - the conductor file contains "Never ask a steward for mechanical work" and
    `wt spec reserve`;
  - the conductor's `| \`message\` (\`kind\` request)` row and the steward's "Never merge into
    `main` by hand" are still there, so SPEC-0234 has not regressed.
- Installed copies need no action. `src/wt/skills.py` links `skills/<name>/` by default, and
  followers already re-read changed skills (SPEC-0231).

## Alternatives considered

- **A mailbox `kind: judgment`.** It is a schema change that needs tests across the follower,
  the TUI and the CLI, so it is deferred (see Non-goals).
- **A conductor-only fix.** The steward would still comply with the next out-of-scope request
  from any sender, including the human's relay.

## Acceptance criteria

- [x] `skills/wt-steward/SKILL.md` has a `## Scope` section. It names project judgment and
      routing as the steward's job. It declines spec numbers, bookkeeping, merges and
      restarts, and points at `wt spec reserve` or the conductor.
- [x] The guardrails in `skills/wt-conduct/SKILL.md` forbid mechanical requests to stewards
      and name `wt spec reserve` as the source of spec numbers.
- [x] `tests/test_skills.py::test_steward_scope_and_no_mechanical_requests_spec_0282` passes,
      and the SPEC-0234 and SPEC-0235 skill tests still pass.

## Test plan

- **Automated:** the new test in `tests/test_skills.py`.
- **Sabotage check:**
  - Delete the conductor bullet. The new test must fail.
  - Restore it, then rename `## Scope` to `## Role`. The test must fail.
- **Regression guard:** `uv run pytest tests/test_skills.py`, then the full `uv run pytest`.
- **Manual:** none. This is skill text, and followers re-read it when it changes (SPEC-0231).

### Test record (2026-10-27)

- `uv run pytest tests/test_skills.py`: 64 passed.
- Sabotage: deleting the conductor bullet and renaming `## Scope` to `## Role` each failed the new test; both restored.
- Full `uv run pytest`: see below, passed.

## Rollout / migration

None. Merged skill text goes live through the symlinked install.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

(none)
