---
id: SPEC-0314
title: "A CLOSING campaign waiting in the close train behind a live head is not `campaign_silent`, and the quiet follower holds it"
status: proposed
owner: user
created: 2026-10-30
milestone: "M10: agentic MVP"
kind: feature
source_idea: IDEA-462
depends_on: [SPEC-0260, SPEC-0287, SPEC-0300]
tags: [agentic, conductor, quiet, events, watchdog, close-train]
---

## Context

Bug `IDEA-462`. The quiet conductor follower (SPEC-0300) delivered `campaign_silent` for
`SPEC-0302-20261030-3259` in `CLOSING` while a close re-gate (the project's full pytest) was
running. The conductor's only possible decision was the one it recorded in the campaign ledger
at 22:11:04: `no_action (campaign_silent): close re-gate running`. The idea blames the
close-in-progress fact not being recognised after a runner re-exec (`runproc` children). The
runner journal and the event log of 2026-10-30 say otherwise.

What happened (`~/.local/share/wt/agentic/runner.jsonl`, `events.jsonl`, the SPEC-0302 ledger):

- 21:55:52 `SPEC-0302` went `REVIEWING -> CLOSING`. `SPEC-0304-20261030-7a4f` had entered
  `CLOSING` at 21:54:16 and its close (with the full re-gate pytest) was running in the pool.
- `SPEC-0302` therefore waited in the close train (SPEC-0260: one close worktree phase per
  repository root, FIFO): `queue._train_wait` set `closing.queued_behind =
  "SPEC-0304-20261030-7a4f"` and wrote one ledger note. No step was submitted for it, so it was
  never in the pool, and the runner's `in_flight` lease list (`_SlotWatch.check`,
  `queue.py:4355`, `sorted(pool.ids())`) did not name it. The runner journal shows no `close`
  step started for `SPEC-0302` until 22:15:31, when the head had merged.
- 22:10:55 the watchdog raised `campaign_silent` (`events.watchdog_checks`, `events.py:786`:
  the campaign's ledger tail is older than `silent_after_s`, 900 s). The follower's
  `quiet._silent_hold` (`quiet.py:166`) holds only when the campaign is in the lease's
  `in_flight` or is the lease's fresh `busy` campaign; the waiter is neither (the *head* is), so
  the event was delivered and cost a conductor wakeup.
- The runner re-exec (`stop reason code_changed`, 22:15:31) came **after** this wake and is not
  involved: `in_flight` is rewritten every loop pass from the live pool, and the close that ran
  after the re-exec (22:15:37-22:33:42) was in `in_flight` for its whole length.

The same blind spot is already closed for the sibling check. `events.stuck_items` skips a
`CLOSING` campaign whose `closing.queued_behind` names another campaign that is itself
`CLOSING` (SPEC-0260, SPEC-0287; `events.py:488-492`), and measures a former waiter from
`closing.head_since`. `watchdog_checks` has no such rule for `campaign_silent`: it reads only
the ledger tail (`events.py:762-770`). A campaign legitimately waits in the train for as long as
the head's close takes, which on this repository is 15-20 minutes per close, longer than
`silent_after_s`, so every deep close train produces false `campaign_silent` events, one per
waiter.

## Goals / Non-goals

**Goals**
- The watchdog does not raise `campaign_silent` for a `CLOSING` campaign that waits in the close
  train behind a live `CLOSING` head, and measures a campaign that has just become head from
  `closing.head_since`, exactly as `stuck_items` does.
- Where such an event still arrives at the quiet follower (raised before the campaign queued, or
  by an older watchdog), `campaign_silent` and `state_stuck` are held while the waiter's head is
  itself held by the existing rule (its close or gate is alive in the runner). A head that is not
  alive still wakes, through its own event or the waiter's.
- One shared predicate for "waits in the close train behind a live head", used by `stuck_items`,
  `watchdog_checks` and the follower, so the three cannot drift.

**Non-goals**
- No change to `in_flight`, the pool, the close train order, `queued_behind` or `head_since`
  writes, or the runner lease shape.
- No change to the re-exec / handover path (`Lease.handover`, `lease.acquire`): the investigation
  found no loss of the close-in-progress fact there. A regression test pins that `in_flight`
  survives it (see the Test plan).
- No change to `silent_after_s`, `gate_budget_s` or the wake classes of other event types.
- The `conductor_ping` digest is unchanged: its `stuck` list comes from `stuck_items`, which
  already skips waiters.

## Decision

A `CLOSING` campaign whose `closing.queued_behind` names a *different* campaign that is
`CLOSING` is "waiting in the close train". A new predicate `events.waits_in_close_train(st,
closing_ids)` expresses that (the rule `stuck_items` already inlines). The watchdog skips
`campaign_silent` for such a campaign and, for a former waiter, takes the silence clock as the
minimum of the ledger-tail age and `now - closing.head_since`. The quiet follower additionally
holds `campaign_silent` and `state_stuck` for a waiter whose head is held by `_silent_hold`.

## Design

**`src/wt/agentic/events.py`**
- `waits_in_close_train(st: dict, closing_ids: set) -> bool`: true when `st["state"] ==
  "CLOSING"`, `head := (st.get("closing") or {}).get("queued_behind")` is truthy,
  `head != st["id"]` and `head in closing_ids`. `stuck_items` replaces its inline test (the
  `head and head != cid and head in closing_ids` condition, `events.py:490-492`) with a call to
  it; behavior unchanged (`test_stuck_items_ignores_train_waiter_and_uses_head_since` and
  `test_stale_queued_behind_is_reported_stuck` keep passing).
- `watchdog_checks`: compute `closing_ids = {r["id"] for r in rows if r.get("state") ==
  "CLOSING"}` once. In the per-campaign loop, after the open-run branch and before the silence
  computation, `continue` when `waits_in_close_train(r, closing_ids)`. For a `CLOSING` campaign
  with `closing.head_since`, `silent` becomes `min(tail age, age of head_since)` (a missing or
  unparsable `head_since` is ignored), so a campaign that just became head is not read as
  silent for the whole of its wait. A stale `queued_behind` (head merged, missing or the
  campaign itself) does not skip: the normal ledger clock applies, as in SPEC-0287.

**`src/wt/agentic/quiet.py`**
- `_silent_hold(ev, ctx)` keeps its current checks (campaign in `in_flight`, or fresh `busy` for
  it) and gains a third: if the event's campaign is a train waiter, return the same check for its
  head. `Ctx` gets a lazy property `closing_waiters` -> `dict[str, str]` mapping each campaign id
  whose `waits_in_close_train` is true to its head id, built from one
  `store.list_campaigns(cfg)` scan on first use (like `live_specs`; tests inject it through a new
  optional `Ctx(..., closing_waiters=None)` argument). The head's own check is the existing
  lease test; it is not recursive (a head has dropped `queued_behind`, `_train_head`).
- Applies to both `campaign_silent` and `state_stuck` (they share `_silent_hold`,
  `quiet.py:201`). Nothing else in `classify` changes; held events flow through the existing
  digest path.
- Why a waiter cannot go silent forever: while it is held, its head is in `in_flight` (or is the
  fresh `busy` close). If the head's close stalls, hangs or parks, the head's own
  `campaign_silent` / `state_stuck` / `parked` event wakes (it is no longer held, or never was);
  if the runner dies, `runner_dead` wakes; when the head merges, the waiter becomes head
  (`head_since`, then its own step in `in_flight`).

**Docs**
- `skills/wt-conduct/SKILL.md`: the *Quiet stream* "Held while a fact says so" entry for
  `campaign_silent`, `state_stuck` becomes "(its gate or close is still running in the runner,
  or it waits in the close train behind a head that is)". The type list in that line is
  unchanged, so `tests/test_skills.py::test_conduct_skill_quiet_rules_match_code` still holds.
- `docs/product/capabilities/agentic.md`: the watchdog paragraph gets one sentence that
  `campaign_silent` skips a close-train waiter and the quiet stream holds one behind a live head.
  SPEC-0260, SPEC-0287 and SPEC-0300 are not edited (history).

## Alternatives considered

- **Add the waiter to `in_flight`.** Rejected: `in_flight` means "has a step in the pool" and
  feeds slot accounting and `_occupied`; a train waiter has no step, and changing the lease
  contract for a reporting problem would ripple into admission and `slots_full_at`.
- **Fix only the follower (hold, keep emitting).** Leaves a false `campaign_silent` in the event
  log for every other consumer (TUI, `wt queue events`, the ping), and `stuck_items` already
  sets the precedent of fixing this at the source.
- **Fix only the watchdog.** Misses events raised before the campaign queued behind a head, or
  by a watchdog running older code; the follower decides with live facts at delivery time
  (the SPEC-0305 / SPEC-0306 pattern), which is cheap here.
- **Hold any waiter regardless of the head's liveness.** Would hide a stalled train: the waiter's
  event is the second signal when the head's close dies without its own event.
- **Raise `silent_after_s` for CLOSING.** Global and blunt: it delays real silence on a close
  that is not in the train, and a train of N closes still outlasts any fixed number.

## Acceptance criteria

- [ ] `events.waits_in_close_train` exists and is true exactly for a `CLOSING` campaign whose
      `closing.queued_behind` is a different `CLOSING` campaign id; false for a head, a
      non-`CLOSING` campaign, a missing/empty `queued_behind`, a self reference, and a head that
      is `MERGED` or absent.
- [ ] `stuck_items` uses the predicate and its output is unchanged for the SPEC-0260 / SPEC-0287
      fixtures.
- [ ] `watchdog_checks` raises no `campaign_silent` for a `CLOSING` waiter behind a `CLOSING`
      head whose ledger tail is older than `silent_after_s`, and still raises it for the head
      itself when its tail is that old.
- [ ] `watchdog_checks` raises `campaign_silent` for a `CLOSING` campaign whose
      `queued_behind` is stale (head `MERGED`, absent, or itself) and whose tail is old.
- [ ] A `CLOSING` campaign with a recent `closing.head_since` and an old ledger tail raises no
      `campaign_silent`; one with an old `head_since` and an old tail does.
- [ ] `quiet.classify` returns `hold` for `campaign_silent` and `state_stuck` of a waiter whose
      head is in the lease `in_flight`, or is the lease's fresh `busy` campaign; and `wake` when
      the head is not in `in_flight`, the lease is not held, the busy step is past its budget, or
      the campaign is not a waiter (including the existing own-campaign cases).
- [ ] No other event type's classification changes (`RULES` untouched).
- [ ] The `conduct` skill line and `docs/product/capabilities/agentic.md` carry the new sentence;
      `tests/test_skills.py` passes.
- [ ] `in_flight` in the lease still names a campaign whose close is running after a handover
      (`lease.acquire` adopts the descriptor) and the follower holds its `campaign_silent`.

## Test plan

- **Automated tests**
  - `tests/test_agentic_close_train.py`:
    `test_waits_in_close_train_predicate` (the true/false matrix of the first criterion);
    `test_watchdog_silent_skips_train_waiter` (head and waiter CLOSING with old ledger tails,
    `watchdog_checks` yields `campaign_silent` for the head only; a stale `queued_behind` yields
    one for the waiter); `test_watchdog_silent_uses_head_since` (recent vs old `head_since`).
    They reuse the `mk` fixture and the `S.create_campaign` / `S.append_event` helpers of
    `test_stuck_items_ignores_train_waiter_and_uses_head_since`, with `now` moved past
    `silent_after_s`.
  - `tests/test_agentic_quiet.py`: `test_silent_held_for_train_waiter_behind_live_head` (the
    lease variants: head in `in_flight`, head fresh `busy`, head absent, lease not held, busy
    past budget; both `campaign_silent` and `state_stuck`; `Ctx(..., closing_waiters={...})`);
    `test_silent_not_held_for_waiter_without_train_entry` (no mapping entry -> `wake`);
    `test_closing_waiters_built_from_store` (campaigns written to a temp store, the lazy property
    maps waiter -> head and omits stale references).
  - `tests/test_agentic_lease.py`: `test_in_flight_survives_handover` (take a lease, write
    `in_flight=["c1"]`, `handover()`, `acquire` in the same process with the env; `lease_status`
    still reports `in_flight == ["c1"]`, `busy` absent) - the pin for the re-exec hypothesis.
  - Existing `test_conduct_skill_quiet_rules_match_code` in `tests/test_skills.py`.
- **Manual verification:** with two specs of one project both in `CLOSING` (the second behind
  the first), `uv run wt queue events --for conductor --follow` for longer than
  `silent_after_s` shows no `campaign_silent` for the second while the first closes; and `uv run
  wt queue conductor-brief --json` lists neither as stuck.
- **Regression guard:** `uv run pytest` (whole suite) including
  `tests/test_agentic_close_train.py::test_stuck_items_ignores_train_waiter_and_uses_head_since`,
  `::test_stale_queued_behind_is_reported_stuck`,
  `tests/test_agentic_quiet.py::test_silent_held_while_in_flight` and
  `tests/test_agentic_events.py` (the watchdog `campaign_silent` case at line 148).

## Rollout / migration

None. The change is read-side (watchdog, follower, skill text, docs). A running follower exits on
`code_changed` and restarts on the new code (SPEC-0231). Events already in the log are not
rewritten.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

None.
