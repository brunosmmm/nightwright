---
id: SPEC-0184
title: "wt-steward skill: Monitor-armed project steward on the mailbox"
status: accepted
owner: user
created: 2026-10-25
updated: 2026-10-25
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0169
tags: [agentic, steward, skills, mailbox]
source_idea: IDEA-036
depends_on: [SPEC-0183, SPEC-0182]
---

## Context

Child of epic [SPEC-0169](./0169-mini-agentic-workflow-queue-campaigns-headless.md)
(promoted from `IDEA-036`, pulled forward on 2026-10-25).

The plan's steward is "a session that has knowledge about a project and works with the
conductor via the messaging system to make project-specific decisions". With the mailbox
(SPEC-0183) and the event-driven conductor (SPEC-0182), a project session can become a
steward by arming a Monitor on its inbox, answering the conductor's questions with project
knowledge, and requesting queue actions for its project.

## Goals / Non-goals

**Goals**
- `skills/wt-steward/SKILL.md`: turns the current session into `steward:<Project>`
  (project from `wt projects config` / cwd), sets `WT_MAILBOX_ADDR`, arms a Monitor on
  `wt mailbox follow --to steward:<Project>` (re-armed on expiry), and handles:
  - `question` from the conductor about a campaign: read `wt campaign show/log`, the spec,
    and the code in the campaign worktree (read-only); reply with `--kind answer --re ID`
    and a concrete recommendation (resume with note X, abandon and rework the spec, or
    escalate to the human);
  - human requests in its own session ("queue SPEC-X", "what's in flight for this
    project"): send `request` messages to the conductor rather than calling queue writes
    directly, so one place decides.
- Read-only access to the project's campaigns via `wt campaign list --project P`.
- One steward per project: a steward lease `agentic/steward-<project>.lock` (reusing the
  generalized lease from SPEC-0182).

**Non-goals**
- Stewards implementing code in campaign worktrees (they advise; the loop implements).
- Multi-project stewards.

## Decision

Ship the skill plus the steward lease subcommand `wt mailbox steward-lease
acquire|release|status --project P`; no other code.

## Design

- Skill sections: When to use / Procedure (resolve project → acquire steward lease →
  `wt mailbox list --to steward:<P> --unanswered` → arm Monitor → per message: read, decide,
  reply → re-arm) / Conventions (advise, don't implement; requests go to the conductor;
  heading guard on ideas; SPEC-0138) / Verify.
- `steward-lease` uses `lease.py` with name `steward-<project>`.
- Docs: capability page "Stewards"; agent-surface skill-map row; `EXPECTED_SKILLS`.

## Alternatives considered

- **Steward calls queue writes directly** — rejected; the conductor stays the single
  decision point, which keeps the ledger's `by` attribution honest.

## Acceptance criteria

- [ ] Skill passes the thinness guard and names `wt mailbox --help` and
      `wt campaign --help`; added to `EXPECTED_SKILLS`.
- [ ] `steward-lease` refuses a second steward for the same project and allows one per
      project.
- [ ] **Manual:** a steward session for Meta-Tools answers a conductor question about a
      parked fake campaign through the mailbox, and the conductor acts on the answer
      (recorded).
- [ ] Full `uv run pytest` green; `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests:** skills guard; steward lease tests.
- **Manual verification:** the conductor ↔ steward round trip above on a scratch data dir.
- **Regression guard:** full `uv run pytest`.

## Clock Log

Built by the agentic loop; idea bookkeeping via `wt spec reconcile --apply` after merge.

## Rollout / migration

New skill; installed by `wt skills install`.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

_None — decisions locked above._
