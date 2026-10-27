---
id: SPEC-0260
title: "Close train: serialize close steps per repository so the queue's own merges never invalidate a re-gate"
status: done
owner: user
created: 2026-10-26
updated: 2026-10-27
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0169
tags: [agentic, queue, merge, close, concurrency]
source_idea: IDEA-304
depends_on: [SPEC-0220]
---

## Context

Promoted from bug `IDEA-304`, "Close re-gates chase a moving base".

A close ([SPEC-0175](./0175-close-step-rebase-re-gate-ff-only-merge-or-pr.md)) rebases the
campaign branch onto the base, re-runs every gate on that tree, and merges it ff-only.
[SPEC-0220](./0220-close-regate-off-main-thread.md) moved the worktree phase
(`close.prepare_close`: guard, DoD, rebase, re-gate) onto the `StepPool`. Its goals say
"Two campaigns can be in the worktree phase of a close at the same time". Only
`close.finish_close` (merge, push, PR) runs on the main thread. `finish_close` re-checks the
base sha. If the base moved since the target was captured, it returns `retry`, and
`queue._apply` records `base_moved` and redoes the whole worktree phase. After
`close.MAX_CLOSE_ATTEMPTS` (2) attempts it parks `base_moving` (`finish_close.moved`).

With 6 slots this breaks down. On 2026-10-25, 21:30–21:46, SPEC-0236 and SPEC-0237 each
passed their re-gate (~5 min of pytest under load). Then SPEC-0228 merged, the base moved,
and both closes started over. After that, each one's merge invalidated the other again. With
N campaigns closing at once, every merge throws away up to N−1 re-gates (the wt re-gate takes
~9 min under load). A close parks `base_moving` whenever two merges land inside one of its
re-gates. Nearly all of these base moves are the queue's own merges: other campaigns'
closes, and the conductor's `wt queue merge` requests, which also go through CLOSING
([SPEC-0233](./0233-queue-merge-requests-branch-merges-as-campaigns.md),
[SPEC-0234](./0234-docs-only-merge-requests-and-conductor-skill-routing.md)).

What the code does today (verified):
- `queue._tick` step 2 plans every active campaign that has no flight. Each CLOSING campaign
  whose `plan_step` is `close` is appended to `pending_agent`. `_submit` then submits
  `_do_prepare_close` for each of them, so all CLOSING campaigns re-gate at the same time.
- `queue._tick` step 4 (single-shot `wt queue tick`, no pool) already closes one campaign at
  a time, in `busy` order.
- `queue._transition` sets `state["closing"] = {"attempts": 0}` when a campaign enters
  CLOSING and drops it when the campaign leaves. A `base_moved` redo is a CLOSING→CLOSING
  `finish`, which is not an entry, so `closing` survives it.
- `queue.plan_step` returns `resolve` instead of `close` while `closing.resolve` is set (the
  [SPEC-0221](./0221-rebase-conflict-resolver-persona.md) conflict resolver).
- `events.stuck_items` exempts a GATING/CLOSING campaign only while the time since it entered
  the state (`_since_state`) is within `gate_budget_s` (1800 s).

## Goals / Non-goals

**Goals**
- The queue's own merges never invalidate a re-gate that is running. For each repository, at
  most one campaign is in the close worktree phase at a time. The others wait and do not
  rebase or gate.
- Closes converge. Waiting campaigns close in FIFO order of when they entered CLOSING.
- Gates still run on the exact tree that is merged. `finish_close`'s checks of the base, the
  worktree HEAD and the branch tip are unchanged.
- A campaign waiting in the train is visible (a state field and one ledger note), and the
  stuck watchdog does not flag it.
- The change is additive. There is no new state, park reason or event type; `closing` gains
  keys.

**Non-goals**
- Base moves from outside the queue (a human or the conductor committing directly to the
  base). They keep the existing retry budget and the `base_moving` park.
- Speculative pre-gating (the next campaign gates on base + head of the train), and skipping
  the re-gate when only docs moved. Both are possible follow-ups (see Alternatives).
- Slot accounting. A waiting CLOSING campaign still holds its slot, as it does today.
- `close.py`. `prepare_close` and `finish_close` are unchanged.

## Decision

Each repository root has a **close train**. The train is derived from campaign state on
every tick; no lock is persisted.

1. When a campaign enters CLOSING, `_transition` stamps `closing.entered_at` (ISO timestamp).
2. In `_tick` step 2, a CLOSING campaign whose step is `close` is submitted only if it is the
   **head** of its repository's train and no other campaign of that repository has a close
   flight in the pool. The head is the campaign with the earliest `entered_at` among the
   non-parked, non-resolving CLOSING campaigns with the same `repo.root`. The other members
   get no flight this tick and wait.
3. A waiting member records `closing.queued_behind = <head id>` and appends one ledger
   `note`, and does both only when the head it waits behind changes. When it becomes head and
   its close is submitted, it drops `queued_behind` and records `closing.head_since`.
4. `events.stuck_items` skips a CLOSING campaign that has `queued_behind` set. For the head,
   the CLOSING age counts from `head_since` when that is present.
5. Single-shot ticks (step 4) take `pending_close` in train order.

A `base_moved` redo keeps the campaign at the head, because its `entered_at` does not
change. Leaving CLOSING (MERGED, TRIAGING, PARKED) takes a campaign out of the train. When it
comes back, that is a fresh entry, so it joins at the back.

This narrows SPEC-0220's goal "two campaigns can be in the worktree phase of a close at the
same time" to campaigns of **different** repositories. The rest of SPEC-0220 is unchanged:
the worktree phase off the main thread, the finish phase on it, and the stop and drain
rules. Following the precedent of SPEC-0139 and SPEC-0206, this spec amends one goal and does
not supersede the whole spec.

## Design

### `queue._transition`

In the `to == "CLOSING"` branch:
`state["closing"] = {"attempts": 0, "entered_at": store.now_iso()}`. The exit already pops
`closing`.

### `queue.close_train(states, close_flights) -> dict[str, list[str]]` (new, pure)

- `states` holds every non-PARKED campaign state in the tick's `items`. A campaign is a
  member when `state == "CLOSING"` and it has no `closing.resolve`. Campaigns already in
  flight are included, so the train covers the whole repository and not only the plannable
  campaigns.
- Members are grouped by `(state.get("repo") or {}).get("root")`. A campaign with no root
  forms its own group, keyed by its id, and is never blocked.
- Each group is sorted by `(parsed closing.entered_at, id)`. A missing or unparseable
  timestamp sorts as the earliest possible time. A legacy campaign that entered CLOSING
  before this change therefore sorts first.
- A member listed in `close_flights` (the ids whose pool flight's first task is
  `Step("close")`) moves to the front of its group. A running close is always the head, even
  when the ordering data is odd.
- The result is `{root: [ids in train order]}`.

### `queue._tick` step 2

Before the planning loop, compute
`trains = close_train(<non-parked items>, {cid for cid, fl in pool.flights.items() if fl.tasks and fl.tasks[0][0].kind == "close"})`
and a reverse map `id → (head id, position)`. In the `step.kind == "close"` branch, before
the existing `stop` check and before `pending_agent` / `pending_close`:
- If the campaign is not at position 0, call `_train_wait(cfg, st, head_id, position)` and
  `continue`.
- Otherwise call `_train_head(cfg, st)` and go on as today.

`_train_wait` runs under `store.item_lock`. If `closing.queued_behind` already equals
`head_id`, it does nothing, so later ticks at the same position write nothing. Otherwise it
re-reads the state, and if the campaign is still CLOSING it sets `closing.queued_behind`,
appends `note {"text": f"close queued behind {head_id} (position {position})", "author":
"queue"}` and writes the state.

`_train_head` also runs under the lock. If `queued_behind` is set, it pops it, sets
`closing.head_since = store.now_iso()` and writes. It updates the in-memory `st` as well,
which `_do_prepare_close` and `_apply.finish` carry forward through the `closing` key.

### `queue._tick` step 4 (single shot)

`pending_close` is sorted by the same key, `(entered_at, id)`, before the loop. The loop
already runs one close at a time.

### `events.stuck_items`

In the `elif st["state"] in ("GATING", "CLOSING")` branch, for CLOSING:
- If `closing.queued_behind` is set, `continue`: the campaign is waiting in the train, not
  stuck.
- Otherwise, if `closing.head_since` is set, the budget check uses `_age_s(head_since, now)`
  instead of `age`.

The waiters can afford to be skipped because the head is the only campaign that can hang,
and the head keeps its own stuck check.

### Visibility

`wt campaign show` already prints `closing`. The note appears in `wt campaign log` and in
the events stream. The CLI does not change.

### As shipped (2026-10-27)

Built as designed in `queue.py` (`_transition`, `_train_key`, `close_train`, `_train_wait`,
`_train_head`, `_tick` steps 2 and 4) and `events.stuck_items`. Details the Design left open:
- `close_train` takes every non-PARKED item and filters itself (non-CLOSING and resolving
  campaigns are dropped there), so a REVIEWING or PARKED state passed in is harmless.
- Single shot: the train check in step 2 lets only the head of each root into
  `pending_close`, so a single-shot `wt queue tick` closes one campaign per repository per
  tick; the next member closes on the next tick. Step 4 still sorts by `(entered_at, id)`.
- `_train_head` writes nothing unless the campaign was a waiter (`queued_behind` set), so a
  campaign that was head from the start has no `head_since` and the watchdog keeps counting
  from its CLOSING transition. It re-checks the on-disk state is still CLOSING before writing.
- Existing tests that asserted `closing == {"attempts": 0}` (`test_agentic_close_inflight.py`,
  `test_agentic_resolver.py`) now go through a `_no_entry` helper that requires
  `entered_at` and compares the rest.
- An extra test, `test_different_roots_close_concurrently`, covers the cross-root criterion
  in the pooled runner (B's `repo.root` is rewritten while it waits, and its close step is
  stubbed).

## Alternatives considered

- **Unbounded base-move retries while first in a FIFO close order.** The closes would still
  run concurrently, wasting N−1 re-gates per merge and keeping the CPU load that makes
  re-gates slow. The train removes the cause instead.
- **A persisted merge-token lock file.** It would need crash recovery and stale-lock
  handling. A train derived from state on every tick survives runner restarts with no extra
  code.
- **Serialize only the finish phase and pre-gate speculatively** (the next campaign gates on
  base + head). Rebasing onto an unmerged head brings new failure modes. This stays a
  possible follow-up if train latency turns out to be the bottleneck.
- **Skip the re-gate when only docs moved.** Deferred, because it changes what "gated on the
  merged tree" means. The train already stops docs-only merge requests from invalidating a
  running re-gate: they wait their turn.

## Acceptance criteria

- [x] Entering CLOSING sets `closing.entered_at`. A `base_moved` redo keeps it, and
      re-entering after TRIAGING or PARKED sets a new one.
- [x] `queue.close_train` exists and is pure. Per `repo.root` it orders members by
      `(entered_at, id)`, leaves out resolving campaigns, and puts an in-flight close at the
      front.
- [x] In the pooled runner there is never more than one `_do_prepare_close` flight per
      repository root. While the first campaign's re-gate is held, a second CLOSING campaign
      of the same root gets no close flight.
- [x] When two campaigns reach CLOSING together, both merge with zero `base_moved` rows and
      exactly one close started each.
- [x] A waiting member has `closing.queued_behind` set to the head's id. It gets exactly one
      `close queued behind` note per change of head, however many ticks it waits.
- [x] `events.stuck_items` does not report a campaign waiting in the train, and the head's
      CLOSING budget counts from `head_since`.
- [x] Campaigns in different repository roots close concurrently.
- [x] A base move from outside the queue during the head's re-gate still redoes the close
      (existing SPEC-0220 behaviour).
- [x] `uv run pytest` passes, and `python3 tools/spec_lint.py` exits 0.

## Test plan

New file `tests/test_agentic_close_train.py`. It reuses the `Env`, `mk` and `_held_closing`
helpers from `tests/test_agentic_close_inflight.py`, where each spec's re-gate blocks while
`hold-<spec>` exists.

- `test_close_train_orders_by_entry_then_id`: unit test of `close_train` on hand-built
  states, with two roots and three members on root R. Asserts the order by `entered_at`, the
  id tie-break, that a member with no `entered_at` sorts first, and that root S is a
  separate train.
- `test_close_train_skips_resolving_and_parked`: a member with `closing.resolve` is not in
  the train, and neither is a PARKED campaign.
- `test_close_train_inflight_member_is_head`: the member in `close_flights` comes first even
  though its `entered_at` is later.
- `test_second_close_waits_while_first_regates` (slots=2, specs=2):
  - Hold both specs. Spin until A's close re-gate runs and B is CLOSING.
  - Assert `env.started(b, "close") == []`, B is not in `pool.flights`,
    `st(b)["closing"]["queued_behind"] == a`, and `env.gate_runs("SPEC-0002") == 1` (its
    GATING run only).
  - Tick 5 more times, then assert B has exactly one `close queued behind` note.
  - Release A and spin until both are MERGED. Assert:
    - no `base_moved` row for either campaign;
    - one close started for each;
    - B had `closing.head_since` set after its close was submitted (captured during the
      spin);
    - main holds both features and has no merge commits.
- In `tests/test_agentic_close_inflight.py`, rewrite
  `test_two_worktree_phases_concurrent_merges_serial` and rename it
  `test_two_closes_go_through_the_train`. Its old expectations (one `base_moved`, started
  counts `[1, 2]`) become `moved == []` and started `[1, 1]`. This is the SPEC-0220 goal
  that this spec narrows.
- `test_entered_at_set_and_reset`:
  - Drive a campaign to CLOSING and assert `entered_at` is set.
  - Move main during a held re-gate (a `base_moved` redo) and assert `entered_at` did not
    change.
  - `wt queue park` it, then `wt queue resume` to CLOSING, and assert a later `entered_at`.
- `test_stuck_items_ignores_train_waiter_and_uses_head_since`: unit test on states written
  through `store`, with `stuck_after_s=0`.
  - A CLOSING campaign with `queued_behind` and a CLOSING transition older than
    `gate_budget_s` is not reported.
  - A head whose transition is that old but whose `head_since` is recent is not reported.
  - The same head with an old `head_since` is reported.
- `test_single_shot_closes_in_train_order`: two CLOSING campaigns go through `Q.tick` with no
  pool. The one with the earlier `entered_at` (set by hand) merges first, judged by the order
  of the `close` events, even when `busy` lists it second.
- Regression:
  - all of `tests/test_agentic_close_inflight.py`, notably
    `test_base_moved_during_regate_redoes_then_merges`,
    `test_base_moving_every_attempt_parks`, `test_watchdog_quiet_while_closing` and
    `test_reexec_drains_inflight_close`;
  - `tests/test_agentic_queue_merge.py`;
  - the full `uv run pytest`.

**Sabotage check:**
1. In `_tick` step 2, remove the train check so every close is submitted as before, and run
   `test_second_close_waits_while_first_regates`. It must fail, because B gets a close flight
   while A is held.
2. Make `close_train` sort by id only and run `test_close_train_orders_by_entry_then_id`. It
   must fail.

Restore both and record the two failures in the Test record.

### Test record (2026-10-27)

- `uv run pytest -q tests/test_agentic_close_train.py`: 8 passed (the 7 planned tests plus
  `test_different_roots_close_concurrently`).
- `uv run pytest -q -x tests/test_agentic_close_inflight.py tests/test_agentic_resolver.py
  tests/test_agentic_queue_merge.py`: 166 passed, including the renamed
  `test_two_closes_go_through_the_train` (`moved == []`, started `[1, 1]`).
- Sabotage 1: train check in `_tick` step 2 disabled (`if False and position:`);
  `test_second_close_waits_while_first_regates` failed (B had a `close` started row while A
  was held). Restored.
- Sabotage 2: `close_train` sorted by id only; `test_close_train_orders_by_entry_then_id`
  failed (`legacy` sorted last instead of second). Restored.
- `python3 tools/spec_lint.py --write-ledger && python3 tools/spec_lint.py`: exit 0.
- Full `uv run pytest -q -x`: 3116 passed, 3 skipped.

## Rollout / migration

Campaigns already in CLOSING when this ships have no `entered_at`. They sort first, in id
order, and an in-flight close stays head. No data migration is needed.

## Definition of done

- [x] Every acceptance criterion is checked and true.
- [x] The Test plan has been executed, including the sabotage check, and the results are in a
      `### Test record`.
- [x] `uv run pytest` passes with no regressions.
- [x] The spec body reflects what shipped (`### As shipped`), the ledger is regenerated and
      spec lint exits 0.

## Open questions

(none)
