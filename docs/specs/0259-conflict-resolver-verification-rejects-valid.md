---
id: SPEC-0259
title: "Resolver may declare dropped_commits superseded by upstream; gates decide"
status: accepted
owner: user
created: 2026-10-26
updated: 2026-10-26
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0169
tags: [agentic, close, personas, rebase, queue, bug]
source_idea: IDEA-328
depends_on: [SPEC-0252]
---

## Context

Child of epic [SPEC-0169](./0169-mini-agentic-workflow-queue-campaigns-headless.md); the
remainder of `IDEA-328` after [SPEC-0252](./0252-conflict-resolver-verification-by-patch-equivalence.md)
shipped. `gitops.verify_resolution` (src/wt/agentic/gitops.py) accepts an old campaign commit
only if range-diff pairs it, `git cherry` marks it present in the target, it is empty, or its
patch reverse-applies to `HEAD`. A new class (IDEA-328 Log, 2026-10-26, SPEC-0229 close): the
campaign commit 9defa5f was superseded by a *different* upstream edit of the same lines
(SPEC-0248 fixed the same flaky test another way). It is not empty, not a patch-id match, and
does not reverse-apply, so the check fails "campaign commit ... is missing after the
resolution" although dropping it was correct.

## Goals / Non-goals

**Goals**
- G1. The resolver can state, in its verdict, which campaign commits it intentionally dropped
  and why.
- G2. Verification accepts a missing commit only on that declaration, and the always-run
  re-gates decide whether the result is right.
- G3. The decision is auditable on the ledger.

**Non-goals**
- Judging the reason's quality: gates are the truth, not the text.
- Relaxing any other check (attached HEAD, no rebase, no unmerged paths, ancestor, revert
  guard, clean tree).

## Decision

The `conflict_resolver` verdict gains an optional `dropped_commits: [{sha, reason}]`.
`verify_resolution` takes the declared set; a commit that falls through all accounting branches
(paired, cherry `-`, empty, reverse-applies) and is declared is accepted as `superseded`
instead of failing. An undeclared one still fails as today. A declaration naming a commit that
is in fact paired or otherwise present is ignored. Because `prepare_close` always re-gates the
rebased tree after resolution (close.py step 5), a wrongful drop surfaces as a gate failure,
which routes to triage or the existing park path; the dropped commits are recorded as a
`finding` so the conductor can audit them.

## Design

- **personas.py**: `CONFLICT_RESOLVER_SCHEMA` gains optional `dropped_commits` (array of
  objects, required `sha` and `reason`, strings). `RESOLVER_INVARIANT` and
  `personas/conflict_resolver.md` say: if a campaign commit's intent was already delivered a
  different way upstream and keeping it would be wrong, do not port it; list it (full or
  abbreviated sha from `git log <fork>..<pre-head>`) with a one-sentence reason; never use it
  to skip a conflict you could resolve.
- **gitops.verify_resolution(..., declared_dropped=())**: new keyword. Declared shas are
  normalized with `git rev-parse --verify <s>^{commit}` (failures are ignored) and intersected
  with `old_commits`. In the loop over `old_commits`, before the final `fail(... is missing
  ...)` return: if `c in declared and c not in paired`, append `_commit_entry(worktree, c)`
  plus `reason` to a new `ResolutionCheck.superseded` list and `continue`. A superseded commit
  is not added to `guarded` (nothing of it is expected in the branch).
  `ResolutionCheck` gains `superseded: list[dict]` (default empty).
- **queue._do_resolve** passes `verdict.get("dropped_commits")` shas; `res.resolution` gains
  `superseded`. **`_apply_resolve`** adds `superseded_commits` to the `rebase_resolved` event
  (schemas.py lists it; `render.rebase_resolved_text` appends `; K superseded`, old events
  unchanged) and, when non-empty, a `finding` (`persona: conflict_resolver`, `severity:
  major`, `gate: rebase`, title `resolver dropped campaign commits`, detail listing sha,
  subject, reason) so the drop is visible in the review and report.
- Gate dependence is by construction: `closing.resolve` is cleared and the close redoes
  `prepare_close`, whose step 5 runs `G.run_all`. Nothing here may add a path from a verified
  resolution to a merge without that re-gate; the test below pins it.

## Alternatives considered

- **Accept any missing commit when gates pass.** Rejected: silently loses intended work whose
  tests happen not to cover it; the declaration forces the resolver to own the call.
- **Compute "superseded" by diffing touched lines.** Rejected: heuristic, and the superseding
  edit differs by definition.
- **Park for a human on any non-reverse-applicable drop.** Rejected: the observed case was
  valid and the gates plus the audit finding are sufficient.

## Acceptance criteria

- [ ] The verdict schema accepts `dropped_commits` and rejects entries without `sha` or
      `reason`; the invariant and persona prompt describe the field.
- [ ] A non-empty campaign commit that is unpaired, not in `git cherry`, not reverse-applicable
      and declared passes verification and appears in `ResolutionCheck.superseded`.
- [ ] The same commit undeclared still fails `is missing after the resolution`; a declared sha
      that is not an old campaign commit has no effect; all other checks are unchanged.
- [ ] `rebase_resolved` carries `superseded_commits` and a `major` finding names each dropped
      commit with its reason; old events render unchanged.
- [ ] After a resolution with superseded commits the close runs the gates before merging; a
      failing gate does not merge.
- [ ] The Test plan passes and `uv run pytest` is green.

## Test plan

- **Automated tests** (`tests/test_agentic_resolver.py`, real git in a tmp repo, FakeRunner
  `shell`; `tests/test_agentic_personas.py`):
  - `test_superseded_commit_declared_verified`: base edits line L one way on the target, the
    campaign edits L another way; the resolver `git rebase --skip`s that commit and returns
    `dropped_commits: [{sha, reason}]`; result verified, event and finding present.
  - `FAILS["superseded_undeclared"]`: same without the declaration fails with `is missing`,
    and `pre_head` is restored.
  - `test_declared_but_present_commit_ignored`; `test_declared_unknown_sha_ignored`.
  - Close integration: with a superseded drop and a gate command that exits 1, the campaign is
    not `MERGED` and the gate ran; with a passing gate it merges.
  - Persona/schema tests for the new field and prompt text; render test for `; K superseded`.
- **Sabotage checks:** accepting every `<` commit fails `superseded_undeclared`; ignoring the
  declaration fails `test_superseded_commit_declared_verified`; treating declared as
  skipping the re-gate fails the close integration test; omitting the finding fails the event
  test.
- **Manual verification:** next real close that supersedes a commit shows `+K superseded` and
  the finding, then a green re-gate.
- **Regression guard:** full `uv run pytest`; the SPEC-0221/0252 resolver cases unchanged.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; `uv run pytest` passes.
- [ ] Spec body updated to match what shipped; ledger regenerated.

## Open questions

None.
