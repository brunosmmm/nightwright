---
id: SPEC-0470
title: Close follows a spec file the branch renamed
status: accepted
owner: user
created: 2026-11-08
updated: 2026-11-08
tags: [agentic]
depends_on: [SPEC-0186, SPEC-0175]
---

## Context

A campaign records `spec.rel_path` when it opens. An implementer may renumber the spec,
which git records as a rename, and the campaign still points at the old path. Close then
reads a file that is not there and diffs the accepted text against an empty document, so
a campaign that already passed review goes to triage instead of merging. IDEA-1024 did
this on 2026-11-08: SPEC-0458 was renamed to SPEC-0463, review had passed, and close
reported `spec text changed outside allowed sections` with the new side reduced to `{}`.

## Decision

When the recorded path is missing, close uses the newest `docs/specs/` rename of that
path whose destination is still in the worktree. The campaign's `spec.rel_path` follows
it, and so does `spec_id` when the new file has an id. A path that is still on disk is
never retargeted. A gate triage that is in flight for that missing path goes back to
CLOSING against the renamed file instead of opening another implement round.

## Design

`follow_renamed_spec` reads `git log -M --diff-filter=R --name-status` for `docs/specs/`.
`adopt_renamed_spec` writes the new path onto the campaign. `prepare_close` calls it
before the spec-change guard. The queue's gate-triage apply calls it before the triager's
verdict is routed, so a run that already started for the missing path does not spend a
loop.

## Acceptance criteria

- [x] A spec file that is present is not retargeted.
- [x] After the branch renames that file, the spec-change guard fails on the old path and
      passes once the campaign adopts the new path.
- [x] The adopted campaign records the new path and the id from the new file.
- [ ] A gate triage whose spec path was renamed returns to CLOSING without another
      implement round.

## Test plan

- [x] `tests/test_agentic_close.py::test_renamed_spec_is_what_close_checks`

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed.
- [ ] No regressions in the close tests that cover the spec guard.
- [ ] Spec body matches what shipped.
- [ ] `docs/LEDGER.md` regenerated.

## As shipped

`adopt_renamed_spec` runs in `prepare_close` and at the start of gate-triage apply.
A missing path with no rename is unchanged, and the guard still fails closed.
