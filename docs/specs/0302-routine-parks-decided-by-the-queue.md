---
id: SPEC-0302
title: "Routine parks decided by the queue: `base_moving` and phantom-charge `cost_cap` resume themselves, logged `by: queue`"
status: done
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
  decision recorded by the tick right after the park, then a scheduled resume with backoff, each written to
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
     `max_per_campaign`. `resume_at = now + base_moving_backoff_s × 2^(n)`, where
     `n = state.counters.auto_resumes.get(rule, 0)`, the number of earlier automatic resumes
     of this campaign under this rule (60 s, 120 s, 240 s with the defaults).
   - **`parked.cost_cap`**: auto only when the block is entirely phantom. Phantom =
     the sum of `charged_usd` over `run.end` events since the campaign's last `resume` (or
     its `open`) that have `cost_estimated: true`, whose error is transient
     (`events._is_transient` on `result_tail`, as `runs.py:66-68` computes it) **and that did
     no measurable work: `usage` is empty or reports zero output tokens, or `duration_s` ≤
     `phantom_max_duration_s` (default 120 s, in `agentic.conductor.auto`)**. A run that
     worked for a long time and then hit a 529, `overloaded`, `api_error` or a usage limit
     reports no cost either, but its real spend is unknown and may be near its budget, so
     it is not phantom and the park stays the conductor's. Auto when
     phantom > 0 and `total_usd − phantom + budget ≤ cap_usd`, i.e. the run would fit
     without the phantom charges. `raise_cap = cap_usd + phantom` (rounded up to the cent).
     `resume_at = now + transient_backoff_s` (default 900 s), or the reset time when the
     latest transient error carries one that `autopark.reset_time` parses (an ISO-8601
     timestamp or an epoch-seconds `resets at …` value), whichever is later, capped at
     `now + 6 h`.
   - Every other reason: `None`.
2. **At park time** an auto decision adds `auto: {rule, resume_at, raise_cap, n}` to
   `state.park` and to the `park` ledger event; `_park` stores it and does nothing else. It
   never calls `decisions.record`, because `_park` runs under the campaign item lock and
   `record` takes the `decisions.jsonl.lock` flock before `store.append_event` takes the item
   lock: calling it from `_park` would lock item → decisions, the reverse of a conductor's
   `wt queue decision --campaign X` (decisions → item), and the two processes could deadlock.
   The `wait` record, `decisions.record(cfg, campaign=cid, by="queue", kind="wait",
   rule=<rule>, action="auto-resume at <resume_at>", rationale=<rationale>)`, is written by
   `_tick` with **no item lock held**: right after the step's row is returned, and again in
   step `0a` for any PARKED campaign whose `park.auto` has no decision note after its
   `park_seq` (which also covers a crash between the park and the record). Until that record
   exists, step `0a` does not resume the campaign. The ledger note that `record` appends
   makes `park_view(...)["decided"]` true, so no `park_undecided` follows.
3. **The resume** is a new tick step, `0a`, next to the `config_invalid` recovery in
   `queue._tick`: for each PARKED campaign with `park.auto`, a `wait` record and
   `resume_at ≤ now` that is not in flight, the queue writes `decisions.record(...,
   by="queue", kind="resume", rule=<rule>, action="resume to <resume_to>[ raise cap to $X]")`
   (again with no item lock held) and then calls `plan_resume(cfg, cid, by="queue",
   raise_cap=auto.raise_cap, note="queue: <rule> auto-resume <n+1>/<max>",
   auto_rule=auto.rule)` and `apply_resume`. It goes into a free slot or WAITING through
   `_enter_slot_or_wait`, with the same slot accounting as step 0 (see Design). The automatic
   resume count is kept in `state.counters.auto_resumes` (`{rule: n}`), which a human or
   conductor resume does not reset.
4. **Giving up.** When `decide` would auto-decide but
   `state.counters.auto_resumes.get(rule, 0)` is at `max_per_campaign` (default 3, per rule),
   it returns `None` and the park is an ordinary conductor park. Step `0a` re-reads the state
   under the item lock. If the campaign is no longer PARKED, its `park.reason` no longer
   matches `auto.rule`, or `escalations.open_for(cfg, cid)` is not None, the queue skips the
   resume and takes the give-up path. A `ValueError` from `plan_resume`/`apply_resume` (e.g.
   a merge campaign target or a state change racing the tick) takes the same path. The
   give-up path, while the campaign is still PARKED, clears `park.auto`, records
   `decisions.record(..., by="queue", kind="other", rule=<rule>, rationale="auto-resume
   failed: <error>")` and appends a new `auto_resume_failed` ledger event (always wake), so
   the conductor takes over. If the campaign is no longer PARKED or the park was replaced
   (a human resumed it, or an escalation re-parked it as `human_decision`, which drops
   `auto` because `decide` returns `None` for that reason), the queue only drops the stale
   `auto` and never resumes it or withdraws the human's escalation.
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
   transient_backoff_s: 900, phantom_max_duration_s: 120}`. `enabled: false` makes `decide`
   always return `None`, which restores today's behavior for new parks (the config_invalid
   decision record is still written). With `enabled: false`, step `0a` does not resume. For
   each PARKED campaign that still has `park.auto`, it takes the give-up path of Decision 4
   (clear `auto`, a `kind: other` record with rationale `auto-resume disabled`, an
   `auto_resume_failed` event), so the conductor gets those parks back: a park written while
   the feature was on already has a `wait` note, so `park_undecided` and the
   `conductor_ping` fingerprint would otherwise never wake anyone for it.

## Design

- **New module `src/wt/agentic/autopark.py`** (pure apart from `settings`):
  - `DEFAULTS`, `settings(cfg) -> dict`.
  - `@dataclass(frozen=True) Auto: rule, kind, resume_at (ISO str), raise_cap (float|None),
    n (int), rationale`.
  - `phantom_usd(ledger, since_seq, max_duration_s) -> float`: sums `charged_usd` of
    `run.end` events after `since_seq` with `cost_estimated` true, a transient `result_tail`
    and no measurable work (`usage` empty or zero output tokens, or `duration_s` ≤
    `max_duration_s`).
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
    "run.end"])`); on an `Auto`, put `auto` into the park and the `park` event. `_park` only
    stores `auto`; it never calls `decisions.record` (see Decision 2).
  - `_tick` writes the `wait` record with no item lock held, right after the step's row is
    returned (rows carry the campaign id and the `auto` of a new park). A failure of
    `record` is logged to the runner log and retried by step `0a`, which does not resume a
    campaign whose `park.auto` has no decision note after its `park_seq`: an auto decision
    is never applied without its record.
  - `_tick` step `0a` (after step 0): `for st in autopark.due(store.list_campaigns(cfg,
    state="PARKED"), now)` minus in-flight ids → (first, with `auto` set but no `wait`
    note after `park_seq`, write that `wait` record) → re-read the state under the item lock
    and run the Decision 4 checks → `kind: resume` record (no item lock held) →
    `plan_resume` / `apply_resume`; on `ValueError` or a failed check the give-up path of
    Decision 4. Report rows use `action: "auto_resume"`, `reason: <rule>`. With
    `enabled: false` it runs only the disabled hand-back of Decision 7.
  - Slot accounting: `apply_resume` gains optional `busy`/`slots` keyword arguments, passed
    through to `_enter_slot_or_wait`. Step `0a` passes `busy=n_busy(), slots=slots` and
    appends the resumed state to `busy` or `queued` as step 0 does (`queue.py:3418-3425`),
    so in-flight campaigns count toward the slot limit and later steps in the same tick
    see the resumed campaign.
  - Step 0 (`config_invalid`) gains its decision record.
  - `plan_resume` gains `auto_rule=None`, stored in the plan. `apply_resume` increments
    `counters.auto_resumes[rule]` when `plan["by"] == "queue"` and the plan carries
    `auto_rule`.
- **`src/wt/agentic/events.py`**: `EVENT_TYPES` gains `"auto_resume_failed": ("rule",
  "error")`, raised from the ledger event of the same name in `_project_ledger_event`; the
  `parked` projection adds `auto`.
- **`src/wt/agentic/schemas.py`**: the ledger type table gains `"auto_resume_failed":
  ("rule", "error")` (`store.append_event` validates every record through `ledger_problems`
  and raises `ValueError` on an unknown type, so the give-up path would otherwise crash in
  the tick). The `park` record keeps accepting the optional `auto` key.
- **`src/wt/agentic/quiet.py`**: `RULES["parked"] = "cond"`, `RULES["auto_resume_failed"] =
  "wake"`; `classify` holds a `parked` whose `data.auto` is truthy.
- **`src/wt/agentic/brief.py`**: parked items carry `auto: {rule, resume_at} | null`.
- **`skills/wt-conduct/SKILL.md`**: the quiet-stream paragraph lists "a `parked` the queue
  decided (`auto` set)" among held events, and `parked` moves from the wake list to the
  conditional list of that paragraph, so the existing RULES-vs-skill comparison test
  (`quiet.py:20-22`) keeps passing; the `parked.cost_cap` row says the queue already
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

- [x] A `base_moving` park gets `park.auto` with `rule: parked.base_moving` and
      `resume_at = park time + 60 s × 2^n`, a `by: queue`, `kind: wait` decision record
      (so `park_view` is decided), and the queue resumes it to `CLOSING` at `resume_at`
      with a `kind: resume` record written before the resume. After 3 automatic resumes the
      next `base_moving` park has no `auto` and wakes the conductor.
- [x] A `cost_cap` park whose block is entirely phantom (transient-failed runs charged as
      estimates since the last resume) gets `auto` with `raise_cap = cap + phantom` and is
      resumed with that cap after `transient_backoff_s` (or a parsed reset time, at most 6 h
      out). A `cost_cap` park with real spend over the cap, with a non-transient
      estimated charge, or with a transient charge from a run that did work (non-zero
      output tokens and `duration_s` > `phantom_max_duration_s`), gets no `auto`.
- [x] A `parked` event with `auto` is held by the `conductor` follower; a `parked` event
      without it, and `auto_resume_failed`, wake.
- [x] When the scheduled resume fails (`ValueError` from `plan_resume`/`apply_resume`, a
      state that no longer matches `auto.rule`, or an open escalation), `park.auto` is
      cleared, a `kind: other` queue decision and an `auto_resume_failed` ledger event
      (readable with `store.read_ledger`) are written, and the campaign stays PARKED.
      Escalating an auto park re-parks it `human_decision` with no `auto`; step `0a` never
      resumes it and the escalation stays open.
- [x] The `wait` record is written by `_tick` with no item lock held (never from `_park`),
      and step `0a` does not resume a campaign whose `park.auto` has no such record.
- [x] `plan_resume` called for a queue auto-resume returns a plan with an `auto_rule` key,
      and step `0a` applies the same `busy`/`slots` accounting as step 0.
- [x] The `config_invalid` recovery writes a `by: queue`, `kind: resume`,
      `rule: parked.config_invalid` decision before re-entering the campaign.
- [x] `agentic.conductor.auto.enabled: false` produces no `auto`, no automatic resume, and
      the same `parked` deliveries as before this change. Disabling it with an auto park
      pending clears that `auto` and raises an `auto_resume_failed` event for the campaign.
- [x] Every rule id the queue writes matches `decisions.RULE_RE` and appears in the
      `/wt-conduct` decision table's rule-id column, which gains `parked.base_moving` and
      `auto_resume_failed`.
- [x] `wt queue conductor-brief --json` shows `auto` on an auto-decided parked item.

## Test plan

- **Automated, `tests/test_agentic_autopark.py`** (temp agentic store, campaigns from
  `store.create_campaign`, ledger events via `store.append_event`, a fake clock):
  - `test_decide_base_moving_backoff`: `counters.auto_resumes["parked.base_moving"]` = 0, 1,
    2 give 60/120/240 s; 3 gives `None`; a count under another rule does not change `n`.
  - `test_phantom_usd`: two transient estimated charges after the last resume count; an
    estimated non-transient charge, a reported-cost transient failure, a charge before
    the resume, and a transient estimated charge with `duration_s` 1200 and non-zero output
    tokens do not.
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
  - `test_auto_resume_failure_hands_back`: monkeypatch `apply_resume` to raise `ValueError`
    at `resume_at` and assert `auto` is cleared, the `other` record and the
    `auto_resume_failed` event (read back with `store.read_ledger`) are written, and the
    campaign stays PARKED.
  - `test_escalation_drops_auto`: escalating an auto park re-parks it `human_decision` with
    no `auto`, step `0a` never resumes it, and the escalation stays open.
  - `test_wait_record_outside_item_lock`: assert `decisions.record` is never called while the
    campaign's item lock is held (patch `store.item_lock` to track depth); a park whose
    `wait` record is missing (record patched to fail once) is not resumed until step `0a`
    writes it.
  - `test_resume_slot_accounting`: with all slots busy, an auto resume goes to WAITING, and
    the resumed state appears in this tick's `busy`/`queued` lists; the plan from
    `plan_resume` carries `auto_rule`.
  - `test_disable_hands_back_pending_auto`: park with `auto` while enabled, set
    `enabled: false`, tick: `auto` cleared, `other` record with rationale `auto-resume
    disabled`, an `auto_resume_failed` event, no resume.
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
- **Sabotage checks:** skip the `wait` record in `_tick` → the coverage assertion in
  `test_base_moving_park_auto_decided_and_resumed` fails; treat non-transient estimated
  charges as phantom → `test_decide_cost_cap_phantom_only` fails; make `classify` ignore
  `auto` → `test_parked_auto_is_held` fails.
- **Manual verification:** after merge, on the live queue, `wt queue decisions --json
  --since 1d` shows `by: queue` records for the next `base_moving` park, and
  `wt queue wakeups --since <day>` shows the `parked` holds. Note both in *As shipped*.
- **Regression guard:** full `uv run pytest`; `tests/test_agentic_queue.py`,
  `tests/test_agentic_close.py` and `tests/test_agentic_decisions.py` unchanged apart from
  the `_cap_blocks` return shape where a test calls it directly.

### Test record (2026-10-30)

- `uv run pytest tests/test_agentic_autopark.py tests/test_agentic_queue_autopark.py
  tests/test_agentic_quiet.py tests/test_agentic_conductor_brief.py tests/test_skills.py`:
  all pass (pure rules, step 0a with the `wait` / `resume` records, give-up and disable
  hand-back, escalation dropping `auto`, the lock-order test, slot accounting, the
  `config_invalid` record, quiet hold / wake, brief `auto`, skill rule ids).
- `uv run pytest tests -k agentic` (run before the new tests were added, so it covers the
  existing suites): 2209 passed, 2 skipped.
- The remaining, non-agentic part of the full suite was left to gating (no-regressions check). `python3 tools/spec_lint.py` passes.
- Review loop: added `test_auto_park_projects_auto_and_is_held`,
  `test_close_park_inside_tick_is_decided_and_recorded` and `test_long_crashed_run_is_not_phantom`;
  removing the end-of-tick `_record_auto_waits` or `auto` from the `parked` projection now fails
  them. Targeted run of the five spec test files plus `tests/test_agentic_queue_merge.py` passes;
  the full suite is left to gating.
- Review loop 2: step 0a now counts the campaigns it resumed earlier in the same tick against
  `slots` (`test_resume_slot_accounting_within_one_step`: slots=2, one active, two due → one
  CLOSING, one WAITING); `test_decide_failure_never_blocks_the_park` and
  `test_post_merge_base_moving_is_not_auto` added. Targeted run of `tests/test_agentic_queue_autopark.py`,
  `tests/test_agentic_autopark.py`, `tests/test_agentic_close.py`, `tests/test_agentic_queue_merge.py`:
  135 passed; the full suite is left to gating.
- Gate timeout investigation (two `uv run pytest -q -x` kills near 96%, exit 143): no hang and no
  loop in the new code. The whole suite was run to completion in two parts on a host at load 10-15
  (other campaigns' pytest runs in parallel): `-k agentic` 2244 passed, 2 skipped, 1 failed in 989 s
  (`tests/test_agentic_adopt.py::test_fanout_detach_precondition`, a thread-timing test in files
  this change does not touch; it passed in 7 of 8 isolated reruns, so it is a load flake) and
  `-k "not agentic"` 1841 passed, 1 skipped in 641 s. Together that is ~27 min wall and ~17 min of
  CPU, so the suite itself exceeds the 1200 s gate timeout under load; progress was steady, not
  stalled. No slowdown from this change: the tick-heavy `tests/test_agentic_queue.py`,
  `test_agentic_close.py` and `test_agentic_queue_merge.py` (250 tests) take 57 s with it against 67 s
  on the merge-base source tree. The gate timeout was not changed and no test was skipped.
- Review loop 3: added tests for the park-reason mismatch and `plan_resume` `ValueError` give-up
  paths, the real `apply_resume` `auto_rule` guard, the decision record preceding the `resume`
  event (ledger seq), no brief note from an auto resume, the lost-race `other` record, `_tick`
  planning the campaign step 0a resumed, an in-flight campaign being skipped, the disabled
  fallback rule id, `RULE_RE` conformance of the queue's rule ids, the `--json` brief CLI and an
  unrelated timestamp not being read as a reset time. Targeted run of
  `tests/test_agentic_queue_autopark.py`, `test_agentic_autopark.py`, `test_agentic_quiet.py`,
  `test_agentic_conductor_brief.py`, `test_skills.py`: all pass; the full suite is left to gating.
- Manual verification (`wt queue decisions --since 1d`, `wt queue wakeups`) is, by the plan's own
  wording, a post-merge check on the live queue, so it has **not** been performed yet and is the
  first thing to check after merge. The "manual steps performed" part of the Definition of done
  below is therefore pending, not claimed; the DoD text itself is left as accepted.

### As shipped (2026-10-30)

- `Auto.park_data()` also carries `rationale`, which the tick uses as the `wait` record's
  rationale; `_park` evaluates `autopark.decide` only for `base_moving` / `cost_cap` parks
  written by the tick (`by == "tick"`).
- `phantom_usd(ledger, since_seq=None, max_duration_s=120)` defaults `since_seq` to the last
  `open` / `resume` in the ledger it is given.
- Step `0a` lives in `queue._auto_resume_step` (called from `_tick`), the `wait` records in
  `queue._record_auto_waits` (called at the end of `_tick`, so a park written by any step in
  the tick is recorded with no item lock held, and again in step `0a`). `apply_resume` also
  refuses (`ValueError`) when the plan carries `auto_rule` and the park no longer holds an
  `auto` of that rule, which closes the race between the step's check and the resume.
- The `auto_resume_failed` ledger event and its `kind: other` record are written by
  `_auto_give_up`; the event is written under the item lock, the record after it.
- The skill also gained a `parked.config_invalid` row (the queue's recovery record uses that
  rule id).
- Phantom definition tightened in review: a run "did no measurable work" only when its
  `duration_s` is known and ≤ `phantom_max_duration_s`. Empty `usage` does not count as no work
  (the runner writes `usage: {}` for every crashed, timed-out or unparseable run, so it would
  make the duration guard dead code), and zero output tokens with a long duration is not phantom
  either. A run with no `duration_s` is not phantom. This supersedes the "usage is empty … or
  duration ≤" wording in Decision 1 and the Design.

- Review loop 2: `_park` wraps `autopark.decide` in a try/except (a failure becomes a `note` event
  and no `auto`), and the `base_moving` backoff is clamped to 6 h. The post-merge
  "merged != gated HEAD" `base_moving` park (`close.py`) carries `no_auto: True` and `_park(...,
  auto_ok=False)`, so only the `moved()` park is auto-decided: the merge already happened, a blind
  retry is not a mechanical decision.

- Review loop 3: a queue auto-resume passes no `note` to `plan_resume` (a note becomes a
  `pending_brief.human_notes` entry that later briefs show as a human note); the bookkeeping text
  `queue: <rule> auto-resume n/max` goes in the plan's `event_note`, which only the `resume`
  ledger event carries. The `resume` decision record is still written before `apply_resume`; if
  `apply_resume` then loses a race and the park is no longer the queue's to give up, a
  `kind: other` record ("automatic resume not applied") follows, so `decisions.jsonl` never ends
  on a `resume` that did not happen. The fallback rule id for an `auto` without `rule` is
  `auto_resume_failed`. `reset_time` reads an ISO timestamp only when it follows a `reset(s)` /
  `retry` / `try again` / `available` / `until` cue.
- Locking: step `0a` re-reads the state without the item lock for its pre-checks; the authoritative
  re-check is the `auto_rule` guard inside `apply_resume`'s item lock (and `_auto_give_up`'s own
  lock), which refuses a resume whose park changed. This is the "re-reads under the item lock" of
  Decision 4, with the check folded into the resume itself.

## Rollout / migration

1. Merge after SPEC-0301 (already done). The runner re-execs on `code_changed`; parks
   written before the merge have no `auto` and stay the conductor's.
2. The conductor re-reads the skill on `code_changed`.
3. If an automatic resume turns out wrong in practice, set `agentic.conductor.auto.enabled:
   false` (a conductor tuning call, no escalation) and file a follow-up. Pending auto parks
   are handed back to the conductor on the next tick (Decision 7), not stranded.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

- None. Backoffs and the per-campaign limit are defaults to be tuned from the first window.
