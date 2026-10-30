---
id: SPEC-0301
title: "Conductor state handoff and decision log: `wt queue decision`, `wt queue conductor-brief`, compaction rule"
status: done
owner: user
created: 2026-10-30
updated: 2026-10-30
milestone: "M10: agentic MVP"
kind: feature
parent: SPEC-0298
depends_on: [SPEC-0299]
tags: [agentic, conductor, audit, skills]
---

## Context

The conductor records its decisions as free-text `wt campaign note`s. The only structure
is that `queue.park_view` treats any note after a park as "decided"
([SPEC-0222](./0222-queue-watch-needs-you-vs-conductor-handling.md)). Queue-level decisions
(runner restarts, drains waited out) are not recorded anywhere. So:

- Nobody can audit which rule of the `/wt-conduct` decision table a decision applied, or
  whether every park got one (MVP exit criterion E4: 100%).
- A restarted or compacted conductor re-orients from `wt queue conduct-status` plus its own
  conversation history. In the [SPEC-0298](./0298-agentic-mvp-cheap-durable-quiet-conductor.md)
  baseline that history is why each turn re-read about 457k tokens. Context is never
  trimmed, because nothing outside the session holds the in-flight state: which sub-agent
  was dispatched for what, and what was last decided on each park. Exit criterion E2 wants
  a median ≤ 150k.

`conduct_status` (`src/wt/agentic/cli_conduct.py`) already gathers the lease, counts,
parked and active items, unanswered mail and recent events. It does not include decisions,
dispatches, escalations or pointers to standing rules, and its `recent_events` and full
`park` dicts make it larger than it needs to be.

## Goals / Non-goals

**Goals**
- Every conductor or queue decision is a structured record: campaign, triggering event,
  rule applied, action and rationale. The records can be listed, along with park coverage.
- `wt queue conductor-brief` gives a fresh or compacted conductor everything it needs to
  continue, in a few thousand tokens.
- The skill tells the conductor to compact at a context threshold and re-orient from the
  brief, never from history.

**Non-goals**
- Automatically deciding parks (a separate SPEC-0298 child that will log through this
  record, `by: queue`).
- Changing `wt queue conduct-status`. It stays as it is for existing callers.
- Enforcing rule conformance (the scorecard child audits it).

## Decision

1. **Decision log.** `<agentic_dir>/conductor/decisions.jsonl` is append-only, with a
   `seq` and the schema `wt.conductor.decision.v1`: `{seq, ts, by, campaign|null,
   spec_id|null, event_id|null, rule, kind, action, rationale}`.
   - `kind` is one of `resume, abandon, escalate, ask_steward, dispatch, restart_runner,
     wait, reconcile, apply_answer, merge, no_action, other`.
   - `rule` is the decision-table row id, validated against `^[a-z_]+(\.[a-z_]+)?$`
     (`RULE_RE`). A row's rule id is `<event type>` (`runner_dead`, `conductor_ping`, …) or
     `parked.<reason>` (`parked.cost_cap`). A row that lists several types or reasons yields
     one id per listed item (e.g. `parked.loop_exhausted`, `parked.triage_escalated`). A
     qualifier becomes a second segment (`run_failed.transient`, `message.answer`,
     `parked.gates_failed` for the merge-campaign row). A row with no event gets an
     explicit id (`wt_broken`). The skill's decision table gains a leading `rule id` column
     listing each row's id(s), so the skill and the log share one vocabulary and the
     scorecard child can tell which row a decision applied.
2. **One write command:** `wt queue decision [CAMPAIGN] --kind K --rule R [--event N]
   [--action "…"] [--by conductor] RATIONALE`. `--by` is `conductor`, `queue` or
   `steward:<P>`. It applies at once, with no dry run,
   because it is a pure append like `wt campaign note`. With a campaign it also appends a
   ledger `note`, `{text: "decision <kind> (<rule>): <rationale>", author: <by>,
   decision: {seq, kind, rule}}`, so `park_view`, `wt campaign log` and the TUI see it
   unchanged. The ledger note is appended first and the `decisions.jsonl` line is written
   only after it succeeds, so a failure leaves at most a plain note, never a logged
   decision that `park_view` cannot see. `wt campaign note` stays for non-decision remarks.
3. **Listing:** `wt queue decisions [--campaign C] [--since D] [--tail N] [--parks] [--json]`.
   `--since` accepts an ISO date/datetime or a relative duration (`2h`, `1d`), like
   `wt queue conductor-cost --since`. `--parks` lists every `park` ledger event in the
   window with the decision record that covers it (see `park_coverage`), or `missing`, and a
   coverage ratio. When `--since` is omitted, `--parks` (and `park_coverage(since=None)`)
   counts only parks whose ts is at or after the first record in `decisions.jsonl`; with an
   empty log it reports `parks: 0`. So parks decided by free-text notes before the merge are
   never reported `missing`.
4. **Brief:** `wt queue conductor-brief [--json]` (`wt.conductor.brief.v1`), built only from
   files. Each list has a fixed cap, with `+N more` when cut. The text form is at most
   12,000 characters (about 3k tokens). Sections are kept in this priority order, highest
   first: `runner`, `conductor`, `counts`, `parked`, `open_dispatches`, `escalations_open`,
   `unanswered_mail`, `active`, `recent_decisions`, `standing`. Truncation removes items
   from the lowest-priority section first and ends each cut section with `+N more`. It
   contains:
   - `runner`: held, pid, heartbeat age, draining reason.
   - `conductor`: follower pid, `session_id` and `context_tokens` (the last turn's context
     from the SPEC-0299 reader), `compact_at`, and `compact_now`.
   - `counts`.
   - `parked` (cap 20): campaign, label, reason, a 200-char detail, `parked_for_s`, owner,
     the open escalation id, `last_decision`, `decided` and `overdue`. `last_decision` is
     `{kind, rule, a 200-char rationale, ts}` for the latest decision record on that
     campaign with ts ≥ the current park's ts (from `park_view(...)["parked_at"]`), or
     null: a decision from an earlier park is never shown as current. `decided` and
     `overdue` come from `park_view`.
   - `active` (cap 20): campaign, label, state, loops, cost.
   - `open_dispatches`: `dispatch` decisions with no later decision on the same campaign,
     or on the queue for campaign-less ones.
   - `recent_decisions` (last 10).
   - `unanswered_mail` (cap 10, 160-char text).
   - `escalations_open` (cap 10).
   - `standing`: the path of the installed `/wt-conduct` skill, the configured
     `agentic.conductor.memory_paths` and the configured `agentic.conductor.standing_rules`
     strings.
5. **Compaction rule.** `agentic.conductor.compact_at_tokens` (read from config) defaults
   to 150000; `memory_paths` and `standing_rules` default to empty lists. The skill
   says: when `compact_now` is true, or the session's context indicator passes the
   threshold, finish the event in hand. Record any in-flight dispatch with
   `wt queue decision --kind dispatch`, then compact. After compacting, or on any fresh
   start, run `wt queue conductor-brief` and re-arm the Monitor. Never re-read event history
   or old conversation to orient. The skill checks the brief once per digest and after each
   escalation.

## Design

- New module `src/wt/agentic/decisions.py`:
  - `KINDS`, `RULE_RE`, `log_path(cfg)` (`store.agentic_dir(cfg) / "conductor" /
    "decisions.jsonl"`, the directory SPEC-0299 introduces).
  - `record(cfg, *, kind, rule, rationale, campaign=None, event_id=None, action=None,
    by="conductor") -> dict`. It validates the kind, the rule, that the campaign exists
    (`store.read_state`), and, when given, that `event_id` exists among
    `events.read_since(cfg, event_id - 1)`. With a campaign it first appends the ledger
    note via `store.append_event(cfg, cid, "note", …, by=f"agent:{by}")`. Only after that
    succeeds does it append the `decisions.jsonl` line, under a flock, with the next `seq`.
    A failure of the note therefore writes no log line. Because the note carries `seq`, the
    seq is reserved under the same flock before the note is written.
  - `read(cfg, *, campaign=None, since=None) -> list[dict]`.
  - `park_coverage(cfg, *, since=None) -> dict`: for each campaign from
    `store.list_campaigns(cfg, include_terminal=True)`, each `park` in
    `store.read_ledger(cfg, cid, types=["park"])` is matched to the first record with the
    same campaign whose ts is ≥ the park's ts and < the next `park` on that campaign (ts
    compared as parsed UTC datetimes, so ledger and decision timestamps are comparable).
    One decision therefore covers at most one park. The `since=None` window is defined in
    Decision 3. Returns `{parks, covered, missing: [...], ratio}`.
  - `open_dispatches(records) -> list[dict]`.
- New module `src/wt/agentic/brief.py`:
  - `conductor_brief(cfg, now=None) -> dict` reuses `Q.queue_payload`, `queue.park_view`,
    `E.reader_holder`, `M.unanswered`, `ESC.open_live` and, for the session and context,
    `conductor_cost.sessions` / `find_transcript` / `read_turns`, taking the last turn of
    the newest registered session. When SPEC-0299 data is absent, these fields are null,
    never an error.
  - `render_text(brief, limit=12000) -> str`.
- CLI: `wt queue decision`, `wt queue decisions` and `wt queue conductor-brief` are
  registered from `src/wt/agentic/cli_conductor.py` (the module SPEC-0299 adds) in its
  `attach`.
- `skills/wt-conduct/SKILL.md`:
  - Procedure step 1 uses `wt queue conductor-brief` (falling back to `conduct-status
    --json` only for the lease check).
  - Step 5 and *Every park gets a decision* use `wt queue decision <cid> --kind … --rule …
    --event <id> "…"` instead of `wt campaign note "conductor: …"`. *Bookkeeping* records
    dispatches with `--kind dispatch`.
  - A new *Compaction and handoff* section contains the rule in Decision 5.
  - A sentence above the decision table: "the first cell of each row lists its rule
    id(s); a park row's id is `parked.<reason>`", and the decision table gains a leading
    `rule id` column listing each row's id(s) per Decision 1 (e.g. `run_failed.transient`,
    `message.answer`, `wt_broken`).
  - The skill edits are rebased on SPEC-0300's if it lands first (both specs edit this
    file); the skill's current periodic-digest row is `conductor_ping`.

## Alternatives considered

- **A `conductor_decision` event in `events.jsonl`.** The conductor's own follower would
  wake on its own writes, which is exactly the kind of noise SPEC-0300 removes. A separate log plus
  a ledger note gives the audit trail without the echo.
- **Extend `conduct-status`.** Its JSON is consumed as is (`wt.conduct.status.v1`, the TUI,
  tests). A separate, size-capped brief keeps that contract and has one job: re-orient.
- **`wt campaign note --decision`.** It cannot record queue-level decisions (runner
  restarts) with no campaign, so a queue-level command covers both.

## Acceptance criteria

- [x] `wt queue decision` validates kind, rule, campaign and event id (each bad value exits
      1 with a clear message and writes nothing). It appends a `wt.conductor.decision.v1`
      record with a monotonically increasing `seq` and, with a campaign, a ledger `note`
      carrying `decision`, after which `park_view(...)["decided"]` is true.
- [x] `wt queue decisions --json` lists records filtered by campaign or since (ISO or
      relative like `2h`). `--parks` reports each park with its covering decision or
      `missing`, plus `ratio`. One decision covers at most one park, and without `--since`
      parks older than the first decision record are excluded.
- [x] `wt queue conductor-brief --json` (`wt.conductor.brief.v1`) contains runner,
      conductor, counts, parked items with `last_decision` and open escalation, active
      items, `open_dispatches`, `recent_decisions`, unanswered mail, open escalations and
      `standing` pointers, with every list capped and the cut reported as `+N more`. A
      parked item's `last_decision` is null when the latest decision is older than the
      current park, and the item carries `decided` and `overdue`.
- [x] The text brief is at most 12,000 characters for a store with 60 parked and 60 active
      campaigns and 500 decisions. Truncation follows the section priority in Decision 4: the
      `runner` and `conductor` lines and at least one parked item always survive, and
      `recent_decisions` and `standing` are cut before `parked`.
- [x] `conductor.context_tokens` is the last turn's context of the newest registered
      conductor session, and `compact_now` is true exactly when it exceeds
      `compact_at_tokens`, which is read from config (`agentic.conductor.compact_at_tokens`,
      default 150000; `memory_paths` and `standing_rules` default to empty lists). With no
      session or transcript both are null or false and the command still succeeds.
- [x] The `/wt-conduct` skill re-orients from `conductor-brief`, records decisions and
      dispatches with `wt queue decision`, states the rule-id convention, and contains the
      compaction rule with the 150k default. Every decision-table row has an explicit rule
      id (or ids) matching `RULE_RE`.
- [x] `wt queue decision` writes the ledger note before the log line; if the note fails, no
      log line is written. `--by` accepts only `conductor`, `queue` or `steward:<P>`.

## Test plan

- **Automated tests** in `tests/test_agentic_decisions.py` and
  `tests/test_agentic_conductor_brief.py`, with a temp agentic store, campaigns from
  `store.create_campaign`, and parks via `store.append_event(..., "park", …)`:
  - `test_decision_validation` (parametrized: bad kind, bad rule, unknown campaign, unknown
    event id → exit 1, nothing written).
  - `test_decision_writes_log_and_ledger_note`: `seq` increments, the ledger note has a
    `decision` dict, and `park_view` is decided.
  - `test_queue_level_decision_no_campaign`.
  - `test_park_coverage`: 3 parks, 2 covered, 1 covered only by a record older than the
    park → `missing`, `ratio == 2/3`. Also: two parks on one campaign with a single later
    decision after the second park → only the second is covered; a park older than the
    first decision record is excluded when `since=None`; an empty log gives `parks: 0`.
  - `test_record_write_order_and_by`: a failing ledger append leaves `decisions.jsonl`
    unchanged; `--by` rejects values other than `conductor`, `queue`, `steward:<P>`;
    `--since 2h` and an ISO date both parse.
  - `test_open_dispatches`: a dispatch followed by a resume on the same campaign is closed.
    A lone dispatch is open.
  - `test_brief_contents_and_caps`: every key is present, `parked[i].last_decision` is
    populated, and lists over their cap report `+N more`. A decision older than the
    current park (park, decide, resume, park again) gives `last_decision: null` and
    `decided: false`.
  - `test_brief_text_size_bound`: 60/60/500 fixture → `len(text) <= 12000`, the `runner`
    and `conductor` lines and at least one parked item are present, and `recent_decisions`
    and `standing` are the sections cut first.
  - `test_brief_context_from_transcript`: a SPEC-0299 session fixture whose last turn has
    context 160k → `compact_now` is true. At 140k → false. With config
    `compact_at_tokens: 100000`, a 140k context gives `compact_now: true`. With no session
    → null and false.
  - `test_cli_decision_decisions_brief` (CliRunner, JSON and text).
- `tests/test_skills.py`: `test_conduct_skill_uses_brief_and_decisions`,
  `test_conduct_skill_has_compaction_rule` and `test_conduct_skill_rule_ids`, which parses
  the decision table in `skills/wt-conduct/SKILL.md` and asserts that every row has a
  non-empty leading `rule id` cell whose ids (comma-separated) each match `RULE_RE`.
- **Sabotage checks:**
  - Skip the ledger note. `test_decision_writes_log_and_ledger_note` must fail, because the
    park is not decided.
  - Match coverage records by campaign only, ignoring ts. `test_park_coverage` must fail.
  - Remove the truncation. `test_brief_text_size_bound` must fail.
- **Manual verification:** on the live queue, run `wt queue conductor-brief` and check that
  the output is under 12k characters and lists every parked item (or the first 20 plus
  `+N more`) with its last decision.
  Then compact the live conductor once per the rule, re-orient from the brief, and note in
  *As shipped* that no `park_undecided` fired in the next 15 min (exit criterion E6).
- **Regression guard:** full `uv run pytest`. `tests/test_agentic_conduct_cli.py` (the
  `conduct-status` contract) and the SPEC-0222 `park_view` tests are unchanged.

### Test record (2026-10-30)

- `uv run pytest tests/test_agentic_decisions.py tests/test_agentic_conductor_brief.py
  tests/test_skills.py` — 15 + 5 + 70 tests pass (`test_conduct_skill_uses_brief_and_decisions`,
  `test_conduct_skill_has_compaction_rule`, `test_conduct_skill_rule_ids` included).
- Related modules: `tests/test_agentic_conduct_cli.py`, `test_agentic_conductor_cost.py`,
  `test_agentic_events.py`, `test_agentic_queue_cli.py`, `test_agentic_dashboard_spec_reviews.py`,
  `test_agentic_tmp_isolation.py` — 249 tests pass together with the above.
- Sabotage checks run: disabling the ledger note fails `test_decision_writes_log_and_ledger_note`
  (and the write-order test); matching coverage by campaign only fails `test_park_coverage` and
  the one-decision-one-park test; removing the truncation loop fails `test_brief_text_size_bound`.
- Manual: `wt queue conductor-brief` on the live store gave 1,048 characters, listing all 3 parked
  items (`last: none`, `decided=true`), the 2 active campaigns, the runner and the conductor
  session (context 111,673 of 150,000, `compact_now=false`). `wt queue decisions --parks` reported
  `0/0` on the still-empty log. Compacting the live conductor and watching for `park_undecided`
  for 15 minutes (E6) needs this change on main and the conductor re-reading the skill, so it is
  left to be observed after the merge.
- The full `uv run pytest` run was left to gating.
- Review follow-up: `test_brief_escalation_and_mail_parts` (real escalation, 13 unanswered long messages: ids, caps, 160-char clip, `+3 more`) and a stricter `test_brief_text_size_bound` (recent decisions/standing/active empty before parked loses items; fails when `recent_decisions` is moved above `parked`). `tests/test_agentic_conductor_brief.py` passes (6).
- Second review follow-up: tests for the newest of two sessions, the `context == compact_at` edge, `overdue` true/false, the runner fields, the caps on open dispatches and escalations, and `decisions --parks --campaign`. `wt queue decisions` prints with Rich markup off (a rationale like `[/x]` no longer crashes it), and `wt queue decision` rejects a lone argument that is an existing campaign id (forgotten rationale). The remaining decision-table rows now say to record with `wt queue decision`. `tests/test_agentic_decisions.py`, `tests/test_agentic_conductor_brief.py`, `tests/test_skills.py` — 97 pass; the full suite is left to gating.

### As shipped

- Matches the Design. `decisions.py` and `brief.py` are new; the three commands are registered in
  `cli_conductor.py`. `wt queue decision` takes `[CAMPAIGN] RATIONALE` as one variadic argument
  (click fills an optional first argument before a required second one), and a wrong argument
  count is a usage error (exit 2); every bad value (kind, rule, `--by`, campaign, event) exits 1.
- The ledger note is written under the log's flock, with the `seq` reserved first, and the log
  line follows only after the note succeeded.
- The brief JSON has a top-level `more` map (`{section: items cut}`) rather than per-list wrappers;
  the text form ends a cut section with `+N more`. `park_coverage` also returns `items` (each park
  with its covering record) and `ratio` is null when there are no parks.
- `SKILL.md`: a leading `rule id` column on all 41 decision-table rows. Existing tests that
  looked a row up by its first cell now skip that column (`_ev` in `tests/test_skills.py`, and
  two inline equivalents in `test_agentic_dashboard_spec_reviews.py` and
  `test_agentic_tmp_isolation.py`).
- SPEC-0300 had not merged when this was built; its `conductor_ping` row keeps that id.

## Rollout / migration

1. Merge after SPEC-0299. The conductor re-reads the skill on `code_changed`.
2. Existing free-text notes stay as they are. Coverage counts only parks whose ts is at or
   after the first `decisions.jsonl` record (Decision 3), which is where the SPEC-0298
   window begins; no `--since` needs to be passed for that.
3. If SPEC-0300 merges first, rebase the shared `skills/wt-conduct/SKILL.md` edits on it and
   keep its row names for the rule ids.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

- None.
