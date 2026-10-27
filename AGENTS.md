# AGENTS.md — contract for agents working in this repo

This repository is developed **spec-first**. Any agent (Claude Code or otherwise) doing
development here **must** follow the spec workflow below. This file is the entry contract;
read it before making changes.

## TL;DR

- **No behavior/feature change without a governing spec.** Trivial, no-behavior edits
  (typos, comments, formatting) are exempt.
- Specs live in [`docs/specs/`](./docs/specs/), governed by
  [`docs/specs/SCHEMA.md`](./docs/specs/SCHEMA.md) + `spec.schema.json`. The index is
  [`docs/LEDGER.md`](./docs/LEDGER.md).
- **Close the loop.** A spec is `done` only when its **Test plan has been executed**, tests
  pass, every acceptance criterion is true, and the spec + ledger are updated to reflect
  what actually shipped.

## Autonomy (all agents)

**Decide, don't defer.** Resolve design details, defaults, thresholds, judgment calls,
conflicts and blockers yourself; record the reasoning in the spec (Design / As shipped) or a
campaign note and tune later from evidence. Escalate to the human only decisions about what
gets built (scope, priorities, product direction) or anything touching their own checkout;
never hand them parameters to adjust. Full working rules (worktree-only, background fresh
sub-agents with per-task models and never forks, missing-CLI -> file an idea, ideas-file
heading check, decide-don't-defer): `/wt-orient`.

## Agent skill priority (SPEC-0138)

Other agent skill packs (notably **Superpowers** — brainstorming, writing-plans, TDD — which
Cursor may load from `~/.claude/plugins` even when you are not using Claude Code) are
**tactical only** in this repo:

- **wt owns** idea→spec→verify lifecycle and the **governing** design (`docs/specs/`, ledger).
- Do **not** treat `docs/superpowers/specs` or `docs/superpowers/plans` as governing authxxity
  here.
- “Just build” / “keep working” → continue via `wt hub` / `wt next` (or promote a thin
  accepted slice); it is not a waive of this file unless the human explicitly says so.
- Claiming `done` requires this contract’s DoD / `/wt-verify` — not Superpowers verification
  alone.

## Before you write code

1. Read [`docs/README.md`](./docs/README.md) and
   [`docs/specs/SCHEMA.md`](./docs/specs/SCHEMA.md).
2. Find or create the governing spec:
   - **Existing work:** locate it in [`docs/LEDGER.md`](./docs/LEDGER.md); set it
     `in-progress`.
   - **New work:** `cp docs/specs/TEMPLATE.md docs/specs/NNNN-slug.md`, fill it in. For a
     **large feature that fans out into many specs**, create an *epic* from
     `TEMPLATE-epic.md` and give each child spec a `parent: SPEC-NNNN`. Get specs to
     `accepted` **before** implementing.
3. Run `python3 tools/spec_lint.py` (validates frontmatter + rules) and
   `python3 tools/spec_lint.py --write-ledger` to refresh `docs/LEDGER.md` (it's generated
   — never hand-edit the block between the `specs:` markers).

## While implementing

- **Keep the spec truthful.** If the design changes under you, update the spec's
  *Decision*/*Design* as you go — don't let it drift from reality.
- **Follow the spec's Test plan.** If a feature spec has no Test plan, it is incomplete —
  add one before writing code.
- **Clock in/out on the linked idea** (SPEC-0060/0061): `wt idea clock-in <IDEA-ID>` when you
  start working a spec's implementation, `wt idea clock-out <IDEA-ID>` when you pause or
  finish. This is supplemental wall-time data alongside wt's passive transcript tracking —
  every clock-in needs a matching clock-out before you stop.

## Closing the loop — Definition of Done (MANDATORY)

A spec may be set `status: done` only when **all** of these are true:

- [ ] Every item under **Acceptance criteria** is checked and actually true.
- [ ] The **Test plan** has been executed: automated tests written and passing
      (`uv run pytest`), and any manual verification performed and noted in the spec.
- [ ] **No regressions:** the pre-existing test suite still passes.
- [ ] The spec body reflects what actually shipped (not the original guess).
- [ ] [`docs/LEDGER.md`](./docs/LEDGER.md) regenerated (`python3 tools/spec_lint.py
      --write-ledger`) so the spec's new status shows.
- [ ] `python3 tools/spec_lint.py` exits 0 (frontmatter valid, rules pass, ledger current).

**"Close the loop on testing" is non-negotiable:** shipping a spec's code without executing
its test plan is an *incomplete* change, not a done one. Do not mark `done` to move on.

## Changing a past decision

Never silently rewrite a spec once it is past `draft`. Write a **new** spec that
`supersedes` it, and set the old one `status: superseded` + `superseded_by`. The ledger
must stay a faithful timeline.

## Tooling

- Build / run / dev: **uv** — `uv run wt …`, `uv run pytest`.
- Specs: `python3 tools/spec_lint.py` (lint) · `--write-ledger` (regenerate the index).
  `uv run pytest` fails if any spec or the ledger drifts.
- Tests: wait with `tests/waits.py` (`WT_TEST_WAIT_S`), never with a fixed sleep before a
  positive assertion (SPEC-0269; `tests/test_test_waits.py` guards it).
- Authxxing guidance: `docs/specs/SCHEMA.md`, the meta-spec
  [SPEC-0000](./docs/specs/0000-how-we-write-specs.md), and the epic model
  [SPEC-0003](./docs/specs/0003-epic-and-subspec-model.md).
