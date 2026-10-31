---
id: SPEC-0317
title: "Quiet follower skips a conductor_digest that holds only empty pings (nothing to act on)"
status: done
owner: user
created: 2026-10-31
updated: 2026-10-31
milestone: "M10: agentic MVP"
kind: feature
source_idea: IDEA-485
tags: [agentic, conductor, events, cost]
depends_on: [SPEC-0300]
---

## Context

[SPEC-0300](./0300-quiet-conductor-follower-digest.md) makes the `conductor` follower hold
informational events and print them as one synthetic `conductor_digest` line once the oldest
held event is older than `digest_interval_s` (default 1800 s). A `conductor_ping` whose
actionable fingerprint is empty is *held* (`quiet.classify`), and `Digest.add` counts it
(`pings_held += 1`, `ping_items = []`).

The digest's only gate is `Digest.due` (`src/wt/agentic/quiet.py`): it is true when the digest
is non-empty and its oldest held event is older than the interval. On an idle queue the
watchdog still raises a `conductor_ping` every 10 min, each one held with no actionable
items. The pending digest therefore holds nothing but `held: {"conductor_ping": n}`,
`pings_held: n`, `ping_items: []`, and `follow` (`src/wt/agentic/events.py`) prints it every
30 min. That is one wakeup, one API turn that re-reads the whole conductor context, whose
`/wt-conduct` row says "Do nothing else". The line carries no information the conductor can
use: SPEC-0300's own rule is "an empty digest is never printed", but its emptiness test looks
at whether any event was held, not whether anything in the digest is worth a turn.

This is the idle-queue case of the wakeups-per-merge cost metric (exit criterion E3 of
[SPEC-0298](./0298-agentic-mvp-cheap-durable-quiet-conductor.md)).

## Goals / Non-goals

**Goals**
- A pending digest whose only content is held `conductor_ping` events with no `ping_items`
  is never printed and is not a wakeup.
- Any other content still produces a digest as today: another held event type, or non-empty
  `ping_items`.

**Non-goals**
- Changing which events are held or woken (`quiet.RULES`, `classify`), the ping's content, or
  the ping fingerprint rules.
- Suppressing digests that hold other purely informational events (`admitted`, `waiting`,
  `cost_warning`, …). They stay deliverable as SPEC-0300 decided; only the ping-only digest
  is the bug.
- Changing `events.jsonl`, `wt queue events` history, steward followers or the format of a
  digest that is printed.

## Decision

A digest is **worth printing** only when its pending content includes a held event type other
than `conductor_ping`, or a non-empty `ping_items`. When `Digest.due` would fire for a digest
that is *not* worth printing, `follow` discards it silently: it clears and saves the digest,
prints nothing, and records no `digest` delivery. The held pings keep their `hold` delivery
records, so `wt queue wakeups` still shows how many pings were held, and the discarded digest
adds no wakeup. The digest's clock restarts with the next held event.

## Design

- `src/wt/agentic/quiet.py`, class `Digest`:
  - New property `worth_printing -> bool`: `True` when `pending["ping_items"]` is non-empty
    or `pending["held"]` has any key other than `"conductor_ping"`. An empty digest (the
    `empty` property) is not worth printing.
  - `due(now, interval)` is unchanged: it still means "the oldest held event is past the
    interval", so the discard happens on the same schedule as a print would.
- `src/wt/agentic/events.py`, `follow`, in the `if quiet and ctx.digest.due(clock(),
  interval)` block:
  - If `ctx.digest.worth_printing`: today's path (build the line, `clear()`, `save()`,
    print, `record_delivery(..., "digest")`, `printed += 1`, honour `max_events`).
  - Otherwise: `ctx.digest.clear()` then `ctx.digest.save()` and nothing else: no output,
    no delivery record, `printed` unchanged. `clear()` keeps `last_ping_fp` and records
    `last_printed_last_id`, so a re-read of the discarded pings after a crash is ignored by
    `Digest.add` exactly as for a printed digest.
- A ping-only digest that is not due yet is untouched, so a later non-ping held event (or a
  held ping with items) lands in the same pending digest and the whole digest, including
  `pings_held`, is printed when due.
- `skills/wt-conduct/SKILL.md`: the *Quiet stream* paragraph (digest sentence, around line
  79) and the `conductor_digest` row (around line 113) say a digest is not delivered when it
  holds only pings with no `ping_items`. Keep the existing wording about `ping_items`.
- No config key, no schema change, no change to `deliveries.jsonl` or `wt.conductor.wakeups.v1`.

## Alternatives considered

- **Do not fold empty-ping holds into the digest at all** — it loses `pings_held` for digests
  that print for another reason and moves the digest clock: an empty ping at T0 followed by
  an `admitted` at T0+25 min would delay the digest to T0+55 min instead of T0+30 min.
- **Gate `Digest.due` on `worth_printing`, leaving the ping-only digest pending** — the
  digest would stay pending with an old `since`, so the next held event would print at once
  instead of waiting an interval, and the pending file would grow unbounded counts on an idle
  queue. Discarding at the due time keeps the schedule and the file bounded.
- **Suppress every digest that holds only informational events** — wider than the reported
  bug and changes SPEC-0300's decision that the conductor sees a periodic summary of
  admissions, merges and cost warnings. Out of scope here.
- **Record a `skipped` delivery action** — adds a new `deliveries.jsonl` action every report
  would have to understand, for a line that was never delivered. The `hold` records already
  show the pings.

## Acceptance criteria

- [x] A pending digest holding only `conductor_ping` events with `ping_items == []` is not
      printed when `Digest.due` fires: `follow` writes nothing for it and returns no
      `conductor_digest` line.
- [x] That discard clears and saves the digest: `pending` is fresh (`first_id is None`), the
      digest file is rewritten, and `last_printed_last_id` covers the held pings.
- [x] That discard records no `digest` delivery and adds no wakeup: `wt queue wakeups`
      `digests` is unchanged and `delivered` does not count it; the `hold` records of the
      pings remain.
- [x] A pending digest holding a ping-only history plus any other held event type (for
      example `admitted`) is printed when due, with `held` and `pings_held` as before.
- [x] A pending digest whose `ping_items` is non-empty is printed when due, as before.
- [x] After a ping-only digest is discarded, the next held event starts a new digest whose
      `since` is that event's timestamp, and it prints one interval later.
- [x] A crash-style re-read of already discarded pings (cursor behind the digest) does not
      re-add them (`Digest.add` returns `False`).
- [x] The `wt-conduct` skill's *Quiet stream* text and its `conductor_digest` row state that
      a digest holding only pings without `ping_items` is not delivered.
- [x] All other `tests/test_agentic_quiet.py` tests still pass unchanged.

## Test plan

- **Automated tests** in `tests/test_agentic_quiet.py` (reuse the existing `follow` fixtures
  and the injected clock used by `test_digest_emitted_after_interval`):
  - `test_digest_skipped_when_only_empty_pings`: append empty-fingerprint pings, advance the
    clock past `digest_interval_s`, run `follow`; assert no `conductor_digest` line, the
    digest file is fresh, no `action: "digest"` row in `deliveries.jsonl`, `hold` rows exist,
    and `wakeups_report(...)["digests"] == 0`.
  - `test_digest_printed_when_pings_and_other_event`: held empty pings plus an `admitted`
    event; assert the digest prints with both `held` entries and `pings_held`.
  - `test_digest_printed_when_ping_items_nonempty`: a held ping with a repeated actionable
    item (`ping_items` non-empty); assert the digest prints with `ping_items`.
  - `test_digest_restarts_after_skipped_digest`: after a discard, a new held event produces a
    digest only after another full interval, with `since` equal to that event's `ts`.
  - `test_skipped_digest_pings_not_readded`: after the discard, `Digest.add` of the same
    ping ids returns `False`.
  - `Digest.worth_printing` unit cases: empty, pings only, pings with items, ping plus
    another type, another type only.
  - `tests/test_skills.py`: keep the existing `conductor_digest` assertions green, and add one
    that the skill text mentions the ping-only digest is not delivered.
- **Manual verification:** with an idle queue and a short `agentic.conductor.quiet.
  digest_interval_s`, run `wt queue events --follow --for conductor` for two intervals and
  confirm no `conductor_digest` line appears while only empty pings arrive; then `wt queue
  wakeups --json` shows `digests: 0` and a non-zero `held`.
- **Regression guard:** `uv run pytest` (the whole suite, including the existing digest
  tests `test_digest_emitted_after_interval`, `test_digest_never_empty`,
  `test_digest_survives_rearm`, `test_ping_items_in_digest_and_fp_kept_after_clear`,
  `test_hold_crash_between_digest_and_cursor`, `test_digest_crash_before_print`), plus
  `python3 tools/spec_lint.py`.

### Test record (2026-10-31)

- `uv run pytest tests/test_agentic_quiet.py tests/test_skills.py`: 165 passed. New tests:
  `test_digest_worth_printing`, `test_digest_skipped_when_only_empty_pings`,
  `test_digest_printed_when_pings_and_other_event`,
  `test_digest_printed_when_ping_items_nonempty`, `test_digest_restarts_after_skipped_digest`,
  `test_skipped_digest_pings_not_readded`,
  `test_conduct_skill_says_ping_only_digest_is_not_delivered`; the existing digest tests pass
  unchanged.
- The manual two-interval `wt queue events --follow` run was not performed against a live queue;
  `test_digest_skipped_when_only_empty_pings` covers the same path (no `conductor_digest` line,
  `wakeups_report` `digests == 0`, `held == 2`).
- The full `uv run pytest` run was left to gating; `python3 tools/spec_lint.py` run after the
  ledger was regenerated.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

None.
