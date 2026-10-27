---
id: SPEC-0261
title: "Divergence detector: skip environmental gate rounds and weigh repeated findings over new ones"
status: accepted
owner: user
created: 2026-10-26
updated: 2026-10-26
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0169
tags: [agentic, queue, divergence, review]
source_idea: IDEA-346
depends_on: [SPEC-0247]
---

## Context

Promoted from bug `IDEA-346`. The idea has no Summary; its Log entry and the campaign ledger
are the evidence.

[SPEC-0247](./0247-divergence-detection-in-the-implementation-loop.md) parks a campaign as
`diverging` when its fix rounds show no convergence. `divergence.rounds` cuts the ledger into
rounds. `divergence.assess` trips on `recurrence` alone, or on any two of `trend`, `churn`
and `gate_repeat`.

Campaign `SPEC-0251-20261025-92e4` parked `diverging` with `trend 0→1→1 · churn 100%/89%`
(ledger seq 152–153). The conductor triaged it and resumed it as healthy. Replaying its
ledger (`wt campaign log`) shows two defects in the detector:

1. **Environmental rounds count as fix rounds.** Gates 2–5 failed because `/tmp` was full
   (ENOSPC). In round 3 (closed by `triage_fix` at seq 70), implementer run 008 reported
   `status: blocked` with the blocker "Disk full (ENOSPC) on the volume holding /tmp…". The
   round says nothing about whether the code converges. It still fills a slot in the
   3-round window. (On this ledger r2 is also a gate round with no counted findings, so
   dropping r3 alone does not change the trend window; Decisions 2 and 3 are what clear this
   replay. Decision 1 is justified by the round's content, and is covered by its own test.)
2. **New blockers weigh the same as repeated ones.** Rounds 4 and 5 each had exactly one
   blocking finding, and each was a *different, new* one: test_reviewer "`wt tui --tab
   events` CLI choice is not tested", then adversarial_reviewer "Event text is put into the
   DataTable as a raw str…". Each round fixed the previous blocker. The adversarial reviewer
   finding a fresh edge case each round is a converging loop, but `trend` (non-decreasing
   blocking count with last > 0) reads it as divergence. `churn` measures the share of *all*
   counted findings (minors included) that are new. A reviewer that keeps raising new minors
   produces high churn by itself. SPEC-0247 chose that on purpose (its "Waived findings"
   bullet), and this case shows it is the wrong choice.

Code today (verified in `src/wt/agentic/divergence.py`):
- `rounds(records)` keeps `finding` events, but only from the reviewer, `review_triager` and
  `gate_triager` personas, and only those with severity `must_fix`/`major`/`minor`. It also
  keeps the round's last `gate`. Implementer findings are dropped (`COUNTED_PERSONAS`), so a
  round's environmental blocker is invisible today.
- `check(cfg, state, pending_events, kind)` reads the ledger types `finding, gate,
  transition, park, resume`. It does not read `run.end`.
- `_trend` uses `len(r.blocking())`. `_churn` uses `cur.findings` (all counted severities)
  and needs at least 2 of them in each of the last two rounds.
- `events._is_transient(text)` and `_TRANSIENT_MARKERS` already classify transient run
  failures (rate limits, overload, 429/529) for the `run_failed` event.

## Goals / Non-goals

**Goals**
- A gate round whose failure is explained by an environmental cause (disk full, quota,
  read-only filesystem), or whose implementer run crashed or was rate-limited so the gated
  tree is incomplete, is left out of the divergence assessment.
- `trend` and `churn` treat a loop that fixes each blocker while reviewers keep finding
  *different* ones as converging. A blocker that persists, or a pile of new blockers, still
  counts.
- Replaying the SPEC-0251 ledger does not trip. Every existing true-positive scenario in
  `tests/test_agentic_divergence.py` still trips: the e2e recurrence park, and gate_repeat
  with trend on the third failed fix.

**Non-goals**
- `recurrence` and `gate_repeat`, and the trip rule, are unchanged.
- No new settings keys. `agentic.divergence` keeps its five keys. The weights below are
  module constants, to be tuned from evidence later.
- Detecting environmental failure from gate output in general. Only the explicit markers
  below count.
- The queue integration (`queue._finish_fix`), the park reason and the conductor row are
  unchanged.

## Decision

1. **Environmental gate rounds are dropped.** `Round` gains `environmental: bool`.
   `rounds()` sets it when, inside that round, any of these holds:
   - (a) an `implementer` `run.end` has `verdict.status == "blocked"`, and one of its
     `blockers` (or its `summary`) matches an `ENV_MARKERS` entry;
   - (b) an `implementer` `run.end` (the run that produced the gated code) has `is_error` or
     `timed_out` set, and `events._is_transient(result_tail)` is true. Other personas'
     failed runs (for example a rate-limited `gate_triager` that is retried) say nothing
     about why the gate failed and do not count;
   - (c) a failing required gate in the round's last `gate` has a `tail` that matches an
     `ENV_MARKERS` entry. (c) never uses `_is_transient`: its bare "429", "529" and
     "overloaded" substrings would match `tests/x.py:429: AssertionError` or `529 passed, 1
     failed` and silently suppress `gate_repeat` and `trend`.

   `assess` drops rounds that are both `kind == "gate"` and `environmental` before any
   signal or `min_rounds` check. Review rounds are never dropped: their gate was green, so
   the reviewers' findings are real even if an implementer run in that round hit the
   environment. `assess` then works on the kept rounds only: `rounds` in the result is
   `len(kept)`, and every round number in recurrence evidence and in the park summary
   (`r1`, `r3`) is a 1-based index into the kept rounds, not the ledger round. The summary
   notes `excluded_rounds` when it is non-zero.
2. **Repeated blockers weigh more than new ones in `trend`.** Each kept round gets a score:
   - each blocking finding that matches (`matches`, any counted severity) a finding of an
     *earlier kept* round adds `REPEAT_WEIGHT = 1.0`;
   - each new blocking finding adds `NEW_WEIGHT = 0.5`.

   `trend` trips when the scores of the last `window` rounds do not decrease and the last
   score is at least `1.0`. So "one persisting blocker" or "two new blockers per round"
   still trends, and "one new blocker per round" does not.
3. **`churn` counts blocking findings only.** In each of the last two kept rounds, at least
   `churn_ratio` of the round's blocking findings must have no match among the previous kept
   round's counted findings, and the round needs at least 2 blocking findings. A minor-only
   stream of fresh findings no longer produces churn.
4. `check` also reads `run.end`. The `loop_divergence` payload gains `excluded_rounds`, the
   count of dropped environmental rounds. This is additive: `schemas.EVENT_KEYS` lists
   required keys only.

Like SPEC-0139 and SPEC-0206, this spec amends SPEC-0247's signal definitions ("Signals",
and the "Waived findings" rationale for churn). It does not supersede the rest: rounds,
identity, recurrence, gate_repeat, trip rule, settings and queue integration.

## Design

### Markers (`divergence.py`)

`ENV_MARKERS = ("enospc", "no space left", "disk full", "errno 28", "edquot", "disk quota",
"out of inodes", "erofs", "read-only file system")` (disk and filesystem only). Matching
lower-cases the text and does a substring test. The helper `_environmental(text) -> bool` is
true when any `ENV_MARKERS` entry matches; it is used by (a) and (c). Rule (b) calls
`events._is_transient(text)` directly. The import is at module level as
`from .events import _is_transient`. `events` imports `queue` only lazily, so this creates
no import cycle.

### `rounds(records)`

- `Round` gains a trailing field `environmental: bool = False`, so the existing positional
  `Round(kind, findings, gates)` calls keep working.
- In the loop, an implementer `run.end` record sets a local `env` flag when (a) or (b) holds. The checks
  are defensive: `verdict` must be a dict, blockers that are not strings are `str()`-ed, and
  a missing `result_tail` falls back to `"timed out"`/`"error"` the way
  `events._project_ledger_event` does.
- At the fix transition, (c) is computed over the same required failing results used for
  `failing_gates`. The round is built with `environmental=env or gate_env`, and `env` is
  reset with `findings` and `gate`.

### `assess(rs, settings)`

`kept = [r for r in rs if not (r.kind == "gate" and r.environmental)]`, then the existing
`min_rounds`, recurrence, trend, churn and gate_repeat logic runs on `kept`. The result
adds `"excluded_rounds": len(rs) - len(kept)`, and `"rounds"` is `len(kept)` (the e2e test's
`div["rounds"] == 3` is unaffected when nothing is excluded).

### `_scores(rs, similarity) -> list[float]` (new)

For each round *i*, sum over `rs[i].blocking()`: `REPEAT_WEIGHT` if the finding
`_any_match`es any finding in `rs[0..i-1]` (all counted severities), else `NEW_WEIGHT`.

### `_trend(rs, window, similarity)`

The signature gains `similarity`. It uses `_scores(rs, similarity)[-window:]`. It trips when
the scores do not decrease and `scores[-1] >= 1.0`. Evidence is `{"counts": [raw blocking
counts], "scores": [scores]}`. `summary` renders `trend` from `scores` with `:g`, for
example `trend 0.5→1→1`.

### `_churn(rs, ratio, similarity)`

The structure is unchanged, but it uses `cur.blocking()` in place of `cur.findings`, both for
the `< 2` guard and for the ratio numerator and denominator. Matching is still against
`prev.findings`, so a blocker that was raised earlier as a minor is not new.

### `check(...)`

`types=["finding", "gate", "transition", "park", "resume", "run.end"]`. The rest is
unchanged.

## Alternatives considered

- **Drop any round with an implementer blocker.** Too broad. A review round with real
  reviewer findings would vanish just because an implementer run in it hit `/tmp`. Only gate
  rounds are dropped.
- **Treat a `retry_gate` triage (the triager judged the failure flaky) as environmental.**
  Not chosen. In SPEC-0251 round 2 the triager retried and then asked for a fix anyway.
  `retry_gate` is too weak a signal, and a flaky test the triager then asks to fix is a code
  problem.
- **Count only repeated blockers in trend** (new weight 0). Not chosen: a loop that adds two
  new blockers every round would never trend. 0.5 keeps "two new per round" visible.
- **A settings key for the weights.** Deferred. The defaults are justified by one case.
  Making them config before more evidence would invite tuning with nothing to tune against.
- **Keep minors in churn but weight them.** Not chosen: a weighted ratio is harder to read in
  evidence, and minors are exactly what the review triager may waive.

## Acceptance criteria

- [ ] `Round.environmental` exists, defaults to False, and is set by `rounds()` under rules
      (a), (b) and (c); (c) does not fire on a gate tail containing "429" or "529 passed",
      and (b) does not fire for a non-implementer run. Implementer blocker findings are still not counted as findings.
- [ ] `assess` drops environmental gate rounds before every check, keeps environmental
      review rounds, and reports `excluded_rounds`.
- [ ] `trend` trips on weighted scores: one new blocker per round over 3 rounds does not
      trip; one repeated blocker (scores 0.5→1→1) does; two new blockers per round
      (1→1→1) do.
- [ ] `churn` counts blocking findings only, and three rounds of fresh minors alone do not
      produce churn.
- [ ] The SPEC-0251 replay ledger (below) gives `assess(...) is None`. The same ledger
      without the ENOSPC blocker in round 3 also gives None, because Decisions 2 and 3 alone
      clear it (the exclusion is covered by its own test, not by the replay).
- [ ] `test_e2e_recurrence_parks_diverging_then_resume_continues` and
      `test_e2e_gate_repeat_trips_on_the_third_failed_fix` pass unchanged.
- [ ] `uv run pytest` passes, and `python3 tools/spec_lint.py` exits 0.

## Test plan

In `tests/test_agentic_divergence.py`:

- `test_rounds_marks_environmental_gate_round`. Three ledgers, one per rule:
  - (a) an implementer `run.end` with `verdict={"status": "blocked", "blockers": ["Disk
    full (ENOSPC) on /tmp"]}`;
  - (b) an `implementer` `run.end` with `is_error=True, result_tail="rate limit reached"`;
  - (c) a failing required gate whose tail is `OSError: [Errno 28] No space left on device`.

  Each sets `environmental` on its round. These do not: a blocked verdict without markers
  ("spec is ambiguous"); an optional failing gate with an ENOSPC tail; a `gate_triager`
  `run.end` with `is_error=True, result_tail="rate limit reached"`; a failing required gate
  whose tail contains `tests/x.py:429: AssertionError` or `529 passed, 1 failed`.
- `test_assess_drops_environmental_gate_rounds_only`:
  - (the `gr()`/`rnd()` helpers gain an `environmental=False` keyword passed to `DV.Round`)
  - three gate rounds `gr()` (same failing gate, same triager title), the middle one
    environmental: no trip, `excluded_rounds == 1` in the non-None case (checked with a
    4-round variant that does trip);
  - an environmental *review* round is kept, so `len` is unchanged.
- `test_trend_weighs_repeated_over_new`: `[a]`,`[b]`,`[c]` (all new, 0.5 each) gives no
  trend. `["x"]*3` (repeated) gives a trend. Two new per round gives a trend. Evidence carries
  `counts` and `scores`.
- `test_churn_counts_blocking_only`: fresh minors ×3 gives no churn. Fresh blocking pairs ×3
  give churn. A blocker that matches a previous round's minor is not new.
- `test_spec0251_replay_does_not_trip`. A hand-built ledger that mirrors seq 5–150 of
  `SPEC-0251-20261025-92e4`. Titles are paraphrased, but the severities, personas,
  transitions, gates and the implementer 008 ENOSPC `run.end` match the real ledger:
  - r1: review, 1 major and minors;
  - r2: gate, failing pytest, no findings;
  - r3: gate, failing pytest, ENOSPC blocker;
  - r4: review, 1 new major and minors;
  - r5 (pending through `check`'s path, built via `DV.rounds` + an appended fix transition):
    1 new major and minors.

  Asserts `DV.assess(DV.rounds(ledger)) is None`. The variant with the r3 `run.end` removed
  also gives None.
- Existing tests are updated where this spec changes the expectations, and the changes are
  noted in the Test record:
  - `test_trend`: the `[a],[b],[c]` assertion flips, and so does
    `"trend" not in sigs([rnd(blocking=list("ab")), rnd(blocking=["a"]), rnd(blocking=list("ab"))])`
    (scores 1.0, 1.0, 2.0 are non-decreasing with last >= 1). This is intended: a count that
    dipped and came back with the same blockers is a persisting blocker and should trend;
  - `test_churn`: the minors assertion flips;
  - `test_trip_rule`: `only_churn` uses blocking findings that decrease 4→3→2, all new.

  Every other existing test passes unchanged.
- Regression: the full `tests/test_agentic_divergence.py`, `tests/test_agentic_queue.py` and
  `uv run pytest`.

**Sabotage check:**
1. Make `assess` keep environmental rounds (`kept = rs`). `test_spec0251_replay_does_not_trip`
   must still pass on the weighting alone, as stated in the acceptance criteria, and
   `test_assess_drops_environmental_gate_rounds_only` must fail.
2. Set `NEW_WEIGHT = 1.0`. `test_trend_weighs_repeated_over_new` must fail. (The replay
   cannot detect this: churn and gate_repeat do not fire there, so a lone trend signal gives
   None.)
3. Revert `_churn` to `cur.findings`. `test_churn_counts_blocking_only` must fail.
4. Let (c) use `_is_transient`. The "429" gate-tail negative case must fail.

Restore both and record the failures in the Test record.

## Rollout / migration

None. The detector reads existing ledger events. A campaign parked `diverging` before this
change keeps its park, and its resume uses the existing reset point.

## Definition of done

- [ ] Every acceptance criterion is checked and true.
- [ ] The Test plan has been executed, including the sabotage check, and the results are in
      a `### Test record`.
- [ ] `uv run pytest` passes with no regressions.
- [ ] The spec body reflects what shipped (`### As shipped`), the ledger is regenerated and
      spec lint exits 0.

## Open questions

(none)
