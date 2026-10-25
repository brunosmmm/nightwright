---
id: SPEC-0198
title: "wt queue followers: who is armed, with pids, cursors and lag"
status: done
owner: user
created: 2026-10-25
updated: 2026-10-25
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0169
tags: [agentic, queue, observability]
source_idea: IDEA-049
---

## Context

Child of epic [SPEC-0169](./0169-mini-agentic-workflow-queue-campaigns-headless.md), promoted from `IDEA-049` (filed under the missing-CLI rule). To check whether a steward was armed, a session read `~/.local/share/wt/agentic/cursors/` and called `events.reader_holder()` from Python. Readers today: the conductor follows the event stream (`wt queue events --follow --for conductor`), and each steward follows its mailbox (`wt mailbox follow --to steward:<P>`). Each reader has `cursors/<safe>.json` (`{reader, after, ts}`), `<safe>.lock` and `<safe>.pid`. `wt queue conduct-status` reports only `conductor_pid`.

## Goals / Non-goals

**Goals**
- `wt queue followers [--json]` lists every reader: name, kind (events | mailbox), live pid (or `—` when not held), cursor position, lag (events or messages after the cursor), and cursor age.
- `wt queue conduct-status` (human and `--json`) includes the armed stewards.

**Non-goals**
- Starting or stopping followers.
- Notifications (IDEA-059).

## Decision

Read the cursors directory and the existing helpers (`events.reader_holder`, `events.cursor_get`, the events log and the mailbox inboxes) in a pure `events.followers(cfg)` function, and render it.

## Design

- `events.followers(cfg) -> list[dict]`: one row per `cursors/*.json`. Fields: `reader`; `kind` = `mailbox` when the reader starts with `mailbox:`, else `events`; `pid` = `reader_holder(cfg, reader)` (None when not held); `after`; `cursor_ts`; `lag` = count of events with id > after (events), or messages with seq > after in that address's inbox (mailbox); `held` bool. Sorted by kind, then reader.
- CLI `wt queue followers [--json]` in `cli_conduct.py`: a Rich table (reader, kind, pid / `not armed` dim, lag with yellow when > 0, cursor age), or `{schema: wt.queue.followers.v1, followers: […]}`.
- `conduct-status` payload gains `stewards: [{address, pid, lag}]`, and the human view a line `stewards: steward:Meta-Tools (pid N, lag 0)` or `none armed`.

## Alternatives considered

- **A registry file written by followers** — a second source of truth; the lock and cursor files already exist.

## Acceptance criteria

- [x] `wt queue followers --json` lists the conductor and one steward from a temp data dir where both followers hold their locks, with pid, lag and cursor fields; a reader whose lock is not held shows `pid: null, held: false`.
- [x] `lag` counts events (or messages) after the cursor.
- [x] `wt queue conduct-status --json` includes `stewards`; the human view shows them.
- [x] `uv run pytest` passes; `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests:** `tests/test_agentic_followers.py`: build a temp data dir; append events and mailbox messages; hold `reader_lock` for `conductor` and `mailbox:steward:P` in-process; set cursors; assert `followers()` rows (held, pid = os.getpid(), lag); release one lock → `held` False; CLI human + `--json`; `conduct-status --json` `stewards`.
- **Regression guard:** full `uv run pytest`.

### Test record (2026-10-25)

- `uv run pytest -q tests/test_agentic_followers.py` — 3 passed (rows/lag/held, empty, CLI human + `--json`, `conduct-status` stewards).
- Full `uv run pytest -q -x` — 1674 passed, 1 skipped; `python3 tools/spec_lint.py` — exit 0 (ledger regenerated).

### As shipped

`followers(cfg)` also returns `cursor_age_s` (seconds). `conduct-status` `stewards` lists only held mailbox followers (`address` is the steward address without the `mailbox:` prefix).

## Rollout / migration

Ships through the agentic queue (balanced profile). No data migration.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

(none)
