---
id: SPEC-0209
title: "Queue an idea: spec_writer persona and the SPEC_WRITING step"
status: accepted
owner: user
created: 2026-10-25
updated: 2026-10-25
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0169
tags: [agentic, queue, specs, personas]
source_idea: IDEA-063
depends_on: [SPEC-0208]
---

## Context

Child of epic [SPEC-0169](./0169-mini-agentic-workflow-queue-campaigns-headless.md), the
second of four specs from `IDEA-063` (it absorbs `IDEA-032`, the spec-writer phase). The user
decided on 2026-10-25 that unwritten specs are part of the queue. Inserting an IDEA runs
**one campaign end to end** — spec writing, spec review/revision, acceptance, then the
existing implement → gate → review → close loop — on one branch, with one merge.

Today `wt queue add` takes only an accepted, reviewed spec (`queue.plan_add`, SPEC-0176 /
SPEC-0188), so every spec is written by hand. This spec adds the entry point and the writing
step. SPEC-0210 adds the review/revision loop and automatic acceptance; SPEC-0211 adds the
watch, listing and event views. Until SPEC-0210 lands, a written spec parks for a human
decision (`spec_ready`), so this spec ships safely on its own.

## Goals / Non-goals

**Goals**
- `wt queue add IDEA-NNN` queues an idea as a campaign of kind `idea`, which writes its own
  spec after it opens.
- A headless `spec_writer` persona writes `docs/specs/NNNN-<slug>.md` in the campaign
  worktree, under a number from the SPEC-0208 allocator, and wt validates and commits it.
- The idea is linked: `:SPEC:` and SPECCED once its spec exists, and `:CAMPAIGN:` from the
  moment it is queued.

**Non-goals**
- Reviewing, revising or accepting the written spec (SPEC-0210).
- Watch, dashboard and event changes beyond a state style (SPEC-0211).
- Outbound portable specs for foreign repos (IDEA-038). Projects with a `.wt/project.yaml` are
  covered; their specs go to `<repo>/docs/specs` with prefix `SPEC`.

## Decision

Campaign kind `idea`: QUEUED → OPENING → **SPEC_WRITING** → (this spec: PARKED `spec_ready`;
SPEC-0210: SPEC_REVIEW). The spec_writer persona runs like the implementer (in the worktree,
`bypassPermissions`, budgeted). wt allocates the number, then validates and commits the file,
and records it in `state.spec`.

## Design

- **States:** `SPEC_WRITING` joins `STATES` and `ACTIVE_STATES` (it holds a slot);
  `RESUMABLE_STATES` gains it; `render.STATE_STYLES` gets a style (cyan).
- **`wt queue add IDEA-NNN [--priority N] [--profile P] [--yes] [--json]`.** An argument
  matching `^IDEA-\d+$` dispatches to `plan_add_idea` / `apply_add_idea`; spec ids keep the
  existing path.
  - The idea must exist and be open (`IDEA`/`INCUBATE`). If it already has a `:SPEC:`, the
    command errors with "queue that spec instead: wt queue add SPEC-…". If an open campaign
    already exists for it, the command errors.
  - Project: the idea's `:PROJECT:`, default `Meta-Tools`. The project must have a loadable
    `.wt/project.yaml`.
  - The campaign id is `IDEA-NNN-YYYYMMDD-xxxx`, via `store.new_campaign_id`.
  - `state.kind = "idea"`, and `state.idea` is a snapshot: `{id, heading, summary,
    questions, log, project, epic, kind}`, taken from `explore.idea_show_payload`.
  - `state.spec = {"id": None, "rel_path": None, "internal": True}`.
  - The backlog readiness of an idea campaign is "ready" while the idea is open.
  - The idea gets `:CAMPAIGN: <campaign id>` (via `org_write.set_property`).
- **Open:** `_do_open` is unchanged, except that the spec hash and `spec_at_open.md` copy are
  skipped while `state.spec.id` is None. An idea campaign's open goes to SPEC_WRITING instead
  of IMPLEMENTING.
- **`plan_step`:** SPEC_WRITING → `Step("write_spec", persona="spec_writer")`. The cost-cap
  check applies as for other personas.
- **The write step** (`_prepare_run` / `_do_persona` path, persona `spec_writer`):
  1. The number is `spec_numbers.allocate(by="queue", campaign=<cid>, key=<project repo>)`,
     which is idempotent across retries.
  2. The prompt (`personas/spec_writer.md`) carries:
     - the idea snapshot;
     - `docs/specs/TEMPLATE.md` (or `TEMPLATE-epic.md` when the idea says epic) and
       `docs/specs/SCHEMA.md` from the worktree;
     - AGENTS.md's spec rules;
     - the epic's spec text (when the idea has `:EPIC:`) and the titles and ids of the
       epic's other children;
     - the reserved id;
     - the rule: write exactly one new file `docs/specs/<NNNN>-<slug>.md`, front matter
       `status: proposed`, `source_idea: <IDEA>`, `parent: <epic>` when set, no
       placeholders, a real Test plan; edit nothing else.
  3. Verdict schema: `{status: written|blocked, spec_path, title, summary, open_questions[],
     blockers[]}`.
- **Validation** (wt, deterministic, after the run):
  - exactly one new or modified file under `docs/specs/`, named `<NNNN>-*.md`;
  - front matter parses, with `id == SPEC-<NNNN>`, `status == proposed`, `source_idea ==` the
    idea, and the required keys `title` / `owner` / `created`;
  - no other files changed.
  Problems raise a `finding` and count as a failed write: one retry with the problems as the
  brief, then park `spec_write_failed`. Verdict `blocked` parks `spec_write_blocked` with the
  blockers.
- **On success:**
  - commit `wt(<cid>): spec writer run NNN — SPEC-NNNN <title>`;
  - set `state.spec = {id, rel_path, internal: True}`;
  - set the idea's `:SPEC: SPEC-NNNN` and state SPECCED (org writes take the SPEC-0190 lock);
  - ledger event `spec_written {spec_id, rel_path, title}` (added to `schemas.EVENT_KEYS`);
  - then park with reason `spec_ready` and `resume_to: IMPLEMENTING`, detail "review the
    spec, then resume". SPEC-0210 replaces this park with SPEC_REVIEW.
- **Resume from `spec_ready` to IMPLEMENTING** (human acceptance, until SPEC-0210): wt sets
  `status: accepted` in the worktree spec (`specs.set_spec_status`), commits "accept",
  writes `spec_at_open.md`, and sets `spec.sha256_at_open`, so the SPEC-0186 guard compares
  against the accepted text.
- **Profiles:** `spec_writer` in each builtin profile:
  - `balanced`: sonnet, effort high, $1.50;
  - `lean`: sonnet, medium, $0.80;
  - `premium`: opus, high, $3.00;
  - `permission_mode: bypassPermissions`, `read_only: false`.
- **PARK_REASONS** gains `spec_ready`, `spec_write_failed` and `spec_write_blocked`.

## Alternatives considered

- **Merge the spec to main before implementation** — rejected by the user: an extra merge per
  idea during closes (`base_moving`), and two campaigns to track per idea.
- **wt picks the slug** — the writer knows the title; wt validates the number prefix instead.

## Acceptance criteria

- [ ] `wt queue add IDEA-NNN` (dry-run, then `--yes`) creates a QUEUED campaign of kind `idea`
      with the idea snapshot and sets the idea's `:CAMPAIGN:`. It refuses an idea that has a
      `:SPEC:`, a closed idea, an unknown project and a duplicate campaign.
- [ ] After admission and open, the campaign is in SPEC_WRITING (not IMPLEMENTING), with no
      spec hash recorded.
- [ ] A FakeRunner `spec_writer` run that writes a valid `docs/specs/<allocated>-x.md` gets
      committed. `state.spec` is set, the idea gets `:SPEC:` and SPECCED, a `spec_written`
      event is recorded, and the campaign parks `spec_ready`.
- [ ] Writes that break validation (wrong number, a second file, `status: accepted`, a missing
      `source_idea`, a change outside `docs/specs/`) get one retry, then park
      `spec_write_failed`. A `blocked` verdict parks `spec_write_blocked`.
- [ ] Resuming from `spec_ready` to IMPLEMENTING sets the spec `accepted`, commits it, and
      writes `spec_at_open.md`. The campaign then implements and closes like any spec
      campaign; an e2e run ends MERGED with both the spec and the feature on main.
- [ ] Two idea campaigns opened in the same tick get different spec numbers.
- [ ] `uv run pytest` passes; `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests:** `tests/test_agentic_idea_campaign.py`, using the `test_agentic_e2e`
  harness.
  - Add a FakeRunner entry helper `spec_written(number, ...)` that writes a valid spec file,
    and make the harness's org config able to capture an idea.
  - One test per AC: add/refusals; the open lands in SPEC_WRITING; the valid write; each
    invalid write variant (parametrized); `blocked`; resume-accept followed by implementation
    and a full run to MERGED; two campaigns with `slots=2` getting distinct numbers.
  - The persona template renders (no unfilled `{{…}}`).
- **Regression guard:** full `uv run pytest`; existing spec campaigns are unaffected (kind
  `implement`).

## Rollout / migration

Ships through the agentic queue. Until SPEC-0210 lands, idea campaigns stop at `spec_ready`
for a human (or the conductor) to read and resume.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

(none)
