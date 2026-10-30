---
id: SPEC-0299
title: "Conductor cost measurement: session registry, `wt queue conductor-cost`, conductor share in the export"
status: accepted
owner: user
created: 2026-10-30
updated: 2026-10-30
milestone: "M10: agentic MVP"
kind: feature
parent: SPEC-0298
tags: [agentic, conductor, cost, transcripts]
---

## Context

wt records every persona run's cost in the campaign ledger and `wt campaign export`
([SPEC-0236](./0236-wt-campaign-export-dataset-for-mis-sizing-analysis.md)) but knows
nothing about the conductor's own spend. The [SPEC-0298](./0298-agentic-mvp-cheap-durable-quiet-conductor.md)
baseline was computed by hand. One conductor session over 5 days took 2,386 turns and read
1.09B cache tokens, about $263 at list price against $645 of persona spend, with 83% of
the conductor's cost in cache reads. The MVP exit criteria E1 (≤ $1.00 conductor cost per
merged campaign) and E2 (median context ≤ 150k per turn) need this number from a command,
not from a notebook.

What exists:
- Claude Code writes one transcript per session at `<transcripts_root>/<slug>/<session>.jsonl`
  (`transcripts_root` defaults to `~/.claude/projects`, and `config.load` exposes it as
  `cfg["_root"]`). Sub-agents write under `<slug>/<session>/subagents/`.
  `ingest.load_streams` already walks these files tolerantly for time tracking, but it keeps
  only timestamps and kinds, not usage.
- Each `assistant` line carries `message.id`, `message.model` and `message.usage`
  (`input_tokens`, `output_tokens`, `cache_read_input_tokens`,
  `cache_creation_input_tokens`, and `cache_creation.ephemeral_5m_input_tokens` /
  `ephemeral_1h_input_tokens`). One API message is split over several lines (one per
  content block) that repeat the same `message.id`.
- Any process Claude Code launches, including a Monitor's `wt queue events --follow --for
  conductor`, inherits `CLAUDE_CODE_SESSION_ID` (the transcript's file stem) and
  `CLAUDE_PID`. This was checked on 2026-10-30: the variable equals the session's
  `<session>.jsonl` stem.
- The follower (`events.follow`) takes the `conductor` reader lock (`events.reader_lock`),
  so exactly one process knows it is the conductor's.

## Goals / Non-goals

**Goals**
- wt learns which Claude Code session(s) are the conductor without the conductor doing
  anything.
- `wt queue conductor-cost` reports the conductor's tokens by kind and dollars at configured
  per-model prices, per day and per merged campaign, plus the context size per turn.
- `wt campaign export` carries the conductor's share per campaign, so cost per merge
  includes the conductor.

**Non-goals**
- Steward session cost. The registry records any reader's session, so a later spec can add
  it, but no command reports it here.
- Shipping a built-in price list. Prices are configuration (see Decision).
- Changing persona cost accounting.
- Live per-turn metering inside the conductor session.

## Decision

1. **Session registry.** When `events.follow` acquires a reader lock and
   `CLAUDE_CODE_SESSION_ID` is set, it appends `{ts, reader, session_id, claude_pid, cwd,
   source: "follower"}` to `<agentic_dir>/conductor/sessions.jsonl`, unless that
   `(reader, session_id)` pair is already there. `wt queue conductor-session add <id>`
   backfills a session by hand (`source: "manual"`), for the pre-existing 5-day session.
   `wt queue conductor-session list` shows the registry. A failure to record never stops the
   follower.
2. **Transcript usage reader.** A new reader parses a session transcript into API turns, one
   per `message.id`, keeping the last line seen for each id. Lines that are not
   `assistant`, have no usage, fail to parse, or come from the `<synthetic>` model are
   skipped. The main transcript is the conductor. Sub-agent transcripts are reported
   separately with `--include-subagents` and are never mixed into the headline figure:
   dispatched sub-agents are delegated work, not conductor context.
3. **Prices are configuration.** `agentic.conductor.prices` maps a model id, or a prefix of
   one, to USD per million tokens: `{input, output, cache_read, cache_write_5m,
   cache_write_1h}`. The longest matching prefix wins. A turn whose model has no price gets
   `usd: null`, and the model is listed in `unpriced_models`. The report still prints
   tokens. No built-in table ships, because a stale hard-coded price would be worse than
   an explicit gap. The rollout step sets the user's config from the published price list.
   Dollar aggregates (`totals.usd`, `usd_by_kind`, `idle_usd`, `by_day[].usd`, each
   campaign's `conductor_usd`, `usd_per_merge` and the export's `conductor_usd`) sum priced
   turns only. When `unpriced_models` is non-empty, `totals.usd_complete` is false and the
   text form appends `(partial: N unpriced turns)`. `usd_per_merge` is null when
   `merges == 0`.
4. **Attribution.** A conductor turn at time *t* is split equally among campaigns that are
   *live* at *t*. A campaign is live from its `open` ledger event (or `created_at`) until its
   terminal transition, the same window `campaign_export.campaign_row` uses for
   `wall_s`. A turn with no live campaign is `idle`. The headline **cost per merge** is total
   conductor USD in the window ÷ campaigns merged or handed off in the window, idle
   included. The per-campaign share is the attribution.

## Design

New module `src/wt/agentic/conductor_cost.py` (pure functions over files), plus its CLI in
a new `src/wt/agentic/cli_conductor.py` whose `attach(queue_group)` is registered in
`src/wt/agentic/cli.py` beside `cli_conduct.attach`.

- `conductor_dir(cfg) -> Path`: `store.agentic_dir(cfg) / "conductor"`, created on write.
- `record_session(cfg, reader, env=os.environ, *, source="follower") -> dict | None`: an
  idempotent append under a flock on `sessions.jsonl.lock`, the same pattern as
  `store.append_event`. It returns the record, or None when the env var is unset or the pair
  is already recorded. It is called from `events.follow` right after
  `with reader_lock(cfg, reader):`, wrapped in `try/except Exception: pass`.
- `sessions(cfg, reader="conductor") -> list[dict]` in first-seen order.
- `find_transcript(cfg, session_id) -> Path | None`: the first match of
  `Path(cfg["_root"]).glob(f"*/{session_id}.jsonl")`. `subagent_transcripts(cfg,
  session_id)` globs `*/{session_id}/subagents/**/*.jsonl`.
- `read_turns(path) -> list[Turn]`: `Turn = {ts, model, msg_id, input, output, cache_read,
  cache_write_5m, cache_write_1h, context}`. When the `cache_creation` breakdown is absent,
  `cache_creation_input_tokens` counts as `cache_write_5m`. `context = input + cache_read +
  cache_write_5m + cache_write_1h`, the prompt size of that turn.
- `price_for(cfg, model) -> dict | None` and `turn_usd(turn, price) -> float | None`.
- `live_windows(cfg, *, now) -> list[(campaign, spec_id, start, end|None, outcome)]` from
  `store.list_campaigns(cfg, include_terminal=True)` and
  `store.read_ledger(cfg, cid, types=["open", "transition"])`.
- `cost_report(cfg, *, since=None, until=None, session_ids=None, include_subagents=False,
  now=None) -> dict`: the `wt.conductor.cost.v1` payload:
  `{sessions: [{session_id, path, found, turns}], window: {since, until},
  totals: {turns, input, output, cache_read, cache_write_5m, cache_write_1h, usd,
  usd_complete, usd_by_kind: {...}}, unpriced_models: [...], context: {median, p90, max,
  last}, by_day: [{date, turns, usd, cache_read, merges}], merges, usd_per_merge, idle_usd,
  persona_usd, campaigns: [{campaign, spec_id, outcome, conductor_usd, persona_usd}],
  subagents: {...} | null}`. Days are local dates in `cfg["_tz"]`. `persona_usd` sums
  `state.cost.total_usd` of campaigns that ended in the window. `merges` counts campaigns
  whose terminal transition to MERGED or HANDED_OFF falls in the window.
- CLI: `wt queue conductor-cost [--since YYYY-MM-DD] [--until YYYY-MM-DD] [--session ID]…
  [--include-subagents] [--json]`. `--since` and `--until` are local dates in `cfg["_tz"]`,
  both inclusive. A turn or campaign end is in the window when its local date d satisfies
  since ≤ d ≤ until. Omitted bounds are open. The text form is a totals line ("N turns · $X · median
  context Yk · $Z per merge (M merges)"), a by-day table and the top 10 campaigns by
  conductor share. `wt queue conductor-session add ID [--reader conductor]` validates that
  the transcript exists (a warning, not a refusal, if it does not) and appends with
  `source: "manual"`. `wt queue conductor-session list [--json]`.
- Export: `campaign_export.CAMPAIGN_COLUMNS` appends `("conductor_usd", "float")` and
  `("cost_with_conductor_usd", "float")`, which is allowed under v1 because columns are only
  appended. `export_tables` calls a new `conductor_cost.attribute(cfg, now)` once, a
  campaign-id → USD map over all registered conductor sessions. Both columns are `None` when
  no session is registered or every turn is unpriced. When at least one session is
  registered and some turn is priced, a campaign with no attributed turn gets
  `conductor_usd = 0.0`. `cost_with_conductor_usd = (cost_usd or 0) + conductor_usd`, and
  it is None exactly when `conductor_usd` is None. A `NOTES` entry explains the equal
  split.

## Alternatives considered

- **Learn the session from the newest transcript in the cwd's slug.** Wrong as soon as two
  sessions share a checkout, which is the normal case (conductor plus stewards plus the
  human).
- **Have the conductor run a `register` command.** It depends on the agent remembering to
  run it after every restart. The follower already runs in the conductor's process tree on
  every arm.
- **Attribute turns by the event that woke them.** The transcript does not link a turn to
  the follower line reliably, and the equal live-campaign split is enough for cost per merge.
- **A built-in price table.** Rejected; see Decision 3.

## Acceptance criteria

- [ ] Arming `events.follow` with `CLAUDE_CODE_SESSION_ID=abc` set appends exactly one
      `sessions.jsonl` record for `(conductor, abc)`. Re-arming does not append again.
      Without the variable nothing is written, and a raising `record_session` does not stop
      the follower.
- [ ] `read_turns` returns one turn per `message.id` (the last line wins), skips malformed,
      non-assistant and `<synthetic>` lines, and computes each token kind and `context` as
      specified.
- [ ] Prices resolve by longest model-id prefix from `agentic.conductor.prices`. An unpriced
      model yields `usd: null` for its turns and is listed in `unpriced_models`, and the token
      totals still include it.
- [ ] `wt queue conductor-cost --json` emits `wt.conductor.cost.v1` with by-day totals,
      context median/p90, the equal live-campaign split (shares sum to total minus
      `idle_usd`), `merges` and `usd_per_merge`. `--session` overrides the registry.
      Sub-agent transcripts are counted only under `--include-subagents`, and only in
      `subagents`.
- [ ] With a mix of priced and unpriced turns, every dollar aggregate sums priced turns only,
      `totals.usd_complete` is false, the text form appends `(partial: N unpriced turns)`,
      and `usd_per_merge` is null when `merges == 0`. `--since` and `--until` are inclusive
      local dates in `cfg["_tz"]`, and `merges` counts MERGED and HANDED_OFF campaigns.
- [ ] `wt campaign export` campaigns rows end with `conductor_usd` and
      `cost_with_conductor_usd` (= `(cost_usd or 0) + conductor_usd`). Both are null when no
      session is registered or no turn is priced; otherwise a campaign with no attributed
      turn has `conductor_usd == 0.0`. The earlier v1 columns keep their order.
- [ ] `wt queue conductor-session add|list` work as specified, and the `/wt-conduct` skill
      names `wt queue conductor-cost` in its *Reporting* step (a daily one-liner: turns, $,
      $/merge, median context).

## Test plan

- **Automated tests** in `tests/test_agentic_conductor_cost.py`. Fixtures write a tiny
  transcript under a temp `cfg["_root"]/-slug/<sid>.jsonl`, and campaigns go into a temp
  agentic store with `store.create_campaign` / `append_event` (open, transition to MERGED).
  - `test_follow_records_session_once`: append one event, then run
    `events.follow(cfg, "conductor", from_start=True, max_events=1, code_root=None)` twice
    with `env` patched (a fresh reader's cursor starts at the last event id, so without
    `from_start=True` or `stop=lambda: True` the call would wait forever). There is one
    record, with reader `conductor` and `source: "follower"`.
  - `test_follow_without_session_env_records_nothing` and
    `test_record_failure_never_breaks_follow` (monkeypatch `record_session` to raise; append
    one event and run `events.follow(cfg, "conductor", from_start=True, max_events=1,
    code_root=None)`, or pass `stop=lambda: True`; the follower still prints the event).
  - `test_read_turns_dedupes_by_message_id`: three lines with one id plus one other id give
    2 turns, with the last line's usage for the first id.
  - `test_read_turns_kinds_and_context`: the 5m/1h split, the fallback to
    `cache_creation_input_tokens`, and context arithmetic. Malformed, `user` and
    `<synthetic>` lines are skipped.
  - `test_price_longest_prefix` and `test_unpriced_model_reported`. The latter mixes priced
    and unpriced turns and asserts that the dollar aggregates sum priced turns only,
    `totals.usd_complete` is false, the text form appends `(partial: N unpriced turns)`,
    token totals still include the unpriced turns, and `usd_per_merge` is null when
    `merges == 0`.
  - `test_cost_report_split_and_per_merge`: two campaigns live over overlapping windows and
    turns before, during and after them, including a turn on the `--until` date, which must
    be counted (both bounds inclusive, local dates in `cfg["_tz"]`). The shares,
    `idle_usd`, `merges == 2` and `usd_per_merge` equal hand-computed values. `by_day` uses
    `cfg["_tz"]`.
  - `test_subagents_only_with_flag`.
  - `test_cli_conductor_cost_json_and_text`, using CliRunner on both forms.
  - `test_conductor_session_add_list`.
- `tests/test_agentic_campaign_export.py`: `test_export_conductor_columns_appended`, where
  the last two columns are the new ones and the values are null without sessions and set
  with one. With a session and a priced turn it also asserts that a campaign with no
  attributed turn gets `conductor_usd == 0.0`, and that a campaign whose `cost_usd` is None
  gets `cost_with_conductor_usd == conductor_usd`.
- `tests/test_skills.py`: `test_conduct_skill_mentions_conductor_cost`.
- **Sabotage checks:**
  - Count every transcript line instead of deduping by `message.id`.
    `test_read_turns_dedupes_by_message_id` must fail.
  - Attribute each turn wholly to the first live campaign.
    `test_cost_report_split_and_per_merge` must fail.
  - Insert the new export columns before `signals`.
    `test_export_conductor_columns_appended` must fail.
- **Manual verification:** `wt queue conductor-session add <the 5-day session id>`, then
  `wt queue conductor-cost --since 2026-10-25 --until 2026-10-30` with list prices
  configured. Total turns are about 2,386, cache reads about 1.09B and USD about $263. Note
  the figures in *As shipped*.
- **Regression guard:** full `uv run pytest`. `tests/test_agentic_followers.py` is
  unchanged. In `tests/test_agentic_campaign_export.py`, only `test_columns_and_key_order`
  and `test_column_types_pinned` change, and only by appending `"conductor_usd"` /
  `("conductor_usd", "float")` and `"cost_with_conductor_usd"` /
  `("cost_with_conductor_usd", "float")` after `signals`. No other existing assertion
  changes.

## Rollout / migration

1. Merge. The next conductor re-arm records its session automatically.
2. Set `agentic.conductor.prices` in the user's config from the published list prices for
   the models in use.
3. Backfill the current session with `wt queue conductor-session add`.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

- None.
