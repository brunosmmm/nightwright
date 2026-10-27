---
id: SPEC-0279
title: "Headless spec revision: queue a spec with an unapplied revise review"
status: accepted
kind: feature
owner: user
created: 2026-10-27
source_idea: IDEA-083
parent: SPEC-0169
depends_on: [SPEC-0230]
tags: [agentic, queue, review, specs]
---

## Context

Promoted from idea `IDEA-083` (kind: bug; user, 2026-10-25: spec revisions must not depend on
a steward).

Verified on main, these parts of the idea are already covered:
- **Idea campaigns.** SPEC-0209 and SPEC-0215 run the `spec_writer` headlessly in
  SPEC_WRITING, in write and revise modes. SPEC-0214 (accepted, not built) plus the
  overrides in SPEC-0230 put the single in-campaign review and the revise step in the queue.
- **Admitting a revised spec.** `spec_review.check_admissible` admits a spec whose latest
  review is `revise` once its text has changed (`admission: "after_revision"`, SPEC-0230).

What is still missing is the gap this spec fills. A **spec campaign** (`wt queue add
SPEC-X`) for a hand-written or conductor-written spec whose latest review is `revise` with
the text **unchanged** is refused by `check_admissible`: "edit the spec, then queue it".
Someone then has to edit it in a session. The conductor skill sends that edit to the author
(`skills/wt-conduct/SKILL.md`, "One spec review round"), and with no steward armed, nothing
moves. `skills/wt-steward/SKILL.md` step 7 still says "Spec work belongs to you".

## Goals / Non-goals

**Goals**
- `wt queue add SPEC-X`, for an internal spec whose latest review is an unapplied `revise`,
  queues a campaign. The campaign opens into SPEC_WRITING. A headless `spec_writer` run in
  a new `amend` mode applies the findings, and the campaign then goes straight to
  IMPLEMENTING with no second review (SPEC-0230).
- The conductor and steward skills say that revision is headless and that a steward may help
  but is never required.

**Non-goals**
- `split` verdicts. They are still refused at admission (automatic splitting is IDEA-065).
- Outbound specs: an unapplied `revise` on an outbound id is still refused with today's
  message.
- Building SPEC-0214's in-campaign review for idea campaigns.
- A standalone `wt spec revise` command. The queue path covers the need, and the human can
  still edit by hand.

## Decision

`check_admissible` returns a third admission, `"revise_pending"`, for an internal spec whose
latest review is `revise` for the current sha, instead of raising. `plan_add` and
`apply_add` carry it into the campaign. `_do_open` routes that campaign to SPEC_WRITING. A
new spec-write mode, `amend`, edits the existing spec under strict front-matter rules. On
success, `_apply_spec_write` refreshes the spec-at-open baseline and transitions to
IMPLEMENTING with `revision_brief(review)` as the implementer's brief.

## Design

- **`spec_review.check_admissible(cfg, spec_path, spec_id)`**: the
  `verdict == "revise" and rsha == sha` branch returns `{**latest, "admission":
  "revise_pending", "current_sha": sha}` when `not SP._is_outbound_spec_id(spec_id)`, and
  raises as today otherwise.
- **`queue.plan_add`**: a `revise_pending` admission sets `plan["revision"] =
  {"review_sha", "current_sha", "findings", "pending": True}`. `_print_plan` shows
  `revision: pending (N findings), spec_writer amends first`.
- **`queue.apply_add`**: for `pending`, it sets
  - `state["admission"] = "revise_pending"`;
  - `state["spec"]["id"] = spec_id`;
  - `state["revision"] = {"review_sha", "findings"}` (from the plan; the apply path reads the
    findings from here, never from the ledger);
  - `pending_brief = {**SR.revision_brief(review), "source": "spec_review_revise"}`.
  It appends an `admitted_revise_pending {review_sha, current_sha, findings}` ledger event.
- **`schemas.EVENT_KEYS`** gains `"admitted_revise_pending": ("review_sha", "current_sha",
  "findings")` (an unregistered type is a `ledger_problems` error). The TUI transition list
  (`tui/agentic_model.py`, next to `admitted_after_revision`) lists it too.
- **`queue._apply`, `kind == "open"`**: the target is `SPEC_WRITING` when
  `has_no_spec(state) or state.get("admission") == "revise_pending"`.
- **`queue._prepare_spec_write`**: the `admission == "revise_pending"` check comes **before**
  the existing `spec.get("id")` → `revise` branch. It gives mode `"amend"`, path
  `spec["rel_path"]` and `number = int(spec_id[5:])`, exactly as revise mode derives it, so
  `SW.snapshot`, `validate_spec_change` and `_apply_spec_write` keep using
  `spec_id_of(number)` unchanged. (Amend is internal-only, so `SPEC-NNNN` always parses.)
- **`spec_write.snapshot`** stores `fm_before` (the spec's front matter at `HEAD`) when
  `mode == "amend"`.
- **`spec_write.write_rules`**: `amend` gives "Amend mode: edit only `<path>`; leave every
  front-matter key unchanged except `updated`; address every finding in the brief".
- **`spec_write._content_problems`** gains a `spec_write` argument (passed by
  `validate_spec_change`). In `amend` mode it calls `_amend_problems(fm_before, fm)` instead
  of `_front_matter_problems`; `_amend_problems` reports any added, removed or changed key
  other than `updated`. `_schema_problems` and the section checks still apply.
- **`spec_write.validate_spec_change`**: the "no change made to `<path>`" message applies to
  `mode in ("revise", "amend")`.
- **`queue._apply_spec_write`**: on a committed `amend`, **after** the existing
  `state["pending_brief"] = None` reset:
  - set `spec.sha256_at_open` to the new sha and rewrite `CL.SPEC_AT_OPEN` from the amended
    file, so the close step's drift check (SPEC-0186) treats the amended text as the
    accepted text;
  - set `state["admission"] = "after_revision"`, so a later resume to SPEC_WRITING can never
    run amend twice. The campaign leaves SPEC_WRITING here and `RESUMABLE_STATES` resumes
    an IMPLEMENTING park to IMPLEMENTING, so SPEC_WRITING is not re-entered;
  - set `pending_brief = SR.revision_brief({"spec_sha256": state["revision"]["review_sha"],
    "findings": state["revision"]["findings"]})`;
  - emit `spec_written` with `mode: "amend"`;
  - `finish("IMPLEMENTING", "spec_amended")` instead of parking.
  Problems and `blocked` keep the existing retry and park handling.
- **Skills**:
  - `skills/wt-conduct/SKILL.md` "One spec review round": `revise` → queue the spec as is;
    the queue amends it headlessly. The stale "until the single-round admission rule ships"
    sentence is removed. One added sentence routes the parks this path can raise: a
    `spec_write_failed` park from an amend, or a `split` refusal, goes to the project's steward
    only when one is armed (`wt mailbox list --to steward:<P>`); otherwise it is escalated to
    the human.
  - `skills/wt-steward/SKILL.md` step 7: a steward may rework a spec when asked but is never
    required; a `revise` goes to the queue.

## Acceptance criteria

- [ ] `wt queue add SPEC-X --yes` with an unapplied `revise` (internal spec) creates a
      campaign with `admission: revise_pending`; an outbound id or a `split` is still
      refused.
- [ ] `wt queue add` (dry run) prints `revision: pending (N findings), spec_writer amends
      first`; opening the campaign goes to SPEC_WRITING, and the `spec_writer` runs in
      `amend` mode with the review findings in its brief.
- [ ] An amend that changes any front-matter key other than `updated` is a write problem
      (retried, then parked `spec_write_failed`).
- [ ] A committed amend moves the campaign to IMPLEMENTING (transition reason
      `spec_amended`) with no further review, and sets `admission: after_revision`. It
      refreshes `spec.sha256_at_open` and `spec_at_open.md`, and the implementer's brief
      carries the findings.
- [ ] `skills/wt-conduct/SKILL.md` and `skills/wt-steward/SKILL.md` no longer route `revise`
      to a steward or author session.

## Test plan

New tests in `tests/test_spec_review_store.py`, `tests/test_agentic_queue.py` and
`tests/test_agentic_spec_writer.py`, with the fake
runner writing the spec file:

- `test_check_admissible_revise_pending_internal` and
  `test_check_admissible_revise_unapplied_outbound_still_refused`.
- `test_queue_add_revise_pending_opens_into_spec_writing`: plan, apply, a tick of `open`, and
  state SPEC_WRITING with the `pending_brief` source `spec_review_revise`; the dry-run output
  contains the `revision: pending` line; the `admitted_revise_pending` event passes
  `schemas.ledger_problems`.
- `test_amend_mode_rules_and_frontmatter_guard`: a fake writer that flips `status` gives a
  problem; one that edits only the body and `updated` gives no problem.
- `test_amend_success_goes_to_implementing`: after the amend commit, the state is
  IMPLEMENTING, `sha256_at_open` equals the amended file's sha, `spec_at_open.md` matches,
  `pending_brief.must_fix` lists the review's findings, the transition reason is
  `spec_amended`, `admission` is `after_revision`, and no `spec_reviewer` run is in the
  ledger.
- `test_amend_number_and_spec_id_flow`: the `spec_write` record has the spec's own number and
  `validate_spec_change` reports `no change made to <path>` for an amend that edits nothing.
- `test_amend_precedes_revise_branch`: a `revise_pending` campaign with `spec.id` set
  snapshots in `amend`, not `revise`, mode.
- `test_skill_texts_no_steward_requirement`: a grep test that the steward skill no longer
  says "Spec work belongs to you" and the conductor skill no longer says "the author applies
  the findings".
- **Sabotage check:** make `_apply_spec_write` park as for `revise`;
  `test_amend_success_goes_to_implementing` must fail. Skip the `SPEC_AT_OPEN` refresh; the
  same test's baseline assertion must fail. Drop `_amend_problems`;
  `test_amend_mode_rules_and_frontmatter_guard` must fail.
- Regression guard: full `uv run pytest`, including the SPEC-0215 write/revise tests, which
  are unchanged.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

(none)
