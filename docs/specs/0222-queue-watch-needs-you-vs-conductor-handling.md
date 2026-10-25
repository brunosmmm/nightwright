---
id: SPEC-0222
title: "queue watch: split parks into 'Needs you' and 'Conductor handling'"
status: accepted
owner: user
created: 2026-10-25
updated: 2026-10-25
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0169
tags: [agentic, queue, dashboard, conductor, watchdog]
source_idea: IDEA-096
depends_on: [SPEC-0181, SPEC-0185, SPEC-0212]
---

## Context

Child of epic [SPEC-0169](./0169-mini-agentic-workflow-queue-campaigns-headless.md),
promoted from bug `IDEA-096`.

`wt queue watch` ([SPEC-0185](./0185-rich-human-output-everywhere-and-a-queue-watch.md)) puts every PARKED campaign
(and every pending park request) in one yellow **Needs attention** section. The header has a
yellow `attention` chip, and `wt queue list` colours every PARKED row yellow. Most parks,
though, belong to the conductor ([SPEC-0182](./0182-event-driven-wt-conduct-monitor-armed-conductor.md)),
whose decision table already says what to do: `rebase_conflict`, `base_moving`, transient run
failures, the first `cost_cap`, `loop_exhausted`, `triage_escalated` and so on. On 2026-10-25
the human asked "why am I getting needs attention for rebases, that is your job". Only a few
parks actually need the human:
- `main_dirty` / `main_not_on_base`: their own checkout;
- explicit escalations (`IDEA-087`, park reason `human_decision`, not built yet).

The data for the split already exists. `events.ping_digest` (SPEC-0212) computes `decided` for
each parked item: a ledger `note` after the latest `park`, which is what the conductor
records with `wt campaign note`. Nothing alerts the conductor, though, when it has left a park
undecided. The periodic ping lists it, at most every 10 minutes, mixed with everything else.

## Goals / Non-goals

**Goals**
- G1. Every park has an **owner**, `conductor` or `human`, derived from its reason by one
  table in code.
- G2. `wt.queue.v1` items carry `park.owner`, `park.decision` (the latest conductor/human
  note after the park: `{text, ts, by}` or null) and `park.overdue`.
- G3. `wt queue watch`:
  - replaces **Needs attention** with **Needs you** (yellow, human-owned parks only);
  - adds **Conductor handling** (dim), which shows the decision note and its age, or
    `overdue` when undecided past the threshold;
  - splits the header chips the same way.
- G4. `wt queue list` colours PARKED rows by owner and lists human-owned parks in a **Needs
  you** table.
- G5. A conductor-owned park still undecided after `park_decide_s` (default 300 s) fires a
  `park_undecided` watchdog event on the queue stream, which is the conductor's channel. It
  fires once per park and never goes to the human.

**Non-goals**
- IDEA-087's escalation system (records, notify, answer commands). This spec only reserves
  the `human_decision` reason as human-owned so that IDEA-087 plugs in.
- Notifying the human about anything (no desktop notifications or mail).
- Changing park reasons, `resume_to`, or the conductor's decision table beyond saying how
  ownership is shown.
- Persisting owner/decision in `state.json`. Both are derived when read (see Design).

## Decision

Add `PARK_OWNERS` and `park_owner(reason)` to `queue.py`: `main_dirty`, `main_not_on_base`
and `human_decision` map to `human`, every other reason maps to `conductor`. Add one shared
helper, `queue.park_view(cfg, state, now, sett)`. It reads the campaign's `park` and `note`
ledger events once and returns `{owner, decision, decided, overdue, parked_at}`.
`queue_payload` merges it into each PARKED item's `park`. `dashboard_payload` splits the
existing attention list into `needs_you` and `conductor_handling`. `ping_digest` reuses
the helper instead of its inline `decided` computation. `watchdog_checks` emits
`park_undecided` for overdue conductor parks.

## Design

### Ownership

```python
PARK_OWNERS = {"main_dirty": "human", "main_not_on_base": "human",
               "human_decision": "human"}          # IDEA-087 reserves human_decision
def park_owner(reason) -> str: return PARK_OWNERS.get(reason or "", "conductor")
```

- Derived from the reason, not stored. Every existing park (including those already on disk)
  gets an owner without migration. The table is the single authority. A reason is moved
  between owners by editing it, and a test pins the table.
- `human_decision` is **not** added to `PARK_REASONS` here. IDEA-087 adds the reason itself
  when it adds the command that creates it. The owner table only reserves the routing.
- `human` (a park made with `wt queue park`) is conductor-owned. Either actor may run that
  command, and the conductor's table covers held items. Its park `detail` is the decision,
  so such a park counts as **decided at park time** and never goes overdue. Choice: a
  deliberate hold is not "waiting for a decision".
- A pending `park_requested` (not yet applied) is conductor-owned, with decision
  `{text: "park requested: <detail>", ts: <requested_at>, by: <requester>}`.

### Decision record

`park_view` reads `store.read_ledger(cfg, cid, types=["park", "note"])`:
- `parked_at` is the `ts` of the latest `park` event, and `park_seq` is its seq.
- `decision` is the **latest** `note` with seq > `park_seq`, as `{text, ts, by}` (`by` from
  the envelope, `author` when present), or null. `decided = decision is not None or reason ==
  "human"`. Any author counts, as in SPEC-0212's `decided`. The conductor records decisions
  with `wt campaign note`, and a human note is a decision too.
- `overdue = owner == "conductor" and not decided and parked_for_s > park_decide_s`.
  `park_decide_s` is a new `agentic.events` setting, default 300. Human-owned parks are
  never overdue: they wait for the human by definition.

### `wt.queue.v1` (`queue_payload`)

- A PARKED item's `park` dict gains `owner`, `decision`, `decided`, `overdue`, `parked_at`.
  The change is additive, so the schema name is unchanged. Non-parked items keep
  `park: null` (or their stale park dict, as today). The new keys are only computed for
  PARKED items or items with `park_requested`, so the ledger is read only for those.
- A `park_requested` item gets a synthesized `park: {reason: null, owner: "conductor",
  decision: …, decided: true, overdue: false}` only in the dashboard. `queue_payload` keeps
  `park_requested: true` as today.

### Dashboard (`wt.queue.dashboard.v1`, `wt queue watch`)

- `dashboard_payload` gains the lists `needs_you` (human-owned) and `conductor_handling`
  (conductor-owned, overdue first, then oldest first). `attention` stays as the union, for
  existing JSON readers. `totals` gains `needs_you`, `conductor_handling`,
  `conductor_overdue`. `totals.attention` stays the sum. `by_project` counts `needs_you`
  and `conductor` instead of `attention`.
- Rendering (`cli_queue.py`):
  - `_needs_you_table` replaces `_attention_table`: title `Needs you (N)`, yellow border,
    and today's columns (reason, detail, resume to, for). It renders first.
  - `_conductor_table`: title `Conductor handling (N)`, `grey50` border, and dim rows with
    the columns campaign, project, title, reason, `decision` and `for`. The `decision`
    column shows one of:
    - the note text (folded, ≤ 2 lines) plus ` · 3m ago`;
    - `overdue 7m` in bold red when overdue;
    - `pending 2m` in dim when undecided but within the threshold.
  - Header chips: `needs you N` (yellow, bold when N > 0; dim `needs you 0` otherwise) and
    `conductor N` (dim, with ` (1 overdue)` in red when any are overdue). They replace the
    `attention` chip.
  - Empty sections are omitted, as today.

### `wt queue list`

- PARKED rows: the state cell is yellow for human-owned parks and dim for conductor-owned
  ones (`PARKED:rebase_conflict`), with a red `!` suffix when overdue.
- When any human-owned park exists, a `Needs you (N)` table (campaign, reason, detail,
  resume to) is printed after the Active table. `--json` is `wt.queue.v1` with the new keys.

### Watchdog (`events.py`)

- `EVENT_TYPES["park_undecided"] = ("reason", "parked_for_s", "owner")`.
- `watchdog_checks` adds, for each PARKED campaign whose `park_view` is overdue, the key
  `park_undecided:<cid>:<park_seq>`. The existing fire-once-per-condition mechanism emits one
  event, `by: watchdog`, with `campaign`/`spec_id`/`project` set. The seq in the key makes a
  new park of the same campaign fire again. A note clears the condition, and the
  condition never re-fires for that park.
- The stream is read by the conductor (`wt queue events --follow --for conductor`). The
  human gets nothing new. `R.queue_event` renders `park_undecided <cid> rebase_conflict 7m
  undecided`.
- `ping_digest`: the parked entries gain `owner` and `overdue`, and `decided` now comes from
  `park_view` (same rule, plus `human` reason ⇒ decided). `counts` gains `needs_you`, and
  `undecided_parks` counts conductor-owned undecided parks only. Human-owned parks are
  listed but are not the conductor's to decide.

### Conductor skill

`skills/wt-conduct/SKILL.md` gets a `park_undecided` row: "you left a park without a
decision note for 5 min: act per the park reason's row now, and record the decision with
`wt campaign note`." The ping row's "parked with `decided: false`" becomes "conductor-owned
parked with `decided: false`".

## Alternatives considered

- **Persist `park.owner` when parking.** Rejected. Existing parks on disk would need a
  migration, and moving a reason between owners would leave stale stored values. Deriving it
  is cheap (one table lookup).
- **Treat only notes prefixed `conductor:` as decisions.** Rejected. A human note on a park
  is also a decision, SPEC-0212 already counts any note, and a prefix convention breaks
  silently on a typo.
- **Hide conductor parks from the dashboard entirely.** Rejected. The human still wants to
  see that the system is handling something, and what it decided. Dimming says "not yours"
  without hiding state.
- **Send overdue conductor parks to the human.** Rejected by the idea. An undecided park is
  the conductor's failure to act, so the fix is to wake the conductor, not to hand the work
  back to the human.
- **Shorter ping interval instead of a per-park event.** Rejected. It adds noise for every
  item to catch one, and a watchdog condition fires exactly once per park.

## Acceptance criteria

- [ ] `queue.PARK_OWNERS` / `park_owner` map `main_dirty`, `main_not_on_base` and
      `human_decision` to `human`, and every other reason in `PARK_REASONS` (and unknown or
      None) to `conductor`.
- [ ] `wt queue list --json` PARKED items carry `park.owner`, `park.decision` (the latest
      note after the latest park, `{text, ts, by}`, or null), `park.decided`, `park.overdue`
      and `park.parked_at`. A note written before the park does not count. A `human`-reason
      park is decided.
- [ ] `overdue` is true only for a conductor-owned, undecided park older than
      `agentic.events.park_decide_s` (default 300), and the setting is honoured.
- [ ] `dashboard_payload` has `needs_you` and `conductor_handling` lists, keeps `attention`
      as their union, and its `totals` carry `needs_you`, `conductor_handling` and
      `conductor_overdue`. A pending park request goes to `conductor_handling`.
- [ ] `wt queue watch` renders `Needs you` (yellow) only for human-owned parks, and
      `Conductor handling` (dim) with the decision text and its age, `pending`, or `overdue`.
      The header shows `needs you` and `conductor` chips and no `attention` chip.
- [ ] `wt queue list` colours PARKED rows by owner (overdue marked) and prints a `Needs you`
      table only when a human-owned park exists.
- [ ] An overdue conductor park fires exactly one `park_undecided` event per park (a re-sync
      does not repeat it, a re-park fires again, a note stops it). A human-owned park never
      fires one.
- [ ] `ping_digest` parked entries carry `owner` and `overdue`, and `undecided_parks` counts
      conductor-owned parks only. The `wt-conduct` skill has the `park_undecided` row.
- [ ] `uv run pytest` passes; `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests:**
  - `tests/test_agentic_queue.py`:
    - the `park_owner` table over every `PARK_REASONS` member, plus `human_decision` and
      None;
    - `queue_payload` park keys for a decided park (note after park), an undecided one, a
      note before the park, a re-park after a note (undecided again), and a `human` park;
    - `overdue` with a frozen `now` on each side of 300 s and with a configured
      `park_decide_s`.
  - `tests/test_agentic_dashboard.py`:
    - one `main_dirty`, one decided `rebase_conflict`, one overdue `base_moving`, and one
      `park_requested` item give the split lists, `attention` as the union, and the totals;
    - the rendered text (Rich console, `width=140`) has `Needs you (1)`, `Conductor handling
      (3)`, the note text and its age, `overdue`, the chips `needs you 1` / `conductor 3 (1
      overdue)`, and no `Needs attention`.
  - `tests/test_agentic_queue_cli.py`: `wt queue list` with a human-owned and a
    conductor-owned park: the `Needs you` table appears, and it is absent with only
    conductor parks. `--json` keys.
  - `tests/test_agentic_events.py`:
    - `park_undecided` fires once for an overdue conductor park, does not repeat on a second
      `sync`, is not fired for `main_dirty`, fires again after a resume and a re-park (new
      seq), and does not fire once a note exists;
    - `ping_digest` `owner` / `overdue` / `undecided_parks` / `counts.needs_you`.
  - `tests/test_skills.py`: the `wt-conduct`
    skill mentions `park_undecided`.
- **Manual verification:** with the live data dir, run `uv run wt queue watch --once` (or a
  screenshot of `watch`) while a conductor park exists. It is under `Conductor handling`
  with the latest note, and `Needs you` is absent. Record the output in the spec.
- **Regression guard:** full `uv run pytest`. The existing dashboard tests that assert
  `Needs attention` are updated in the same change. That is the only intended rendering
  break.

## Clock Log

Built by the agentic loop or the human lane; idea bookkeeping via `wt spec reconcile --apply`.

## Rollout / migration

None. Owner and decision are derived when read, so existing parks render in the right section
at once. JSON readers of `attention` / `totals.attention` keep working. The conductor skill
row ships in the same change. The live runner re-execs on merge (SPEC-0194), and the next
`events` sync starts firing `park_undecided` for parks already overdue, once each.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

_None — decisions locked above._
