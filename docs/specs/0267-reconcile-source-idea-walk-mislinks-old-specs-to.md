---
id: SPEC-0267
title: "Reconcile source_idea walk verifies idea identity before linking"
status: accepted
owner: user
created: 2026-10-27
source_idea: IDEA-357
kind: feature
---

## Context

Promoted from idea `IDEA-357` (Reconcile source_idea walk mislinks old specs to unrelated ideas: IDEA ids were reused after ideas-file rotation).

## Context

IDEA-357. SPEC-0257 made `wt spec reconcile` walk specs' `source_idea` and link/advance the named idea. IDEA ids were reused after the ideas-file rotation (`ideas.org`, `ideas-20261025.org`, `ideas-20261025-2.org`, `ideas-archive.org`), so ~95 specs older than SPEC-0200 name ids that now belong to unrelated ideas (e.g. SPEC-0138 `source_idea: IDEA-312`, now `wtfug:legacy-SPEC-0220 ...`). `--apply` would write wrong `:SPEC:` links and advance/archive those ideas. The walk resolves ids via `by_id` in `reconcile_ideas` (`src/wt/specs.py`) with no identity check.

## Goals / Non-goals

**Goals**
- `wt spec reconcile` (dry run and `--apply`) never links, advances or archives an idea on the strength of a `source_idea` id that has since been reused by an unrelated idea.
- Suspect pairs are reported as `flag-mismatch` (visible, no write).
- Recent legitimate links (spec created after its idea) behave exactly as in SPEC-0257.

**Non-goals**
- Recording the idea's stable org `:ID:` UUID on specs: `source_idea` is a free string, the schema has `additionalProperties: false`, and the idea `:ID:` property in use *is* the reusable `IDEA-NNN`; a new frontmatter key is a separate spec if ever wanted.
- Repairing the ~95 old specs' `source_idea` values, or changing the already-linked (`:SPEC:` set) path.

## Decision

The `source_idea` walk links an unlinked idea to a spec only if the idea passes an **identity check**: its `:CREATED:` date is on or before the spec's `created` date (an idea cannot be promoted into a spec that predates it). Otherwise the pair becomes `flag-mismatch` with a detail naming both dates, and nothing is written. An idea with no parseable `:CREATED:` is unverifiable and is also flagged. The existing "already linked to another spec" case stays `flag-conflict`.

## Design

- `src/wt/specs.py:_spec_claims` additionally returns each claimant spec's `created` date (alongside id and status).
- `src/wt/specs.py:reconcile_ideas`, in the `for idea_id, specs in sorted(claims.items())` loop, in the `elif len(live) == 1` branch: before `work.append((task, live[0], True))`, call a new helper `_idea_predates_spec(task, spec_created)` using `report.parse_inactive_stamp(task.properties.get("CREATED"))`. False -> `plan.append((idea_id, live[0], "flag-mismatch", f"idea created {c} is after spec created {s}; id likely reused"))` (or "idea has no :CREATED:"), when the spec is in scope.
- The same check gates the `flag-ambiguous` branch: claimants that fail the check are dropped from the live list first, so a reused id with one old and one genuine claimant resolves to the genuine one.
- Docs: add `flag-mismatch` to the action list in the `reconcile_ideas` docstring and to the CLI output legend; it is report-only and exit-code neutral like other `flag-*` rows.

## Alternatives considered

- **Heading/title similarity** - fuzzy, and headings are edited after capture.
- **Stable `:ID:` UUID on the spec** - exact, but needs a schema key and a backfill of ~95 specs that cannot be inferred; deferred.
- **Skip all specs < SPEC-0200** - arbitrary cut-off that would also hide true links.

## Acceptance criteria

- [ ] An unlinked idea whose `:CREATED:` is after the claiming spec's `created` yields `flag-mismatch`; dry run and `--apply` write no `:SPEC:`, state change, archive, or spec edit for it.
- [ ] An idea created on or before the spec's `created` date still yields `link` (+ `advance`) and `--apply` writes them.
- [ ] An idea with no parseable `:CREATED:` yields `flag-mismatch`, not `link`.
- [ ] A reused id claimed by one mismatching and one genuine live spec resolves to the genuine spec (no `flag-ambiguous`).
- [ ] `flag-mismatch` rows are rendered by `wt spec reconcile` and documented; scoped (`spec_ids`) runs only report mismatches for in-scope specs.
- [ ] Full `uv run pytest` and `python3 tools/spec_lint.py` pass.

## Test plan

- **Automated (`tests/test_reconcile.py`):**
  - `test_reused_id_is_flagged_not_linked`: real collision shape - spec created 2026-10-10 with `source_idea: IDEA-312`, idea IDEA-312 `:CREATED: [2026-10-25 Sun 09:00]`, no `:SPEC:`. Assert `flag-mismatch`; after `--apply` the org file and spec bytes are unchanged and the idea state is not PROMOTED.
  - `test_recent_legit_link_still_links`: spec created 2026-10-26, idea created 2026-10-25 -> `link` and, with `--apply`, `:SPEC:` written and state advanced; same-day created counts as legit.
  - `test_missing_created_is_flagged`, `test_reused_id_two_claimants_picks_genuine`, `test_scoped_run_only_reports_in_scope_mismatch`.
  - **Sabotage check:** temporarily make `_idea_predates_spec` always return True; confirm `test_reused_id_is_flagged_not_linked` fails; restore.
- **Manual:** `uv run wt spec reconcile` (dry run, in the worktree against real data) shows the ~95 SPEC-0138-style rows as `flag-mismatch` and no `link` rows for specs < SPEC-0200.
- **Regression guard:** existing `tests/test_reconcile.py` SPEC-0257 tests are updated only to give their fixture ideas/specs consistent dates; full `uv run pytest`.

## Rollout / migration

Code-only; no data migration. Until merged, do not run unscoped `wt spec reconcile --apply`.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

None.
