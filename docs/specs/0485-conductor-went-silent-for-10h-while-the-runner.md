---
id: SPEC-0485
title: "Conductor liveness when pings stay unacked"
status: accepted
owner: user
created: 2026-11-09
updated: 2026-11-09
milestone: "M9: agentic workflow"
kind: feature
tags: [agentic, conductor, events, watchdog]
source_idea: IDEA-1039
---

## Context

On 2026-11-08 from 21:19 through 2026-11-09 07:14 the watchdog appended `conductor_ping`
events (about 7680–7756) with `runner: null` every few minutes. Nothing restarted the
runner and nothing told the human. The runner process had died at 21:19
([SPEC-0484](./0484-runner-contains-apply-errors-and-holds-on-backend-faults.md)); putting
that process under a systemd user unit is
[SPEC-0483](./0483-runner-under-a-systemd-user-unit.md). This spec is the other half: the
conductor session that should have acted was silent for those ten hours.

`runner: null` on a ping is the existing `runner_silence` result
(`src/nightwright/agentic/events.py`, `runner_silence`): the lease is free, stale, or has
no `tick_at`, so the helper returns None and `ping_digest` stores that under `runner`. A
dead runner is already a one-shot `runner_dead` wake. Quiet mode does not treat a null
`runner` as ping content (`quiet.ping_fingerprint` adds a runner pair only when `runner`
is truthy), so the later pings are holds.

The conductor follower stays up across a wake and commits its cursor only on
`--ack` ([SPEC-0481](./0481-conductor-follower-stays-up.md)). A printed wake records
`pending` through `cursor_note_pending` and leaves `after` where it is. After that first
printed wake, `follow` skips `cursor_set` for a later hold, so `after` stays behind newer
`conductor_ping` ids even once `pending` is gone. `cursor_note_pending` rewrites `ts` on
every new wake, so a session that keeps printing wakes never grows an old `ts`. The
incident cursor aged only because the later lines were holds. `launch.request_relaunch`
([SPEC-0424](./0424-launch-freshness-and-steward-dontask.md)) mails the human when a
launch hash is stale. It does not watch the ack cursor, and `launch.run` replaces the
current process with the agent, so a follower cannot use it to start a second conductor.
The incident follower was alive and still held the conductor reader lock, so
`nw queue conductor-launch` from another terminal raises that the reader is already
followed.

## Goals / Non-goals

**Goals**

- A pinging follower detects that the conductor cursor has had pending > after (an unacked wake) for conductor_unacked_after_s.
- That episode appends one `conductor_unresponsive` event, mails the human once with the
  relaunch command, and shows one herdr toast.
- An ack that clears pending (after at or beyond that id) clears the episode. A later
  unacked wake that reaches the threshold raises a new one.
- A healthy quiet follower, a missing conductor cursor, a disabled threshold, and a
  non-pinging `sync` leave the stream and the human inbox alone.

**Non-goals**

- Restarting the runner, or changing the systemd unit in SPEC-0483.
- Changing the meaning of `conductor_ping.data.runner`. Null remains "no silent live
  runner".
- Spawning, signalling, or killing a conductor session. The human stops that session in
  the conductor's terminal, so its follower drops the reader lock, and then runs the
  relaunch command there.
- Detecting silence when no follower is calling `sync(..., ping=True)`. The pings in the
  incident were produced by such a follower, and that is the process that runs this check.
- Waking the conductor on every empty ping while the runner is down.

## Decision

The conductor is unresponsive when the conductor cursor file has pending greater than after and that unacked wake has stayed ahead of after for at least conductor_unacked_after_s (default 1800; 0 turns the check off). Do not compare last_ping_id to after, and do not floor the threshold at 1800 when it is below ping_interval_s. cursor ts is not the age: cursor_note_pending rewrites it on every new wake, and after the first printed wake follow does not cursor_set later holds.

The check runs inside `sync` only when `ping=True`, on every such call, after `_ping`. It
fires once per episode. The episode stays open until pending is absent or after >= pending. Held pings after the alert do not keep it open once that wake is acked. A later unacked wake that reaches the threshold opens a new episode. The alert is an event plus one mail
to the human. The mail tells the human to stop that session in the conductor's terminal,
so the follower drops the reader lock, and then gives the relaunch command:
`nw queue conductor-launch --resume` when the conductor registry has a `source: launch`
record, and `nw queue conductor-launch` otherwise. Herdr toasts that event and tells the
human to stop the conductor session before the same command. The check does not spawn a
process and does not signal the follower pid.

A value of 1800 is the stall that counts. A session that has not acked for half an hour has
left the follower stuck on that wake, which is how the ten-hour gap happened.

## Design

`events.DEFAULTS` gains `conductor_unacked_after_s: 1800`, read through `events.settings`
like the other durations (`agentic.events.conductor_unacked_after_s`). `limit` is that
configured value. `limit <= 0` is a no-op with no 1800 floor, and `ping_interval_s` does
not change it.

`_ping` records `proj["last_ping_id"]` to the id it appends, when it appends one. The
projector keys `conductor_unacked_since` (ISO timestamp from `store.now_iso` of the first
sync that saw the current unacked wake), `conductor_unresponsive_ping` (the `ping_id` the
open episode alerted on), and `conductor_unresponsive_mailed` (true once `mailbox.send`
has returned) live beside `last_ping`. They are not stored in `proj["conditions"]`, which
`_watch` replaces on every sync. The check does not require `last_ping_id`.

`events.note_conductor_liveness(cfg, proj, now)` runs inside the events lock, after `_ping`,
only from `sync(..., ping=True)`. It never calls `reader_holder`: that probe takes the
reader flock and, in this process, would unlock the follower that is inside `follow`. It
reads `after`, `pending`, and `ts` under the cursor-file lock and reads
`<cursors>/conductor.pid` as text. `ts` is not the age. `follower_pid` is that pid when
`gate_slots._pid_alive` says it is alive, otherwise null. A missing conductor cursor file
is treated as `pending` absent.

On each pinging sync, when `pending` is absent or `after >= pending`, drop
`conductor_unacked_since` and both episode keys and return. That clear runs even when
`_ping` has just appended a newer ping, so `after` behind `last_ping_id` with no `pending`
drops a stuck `conductor_unresponsive_ping` instead of suppressing the next episode.

When `pending > after`, set `conductor_unacked_since` on the first such sync and leave it
in place if `pending` moves forward while `after` does not. `unacked_s` is the integer age
of `conductor_unacked_since` (`int` of `events._age_s`). A missing or unparseable since is
the first observation: set it to now, so this sync's age is 0. `limit <= 0` returns after
the clear step, without setting `conductor_unacked_since` and without appending. When
`unacked_s < limit`, the check does not open an episode.

When `unacked_s >= limit` and `conductor_unresponsive_ping` is absent, append
`conductor_unresponsive` with `by=watchdog` and data:

- `ping_id`: `last_ping_id` if `_ping` has recorded one, otherwise `pending`
- `cursor_after`: the cursor `after`
- `unacked_s`: the integer age of `conductor_unacked_since`
- `follower_pid`: the live pid or null
- `relaunch`: the command above

Set `conductor_unresponsive_ping` to `ping_id`. While that key is set, do not append
another event.

Mail, still inside the events lock and only to `human` (so the send does not take the
events lock again), `frm=conductor`, `kind=info`, once per episode:

`conductor has not acked past event {cursor_after} for {unacked_s}s (newest ping #{ping_id}, follower {pid or down}). In the conductor's terminal, stop that session so its follower drops the reader lock, then run: {relaunch}`

Set `conductor_unresponsive_mailed` only after `send` returns. A failed send leaves the
flag clear so the next pinging sync retries the mail and does not append a second event.
Any exception from the check or the mail is swallowed. Nothing from this path is written
to the follower's stdout.

`EVENT_TYPES["conductor_unresponsive"]` requires those five data keys. `summary_line` for
this type is one line:

`#{id} conductor_unresponsive unacked_s={unacked_s} after={cursor_after} ping={ping_id} follower={pid or down}`

`quiet.RULES["conductor_unresponsive"]` is `"wake"`.

`herdr_watch.notification_for` returns `("nw: conductor silent", "unacked {unacked_s}s. Stop the conductor session, then {relaunch}", "none")` for this type. Sound stays `none` ([SPEC-0479](./0479-herdr-notifications-are-silent.md)).

`skills/nw-conduct/SKILL.md` gains a decision-table row:

`conductor_unresponsive` — an unacked wake (`pending` > `after`) stayed ahead of `after` for `data.unacked_s` seconds (SPEC-0485; `data`: `ping_id`, `cursor_after`, `unacked_s`, `follower_pid`, `relaunch`). The human was already mailed to stop that session and run `data.relaunch`. Do not spawn, stop, or kill a session. Ack through this event. When the runner is down, act on the `runner_dead` row. Record `nw queue decision --kind other --rule conductor_unresponsive`.

`docs/product/guides/agent-surface.md` lists `conductor_unresponsive` among the watchdog
signals on the `nw.event.v1` row.

## Alternatives considered

- **Age the cursor `ts`, and treat `last_ping_id > after` as the stall.** After the first
  printed wake, `follow` does not `cursor_set` later holds, so a healthy conductor that
  acked a wake and then only held pings leaves `after` behind those pings. With a 1800s
  floor that pages a healthy session. The other way, `cursor_note_pending` rewrites `ts`
  on every new wake, so a dead session whose queue still prints wakes never accumulates
  1800s of cursor age. Requiring `last_ping_id > after` also makes the clear unreachable:
  the check runs after `_ping`, and a sync that just appended a ping never sees `after`
  already caught up to that id. A stuck `conductor_unresponsive_ping` then suppresses
  every later episode.
- **Put a null `runner` into `ping_fingerprint`.** That wakes a live conductor on every
  ping while the lease is free. The incident was a conductor that was not acking, so those
  wakes would have sat on the same stuck cursor.
- **Spawn `nw queue conductor-launch` from the follower.** `launch.run` replaces the
  current process. A second session cannot take the reader lock while the stuck follower
  still holds it. Running the command from another terminal raises that reader `conductor`
  is already followed by the live pid. SPEC-0424 leaves the terminal to the human and
  names the command instead.
- **Alert only when the follower pid is dead.** The follower that kept appending pings was
  alive. SPEC-0481 is that case: the process stays up and `after` does not move until the
  wake is acked.

## Acceptance criteria

- [ ] A conductor cursor with `pending > after` and `conductor_unacked_since` at least
      1800s old causes one `conductor_unresponsive` event on `sync(ping=True)`, carrying
      `ping_id`, `cursor_after`, `unacked_s`, `follower_pid`, and `relaunch`. A second
      pinging sync in the same episode appends no further event.
- [ ] That episode writes one human mail, `from` conductor, kind `info`, whose text
      contains the unacked seconds, the phrase "In the conductor's terminal", and
      "stop that session" before the relaunch command. A failed send is retried on a
      later pinging sync and still leaves a single event.
- [ ] `relaunch` is `nw queue conductor-launch --resume` when a conductor session record
      has `source: launch`, and `nw queue conductor-launch` when none does.
- [ ] `follower_pid` is the pid in the conductor pid file when that pid is alive, and null
      when the file is missing or the pid is dead. The check does not drop a reader lock
      the caller already holds.
- [ ] No event and no human mail when `pending` is absent, even if `after` is behind
      `last_ping_id` and the cursor `ts` is at least 1800s old; when `pending > after` but
      `conductor_unacked_since` is younger than the limit; when no conductor cursor file
      exists; when `conductor_unacked_after_s` is 0; or when `sync` is called with
      `ping=False`.
- [ ] A configured threshold of 300 fires at unacked age 300 and does not fire at 299;
      `ping_interval_s` does not change it. A cursor `ts` rewritten by later wakes does
      not reset the age.
- [ ] `commit_ack` through `pending` drops the episode keys and `conductor_unacked_since`
      on the next pinging sync. Held pings after that ack do not open an episode. A new
      wake left unacked past the limit appends a new event and a new mail.
- [ ] `summary_line` for the event is a single line of the form in Design.
      `validate("conductor_unresponsive", data)` requires the five keys. A `conductor_ping`
      whose `runner` is null still validates.
- [ ] `quiet.RULES["conductor_unresponsive"]` is `wake`. `notification_for` returns the
      title `nw: conductor silent`, a body containing "Stop the conductor session" and the
      relaunch command, and sound `none`.
- [ ] The conduct skill row contains `data.relaunch`, tells the session not to spawn or
      kill a session, and tells it to record `--rule conductor_unresponsive`. The
      `nw.event.v1` row in the agent-surface guide names `conductor_unresponsive`.

## Test plan

- **Automated tests:** `tests/test_conductor_liveness.py`, injected clock, temporary cfg.
  - `test_unacked_ping_escalates_once` — `pending > after`; the first pinging sync records
    `conductor_unacked_since` and appends nothing; a pinging sync once that since is at
    least 1800s old appends one event with the five keys; a further pinging sync in the
    same episode appends no further event; one human mail whose text contains the unacked
    seconds, "In the conductor's terminal", "stop that session", and `relaunch`, with the
    stop sentence before the command.
  - `test_mail_failure_is_retried_without_a_second_event` — `mailbox.send` raises once,
    then succeeds; one event and one mail.
  - `test_relaunch_command_follows_the_launch_record` — with a `source: launch` conductor
    session the command is `nw queue conductor-launch --resume`; with no launch record it
    is `nw queue conductor-launch`.
  - `test_follower_pid_and_the_reader_lock_stays_held` — a live pid file is copied into
    `follower_pid`; a dead pid yields null; `sync(ping=True)` while this process holds
    `reader_lock` for `conductor` leaves that lock held.
  - `test_healthy_young_missing_disabled_and_non_ping_sync_stay_quiet` — `pending` absent
    after an acked wake, with `after` behind `last_ping_id`, cursor `ts` at least 1800s
    old, and later lines that are held pings; `pending > after` with
    `conductor_unacked_since` younger than the limit; no cursor file; threshold 0; and
    `sync(ping=False)`. Each appends no `conductor_unresponsive` and sends no human mail.
  - `test_threshold_300_fires_at_300_not_299` — configured threshold 300; unacked age 299
    does not fire; age 300 does; `ping_interval_s` does not change either result; a later
    wake that rewrites cursor `ts` does not reset the age.
  - `test_ack_clears_the_episode` — `commit_ack` through `pending`, then the next pinging
    sync drops the episode keys and `conductor_unacked_since`; held pings after that ack,
    including ones past 1800s, append no event and send no mail; a new wake left unacked
    past the limit appends one new event and one new mail.
  - `test_summary_line_and_validate` — the summary is one line and contains `unacked_s=`;
    missing keys fail `validate`; a ping payload with `"runner": null` still validates.
- **Quiet, herdr, skill:** `tests/test_agentic_quiet.py` (or the liveness module) asserts
  `RULES["conductor_unresponsive"] == "wake"`. Extend `tests/test_herdr.py::test_notification_policy`
  for the toast title, a body containing "Stop the conductor session" and the relaunch
  text, and sound `none`. Extend `tests/test_skills.py`
  so the conduct row contains `data.relaunch`, `conductor_unresponsive`, and the
  decision `--rule`. Assert the agent-surface `nw.event.v1` line contains
  `conductor_unresponsive`.
- **Manual verification:** none. The incident shape is an unacked wake (`pending > after`)
  held past the limit while pings carry a null `runner`, and the injected clock covers it.
- **Regression guard:** `tests/test_agentic_ping.py`, `tests/test_agentic_events.py`, and
  `tests/test_agentic_quiet.py` still pass. A healthy follower that acks its wakes, then
  holds later pings with an old cursor `ts` and `after` behind `last_ping_id`, must still
  produce no `conductor_unresponsive`.

## Clock Log

Implementation sessions clock in and out on IDEA-1039 with `nw idea clock-in IDEA-1039`
and `nw idea clock-out IDEA-1039`. No implementation session has run.

## Rollout / migration

The event type is additive. `ping_id` is `last_ping_id` when `_ping` has recorded one,
otherwise the cursor `pending`, so a projector that has not yet stored `last_ping_id`
still alerts on an unacked wake. The clear drops `conductor_unacked_since` and both
episode keys when `pending` is absent or `after >= pending`, including on a sync that just
appended a newer ping. Absence of those keys means no open episode. The default threshold
is on. `agentic.events.conductor_unacked_after_s: 0` disables the check without a code
change and without a 1800 floor. No cursor or ledger rewrite.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; the named tests pass. Manual steps: none.
- [ ] No regressions in the named regression-guard files.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` regenerated and `python3 tools/spec_lint.py` exits 0.

## Open questions

None.
