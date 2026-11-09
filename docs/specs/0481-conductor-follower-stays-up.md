---
id: SPEC-0481
title: The conductor follower stays up across a wake
status: done
updated: 2026-11-08
owner: user
created: 2026-11-08
tags: [agentic, conductor]
supersedes: [SPEC-0444]
source_idea: IDEA-1037
---

## Context

`nw queue events --follow --for conductor` prints one batch of wake lines and exits
(SPEC-0444). A park is a wake, so every park ends the process. The reader lock drops,
`conductor_pid` goes null, and the command's `finally` stops the herdr watcher that follower
started. A session that is not blocked on that shell does not start it again. The follower
stays down, and the panes that watcher owned are already closed.

SPEC-0444 made the exit the wake, because a follower that stayed up and advanced the cursor
could consume mail the session never saw. The commit rule from that spec still holds. The
exit does not.

## Decision

A conductor wake does not end the follower. The process prints each new wake once, leaves
`after` where it is, records `pending`, and keeps the reader lock. It still exits on
`code_changed`, when its parent pid changes, on Ctrl-C or a broken pipe, when `stop`
returns true, and when `max_events` is reached. Those exits still stop the herdr watcher.
A wake does not.

The cursor commits only when the session acks. `nw queue events --ack ID --for conductor`
without `--follow` commits while the follower holds the reader lock. A new process reprints
from `after` until that ack, then stays up.

## Design

`events.follow` with `uncommitted` no longer returns after noting `pending`. It remembers
the ids it has printed and skips them on later polls. A held event whose id is past that
mark does not call `cursor_set`, because `cursor_set` replaces the cursor file and would
move `after` past the unacked wake.

`events.commit_ack` moves `after` forward to `min(ack, last event id)` and clears `pending`
when that id is at or behind the new cursor. It never moves `after` backward. It takes a
cursor-file lock, not the reader lock. `cursor_note_pending` takes the same lock and does
not write `pending` when `after` is already at or past that id.

The CLI accepts `--ack` without `--follow` and prints `nw.events.ack.v1`. `--follow` still
passes `ack` into `follow`, which commits before the loop.

The conduct skill tells the session that a park does not end the follower, to ack with
`--ack` and no second `--follow`, and to re-arm only after `code_changed`, parent death,
or Ctrl-C.

## Alternatives considered

- **Restart the follower the moment it exits.** The process is still down between the exit
  and the restart, and that exit still stops the watcher and closes the panes.
- **Leave the exit in place and only keep the watcher alive.** Conducting stays down for
  the same reason: nothing is holding the conductor reader lock, so nothing is reading the
  next park.

## Acceptance criteria

- [x] A conductor follow prints a new wake, leaves `after` unchanged, records `pending`, and
      does not exit. A second poll prints nothing more.
- [x] A new process without an ack reprints that wake, then stays up.
- [x] `--ack ID` commits through `ID`. `--ack` without `--follow` does this while the reader
      lock is held, and clears `pending`.
- [x] A follow whose parent pid changes returns and drops the reader lock.
- [x] `code_changed` still makes the follower return.
- [x] Readers other than the conductor still commit on print.
- [x] The conduct skill says the follower stays up, and to ack without starting a second
      follow.

## Test plan

- [x] `tests/test_agentic_events.py::test_uncommitted_follow_replays_until_ack`
- [x] `tests/test_agentic_events.py::test_uncommitted_follow_stays_up_across_wakes`
- [x] `tests/test_agentic_events.py::test_cli_ack_without_follow`
- [x] `tests/test_agentic_events.py::test_follow_returns_when_its_parent_dies`
- [x] `tests/test_agentic_events.py::test_follow_exits_with_code_changed_and_resumes`
- [x] `tests/test_agentic_events.py::test_cli_events_follow_no_code_watch`
- [x] `tests/test_agentic_events.py`, `tests/test_agentic_quiet.py`, and
      `tests/test_agentic_followers.py` — 179 passed.
- [x] The conduct skill text is the check for the last criterion.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; the named tests pass.
- [x] No regressions in those tests.
- [x] Spec body matches what shipped.
- [x] `docs/LEDGER.md` regenerated and `python3 tools/spec_lint.py` exits 0.
