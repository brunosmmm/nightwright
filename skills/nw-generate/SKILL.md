---
name: nw-generate
description: "Turn an accepted internal SPEC-NNNN into tracked org tasks. Use once an internal feature/epic is accepted and you're ready to start doing the work it describes."
trigger: turning an accepted internal spec into actionable tasks
---

# nw-generate

## When to use

You have an **internal** spec (`docs/specs/SPEC-NNNN`) that's `accepted` (or further along)
and you're ready to start doing the work — you need it broken into trackable org tasks, not
just prose.

**Outbound** (`PROJ-NNNN` in the nightwright data-dir outbox): do **not** generate. Use `/nw-export`,
then `/nw-implement-spec` in the target repo (SPEC-0041 / SPEC-0045). The CLI refuses
`nw spec generate` on outbound ids.

## Procedure

1. Confirm the spec is at least `accepted` — `nw spec generate` on a `draft`/`proposed` spec is
   premature; finish authxxing it first (see `nw-new-work`). Confirm the id is `SPEC-NNNN`
   (internal), not an outbound `PROJ-NNNN`.
2. Generate tasks: `nw spec generate <SPEC-ID>`. Run `nw spec generate --help` for the exact
   flag syntax (target file, JIRA key linking).
   - For an **epic**, tasks come from its Breakdown/sub-specs.
   - For a **feature**, tasks come from its Acceptance criteria.
3. If this work is tracked against an external ticket, key the generated tasks so time-tracking
   can join to them later — see `nw spec generate --help` for how to pass that key.

## Conventions / guardrails

- Re-running `nw spec generate` on the same spec should not duplicate tasks it already
  generated — verify with `nw tasks` before assuming you need to regenerate.
- Task keying matters for `nw digest` (which joins tracked time to tasks by that key) — don't
  skip it if this work will be time-tracked against a ticket.
- Never point this skill at outbound specs.

## Verify

`nw tasks` lists the new tasks (filter with `--project`/`--tag`/`--key` as needed). Once time is
logged against them, `nw digest` should join tracked hours to the same tasks — spot-check that
before considering this step done.
