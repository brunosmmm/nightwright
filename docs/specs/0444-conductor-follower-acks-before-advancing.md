---
id: SPEC-0444
title: The conductor follower commits an event only after the session acks it
status: superseded
superseded_by: SPEC-0481
owner: user
created: 2026-11-07
updated: 2026-11-08
source_idea: IDEA-1016
tags: [agentic, conductor]
---

## Context

Superseded by [SPEC-0481](./0481-conductor-follower-stays-up.md). The ack-before-commit rule
stands. The follower no longer exits on a wake.

The Cursor conductor is woken by its harness when `nw queue events --follow --for conductor`
exits, or when that command was started with an output watch. The watch is a harness
parameter. It is not part of the `nw` command. The conduct skill tells the session to re-arm
"the same command" after `code_changed`. That command does not include the watch, so a
re-arm drops it.

`events.follow` then prints each line, flushes, and advances the conductor cursor. A
follower with no watch still consumes the stream. The session never wakes, and the next
re-arm does not replay those lines. On 2026-11-06 22:56 that happened: message events 6774,
6776, 6777, 6841 and 6842 were consumed while session 855e9bf1 stayed alive and answered
chat. Requests conductor:289, conductor:292 and conductor:293 sat unanswered until a person
typed in that chat.

A second failure sits on top. Stopping the shell that launched the follower did not stop
the `nw` child. The child kept the reader lock, so a re-arm failed with "already followed".

## Goals / Non-goals

**Goals**
- A wake line printed for the conductor is not committed until the session passes `--ack`.
- The conductor command exits after printing those lines, so the session is woken by the
  command finishing and does not depend on an output watch.
- A re-arm without `--ack` reprints the same lines.
- The follower releases the reader lock when its parent process dies.

**Non-goals**
- Changing mailbox followers, quiet holds, or digest commit behavior.
- Waking a session that never re-arms and never reads a finished command.
- Replaying events the cursor already passed before this spec.

## Decision

The harness watch cannot be kept inside `nw`. The conductor cursor commits a printed wake
line only when a later `nw queue events --follow --for conductor --ack ID` says the session
handled it. Until then the follower reprints that line and exits, which is the wake. Held
quiet events still commit, as they do today. Every events follower exits when its parent pid
changes, so a dead shell cannot keep the lock.

## Design

`events.follow` gains `ack`, `uncommitted` and `replay_s`.

- `ack` moves the cursor forward to `min(ack, last event id)` before the loop and clears
  `pending`. It never moves the cursor backward.
- `uncommitted` prints wake lines and does not call `cursor_set` for them. It records
  `pending` on the cursor file (the `after` field stays put) and returns, so the process
  exits. The next run reprints from `after`. When `pending` is already that first id and
  `replay_s` is set, it sleeps that long first so a session that re-arms without `--ack`
  does not tight-loop.
- The CLI sets `uncommitted` and `replay_s=5` only for `--for conductor`. `--ack` is
  forwarded. Other readers keep commit-on-print.
- At the start of each loop, if `os.getppid()` is no longer the pid captured at start, the
  follower returns and the lock drops.

The conduct skill re-arms with `--ack` of the last event id it handled. A `code_changed`
re-arm does the same, after re-reading the skill when `skills_changed` names it.

## Alternatives considered

- **Keep the output watch in the re-arm.** The watch is not part of the command the skill
  can repeat. The 2026-11-06 re-arm dropped it.
- **Advance the cursor only after a stdin ack while staying running.** A session with no
  watch never sees the line, so it never acks, and the process staying up is what hid the
  mail.

## Acceptance criteria

- [x] A conductor follow prints a new wake line, leaves `after` unchanged, and exits.
- [x] Running it again without `--ack` prints that same line.
- [x] `--ack ID` commits the cursor through `ID` (clamped to the last event) and the next
  follow does not print that line.
- [x] A follow whose parent pid changes returns without holding the reader lock.
- [x] Readers other than `conductor` still commit on print.
- [x] The conduct skill tells the session to re-arm with `--ack`.

## Test plan

- **Automated tests:** `tests/test_agentic_events.py` covers uncommitted replay, `--ack`,
  parent death, and the conductor CLI passing `uncommitted`. Existing follow tests stay on
  the commit-on-print default.
- **Manual verification:** `nw queue events --follow --for conductor` against a copy is not
  run against the live conductor. The unit tests are the check.
- **Regression guard:** `tests/test_agentic_events.py`, `tests/test_agentic_quiet.py`.

## Rollout / migration

The live conductor cursor is already past the events lost on 2026-11-06. This spec does not
rewind it. The next `code_changed` exits the old follower. The session re-arms. Events
printed after this lands wait for `--ack`.

### As shipped

`events.follow` takes `ack`, `uncommitted` and `replay_s`. The conductor CLI sets
`uncommitted` and `replay_s=5`. `--ack` commits forward, clamped to the last event id.
`pending` on the cursor file is the last printed uncommitted id. A changed parent pid makes
the follower return. Quiet holds still commit. `tests/test_agentic_events.py` and
`tests/test_agentic_quiet.py`: 171 passed.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` regenerated and `python3 tools/spec_lint.py` exits 0.
