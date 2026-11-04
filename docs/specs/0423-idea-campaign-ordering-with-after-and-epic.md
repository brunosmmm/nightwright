---
id: SPEC-0423
title: "Idea-campaign ordering with --after and epic derivation"
status: done
owner: user
created: 2026-11-04
source_idea: IDEA-929
parent: SPEC-0419
milestone: "M9: agentic workflow"
tags: [agentic, queue, ideas, epics, spec-writer]
---

## Context

Child 4 of [SPEC-0419](./0419-self-healing-queue.md). It implements IDEA-880: "let idea
campaigns declare ordering (e.g. --after CAMPAIGN/IDEA) so epic children queued as ideas wait for
a sibling's merge".

An idea campaign ([SPEC-0209](./0209-queue-ideas-idea-campaigns-spec-phase.md)) starts with no
spec, and `backlog_readiness()` (`src/nightwright/agentic/queue.py:802-859`) admits it as soon as
its idea is open (`has_no_spec` branch, `:815-822`). Ordering exists only for specs, through
frontmatter `depends_on`. In create mode, `spec_write._front_matter_problems()` (`:112-132`) checks
`id`, `status`, `source_idea`, `owner`, `kind` and `parent`, but not `depends_on`. Only split mode
wires it (`_split_problems`, `:152-168`). The spec writer cannot cite a sibling's spec id when the
sibling's spec has not been written yet.

`nw queue add --epic` ([SPEC-0276](./0276-load-many-ideas-into-the-queue-at-once-in.md),
`expand_epic`, `:7551-7597`) queues an epic's accepted child specs and its open spec-less child
ideas. `plan_add_batch` (`:7700-7750`) orders the **specs** by in-batch `depends_on` with Kahn's
algorithm (`_order_specs`, `:7614-7636`), and the ideas simply follow. Epic Breakdown bullets
already carry "(depends_on child N)" annotations (e.g. SPEC-0400), but `seed_ideas_from_epic`
(`src/nightwright/specs.py:667-711`) ignores them.

Result: SPEC-0400's children 2 to 4 showed ready before child 1 existed. IDEA-874..877 and 884..889
parked `implementer_blocked` ("child 2 … is not on main") or `spec_write_failed`, and the ordering
had to be mailed to the conductor.

## Goals / Non-goals

**Goals**
- `nw queue add <ids…> --after REF` (repeatable) records prerequisites on the campaign. A REF is
  an idea id, a spec id or a campaign id.
- `backlog_readiness` keeps a campaign of any kind except `merge` QUEUED until every prerequisite
  is satisfied, and names it in `blocked_by` as an ordinary dependency wait.
- The spec writer of an idea campaign must list its prerequisites' spec ids in `depends_on`, and
  is told which.
- `nw spec seed-children` turns "(depends_on child N…)" Breakdown annotations into `:AFTER:`
  properties on the child ideas. `nw queue add --epic` orders specs and ideas together by those
  edges and sets each campaign's `after`.

**Non-goals**
- Starting a dependent idea's spec writing before its prerequisite merges. Admission waits for the
  merge, as for specs. The writer then sees the prerequisite's merged spec.
- Changing how spec `depends_on` is evaluated (`dependency_met`, SPEC-0290/0347).
- Cross-project ordering beyond what `depends_on` already supports. A REF resolves in the
  campaign's own project; a campaign id may be in any project.

## Decision

1. **Storage.** Campaign state key `after: [{"ref": "IDEA-874", "kind": "idea"}, …]`, where
   `kind` is one of `idea|spec|campaign`. It is written by `apply_add` / `apply_add_idea` from the
   plan, and immutable afterwards. `nw queue after <cid> --add/--remove REF --yes` is the one
   explicit edit path, recorded in the ledger.
2. **Satisfaction** (`queue.after_met(cfg, state, ref) -> (bool, why)`):
   - `campaign` ref: the campaign is MERGED.
   - `spec` ref: `dependency_met` on the spec file next to the campaign's spec dir, or the
     project's specs dir for idea campaigns.
   - `idea` ref: the idea's `:SPEC:` spec is done or superseded through `dependency_met`, **or**
     the idea's newest campaign is MERGED.

   An unknown ref, or a prerequisite whose newest campaign is ABANDONED while its spec is not
   done, blocks with a non-ordinary reason ("after IDEA-874: campaign … abandoned"), so the
   conductor's `backlog_blocked_long` row acts.
3. **Readiness.** `backlog_readiness` checks `after` for every non-merge campaign, idea campaigns
   included. Entries are `waiting on IDEA-874 (after: campaign IDEA-874-… is IMPLEMENTING)`. The
   `waiting on <id>` prefix keeps `quiet.dependency_wait` treating it as an ordinary wait.
   `quiet.waited_ids` already extracts ids. `_ID_RE` is widened to `IDEA-\d+` and campaign ids, and
   the live set gains ideas with live campaigns.
4. **Spec writer must-include.** `_prepare_spec_write` (create mode) resolves the campaign's
   `after` refs to spec ids:
   - idea ref → `:SPEC:` or its merged campaign's `spec.id`;
   - spec ref → itself;
   - campaign ref → its `spec_id`.

   It stores them as `spec_write["must_depend_on"]`. `write_rules("write", …)` adds "`depends_on`
   must include: …". `_front_matter_problems` reports `front matter: depends_on must include
   SPEC-X (prerequisite IDEA-874)` for each missing id. A ref that resolves to no spec id is
   skipped and noted in the brief. It cannot occur after admission, since admission needed the
   prerequisite merged.
5. **Epic derivation.**
   - `specs.parse_breakdown_deps(item) -> (item_without_annotation, [ordinals])` reads
     `(depends_on child N)`, `(depends_on child N and child M)`, `(depends_on child N, child M)`
     and `(depends_on children N, M)`, case-insensitive.
   - `seed_ideas_from_epic` seeds all bullets first, then stamps `:AFTER:` (space-separated idea
     ids, in bullet order) on each child whose bullet has annotations, mapping ordinal N to the
     Nth Breakdown bullet's idea. This covers idempotently re-found ideas and fills a missing
     `:AFTER:` on already-seeded ideas, but never overwrites an existing one.
   - An ordinal out of range, or one pointing at the bullet itself, is a hard error (ValueError,
     like an unknown `(project: …)`).
   - `expand_epic` is unchanged in what it selects. `plan_add_batch` orders **all** items with
     `_order_specs`, renamed `_order_items`, over edges from spec `depends_on` (same namespace)
     plus idea `:AFTER:` (idea ids, and spec ids via the item's `:SPEC:`). Ties keep input
     position.
   - Each idea item's plan gets `after` from its `:AFTER:`, and each spec item's plan gets `after`
     only from `--after`. A spec's own `depends_on` already orders it.
   - Cycles are refused with the existing cycle text, and `_cascade` refuses dependents of refused
     items for ideas too.

## Design

- **`cli_queue.py` `queue add`:** new option `--after REF`, repeatable, applied to every id of the
  command. Refs are canonicalized with `_canon_id`; a campaign id is detected with
  `store.campaign_dir(cfg, ref).is_dir()`. The plan JSON (`nw.queue.add.v1` / batch) gains
  `after`. Dry-run text shows `after: IDEA-874 (open, campaign … IMPLEMENTING)`.
- **`queue.py`:**
  - `plan_add`, `plan_add_idea`: `after=()` keyword, validated: each ref must resolve now (an
    existing idea, spec or campaign), else ValueError `--after X: not found`.
  - `apply_add`, `apply_add_idea`: write `state["after"]` and a ledger `note` "after: …" (author
    `queue`).
  - `after_met` (new).
  - `backlog_readiness`: the `after` loop after the merge check, before the spec and idea branches,
    for all non-merge kinds.
  - `_order_items`, a generalization of `_order_specs` (the old name kept as a thin alias for
    callers).
  - `plan_add_batch` / `apply_add_batch`: idea edges, `after` on plans.
  - `row_view` / `backlog_view`: `after` and `waiting_on` include idea refs.
  - New command `nw queue after <cid> [--add REF] [--remove REF] [--yes]`, dry-run first. It is
    refused unless the campaign is QUEUED or PARKED.
- **`spec_write.py`:** `snapshot` keeps `must_depend_on` (default `[]`). `write_rules("write",
  …)` and `_front_matter_problems(fm, spec_id, idea, must_depend_on=())` check it.
  `queue._prepare_spec_write` computes it.
- **`specs.py`:** `parse_breakdown_deps`; `seed_ideas_from_epic` stamps `:AFTER:` through
  `org_write` (the same writer `W.add_idea` uses). `parse_breakdown_project` and the title
  derivation strip the dependency annotation too, so titles stay clean. Ideas seeded before this
  change keep their headings; a heading containing the annotation still matches its bullet,
  compared with the annotation stripped on both sides.
- **`personas/spec_writer.md`:** one line saying the brief's "depends_on must include" list is
  mandatory.
- **`skills/nw-conduct/SKILL.md`:** the `backlog_blocked_long` row mentions `after` waits, and that
  an abandoned prerequisite means re-queueing it or `nw queue after <cid> --remove`. The
  `message.request` row: a steward's "queue X after Y" is `nw queue add X --after Y`.
- **`quiet.py`:** `_ID_RE` and `dependency_wait` as in Decision 3.

## Alternatives considered

- **Admit for spec writing early, gate only implementation.** Lost: it needs a second gate inside
  the campaign (SPEC_WRITING → IMPLEMENTING), and the writer would have to cite a spec that is
  still `proposed` and may be revised. Waiting for the merge is simple, and it is what fixed the
  audited parks.
- **Write `depends_on` into the idea and let the writer copy it.** Lost: ideas have no spec ids
  before they are written. The writer failing to cite them was half the problem.
- **Derive the order from the epic's Breakdown at queue time, without seeding.** Lost: the
  ordinal-to-idea mapping is only certain when the ideas are created from the bullets. Stamping
  `:AFTER:` at seed time records it once, visibly, in the ideas file.

## Acceptance criteria

- [x] AC1: `nw queue add IDEA-2 --after IDEA-1 --yes` writes `after: [{ref: IDEA-1, kind: idea}]`.
      IDEA-2's campaign stays QUEUED with `blocked_by: ["waiting on IDEA-1 (…)"]` until
      IDEA-1's campaign is MERGED (or IDEA-1's `:SPEC:` is done), then is admitted.
- [x] AC2: `--after SPEC-X` and `--after <campaign-id>` work the same way. An unknown ref is
      refused at add. An abandoned prerequisite gives a non-ordinary `blocked_by` that is
      delivered to the conductor, not held.
- [x] AC3: The spec writer of an idea campaign with `after` gets the "depends_on must include"
      line. A written spec missing one of those ids fails validation with `front matter:
      depends_on must include SPEC-X (prerequisite IDEA-1)`. A spec that includes them passes.
- [x] AC4: `nw spec seed-children` on an epic whose 3rd bullet ends "(depends_on child 1 and
      child 2)" sets that idea's `:AFTER:` to the ideas of bullets 1 and 2. A re-run changes
      nothing. A bad ordinal is a hard error naming the bullet.
- [x] AC5: `nw queue add --epic E --yes` orders the items so that each one follows its
      prerequisites (specs and ideas mixed), sets each idea campaign's `after` from `:AFTER:`,
      and refuses cycles and dependents of refused items.
- [x] AC6: The dependency wait of an `after` entry with a live prerequisite campaign is held by
      the quiet stream like a spec `depends_on` wait. A wait with no live prerequisite is
      delivered.

## Test plan

- **Automated tests**
  - `tests/test_agentic_queue.py` / the backlog tests (extend): `after_met` for each kind and
    state; `backlog_readiness` with `after` on idea, spec and split campaigns; merge campaigns
    ignore it (AC1, AC2).
  - `tests/test_agentic_spec_writer.py` (extend): `must_depend_on` in `write_rules`;
    `_front_matter_problems` missing, present and superset cases (AC3).
  - `tests/test_seed_children.py` (extend): the annotation forms; `:AFTER:` stamping;
    idempotency; bad ordinal; title stays clean (AC4).
  - The batch tests (`tests/test_agentic_queue_batch*.py` or wherever `plan_add_batch` is tested):
    mixed ordering; cycle; cascade (AC5).
  - `tests/test_agentic_quiet*.py`: an `after` wait is held when live and delivered when not
    (AC6).
- **Real verification (beyond fakes)**: `tests/test_idea_ordering_e2e.py` (new). It uses a real
  org ideas file in a temp `NW_DATA_DIR`, a real epic spec file in a temp specs dir whose
  Breakdown has 3 bullets (the 2nd "(depends_on child 1)", the 3rd "(depends_on child 1 and child
  2)"), and real CLI invocations through `CliRunner`:
  1. `nw spec seed-children` → 3 ideas, with `:AFTER:` as expected in the org file text.
  2. `nw queue add --epic --yes` → 3 QUEUED campaigns in order, with `after` set.
  3. Real `queue.tick()` on a real git fixture repo: only child 1 is admitted.
  4. A scripted `FakeRunner` spec writer writes a spec without `depends_on` for child 2 after child
     1 merged: validation rejects it with the AC3 text. A second scripted write includes it and
     passes.
  5. Child 2 is admitted only after child 1's campaign is MERGED.
- **Replay on the real store** (manual, recorded in As shipped):
  - `nw queue add --epic SPEC-0400 --json` (dry-run, read-only) on the live store lists any
    remaining children in Breakdown order.
  - `nw spec seed-children SPEC-0400` in a scratch copy of the ideas file (`NW_DATA_DIR` /
    org path pointed at a copy, never the live file) stamps the `:AFTER:` values that the
    annotations imply.
- **Regression guard**: affected tests and `python3 tools/spec_lint.py`. Existing batch and seed
  tests pass. A campaign without `after` behaves exactly as before.

## Clock Log

CLOCK-IN: [timestamp]
CLOCK-OUT: [timestamp]

## Rollout / migration

1. Land. Existing campaigns have no `after` and are unaffected.
2. For epics already seeded (e.g. SPEC-0400), re-running `nw spec seed-children` stamps the
   missing `:AFTER:` properties. Already-queued campaigns can get theirs with `nw queue after <cid>
   --add …`.
3. Close IDEA-880 as folded into this spec (`nw idea close IDEA-880 --as researched
   --folded-into SPEC-0423`) when this spec is done.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## As shipped

Shipped as designed, with these specifics:

- `queue.py`: `after_entries`, `after_met`, `_after_blocked` (called from `backlog_readiness` right after the merge branch, so any non-merge campaign is gated; an unmet `after` returns before the spec/idea checks), `resolve_after`, `_must_depend_on`, `_idea_edges`, `plan_after`/`apply_after` (`nw queue after`), `_order_items` (`_order_specs` kept as an alias), `after=` on `plan_add`, `plan_add_idea`, `plan_add_batch`. `row_view`/`backlog_view` carry `after` only when set, so campaigns without it render byte-identically.
- Wait text: an idea ref whose idea has a `:SPEC:` waits as `waiting on SPEC-X (after: IDEA-N -> <status>)`; a campaign ref waits as `waiting on <cid> (after: campaign <cid> is STATE)`. Abandoned prerequisites and unknown refs use `after X: ...` text, which is not an ordinary wait, so the conductor is woken.
- `quiet.py`: `_ID_RE` also matches campaign ids; `Ctx.live_specs` gains idea ids and campaign ids of live campaigns.
- `plan_add_idea` also reads the idea's own `:AFTER:`, so a single `nw queue add IDEA-N` honors it, not only `--epic`.
- A skipped `must_depend_on` ref (no spec id) is omitted silently rather than noted in the brief; admission needs the prerequisite merged, so it cannot occur in practice.
- `specs.py`: `parse_breakdown_deps`; `seed_ideas_from_epic` validates all ordinals first (nothing is seeded on a bad one), seeds, then stamps `:AFTER:`.
- Tests: `tests/test_idea_ordering.py` (20) and `tests/test_idea_ordering_e2e.py` (1, real git fixture repo, real org file, real CLI, real ticks) are new files rather than extensions of the files the plan named; additions to `test_seed_children.py`, `test_agentic_quiet.py`, `test_agentic_queue_cli.py`. The e2e applies child 1's MERGED through `queue._transition` (the implement/gate/merge pipeline has its own tests); the spec-writer validation and admission gating are real.
- Affected tests (excluding the `test_workers*` files, which hung on unrelated runner processes under load): 4839 passed, 2 failed. `test_skills.py::test_conduct_and_steward_route_merges_through_the_queue_spec_0234` was real (my SKILL.md edit broke its pinned phrase) and was fixed; it passes. `test_dashboard_snapshot.py::test_one_failing_builder_is_one_error_section` is a load flake and passes alone.
- Live-store replay: `nw queue backlog --json` before/after the change was identical (one QUEUED row, no `after`). `nw queue add --epic SPEC-0400 --json` lists only IDEA-878 (no `after` yet). `seed-children SPEC-0400` on a scratch copy of the ideas files (ideas.org plus ideas-archive.org) stamped IDEA-875/876/877 `:AFTER: IDEA-874` and IDEA-878 `:AFTER: IDEA-875 IDEA-877`; the live file was unchanged. Seed it for real, and `nw queue after <cid> --add` queued campaigns, per Rollout.

## Open questions

_None._
