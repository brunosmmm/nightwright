---
id: SPEC-0306
title: "Quiet follower holds `backlog_blocked_long` (and its ping item) while the blocker is an ordinary dependency still in flight"
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
- `backlog_blocked_long` is held when every `blocked_by` entry is an ordinary dependency wait,
  exactly the test `backlog` already uses, and it wakes for anything else (a missing or
  rejected/superseded/done spec, a missing branch, an unknown idea, an empty `blocked_by`).
- A `conductor_ping` does not count an ordinary-dependency-blocked item as actionable, so the
  same item cannot wake the conductor through the ping.
- A held item stays visible: it appears in the `conductor_digest` like any held event.

**Non-goals**
- Changing the watchdog threshold (`blocked_after_s`), what `events.sync` appends, or the
  contents of the `backlog_blocked_long` event or of the ping.
- Detecting a blocker that is itself stuck. A parked, stuck, silent or dead blocker already
  raises its own wake events (`parked`, `state_stuck`, `campaign_silent`, `runner_dead`), so the
  blocked item needs no separate alarm.
- Changing which `backlog` texts count as benign (SPEC-0300 stands).

## Decision

`backlog_blocked_long` becomes a conditional type (`"cond"`) in `quiet.RULES`. `classify` holds
it when `blocked_by` is non-empty and every entry passes the same benign-prefix test as
`backlog` (a shared helper, so the two cannot drift), and wakes it otherwise. The ping
fingerprint skips `blocked` items whose `blocked_by` passes that test. Everything else about the
quiet follower is unchanged, and unknown event types still wake.

## Design

- `src/wt/agentic/quiet.py`:
  - New `dependency_wait(blocked_by) -> bool`: true when `blocked_by` is a non-empty list and
    every entry is a string starting with one of `_BENIGN_BLOCKED`. `backlog_resolved` becomes
    `bool(data.get("ready")) or all-benign` where the all-benign part is the existing test (an
    empty `blocked_by` on a non-ready `backlog` event keeps its current verdict: held); the
    prefix test itself lives in one helper used by both.
  - `RULES["backlog_blocked_long"]` changes from `"wake"` to `"cond"`.
  - `classify`: `if t == "backlog_blocked_long": return "hold" if dependency_wait(data.get(
    "blocked_by")) else "wake"`. A missing or non-list `blocked_by` wakes.
  - `ping_fingerprint`: the `blocked` category skips an item when `dependency_wait(
    it.get("blocked_by"))`; the other categories are unchanged. An item without a `blocked_by`
    key (older ping payloads) is counted, so the failure mode is an extra wake, never a miss.
- Held `backlog_blocked_long` events flow through the existing digest path
  (`Digest.add` and `campaigns["backlog_blocked_long"]`), so the digest line names the
  campaigns and counts them under `held`. No new digest fields.
- `skills/wt-conduct/SKILL.md`: the *Quiet stream* "Held while a fact says so" list gains
  `backlog_blocked_long` (only an ordinary dependency wait), in the same position style as
  `backlog`. The `backlog_blocked_long` table row notes that it arrives only when the blocker
  is not an ordinary dependency wait, and that a digest listing it needs no action unless the
  blocker is itself parked or stuck. `tests/test_skills.py::test_conduct_skill_quiet_rules_match_code`
  compares this list with `quiet.RULES`.
- Docs: `docs/product/capabilities/agentic.md` (the watchdog paragraph at line 470) gets one
  sentence that the quiet conductor stream holds a dependency-wait `backlog_blocked_long`.
  SPEC-0300 and SPEC-0212 are not edited (their text is history); this spec records the change.

## Alternatives considered

- **Keep `backlog_blocked_long` as an always-wake and rely on the decision-table "wait"
  outcome.** That is today's behavior and the bug: the table's own outcome for the common case
  is to do nothing.
- **Make it an always-hold type.** An item blocked on a missing or terminal spec can never
  become ready; the conductor must hear about it. Reusing the `backlog` benign test keeps that.
- **Raise `blocked_after_s` for dependency waits.** Changes the watchdog for every reader (TUI,
  stewards) and still wakes the conductor, only later.
- **Hold the event but leave the ping alone.** The ping would wake the conductor once for the
  same item, so the leak would move rather than close.

## Acceptance criteria

- [ ] `quiet.RULES["backlog_blocked_long"] == "cond"`, and `classify` returns `"hold"` for a
      `backlog_blocked_long` event whose `blocked_by` is `["waiting on SPEC-0001 (in-progress)"]`
      or `["spec status is draft"]` / `proposed` / `in-progress`.
- [ ] `classify` returns `"wake"` for a `backlog_blocked_long` event whose `blocked_by` contains
      `"SPEC-0999 not found"`, `"spec status is rejected"` (also `superseded`, `done`, `missing`),
      `"branch x missing"`, `"idea IDEA-1 not found"`, a mix of one benign and one non-benign
      entry, is empty, or is absent.
- [ ] `ping_fingerprint` omits a `blocked` item whose `blocked_by` is only ordinary dependency
      waits, keeps one with any other text and one without a `blocked_by` key, and leaves the
      other categories unchanged. A ping whose only actionable content is such an item is held.
- [ ] End to end through `events.follow` in quiet mode, a held `backlog_blocked_long` prints no
      line, advances the cursor, is recorded as `hold`, and appears under `held` and `campaigns`
      in the next `conductor_digest`. A waking one prints and is recorded as `wake`.
- [ ] `events.jsonl`, `wt queue events` without `--follow`, and `wt queue events --follow --all`
      still show every `backlog_blocked_long` event, unchanged.
- [ ] The skill's *Quiet stream* "Held while a fact says so" list names `backlog_blocked_long`
      and `test_conduct_skill_quiet_rules_match_code` passes.
- [ ] `tests/test_agentic_quiet.py` no longer lists `backlog_blocked_long` among the unconditional
      WAKE types, and all existing quiet, events and ping tests pass.

## Test plan

- **Automated tests:**
  - `tests/test_agentic_quiet.py`: remove `backlog_blocked_long` from the `WAKE` list; add
    `test_classify_backlog_blocked_long` (the hold and wake cases above, including empty, absent
    and mixed `blocked_by`), mirroring `test_classify_backlog`; extend
    `test_ping_fingerprint_hold_and_change` with a benign-blocked item (empty fingerprint, held
    ping), a non-benign one (`["blocked", cid]`, wakes once, then held) and a legacy item with
    no `blocked_by` (counted); add a follow-through test that appends one benign and one
    non-benign `backlog_blocked_long` through `E.append`, runs `E.follow` quiet, and asserts the
    printed lines, the cursor, the `deliveries.jsonl` actions and the digest contents.
  - `tests/test_agentic_ping.py`: assert the watchdog still emits `backlog_blocked_long` and the
    ping still lists the blocked item (the change is delivery-side only).
  - `tests/test_skills.py::test_conduct_skill_quiet_rules_match_code`: unchanged, must pass
    with the updated skill list.
- **Manual verification:** with a scratch data dir, queue a spec whose `depends_on` spec is
  `in-progress`, set `blocked_after_s` to 1, run `wt queue events --follow --for conductor`
  and confirm no `backlog_blocked_long` line appears while `wt queue events --type
  backlog_blocked_long` lists it; then point the dependency at a missing spec id and confirm
  the line appears.
- **Regression guard:** `uv run pytest` (full suite, including the spec and ledger drift
  tests) and `python3 tools/spec_lint.py` pass; `wt queue wakeups` over a replayed event log
  shows `backlog_blocked_long` under `hold` for dependency waits.

## Rollout / migration

No data migration. The follower re-reads `quiet.RULES` on start; a running conductor follower
exits on the `code_changed` line after this merges and picks up the new rule. Digests persisted
before the change stay valid (no schema change).

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

None.
