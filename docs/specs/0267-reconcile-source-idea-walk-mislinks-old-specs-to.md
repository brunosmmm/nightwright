---
id: SPEC-0267
title: "Reconcile source_idea walk verifies idea identity before linking"
status: done
owner: user
created: 2026-10-27
source_idea: IDEA-357
kind: feature
---

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

The `source_idea` walk links an unlinked idea to a spec only if the idea passes an **identity check**: its `:CREATED:` date is on or before the spec's `created` date (an idea cannot be promoted into a spec that predates it). Otherwise the pair becomes `flag-mismatch` with a detail naming both dates, and nothing is written. An idea with no parseable `:CREATED:` is unverifiable and is also flagged. Dates are compared as `stamp.date() <= spec_created` (the `parse_inactive_stamp` datetime is reduced to a date; the spec's YAML `created` is normalised from `date` or `YYYY-MM-DD` string; an unparseable spec `created` is also flagged). Id lookup is `id -> [ideas]`: when several ideas carry one id (rotated/archive org files), keep only candidates passing the date check and take the most recently created; if none pass, `flag-mismatch`; if several pass with the same `:CREATED:`, `flag-ambiguous`. No write in either flag case. The existing "already linked to another spec" case stays `flag-conflict`.

## Design

- `src/wt/specs.py:_spec_claims` additionally returns each claimant spec's `created` date (alongside id and status).
- `src/wt/specs.py:reconcile_ideas`: `by_id` becomes `{id: [tasks]}`; a helper `_pick_idea(cands, spec_created)` returns (task, None) or (None, reason).
- `src/wt/specs.py:reconcile_ideas`, in the `for idea_id, specs in sorted(claims.items())` loop, in the `elif len(live) == 1` branch: before `work.append((task, live[0], True))`, call a new helper `_idea_predates_spec(task, spec_created)` using `report.parse_inactive_stamp(task.properties.get("CREATED"))`. False -> `plan.append((idea_id, live[0], "flag-mismatch", f"idea created {c} is after spec created {s}; id likely reused"))` (or "idea has no :CREATED:"), when the spec is in scope.
- The same check gates the `flag-ambiguous` branch: claimants that fail the check are dropped from the live list first, and each dropped in-scope claimant gets its own `flag-mismatch` row; a reused id with one old and one genuine claimant resolves to the genuine one (`link` + a `flag-mismatch` for the old). If every claimant fails, only `flag-mismatch` rows are emitted (never `flag-ambiguous`).
- `src/wt/agentic/queue.py` close hook (~line 1799): `flag-mismatch` becomes a `minor` finding "idea reconcile mismatch".
- `src/wt/cli.py` reconcile `marks` dict: add a glyph/label for `flag-mismatch`.
- Docs: add `flag-mismatch` to the action list in the `reconcile_ideas` docstring and to the CLI output legend; it is report-only and exit-code neutral like other `flag-*` rows.

## Alternatives considered

- **Heading/title similarity** - fuzzy, and headings are edited after capture.
- **Stable `:ID:` UUID on the spec** - exact, but needs a schema key and a backfill of ~95 specs that cannot be inferred; deferred.
- **Skip all specs < SPEC-0200** - arbitrary cut-off that would also hide true links.

## Acceptance criteria

- [x] An unlinked idea whose `:CREATED:` is after the claiming spec's `created` yields `flag-mismatch`; dry run and `--apply` write no `:SPEC:`, state change, archive, or spec edit for it.
- [x] An idea created on or before the spec's `created` date still yields `link` (+ `advance`) and `--apply` writes them.
- [x] An idea with no parseable `:CREATED:` yields `flag-mismatch`, not `link`.
- [x] A reused id claimed by one mismatching and one genuine live spec resolves to the genuine spec (no `flag-ambiguous`).
- [x] `flag-mismatch` rows are rendered by `wt spec reconcile` and documented; scoped (`spec_ids`) runs only report mismatches for in-scope specs.
- [x] A source_idea id carried by several ideas (across org files) resolves only to a date-valid, most-recently-created candidate, else flags with no write.
- [x] The close hook reports `flag-mismatch` as a minor finding.
- [x] Full `uv run pytest` and `python3 tools/spec_lint.py` pass.

## Test plan

- **Automated (`tests/test_reconcile.py`):**
  - `test_reused_id_is_flagged_not_linked`: real collision shape - spec created 2026-10-10 with `source_idea: IDEA-312`, idea IDEA-312 `:CREATED: [2026-10-25 Sun 09:00]`, no `:SPEC:`. Assert `flag-mismatch`; after `--apply` the org file and spec bytes are unchanged and the idea state is not PROMOTED.
  - `test_recent_legit_link_still_links`: spec created 2026-10-26, idea created 2026-10-25 -> `link` and, with `--apply`, `:SPEC:` written and state advanced; same-day created counts as legit.
  - `test_missing_created_is_flagged`, `test_reused_id_two_claimants_picks_genuine` (asserts the `link` for the genuine spec plus `flag-mismatch` for the dropped one, and that all-fail yields `flag-mismatch`, not `flag-ambiguous`), `test_duplicate_idea_ids_resolve_or_flag` (two ideas sharing an id across two org files: new spec -> the new idea, never the old; none date-valid -> flag), a close-hook test that `flag-mismatch` yields a minor finding, a CliRunner test that dry-run output contains the `flag-mismatch` glyph/label, `test_scoped_run_only_reports_in_scope_mismatch`.
  - **Sabotage check:** temporarily make `_idea_predates_spec` always return True; confirm `test_reused_id_is_flagged_not_linked` fails; restore.
- **Manual:** `uv run wt spec reconcile` (dry run, in the worktree against real data) shows the ~95 SPEC-0138-style rows as `flag-mismatch` or `flag-conflict`, and no `link`/`advance` rows for specs < SPEC-0200.
- **Regression guard:** existing `tests/test_reconcile.py` SPEC-0257 tests are updated only to give their fixture ideas/specs consistent dates; full `uv run pytest`.

### Test record (2026-10-27)

- `uv run pytest -q tests/test_reconcile.py`: 91 passed, including the new
  `test_duplicate_idea_ids_done_spec_advances_and_archives_the_right_one` and the added
  state assertions (chosen duplicate SPECCED, twin still IDEA, both directions).
- Sabotage: reverting the `org_write._fresh_idea` fix makes the new done-spec test fail with
  `ambiguous selector`; restored.
- Full `uv run pytest -q -x` and `python3 tools/spec_lint.py` pass (see gate results).

### Test record (2026-10-27, review follow-up)

- Manual dry run, `uv run wt spec reconcile` in the worktree against real data: exit 0, 189 rows;
  97 `flag-mismatch` (`≠`) and 28 `flag-conflict` (`!`) rows, e.g. `≠ IDEA-312 SPEC-0138 idea
  created 2026-10-25 is after spec created 2026-08-30; id likely reused`. The only `link`/`advance`
  rows for specs < SPEC-0200 are SPEC-0195..0199 (IDEA-049/050/053/055/058): genuine same-day
  links (spec created 2026-10-25, their topics match the idea titles), not reused ids.
- Sabotage, as named in the plan: `_idea_predates_spec` always `(True, "")` makes 16 tests in
  `tests/test_reconcile.py` fail (including `test_reused_id_is_flagged_not_linked`); restored.
- Review follow-ups, each with a test that fails against the previous code (checked by
  reverting): `drifted` is keyed by `(idea_id, spec_id)` so drift on one duplicate-id twin leaves
  the other's link/advance alone (`test_drift_on_one_duplicate_id_twin_leaves_the_other_alone`);
  the close hook's `idea_reconciled` snapshot keeps every `(state, SPEC)` per id and reports the
  one that moved (`test_merge_reconcile_event_describes_the_moved_idea_not_its_duplicate_id_twin`).
  Added coverage: quoted-string/datetime spec `created`, `_as_date`, duplicate id with only
  superseded/rejected claimants, `_fresh_idea` re-raise (zero and several hits).

### As shipped (2026-10-27)

`org_write.set_state` auto-archives a terminal idea by re-finding it; with an `:ID:` carried by
several ideas that bare-id lookup raised "ambiguous selector" after the keyword was written.
It now falls back (`_fresh_idea`) to the idea's file + heading, so the pinned advance to
PROMOTED/SHIPPED archives the right idea and leaves its twin untouched. Non-duplicate behaviour
is unchanged.

## Rollout / migration

Code-only; no data migration. Until merged, do not run unscoped `wt spec reconcile --apply`.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

None.
