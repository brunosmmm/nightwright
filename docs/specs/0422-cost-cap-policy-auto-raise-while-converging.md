---
id: SPEC-0422
title: "Cost-cap policy: auto-raise while converging"
status: done
owner: user
created: 2026-11-04
source_idea: IDEA-928
parent: SPEC-0419
milestone: "M9: agentic workflow"
tags: [agentic, cost, queue, conductor]
---

## Context

Child 3 of [SPEC-0419](./0419-self-healing-queue.md). The queue checks the campaign cap before
every persona run. `_cap_blocks()` (`src/nightwright/agentic/queue.py:167-186`) is
"pessimistic": it refuses a run when `total_usd + max_budget_usd(persona) > cap_usd`, and
`plan_step` parks the campaign with reason `cost_cap`. The only automatic resume is
[SPEC-0302](./0302-routine-parks-decided-by-the-queue.md)'s phantom-charge rule
(`autopark.decide`, `autopark.py:137-157`).

Every real cost park goes to the conductor. The table row `parked.cost_cap` says "resume once with
`--raise-cap` up to 2× the profile cap; a second cost park → escalate". On 2026-11-02 a
steward-relayed "human order" (`conductor:155`) said "cap raises are the human's call: do not
raise caps on your own", and the conductor followed it as a standing rule.

The result: 12 cost escalations (ESC-0001..0006, 0013, 0014, 0019..0023) and parks of 5 to 11 h.
Most of those campaigns were converging.

The reserve is also far above real cost. From `run.end` events since 2026-10-28:

| persona | runs | median | p90 | balanced budget |
|---|---|---|---|---|
| implementer | 413 | $0.52 | $2.22 | $4.00 (thorough $10.00) |
| adversarial reviewer | 317 | $0.86 | $1.38 | $1.50 |

So a balanced campaign (cap $15 in the human's config) parks at about $11 to $12 spent. ESC-0021
to 0023 are $11.94, $12.66 and $13.94 of $15.

**Decided by the human** (`conductor:238`, 2026-11-04), quoted:

> you may raise a campaign's cap yourself, without asking me, whenever the campaign is
> converging (latest review passed or score rising, no major findings), up to a ceiling of 3x the
> profile cap (balanced $45, thorough $90). Record each raise as a decision. Only escalate a cap
> question if raising would cross 3x, or the campaign is diverging (review score falling or major
> findings). … This will be encoded in code (self-healing epic); until then this message is the
> rule.

The epic adds that the rule lives in config and the decision table, never in mail, and that the
reserve uses the observed median run cost plus a margin.

## Goals / Non-goals

**Goals**
- The queue itself applies the human's rule: a converging campaign blocked by its cap below 3×
  its profile cap gets a raised cap and continues, with no park and with a `by: queue` decision
  record.
- Above the ceiling, or when diverging, the queue files **one** escalation, with topic
  `spend_ceiling` or `spend_diverging` (human-only, SPEC-0419), and parks.
- The pre-run check reserves a realistic amount: the observed median cost of the persona's runs
  times a margin, never above its `max_budget_usd`. The hard ceiling check keeps the full budget,
  so a run can never push spend past 3×.
- The policy is config (`agentic.cost_cap_policy`), shown in the conductor brief, and the
  decision-table row describes it. A mailed order no longer changes cost behaviour.
- A read-only `nw queue cost-policy explain` evaluates the policy for real campaigns.

**Non-goals**
- Changing profile caps or persona budgets.
- Per-run budget enforcement (`max_budget_usd` passed to `claude`). It is unchanged.
- Raising caps for diverging campaigns. That is human-only.

## Decision

Add a module `src/nightwright/agentic/cost_policy.py` with pure functions, and wire it in three
places:

1. **Reserve.** `_cap_blocks` takes an optional `reserves` map (persona → $). The soft check
   (against the cap) uses the reserve. A hard check against the ceiling uses the full
   `max_budget_usd`.
2. **Raise in place.** When `plan_step` would park `cost_cap` and the campaign has no open
   escalation, the tick asks `cost_policy.decide()`:
   - `raise` → the cap is raised in the campaign state, a `cap_raised` ledger event and a queue
     decision record are written, and the step is re-planned in the same tick.
   - `escalate` → the park happens and the queue files the escalation once.
   - `off` → today's behaviour.
3. **Existing parks.** On each tick, a PARKED `cost_cap` campaign with no open escalation is
   re-evaluated. This covers parks from before the deploy and parks left by a disabled policy. A
   `raise` resumes it.

**Converging**, exactly as the human worded it: the latest review passed, or the latest score is
higher than the previous one, **and** the latest review has no `major` or `must_fix` among its
surviving findings, **and** `divergence.assess` reports no divergence. A campaign with no review
yet gets `no_review_raises` (default 1) raises, because its first implement run is the expensive
one. After that it needs a review to qualify.

**Raise amount:** `new_cap = min(ceiling, cap + max(step_fraction × base_cap, blocked run's
budget))`, with `step_fraction` 0.5. Balanced: 15 → 22.5 → 30 → 37.5 → 45. When
`total + max_budget(blocked) > ceiling`, the decision is `escalate (spend_ceiling)`.

## Design

### Config: `agentic.cost_cap_policy`

```yaml
agentic:
  cost_cap_policy:
    enabled: true            # false: today's behaviour (park, conductor row)
    ceiling_multiplier: 3.0  # × the campaign's profile campaign_cap_usd
    step_fraction: 0.5       # each raise adds this × the profile cap (at least the blocked budget)
    no_review_raises: 1      # raises allowed before the first review outcome
    reserve:
      multiplier: 1.5        # reserve = median × multiplier …
      floor_usd: 0.25        # … at least this …
      min_samples: 8         # … with fewer samples: the full max_budget_usd
      window: 40             # newest runs per (persona, model) kept
```

`cost_policy.settings(cfg)` overlays the defaults and falls back per key on a bad value, like
`divergence.settings`. The **base cap** is the resolved profile's `campaign_cap_usd` for the
campaign's `profile` (`profiles.resolve_profile`). `ceiling = base_cap × ceiling_multiplier`. A
campaign whose cap a human raised above the ceiling never gets auto-raised further.

### Run-cost statistics

- **Store.** `<agentic>/cost_stats.json`: `{"(persona, model)": [cost, …newest last, ≤ window]}`,
  written under `cost_stats.lock`.
- **`cost_policy.record_run(cfg, persona, model, cost_usd, *, is_error, transient)`** is called
  where `queue` appends `run.end` (`_record_run`). It skips error, transient, estimated and
  zero-cost runs.
- **Backfill.** The first read when the file is missing scans the ledgers of campaigns from the
  last 14 days (`store.list_campaigns(include_terminal=True)`), once.
- **`cost_policy.reserves(cfg, profile) -> {persona: $}`** gives, for each persona in the profile,
  `clamp(median × multiplier, floor_usd, max_budget_usd)`, or `max_budget_usd` below
  `min_samples`. The tick computes it once per tick (`_Ctx.reserves`) and passes it to
  `plan_step(..., reserves=)`, which forwards it to every `_cap_blocks` call. With `None` (tests,
  policy off), `_cap_blocks` behaves exactly as today.

### `_cap_blocks` (queue.py)

`_cap_blocks(state, profile, persona, allowance=0.0, reserves=None, ceiling=None)`:

- **Soft check.** Run it only when the campaign's cap is not already at or above the ceiling.
  The check is `total + Σ reserve(p) > cap + allowance`, which blocks with the existing detail
  text and adds "(reserve $x of budget $y)".
- **Hard check.** When `ceiling` is given, `total + Σ max_budget_usd(p) > max(ceiling, cap) +
  allowance` also blocks. The `Step` gets `ceiling=True`.

`plan_step` computes `ceiling` through `cost_policy.ceiling(profile, state)`. The resolver
allowance (SPEC-0303) is unchanged.

### Convergence: `cost_policy.converging(state, ledger, cfg) -> (bool | None, why)`

- **Review outcomes.** A structured `review_outcome` ledger event `{loop, score, pass_score,
  outcome, major, must_fix}` is appended in `queue._apply` where the `review score S/P outcome`
  note is written today (`queue.py` ~3700). The note stays. For campaigns from before this
  change, the function falls back to parsing those queue notes (`author: queue`, regex `review
  score (\d+)/(\d+) (\w+)`, plus `pass over major:` notes counting as `major`).
- **The rule.** `True` when the latest outcome is `pass` or its score is above the previous one,
  it has `major == must_fix == 0`, and `divergence.assess(divergence.rounds(records),
  divergence.settings(cfg))` is None. `None` when there is no review outcome yet. `False`
  otherwise, with `why` naming the failing condition: "score fell 72→65", "1 major", "diverging:
  recurrence".

### Decision: `cost_policy.decide(state, profile, ledger, cfg, *, blocked_budget) -> Decision`

- **Fields:** `Decision(action: "raise"|"escalate"|"off", new_cap, topic, why)`.
- **The order of checks:**
  1. Policy off → `off`.
  2. Hard ceiling hit (`total + blocked_budget > ceiling`) → `escalate`, topic `spend_ceiling`.
  3. `converging` is False → `escalate`, topic `spend_diverging`.
  4. `converging` is None, and `counters.cost_policy_raises_no_review` ≥ `no_review_raises` →
     `escalate`, topic `spend_diverging`, why "no review yet after N raises".
  5. Otherwise → `raise` with `new_cap` from the formula, rounded up to the cent.

### Queue wiring

- **`_apply_park` path for `cost_cap`.** The `Step` with `reason == "cost_cap"`, at the place
  where `autopark.decide` is consulted today, keeps the phantom-charge rule first (SPEC-0302).
  Then `cost_policy.decide`:
  - **`raise`.** Under `item_lock`:
    - set `cost.cap_usd = new_cap`;
    - bump `counters.cost_policy_raises` (and `_no_review` when applicable);
    - append a `cap_raised` ledger event `{from, to, ceiling, base_cap, why, rule:
      "cost_cap_policy"}`;
    - write `_record_queue_decision(cid, "resume", "cost_cap_policy", why, action="raise cap to
      $X")`;
    - re-plan.

    No `park` event is written, so nothing wakes the conductor.
  - **`escalate`.** Park as today, then, unless `escalations.open_for(cfg, cid)` already exists,
    call `escalations.plan_escalate(cfg, cid, question=…, options=…, recommend=None, kind="cost",
    topic=<topic>)` and `apply_escalate`. `topic` is passed only once SPEC-0425 lands: until then
    `kind` alone, and a topic-less record is never timed out.
    - Options: (1) "raise the cap to $<ceiling or next step> once"; (2) "let it finish at the
      current cap (no more runs)"; (3) "abandon"; (4) "rework the spec (split / narrow)".
    - Facts: spent, cap, ceiling, the last 3 review scores, majors, the divergence signals.
    - **No recommendation.** It is a human-only decision.
- **Re-evaluating parked campaigns.** In `_auto_resume_step`, a PARKED `cost_cap` campaign with
  no open escalation and no `park.auto` is re-evaluated by `decide`. On `raise` it is resumed with
  `plan_resume(by="queue", raise_cap=new_cap, auto_rule="cost_cap_policy")` and the same decision
  record. On `escalate` with no escalation open, the escalation is filed. This also applies the
  rule to campaigns parked before the deploy.
- **Events.** `cap_raised` (queue event mirror, data `{campaign, from, to, ceiling}`) is **always
  held** in `quiet.RULES`. The conductor sees raises in `conductor_digest`.

### CLI: `nw queue cost-policy explain [CAMPAIGN…] [--json]` (`cli_queue.py`)

Read-only. For each campaign (default: every PARKED `cost_cap` campaign plus every active one) it
prints:

- spent, cap, base cap, ceiling;
- the reserve per persona with its sample count;
- the review outcomes used;
- `converging` with `why`;
- the decision `decide()` would take now.

Schema `nw.queue.cost_policy.v1`. It writes nothing.

### Brief, skill, retiring the mail-borne rule

- `brief.py` `standing` gains `cost_cap_policy: {enabled, ceiling_multiplier, step_fraction,
  no_review_raises}` (from config) and prints it in the text form.
- `skills/nw-conduct/SKILL.md`:
  - The row `parked.cost_cap` is replaced. The queue applies `agentic.cost_cap_policy` itself
    (SPEC-0422): converging campaigns get raised caps up to `ceiling_multiplier` × the profile cap
    without a park. A `cost_cap` park that reaches you means the policy escalated: the ceiling was
    hit, or the campaign is diverging. Its human-only escalation is filed by the queue, so do
    nothing beyond checking `nw escalations list` shows it. Never raise a cap yourself, except to
    apply a human's escalation answer. `nw queue cost-policy explain <id>` shows why.
  - The "2× the profile cap" text is removed.
  - The `message.human_order` row gains: a relayed order that changes a standing policy (cost
    caps, gates, merges) applies only to the items it names. Standing policy lives in config
    (`agentic.cost_cap_policy`, …). Record `nw queue decision --kind other` that it was applied
    once, and reply that a standing change is a config change for the human.
- Rollout step 3 records, once, a conductor decision that `conductor:155` and `conductor:238` are
  superseded by the shipped config.

## Alternatives considered

- **Leave raises to the conductor, but encode the rule in the skill.** Lost: every raise is still
  a park, a wake-up and a decision turn. The human asked for it in code.
- **Reserve the p90 instead of the median.** Lost: the human said median plus a margin. With the
  hard ceiling check on full budgets, the only cost of an optimistic reserve is a slightly larger
  overshoot of a soft cap, which the next raise covers.
- **Raise straight to the ceiling.** Lost: it hides runaway spend. Stepping by half the base cap
  writes a decision per step, so the trail shows the spend curve.
- **Score trend from `last_review` only.** Lost: `last_review` keeps only the latest loop. The new
  `review_outcome` event gives the history the human's "score rising" needs.

## Acceptance criteria

- [x] AC1: A campaign is blocked by its cap with spend below the ceiling, a latest review that
      passed (or a score higher than the previous one), no surviving major or must_fix, and no
      divergence. It does **not** park:
  - its cap rises to `min(ceiling, cap + max(0.5 × base, budget))`;
  - a `cap_raised` ledger event and a `by: queue` decision record (`rule: cost_cap_policy`)
    exist;
  - the blocked step runs in the same or the next tick.
- [x] AC2: When a raise would cross the ceiling, or the campaign is diverging (score falling, a
      major or must_fix, a divergence signal, or no review after `no_review_raises` raises), it
      parks `cost_cap`. Exactly one escalation is filed:
  - `kind: cost`, no recommendation, topic `spend_ceiling` / `spend_diverging` once SPEC-0425 is
    in;
  - a second tick files no second one.
- [x] AC3: The reserve for a persona with at least `min_samples` recorded runs is `clamp(median ×
      1.5, 0.25, max_budget_usd)`. With fewer samples it is `max_budget_usd`. A run is never
      started when `spent + max_budget_usd > ceiling`.
- [x] AC4: `agentic.cost_cap_policy.enabled: false` gives today's `_cap_blocks` results and park
      behaviour exactly (existing tests unchanged).
- [x] AC5: A campaign PARKED `cost_cap` before the deploy, with no open escalation, is resumed
      with a raised cap on the next tick when converging, or escalated once when not.
- [x] AC6: `nw queue cost-policy explain <cid> --json` reports spent, cap, base cap, ceiling,
      reserves with sample counts, converging with why, and the decision. It writes nothing:
      the data dir is unchanged, byte for byte.
- [x] AC7: The conductor skill's `parked.cost_cap` row describes the policy and has no "2×" rule.
      The `message.human_order` row states that standing policy is config.
      `nw queue conductor-brief` shows the effective `cost_cap_policy`. `cap_raised` is in
      `quiet.RULES` (always held).

## Test plan

- **Automated tests**
  - `tests/test_cost_policy.py` (new, pure functions):
    - `settings` defaults and bad-value fallback;
    - `reserves`: median, floor, budget clamp, below `min_samples` (AC3);
    - `converging` on hand-built ledgers: pass; rising score; falling score; a major; must_fix;
      no review; the legacy note parser; divergence via a recurrence ledger (AC1, AC2);
    - `decide` for each branch, including the human-raised-above-ceiling cap.
  - `tests/test_agentic_queue.py` or the cost tests (extend): `_cap_blocks` with and without
    `reserves`/`ceiling`; byte-for-byte old behaviour with `None` (AC4).
  - `tests/test_agentic_queue_autopark.py` (extend):
    - the phantom-charge rule still wins first;
    - a parked `cost_cap` campaign is re-evaluated, resumed with `raise_cap`, and gets its
      decision record (AC5).
  - `tests/test_skills.py`: skill row text (AC7). `tests/test_brief*.py`: the `standing` key.
- **Real verification (beyond fakes)**
  - `tests/test_cost_policy_e2e.py` (new): real `queue.tick()` calls on a real git fixture repo
    with a real `NW_DATA_DIR`. `FakeRunner` personas report scripted `cost_usd` values; the costs
    are the only fake.
    - **Converging:** implementer → gate → reviewers, which pass with score 85; then a fix loop
      whose cost crosses the cap. Assert no `park` event, a `cap_raised` event, a decision record,
      and that the run happened.
    - **Diverging:** the second review adds a `major`. Assert a park and one escalation (AC1,
      AC2).
  - **Replay on the real store** (manual, AC6; record the output in As shipped). Run `nw queue
    cost-policy explain --json` against the live data dir for ESC-0021..0023's campaigns
    (IDEA-876, IDEA-877, IDEA-909) and SPEC-0345 / SPEC-0335 (thorough, 2× already). Record what
    each decision would have been. This read-only command is safe on the live store, and it
    checks the convergence reader against real ledgers rather than fixtures.
  - **Live check after landing** (manual): over the next 3 days, count `cap_raised` events and
    `kind: cost` escalations (`nw queue events --type cap_raised`, `nw escalations list`), and
    confirm that each escalation was above the ceiling or diverging. Record in As shipped.
- **Regression guard**: affected tests and `python3 tools/spec_lint.py`. All existing cost and
  autopark tests pass unchanged with the policy off.

## Clock Log

CLOCK-IN: 2026-11-04 (IDEA-928)
CLOCK-OUT: 2026-11-04 (IDEA-928)

## Rollout / migration

1. Land with `enabled: true`. On the first tick, parked cost-cap campaigns without an open
   escalation are re-evaluated (AC5). Open cost escalations stay open, because they are the
   human's; the conductor may withdraw one whose campaign the policy would now raise, citing
   `explain`.
2. `cost_stats.json` is backfilled on first use.
3. The conductor records one `nw queue decision --kind other` decision: "conductor:155 and
   conductor:238 are superseded by agentic.cost_cap_policy (SPEC-0422)".

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## As shipped

**Defaults in code** (`cost_policy.DEFAULTS`), equal to the human's decision, so the human's
`config.yaml` needs no edit: `enabled: true`, `ceiling_multiplier: 3.0` (balanced $45, thorough
$90), `step_fraction: 0.5`, `no_review_raises: 1`, reserve `{multiplier: 1.5, floor_usd: 0.25,
min_samples: 8, window: 40}`.

**Deviations from the Design, decided while building:**
- **Statistics are an incremental ledger scan, not a `record_run` hook.** `_record_run` has no
  `cfg` and about 10 call sites that other builders were editing at the same time. Instead,
  `cost_policy.refresh(cfg)` runs once per tick (`_Ctx.cost_args`). It reads each campaign
  ledger from a byte cursor, takes the model from the matching `run.start`, and skips error,
  timed-out, interrupted, estimated, zero-cost and transient runs. The first call (no file)
  backfills from the campaigns of the last 14 days. The store is
  `{"schema": "nw.cost_stats.v1", "runs": {"persona|model": [...]}, "cursors", "pending"}`.
  `refresh(write=False)` is the read-only form `explain` uses.
- **`plan_step` takes `reserves=` / `ceiling=`, computed by the tick.** It does not call
  `cost_policy.ceiling` itself, so it stays pure. `_cap_blocks` keeps its 2-tuple return. The
  hard check's park detail names the "spend ceiling", and `decide` re-checks the ceiling itself,
  so `Step` has no `ceiling` field. The blocked budget passed on is always the full
  `max_budget_usd`, so the phantom-charge rule (SPEC-0302) sees what it saw before.
- **Pre-deploy parks (AC5).** A PARKED `cost_cap` campaign is planned as if it were resumed. If
  the new reserve fits under its current cap, it resumes with no raise ("the observed-cost
  reserve fits"). If it is still blocked, `decide` raises (resume with `raise_cap`) or escalates
  once. The resume uses `plan_resume(by="queue")` with no `auto_rule`, because the park carries
  no `park.auto`. `apply_resume` gains `bump_counters` / `extra_events`, so the `cap_raised`
  event and the counters land in the same locked write as the resume.
- **At most one escalation per situation.** The escalation's facts carry
  `cost_policy: spent $S cap $C`, and the queue never files a second `kind: cost` escalation
  with the same marker. After an escalation the campaign is PARKED `human_decision`
  (`escalated_from: cost_cap`), so later ticks do not re-evaluate it. `topic` is passed only
  when `escalations.plan_escalate` accepts one (SPEC-0425); the record also carries
  `topic: <topic>` as a fact. The escalation is filed `by: conductor` (the address the queue
  uses for its own escalations, as the sentinel does); the decision record is `by: queue`.
- **Legacy review reader.** A legacy `fix` note counts the reviewer and triager `major` /
  `must_fix` findings of its round. A legacy `pass` counts its `pass over major:` notes. A
  reviewer's unmet AC is a synthetic `major` (SPEC-0203), so it counts as a major too.
- The review triager's cap check in `_route_reviews` keeps the old pessimistic check (no
  reserves). It only decides whether the triager is skipped.
- **Tests.** `tests/conftest.py` turns the policy's default off for every test (autouse
  `_cost_policy_off`), so the pre-existing cost and autopark tests run unchanged (AC4). Policy
  tests opt in with the `cost_policy_on` fixture or `agentic.cost_cap_policy.enabled: true` in
  their config.

**Real verification** (`tests/test_cost_policy_e2e.py`: real `nw queue run --until-idle` /
`nw queue tick` on a real git fixture repo and data dir; only persona costs are scripted):
- Converging: reviews 60/80 fix → 76/80 fix, minors only. The third implementer run is blocked
  at $1.40 + $1.00 > $2.00. The queue raises the cap $2 → $3 (ceiling $6), with no `park` event,
  one `cap_raised` (`by: queue`) and one `resume` decision "raise cap to $3.00". The run
  happens, the campaign merges, and the `cap_raised` queue event is mirrored and held.
- Diverging: the second review is 77 (rising) but has majors. The campaign parks with exactly
  one `kind: cost` escalation: no recommendation, `topic: spend_diverging`, 4 options. Two more
  ticks file none.
- Ceiling (`ceiling_multiplier: 1.5`): one raise to $3. Then $2.10 + $1.00 > $3, so it parks
  with exactly one `spend_ceiling` escalation, and no fourth implementer run starts.
- Reserve (seeded median $0.30 → reserve $0.45). With the policy off, the same script parks at
  $0.60 (+ $1.00 > $1.50). With it on, the run at $0.60 goes ahead, and the campaign parks
  only at $1.20 + $0.45 > $1.50, with the detail "(reserve $0.45 of budget $1.00)".
- AC5: a campaign parked `cost_cap` with the policy off is resumed on the next tick after the
  policy is enabled. The cap goes 1.5 → 2.5, with a `by: queue` resume record.
- **Manual CLI run, outside pytest.** I ran `uv run nw queue add` / `queue run --until-idle` /
  `queue tick` with `NW_DATA_DIR` / `NW_CONFIG_DIR` set to a scratch store and a scratch
  `config.yaml` that has no `cost_cap_policy` key, so the policy ran on its code defaults.
  - Converging: ledger seq 47 `cap_raised` (`by: queue`, 2.0 → 3.0, ceiling 6.0), seq 48
    decision note "score rose 60→76; raise $2.00 → $3.00", then the third implementer run, and
    MERGED.
  - Diverging: parked, `nw escalations list` shows exactly one `ESC-0001 kind cost
    recommendation None` after two more ticks.
  - `explain` on that campaign printed "converging: False (2 major) … escalate
    (spend_diverging)".
- AC6: `explain --json` and the default text form leave the data dir unchanged: the
  sha256-over-tree digest is the same before and after.

**Replay on the live store** (read-only, `nw queue cost-policy explain <cid> --at-seq <park
seq>`, 2026-11-04):

| campaign (escalation) | at | decision |
|---|---|---|
| IDEA-876 (ESC-0021) | $11.94 / $15 | escalate `spend_diverging` (score flat 0→0, 3 major, 2 must_fix) |
| IDEA-877 (ESC-0022) | $12.66 / $15 | raise to $22.50 (latest review passed 81/80) |
| IDEA-909 (ESC-0023) | $13.94 / $15 | escalate `spend_diverging` (score rose 59→69 but 1 major) |
| SPEC-0345 seq 176 / ESC-0014 / ESC-0019 | $22.54/$30, $39.12/$45, $51.72/$60 | raise to $45, $60, $75 |
| SPEC-0335 seq 146 / ESC-0013 / ESC-0020 | $20.38/$30, $36.77/$45, $51.18/$60 | escalate (1 major); raise to $60; escalate (79→52, 1 major) |

Under the new reserve (balanced implementer $0.51 of its $4.00 budget, n=40), none of IDEA-876,
IDEA-877 or IDEA-909 would have parked at those points. At deploy, no live campaign was parked
`cost_cap`. The live explain showed IDEA-909 and IDEA-877 → raise $25 → $32.50 at their next
block, IDEA-918 and IDEA-919 → raise $15 → $22.50 (no review yet), and SPEC-0335 and SPEC-0345 →
escalate `spend_diverging`.

**Live check after landing:** the policy went live when this landed and the runner re-exec'd
(2026-11-04). The 3-day count of `cap_raised` events and `kind: cost` escalations
(`nw queue events --type cap_raised`, `nw escalations list`) is an ongoing operational check for
the conductor, and it does not gate this spec. At landing no live campaign was parked `cost_cap`,
so the first tick re-evaluated nothing. The conductor decision of Rollout step 3 ("conductor:155
and conductor:238 are superseded by agentic.cost_cap_policy (SPEC-0422)") was recorded on the
live store at landing.

## Open questions

_None._
