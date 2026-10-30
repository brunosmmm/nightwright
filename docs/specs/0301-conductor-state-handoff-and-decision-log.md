---
id: SPEC-0301
title: "Conductor state handoff and decision log: `wt queue decision`, `wt queue conductor-brief`, compaction rule"
status: accepted
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
   - `rule` is the decision-table row id: the event `type` (`runner_dead`,
     `conductor_digest`, …) or `parked.<reason>` for park rows (`parked.cost_cap`). It is
     validated against `^[a-z_]+(\.[a-z_]+)?$`. The table's first cell is the id, so the
     skill and the log share one vocabulary.
2. **One write command:** `wt queue decision [CAMPAIGN] --kind K --rule R [--event N]
   [--action "…"] [--by conductor] RATIONALE`. It applies at once, with no dry run,
   because it is a pure append like `wt campaign note`. With a campaign it also appends a
   ledger `note`, `{text: "decision <kind> (<rule>): <rationale>", author: <by>,
   decision: {seq, kind, rule}}`, so `park_view`, `wt campaign log` and the TUI see it
   unchanged. `wt campaign note` stays for non-decision remarks.
3. **Listing:** `wt queue decisions [--campaign C] [--since D] [--tail N] [--parks] [--json]`.
   `--parks` lists every `park` ledger event in the window with the first decision record
   on that campaign after it, or `missing`, and a coverage ratio.
4. **Brief:** `wt queue conductor-brief [--json]` (`wt.conductor.brief.v1`), built only from
   files. Each list has a fixed cap, with `+N more` when cut. The text form is at most
   12,000 characters (about 3k tokens) and is truncated from the least important section
   upward. It contains:
   - `runner`: held, pid, heartbeat age, draining reason.
   - `conductor`: follower pid, `session_id` and `context_tokens` (the last turn's context
     from the SPEC-0299 reader), `compact_at`, and `compact_now`.
   - `counts`.
   - `parked` (cap 20): campaign, label, reason, a 200-char detail, `parked_for_s`, owner,
     `last_decision` {kind, rule, a 200-char rationale, ts} or null, and the open
     escalation id.
   - `active` (cap 20): campaign, label, state, loops, cost.
   - `open_dispatches`: `dispatch` decisions with no later decision on the same campaign,
     or on the queue for campaign-less ones.
   - `recent_decisions` (last 10).
   - `unanswered_mail` (cap 10, 160-char text).
   - `escalations_open` (cap 10).
   - `standing`: the path of the installed `/wt-conduct` skill, the configured
     `agentic.conductor.memory_paths` and the configured `agentic.conductor.standing_rules`
     strings.
5. **Compaction rule.** `agentic.conductor.compact_at_tokens` defaults to 150000. The skill
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
    `events.read_since(cfg, event_id - 1)`. It appends under a flock with the next `seq`
    and, with a campaign, the ledger note via `store.append_event(cfg, cid, "note", …,
    by=f"agent:{by}")`.
  - `read(cfg, *, campaign=None, since=None) -> list[dict]`.
  - `park_coverage(cfg, *, since=None) -> dict`: for each campaign from
    `store.list_campaigns(cfg, include_terminal=True)`, each `park` in
    `store.read_ledger(cfg, cid, types=["park"])` is matched to the first record with the
    same campaign and `ts >=` the park's ts. Returns `{parks, covered, missing: [...],
    ratio}`.
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
  - A sentence above the decision table: "the first cell of each row is its rule id; a
    park row's id is `parked.<reason>`".

## Alternatives considered

- **A `conductor_decision` event in `events.jsonl`.** The conductor's own follower would
  wake on its own writes, which is exactly the noise SPEC-0300 removes. A separate log plus
  a ledger note gives the audit trail without the echo.
- **Extend `conduct-status`.** Its JSON is consumed as is (`wt.conduct.status.v1`, the TUI,
  tests). A separate, size-capped brief keeps that contract and has one job: re-orient.
- **`wt campaign note --decision`.** It cannot record queue-level decisions (runner
  restarts) with no campaign, so a queue-level command covers both.

## Acceptance criteria

- [ ] `wt queue decision` validates kind, rule, campaign and event id (each bad value exits
      1 with a clear message and writes nothing). It appends a `wt.conductor.decision.v1`
      record with a monotonically increasing `seq` and, with a campaign, a ledger `note`
      carrying `decision`, after which `park_view(...)["decided"]` is true.
- [ ] `wt queue decisions --json` lists records filtered by campaign or since. `--parks`
      reports each park with its covering decision or `missing`, plus `ratio`.
- [ ] `wt queue conductor-brief --json` (`wt.conductor.brief.v1`) contains runner,
      conductor, counts, parked items with `last_decision` and open escalation, active
      items, `open_dispatches`, `recent_decisions`, unanswered mail, open escalations and
      `standing` pointers, with every list capped and the cut reported as `+N more`.
- [ ] The text brief is at most 12,000 characters for a store with 60 parked and 60 active
      campaigns and 500 decisions, and parked items come before recent decisions when
      truncating.
- [ ] `conductor.context_tokens` is the last turn's context of the newest registered
      conductor session, and `compact_now` is true exactly when it exceeds
      `compact_at_tokens`. With no session or transcript both are null or false and the
      command still succeeds.
- [ ] The `/wt-conduct` skill re-orients from `conductor-brief`, records decisions and
      dispatches with `wt queue decision`, states the rule-id convention, and contains the
      compaction rule with the 150k default.

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
    park → `missing`, `ratio == 2/3`.
  - `test_open_dispatches`: a dispatch followed by a resume on the same campaign is closed.
    A lone dispatch is open.
  - `test_brief_contents_and_caps`: every key is present, `parked[i].last_decision` is
    populated, and lists over their cap report `+N more`.
  - `test_brief_text_size_bound`: 60/60/500 fixture → `len(text) <= 12000`, and the
    `parked` header is present.
  - `test_brief_context_from_transcript`: a SPEC-0299 session fixture whose last turn has
    context 160k → `compact_now` is true. At 140k → false. With no session → null and false.
  - `test_cli_decision_decisions_brief` (CliRunner, JSON and text).
- `tests/test_skills.py`: `test_conduct_skill_uses_brief_and_decisions` and
  `test_conduct_skill_has_compaction_rule`.
- **Sabotage checks:**
  - Skip the ledger note. `test_decision_writes_log_and_ledger_note` must fail, because the
    park is not decided.
  - Match coverage records by campaign only, ignoring ts. `test_park_coverage` must fail.
  - Remove the truncation. `test_brief_text_size_bound` must fail.
- **Manual verification:** on the live queue, run `wt queue conductor-brief` and check that
  the output is under 12k characters and lists every parked item with its last decision.
  Then compact the live conductor once per the rule, re-orient from the brief, and note in
  *As shipped* that no `park_undecided` fired in the next 15 min (exit criterion E6).
- **Regression guard:** full `uv run pytest`. `tests/test_agentic_conduct_cli.py` (the
  `conduct-status` contract) and the SPEC-0222 `park_view` tests are unchanged.

## Rollout / migration

1. Merge after SPEC-0299. The conductor re-reads the skill on `code_changed`.
2. Existing free-text notes stay as they are. Coverage counts only parks that start after
   the merge, which is where the SPEC-0298 window begins.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

- None.
