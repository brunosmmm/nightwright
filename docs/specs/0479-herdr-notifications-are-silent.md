---
id: SPEC-0479
title: Herdr notifications are silent
status: done
owner: user
created: 2026-11-08
updated: 2026-11-08
tags: [agentic, herdr]
depends_on: [SPEC-0361]
---

## Context

`nw herdr watch` asks herdr to play a sound on four queue events: an escalation, a
non-transient run failure, and a conductor-owned park use `--sound request`; a merge uses
`--sound done` (SPEC-0360 invariant 8, SPEC-0361). Herdr plays that sound. The human does
not want it.

## Decision

Those notifications still show. The sound is always `none`. Nightwright does not ask herdr
to play `request` or `done`.

## Design

`notification_for` in `src/nightwright/agentic/herdr_watch.py` keeps the same four events
and the same titles and bodies. The third element of each tuple is `none`.
`notify_args` still accepts `done` and `request`; nothing in the watcher passes them.

## Acceptance criteria

- [x] An escalation, a non-transient run failure, a conductor-owned park, and a merge each
      produce one `notification show` with `--sound none`.
- [x] A transient run failure, a park the conductor does not own, and any other event
      produce no notification.
- [x] A second watcher run does not repeat a notification already sent.

## Test plan

- [x] `tests/test_herdr.py::test_notifications_once_per_event_are_silent`
- [x] `tests/test_herdr.py::test_notification_policy`
- [x] `tests/test_herdr.py::test_new_cursor_does_not_replay_history`
- [x] `uv run pytest -q` of those three tests plus `test_argv_builders` — 4 passed.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; the named tests pass.
- [x] No regressions in those tests.
- [x] Spec body matches what shipped.
- [x] `docs/LEDGER.md` regenerated.

## As shipped

`notification_for` returns `none` for every notification it still sends.
