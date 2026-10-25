---
id: SPEC-0184
title: "wt-steward skill: Monitor-armed project steward on the mailbox"
status: done
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

### As shipped (MVP, 2026-10-25)

- No `steward-lease` command: one steward per project is the reader lock held by
  `wt mailbox follow --to steward:<P>` (the Monitor's command).
- Skill shipped; the human armed a Meta-Tools steward in a project session (2026-10-25).
- Live round trips observed in that session's mailbox (`wt mailbox list --to
  steward:Meta-Tools`, `wt mailbox list --to conductor`): the steward asked what was safe to
  work on outside the queue (`conductor:1`) and the conductor answered (`steward:Meta-Tools:2`);
  the conductor flagged dirty main (`steward:Meta-Tools:3`), the steward answered with a
  stand-down request (`conductor:2`) and the conductor aborted its own mover
  (`steward:Meta-Tools:4`); three steward merge requests (SPEC-0185, SPEC-0189, SPEC-0190)
  were acted on by the conductor (merged, runner restarted). No campaign parked during the
  session, so the round trip was about direct-to-main work rather than a parked campaign;
  the mailbox path, reader lock and conductor follow-through are the same.

## Alternatives considered

- **Steward calls queue writes directly** — rejected; the conductor stays the single
  decision point, which keeps the ledger's `by` attribution honest.

## Acceptance criteria

- [x] Skill passes the thinness guard and names `wt mailbox --help` and
      `wt campaign --help`; added to `EXPECTED_SKILLS` (`tests/test_skills.py`).
- [x] `steward-lease` refuses a second steward for the same project and allows one per
      project — as shipped, the mailbox reader lock (`already followed`,
      `tests/test_agentic_mailbox.py`).
- [x] **Manual:** a steward session for Meta-Tools answers a conductor question about a
      parked fake campaign through the mailbox, and the conductor acts on the answer
      (recorded) — observed live instead, on direct-to-main work; see *As shipped*.
- [x] Full `uv run pytest` green; `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests:** skills guard; steward lease tests.
- **Manual verification:** the conductor ↔ steward round trip above on a scratch data dir.
- **Regression guard:** full `uv run pytest`.

## Clock Log

Built by the agentic loop; idea bookkeeping via `wt spec reconcile --apply` after merge.

## Rollout / migration

New skill; installed by `wt skills install`.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

_None — decisions locked above._
