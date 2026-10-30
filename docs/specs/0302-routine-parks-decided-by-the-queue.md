---
id: SPEC-0302
title: "Routine parks decided by the queue: `base_moving` and phantom-charge `cost_cap` resume themselves, logged `by: queue`"
status: accepted
owner: user
created: 2026-10-30
updated: 2026-10-30
milestone: "M10: agentic MVP"
kind: feature
parent: SPEC-0298
depends_on: [SPEC-0301]
tags: [agentic, conductor, queue, parks, cost]
---

## Context

Every park wakes the conductor: `parked` is an always-wake type in `quiet.RULES`
(`src/wt/agentic/quiet.py:29`), and a park with no decision note raises `park_undecided`
after `park_decide_s` (300 s, `events.py:103`). For some park reasons the conductor's
decision has been the same mechanical action every time. Measured from the campaign
ledgers under `~/.local/share/wt/agentic/campaigns` on 2026-10-30 (all 98 parks):

- **`base_moving` (10 parks).** All 10 were resumed to `CLOSING` with no cap change, no
  note and no loop reset. The park comes from `close.finish_close` (`close.py:569-581`)
  once the base moved `MAX_CLOSE_ATTEMPTS` (2, `close.py:27`) times during one close. The
  conductor's own notes name the cause: concurrent merges into main ("base_moving from
  concurrent human-lane merges", "self-inflicted (my spec merges during its close)").
  Nothing about the campaign needs a decision; it needs to try again once main is quiet.
- **`cost_cap` (16 parks).** 15 were resumed with `--raise-cap`, one went to the human. Most
  are genuine (real spend reached the cap) and stay the conductor's: the skill's
  `parked.cost_cap` row raises once, then escalates. One sub-case is not a judgment at all.
  `queue._record_run` (`queue.py:872-902`) charges a run that reported no cost its **full
  budget** as an estimate (`charged = budget if estimated else result.cost_usd`). A
  transient failure (account limit, 429, overload; `events._TRANSIENT_MARKERS`,
  `events.py:85`) usually reports no cost, so it charges, e.g., $4.00 for a run that did
  no work. The next plan then parks `cost_cap` (`queue._cap_blocks`, `queue.py:132`) on
  money that was never spent. Raising the cap by exactly that phantom charge and retrying
  after the limit resets is what the skill's transient rows already tell the conductor to
  do (`run_failed.transient`: "resume after the reset").
- **`rebase_conflict` on generated files only** never parks today: `gitops.rebase`
  (`gitops.py:367-406`) regenerates a step whose conflicts are all `merge.regenerate`
  paths (SPEC-0191). Since 2026-10-27 that happened 44 times with no park. The other
  `rebase_conflict` parks are SPEC-0303's subject.
- **`config_invalid`** is already resumed by the queue once the project config loads
  (`queue._tick` step 0, `queue.py:3404-3425`, SPEC-0194), but that recovery writes no
  decision record. Under SPEC-0298 exit criterion E4 (every park has a structured decision
  record before it leaves PARKED), each such recovery counts as an undecided park.

The decision log ([SPEC-0301](./0301-conductor-state-handoff-and-decision-log.md),
`decisions.record`, `decisions.py:83`) already accepts `by: queue`, so the queue can record
its own decisions in the same vocabulary as the conductor.

## Goals / Non-goals

**Goals**
- `base_moving` parks and phantom-charge `cost_cap` parks are decided by the queue: a `wait`
  decision at park time, then a scheduled resume with backoff, each written to
  `decisions.jsonl` with `by: queue` and the skill's rule id. Fewer conductor wakeups (E3),
  full decision coverage (E4).
- A park the queue decided does not wake the conductor. One it gives up on does.
- The existing `config_invalid` recovery records a `by: queue` decision.
- Bounded: a campaign gets at most a fixed number of automatic resumes; after that the park
  is the conductor's again, exactly as today.

**Non-goals**
- Genuine `cost_cap` parks (real spend), `loop_exhausted`, `rebase_conflict` and every other
  reason: unchanged, still the conductor's.
- Changing the close algorithm or `MAX_CLOSE_ATTEMPTS` (SPEC-0303 covers conflicts; a busy
  base is handled by retrying later, not by retrying harder).
- Changing how runs are charged. The phantom charge stays recorded as an estimate; only the
  cap is raised to cover it.

## Decision

1. **A pure rule function** `autopark.decide(state, ledger, settings, now) -> Auto | None` is
   evaluated inside `queue._park` for every new park (the single place a park is written,
   `queue.py:859-869`). It returns `None` (the conductor decides, as today) or
   `Auto(rule, kind="wait", resume_at, raise_cap=None, rationale)`.
   - **`parked.base_moving`**: always auto while the campaign's auto-resume count is below
     `max_per_campaign`. `resume_at = now + base_moving_backoff_s × 2^(n)`, where `n` is
     the number of earlier automatic resumes of this campaign (60 s, 120 s, 240 s with the
     defaults).
   - **`parked.cost_cap`**: auto only when the block is entirely phantom. Phantom =
     the sum of `charged_usd` over `run.end` events since the campaign's last `resume` (or
     its `open`) that have `cost_estimated: true` and whose error is transient
     (`events._is_transient` on `result_tail`, as `runs.py:66-68` computes it). Auto when
     phantom > 0 and `total_usd − phantom + budget ≤ cap_usd`, i.e. the run would fit
     without the phantom charges. `raise_cap = cap_usd + phantom` (rounded up to the cent).
     `resume_at = now + transient_backoff_s` (default 900 s), or the reset time when the
     latest transient error carries one that `autopark.reset_time` parses (an ISO-8601
     timestamp or an epoch-seconds `resets at …` value), whichever is later, capped at
     `now + 6 h`.
   - Every other reason: `None`.
2. **At park time** an auto decision adds `auto: {rule, resume_at, raise_cap, n}` to
   `state.park` and to the `park` ledger event, and, after the park transition is written,
   `decisions.record(cfg, campaign=cid, by="queue", kind="wait", rule=<rule>,
   action="auto-resume at <resume_at>", rationale=<rationale>)`. The ledger note that
   `record` appends makes `park_view(...)["decided"]` true, so no `park_undecided` follows.
3. **The resume** is a new tick step, `0a`, next to the `config_invalid` recovery in
   `queue._tick`: for each PARKED campaign with `park.auto` and `resume_at ≤ now` that is not
   in flight, the queue writes `decisions.record(..., by="queue", kind="resume",
   rule=<rule>, action="resume to <resume_to>[ raise cap to $X]")` and then calls
   `plan_resume` / `apply_resume` with `by="queue"`, `raise_cap=auto.raise_cap` and the note
   `"queue: <rule> auto-resume <n+1>/<max>"`. It goes into a free slot or WAITING through
   `_enter_slot_or_wait`, exactly like a conductor resume. The automatic resume count is
   kept in `state.counters.auto_resumes` (`{rule: n}`), which a human or conductor resume
   does not reset.
4. **Giving up.** When `decide` would auto-decide but the count is at `max_per_campaign`
   (default 3 per rule), it returns `None` and the park is an ordinary conductor park. When
   the scheduled resume raises (`ValueError` from `plan_resume` or `apply_resume`, e.g. a
   pending escalation or a human already resumed it), the queue clears `park.auto`, records
   `decisions.record(..., by="queue", kind="other", rule=<rule>, rationale="auto-resume
   failed: <error>")` and appends a new `auto_resume_failed` event (always wake), so the
   conductor takes over.
5. **The conductor stays out of it.** The `parked` event's data gains `auto` (projected in
   `events._project_ledger_event`, `events.py:327-329`). `quiet.RULES["parked"]` becomes
   `"cond"`: a `parked` with a non-null `auto` is **held** (it shows in the next
   `conductor_digest` count), every other `parked` still wakes. The `/wt-conduct` skill gets
   a `parked.auto` sentence in the quiet-stream paragraph and an `auto_resume_failed` row.
   `wt queue conductor-brief` shows `auto` on a parked item (`resume_at`), so a re-oriented
   conductor does not decide it again.
6. **`config_invalid` recovery** (`_tick` step 0) records
   `decisions.record(..., by="queue", kind="resume", rule="parked.config_invalid",
   rationale="project config loads again")` before it re-enters the campaign.
7. **Config** at `agentic.conductor.auto` (code-held defaults; bad values fall back like
   `events.settings`): `{enabled: true, max_per_campaign: 3, base_moving_backoff_s: 60,
   transient_backoff_s: 900}`. `enabled: false` makes `decide` always return `None`, which
   restores today's behavior exactly (the config_invalid decision record is still written).

## Design

- **New module `src/wt/agentic/autopark.py`** (pure apart from `settings`):
  - `DEFAULTS`, `settings(cfg) -> dict`.
  - `@dataclass(frozen=True) Auto: rule, kind, resume_at (ISO str), raise_cap (float|None),
    n (int), rationale`.
  - `phantom_usd(ledger, since_seq) -> float`: sums `charged_usd` of `run.end` events after
    `since_seq` with `cost_estimated` true and a transient `result_tail`.
  - `reset_time(text, now) -> datetime | None`.
  - `decide(state, ledger, sett, now, *, budget) -> Auto | None`. `budget` is the blocked
    run's budget, which `_park` cannot recompute; the `cost_cap` Step carries it
    (`Step.detail` already names it, so `plan_step` adds a `budget` field to the park
    Step, filled from `_cap_blocks`, which returns it alongside the detail).
  - `due(states, now) -> list[state]`: PARKED states whose `park.auto.resume_at ≤ now`.
- **`src/wt/agentic/queue.py`**:
  - `_cap_blocks` returns `(detail, budget)`; its three callers in `plan_step` are updated
    and `Step` gains `budget: float | None = None`.
  - `_park(..., budget=None)`: after building `state["park"]` and before `_transition`,
    call `autopark.decide` (reading `store.read_ledger(cfg, cid, types=["open", "resume",
    "run.end"])`); on an `Auto`, put `auto` into the park. After `_transition` returns,
    call `decisions.record(... kind="wait" ...)`. A failure of `record` is logged to the
    runner log and leaves the park undecided (the conductor's `park_undecided` path, as
    today): an auto decision is never applied without its record.
  - `_tick` step `0a` (after step 0): `for st in autopark.due(store.list_campaigns(cfg,
    state="PARKED"), now)` minus in-flight ids → record, then `plan_resume` /
    `apply_resume`; on `ValueError` the give-up path of Decision 4. Report rows use
    `action: "auto_resume"`, `reason: <rule>`.
  - Step 0 (`config_invalid`) gains its decision record.
  - `apply_resume` increments `counters.auto_resumes[rule]` when `plan["by"] == "queue"`
    and the plan carries `auto_rule`.
- **`src/wt/agentic/events.py`**: `EVENT_TYPES` gains `"auto_resume_failed": ("rule",
  "error")`, raised from the ledger event of the same name in `_project_ledger_event`; the
  `parked` projection adds `auto`.
- **`src/wt/agentic/quiet.py`**: `RULES["parked"] = "cond"`, `RULES["auto_resume_failed"] =
  "wake"`; `classify` holds a `parked` whose `data.auto` is truthy.
- **`src/wt/agentic/brief.py`**: parked items carry `auto: {rule, resume_at} | null`.
- **`skills/wt-conduct/SKILL.md`**: the quiet-stream paragraph lists "a `parked` the queue
  decided (`auto` set)" among held events; the `parked.cost_cap` row says the queue already
  resumes a phantom-charge cap park itself, so a `cost_cap` park that reaches the conductor
  is real spend; a new `auto_resume_failed` row (rule id `auto_resume_failed`): read the
  error, then decide the park per its reason's row. A `parked.base_moving` row (rule id
  `parked.base_moving`) is added: "reaches you only after the queue's automatic resumes are
  spent: the base keeps moving under the close; check what is merging into main (a
  self-inflicted merge loop, a human lane), then resume to `CLOSING` or escalate".

## Alternatives considered

- **Raise `MAX_CLOSE_ATTEMPTS`.** Retrying immediately while main is busy fails the same way;
  a backoff spreads the retries out. The bounded auto-resume also covers base moves outside
  the close's control (the conductor's own merges).
- **Stop charging failed runs their budget.** The estimate exists because a crashed run's
  real spend is unknown; dropping it would under-count real spend (SPEC-0299 attribution and
  the cap both rely on it). Raising the cap by exactly the phantom amount keeps the record
  honest and the cap meaningful.
- **Let the conductor handle these through a decision-table rule.** That is today: each park
  costs a wakeup and a turn at ~457k context for a decision with one possible answer.
- **A `wt queue resume --at` scheduler for the conductor to use.** It still needs the wakeup
  to schedule it; the queue knows enough to decide at park time.

## Acceptance criteria

- [ ] A `base_moving` park gets `park.auto` with `rule: parked.base_moving` and
      `resume_at = park time + 60 s × 2^n`, a `by: queue`, `kind: wait` decision record
      (so `park_view` is decided), and the queue resumes it to `CLOSING` at `resume_at`
      with a `kind: resume` record written before the resume. After 3 automatic resumes the
      next `base_moving` park has no `auto` and wakes the conductor.
- [ ] A `cost_cap` park whose block is entirely phantom (transient-failed runs charged as
      estimates since the last resume) gets `auto` with `raise_cap = cap + phantom` and is
      resumed with that cap after `transient_backoff_s` (or a parsed reset time, at most 6 h
      out). A `cost_cap` park with real spend over the cap, or with a non-transient
      estimated charge, gets no `auto`.
- [ ] A `parked` event with `auto` is held by the `conductor` follower; a `parked` event
      without it, and `auto_resume_failed`, wake.
- [ ] When the scheduled resume fails, `park.auto` is cleared, a `kind: other` queue
      decision and an `auto_resume_failed` event are written, and the campaign stays PARKED.
- [ ] The `config_invalid` recovery writes a `by: queue`, `kind: resume`,
      `rule: parked.config_invalid` decision before re-entering the campaign.
- [ ] `agentic.conductor.auto.enabled: false` produces no `auto`, no automatic resume, and
      the same `parked` deliveries as before this change.
- [ ] Every rule id the queue writes matches `decisions.RULE_RE` and appears in the
      `/wt-conduct` decision table's rule-id column, which gains `parked.base_moving` and
      `auto_resume_failed`.
- [ ] `wt queue conductor-brief --json` shows `auto` on an auto-decided parked item.

## Test plan

- **Automated, `tests/test_agentic_autopark.py`** (temp agentic store, campaigns from
  `store.create_campaign`, ledger events via `store.append_event`, a fake clock):
  - `test_decide_base_moving_backoff`: n = 0, 1, 2 give 60/120/240 s; n = 3 gives `None`.
  - `test_phantom_usd`: two transient estimated charges after the last resume count; an
    estimated non-transient charge, a reported-cost transient failure, and a charge before
    the resume do not.
  - `test_decide_cost_cap_phantom_only` (parametrized): all-phantom → `Auto` with
    `raise_cap = cap + phantom`; real spend over the cap → `None`; phantom present but not
    enough to explain the block → `None`.
  - `test_reset_time_parsing`: ISO and epoch forms parse; an unparseable error falls back to
    `transient_backoff_s`; a reset 10 h out is capped at 6 h.
  - `test_settings_fallback`: bad values fall back to defaults; `enabled: false` → `None`.
- **Automated, `tests/test_agentic_queue_autopark.py`** (fake runner and temp git repo, as
  `tests/test_agentic_queue.py` drives `tick`):
  - `test_base_moving_park_auto_decided_and_resumed`: force `finish_close` to return a
    `base_moving` park (the `_hooks["before_merge"]` hook moves main twice); assert the park
    has `auto`, `decisions.read` has a `wait` then (after advancing the clock and ticking) a
    `resume` record, both `by: queue`, the campaign is back in CLOSING, and
    `park_coverage` counts the park as covered.
  - `test_base_moving_gives_up_after_max`: the fourth park has no `auto` and
    `park_view(...)["decided"]` is false.
  - `test_transient_phantom_cost_cap_auto_resume`: a fake runner returns a transient error
    with no cost, the next plan parks `cost_cap`; assert `auto.raise_cap`, then after the
    backoff the campaign resumes with `cost.cap_usd == raise_cap` and the resume event's
    `cap_usd` matches.
  - `test_auto_resume_failure_hands_back`: open an escalation on the campaign before
    `resume_at`; the tick clears `auto`, writes the `other` record and an
    `auto_resume_failed` ledger event; the campaign stays PARKED.
  - `test_config_invalid_recovery_records_decision`.
  - `test_auto_disabled_is_today`: with `enabled: false` the same scenario parks with no
    `auto` and no decision record.
- **Automated, `tests/test_agentic_quiet.py`**: `test_parked_auto_is_held`
  (with/without `auto`, plus `auto_resume_failed` → wake); the always-wake parametrization
  drops plain `parked`-with-auto only.
- **Automated, `tests/test_skills.py`**: `test_conduct_skill_auto_parks` (the skill has the
  `parked.base_moving` and `auto_resume_failed` rule ids and mentions held auto parks);
  `test_conduct_skill_rule_ids` keeps passing.
- **Automated, `tests/test_agentic_conductor_brief.py`**: `test_brief_shows_auto`.
- **Sabotage checks:** skip the `wait` record in `_park` → the coverage assertion in
  `test_base_moving_park_auto_decided_and_resumed` fails; treat non-transient estimated
  charges as phantom → `test_decide_cost_cap_phantom_only` fails; make `classify` ignore
  `auto` → `test_parked_auto_is_held` fails.
- **Manual verification:** after merge, on the live queue, `wt queue decisions --json
  --since 1d` shows `by: queue` records for the next `base_moving` park, and
  `wt queue wakeups --since <day>` shows the `parked` holds. Note both in *As shipped*.
- **Regression guard:** full `uv run pytest`; `tests/test_agentic_queue.py`,
  `tests/test_agentic_close.py` and `tests/test_agentic_decisions.py` unchanged apart from
  the `_cap_blocks` return shape where a test calls it directly.

## Rollout / migration

1. Merge after SPEC-0301 (already done). The runner re-execs on `code_changed`; parks
   written before the merge have no `auto` and stay the conductor's.
2. The conductor re-reads the skill on `code_changed`.
3. If an automatic resume turns out wrong in practice, set `agentic.conductor.auto.enabled:
   false` (a conductor tuning call, no escalation) and file a follow-up.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

- None. Backoffs and the per-campaign limit are defaults to be tuned from the first window.
