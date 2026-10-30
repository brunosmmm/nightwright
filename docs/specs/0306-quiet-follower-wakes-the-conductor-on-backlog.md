---
id: SPEC-0306
title: "Quiet follower holds `backlog_blocked_long` (and its ping item) while the blocker is an ordinary dependency wait with a live campaign"
status: proposed
owner: user
created: 2026-10-30
milestone: "M10: agentic MVP"
kind: feature
source_idea: IDEA-412
depends_on: [SPEC-0300]
tags: [agentic, conductor, quiet, events]
---

## Context

SPEC-0300 made the `conductor` event follower quiet: `quiet.RULES` (`src/wt/agentic/quiet.py:25`)
holds informational events and folds them into a periodic `conductor_digest`. A plain
`backlog` event is already held while its `blocked_by` only restates an ordinary dependency wait
(`quiet.backlog_resolved`, `quiet.py:119`): `waiting on SPEC-X (...)`, or the queued spec still
`draft` / `proposed` / `in-progress`.

`backlog_blocked_long` is the watchdog's follow-up for the same condition: a QUEUED item whose
stored readiness has been `blocked` for longer than `blocked_after_s` (default 1200,
`events.blocked_items`, `events.py:432`). It carries `blocked_for_s` and the same `blocked_by`
text. `quiet.RULES` lists it as an unconditional `wake` (`quiet.py:33`), so every campaign that
queues behind a dependency that is merely still being built (the normal case for a spec chain)
wakes the conductor once, 20 minutes after it queued, and the conductor's decision-table row
(`skills/wt-conduct/SKILL.md`, `backlog_blocked_long`) ends in "record a wait decision and wait".
That is a wakeup spent on a non-decision, which the SPEC-0298 MVP counts against the
wakeups-per-merge target (exit criterion E3).

The periodic `conductor_ping` repeats the leak: its `blocked` list is every `blocked_items`
row regardless of what blocks it, and `quiet.ping_fingerprint` (`quiet.py:84`) includes
`("blocked", campaign)`. A new benign-blocked item therefore changes the fingerprint and wakes
the conductor once through the ping, even when the `backlog_blocked_long` event is held.

## Goals / Non-goals

**Goals**
- `backlog_blocked_long` is held when `blocked_by` is only ordinary dependency waits **and every
  dependency it waits on has a live campaign** (a campaign for that spec in a non-terminal
  state, so something is building it). It wakes for anything else: a dependency with no live
  campaign, a missing, rejected, superseded or done spec, a missing branch, an unknown idea, an
  empty `blocked_by`.
- A `conductor_ping` does not count such an in-flight-dependency-blocked item as actionable, so
  the same item cannot wake the conductor through the ping.
- A held item stays visible: it appears in the `conductor_digest` like any held event.

**Non-goals**
- Changing the watchdog threshold (`blocked_after_s`), what `events.sync` appends, or the
  contents of the `backlog_blocked_long` event or of the ping.
- Detecting a blocker that is itself stuck. A parked, stuck, silent or dead blocker already
  raises its own wake events (`parked`, `state_stuck`, `campaign_silent`, `runner_dead`), so the
  blocked item needs no separate alarm.
- A dependency with no live campaign is not in flight; that item still wakes. This includes a
  dependency whose only campaign is terminal (merged, handed off, abandoned) while the spec
  status has not caught up: the conductor must reconcile or queue it.
- Changing which `backlog` texts count as benign (SPEC-0300 stands). The plain `backlog` event
  keeps its text-only test; only `backlog_blocked_long` and the ping add the campaign check,
  because they fire after 20 minutes of waiting, when "nothing is building the dependency" has
  become the likely explanation.

## Decision

`backlog_blocked_long` becomes a conditional type (`"cond"`) in `quiet.RULES`. `classify` holds
it only when `dependency_wait(blocked_by, live_specs)` is true: `blocked_by` is a non-empty list,
every entry passes the benign-prefix test `backlog` already uses (a shared helper, so the two
cannot drift), **and** every `waiting on SPEC-X (...)` entry names only dependencies that have a
campaign in a non-terminal state. Entries of the form `spec status is draft` / `proposed` /
`in-progress` describe the queued spec itself, not a dependency, and keep the plain prefix test.
Anything else wakes. The ping fingerprint applies the same test to its `blocked` items. The set
of live specs is read lazily from the store through `Ctx`, the same way `Ctx.lease` is read.
Everything else about the quiet follower is unchanged, and unknown event types still wake.

## Design

- `src/wt/agentic/quiet.py`:
  - `Ctx.live_specs` (lazy property, cached for one poll like `Ctx.lease`): the set of
    `spec_id` of `store.list_campaigns(cfg)` (which hides terminal states, `store.py:342`).
    `Ctx.__init__` takes an optional `live_specs=` so tests inject it without a store.
  - New `waited_ids(entry) -> list[str]`: the dependency ids a `waiting on ...` entry is
    waiting for, taken from the text after its last `waiting on ` (regex `[A-Za-z]+-\d+`). For
    `waiting on SPEC-0001 (proposed)` that is `["SPEC-0001"]`; for the superseded form
    `waiting on SPEC-0001 (superseded; waiting on SPEC-0007, SPEC-0008)` it is the replacements
    `["SPEC-0007", "SPEC-0008"]`, which are what actually block (the superseded spec has no
    campaign by design). An entry that yields no id is not in flight.
  - New `dependency_wait(blocked_by, live_specs) -> bool`: true when `blocked_by` is a non-empty
    list, every entry is a string starting with one of `_BENIGN_BLOCKED`, and every entry that
    starts with `waiting on ` has a non-empty `waited_ids` that is a subset of `live_specs`.
    `backlog_resolved` keeps its text-only verdict through a shared helper `_benign(entry)`, so
    `backlog` behavior is byte-for-byte unchanged (an empty `blocked_by` on a non-ready `backlog`
    event is still held).
  - `RULES["backlog_blocked_long"]` changes from `"wake"` to `"cond"`.
  - `classify`: `if t == "backlog_blocked_long": return "hold" if dependency_wait(data.get(
    "blocked_by"), ctx.live_specs) else "wake"`. A missing or non-list `blocked_by` wakes.
  - `ping_fingerprint(data, live_specs=frozenset())`: the `blocked` category skips an item when
    `dependency_wait(it.get("blocked_by"), live_specs)`; the other categories are unchanged. With
    the default empty set no dependency counts as live, so a caller that forgets the argument
    gets an extra wake, never a miss. An item without a `blocked_by` key (older ping payloads) is
    counted. `classify`, `Digest.add` (new optional `ctx` argument, passed by `events.follow`)
    and `Digest.note_ping_wake` (same) all pass `ctx.live_specs`, so the fingerprint stored in
    `last_ping_fp` is computed exactly like the one it is later compared with.
- Held `backlog_blocked_long` events flow through the existing digest path
  (`Digest.add` and `campaigns["backlog_blocked_long"]`), so the digest line names the
  campaigns and counts them under `held`. No new digest fields.
- Why the item cannot go silent: the watchdog key is `backlog_blocked_long:{cid}`, fired once
  per blocked stretch (`events.py:767`). A held event is not re-sent, so the hold must only
  apply while the dependency is in flight. If that dependency's campaign later parks, goes stuck
  or dies, its own wake event fires; if it finishes, the blocked item becomes ready and is
  admitted. A dependency with no campaign never produces an event of its own, which is exactly
  the case the campaign check sends to the conductor.
- `skills/wt-conduct/SKILL.md`: the *Quiet stream* "Held while a fact says so" list gains
  `backlog_blocked_long` (only an ordinary dependency wait whose dependencies each have a live
  campaign), in the same position style as `backlog`. The `backlog_blocked_long` table row keeps
  its "queued, parked or being merged" action and adds: "it arrives only when a dependency has
  no live campaign or the blocker is not an ordinary dependency wait; in a `conductor_digest`
  that lists it, check that each `waiting on SPEC-X` has a campaign; if none, queue it or
  escalate". `tests/test_skills.py::test_conduct_skill_quiet_rules_match_code` compares the list
  with `quiet.RULES`.
- Docs: `docs/product/capabilities/agentic.md` (the watchdog paragraph at line 470) gets one
  sentence that the quiet conductor stream holds a `backlog_blocked_long` whose dependencies
  are all being built. SPEC-0300 and SPEC-0212 are not edited (their text is history); this
  spec records the change.

## Alternatives considered

- **Keep `backlog_blocked_long` as an always-wake and rely on the decision-table "wait"
  outcome.** That is today's behavior and the bug: the table's own outcome for the common case
  is to do nothing.
- **Hold every text-level dependency wait (reuse the `backlog` test unchanged).** Simpler, but
  `waiting on SPEC-X (proposed)` is true whether or not anything is building SPEC-X. The event
  fires once per blocked stretch, so a dependency nobody queued would appear in one digest and
  never wake the conductor again. The campaign check costs one store scan per poll.
- **Make it an always-hold type.** An item blocked on a missing or terminal spec can never
  become ready; the conductor must hear about it.
- **Raise `blocked_after_s` for dependency waits.** Changes the watchdog for every reader (TUI,
  stewards) and still wakes the conductor, only later.
- **Hold the event but leave the ping alone.** The ping would wake the conductor once for the
  same item, so the leak would move rather than close.

## Acceptance criteria

- [ ] `quiet.RULES["backlog_blocked_long"] == "cond"`. With `live_specs` containing `SPEC-0001`,
      `classify` returns `"hold"` for a `backlog_blocked_long` event whose `blocked_by` is
      `["waiting on SPEC-0001 (in-progress)"]`, `["waiting on SPEC-0001 (proposed)"]`, or
      `["spec status is draft"]` / `proposed` / `in-progress`, and for the superseded form
      `["waiting on SPEC-0001 (superseded; waiting on SPEC-0007)"]` when `SPEC-0007` is live.
- [ ] `classify` returns `"wake"` for `["waiting on SPEC-0001 (proposed)"]` when `live_specs`
      does not contain `SPEC-0001` (no campaign, or only a terminal one), for the superseded
      form when its replacement has no live campaign, and for an entry naming two dependencies
      of which one is not live.
- [ ] `classify` returns `"wake"` for a `backlog_blocked_long` event whose `blocked_by` contains
      `"SPEC-0999 not found"`, `"spec status is rejected"` (also `superseded`, `done`, `missing`),
      `"branch x missing"`, `"idea IDEA-1 not found"`, a mix of one benign and one non-benign
      entry, is empty, or is absent, even when every named dependency is live.
- [ ] `Ctx.live_specs` is read from the store on first use only (a poll that sees no
      `backlog_blocked_long` or blocked ping item does not scan campaigns) and contains the
      `spec_id` of non-terminal campaigns only.
- [ ] `ping_fingerprint` omits a `blocked` item whose `blocked_by` passes `dependency_wait` with
      the supplied `live_specs`, keeps one whose dependency is not live, one with any other text
      and one without a `blocked_by` key, and leaves the other categories unchanged. A ping whose
      only actionable content is such an item is held, and the fingerprint stored by
      `note_ping_wake` equals the one `classify` compares against.
- [ ] End to end through `events.follow` in quiet mode, a held `backlog_blocked_long` prints no
      line, advances the cursor, is recorded as `hold`, and appears under `held` and `campaigns`
      in the next `conductor_digest`. A waking one (including a dependency with no live
      campaign) prints and is recorded as `wake`.
- [ ] `events.jsonl`, `wt queue events` without `--follow`, and `wt queue events --follow --all`
      still show every `backlog_blocked_long` event, unchanged.
- [ ] The skill's *Quiet stream* "Held while a fact says so" list names `backlog_blocked_long`,
      its table row contains the digest check ("each `waiting on SPEC-X` has a campaign"), and
      `test_conduct_skill_quiet_rules_match_code` passes.
- [ ] `tests/test_agentic_quiet.py` no longer lists `backlog_blocked_long` among the unconditional
      WAKE types, and all existing quiet, events and ping tests pass.

## Test plan

- **Automated tests:**
  - `tests/test_agentic_quiet.py`: remove `backlog_blocked_long` from the `WAKE` list; add
    `test_classify_backlog_blocked_long` (the hold and wake cases above, injecting `live_specs`
    into `Ctx`, including not-live dependency, superseded form, two-dependency entry, empty,
    absent and mixed `blocked_by`), mirroring `test_classify_backlog`; add
    `test_waited_ids` (plain, superseded, multi-id and id-less entries); add
    `test_ctx_live_specs_reads_store` (a tmp store with one QUEUED, one IMPLEMENTING and one
    MERGED campaign: only the first two specs are live; the store is not scanned until the
    property is read); extend `test_ping_fingerprint_hold_and_change` with a blocked item on a
    live dependency (empty fingerprint, held ping), one on a non-live dependency and one
    non-benign (`["blocked", cid]`, wakes once, then held) and a legacy item with no
    `blocked_by` (counted); add a follow-through test that appends one in-flight-dependency, one
    no-campaign-dependency and one non-benign `backlog_blocked_long` through `E.append`, runs
    `E.follow` quiet against a store holding the in-flight dependency's campaign, and asserts the
    printed lines, the cursor, the `deliveries.jsonl` actions and the digest contents.
  - `tests/test_agentic_ping.py`: assert the watchdog still emits `backlog_blocked_long` and the
    ping still lists the blocked item (the change is delivery-side only).
  - `tests/test_skills.py::test_conduct_skill_quiet_rules_match_code`: unchanged, must pass
    with the updated skill list; add an assertion that the `backlog_blocked_long` table row
    mentions the campaign check.
- **Manual verification:** with a scratch data dir, queue spec B whose `depends_on` spec A has
  its own queued campaign, set `blocked_after_s` to 1, run `wt queue events --follow --for
  conductor` and confirm no `backlog_blocked_long` line appears while `wt queue events --type
  backlog_blocked_long` lists it; then abandon A's campaign (A stays `proposed`) and confirm the
  line appears for B; then point the dependency at a missing spec id and confirm it appears too.
- **Regression guard:** `uv run pytest` (full suite, including the spec and ledger drift
  tests) and `python3 tools/spec_lint.py` pass; `wt queue wakeups` over a replayed event log
  shows `backlog_blocked_long` under `hold` for in-flight dependency waits and under `wake` for
  the rest.

## Rollout / migration

No data migration. The follower re-reads `quiet.RULES` on start; a running conductor follower
exits on the `code_changed` line after this merges and picks up the new rule. Digests persisted
before the change stay valid (no schema change). A `last_ping_fp` persisted before the change
may include blocked items that are now filtered; the worst case is one extra wake on the first
ping after the upgrade.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

None.
