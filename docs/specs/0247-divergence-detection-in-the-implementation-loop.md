---
id: SPEC-0247
title: "Divergence detection in the implementation loop: park diverging, conductor triages, rework as an outcome"
status: in-progress
owner: user
created: 2026-10-25
updated: 2026-10-26
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0169
tags: [agentic, queue, review, triage, conductor]
source_idea: IDEA-089
depends_on: [SPEC-0203, SPEC-0222, SPEC-0232]
---

## Context

Child of epic [SPEC-0169](./0169-mini-agentic-workflow-queue-campaigns-headless.md), promoted
from `IDEA-089` (user request, 2026-10-25).

SPEC-0211, 0214, 0215 and 0216 each took three or four spec-review rounds. Every round found
valid but new problems, each fix added surface for the next round, and the loop did not
converge. Only a human noticed. The implementation loop has the same shape: implement, then
gate or review, then a fix brief, then implement again. Today the only brake is
`counters.loops > loop_max` (`loop_exhausted`). It fires late (`loop_max` is 2 to 4,
depending on the profile), it says nothing about *why*, and the conductor's row for it
(`skills/wt-conduct/SKILL.md`) treats it as "resume, ask the steward, or escalate". Nothing
offers **rework**: discard the attempt and start over from a better spec. A loop that keeps
diverging usually means the spec is wrong.

Everything needed to see divergence is already in the campaign ledger:
- a `finding` event (persona, severity, title, `ac`) for each reviewer finding and each
  triager must-fix (`queue._record_run`);
- a `gate` event with per-gate `results` (name, `ok`);
- a `transition` to `IMPLEMENTING` with reason `review_fix` or `triage_fix` that closes each
  fix round.

This spec covers the **implementation loop**. The spec-review loop is single-round for
standalone reviews since SPEC-0230. The in-campaign spec loop (SPEC-0214) is not built yet.
It will feed the same detector once it is, and adds its own signals (verdict oscillation,
scope growth). That is a follow-up.

## Goals / Non-goals

**Goals**
- A pure, deterministic detector that turns a campaign's ledger into fix rounds and computes
  divergence signals from them.
- When it trips, the campaign parks with a new reason, `diverging`, before the next
  implementer run. The evidence goes in a `loop_divergence` ledger event.
- A resolution ladder with **rework** as a first-class outcome:
  1. the conductor dispatches a triage sub-agent that reads the evidence and picks one of
     continue (bounded), rework, or escalate;
  2. the conductor applies the pick;
  3. the human gets a SPEC-0232 escalation, for scope decisions (split or narrow) and for
     anything the triage cannot settle.

**Non-goals**
- Spec-loop signals (verdict oscillation, split suggested twice, AC count or length growth).
  These wait for SPEC-0214.
- A dedicated triager persona. The conductor's sub-agent is the triage rung, the same
  pattern as the `review_escalated` row. A persona can replace it once there is volume.
- Diff-size growth, which needs git, not the ledger. Per-test gate identity: gates report
  per gate, not per test.
- Automatic rework. The conductor does it with existing commands.

## Decision

New module `src/wt/agentic/divergence.py`:
- `rounds(records)` builds fix rounds from ledger records;
- `assess(rounds, settings)` returns the tripped signals, or None;
- `check(cfg, state, pending_events)` reads the ledger and calls both.

`queue._route_reviews` (review `fix`) and the triage `fix` branch of `queue._apply` call
`check` after `loops += 1` and before `finish("IMPLEMENTING", …)`. If it trips, they append a
`loop_divergence` event and `finish("PARKED", "diverging", park={…, "resume_to":
"IMPLEMENTING"})`, keeping the new `pending_brief`. The conductor's skill gets a `diverging`
row.

## Design

### Rounds (`divergence.rounds(records) -> list[Round]`)

- A **round** is the stretch of ledger between two fix transitions. A fix transition is a
  `transition` with `to == "IMPLEMENTING"` and `reason` in `{"review_fix", "triage_fix"}`.
  Round *k* holds the `finding` events and `gate` events after fix transition *k−1* (or
  after `open`) up to and including fix transition *k*. `check` appends the pending events of
  the current `_apply` (the fix not yet written) as the last round.
- `Round = {kind: "review"|"gate", findings: [Finding], failing_gates: frozenset[str]}`.
  `kind` comes from the closing reason. `failing_gates` are the `name`s of the results with
  `ok: false` in the round's last `gate` event.
- Only findings with `severity` in `{must_fix, major, minor}` count, and only those whose
  `persona` is a reviewer (`personas.REVIEWER_PERSONAS`), `review_triager` or
  `gate_triager`. `nit` and `info` are ignored, and so are the personas `implementer`,
  `human` and `close`. `implementer` is excluded because `queue._record_run` writes each
  implementer `blockers` entry as a `major` finding with the fixed title "implementer
  blocker". Two unrelated blockers would match each other and trip a false `recurrence`.
- **Waived findings** (SPEC-0206) remain as `finding` events. The ledger does not link a
  waiver id to its event, so they are not removed. Waivers are only allowed on `minor`/`nit`
  findings, and `trend` and `recurrence` count only `must_fix`/`major` (below). So a waived
  finding can affect only `churn`, which is intended: reviewers who keep raising new minors
  are part of the churn picture.
- **Reset point:** rounds that end before the latest `resume` event whose preceding park was
  `diverging` are dropped. A resumed campaign has to diverge again, on fresh rounds, before
  it trips again.

### Finding identity

`key(f)`: the lower-cased title with every run of non-alphanumerics collapsed to one space,
plus `f.ac` when present. Two findings **match** when their keys are equal, or when they have
the same `ac` (or both have none) and the Jaccard similarity of their title word sets
(words of 3+ letters) is at least `similarity` (default 0.6). When either word set is empty
(titles such as "UI" or "a/b"), only equal keys match. Persona is not part of the
identity: a test reviewer and a conformance reviewer raising the same defect is the same
defect.

### Signals (`assess(rounds, settings) -> dict | None`)

Signals are computed over the last `window` rounds (default 3). Nothing is assessed with
fewer than `min_rounds` rounds (default 2).
- **trend** (needs `window` rounds): the blocking count (`must_fix` + `major`) does not
  decrease from one round to the next across the last `window` rounds, and the last count
  is above 0. Evidence: the counts.
- **churn** (needs 3 rounds, because it compares rounds 2 and 3 of the window with the
  round before each): in each of the last two rounds, at least `churn_ratio` (default
  0.75) of the findings have no match in the round before, and the round has at least 2
  findings. Evidence: the ratios.
- **recurrence** (needs 3 rounds): a `must_fix`/`major` finding of the last round matches a
  `must_fix`/`major` finding of some earlier round *i*, and has no match in some round *j*
  with i < j < last (it was addressed, then came back). Evidence: the recurring titles with
  their round numbers.
- **gate_repeat** (needs `window` rounds): the last `window` rounds are all gate rounds
  (consecutive, with no review round between them), and the same gate name is in the
  `failing_gates` of each. Evidence: the gate names.

**Trip rule:** `recurrence` alone trips, and so do any two of the other three. `recurrence` is
the strongest evidence that fixes are not sticking. A single trend or churn reading is common
in a loop that is healthy.

`trend` and `gate_repeat` are correlated: each failed gate round also carries the gate
triager's `must_fix`, so a gate that fails three times in a row with no drop in blocking
count fires both. That pairing tripping is **intended**. Three consecutive triage-fix
rounds on the same failing gate, with nothing getting better, is a stuck loop. Because
both signals need `window` (3) rounds, a gate that needs a second or third attempt never
trips. Only the third failed fix in a row can trip, and that is the point where today's
`thorough` profile still has a loop left and `balanced` is one loop from
`loop_exhausted`. Result: `{"signals": [{"name", "evidence"}], "rounds": n,
"loop": counters.loops}`.

### Settings

`agentic.divergence`: `{enabled: true, window: 3, min_rounds: 2, churn_ratio: 0.75,
similarity: 0.6}`. `divergence.settings(cfg)` merges these over the defaults. A key whose
value has the wrong type or is out of range falls back to its default:
- `window` and `min_rounds` must be ints ≥ 2;
- `churn_ratio` and `similarity` must be numbers in (0, 1].
`enabled: false` disables the check. Profiles do not override it.

### Queue integration (`queue.py`, `schemas.py`)

- `schemas.EVENT_KEYS["loop_divergence"] = ("loop", "signals", "rounds")`.
- `"diverging"` joins `PARK_REASONS`; `park_owner("diverging")` is `conductor` (the SPEC-0222
  default, no table entry needed).
- In `_route_reviews`, `fix` branch, after `_merge_carry`, `pending_brief` is built and
  `counters["loops"] += 1`. In `_apply`, triage `fix` branch, after `pending_brief` is
  built. Both then call `div = DV.check(ctx.cfg, state, events)`. When `div` is not None:
  append `("loop_divergence", div)` and return `finish("PARKED", "diverging",
  park={"reason": "diverging", "detail": <summary>, "resume_to": "IMPLEMENTING"})`.
  `<summary>` is one line, for example `recurrence: "x" (r1, r3) · trend 3→3→4`.
  `pending_brief` stays set, so a resume continues with the brief.
- The `loop_exhausted` check comes first. When the loop budget is already spent, the park is
  `loop_exhausted`, as today.
- `check` never raises out of `_apply`. It catches `OSError`/`ValueError` while reading the
  ledger, adds a `note` (`divergence check skipped: <e>`), and returns None.

### Conductor row (`skills/wt-conduct/SKILL.md`)

A new decision-table row for `parked diverging`:
1. Dispatch a fresh sub-agent with the `loop_divergence` evidence, the last `pending_brief`,
   the spec and the diff. It recommends one of:
   - **continue**: the findings are converging despite the signal. Apply with `wt queue
     resume <id>`. This is bounded: the reset point means a second trip needs fresh rounds.
   - **rework**: the spec is the problem. Apply with `wt queue abandon <id>`, then
     `/wt-rework` to supersede the spec. The brief for the new spec is the accumulated
     findings as constraints, what was learned, and a simpler scope. Then queue the new
     spec.
   - **escalate**: a scope decision (split or narrow what gets built) or no clear pick.
     Apply with `wt queue escalate <id> --question … --option continue --option rework
     --option split --recommend N --fact <evidence> --yes`. Without `--yes`, SPEC-0232's
     command is a dry run.
2. Record the decision as a campaign note.

## Alternatives considered

- **Lower `loop_max`** — it stops healthy long loops and diverging short ones alike, and still
  says nothing about why.
- **An LLM judges every round** — costly on every loop, and not reproducible. Deterministic
  signals gate the expensive judgement to the rare case.
- **Force through (keep looping until `loop_exhausted`)** — the user explicitly does not want
  that. Divergence wastes the remaining budget.
- **Trip on any single signal** — trend and churn alone fire in healthy loops (a reviewer
  finding new minor issues after a big fix). It would park too often.

## Acceptance criteria

- [x] `rounds()` on a hand-built ledger yields one round per `review_fix`/`triage_fix`
      transition plus the pending one, with the right `kind`, counted findings (`nit`,
      `info`, and `implementer`, `human` and `close` findings excluded) and
      `failing_gates`. Rounds before the
      latest resume from a `diverging` park are dropped.
- [x] Finding matching: equal normalised keys match; titles at or above the Jaccard threshold
      with the same `ac` match; different `ac`s never match; titles with empty word sets
      match only on equal keys.
- [x] `trend`, `churn`, `recurrence` and `gate_repeat` each fire on a minimal fixture (at
      their stated minimum round counts) and stay silent on a converging one (blocking
      counts 4→2→0, findings repeated rather than new). Two implementer blockers in
      rounds 1 and 3 do not fire `recurrence`. Two consecutive failed gate rounds fire
      neither `trend` nor `gate_repeat`; three consecutive rounds with the same gate
      failing and a steady blocking count fire both, and trip.
- [x] The trip rule: recurrence alone trips; any two of the other three trip; one of them
      alone does not; fewer than `min_rounds` rounds, or `enabled: false`, never trip.
- [x] `divergence.settings` falls back to the default for each wrong-typed or out-of-range
      key.
- [x] A FakeRunner campaign under a profile with `loop_max` 4 parks `diverging` at the end
      of loop 3, before `loop_exhausted`. Its reviewer returns blocking finding X in loop
      1, a different, non-matching blocking finding Y in loop 2 (so the loop continues),
      and X again in loop 3. It has a `loop_divergence` event (signals include `recurrence`),
      `park.resume_to` `IMPLEMENTING`, `pending_brief` set, and `park_owner` `conductor`.
- [x] After `wt queue resume`, the next implementer run happens, and the campaign does not
      re-park `diverging` on the next fix round, which has fewer than `min_rounds` fresh
      rounds.
- [x] A ledger read error inside `check` yields a `divergence check skipped` note and the
      normal `IMPLEMENTING` transition.
- [x] `skills/wt-conduct/SKILL.md` has the `diverging` row (continue / rework / escalate with
      the commands above). A new check in `tests/test_skills.py` asserts the row, including
      `--yes` on the escalate command; the existing skill tests pass.
- [ ] `uv run pytest` passes; `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests:** `tests/test_agentic_divergence.py`.
  - Pure unit tests for `rounds`, matching, the four signals, the trip rule and settings, on
    hand-built ledger record lists built with `schemas.with_defaults`.
  - Queue integration with the FakeRunner from `test_agentic_e2e` (a scripted review
    sequence with a recurring finding; a triage-fix sequence with the same failing gate for
    `gate_repeat`), then resume.
  - The read-error case by monkeypatching `store.read_ledger` to raise `OSError`.
- **Manual verification:** on the next real campaign that parks `diverging`, check that the
  evidence matches the reviewer findings and that the conductor row is followed. Note the
  result here.
- **Regression guard:** full `uv run pytest`. The existing loop and triage tests
  (`test_agentic_review_triage.py`, `test_agentic_queue.py`) must pass unchanged, because
  their scripted loops converge or exhaust without recurrence.

## Rollout / migration

On by default. If it misfires, `agentic.divergence.enabled: false` turns it off without a
code change. Thresholds are tuned from the `loop_divergence` events that real campaigns
record.

### Test record (2026-10-26)

- `uv run pytest tests/test_agentic_divergence.py tests/test_skills.py` — 77 passed: unit
  tests for `rounds` (kinds, counted findings, failing gates, the reset after a `diverging`
  resume), matching, the four signals and their minimum round counts, the trip rule, settings
  fallbacks; FakeRunner campaigns (recurrence parks `diverging` at the end of loop 3 under
  `loop_max` 4, then resume merges without a re-park; `loop_exhausted` wins when the budget is
  spent; `enabled: false`; gate-repeat on the third failed fix; a ledger read error).
- Full `uv run pytest -q`: 2695 passed, 3 failed, 137 errors in 549s. The run hit
  "No space left on device" (the /tmp volume ran out of inodes), so the errors and the 3
  failures could not be told apart from that. An earlier `-x` run failed once in
  `tests/test_agentic_adopt.py::test_adopt_after_park_resume_while_no_runner` (process-adoption
  timing, nothing to do with divergence); that file passes alone (37 passed). Not yet re-run
  on a volume with free inodes, so the full suite is **unconfirmed**.
- `python3 tools/spec_lint.py --write-ledger && python3 tools/spec_lint.py` exited 0 while the
  spec said `done`; the spec is back at `in-progress`, so the ledger needs regenerating.
- Manual verification (next real campaign that parks `diverging`) is pending by design.

### As shipped (2026-10-26)

- `divergence.check(cfg, state, pending_events, kind=None)` takes an optional `kind`
  (`"review"` or `"gate"`); `queue._finish_fix` passes it, so the pending round's kind does
  not depend on the campaign's state name. Without it a campaign in `TRIAGING` closes a gate
  round.
- Both fix branches share one helper, `queue._finish_fix`, instead of two inline calls.
- `assess` returns `{signals, rounds}`; `check` adds `loop`.
- Records up to the resume of a `diverging` park (also one escalated to `human_decision`
  with `escalated_from: diverging`) are dropped before rounds are cut, because the tripped
  round never gets a closing fix transition.

## Definition of done

- [x] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [x] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

(none)
