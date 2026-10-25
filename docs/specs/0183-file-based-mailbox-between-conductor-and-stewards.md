---
id: SPEC-0183
title: "File-based mailbox between conductor and stewards"
status: done
owner: user
created: 2026-10-25
updated: 2026-10-25
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0169
tags: [agentic, mailbox, conductor, steward]
source_idea: IDEA-035
depends_on: [SPEC-0181]
---

## Context

Child of epic [SPEC-0169](./0169-mini-agentic-workflow-queue-campaigns-headless.md)
(promoted from `IDEA-035`, pulled forward from the later waves on 2026-10-25).

The plan calls for deterministic, bidirectional messaging between the conductor (one
session that runs the queue) and stewards (sessions that know a project). The conductor
asks a steward project-specific questions about a parked campaign; a steward asks the
conductor to queue, park or resume work, or answers. Both sides are woken by Monitor on a
command that prints one line per incoming message. File-based transport was decided with the
user; the queue event stream (SPEC-0181) already establishes the pattern.

## Goals / Non-goals

**Goals**
- Addresses: `conductor`, `steward:<Project>` (project names from the registry), `human`.
- `wt mailbox send --to ADDR [--from ADDR] [--campaign ID] [--re MSG_ID] [--kind
  question|answer|request|info] TEXT` appends a message to the recipient's inbox
  `<data_dir>/agentic/mailbox/<addr>.jsonl` with envelope `{schema: "wt.message.v1", id,
  ts, from, to, kind, campaign?, re?, text, needs_reply: bool}`; prints the message id.
- `wt mailbox follow --to ADDR` streams new messages forever (one JSON line each) with a
  per-reader cursor, Monitor-friendly, same semantics as `wt queue events --follow`.
- `wt mailbox list --to ADDR [--unanswered] [--json]` and `wt mailbox thread MSG_ID`
  (a question and its answers by `re`).
- Messages to `conductor` are also appended to the event stream as `message` events, so the
  conductor's single Monitor on `wt queue events --follow --for conductor` wakes on them.
- `--from` defaults from `WT_MAILBOX_ADDR` (set by the skills) so agents never forge
  another sender by accident; unknown addresses are refused.

**Non-goals**
- The conductor's and steward's decision procedures (SPEC-0182, SPEC-0184).
- Remote delivery, encryption, multi-machine.

## Decision

Add `src/wt/agentic/mailbox.py` on the same primitives as `events.py` (flocked append,
monotonic per-inbox ids, cursors), a `mailbox` click group, and a hook so conductor-bound
messages are mirrored into the event stream.

## Design

- `mailbox.py`: `ADDRESS_RE = conductor | human | steward:<name>`; `validate_address(cfg,
  addr)` checks steward names against `projects_payload`; `send(cfg, *, to, frm, text,
  kind, campaign, re, needs_reply)`; `read_since(cfg, addr, after)`; `follow(cfg, addr,
  reader, poll_s)`; `thread(cfg, msg_id)` (ids are `<addr>:<n>`).
- `needs_reply` defaults true for `question` and `request`; `list --unanswered` shows
  messages with `needs_reply` and no reply (`re`) in the sender's inbox.
- Mirror: `send` to `conductor` calls `events.append(cfg, "message", campaign=…,
  data={msg_id, from, kind, text})` (add `message` to `EVENT_TYPES`).
- CLI group `wt mailbox` with `send`, `follow`, `list`, `thread`; `--to`/`--from`
  completers from the address set.
- Docs: capability page "Mailbox"; agent-surface row `wt.message.v1`; reference row
  `mailbox`.

## Alternatives considered

- **One shared mailbox file** — rejected; per-recipient inboxes keep follow cheap and make
  "who has unread mail" a file question.
- **Reuse ledger notes** — rejected; notes are per campaign and have no recipient.

## Acceptance criteria

- [x] `send` validates addresses (unknown steward project refused), writes the envelope,
      returns an id; concurrent senders get distinct, increasing ids.
- [x] `follow` prints only new messages as JSON lines and resumes after its cursor across
      restarts.
- [x] A message to `conductor` appears as a `message` event in `events.jsonl`.
- [x] `list --unanswered` and `thread` reflect replies linked by `re`.
- [x] `--from` defaults to `WT_MAILBOX_ADDR`; sending without either is a usage error.
- [x] Completion, reference, agent-surface guards pass.
- [x] Full `uv run pytest` green; `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests:** `tests/test_agentic_mailbox.py` (addresses, send/read/ids under
  threads, cursors, follow subprocess restart, mirror into events, thread/unanswered, CLI).
- **Manual verification:** two shells: `wt mailbox follow --to steward:Meta-Tools` and a
  `wt mailbox send` from `conductor`; reply and check `thread`.
- **Regression guard:** full `uv run pytest`.

### Test record (2026-10-25)

- `tests/test_agentic_mailbox.py`: 8 tests (addresses incl. case-insensitive steward
  canonicalization, ids under threads, conductor mail mirrored as `message` events, follow
  cursor resume, unanswered/thread, CLI). Full suite 1483 passed.
- **Live:** the human armed `/wt-steward` for Meta-Tools (follower holding
  `mailbox:steward:Meta-Tools`); the conductor sent `steward:Meta-Tools:1` (info) through
  `wt mailbox send`. *As shipped:* ids are `<address>:<n>`; one-steward-per-project comes from
  the follower's reader lock (no separate lease command).

## Clock Log

Built by the agentic loop; idea bookkeeping via `wt spec reconcile --apply` after merge.

## Rollout / migration

New files on first message.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

_None — decisions locked above._
