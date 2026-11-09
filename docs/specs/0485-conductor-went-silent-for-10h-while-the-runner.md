---
id: SPEC-0485
title: "Conductor liveness when pings stay unacked"
status: proposed
owner: user
created: 2026-11-09
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
`--ack` ([SPEC-0481](./0481-conductor-follower-stays-up.md)). A hold whose id is past that
unacked wake does not move `after`. Pings keep appending, the cursor stays behind them, and
the session is the only thing that was supposed to notice. `launch.request_relaunch`
([SPEC-0424](./0424-launch-freshness-and-steward-dontask.md)) mails the human when a
launch hash is stale. It does not watch the ack cursor, and `launch.run` replaces the
current process with the agent, so a follower cannot use it to start a second conductor.

## Goals / Non-goals

**Goals**

- A pinging follower detects that the conductor cursor has stayed behind the newest
  `conductor_ping` past a threshold.
- That episode appends one `conductor_unresponsive` event, mails the human once with the
  relaunch command, and shows one herdr toast.
- An ack through the newest ping clears the episode. A later stall raises a new one.
- A healthy quiet follower, a missing conductor cursor, a disabled threshold, and a
  non-pinging `sync` leave the stream and the human inbox alone.

**Non-goals**

- Restarting the runner, or changing the systemd unit in SPEC-0483.
- Changing the meaning of `conductor_ping.data.runner`. Null remains "no silent live
  runner".
- Spawning, signalling, or killing a conductor session. The relaunch command is for the
  human to run in the conductor's terminal.
- Detecting silence when no follower is calling `sync(..., ping=True)`. The pings in the
  incident were produced by such a follower, and that is the process that runs this check.
- Waking the conductor on every empty ping while the runner is down.

## Decision

The conductor is unresponsive when a conductor cursor file exists, the newest
`conductor_ping` id is greater than that cursor's `after`, and the cursor `ts` is at least
`conductor_unacked_after_s` old. The default threshold is 1800 seconds. `0` turns the check
off. A positive setting at or below `ping_interval_s` is treated as 1800, because a healthy
quiet follower's cursor `ts` is about one ping interval old at the moment the next ping is
appended, before that ping is held.

The check runs inside `sync` only when `ping=True`, on every such call, after `_ping`. It
fires once per episode. The episode stays open until `after` is at or beyond the newest
ping id, including pings that arrived after the alert. The alert is an event plus one mail
to the human whose text contains the relaunch command: `nw queue conductor-launch --resume`
when the conductor registry has a `source: launch` record, and `nw queue conductor-launch`
otherwise. Herdr toasts that event. The check does not spawn a process and does not signal
the follower pid.

A value of 1800 is the stall that counts. A session that has not acked for half an hour has
left the follower stuck on that wake, which is how the ten-hour gap happened. A shorter
default would overlap one healthy ping interval.

## Design

`events.DEFAULTS` gains `conductor_unacked_after_s: 1800`, read through `events.settings`
like the other durations (`agentic.events.conductor_unacked_after_s`).

`_ping` records `proj["last_ping_id"]` to the id it appends. The projector keys
`conductor_unresponsive_ping` (the ping id the open episode alerted on) and
`conductor_unresponsive_mailed` (true once `mailbox.send` has returned) live beside
`last_ping`. They are not stored in `proj["conditions"]`, which `_watch` replaces on every
sync. An old projector without `last_ping_id` skips the check until the next appended ping
writes it.

`events.note_conductor_liveness(cfg, proj, now)` runs inside the events lock, after `_ping`,
only from `sync(..., ping=True)`. It never calls `reader_holder`: that probe takes the
reader flock and, in this process, would unlock the follower that is inside `follow`. It
reads the conductor cursor with the cursor-file lock (`after`, `ts`) and reads
`<cursors>/conductor.pid` as text. `follower_pid` is that pid when `gate_slots._pid_alive`
says it is alive, otherwise null.

The check is a no-op unless `last_ping_id` is present, the conductor cursor file exists,
and `last_ping_id > after`. `unacked_s` is the age of the cursor `ts`. A missing or
unparseable `ts` is a no-op. Let `limit` be 1800 when the configured threshold is positive
and `<= ping_interval_s`; otherwise the configured value. `limit <= 0` is a no-op. When
`unacked_s < limit`, the check does not open an episode.

When the predicate holds and `conductor_unresponsive_ping` is absent, append
`conductor_unresponsive` with `by=watchdog` and data:

- `ping_id`: `last_ping_id`
- `cursor_after`: the cursor `after`
- `unacked_s`: the integer age
- `follower_pid`: the live pid or null
- `relaunch`: the command above

Set `conductor_unresponsive_ping` to `ping_id`. While that key is set, do not append
another event. When `after >= last_ping_id`, drop both projector keys.

Mail, still inside the events lock and only to `human` (so the send does not take the
events lock again), `frm=conductor`, `kind=info`, once per episode:

`conductor has not acked past event {cursor_after} for {unacked_s}s (newest ping #{ping_id}, follower {pid or down}). Relaunch: {relaunch}`

Set `conductor_unresponsive_mailed` only after `send` returns. A failed send leaves the
flag clear so the next pinging sync retries the mail and does not append a second event.
Any exception from the check or the mail is swallowed. Nothing from this path is written
to the follower's stdout.

`EVENT_TYPES["conductor_unresponsive"]` requires those five data keys. `summary_line` for
this type is one line:

`#{id} conductor_unresponsive unacked_s={unacked_s} after={cursor_after} ping={ping_id} follower={pid or down}`

`quiet.RULES["conductor_unresponsive"]` is `"wake"`.

`herdr_watch.notification_for` returns `("nw: conductor silent", "unacked {unacked_s}s. {relaunch}", "none")` for this type. Sound stays `none` ([SPEC-0479](./0479-herdr-notifications-are-silent.md)).

`skills/nw-conduct/SKILL.md` gains a decision-table row:

`conductor_unresponsive` — the conductor cursor sat behind a ping for `data.unacked_s` seconds (SPEC-0485; `data`: `ping_id`, `cursor_after`, `unacked_s`, `follower_pid`, `relaunch`). The human was already mailed `data.relaunch`. Do not spawn, stop, or kill a session. Ack through this event. When the runner is down, act on the `runner_dead` row. Record `nw queue decision --kind other --rule conductor_unresponsive`.

`docs/product/guides/agent-surface.md` lists `conductor_unresponsive` among the watchdog
signals on the `nw.event.v1` row.

## Alternatives considered

- **Put a null `runner` into `ping_fingerprint`.** That wakes a live conductor on every
  ping while the lease is free. The incident was a conductor that was not acking, so those
  wakes would have sat on the same stuck cursor.
- **Spawn `nw queue conductor-launch` from the follower.** `launch.run` replaces the
  current process. A second session cannot take the reader lock while the stuck follower
  still holds it. SPEC-0424 leaves the terminal to the human and names the command instead.
- **Alert only when the follower pid is dead.** The follower that kept appending pings was
  alive. SPEC-0481 is that case: the process stays up and `after` does not move.

## Acceptance criteria

- [ ] A conductor cursor behind `last_ping_id` with `ts` at least 1800s old causes one
      `conductor_unresponsive` event on `sync(ping=True)`, carrying `ping_id`,
      `cursor_after`, `unacked_s`, `follower_pid`, and `relaunch`. A second pinging sync in
      the same episode appends no further event.
- [ ] That episode writes one human mail, `from` conductor, kind `info`, whose text
      contains the unacked seconds and the same relaunch command. A failed send is retried
      on a later pinging sync and still leaves a single event.
- [ ] `relaunch` is `nw queue conductor-launch --resume` when a conductor session record
      has `source: launch`, and `nw queue conductor-launch` when none does.
- [ ] `follower_pid` is the pid in the conductor pid file when that pid is alive, and null
      when the file is missing or the pid is dead. The check does not drop a reader lock
      the caller already holds.
- [ ] No event and no human mail when the cursor `after` is at or past the newest ping,
      when the cursor `ts` is younger than the limit, when no conductor cursor file exists,
      when `conductor_unacked_after_s` is 0, or when `sync` is called with `ping=False`.
- [ ] A configured threshold greater than 0 and at or below `ping_interval_s` behaves as
      1800: a cursor age between the two does not fire, and an age of at least 1800 does.
- [ ] After the cursor is acked to the newest ping, the episode keys are gone. A later
      stall at or past the limit appends a new event and a new mail.
- [ ] `summary_line` for the event is a single line of the form in Design.
      `validate("conductor_unresponsive", data)` requires the five keys. A `conductor_ping`
      whose `runner` is null still validates.
- [ ] `quiet.RULES["conductor_unresponsive"]` is `wake`. `notification_for` returns the
      title `nw: conductor silent`, a body containing the relaunch command, and sound
      `none`.
- [ ] The conduct skill row contains `data.relaunch`, tells the session not to spawn or
      kill a session, and tells it to record `--rule conductor_unresponsive`. The
      `nw.event.v1` row in the agent-surface guide names `conductor_unresponsive`.

## Test plan

- **Automated tests:** `tests/test_conductor_liveness.py`, injected clock, temporary cfg.
  - `test_unacked_ping_escalates_once` — cursor `after` behind a ping, `ts` older than
    1800s; two `sync(ping=True)` calls; one event with the five keys; one human mail
    whose text contains `unacked_s` and `relaunch`.
  - `test_mail_failure_is_retried_without_a_second_event` — `mailbox.send` raises once,
    then succeeds; one event and one mail.
  - `test_relaunch_command_follows_the_launch_record` — with a `source: launch` conductor
    session the command is `nw queue conductor-launch --resume`; with no launch record it
    is `nw queue conductor-launch`.
  - `test_follower_pid_and_the_reader_lock_stays_held` — a live pid file is copied into
    `follower_pid`; a dead pid yields null; `sync(ping=True)` while this process holds
    `reader_lock` for `conductor` leaves that lock held.
  - `test_healthy_young_missing_disabled_and_non_ping_sync_stay_quiet` — caught-up cursor,
    `ts` younger than the limit, no cursor file, threshold 0, and `sync(ping=False)` each
    append no `conductor_unresponsive` and send no human mail.
  - `test_threshold_inside_the_ping_interval_uses_1800` — threshold 300 and `ping_interval_s`
    600; cursor age 900 does not fire; cursor age 1800 does.
  - `test_ack_clears_the_episode` — `commit_ack` through the newest ping, then a new stall
    past the limit, produces a second event and a second mail.
  - `test_summary_line_and_validate` — the summary is one line and contains `unacked_s=`;
    missing keys fail `validate`; a ping payload with `"runner": null` still validates.
- **Quiet, herdr, skill:** `tests/test_agentic_quiet.py` (or the liveness module) asserts
  `RULES["conductor_unresponsive"] == "wake"`. Extend `tests/test_herdr.py::test_notification_policy`
  for the toast title, the relaunch text, and sound `none`. Extend `tests/test_skills.py`
  so the conduct row contains `data.relaunch`, `conductor_unresponsive`, and the
  decision `--rule`. Assert the agent-surface `nw.event.v1` line contains
  `conductor_unresponsive`.
- **Manual verification:** none. The incident shape is the stalled cursor plus a null
  `runner` on the ping, and the injected clock covers it.
- **Regression guard:** `tests/test_agentic_ping.py`, `tests/test_agentic_events.py`, and
  `tests/test_agentic_quiet.py` still pass. A healthy follower that holds each ping inside
  the interval must still produce no `conductor_unresponsive`.

## Clock Log

Implementation sessions clock in and out on IDEA-1039 with `nw idea clock-in IDEA-1039`
and `nw idea clock-out IDEA-1039`. No implementation session has run.

## Rollout / migration

The event type is additive. A projector that has `last_ping` and no `last_ping_id` does
not alert until the next ping records the id. Absence of the episode keys means no open
episode. The default threshold is on. `agentic.events.conductor_unacked_after_s: 0`
disables the check without a code change. No cursor or ledger rewrite.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; the named tests pass. Manual steps: none.
- [ ] No regressions in the named regression-guard files.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` regenerated and `python3 tools/spec_lint.py` exits 0.

## Open questions

None.
