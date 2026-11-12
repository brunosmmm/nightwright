---
id: SPEC-0570
title: Repair the IDEA-1024 / SPEC-0458 provenance link
status: done
owner: user
created: 2026-11-12
updated: 2026-11-12
kind: feature
tags: [specs, provenance]
source_idea: IDEA-1161
---

## Context

IDEA-1024 is "Periodic deterministic resolution of queue dependency conflicts". Two records say its spec is SPEC-0458, "The run tail renders a triage and an implementer result as cards":

- IDEA-1024's `:SPEC:` property (the private ideas file).
- `source_idea: IDEA-1024` in `docs/specs/0458-tail-renders-triage-and-implementer-cards.md`, added by commit `50bd36b9` ("record source_idea IDEA-1024 in frontmatter").

Neither is right. The history shows which side is wrong:

- IDEA-1024's campaign wrote its spec as SPEC-0458. A different spec, the tail-cards spec (commit `823355e2`, 2026-11-07, authored directly with no idea), landed on `main` under the same number first.
- The campaign's spec was renumbered to **SPEC-0463** so that SPEC-0458 on `main` stays the tail-cards spec. `docs/specs/0463-periodic-deterministic-resolution-of-queue.md` says so (line 215), its title is IDEA-1024's heading, and its frontmatter already has `source_idea: IDEA-1024`. [SPEC-0470](./0470-close-follows-a-renamed-spec.md) records the same rename.
- IDEA-1024's `:SPEC:` was never updated after the rename, so it still names SPEC-0458. Commit `50bd36b9` then trusted that stale `:SPEC:` and stamped `source_idea: IDEA-1024` onto SPEC-0458, which has no idea behind it.

So both pointers are the same single mistake: a stale `:SPEC:` that was copied into a spec. SPEC-0463 is the correct spec for IDEA-1024 and SPEC-0458 has no source idea.

## Goals / Non-goals

**Goals**

- IDEA-1024's `:SPEC:` names SPEC-0463.
- SPEC-0458 no longer claims IDEA-1024 as its source idea.
- A repo test pins the corrected pair, so the stamp cannot silently return.

**Non-goals**

- No change to the body, status or any other frontmatter of SPEC-0458 or SPEC-0463. The tail-cards design and the periodic-resolver design are both correct and shipped.
- No new lint rule. Several specs legitimately share one `source_idea` (an epic and its children), so "one idea, one spec" is not an invariant that `tools/spec_lint.py` could enforce.
- No edit to `docs/LEDGER.md` by hand. It is generated and does not show `source_idea`.
- No change to how `nw` stamps `source_idea`.

## Decision

Fix both sides, because both carry the same stale value. In the repo, delete the `source_idea: IDEA-1024` line from SPEC-0458: its true origin is a direct change with no idea, and the schema makes `source_idea` optional. On the machine that holds the ideas file, repoint IDEA-1024's `:SPEC:` to SPEC-0463 with the identity-checked `nw idea spec-link` (SPEC-0281). The change to SPEC-0458 is a provenance-metadata correction, not a design change, so it does not need a superseding spec ([AGENTS.md](../../AGENTS.md) "Changing a past decision" concerns decisions, not a wrong back-pointer).

## Design

1. **SPEC-0458 frontmatter.** Remove the line `source_idea: IDEA-1024` from `docs/specs/0458-tail-renders-triage-and-implementer-cards.md`. Touch nothing else in the file, including `updated`.
2. **IDEA-1024 `:SPEC:`.** Run the dry run first, then the write:

   ```bash
   nw idea spec-link IDEA-1024 SPEC-0463
   nw idea spec-link IDEA-1024 SPEC-0463 --yes --heading "Periodic deterministic"
   ```

   `spec-link` refuses a spec whose `source_idea` names another idea. SPEC-0463 names IDEA-1024, so it passes. This must run in the checkout that holds the ideas file; the ideas file is not in the repo and `spec-link` never edits spec files.
3. **Regression test.** New `tests/test_spec_source_idea_links.py` carries exactly one module-level marker, `pytestmark = pytest.mark.fast` (the file name matches no other SPEC-0490 tier, so it is `fast`). It reads the frontmatter of the two spec files with the same YAML loading `tools/spec_lint.py` uses and asserts:
   - SPEC-0463 has `source_idea: IDEA-1024` and a title that starts with "Periodic deterministic resolution".
   - SPEC-0458 has no `source_idea` key.
4. **Ledger.** Run `python3 tools/spec_lint.py --write-ledger` so the generated block picks up SPEC-0570. The SPEC-0458 and SPEC-0463 rows do not change.

## Alternatives considered

- **Repoint SPEC-0458's `source_idea` to the tail-cards idea.** Rejected: no idea exists for it. The commit that created SPEC-0458 was a direct change, and a search for the idea finds only unrelated ideas (IDEA-1012, IDEA-654).
- **Make the ideas side canonical and renumber SPEC-0463 back to SPEC-0458.** Rejected: SPEC-0458 was on `main` first, [SPEC-0470](./0470-close-follows-a-renamed-spec.md) and the code refer to it as the tail-cards spec, and renumbering a done spec breaks every link to it.
- **Fix only the idea's `:SPEC:`.** Rejected: SPEC-0458 would keep a back-pointer to an idea it did not come from, and `nw idea spec-link IDEA-1024 SPEC-0458` would still pass its identity check.
- **Add a lint rule that a spec's `source_idea` heading must match its title.** Rejected: the ideas file is machine-local, so the rule cannot run in the pytest-gated lint, and titles legitimately differ from idea headings.

## Acceptance criteria

- [x] `docs/specs/0458-tail-renders-triage-and-implementer-cards.md` has no `source_idea` key, and `git diff` against its previous content shows only that one removed line.
- [x] `docs/specs/0463-periodic-deterministic-resolution-of-queue.md` is unchanged and still has `source_idea: IDEA-1024`.
- [x] `nw idea show IDEA-1024` reports `spec: SPEC-0463`.
- [x] `tests/test_spec_source_idea_links.py` exists, has exactly one module-level `pytestmark = pytest.mark.fast`, and passes.
- [x] `python3 tools/spec_lint.py` exits 0 and `docs/LEDGER.md` is current.
- [x] `python3 tools/test_tiers.py --check` exits 0 when `tools/test_tiers.py` exists.

## Test plan

- **Automated tests:** `tests/test_spec_source_idea_links.py`, tier `fast` (`pytestmark = pytest.mark.fast`).
  - `test_idea_1024_spec_is_the_periodic_resolver`: SPEC-0463 has `source_idea == "IDEA-1024"` and its title starts with "Periodic deterministic resolution".
  - `test_tail_cards_spec_has_no_source_idea`: SPEC-0458's frontmatter has no `source_idea` key.

  The test reads the two files directly from `docs/specs/` and needs no fixtures, no ideas file and no `nw` import, so it is deterministic in any checkout. Run it with `python3 tools/affected_tests.py --run uv run pytest -q`. Also run `python3 tools/test_tiers.py --check` (exits 0) when `tools/test_tiers.py` exists.
- **Manual verification** (on the machine with the ideas file):
  1. `nw idea show IDEA-1024` before the fix shows `spec: SPEC-0458`.
  2. `nw idea spec-link IDEA-1024 SPEC-0463` (dry run) shows the proposed change and no identity refusal.
  3. `nw idea spec-link IDEA-1024 SPEC-0463 --yes --heading "Periodic deterministic"` writes it.
  4. `nw idea show IDEA-1024` now shows `spec: SPEC-0463`.
  5. `python3 tools/spec_lint.py --check-source-ideas` lists no problem for SPEC-0458 or SPEC-0463.
- **Regression guard:** `python3 tools/spec_lint.py` exits 0 (frontmatter valid, ledger current); the affected tests pass at the close gate. `git diff main -- docs/specs/0458-*.md docs/specs/0463-*.md` shows only the removed `source_idea` line in SPEC-0458.

### Test record (2026-11-12)

- `uv run pytest -q tests/test_spec_source_idea_links.py`: 2 passed. `python3 tools/test_tiers.py --check`: exit 0. `python3 tools/spec_lint.py`: exit 0, ledger current. Full suite left to gating.
- `git diff main -- docs/specs/0458-*.md docs/specs/0463-*.md` shows only the removed `source_idea: IDEA-1024` line in SPEC-0458.
- Manual: `nw idea spec-link IDEA-1024 SPEC-0463` dry run showed `:SPEC: SPEC-0458 -> SPEC-0463` with no identity refusal. The write was refused for the campaign agent (`NW_PERSONA_RUN=1`: "refusing to write the org from a campaign agent"), so `nw idea show IDEA-1024` still reports `spec: SPEC-0458`.

### As shipped (2026-11-12)

Repo side shipped (SPEC-0458 line removed, regression test, ledger). The ideas-file write is a pending operator step: run `nw idea spec-link IDEA-1024 SPEC-0463 --yes --heading "Periodic deterministic"` on the ideas machine, then `nw idea show IDEA-1024`. The spec stays `in-progress` until then.

**Update (2026-11-12): AC waived as verified, not executed from the worktree.** The ideas file is machine-local, so `nw idea show IDEA-1024` cannot be run from the campaign worktree. Evidence: the Meta-Tools steward ran `nw idea spec-link IDEA-1024 SPEC-0463 --yes --heading "Periodic deterministic"` in the ideas checkout on 2026-11-12 at 09:46, and `nw idea show IDEA-1024` then printed `spec: SPEC-0463`; the conductor re-confirmed it; the human waived the AC through escalation ESC-0070 (choice 1, event 13016). Definition of done: all items are ticked; "No regressions" rests on the affected tests recorded as passing in the Test record above (the full suite is left to the queue close gate and the main sentinel).

## Rollout / migration

1. Land the SPEC-0458 frontmatter edit, the new test and the regenerated ledger together.
2. Run the `nw idea spec-link` write once in the checkout that holds the ideas file. It is idempotent: a second run reports the link already set.
3. Run the manual verification steps above and record the result in the spec's "As shipped" note when it moves to `done`.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; affected tests pass; manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

- None. The wrong side was decided from the rename history above: both pointers are the stale `:SPEC:` copy, and SPEC-0463 is the right spec.
