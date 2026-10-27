---
id: SPEC-0276
title: "Batch queue add: many specs and ideas at once, in dependency order"
status: accepted
kind: feature
owner: user
created: 2026-10-27
source_idea: IDEA-071
parent: SPEC-0169
tags: [agentic, queue, cli]
---

## Context

Promoted from idea `IDEA-071` (user goal 2026-10-25: load the ideas into the queue and let
them be implemented towards the MVP).

Today `wt queue add` (`src/wt/agentic/cli_queue.py:queue_add`) takes exactly one id. A
`SPEC-NNNN` goes through `queue.plan_add` / `queue.apply_add`; an `IDEA-NNN` goes through
`queue.plan_add_idea` / `queue.apply_add_idea` (SPEC-0209). Loading twenty items takes twenty
commands, and nothing checks the set as a whole:

- `queue.backlog_readiness` already blocks a queued spec until every `depends_on` is `done` or
  `superseded`. A dependency that is `accepted` but **not queued** therefore blocks its
  dependent forever, and nobody is told at add time.
- A dependency cycle inside the set only shows up later, as two campaigns that never become
  ready.
- There is no way to say "everything under epic SPEC-0169 that is ready".

## Goals / Non-goals

**Goals**
- `wt queue add` accepts several ids, `--from-file PATH` and `--epic SPEC-NNNN`, and plans
  them as one batch (dry-run by default, `--yes` writes).
- The batch is created in dependency order. For each item it reports the plan or the
  refusal, and any `depends_on` that would block the item forever.
- `--yes` is all-or-nothing unless `--skip-invalid` is given.

**Non-goals**
- A new admission key or batch-aware scheduling. Once the set is complete,
  `backlog_readiness` and `admission_key` already order admission correctly.
- Dependencies between ideas (ideas carry no `depends_on`).
- Estimating real cost. The batch reports only the sum of the profile caps.

## Decision

Add a pure planner, `queue.plan_add_batch(cfg, ids, *, project, profile, base, force,
priority) -> dict`. It calls the existing single-item planners and adds the set-level checks.
Add `queue.apply_add_batch(cfg, batch, *, skip_invalid)`, which calls the existing appliers
in batch order. The CLI only gathers ids. No new campaign state is stored.

## Design

### Input (`cli_queue.queue_add`)

- `SPEC_ID` becomes `IDS...` (`nargs=-1`). Zero ids with no `--from-file` and no `--epic`
  is a usage error. A single positional id with no other source keeps today's exact path and
  output (`wt.queue.add.v1`), so existing scripts and the conductor skill are unaffected.
- `--from-file PATH` reads one id per line. Blank lines and any text after `#` are ignored.
- `--epic SPEC-NNNN` expands to:
  - (a) every internal spec whose frontmatter has `parent: SPEC-NNNN` and `status: accepted`;
  - (b) every open idea (`queue.idea_is_open`) whose `:EPIC:` property is `SPEC-NNNN` and
    that has no `:SPEC:`.
  Children with any other status are listed under `skipped` with their status and are never
  queued.
- Sources are concatenated in the order positional, file, epic, then de-duplicated; the first
  occurrence wins.
- `--skip-invalid` is a flag. `--priority`, `--profile`, `--base`, `--project` and `--force`
  apply to every item, as today.

### Planning (`queue.plan_add_batch`)

1. For each id, call `plan_add_idea` when `_IDEA_ID_RE` matches, else `plan_add`. A
   `ValueError` is caught into `{"id", "error"}`. Planning does not reserve campaign ids
   from `store.new_campaign_id`: `apply_add_batch` re-plans each item at write time, the
   same way the single-item path re-checks live state in `apply_add`.
2. **Order.** Specs are sorted topologically over the `depends_on` edges that have both ends
   in the batch (Kahn's algorithm; ties go by input position). Ideas follow the specs, in
   input order. Every spec in a cycle gets `error: "dependency cycle: A -> B -> A"`.
3. **Dangling dependencies.** For each planned spec, a `depends_on` id goes into the item's
   `would_block` list as `"SPEC-X is accepted and not queued"` when all of these hold:
   - it is not `done` or `superseded` (`DEPENDENCY_SATISFIED`);
   - it is not in the batch;
   - it has no active campaign (`store.active_campaign_for_spec`).
   This is a warning, not an error, because the human may queue the dependency later.
4. Result: `{"schema": "wt.queue.add.batch.v1", "items": [{position, id, kind, plan|error,
   would_block}], "skipped": [{id, reason}], "ok": n, "errors": n, "cap_usd_total": <sum of
   the plans' cap_usd>}`.

### Writing (`queue.apply_add_batch`)

- If any item has an `error` and `skip_invalid` is false, raise `ValueError("N of M items
  cannot be queued; fix them or pass --skip-invalid")` and write nothing.
- Otherwise, in batch order, re-plan and apply each valid item (`apply_add` or
  `apply_add_idea`). A failure at write time is a race (someone queued the item
  meanwhile): it is recorded on that item as `error` and the loop continues, and earlier
  items stay written. Each written item gets `"written": true` in the payload.

### Output

Human output is one table with position, id, kind, profile, cap, and the `error` or
`would_block` text. A totals line follows (`N to queue · M refused · $X cap total`), then the
`_NOT_WRITTEN` hint on a dry run. `--json` emits `wt.queue.add.batch.v1`.

## Acceptance criteria

- [ ] `wt queue add A B C`, `--from-file` and `--epic` plan the same de-duplicated batch. A
      single positional id still emits `wt.queue.add.v1` unchanged.
- [ ] Specs are created in topological `depends_on` order, in input order among independent
      specs. Ideas come after specs. A cycle is reported on each spec in it and is never
      written.
- [ ] A `depends_on` that is not satisfied, not in the batch and not queued is listed in the
      item's `would_block`.
- [ ] `--yes` with any refused item writes nothing unless `--skip-invalid` is given; with it,
      only the valid items are written.
- [ ] `--epic` takes the accepted children and the open `:EPIC:` ideas without `:SPEC:`, and
      lists the other children under `skipped`.
- [ ] `--json` emits `wt.queue.add.batch.v1` with each item's plan or error and
      `cap_usd_total`.

## Test plan

New tests go in `tests/test_agentic_queue_cli.py` (CLI) and `tests/test_agentic_queue.py`
(planner). They use the existing fixture repos with tmp specs and a stubbed review store.

- `test_batch_topological_order`: SPEC-B has `depends_on: [SPEC-A]`; input `B A` gives items
  `A, B`, and `--yes` creates A's campaign before B's.
- `test_batch_cycle_refused`: with A↔B, both carry the cycle error and `--yes` writes nothing.
- `test_batch_would_block_dangling_dep`: B depends on C, which is accepted and not queued, so
  `would_block` names C. When C is queued separately first, `would_block` is empty.
- `test_batch_all_or_nothing` and `test_batch_skip_invalid`: a batch of three with one
  unreviewed spec creates no campaigns; with `--skip-invalid` it creates two.
- `test_batch_epic_expansion`: an epic with accepted, draft and done children plus an open
  `:EPIC:` idea. The accepted child and the idea are queued; the draft and done children are
  in `skipped`.
- `test_batch_from_file_comments_and_dedup`, and `test_single_id_output_unchanged` (golden
  `wt.queue.add.v1` keys).
- **Sabotage check:**
  - Replace the topological sort with input order: `test_batch_topological_order` must fail.
  - Drop the `skip_invalid` guard: `test_batch_all_or_nothing` must fail.
- Regression guard: full `uv run pytest`.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

(none)
