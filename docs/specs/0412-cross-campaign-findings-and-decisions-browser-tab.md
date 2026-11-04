---
id: SPEC-0412
title: "TUI Findings tab: cross-campaign browser of every finding, decision, mail and review record over nw.history.record.v1"
status: proposed
owner: user
created: 2026-11-03
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0405
tags: [tui, agentic, observability, findings, textual]
source_idea: IDEA-889
---

## Context

Child 7 of epic [SPEC-0405](./0405-rich-queue-tui-prioritized-live-queue-campaign.md) (rich
queue TUI), promoted from `IDEA-889`. The human asked for "a navigable history of all findings
and decisions by all agents that record findings and decisions".

**What exists today.** Findings, decisions and mail are scattered over many sources, and the
TUI shows only slices of them:

- per-campaign `ledger.jsonl` (`finding`, `followup`, `human_decision`, `spec_review`, `note`,
  and, once epic child 1 lands, `triage`);
- per-run `run.json` verdicts (`must_fix`, `blockers`, triager `waive`);
- `conductor/decisions.jsonl`, `escalations/ESC-NNNN.json`, `mailbox/<inbox>.jsonl`;
- spec reviews (`reviews/`, `spec-reviews.jsonl`), `flaky-tests.json` and
  `sentinel/<project>.last.json`.

The desk (`src/nightwright/tui/app.py`, `IdeaDesk`) has tabs 1 ideas, 2 hub, 3 queue,
4 mailbox ([SPEC-0249](./0249-tui-mailbox-tab.md)) and 5 events
([SPEC-0251](./0251-tui-events-tab.md)). The mailbox tab shows mail only, the events tab shows
the alert stream only, and the campaign screen
([SPEC-0246](./0246-tui-campaign-detail-screen.md)) shows one campaign's ledger only. Nothing
lets the human ask a cross-campaign question such as "every `major` finding the reviewer raised
on project X this month" or "what did the conductor decide about escalations last week".

The epic's shared history layer (`agentic/history.py`, epic child 2) already turns all those
sources into one flat stream of `nw.history.record.v1` records (`id`, `ts`, `source`, `kind`,
`agent`, `campaign`, `spec_id`, `project`, `severity`, `title`, `text`, `status`, `links`,
`linked_by`) and exposes the same filtered query on the CLI as `nw campaign records`. The
campaign detail popup (`CampaignDetail`, epic child 5) is the single place a campaign is
explored in depth. This spec adds the tab that lists the records and hands off to that popup.

This spec depends on epic child 2 (history layer, record cache and fixture), child 3 (TUI test
harness) and child 5 (detail popup). Those children have no spec ids at the time of writing,
so the dependency is recorded here in prose and in the epic's breakdown. Implementation starts
once they are `done`.

## Goals / Non-goals

**Goals**
- G1. A desk tab 7, `findings`, listing **every** `nw.history.record.v1` record, newest first,
  from every source the history layer reads, including records that belong to no campaign.
- G2. A filter bar with the keys `agent: kind: project: severity: status: campaign: since:
  until:` plus free text that does a full-text search over record title and text.
- G3. A record pane showing the full record and its links (run, resolving decision,
  follow-up idea, escalation, thread, campaign).
- G4. Three navigation keys: `c` opens the campaign popup focused on the record, `r` opens it
  on the producing run, `d` goes to the record that resolved this one.
- G5. Strictly read-only, fully keyboard-drivable, and inside the epic's performance budget
  (query over 50 000 records in at most 200 ms).

**Non-goals**
- Any write path: no triage, answering, waiving, or editing from this tab. The popup's
  existing actions (park, resume, prioritize, abandon, SPEC-0250 answer flow) stay in the
  popup and are not re-exposed here.
- New record sources, schema fields, or changes to `history.py`. If a field is missing, this
  spec lives with it and shows `-`.
- The History tab (closed campaigns, epic child 6) and its tab binding `6`.
- Saved filters, export from the TUI (`nw campaign records --json` covers that), and charts.

## Decision

Add `FindingsView`, a new widget in `src/nightwright/tui/findings_tab.py` mounted as desk tab
`tab-findings` and bound to `7`. It follows the shape of the events tab
([SPEC-0251](./0251-tui-events-tab.md)): a `DataTable` plus a filter `Input`, with a side pane.
All data comes from `history.py`'s record query (the function behind `nw campaign records`);
the tab never parses ledgers, run files or logs itself. Loading and querying run in Textual
workers, never on the event-loop thread ([SPEC-0129](./0129-tui-poll-reload-off-the-event-loop-thread.md)).
Untrusted text is always rendered as `rich.text.Text`, never as markup
([SPEC-0265](./0265-operator-surfaces-survive-bad-data-and-never.md)).
The tab is read-only: its only filesystem writes are the history layer's disposable cache under
`cache_dir`.

Judgment calls made here (recorded so implementers do not re-ask):

- **Filter grammar** is a small `key:value` token parser in the style of the events tab, as a
  pure function `parse_record_filter(text) -> RecordFilter` that is unit-tested without
  Textual. It does not reuse the History tab's parser, so the two tabs can land in either
  order.
- **`d` stays inside the tab.** It moves the list cursor to the resolving record (clearing
  filters that hide it), because the resolving decision is itself a record and may have no
  campaign. `c` and `r` open the popup.
- **Pagination by page of 500 rows**, not by a virtual table, to keep the DataTable simple.
  The query itself covers all matches; only rendered rows are paged.

## Design

### Module and wiring

- `src/nightwright/tui/findings_tab.py`: `FindingsTable(DataTable)`, `RecordFilterInput(Input)`,
  `FindingsView(Horizontal)` (table on the left, record pane on the right; the pane drops
  below the table under 120 columns, as the events tab does for narrow terminals), and
  `parse_record_filter` / `RecordFilter`.
- `src/nightwright/tui/app.py`: add `TabPane("findings", id="tab-findings")` yielding
  `FindingsView(self.cfg, id="findings-view")`, `Binding("7", "tab('tab-findings')",
  "findings", show=False)`, and extend `_tab_activated` so `activate()` / `deactivate()` is
  called on the findings view exactly as for the events and mailbox views (every other tab
  activation deactivates it). `IdeaDesk(tab="findings")` must be accepted like the other tab
  names.
- No change to `history.py`, `agentic_model.py` write paths, or any ledger/log writer.

### Rows and columns

Rows come from the history record query, ordered `ts` descending with ties broken by record
`id` descending (stable, so rows never swap places). Columns:

| Column | Content |
|---|---|
| time | `ts` as local `MM-DD HH:MM` |
| kind | `kind` (finding, verdict, triage, decision, escalation, answer, human_decision, mail, followup, spec_review, close_check, note, flaky, sentinel), colour-coded |
| agent | `agent` (persona, `conductor`, `steward:<P>`, `human`, `queue`) |
| sev | `severity`, or `-`; `blocker`/`major` highlighted |
| status | `status` or `-` |
| campaign | campaign id shortened as in the queue tab, or `-` when the record has none |
| title | `title`, truncated to the free width |

A record with no campaign is listed like any other and shows `-` in the campaign column. A
record with an unknown kind or source (a future history version) is shown as-is, never
dropped and never a crash.

### Filter grammar

One input (`f` or `/` opens it, Enter applies, Esc closes and keeps the last applied filter,
`F` clears it) holds whitespace-separated tokens:

| Token | Matches |
|---|---|
| `agent:X` | `agent == X`, or `agent` starts with `X:` (so `agent:steward` matches `steward:Meta-Tools`) |
| `kind:X` | `kind == X` |
| `project:X` | `project == X`, case-insensitive |
| `severity:X` | `severity == X`; `severity:none` matches records with no severity |
| `status:X` | `status == X`; `status:none` matches records with no status |
| `campaign:X` | `campaign` contains `X` (an id or a unique fragment); `campaign:none` matches campaign-less records |
| `since:D` / `until:D` | `ts` on or after / before the end of day `D`; `D` is `YYYY-MM-DD` or relative (`7d`, `12h`) |
| anything else | free-text term: case-insensitive substring over title and text; several terms are AND-ed; `"quoted phrase"` is one term |

Rules: a key given twice keeps the last value; an unknown `key:` is treated as a free-text
term (so a pasted `foo:bar` finds text); an invalid value for `kind`, `severity`, `status` or a
bad date is not an error in the parse, it is reported in the status line ("unknown kind
'x'") and the filter then matches nothing for that key, never everything. `parse_record_filter`
never raises. The parsed `RecordFilter` is translated to the history record query's
arguments, which are the same ones `nw campaign records` takes (`--agent --kind --project
--severity --status --campaign --since --until --query`), so the CLI and the tab agree by
construction.

### Query, paging and refresh

- `activate()` loads in a worker. A cold record cache shows a "building history…" progress
  line from the history layer; a warm one is shown at once. A query result is the full list of
  matching record ids with their cheap row fields; the DataTable renders the first **500**
  and appends the next 500 when the cursor moves past the last row or `n` is pressed. The
  status line reads `500 of 12 345 matching · 48 211 records`.
- While the tab is visible, a poll every 5 s stats the global logs and the non-terminal
  campaigns only (the history layer's signature check); when the signature moves, the query
  is re-run in a worker. The poll stops on `deactivate()`. A terminal campaign's files are
  never read by the poll.
- On a refresh the cursor stays on the record `id` it was on, and the scroll position is
  preserved; new records appear above it and a `+N new` counter shows in the status line until
  the cursor returns to the top. If the cursor's record no longer matches the filter the
  cursor goes to the nearest row, never to the top.
- A history-layer error (corrupt cache, unreadable log) shows a one-line error in the status
  line and keeps the previous rows; it never crashes the desk.

### Record pane

The pane shows, for the cursor record, as plain `Text`: header (`kind`, `source`, `ts`,
`agent`, `severity`, `status`, `linked_by`), campaign, spec id and project, the full `title`
and `text` (wrapped; very long text is cut at 20 000 characters with a "truncated" marker),
then a **Links** list: `run` (`links.run_id`), `resolved by` (`links.resolved_by`, shown as
the target record's kind, agent and title), `follow-up idea` (`links.followup_idea` with the
text of the follow-up checklist line when the history layer provides it), `escalation`,
`thread`, `campaign`. A link whose `linked_by` is `inferred` or `text` is labelled `(inferred)`
or `(from text)`, so the confidence the history layer recorded is visible. Missing links show
nothing rather than `None`.

### Keys

All keys work from the keyboard; the mouse is only a shortcut.

| Key | Action |
|---|---|
| `j` / `k`, arrows, PageUp/PageDown, `g` / `G` | move; `G` also loads further pages until the last row |
| `f` or `/` | focus the filter input; `F` clears the filter |
| `n` | load the next page of 500 rows |
| `c`, Enter, double-click | open the `CampaignDetail` popup for the record's campaign, **focused on the record**: its tab is Findings & decisions and its row is selected; if the record has none (campaign-less), show a status-line note "record has no campaign" and do nothing else |
| `r` | open the popup on its Runs tab with `links.run_id` selected; if the record has no run link, show "no run linked" |
| `d` | move the list cursor to `links.resolved_by`, clearing filters that hide it and noting that in the status line; if none, show "no resolving decision" |

Esc in the popup returns to the Findings tab with the cursor on the same record id. The tab's
bindings shadow the desk's single-letter ones while the table has focus, as the events tab
does.

The popup's focus contract (`CampaignDetail(cfg, cid, focus=<record id>, tab=<name>,
run=<run id>)`) is owned by epic child 5. If child 5 shipped a different signature, this
spec adapts to it; the behaviour above is what matters.

### Read-only and performance

- The tab opens no file for writing and holds no lock. It does not sync the event stream or
  set any cursor ([SPEC-0251](./0251-tui-events-tab.md) rule). Opening the popup from here is
  the only cross-over and the popup is read-only for history.
- Budget (epic): a filter or full-text query over 50 000 records returns in at most 200 ms
  (asserted with ×3 slack). Repainting 500 rows is bounded by paging.
- While the tab is not active it registers no timer and does no I/O.

## Alternatives considered

- **Virtual scrolling over the whole result.** More code and more Textual surface; paging by
  500 rows is enough for a browser that is filtered before it is read.
- **Reusing the History tab's filter parser.** Would force a landing order between epic
  children 6 and 7. A small pure parser here costs less than the coupling.
- **`d` opening the popup on the decision.** The decision may be a campaign-less conductor
  record; moving the cursor works for all records and keeps the popup for campaign context.
- **Parsing sources directly in the tab.** Duplicates the history layer and breaks the "CLI
  and UI agree" property of the epic.
- **A free-text-only search box.** Cannot express "major findings by the reviewer on project
  X since last week", which is the point of the tab.

## Acceptance criteria

- [ ] **AC1.** Desk tab 7 `findings` exists, is reachable with key `7` and with
  `IdeaDesk(tab="findings")`, and activating/deactivating it starts/stops its polling (no
  timer and no I/O while another tab is active).
- [ ] **AC2.** The list shows records newest first (`ts` descending, tie broken by `id`
  descending) and contains records of **every source** the history layer produces on the
  fixture (ledger, run, decisions, escalation, mailbox, spec_review, flaky, sentinel), with
  the kinds listed in *Rows and columns*. Campaign-less records are listed with `-` in the
  campaign column.
- [ ] **AC3.** The filter keys `agent: kind: project: severity: status: campaign: since:
  until:` each narrow the list as documented in *Filter grammar* (including prefix `agent:`
  matching, `none` values and relative dates), and free text matches case-insensitively over
  title and text with multiple terms AND-ed. The set of records the tab shows for a filter is
  identical to `nw campaign records --json` run with the equivalent flags.
- [ ] **AC4.** `parse_record_filter` never raises, for any input string; invalid values are
  reported in the status line and match nothing for that key; unknown keys become free text.
- [ ] **AC5.** The record pane shows the full record (title, text, all fields) and a Links
  list including run, resolved-by, follow-up idea (with its text), escalation, thread and
  campaign, with `linked_by` confidence labels. Text containing Rich markup such as
  `[bold]` or `[/]` renders literally and does not crash.
- [ ] **AC6.** On a record with a campaign, `c`, Enter and double-click open the campaign
  popup for that campaign focused on that record; `r` opens it on the Runs tab with the
  producing run selected (for a record with `links.run_id`); Esc returns to the tab with the
  cursor on the same record id. On a campaign-less record `c` shows "record has no campaign"
  and opens nothing.
- [ ] **AC7.** `d` on a record whose `links.resolved_by` is set moves the cursor to that
  record, clearing hiding filters and saying so; with no link it shows "no resolving decision"
  and the cursor does not move.
- [ ] **AC8.** Paging: with more than 500 matches the table renders 500 rows and the status
  line shows `shown of matching · total`; `n` (and moving past the last row) appends the next
  500; no match yields an empty table and a "no matching records" status, not an error.
- [ ] **AC9.** A refresh (poll after the global logs or a live campaign changed) keeps the
  cursor on the same record id and the same scroll position, shows `+N new` for records
  added above, and moves no existing row.
- [ ] **AC10.** A filter or full-text query over 50 000 records completes in at most 200 ms
  (×3 slack in the test) on the 500-campaign clone; rendering the first page and opening the
  tab on a warm cache stays within the epic's index-load budget; the event-loop thread does
  no file I/O.
- [ ] **AC11.** The tab is read-only: after exercising it (filters, paging, `c`/`r`/`d`,
  poll refreshes) every file in the data dir is byte-identical, and the tab introduces no
  binding that calls `queue_action`, `plan_*` or `apply_*`.
- [ ] **AC12.** A corrupt or missing history cache, or an unreadable global log, leaves the
  desk running: the tab shows an error line and keeps the previous rows.
- [ ] **AC13.** Every function of the tab can be driven by the keyboard alone.
- [ ] **AC14.** The tab's tests run under a plain `uv run pytest` with none skipped for a
  missing Textual, and a plain-text snapshot of the tab at 160×48 matches its golden.

## Test plan

Fixture: `tests/fixtures/agentic_history/` from epic child 2 (sanitized real campaigns,
including an answered escalation, triage waives, flaky tests, a pre-SPEC-0362 campaign, an
abandoned and a handed-off campaign), copied to a temp data dir by `tests/tui_harness.py`
(epic child 3). The synthetic cases below build records directly through the history layer's
test helpers or by writing small ledger/mailbox files into the temp data dir.

- **Automated tests, pure:** `tests/test_findings_filter.py`
  - `parse_record_filter` table-driven: each key, repeated keys (last wins), `agent:steward`
    prefix match, `none` values, quoted phrases, relative `since:7d`, unknown key as free
    text, invalid kind/severity/date reported and matching nothing, and a Hypothesis-style
    fuzz loop over random strings asserting it never raises (AC3, AC4).
  - Equivalence with the CLI: for a list of filter strings, the record ids from the tab's
    query function equal the ids from `nw campaign records --json` with the matching flags
    (AC3).
- **Automated tests, Pilot on the real app:** `tests/test_tui_findings_tab.py`
  (uses `waits.pilot_until`, never fixed sleeps, SPEC-0269)
  1. Start `IdeaDesk`, press `7`, wait for rows; assert newest-first order against
     `nw campaign records --json`, the source/kind coverage set equals the set of sources
     in the CLI output (AC1, AC2), and campaign-less rows show `-`.
  2. Apply `agent:conductor kind:decision`, `severity:major status:open`, `project:Meta-Tools
     since:2026-01-01`, `campaign:none`, a free-text phrase taken from one real finding, and a
     no-match string; assert row counts and the empty-state status (AC3, AC8).
  3. Page: with a cloned set above 500 matches, assert 500 rows then 1000 after `n`, and the
     status text (AC8).
  4. Record pane: select a finding with a run link, a decision with `resolved_by`, a record
     with a follow-up idea and an inferred link; assert the pane text and labels. Inject a
     record whose title/text is `[bold]x[/] [link=http://a]y`; assert the literal text and
     no exception (AC5).
  5. Navigation: `c` (and Enter, and `Pilot.double_click`) opens `CampaignDetail` for the
     right campaign focused on the record; `r` opens the Runs tab with the run selected; Esc
     returns with the cursor on the same record id; on a campaign-less record `c` opens
     nothing and sets the status message; `d` moves to the resolving record and clears a
     hiding filter (AC6, AC7).
  6. Refresh: append a mailbox message and a ledger note to the temp data dir, wait for the
     poll, assert the cursor's record id and scroll are unchanged, `+N new` shows, and the
     old rows keep their order (AC9).
  7. Corrupt the record cache file and truncate a log to garbage; assert the error line,
     previous rows kept, no exception (AC12).
  8. Keyboard-only run of steps 2, 4 and 5 without any mouse event (AC13).
  9. Idle: switch to another tab; with `os.stat` / `open` patched to count calls, assert
     no findings poll fires and no file of a terminal campaign is opened (AC1, AC10).
  10. Hash every data-dir file before and after steps 1 to 6; assert byte-identical, and
      statically assert (source scan) that `findings_tab.py` imports nothing from the write
      side of `agentic_model` (`queue_action`) (AC11).
- **Text snapshot:** pyte render of the Findings tab on the fixture at 160×48, golden under
  `tests/snapshots/`; regenerated only with `NW_UPDATE_SNAPSHOTS=1` (AC14).
- **Performance:** extend `tests/test_history_perf.py` (500 campaigns, about 50 000 records):
  filter and full-text queries through the tab's query path ≤ 200 ms ×3; the first page
  renders within the index-load budget ×3 (AC10).
- **Guard:** the existing guard test from epic child 3 keeps asserting no `collect_ignore`,
  `importorskip("textual")` or `needs_textual` in `tests/test_tui_*.py`; the new test files
  must pass it (AC14).
- **Manual verification** (human, real data dir; recorded in the epic's *As shipped*):
  1. `nw tui`, press `7`. Is the list newest first, with every kind present?
  2. Type `severity:major since:7d`, then a reviewer phrase you remember; compare the count
     with `nw campaign records --severity major --since 7d --json | jq length`.
  3. On a finding press `r`, then Esc, then `c`, then Esc; the cursor must be where it was.
  4. On a record with a resolving decision press `d`.
  5. Leave the tab open 10 minutes with the queue running; confirm no flicker, no cursor
     jump, and unchanged `nw queue tick` latency.
- **Regression guard:** existing `tests/test_tui_*.py` and `tests/test_agentic_dashboard.py`
  stay green (affected-tests run via `python3 tools/affected_tests.py --run uv run pytest -q`);
  `python3 tools/spec_lint.py` exits 0.

## Clock Log

CLOCK-IN: [timestamp]
CLOCK-OUT: [timestamp]

## Rollout / migration

1. Land after epic children 2, 3 and 5 are `done`; this spec adds one tab and no data.
2. No migration: the tab reads the history layer's cache, built lazily on first use. Old
   campaigns show inferred links, labelled as such.
3. Tab `6` (History) belongs to epic child 6; whichever of children 6 and 7 lands first
   leaves the other's number simply unused. Verify with the manual checklist above.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest` on the affected files);
  manual steps performed and noted in the epic's *As shipped*.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date) via `python3 tools/spec_lint.py --write-ledger`.

## Open questions

None. The judgment calls (own filter parser, `d` stays in the tab, paging by 500) are
recorded in *Decision*.
