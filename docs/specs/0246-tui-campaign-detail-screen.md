---
id: SPEC-0246
title: "TUI campaign detail screen: header, filterable ledger timeline, runs, gates and reviews"
status: superseded
superseded_by: SPEC-0407
owner: user
created: 2026-10-25
updated: 2026-10-25
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0169
tags: [agentic, tui, campaign, textual]
source_idea: IDEA-084
depends_on: [SPEC-0241, SPEC-0199, SPEC-0203]
---

## Context

Child of epic [SPEC-0169](./0169-mini-agentic-workflow-queue-campaigns-headless.md), promoted
from `IDEA-084`.

To understand one campaign today the human runs several commands and joins the output by hand:
- `wt campaign show` for the state and the last 5 events;
- `wt campaign log` for the ledger;
- `wt campaign runs` / `run` ([SPEC-0199](./0199-wt-campaign-runs-and-run.md)) for persona runs;
- the gate logs under `<campaign dir>/gates/`;
- `wt spec review` for the spec review.

The queue tab ([SPEC-0241](./0241-tui-queue-tab-over-the-queue-dashboard.md)) posts
`QueueView.CampaignSelected` on `enter` and shows a CLI hint until this screen exists.

## Goals / Non-goals

**Goals**
- G1. A campaign screen, opened from the queue tab or by `wt tui --campaign ID`. It has a header
  with the state, counters, cost, sizing/profile, branch/worktree and park.
- G2. The whole ledger as a timeline that can be filtered by event group. A selected row
  expands into a detail pane: a run's verdict, cost and stderr tail; a gate's per-gate result
  and log tail; a finding; a park; a note.
- G3. A review panel: the last code review (the SPEC-0203 triage score, the verdict, blocking,
  advisory and waived findings) and the latest spec review (verdict, size/tone/rationale,
  findings).
- G4. Live refresh while open, and the queue tab's actions (park, resume, prioritize, abandon)
  on the campaign shown.

**Non-goals**
- Opening files in an editor or pager. Paths are shown as text so they can be copied.
- Editing the ledger, adding notes (`wt campaign note` stays on the CLI), or re-running a gate
  or a persona.
- Rendering prompts or result JSON in full. The detail pane shows the paths and the tails
  `campaign_run` already returns.

## Decision

A pure loader `load_campaign(cfg, cid) -> CampaignDetail` in `src/wt/tui/agentic_model.py`
builds the header lines, the timeline entries, the review panel text and a change signature from
`store.read_state`, `store.read_ledger`, `runs.campaign_runs` / `campaign_run` and
`spec_review.latest_review`. A Textual `CampaignScreen(Screen)` in `src/wt/tui/campaign_screen.py`
renders it. `IdeaDesk` pushes the screen on `CampaignSelected` and for `--campaign`. The action
flow from SPEC-0241 moves into a shared `QueueActions` mixin used by both the queue table and
this screen.

## Design

### Data (`agentic_model.py`)

`CampaignDetail` is a dataclass with `campaign`, `state`, `header` (list of `Text` lines),
`entries` (list of `TimelineEntry`), `review` (`Text`), `actions` (as `allowed_actions`) and
`signature`.

The header lines reuse the facts `wt campaign show` prints (`cli_campaign._summary` plus the
same state keys):
1. `<id>  <state>  spec <spec_id or idea_id> <spec title>  project <p>`. The title comes from
   `queue._spec_title`.
2. `loops <l/max>  runs <n>  cost $<total> (~$<est> est.) / $<cap>`, then
   `profile <name> (<source>)  size <s>  tone <t>` when `state.sizing` is set (SPEC-0219).
3. `branch <b>  worktree <path>`, with ` (missing)` when the directory is gone.
4. When parked: `parked <reason>: <detail> → resume to <resume_to>`. When a park is requested:
   `park requested: <detail>`.
5. When `state.close.followups` has any filed entry: `follow-ups <ids>`, where `<ids>` are the
   distinct `filed[*].idea_id` values in first-seen order (this covers SPEC-0239's group id and
   ids of other open groups, and SPEC-0207's single ideas on older campaigns), followed by
   ` · <u> unfiled · <w> waived · <c> capped` for the non-zero counts (`capped` exists only
   after SPEC-0239). When nothing was filed but some count is non-zero, the line shows only the
   counts.

`TimelineEntry` is a frozen dataclass: `seq`, `ts`, `type`, `group`, `text`, and `ref` (the
run id for run events, the `gate_seq` for gate events, else None). There is one entry per
ledger event, in `seq` order. `text` is `render.campaign_event(ev)` without its seq/ts columns.
Groups:

| group | event types |
|---|---|
| `transitions` | `open`, `transition`, `park`, `resume`, `priority`, `close`, `admitted_after_revision` |
| `runs` | `run.start`, `run.end`, `cost` |
| `gates` | `gate` |
| `findings` | `finding`, `followup` |
| `notes` | `note`, `commit`, `backlog`, and any type not listed |

`entry_detail(cfg, cid, entry) -> Text` builds the detail pane for the selected entry:
- **run** (`run.start` / `run.end`, which carry `run_id`; `cost` events have no `run_id` in
  `schemas.EVENT_KEYS`, so they fall back to the payload detail below):
  - from `runs.campaign_run(cfg, cid, run_id)`: persona, model, effort, status,
    `verdict_status`, duration, cost and charged cost;
  - the verdict `summary` and its `must_fix` / `blockers` lists;
  - the `permission_denials` count and the `stderr_tail` (already capped at
    `runs.STDERR_TAIL_LINES`);
  - the `paths` (prompt, result, stderr, run.json).
- **gate:** each entry of the event's `results` (name, ok, required, exit code, duration), then
  the last 40 lines of each failed gate's log. The log is read from `store.campaign_dir(cfg,
  cid) / result["log"]`, which is relative to the campaign dir (`gates.GateResult.log`).
- **finding** / **followup:** every payload field, with `detail` unclipped.
- **park** / **transition** / **resume** / **note** / **cost** / any other type: every payload
  field unclipped.

A file that cannot be read shows `(missing: <path>)`. A `ValueError` from
`runs.campaign_run` (an unknown or ambiguous run id, for example a ledger `run_id` with no
gathered record) is shown inline as `(run unavailable: <error>)`, followed by the event's own
payload. The pane never raises.

`review_text(cfg, state) -> Text` builds the review panel:
- **Code review.** `state.last_review` persists `verdict`, `score`, `advisory`, `waived`,
  `loop` and the raw per-reviewer `verdicts` (queue.py `_store_verdicts` and the triage
  update), but **not** `pass_score` or the blocking list. Rather than change the persisted
  state, the panel derives both (the smaller change):
  - `profile = profiles.resolve_profile(cfg, state.get("profile"))`; `pass_score` is
    `profiles.review_params(profile)[0]`;
  - the blocking list is `queue.triage_reviews(verdicts, profile, waived=[w["id"] for w in
    last_review.waived])["blocking"]`, where `verdicts` is `last_review.verdicts` in
    `profiles.reviewers_of(profile)` order (the same input SPEC-0235 uses). This is pure and
    reproduces the triage of the stored verdicts.
  - If the profile no longer resolves (`ValueError`), the panel shows `pass ?` and `blocking:
    unavailable (<error>)`, and still shows the stored fields.
  The panel shows `score <score> / pass <pass_score>  verdict <verdict>  loop <loop>`, then the
  blocking (derived), advisory (stored) and waived (stored) findings grouped by severity
  (`must_fix`, `major`, `minor`, `nit`), each as `<id> <severity> <personas> <title>
  (<files>)`; waived findings also show their `reason`.
- **Spec review:** for a campaign with a spec, `spec_review.latest_review(cfg, spec_id,
  spec_path)` with `spec_path = Path(state.repo.root) / state.spec.rel_path` (the main
  checkout, as `followups.parent_epic` and `queue._spec_title` resolve it). It shows the
  verdict, size/tone/rationale (SPEC-0219), the cost and `reviewed_at`, then the findings
  (`severity title`). An idea-only campaign (`queue.has_no_spec(state)`) or a missing
  `repo.root` / `rel_path` reads `no spec review`.
- Either part that is absent reads `no code review yet` / `no spec review`.

`campaign_signature(cfg, cid)` is `(mtime_ns of state.json, size of ledger.jsonl)`.

### Screen (`campaign_screen.py`)

- **Layout:** the header as a `Static` (top), then a `DataTable` timeline (columns `seq`, `time`
  (HH:MM:SS), `type`, `text`, clipped to the width), then a one-line status `Static`
  (`#campaign-status`, under the timeline), then a detail pane (`VerticalScroll`, about 40% of
  the height). The detail pane shows `entry_detail` for the highlighted row, or
  `review_text` while review mode is on.
- **Bindings:**
  - `f` cycles the filter `all → transitions → runs → gates → findings → notes → all`. The
    `#campaign-status` line shows `filter <group> · <shown>/<total> events`.
  - `v` toggles review mode for the detail pane.
  - `g` / `G` jump to the first or last row.
  - `escape` / `q` pop the screen.
  - `p`, `u`, `P`, `A` run the SPEC-0241 actions on this campaign.
- **Opening:** the cursor starts on the last event, so the newest activity shows first.
- **Live refresh:** `set_interval(2.0)` compares `campaign_signature` and reloads in a thread
  worker on a change. The filter is kept. The cursor stays on the same `seq`, or follows the end
  when it was on the last row.
- **Read-only**, like the queue tab: no `events.sync`, cursor or reader lock.
- SPEC-0241's `IdeaDesk.check_action` gating also disables the idea-scoped app actions while
  this screen is shown (the rule is "the default screen is on top and `tab-ideas` is active").
- **Shared actions:** SPEC-0241's prompt → plan → confirm → apply flow moves into
  `QueueActions` (a mixin with `run_queue_action(action, campaign_id, state, actions)`). It is
  used by `QueueTable` and `CampaignScreen` with unchanged behaviour. After an apply the screen
  reloads. After `abandon` it also stays open on the now-terminal campaign.

### Entry points

- `IdeaDesk` handles `QueueView.CampaignSelected` by pushing `CampaignScreen(cfg, cid)`. This
  replaces the SPEC-0241 hint. Popping the screen returns to the queue tab with its cursor where
  it was.
- `wt tui --campaign ID`:
  - validates the id with `store.read_state` before the app starts; an unknown id is a
    `ClickException` and no TUI opens;
  - otherwise activates the queue tab and pushes the screen in `on_mount`;
  - accepts a unique prefix of the id, resolved against `store.list_campaigns(cfg,
    include_terminal=True)`; an ambiguous prefix is an error that lists the matches.

## Alternatives considered

- **A modal over the queue tab.** Rejected: the timeline needs the full height, and a pushed
  `Screen` keeps its own bindings without colliding with the desk's.
- **Separate tabs for timeline, runs, gates and review inside the screen.** Rejected: the
  ledger already has run and gate events in order. Filtering one timeline and expanding a row
  shows the same facts without another level of navigation. Only the review summary, which is
  not a ledger event, gets its own mode (`v`).
- **Reading the gate logs for every gate event up front.** Rejected: the logs are read only for
  the highlighted entry, so a campaign with many gate runs opens instantly.

## Acceptance criteria

- [x] AC1. `enter` on a queue row pushes the campaign screen, and Esc returns to the queue tab
      with the cursor unchanged. `wt tui --campaign <id or unique prefix>` opens the screen
      directly. An unknown or ambiguous id exits with an error before the TUI starts.
- [x] AC2. The header shows the five documented lines when their facts exist. A seeded campaign
      with sizing, a park and a missing worktree shows every line, and a fresh campaign shows
      lines 1–3.
- [x] AC3. The timeline lists every ledger event in `seq` order. `f` cycles the groups with the
      documented membership. The status line shows the filter and `<shown>/<total>`. The cursor
      starts on the last event.
- [x] AC4. The detail pane shows, for a run, the `campaign_run` fields, verdict lists, stderr tail
      and paths. For a gate it shows the per-gate results and the log tail of each failed gate.
      For findings, parks, transitions, notes and cost events it shows their payloads unclipped.
      A missing run directory or gate log shows `(missing: …)`, and a `campaign_run`
      `ValueError` shows `(run unavailable: …)`, without an exception.
- [x] AC5. `v` shows the review panel: the stored score and verdict, the pass score from
      `review_params`, the blocking findings derived with `triage_reviews` from the stored
      verdicts, and the stored advisory and waived findings grouped by severity, with waived
      reasons (and the `unavailable` fallback for a profile that no longer resolves), and the latest spec review (verdict,
      size/tone/rationale, findings). When either review is absent, it shows the documented
      placeholder.
- [x] AC6. A ledger append while the screen is open adds the row within one poll interval with
      no key press. The filter is kept, and the cursor stays on its `seq` or follows the end.
      The screen never calls `events.sync`, `cursor_set` or `reader_lock` (monkeypatched to
      raise in the test).
- [x] AC7. `p`/`u`/`P`/`A` on the screen run the same flow as on the queue tab, through the shared
      `QueueActions`. The SPEC-0241 queue tab tests still pass unchanged, and the header
      reflects the new state after an apply.
- [x] AC8. Full `uv run pytest` is green and `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests:**
  - `tests/test_tui_campaign_model.py` (new, no Textual):
    - `load_campaign` header lines for the fresh, sized, parked and missing-worktree cases;
    - group membership for every `schemas.EVENT_KEYS` type plus an unknown type;
    - `entry_detail` for a run with a run dir (prompt/result/stderr written by the test), a run
      without one, a `run.end` whose run id `campaign_run` cannot resolve, a `cost` event, a
      gate with one failed gate whose log exists and one whose log is missing, and a finding;
    - the follow-ups header line for a SPEC-0207-style summary (several single ids) and for
      counts only;
    - `review_text` with a `last_review` whose stored `verdicts` yield a blocking finding
      through `triage_reviews` plus stored advisory and waived findings, with a profile that no
      longer resolves, with a stored spec review (`spec_review.save_review`), for an idea-only
      campaign, and with neither;
    - `campaign_signature` changes on a ledger append.
  - `tests/test_tui_campaign_screen.py` (new, `needs_textual`):
    - open from the queue tab and Esc back with the cursor kept;
    - `--campaign` with a full id, a prefix, an unknown id and an ambiguous id (CLI runner,
      `T.run` patched for the valid cases);
    - `f` cycling and the status counts;
    - `v` toggling;
    - live append;
    - one action (park) end to end through the screen;
    - the forbidden-call monkeypatches.
  - `tests/test_tui_queue_tab.py` unchanged and green (the shared mixin refactor).
- **Manual verification:** `uv run wt tui --campaign <a merged campaign>` on the live data dir.
  Filter to `gates`, open a failed gate, toggle `v`, and record what was seen here.
- **Regression guard:** full `uv run pytest`.

### Test record (2026-10-26)

- `uv run pytest -q tests/test_tui_campaign_model.py` — 14 passed.
- `uv run pytest -q tests/test_tui_campaign_screen.py` — 8 passed (open/Esc with cursor kept,
  `--campaign` full id / prefix / unknown / ambiguous, `f` cycling and status counts, `v`,
  live append with `events.sync` / `cursor_set` / `reader_lock` patched to raise, park and
  abandon end to end through the screen, idea keys inert).
- `uv run pytest -q -x tests/test_tui_queue_tab.py tests/test_tui_queue_model.py
  tests/test_tui_entry.py` — passed after the mixin refactor, except the one test that asserted
  the SPEC-0241 CLI hint (see As shipped).
- Manual: `load_campaign` / `entry_detail` / `review_text` run against the live data dir on a
  merged merge-request campaign (header lines 1–3, 7 events, a passing gate with per-gate
  results) and on a parked implement campaign with a failed gate (pytest log tail shown, the
  `gate_triager` run with verdict lists and paths, review panel `score 92 / pass 80`, 2
  advisory and 6 waived findings with reasons). The interactive `wt tui --campaign` was not
  driven by hand; the pilot tests cover the screen.
- Full `uv run pytest -q -x` — 2840 passed, 3 skipped on the re-run after the ledger fix; the first run stopped at
  `test_internal_spec_lint_unaffected_by_outbox` (`docs/LEDGER.md` stale until
  `--write-ledger` was run).
- `python3 tools/spec_lint.py --write-ledger` then `python3 tools/spec_lint.py` — exit 0.

### As shipped (2026-10-26)

- `tests/test_tui_queue_tab.py::test_enter_posts_campaign_selected_and_shows_the_cli_hint`
  asserted the SPEC-0241 CLI hint, which this spec replaces; it is renamed
  `…_opens_the_campaign_screen` and now asserts the pushed `CampaignScreen`. No other queue tab
  test changed.
- `QueueActions` lives in `queue_tab.py`; `run_queue_action(action, campaign_id, state,
  actions)` starts the worker, and the priority picker's current value is read from
  `store.read_state` instead of the queue row.
- `CampaignScreen` stores the visible rows as `shown` (`Widget.visible` is taken).
- `TimelineEntry` also carries the raw `event` dict (excluded from equality) so `entry_detail`
  needs no second ledger read.
- `IdeaDesk(campaign=ID)` forces the queue tab and pushes the screen in `on_mount`; the id /
  prefix resolution is done in `wt tui` (`cli.py`) against `store.list_campaigns(include_terminal=True)`.
- `IdeaDesk.check_action` disables the idea-scoped actions whenever a `CampaignScreen` is in the
  screen stack (modals from other screens keep the old behaviour).
- The `must_fix` / `blockers` items of a run verdict print as the stored values (a dict prints
  as its repr).

## Clock Log

Built by the agentic loop or the human lane; idea bookkeeping via `wt spec reconcile --apply`.

## Rollout / migration

Nothing to migrate. The screen is available on the next `wt tui` launch with the `tui` extra.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

_None — decisions locked above._
