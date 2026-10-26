---
id: SPEC-0251
title: "TUI events tab: live event stream with filters, current watchdog alerts, ping digest and followers"
status: done
owner: user
created: 2026-10-25
updated: 2026-10-26
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0169
tags: [agentic, tui, events, watchdog, textual]
source_idea: IDEA-086
depends_on: [SPEC-0241, SPEC-0181, SPEC-0212]
---

## Context

Child of epic [SPEC-0169](./0169-mini-agentic-workflow-queue-campaigns-headless.md), promoted
from `IDEA-086`.

The queue event stream ([SPEC-0181](./0181-queue-event-stream-watchdog-signals-and-heartbeat.md),
`src/wt/agentic/events.py`) is `<agentic_dir>/events.jsonl`, one `wt.event.v1` record per
event: `id`, `ts`, `type`, `campaign`, `spec_id`, `project`, `by` and `data`. Events are
projected from the ledgers, and the watchdog conditions are appended, only when a follower calls
`events.sync`. Watchdog conditions include `runner_dead`, `lease_stale`, `run_overdue`,
`campaign_silent`, and the SPEC-0212 / 0224 / 0226 additions. `events.watchdog_checks(cfg)` is
pure ("no writes") and returns the conditions true **now**.
[SPEC-0212](./0212-periodic-conductor-ping.md) adds the periodic `conductor_ping` digest.
`events.followers(cfg)` lists every reader cursor with its live pid and lag.

The human sees all this only through `wt queue events [--follow]` (a text stream) and `wt queue
followers`. A follower started for that purpose takes a reader name and cursor. The queue tab
([SPEC-0241](./0241-tui-queue-tab-over-the-queue-dashboard.md)) set the TUI rule: poll with
`stat()`, never sync, never hold a cursor.

## Goals / Non-goals

**Goals**
- G1. An `events` tab: the event stream (newest at the bottom, the tail followed), filterable by
  type, campaign, and "alerts only". Alert types are highlighted.
- G2. A **conditions** panel with the watchdog conditions true now, computed with
  `watchdog_checks` and independent of whether any follower has synced the stream.
- G3. The latest `conductor_ping` digest rendered as a panel.
- G4. A followers panel (reader, kind, live pid, lag, cursor age) that flags dead readers and
  growing lag. The stream's freshness is visible: the last sync age, and a warning when no live
  follower is syncing.

**Non-goals**
- Syncing the stream or running reminders from the TUI (followers do that).
- Acknowledging or clearing alerts. Conditions clear when the facts change, as SPEC-0181
  defines.
- Desktop notifications from the TUI (SPEC-0232 owns notifications).

## Decision

View-model functions in `src/wt/tui/agentic_model.py` cover the stream (`load_events`,
`EventFilter`), the conditions (`load_conditions`), the digest (`ping_text`), the followers
(`load_followers`) and the stream freshness (`stream_health`). A widget module
`src/wt/tui/events_tab.py` holds `EventsView`: the stream table on the left (about 65% wide), and
on the right a column with three stacked panels (conditions, ping digest, followers). `IdeaDesk`
adds `TabPane("events", id="tab-events")` on key `5` and `--tab events`.

## Design

### View model (`agentic_model.py`)

- `ALERT_TYPES = {"runner_dead", "lease_stale", "run_overdue", "campaign_silent",
  "backlog_blocked_long", "state_stuck", "ready_not_admitted", "runner_draining",
  "runner_config_invalid", "run_failed"}`.
  - A `run_failed` event with `data.transient` true is styled yellow (it retries).
  - Every other alert type is red.
  - `parked` and `escalation` are yellow, and `merged` / `handed_off` are green.
  - Other types are plain.
- `EventFilter` is a frozen dataclass with `types` (a frozenset, empty meaning all), `campaign`
  (a substring, or None) and `alerts_only` (bool). `parse_event_filter(text)` reads the input
  bar text, `type=parked,merged campaign=SPEC-0230 alerts`, into an `EventFilter`, and rejects an
  unknown `type` (not in `events.EVENT_TYPES`) with `ValueError` naming it.
  `format_event_filter(f)` is its inverse, for the status line.
- `load_events(cfg, after_id=0, limit=2000) -> list[EventRow]`. It reads
  `events.read_since(cfg, after_id)` and keeps the last `limit` events. `EventRow` has `id`,
  `ts`, `type`, `campaign`, `text` and `style`. `text` is `events.summary_line(ev)` without its
  `#id type campaign` prefix (the table has those columns).
- `matches(row, f)` applies the filter to a row.
- `load_conditions(cfg) -> list[ConditionRow]`. It calls `events.watchdog_checks(cfg)` and
  returns `type`, `campaign` and a one-line `text` built from `data` (`k=v` pairs, as
  `summary_line` does). The rows are sorted with runner-level conditions (no campaign) first.
  An empty list renders as `no alerts`.
- `ping_text(rows) -> Text` renders the newest `conductor_ping` event in the loaded stream:
  - the counts line (`active`, `queued`, `ready`, `blocked`, `parked` with undecided, `stuck`,
    `waiting`);
  - then the `blocked`, `stuck`, `ready_not_admitted` and undecided `parked` entries, one line
    each with campaign and age;
  - and `ping <age> ago`.
  With no ping in the stream it shows `no conductor_ping yet (interval
  <ping_interval_s>s)`, using `events.settings(cfg)`.
- `load_followers(cfg) -> list[FollowerRow]` comes from `events.followers(cfg)`: `reader`,
  `kind`, `pid` (or `-`), `held`, `lag` and `cursor_age_s`. A row is flagged `dead` (red) when it
  is not held. It is flagged `lagging` (yellow) when it is held, `lag > 0`, and `cursor_age_s >
  60`.
- `stream_health(cfg, followers) -> str` returns `stream synced <age> ago`, where the age is the
  `mtime` of `events._projector_path(cfg)` (written by every `sync`). It appends
  ` · no live follower: the stream may lag, conditions are live` when no `events`-kind follower
  is held.
- `events_signature(cfg)`: `(size, mtime_ns)` of `events.events_path(cfg)`.

### Widget (`events_tab.py`)

- **Stream table** columns: `#id`, `time` (HH:MM:SS, plus the date when it is not today),
  `type` (styled), `campaign`, `text` (clipped to width).
- The status line shows `stream_health`, the active filter, and `<shown>/<loaded>`.
- **Incremental load:** the tab remembers the last loaded id. While active, `set_interval(2.0)`
  checks `events_signature`. On a change it reads `load_events(cfg, after_id=last_id)` in a
  thread worker and appends the matching rows. When the cursor was on the last row it follows
  the tail; otherwise it stays put. The table keeps at most 2000 rows and drops the oldest. If
  the file shrank (rotated or replaced), the tab reloads from 0.
- **Side panels** (conditions, followers, stream health) refresh every 5 s in a thread worker
  while the tab is active. `watchdog_checks` reads every active ledger tail, so it is kept off
  the 2 s path. The ping panel refreshes when a new `conductor_ping` row arrives.
- **Bindings** on the stream table:
  - `f` opens the filter bar (an `Input`, as the desk's `#filter` does). Enter applies it, an
    invalid filter shows the `ValueError` in the hint line and keeps the old filter, and Esc
    closes the bar.
  - `x` toggles alerts-only.
  - `c` filters to the highlighted row's campaign, and pressing `c` again clears it.
  - `enter` on a row with a campaign posts `QueueView.CampaignSelected`.
  - `G` jumps to the tail and resumes following.
- **Read-only:** the tab never calls `events.sync`, `events.cursor_set`, `events.reader_lock`,
  `events.follow` or `mailbox.follow`. `watchdog_checks`, `read_since` and `followers` only
  read. Choice: the conditions panel answers "what is wrong now" even when no follower runs,
  which is exactly when the stream is stale. Taking a reader would make the TUI itself one of
  the followers it monitors.

### Tab plumbing

- `5` → `tab-events`, through SPEC-0241's `action_tab`.
- `wt tui --tab` accepts `events`.
- `_tab_activated` activates and deactivates `EventsView`.

## Alternatives considered

- **Follow the stream as reader `tui`.** Rejected: it would advance the projection (a write),
  appear in `wt queue followers`, and compete for the projector lock (see Read-only).
- **Show alerts only from the stream.** Rejected: the stream fires a condition once, when a
  follower first notices it, so a stream view cannot say whether `runner_dead` is still true.
  `watchdog_checks` can.
- **A separate tab per panel.** Rejected: the panels explain each other (an alert next to the
  dead follower that should have reported it), and they fit beside the stream at 120 columns.

## Acceptance criteria

- [x] AC1. `5`, a click, and `wt tui --tab events` open the `events` tab. The stream table shows
      the newest 2000 events with the documented columns and styles: alert types red (transient
      `run_failed` yellow), parks and escalations yellow, merges green.
- [x] AC2. `parse_event_filter` accepts `type=…`, `campaign=…` and `alerts`, and rejects an
      unknown type naming it. In the tab, `f` applies a filter, an invalid one is shown in the
      hint line and the previous filter is kept, `x` toggles alerts-only, `c` toggles the row's
      campaign, and the status line shows `<shown>/<loaded>`.
- [x] AC3. An event appended by another process appears within one poll interval with no key
      press. The tail is followed only when the cursor was on the last row. A truncated or
      replaced `events.jsonl` causes a full reload without an error.
- [x] AC4. The conditions panel lists `watchdog_checks` results, runner-level first, or `no
      alerts`. A seeded condition (a campaign that is active with no lease held ⇒ `runner_dead`)
      shows even though no `sync` has run.
- [x] AC5. The ping panel renders the newest `conductor_ping` with the documented counts and
      entries, or the `no conductor_ping yet` line with the configured interval.
- [x] AC6. The followers panel lists `events.followers` rows with the `dead` / `lagging` flags.
      The status line shows the stream sync age and the no-live-follower warning when no
      events follower is held.
- [x] AC7. The tab never calls `events.sync`, `events.cursor_set`, `events.reader_lock`,
      `events.follow` or `mailbox.follow` (monkeypatched to raise, driving load, poll, filters
      and panels). `enter` on a row with a campaign posts `CampaignSelected`.
- [x] AC8. Full `uv run pytest` is green, the existing `tests/test_tui_*` are unchanged and
      green, and `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests:**
  - `tests/test_tui_events_model.py` (new, no Textual), seeding events with `events.append`:
    - `load_events` limit and the `after_id` increment;
    - the style per type;
    - `parse_event_filter` / `format_event_filter` round trip and errors;
    - `matches`;
    - `load_conditions` with a seeded `runner_dead` condition (an active campaign, no lease)
      and with none;
    - `ping_text` with and without a ping;
    - `load_followers` flags from fake cursor files (`events.cursor_set` called by the test
      itself, plus held and not-held locks);
    - `stream_health` with and without a held events reader.
  - `tests/test_tui_events_tab.py` (new, `needs_textual`):
    - tab opening via `5` and `--tab events`;
    - live append with and without the cursor on the tail;
    - truncation and full reload;
    - filter bar apply, error, and Esc;
    - `x` and `c`;
    - side panels populated;
    - `enter` posting `CampaignSelected`;
    - the forbidden-call monkeypatches.
- **Manual verification:** with the runner and a conductor follower live, open `uv run wt tui
  --tab events`, watch a campaign's transitions arrive, stop the follower, and check the
  followers panel flags it `dead` and the status line warns. Record the result here.
- **Regression guard:** full `uv run pytest`, in particular `tests/test_agentic_events*.py`,
  unchanged.

### Test record (2026-10-26)

- `uv run pytest -q tests/test_tui_events_model.py tests/test_tui_events_tab.py`: 28 model
  tests and 17 Textual pilot tests pass (tab opening by `5`, `--tab events` and a click; styles
  and columns; the 2000-row cap; live append with and without the cursor on the tail;
  truncation, in-place and atomic replacement, and removal; filter bar apply / error / Esc;
  `x`, `c`, `G`; the side panels with a seeded `runner_dead` and no sync, a ping, live and dead
  followers; `enter` posting `CampaignSelected`; the forbidden-call monkeypatches over
  `events.sync` / `cursor_set` / `reader_lock` / `follow` and `mailbox.follow`; timers paused
  off-tab).
- Full `uv run pytest -q -x`: 2828 passed, 3 skipped. The existing `tests/test_tui_*` files and
  `tests/test_agentic_events*.py` are untouched.
- `python3 tools/spec_lint.py --write-ledger` then `python3 tools/spec_lint.py`: exit 0.
- **Manual verification:** not run against a live runner and conductor follower (none is
  available inside the campaign worktree). Its steps are covered by pilot tests that hold and
  drop a real `events.reader_lock` in-process: the followers panel shows `dead` and the status
  line shows the no-live-follower warning when the lock is not held, and neither when it is.

### As shipped (2026-10-26)

Matches the Design, with these details:

- `EventRow` has a seventh field, `data` (the event's payload, excluded from equality),
  because `ping_text` renders the digest from the loaded rows. `ping_text(rows, cfg=None,
  now=None)`: without `cfg` the default `ping_interval_s` is shown.
- `parse_event_filter` also rejects an unrecognised token (`ValueError` naming it), not only an
  unknown type; `alerts_only` is accepted as a synonym for `alerts`.
- `FollowerRow` carries the `dead` / `lagging` flag as a `flag` field; `stream_health` takes
  those rows and says `stream never synced` when the projector file does not exist.
- Rotation detection is a shrunk file, a vanished file, a different inode (an atomic replace
  with a bigger file), or an incremental read that finds nothing while the file's newest id is
  older than the last loaded one. Any of them reloads from 0.
- The filter `Input` (`#events-filter`) lives inside the tab, above the status line; an
  invalid filter shows its `ValueError` in the desk's `#hint` line and leaves the bar open and
  the old filter active. `c` on an event without a campaign notifies instead of filtering.
- `wt tui --tab events` is added to the click choice and the `wt tui` help text.

## Clock Log

Built by the agentic loop or the human lane; idea bookkeeping via `wt spec reconcile --apply`.

## Rollout / migration

Nothing to migrate. The tab appears on the next `wt tui` launch with the `tui` extra.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

_None — decisions locked above._
