---
id: SPEC-0300
title: "Quiet conductor: the conductor follower holds informational events, prints a digest, and counts wakeups"
status: done
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
     not: the park that follows carries the decision, and the skill's park rows look the
     failure up themselves, see Design), `merged`, `handed_off`, and `backlog` whose
     `blocked_by` names only specs that exist.
   - **Hold `conductor_ping`** when its *actionable fingerprint* is empty, or equal to the
     last delivered ping's fingerprint. The fingerprint is the sorted set of
     `(category, campaign)` for `blocked`, `stuck`, `ready_not_admitted`,
     `waiting_not_admitted`, conductor-owned undecided `parked`, plus `("runner", pid)` when
     `runner` is non-null. A held ping with a non-empty fingerprint is never invisible: the
     digest carries `ping_items: [[category, campaign], …]` (the last held fingerprint), so
     an item that persists after the conductor's first action failed still reappears, and
     the `conductor_digest` row says to act on those items per the table.
   - **Hold `runner_draining`** while `draining_s ≤ max((w.timeout_s or gate_budget_s) for w
     in waiting_on, default=gate_budget_s) + 300` (the same fallback as `queue.py:4157`).
     That is the table's own "wait" window, so a drain past it wakes.
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
   action: "wake"|"hold"|"digest"}` to `<agentic_dir>/conductor/deliveries.jsonl`. The
   synthetic `code_changed` exit line is recorded too, as `action: "wake"`,
   `type: code_changed`, `event_id: null`. `wt queue wakeups` reads it. **A wakeup is a
   delivered line**: a `wake` event, a digest, or a `code_changed`.
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
    campaigns: {type: [cid, … ≤ 10]}, merged: [{campaign, spec_id, label}], pings_held: n,
    ping_items: [[category, campaign], …]}`.
  - `Digest.add` ignores an event whose id is ≤ the digest's `last_id` (idempotent re-add).
    `clear()` resets the pending fields only and records `last_printed_last_id`;
    `last_ping_fp` is kept, and it is updated whenever a ping is delivered as `wake`, so
    the next unchanged ping stays held after a digest.
  - `record_delivery(cfg, reader, ev, action)`: an append under a flock in
    `store.agentic_dir(cfg) / "conductor"`.
  - `wakeups_report(cfg, *, since=None, until=None, reader="conductor") -> dict`:
    `wt.conductor.wakeups.v1` = `{delivered, held, digests, by_type: {type: {wake, hold}},
    merges, wakeups_per_merge, by_day: [...]}`. `merges` counts `merged` + `handed_off`
    events in `events.jsonl` in the window. `wakeups_per_merge` is `null` when
    `merges == 0`.
- `src/wt/agentic/events.py`:
  - `EVENT_TYPES` gains `"conductor_digest": ("first_id", "last_id", "held", "merged")`,
    with a comment that it is synthetic and never appended.
  - `follow(..., quiet=None)`. `None` means resolved from settings for `reader`. Inside the
    loop, each event from `read_since` is classified when quiet is on. On `wake` it is
    printed as today. On `hold` it is added to the digest and the digest is saved
    *before* `cursor_set`; the delivery is recorded too. So a crash between the two writes
    re-reads the event, and `Digest.add` ignores it (id ≤ `last_id`) rather than counting it
    twice. In both cases `cursor_set` advances. After the batch, `Digest.due` decides
    whether the digest is printed. A due digest is cleared and saved (keeping
    `last_printed_last_id`) *before* the line is printed and recorded as `digest`, so a
    crash there drops at most that one digest line and never duplicates it. In quiet mode
    `printed` and `max_events` count delivered lines only (wake events and digests); held
    events do not count. When quiet is on, the `code_changed` exit line is recorded with
    `record_delivery(cfg, reader, ev, "wake")` (`type: code_changed`, `event_id: null`)
    before `follow` returns. When quiet is off, the current code path runs unchanged.
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
    needs no action unless a park follows. If `ping_items` is non-empty, act on those
    items per the table. Do nothing else.
  - The `parked loop_exhausted / triage_escalated / implementer_blocked` row and the
    transient `run_failed` row are rewritten so they no longer rely on having seen a
    `run_failed` line: before deciding, read the campaign's recent failures with
    `wt queue events --campaign <id> --type run_failed` (the `transient` flag, and the
    reset time from `error`). Transient failures still resume with `--reset-loops`.
  - The `merged` / `handed_off` row moves its reconcile and report to the
    `conductor_digest` row. The runner re-exec check (`wt queue runner-log --tail 3`,
    restart outside the close guard) is written out in full in the `code_changed` row,
    which no longer points at the `merged` row. The `conductor_digest` row also says: if
    any merged campaign's project config (`.wt/project.yaml`) changed, run the same check.

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

- [x] `quiet.classify` returns `hold` for every always-hold type and `wake` for every
      always-wake type listed in the Decision and for an unknown type. The `RULES` table and
      the skill's list match (a test parses both).
- [x] The conditional holds behave as specified. A ping with an empty or unchanged
      actionable fingerprint is held, and a changed one wakes. Held pings with a non-empty
      fingerprint put `ping_items` in the digest, and `clear()` keeps `last_ping_fp`.
      `runner_draining` is held within `max((timeout_s or gate_budget_s)…, default
      gate_budget_s) + 300` (including an empty `waiting_on`), then wakes.
      `campaign_silent` / `state_stuck` are held
      while the campaign is in the held lease's `in_flight` or `busy` within budget, and wake
      otherwise.
- [x] Held events advance the cursor and come out in one `conductor_digest` line once the
      oldest is older than `digest_interval_s`. The pending digest survives a follower exit
      and re-arm, with no event lost or delivered twice, and an empty digest is never
      printed. On `hold` the digest is saved before the cursor advances, so a crash between
      the two writes does not double-count the event; a due digest is cleared and saved
      before it is printed. In quiet mode `max_events` counts delivered lines only.
- [x] Quiet mode applies only to configured readers. `--all` and `enabled: false` restore
      today's output byte for byte, and `events.jsonl` is identical with and without quiet
      mode.
- [x] Every classified event, digest and `code_changed` exit line is recorded in
      `deliveries.jsonl`. `wt queue wakeups --json` emits `wt.conductor.wakeups.v1` with
      `delivered`, `held`, `merges` and `wakeups_per_merge`, which is `null` when there
      are no merges.
- [x] The `/wt-conduct` skill documents the quiet stream and the `conductor_digest` row
      (including `ping_items`), and moves the merged/handed-off reconcile and report to the
      digest. The `code_changed` row carries the runner re-exec steps itself, and the digest
      row repeats the check when a merged campaign changed `.wt/project.yaml`.
- [x] The skill's park rows (`loop_exhausted` / `triage_escalated` / `implementer_blocked`)
      and the transient `run_failed` row no longer rely on having seen a `run_failed` line:
      they tell the conductor to look it up with `wt queue events --campaign <id> --type
      run_failed`.

## Test plan

- **Automated tests** in `tests/test_agentic_quiet.py`, with a temp agentic store, events
  appended through `events.append` and `events.follow(..., out=StringIO, max_events=…,
  stop=…)`:
  - `test_classify_always_hold_and_wake_tables`: parametrized over every listed type, plus
    `"brand_new_type"` → wake.
  - `test_ping_fingerprint_hold_and_change`: an empty ping is held, a ping with one stuck
    item wakes, the same ping again is held, and a different stuck campaign wakes.
  - `test_runner_draining_hold_window` (two `draining_s` values around the bound, plus an
    empty `waiting_on` and a null `timeout_s`, both using the `gate_budget_s` fallback).
  - `test_ping_items_in_digest_and_fp_kept_after_clear`: held non-empty pings put
    `ping_items` in the digest, and after a digest the same ping is still held.
  - `test_hold_crash_between_digest_and_cursor`: `cursor_set` raises once after a hold,
    the follower re-arms, and the event appears exactly once in the next digest.
  - `test_digest_crash_before_print`: a crash after the digest is cleared and saved but
    before the line is printed never prints the digest twice.
  - `test_max_events_counts_delivered_only`: held events do not count toward `max_events`.
  - `test_silent_held_while_in_flight`: the lease fixture has `in_flight: [cid]` → held.
    Not in flight → wake. `busy` past `budget_s` → wake.
  - `test_digest_emitted_after_interval` (a fake clock via `now` injection).
    `test_digest_never_empty`.
  - `test_digest_survives_rearm`: hold 3 events, exit the follower, re-arm, advance the
    clock. One digest lists all 3, and no event id is printed twice.
  - `test_quiet_off_is_byte_identical`: the same event sequence with `quiet=False` and with
    a non-conductor reader produces exactly the pre-change output.
    `test_events_jsonl_unchanged`.
  - `test_deliveries_and_wakeups_report`: 2 merges, 6 wake events, 1 digest, 1
    `code_changed` (via a fake watcher) and 10 holds give `delivered == 8`, `held == 10`
    and `wakeups_per_merge == 4.0`.
  - `test_cli_events_all_flag` and `test_cli_wakeups_json` (CliRunner), the latter also
    with a window of zero merges: `wakeups_per_merge` is `null`.
- `tests/test_skills.py`: `test_conduct_skill_quiet_rules_match_code`, which parses the
  skill's hold list and compares it with `quiet.RULES`,
  `test_conduct_skill_has_conductor_digest_row`, and
  `test_conduct_skill_park_rows_look_up_run_failed` (the park rows and transient
  `run_failed` row mention `--type run_failed`; the `code_changed` row contains
  `runner-log`; the digest row mentions `.wt/project.yaml` and `ping_items`).
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

### Test record (2026-10-30)

- `uv run pytest tests/test_agentic_quiet.py tests/test_skills.py tests/test_agentic_events.py
  tests/test_agentic_followers.py tests/test_agentic_conduct_cli.py`: 143 passed. The three
  named regression files are unchanged.
- Sabotage checks run and each failed the named test, then reverted: unknown type held
  (`test_classify_always_hold_and_wake_tables_wake[brand_new_type]`); digest not saved on hold
  (`test_digest_survives_rearm` among five failures); `in_flight` check dropped
  (`test_silent_held_while_in_flight`).
- Full `uv run pytest`: 3963 passed, 3 skipped (one earlier `-x` run hit
  `test_agentic_adopt.py::test_fanout_detach_precondition`, which passes alone and in the full
  run: a pre-existing timing flake, unrelated).
- Manual verification (re-arm the live conductor, run `wt queue wakeups --since <day>` after a
  day) can only happen once this is merged and the conductor has re-armed. It is not done
  here; the conductor records the first wakeups-per-merge figure with a campaign note.

### As shipped (2026-10-30)

- `events.follow(quiet=...)` defaults to `False`, not "resolved from settings". Existing tests
  (`test_agentic_events.py`, which must stay unchanged) call `follow(cfg, "conductor")` directly
  and expect every event; resolving there would hold them. `wt queue events --follow` resolves
  it with `quiet.enabled_for(cfg, reader) and not --all`, so the live conductor is quiet.
  `follow` also takes `now=` (a clock callable) for tests.
- `backlog` is held by reading `blocked_by` text (`ready`, or only `waiting on …` / `spec
  status is …` entries), not by a set of known spec ids in `Ctx`. Any other text (`… not
  found`, `branch … missing`) wakes.
- A held `conductor_ping` with an empty fingerprint also resets `last_ping_fp`, so items that
  recur after an all-clear wake again.
- `wt queue events` gained `--campaign <id>` (history filter) because the skill's park rows
  tell the conductor to run `wt queue events --campaign <id> --type run_failed`.
- `events.followers` skips `*.digest.json` files in the cursors dir.
- Two existing `tests/test_skills.py` assertions about the re-exec check moved from the
  `merged` row to the `code_changed` row, as the Design says.
- A digest line's `--text` form is `digest #a..#b held=type:n … merged=labels`
  (plus `ping_items=n` when set).



## Rollout / migration

1. Merge. The conductor's follower exits with `code_changed`, the conductor re-reads the
   skill and re-arms, and quiet mode is on from then.
2. If a held type turns out to need decisions, move it to wake in `quiet.RULES` and the
   skill in a follow-up spec. That is a tuning decision for the conductor, not an
   escalation.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

- None.
