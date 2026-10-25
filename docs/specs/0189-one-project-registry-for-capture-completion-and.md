---
id: SPEC-0189
title: "One project registry for capture, completion and promotion"
status: done
owner: user
created: 2026-10-25
updated: 2026-10-25
milestone: "M4: cli ergonomics"
kind: feature
tags: [projects, capture, cli]
source_idea: IDEA-010
---

## Context

Promoted from bug `IDEA-010` (wt projects add registers a project that idea capture still
calls unknown).

wt has two answers to "which projects exist":

- `rules.projects_payload` (`wt projects --json`, the TUI desk chooser) = mapping buckets ∪
  `outbox_targets` keys ∪ `Meta-Tools`.
- `rules.known_projects` = mapping buckets only (SPEC-0018). It is what the capture
  warning (`org_write._warn_unknown_associations`), `--project` shell completion, the
  `idea_extensions` check and the human `wt projects` listing use.

So right after `wt projects add comfort-assistant --repo … --yes`, `wt idea capture
--project comfort-assistant` warned "unknown project 'comfort-assistant'; known projects:
(none mapped yet)". Capturing with `--project Meta-Tools` warned too, because the built-in
project is not a mapping bucket. The workaround was an unrelated `wt map` call.

The same split hits promotion: `wt spec new --from-idea IDEA-044` (an idea on project
Meta-Tools) failed with "project 'Meta-Tools' … has no outbound target", needing
`--internal`, although Meta-Tools is wt's own internal project.

## Goals / Non-goals

**Goals**
- One registry function, `rules.registered_projects(cfg)` = sorted(mapping buckets ∪
  `outbox_targets` keys ∪ `Meta-Tools`), used by `projects_payload`, the capture warning,
  `--project` completion, the `idea_extensions` check and the human `wt projects` list.
- `wt spec new --from-idea` on an idea whose project is `Meta-Tools` promotes internally
  without `--internal`.
- `wt projects` (no `--json`) lists every registered project in a Rich table with topic
  count, outbound flag and research root, instead of only mapping buckets.

**Non-goals**
- Changing `known_projects` (mapping buckets; still what `wt map` maintains) or the
  `wt.projects.v1` JSON shape.
- Making the capture check blocking (it stays a warning, SPEC-0018).

## Decision

Add `INTERNAL_PROJECT = "Meta-Tools"` and `registered_projects(cfg)` to `wt.rules`; route
every "is this a project?" question through it; treat `INTERNAL_PROJECT` as internal in
`specs.promote_idea`'s routing.

## Design

- `rules.py`: `INTERNAL_PROJECT`; `registered_projects(cfg)`; `projects_payload` and
  `resolve_research_context` use them instead of literals.
- `org_write._warn_unknown_associations`: `registered_projects`; the warning lists the
  same set (`known projects: Meta-Tools, comfort-assistant`).
- `completion.complete_project`: `registered_projects` (so even a broken config completes
  `Meta-Tools`).
- `specs._canonical_seed_project` (Breakdown `project:` annotations, SPEC-0097):
  `registered_projects` instead of rebuilding `projects_payload`.
- `extension_schema._warn_unknown_extension_projects`: `registered_projects`.
- `specs` routing: `if internal or project is None or project == INTERNAL_PROJECT` →
  internal.
- `cli.projects_list_cmd` human mode: a `Projects` table over `projects_payload` — name
  (per-project colour, SPEC-0185), topics, outbound (✓ or —), research root + source (or
  the note when unresolved); a dim `no projects mapped to topics yet — see wt map` hint
  when there are no mapping buckets.

## Alternatives considered

- **Have `wt projects add` also write a mapping bucket** — two writes for one fact, and
  still leaves Meta-Tools out.
- **Make `known_projects` itself the union** — `wt map` and the topic counts rely on it
  meaning "mapping buckets"; a new name keeps both meanings explicit.

## Acceptance criteria

- [x] `wt idea capture --project <outbox-only project>` and `--project Meta-Tools` print
      no unknown-project warning; an unregistered name still warns and lists the
      registered set.
- [x] `--project` completion offers outbox-only projects and Meta-Tools.
- [x] An `idea_extensions` entry for an outbox-only project does not warn.
- [x] `wt spec new --from-idea` on a Meta-Tools idea promotes internally without
      `--internal`; an unconfigured project still errors.
- [x] `wt projects` lists outbox-only projects and Meta-Tools with topic count, outbound
      flag and research root; `--json` unchanged.
- [x] Breakdown `project:` annotations resolve against the same registry.
- [x] `uv run pytest` passes; `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests:** `tests/test_project_registry.py` — `registered_projects` union and
  order; capture warning absent for outbox-only and Meta-Tools, present (with the set)
  for an unknown name (CliRunner, stderr); `complete_project`; extension-schema warning;
  `promote_idea` on a Meta-Tools idea → internal; unconfigured project → ValueError;
  `wt projects` human table rows.
- **Manual verification:** `wt idea capture --project comfort-assistant …` dry case via a
  temp config; `wt projects` on the live config shows Meta-Tools and comfort-assistant.
- **Regression guard:** full `uv run pytest` (existing SPEC-0018 / SPEC-0095 / SPEC-0137
  tests unchanged).

### Executed (2026-10-25)

- `uv run pytest`: 1549 passed, 1 skipped. New `tests/test_project_registry.py` (11 tests);
  `test_assoc.test_cli_projects_empty` now also asserts Meta-Tools is listed;
  `test_completion.test_completers_soft_fail_on_broken_cfg` now expects `["Meta-Tools"]`
  (always registered) instead of `[]`.
- Live `wt projects` (worktree) lists Meta-Tools (internal, wt checkout) and
  comfort-assistant (outbound, its repo). The capture warning and Meta-Tools promotion
  are covered by the automated tests on temp configs, so the real ideas file was not
  touched with throwaway ideas.

## Rollout / migration

Built on branch `idea-010-projects` (worktree `/home/bruno/work/wt-idea-010`) and merged
through the conductor. No data migration.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

(none)
