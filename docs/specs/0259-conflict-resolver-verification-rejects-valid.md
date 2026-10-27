---
id: SPEC-0259
title: "Resolver may declare dropped_commits superseded by upstream; gates decide"
status: done
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
is in fact paired or otherwise present is ignored. The safety net is the re-gate of the rebased tree
(close.py step 5, `G.run_all`), so a wrongful drop surfaces as a gate failure that routes to
triage or the existing park path; the dropped commits are recorded as a `finding` so the
conductor can audit them.

**That net does not exist for every close.** A `merge_request` with `merge.docs_only`
(`wt queue merge --docs-only`, SPEC-0234) returns its `PreparedClose` at close.py:409-426,
before step 5, so no gate ever runs; and `_dispatch_resolver` fires on any `rebase_conflict`
park, which happens at step 4 before that branch. Decision: **a declaration is honored only
when the close will re-gate.** For a docs-only merge request `verify_resolution` is called with
no declarations (`declared_dropped=()`), so a drop there fails verification as `is missing
after the resolution` exactly as today (`pre_head` restored, the existing park path). Docs-only
is the only gate-skipping close path today; the gating condition is a single helper
`_close_regates(state)` (False for a docs-only merge request) so a future gate-skipping path
has one place to be excluded. The docs-only allowlist alone is not accepted as the guard,
because a silently lost docs commit would merge with no check at all.

## Design

- **personas.py**: `CONFLICT_RESOLVER_SCHEMA` gains optional `dropped_commits` (array of
  objects, required `sha` and `reason`, strings with `minLength: 1` so an empty reason cannot
  satisfy the audit trail). `RESOLVER_INVARIANT` (personas.py:48-50) allows only `rebase
  --continue` today, yet taking the upstream side leaves nothing to commit and
  `--continue` refuses; so add `rebase --skip` to its allowed-git list and to step 5 of
  `personas/conflict_resolver.md`, stated as "only for a commit you then list in
  `dropped_commits`". The invariant and prompt also say: if a campaign commit's intent was already delivered a
  different way upstream and keeping it would be wrong, do not port it; list it (full or
  abbreviated sha from `git log <fork>..<pre-head>`) with a one-sentence reason; never use it
  to skip a conflict you could resolve.
- **gitops.verify_resolution(..., declared_dropped=())**: new keyword. Declared shas are
  normalized with `git rev-parse --verify <s>^{commit}` (failures are ignored) and intersected
  with `old_commits`. In the loop over `old_commits`, before the final `fail(... is missing
  ...)` return (paired commits already `continue` earlier, gitops.py:514-519, so a declared
  commit reaching here is necessarily unpaired): if `c in declared`, append `_commit_entry(worktree, c)`
  plus `reason` to a new `ResolutionCheck.superseded` list and `continue`. A superseded commit
  is not added to `guarded` (nothing of it is expected in the branch).
  `ResolutionCheck` gains `superseded: list[dict]` (default empty).
- **queue._do_resolve** passes `verdict.get("dropped_commits")` shas, or none when
  `not _close_regates(state)`; `res.resolution` gains
  `superseded`. **`_apply_resolve`** adds `superseded_commits` to the `rebase_resolved` event
  (schemas.py lists it; `render.rebase_resolved_text` appends `; K superseded`, old events
  unchanged) and, when non-empty, a `finding` (`persona: conflict_resolver`, `severity:
  major`, `gate: rebase`, title `resolver dropped campaign commits`, detail listing sha,
  subject, reason; `files` = the files the dropped commits touched) so the drop is visible in
  the review and report. The `events.py` projection of `rebase_resolved` (events.py:284) is
  left unchanged: the count is carried by the event itself and the finding, not the projection.
- Gate dependence: `closing.resolve` is cleared and the close redoes `prepare_close`, whose
  step 5 runs `G.run_all`, for every non-docs-only close. Nothing here may add a path from a
  declared drop to a merge without that re-gate (docs-only closes ignore declarations, above);
  the tests below pin both.

## Alternatives considered

- **Accept any missing commit when gates pass.** Rejected: silently loses intended work whose
  tests happen not to cover it; the declaration forces the resolver to own the call.
- **Compute "superseded" by diffing touched lines.** Rejected: heuristic, and the superseding
  edit differs by definition.
- **Park for a human on any non-reverse-applicable drop.** Rejected: the observed case was
  valid and the gates plus the audit finding are sufficient.

## Acceptance criteria

- [x] The verdict schema accepts `dropped_commits` and rejects entries without `sha` or
      `reason`; the invariant and persona prompt describe the field.
- [x] A non-empty campaign commit that is unpaired, not in `git cherry`, not reverse-applicable
      and declared passes verification and appears in `ResolutionCheck.superseded`.
- [x] The same commit undeclared still fails `is missing after the resolution`; a declared sha
      that is not an old campaign commit has no effect; all other checks are unchanged.
- [x] `rebase_resolved` carries `superseded_commits` and a `major` finding names each dropped
      commit with its reason; old events render unchanged.
- [x] After a resolution with superseded commits the close runs the gates before merging; a
      failing gate does not merge.
- [x] For a `--docs-only` merge request, declarations are ignored: a declared drop fails
      verification as `is missing after the resolution` and nothing is superseded.
- [x] `rebase --skip` is in the resolver's allowed git and in the persona prompt (only for a
      declared drop); an empty `sha` or `reason` is rejected by the schema.
- [x] The Test plan passes and `uv run pytest` is green.

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
  - `test_docs_only_merge_request_ignores_declaration`: a docs-only merge-request campaign
    with a declared, otherwise-missing commit fails with `is missing`, `pre_head` restored.
  - Persona/schema tests for the new field (including empty `sha`/`reason` rejected) and prompt
    text (`rebase --skip` in the invariant and step 5); render test for `; K superseded`.
- **Sabotage checks:** accepting every `<` commit fails `superseded_undeclared`; ignoring the
  declaration fails `test_superseded_commit_declared_verified`; treating declared as
  skipping the re-gate fails the close integration test; omitting the finding fails the event
  test; dropping the `_close_regates` guard fails the docs-only test.
- **Manual verification:** next real close that supersedes a commit shows `+K superseded` and
  the finding, then a green re-gate.
- **Regression guard:** full `uv run pytest`; the SPEC-0221/0252 resolver cases unchanged.

### Test record (2026-10-26)

- `uv run pytest tests/test_agentic_resolver.py tests/test_agentic_personas.py`: new tests
  `test_superseded_commit_declared_verified`, `FAILS[superseded_undeclared]` and
  `[superseded_unknown_sha_declared]`, `test_declared_but_present_commit_ignored`,
  `test_declared_unknown_sha_ignored`, the verify_resolution unit cases, the failing-gate
  close integration test, `test_docs_only_merge_request_ignores_declaration`,
  `test_close_regates_only_without_docs_only_merge_request`, the persona/schema test and the
  render test all pass.
- Full `uv run pytest`: 3045 passed, 3 skipped. `python3 tools/spec_lint.py --write-ledger` then
  `python3 tools/spec_lint.py`: exit 0.
- Manual verification (next real close that supersedes a commit) is left to that close.

### As shipped

- `superseded_commits` is **not** added to `EVENT_KEYS["rebase_resolved"]` (schemas.py): that
  list is the *required* keys, so listing it would flag every older `rebase_resolved` record as
  invalid. It is an additive payload key like `profile_sources`; old events render unchanged.
- `ResolutionCheck.superseded` entries are `{sha, subject, reason}`; the finding's `files` are
  computed by the new `gitops.commit_files` (paths the dropped commits touched).
- `_close_regates` and `_declared_drops` live in queue.py; a docs-only merge request passes no
  declarations to `verify_resolution`.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; `uv run pytest` passes.
- [x] Spec body updated to match what shipped; ledger regenerated.

## Open questions

None.
