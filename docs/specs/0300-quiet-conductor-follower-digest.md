---
id: SPEC-0300
title: "Quiet conductor: the conductor follower holds informational events, prints a digest, and counts wakeups"
status: accepted
owner: user
created: 2026-10-30
updated: 2026-10-30
milestone: "M10: agentic MVP"
kind: feature
parent: SPEC-0298
tags: [agentic, conductor, events, cost]
---

## Context

The conductor ([SPEC-0182](./0182-event-driven-wt-conduct-monitor-armed-conductor.md)) is
woken by every line its Monitor reads from `wt queue events --follow --for conductor`.
`events.follow` prints every event after the reader's cursor, and every line is one wakeup:
one API turn that re-reads the whole conductor context. In the 5-day
[SPEC-0298](./0298-agentic-mvp-cheap-durable-quiet-conductor.md) baseline that came to
2,386 turns for 93 merges. Most of those turns acknowledged events that the `/wt-conduct`
decision table itself calls informational, or that were false alarms:

- `admitted`, `resumed`, `waiting`, `abandoned`, `cost_warning`, `escalation`,
  `escalation_withdrawn`, `rebase_resolved` and non-transient `run_failed`. The table says
  "informational; act only if it contradicts what you decided" or "none".
- `conductor_ping` every 10 min (SPEC-0212), even when nothing in it needs action, or the
  same items are listed again.
- `runner_draining` while it waits on a gate or close within budget. The table says
  "wait".
- `campaign_silent` / `state_stuck` while the campaign's gate or close is still running
  in the runner (the lease's `in_flight` / `busy`). These are false alarms.
- `merged` / `handed_off`, which ask only for `wt spec reconcile --apply` and a one-line
  report. After a merge of wt code they echo the `code_changed` line that already woke the
  conductor.

Nothing records what the follower delivered, so "wakeups per merge" (exit criterion E3,
≤ 5) cannot be measured today.

## Goals / Non-goals

**Goals**
- The `conductor` follower delivers decision-needing events at once and folds a declared
  set of informational events into a periodic, compact `conductor_digest` line.
- Conditional holds (ping, drain, silent/stuck) are decided from facts at delivery time,
  and an event becomes a wakeup again as soon as its fact says it needs action.
- Every delivered and held line is counted, and `wt queue wakeups` reports wakeups per
  merge.
- Quieting is configurable and can be turned off per follower.

**Non-goals**
- Changing what `events.sync` appends. `events.jsonl`, `wt queue events` history, the TUI
  events tab and steward followers are untouched.
- Changing watchdog thresholds or the ping's content (SPEC-0212 and SPEC-0270 stand).
- Deciding parks automatically (a separate SPEC-0298 child).

## Decision

1. **Classification at delivery.** A new `quiet.classify(cfg, ev, ctx) -> "wake" | "hold"`
   runs in `events.follow` for each event, but only when the reader's quiet mode is on. By
   default that is the `conductor` reader only. Unknown or new event types are **wake**:
   the filter fails loud, never silent.
2. **The declared rules** (`quiet.RULES`, mirrored in the skill table):
   - **Always hold:** `admitted`, `resumed`, `waiting`, `abandoned`, `cost_warning`,
     `escalation`, `escalation_withdrawn`, `rebase_resolved`, `run_failed` (transient or
     not, because the park that follows carries the decision), `merged`, `handed_off`, and
     `backlog` whose `blocked_by` names only specs that exist.
   - **Hold `conductor_ping`** when its *actionable fingerprint* is empty, or equal to the
     last delivered ping's fingerprint. The fingerprint is the sorted set of
     `(category, campaign)` for `blocked`, `stuck`, `ready_not_admitted`,
     `waiting_not_admitted`, conductor-owned undecided `parked`, plus `("runner", pid)` when
     `runner` is non-null.
   - **Hold `runner_draining`** while `draining_s ≤ max(waiting_on[].timeout_s) + 300`. That
     is the table's own "wait" window, so a drain past it wakes.
   - **Hold `campaign_silent` / `state_stuck`** while the runner lease is held and the
     campaign is in `lease.in_flight`, or is `lease.busy.campaign` within `busy.budget_s`.
     A gate or close process for it is alive.
   - **Always wake:** everything else. That includes every `parked`, `park_undecided`,
     `runner_dead`, `lease_stale`, `run_overdue`, `message`, `escalation_answered`,
     `ready_not_admitted`, `waiting_not_admitted`, `runner_journal_silent`,
     `temp_pressure`, `runner_config_invalid`, `backlog_blocked_long`, `backlog` with an
     unknown `blocked_by`, and the synthetic `code_changed`.
3. **Digest.** Held events advance the cursor and go into a pending digest that is persisted
   at `cursors/<reader>.digest.json`, so it survives the follower's exits (`code_changed`,
   Monitor expiry). When the pending digest is non-empty and its oldest held event is older
   than `digest_interval_s` (default 1800), the follower prints one synthetic
   `conductor_digest` line, like `code_changed`: `id: null` and not appended to
   `events.jsonl`. An empty digest is never printed.
4. **Counting.** Each classified event appends `{ts, reader, event_id, type, campaign,
   action: "wake"|"hold"|"digest"}` to `<agentic_dir>/conductor/deliveries.jsonl`.
   `wt queue wakeups` reads it. **A wakeup is a delivered line**: a `wake` event, a digest,
   or a `code_changed`.
5. **Config** lives at `agentic.conductor.quiet`: `{enabled: true, readers: ["conductor"],
   digest_interval_s: 1800}`. `wt queue events --follow --all` turns quiet mode off for
   that follower.

## Design

- New module `src/wt/agentic/quiet.py`:
  - `RULES: dict[str, str]` maps a type to `"hold"` | `"wake"` | `"cond"`, and `DEFAULTS`.
    `settings(cfg)` validates like `events.settings`: bad values fall back to defaults.
  - `Ctx`: one per poll, built lazily. It holds `lease = L.lease_status(cfg)`, the set of
    known spec ids for `backlog`, and `last_ping_fp`, read from the digest file.
  - `classify(cfg, ev, ctx) -> str`, `ping_fingerprint(data) -> list`.
  - `Digest`: `load(cfg, reader)`, `add(ev)`, `due(now, interval)`, `line(as_json)`
    (through `events.summary_line` for `--text`), `clear()`, all saved atomically with
    `export._atomic_write`. Payload: `{first_id, last_id, since, until, held: {type: n},
    campaigns: {type: [cid, … ≤ 10]}, merged: [{campaign, spec_id, label}], pings_held: n}`.
  - `record_delivery(cfg, reader, ev, action)`: an append under a flock in
    `store.agentic_dir(cfg) / "conductor"`.
  - `wakeups_report(cfg, *, since=None, until=None, reader="conductor") -> dict`:
    `wt.conductor.wakeups.v1` = `{delivered, held, digests, by_type: {type: {wake, hold}},
    merges, wakeups_per_merge, by_day: [...]}`. `merges` counts `merged` + `handed_off`
    events in `events.jsonl` in the window.
- `src/wt/agentic/events.py`:
  - `EVENT_TYPES` gains `"conductor_digest": ("first_id", "last_id", "held", "merged")`,
    with a comment that it is synthetic and never appended.
  - `follow(..., quiet=None)`. `None` means resolved from settings for `reader`. Inside the
    loop, each event from `read_since` is classified when quiet is on. On `wake` it is
    printed as today. On `hold` it is added to the digest and recorded. In both cases
    `cursor_set` advances. After the batch, `Digest.due` decides whether the digest is
    printed, recorded as `digest` and cleared. When quiet is off, the current code path runs
    unchanged.
  - `summary_line` gets a `conductor_digest` branch: `digest #first..#last held=<type>:n …
    merged=<labels>`.
- `src/wt/agentic/cli_conduct.py`: `queue_events` gets `--all` (passes `quiet=False`).
  `wt queue wakeups [--since YYYY-MM-DD] [--until …] [--reader conductor] [--json]` is
  registered in the same `attach`.
- `skills/wt-conduct/SKILL.md`:
  - A short *Quiet stream* paragraph under step 4 lists the hold rules.
  - The decision-table rows for the always-hold types say "(arrives only in
    `conductor_digest`)".
  - A new `conductor_digest` row: if `merged` is non-empty, run `wt spec reconcile --apply`
    and give one line per merge. Scan `held` counts. A `cost_warning` or `run_failed` count
    needs no action unless a park follows. Do nothing else.
  - The `merged` / `handed_off` row moves its action to the digest row. The runner re-exec
    check stays on `code_changed`.

## Alternatives considered

- **Stop appending informational events.** That loses history for the TUI, stewards and
  audits. The problem is waking, not recording.
- **Hold everything except an allow-list.** A new decision-needing type would be silent
  until someone added it. Wake-by-default for unknown types is safer.
- **Coalesce consecutive lines instead of a digest.** Monitor may already batch lines that
  arrive together. Coalescing does not remove the per-event turns spread over time, which
  is where the cost was.
- **A shorter ping interval with richer content.** That makes more wakeups, not fewer.

## Acceptance criteria

- [ ] `quiet.classify` returns `hold` for every always-hold type and `wake` for every
      always-wake type listed in the Decision and for an unknown type. The `RULES` table and
      the skill's list match (a test parses both).
- [ ] The conditional holds behave as specified. A ping with an empty or unchanged
      actionable fingerprint is held, and a changed one wakes. `runner_draining` is held
      within `max(timeout_s) + 300`, then wakes. `campaign_silent` / `state_stuck` are held
      while the campaign is in the held lease's `in_flight` or `busy` within budget, and wake
      otherwise.
- [ ] Held events advance the cursor and come out in one `conductor_digest` line once the
      oldest is older than `digest_interval_s`. The pending digest survives a follower exit
      and re-arm, with no event lost or delivered twice, and an empty digest is never
      printed.
- [ ] Quiet mode applies only to configured readers. `--all` and `enabled: false` restore
      today's output byte for byte, and `events.jsonl` is identical with and without quiet
      mode.
- [ ] Every classified event and digest is recorded in `deliveries.jsonl`.
      `wt queue wakeups --json` emits `wt.conductor.wakeups.v1` with `delivered`, `held`,
      `merges` and `wakeups_per_merge`.
- [ ] The `/wt-conduct` skill documents the quiet stream and the `conductor_digest` row, and
      moves the merged/handed-off action to the digest.

## Test plan

- **Automated tests** in `tests/test_agentic_quiet.py`, with a temp agentic store, events
  appended through `events.append` and `events.follow(..., out=StringIO, max_events=…,
  stop=…)`:
  - `test_classify_always_hold_and_wake_tables`: parametrized over every listed type, plus
    `"brand_new_type"` → wake.
  - `test_ping_fingerprint_hold_and_change`: an empty ping is held, a ping with one stuck
    item wakes, the same ping again is held, and a different stuck campaign wakes.
  - `test_runner_draining_hold_window` (two `draining_s` values around the bound).
  - `test_silent_held_while_in_flight`: the lease fixture has `in_flight: [cid]` → held.
    Not in flight → wake. `busy` past `budget_s` → wake.
  - `test_digest_emitted_after_interval` (a fake clock via `now` injection).
    `test_digest_never_empty`.
  - `test_digest_survives_rearm`: hold 3 events, exit the follower, re-arm, advance the
    clock. One digest lists all 3, and no event id is printed twice.
  - `test_quiet_off_is_byte_identical`: the same event sequence with `quiet=False` and with
    a non-conductor reader produces exactly the pre-change output.
    `test_events_jsonl_unchanged`.
  - `test_deliveries_and_wakeups_report`: 2 merges, 6 wake events, 1 digest and 10 holds
    give `delivered == 7`, `held == 10` and `wakeups_per_merge == 3.5`.
  - `test_cli_events_all_flag` and `test_cli_wakeups_json` (CliRunner).
- `tests/test_skills.py`: `test_conduct_skill_quiet_rules_match_code`, which parses the
  skill's hold list and compares it with `quiet.RULES`, and
  `test_conduct_skill_has_conductor_digest_row`.
- **Sabotage checks:**
  - Make `classify` return `hold` for unknown types.
    `test_classify_always_hold_and_wake_tables` must fail.
  - Do not persist the digest on exit. `test_digest_survives_rearm` must fail.
  - Drop the `in_flight` check. `test_silent_held_while_in_flight` must fail.
- **Manual verification:** re-arm the live conductor on the new code, and after one day run
  `wt queue wakeups --since <day>`. Wakeups per merge is reported. Record it in
  *As shipped*.
- **Regression guard:** full `uv run pytest`. `tests/test_agentic_events.py`,
  `tests/test_agentic_followers.py` and `tests/test_agentic_conduct_cli.py` are unchanged.

## Rollout / migration

1. Merge. The conductor's follower exits with `code_changed`, the conductor re-reads the
   skill and re-arms, and quiet mode is on from then.
2. If a held type turns out to need decisions, move it to wake in `quiet.RULES` and the
   skill in a follow-up spec. That is a tuning decision for the conductor, not an
   escalation.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

- None.
