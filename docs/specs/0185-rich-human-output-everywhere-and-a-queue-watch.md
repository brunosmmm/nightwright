---
id: SPEC-0185
title: "Rich human output everywhere and a queue watch dashboard"
status: in-progress
owner: user
created: 2026-10-25
updated: 2026-10-25
milestone: "M4: cli ergonomics"
kind: feature
tags: [cli, rich, ux, agentic, queue]
source_idea: IDEA-044
---

## Context

Promoted from idea `IDEA-044` (All human-facing wt CLI output should use Rich for good
visualizations). The user asked (2026-10-25) that every human-facing `wt` command render
with Rich, consistently, and that `wt queue watch` become a real dashboard: much more
about the campaigns being worked, the items that are blocked or waiting, the backlog, and
what was recently delivered.

Today most commands already print through the shared `wt.console.console`, but a set of
human paths bypass styling:

- `wt projects config` / `wt projects init` (dry run) write raw YAML through `sys.stdout`
  (chosen so Rich would not wrap or crop long paths when piped).
- `wt campaign show` / `wt campaign log` event lines, `wt queue events --text`,
  `wt mailbox thread`, `wt queue tick|run` step rows and the `plan` blocks of the queue
  write commands print with `markup=False, highlight=False` — plain monochrome text.
- `wt queue gates` failure tails print as plain text.
- Warnings go to stderr through bare `print(…, file=sys.stderr)` in eight modules
  (`rules`, `extension_schema`, `meetings`, `sheet_sync`, `org_write`, `export`, `specs`).
- `wt queue watch` is a live copy of `wt queue list`: a slot table and a backlog table. It
  says nothing about what a campaign is doing now (which persona, how long, against what
  budget), the last gate, the last verdict, why an item is parked, or what just merged.

## Goals / Non-goals

**Goals**
- One small rendering module, `wt.render`, that every human path uses for the shared
  shapes: a campaign event line, a queue event line, a mailbox message, a tick step row, a
  key/value plan block, a code/log block, and stderr warnings.
- Every human-facing output path listed in Context goes through Rich with consistent
  colour and glyphs. Text content stays the same words (ids, states, reasons) so muscle
  memory and greps keep working.
- Code-like output (YAML, logs) is syntax-highlighted on a terminal and **byte-identical
  plain text when piped**, so agents and `| grep` never see wrapped or cropped paths.
- A guard test fails when a new bare `print` / `sys.stdout.write` / `click.echo` /
  `sys.stderr.write` appears outside an explicit allowlist of machine-output paths.
- `wt queue watch` becomes a dashboard: a header (runner, lease, slots, counts, spend), a
  panel per active campaign, a **Needs attention** section (parked, park requested), the
  **Backlog** (with what each blocked item waits on), and **Recently delivered**
  (merged / handed off / abandoned). It also gets `--once` (one frame, no Live) and
  `--json` (the same data as `wt.queue.dashboard.v1`).

**Non-goals**
- Machine output: `--json` everywhere, the `--follow` streams (`wt queue events --follow`,
  `wt mailbox follow` — Monitor input, one line per event, unchanged bytes), `wt sheet
  plan|record` (JSON-only by contract, SPEC-0125), `wt completion source`, `wt campaign
  prompt` (a raw prompt meant to be piped), `wt idea show --plain`.
- `runner.log` and other saved logs (IDEA-045 makes those JSONL).
- A Textual TUI tab for the queue (IDEA-037); followers listing (IDEA-049); a per-run
  listing (IDEA-050).
- Changing `wt queue list`'s compact layout (it stays the dense 80-column view).

## Decision

Add `src/wt/render.py` with the shared renderers and a stderr console, convert the listed
call sites to it, and add an AST guard test with an allowlist. Build the dashboard from a
pure payload function `queue.dashboard_payload(cfg, …)` (testable without a terminal)
rendered by `cli_queue._dashboard(payload, width)`; `watch` refreshes it in `rich.live.Live`.

## Design

### `wt.render`

- `err_console = Console(stderr=True, soft_wrap=True, highlight=False)`.
- `warn(msg)`: prints `msg` to `err_console` in yellow, markup-escaped, never wrapped. The
  text keeps the existing `  ! …` prefixes so stderr greps in tests and skills still match.
- `is_pretty(console)`: `console.is_terminal`.
- `print_code(text, lexer, *, console, title=None)`: on a terminal, a `rich.syntax.Syntax`
  (theme from the idea-show theme config, `background_color="default"`, no word wrap),
  optional dim `# title` line; otherwise `console.file.write(text)` unchanged.
- `state_style(state)`: one colour per campaign state, shared by every renderer:
  QUEUED dim, OPENING/IMPLEMENTING cyan, GATING blue, TRIAGING magenta, CLOSING green,
  PARKED yellow, MERGED/HANDED_OFF green, ABANDONED dim red.
- `campaign_event(ev) -> Text`: `seq` dim, `ts` dim, `type` coloured by family (transition → arrow with both states styled;
  run.start/run.end cyan; gate green/red by `ok`; park yellow; close green; note italic),
  then `key=value` pairs with dim keys; long values truncated as today (60 chars).
- `queue_event(ev) -> Text`: the `wt queue events --text` line, same words as
  `events.summary_line`, styled (`#id` dim, type coloured, campaign bold).
- `message(m) -> Text`: mailbox line, `kind` coloured (question yellow, answer green,
  request cyan, info dim), `from`/`re` dim, full text.
- `tick_row(row) -> Text`: `campaign: FROM → TO (action; reason) run … $cost` with states
  styled via `state_style`.
- `plan(title, mapping) -> Table`: two-column grid, keys dim, empty values skipped.

`events.summary_line`, `mailbox.summary_line` and `queue.tick_row_text` stay (they feed the
follow streams and tests); the renderers produce the same plain text (`Text.plain`).

### Converted call sites

`projects config` / `projects init` → `print_code(yaml, "yaml", title=path)`;
`queue gates` failure tail → `print_code(tail, "text")` under a red `✗ name — last lines`;
`campaign show|log` → `campaign_event`; `queue events --text` → `queue_event`;
`mailbox thread` → `message`; `queue tick|run` → `tick_row`; `_print_plan` → `plan`;
the stderr `print`s in `rules`, `extension_schema`, `meetings`, `sheet_sync`, `org_write`,
`export`, `specs` → `warn`. `meetings` stdout progress → `console.print`.

### Guard

`tests/test_rich_output_guard.py` parses every `src/wt/**/*.py` with `ast` and collects
calls to `print`, `click.echo`, `sys.stdout.write`, `sys.stderr.write` (and
`json.dump(…, sys.stdout)` is ignored — it is JSON). Each hit must match an allowlist entry
`(module, enclosing function)` with a one-line reason (JSON emitters, follow streams,
`print_code` plain fallback, completion source, campaign prompt, idea show plain mode,
`config` pre-import pyyaml error). A new hit fails with the file:line and the fix.

### Dashboard (`wt queue watch`)

`queue.dashboard_payload(cfg, *, project=None, recent=5, recent_hours=24, now=None)` returns
`wt.queue.dashboard.v1`:

```
{schema, generated_at, lease, slots, totals: {active, attention, backlog, ready, blocked,
 delivered, spend_today_usd},
 active: [item + {spec_title, phase_index, activity, last_gate, last_verdict, recent_events}],
 attention: [item + {spec_title, park: {reason, detail, resume_to, since}, park_age_s,
             last_verdict}],
 backlog: [backlog_view row + {spec_title, project}],
 delivered: [{campaign, spec_id, spec_title, project, state, merged_sha, pr_url, cost_usd,
              duration_s, finished_age_s}]}
```

- `activity` comes from the campaign ledger: an open `run.start` (no matching `run.end`)
  gives `{kind: "run", persona, model, effort, run_id, elapsed_s, timeout_s,
  max_budget_usd}`; else state GATING/CLOSING after the last transition gives
  `{kind: "gates"}`; else `{kind: "idle"}`.
- `phase_index` places the state on the stepper OPENING → IMPLEMENTING → GATING → TRIAGING
  → CLOSING.
- `spec_title` is read from the spec frontmatter in the project repo (cached per path +
  mtime; missing file → `None`).
- `delivered`: terminal campaigns whose `updated_at` is within `recent_hours`, newest first,
  at most `recent`.
- `attention`: PARKED campaigns and any non-terminal campaign with `park_requested`.
- `spend_today_usd`: sum of `cost.total_usd` over campaigns updated today (local date).

Rendering (`_dashboard(payload, width)`):

- **Header** panel: runner (pid, heartbeat age, STALE in red), slots `busy/total` as a
  small bar, counts as coloured chips (active, attention, backlog ready/blocked,
  delivered 24h), spend today, `generated_at` clock.
- **Active**: one panel per campaign, border coloured by `state_style`. Title = campaign
  id; body: spec id + title, project · profile · branch; the phase stepper (done steps
  green, current bold, rest dim); activity line ("implementer · sonnet/high · run 002 ·
  4m12s / 30m · budget $4.00" or "running gates" or "idle"); loops `x/max`; cost bar
  `$spent / $cap`; last gate (`✓ pytest 114s  ✓ spec-lint 1s` or red `✗`); last verdict
  status + first summary line; last 3 events via `campaign_event`. "No active campaigns"
  when empty.
- **Needs attention**: table — campaign, spec + title, reason (yellow), detail, resume
  to, parked for. Hidden when empty.
- **Backlog**: table — #, campaign, spec + title, priority, ready (green) or waiting on
  (yellow, the `blocked_by` list), age.
- **Recently delivered**: table — finished ago, campaign, spec + title, outcome
  (MERGED green / HANDED_OFF green with PR url / ABANDONED dim), sha (7), cost, duration.
- At width ≥ 140 Active panels sit two per row (`rich.columns.Columns`); below, stacked.

CLI: `wt queue watch [--interval S] [--project P] [--recent N] [--once] [--json]`;
`--iterations` stays hidden. `--json` prints one payload and exits; `--once` prints one
frame without `Live` (screenshots, pipes, tests).

## Alternatives considered

- **Leave `sys.stdout` YAML as is** — loses colour for humans; `print_code`'s TTY test keeps
  the piped bytes identical, so there is no trade-off.
- **Render warnings through the main console** — mixes them into stdout and breaks
  `--json` consumers that read stdout.
- **A Textual app for the dashboard** — heavier, and IDEA-037 owns the TUI; `Live` keeps
  `watch` a plain CLI command.
- **Extend `wt queue list` instead of `watch`** — `list` is the compact, copyable view
  agents and humans pipe; the dashboard is a different, wide, refreshing view.

## Acceptance criteria

- [ ] `src/wt/render.py` exists with `err_console`, `warn`, `print_code`, `state_style`,
      `campaign_event`, `queue_event`, `message`, `tick_row`, `plan`.
- [ ] Each renderer's `Text.plain` equals the current plain line for the same input
      (`events.summary_line`, `mailbox.summary_line`, `queue.tick_row_text`, the campaign
      event line).
- [ ] Every call site in *Converted call sites* uses `wt.render`; no human path prints
      with `markup=False, highlight=False` any more.
- [ ] `wt projects config` piped (non-terminal) output is byte-identical to before; on a
      terminal it is a highlighted YAML block.
- [ ] Warnings go to stderr via `warn` with their existing text; stdout of a `--json`
      command carries no warning text.
- [ ] The guard test fails on a new bare `print(…)` in `src/wt` and passes on the tree.
- [ ] `queue.dashboard_payload` returns `wt.queue.dashboard.v1` with `active`,
      `attention`, `backlog`, `delivered`, `totals`; `activity` reports an open persona run
      with elapsed time, gates, or idle.
- [ ] `wt queue watch --once` renders Header, Active (or "No active campaigns"), Backlog,
      Recently delivered, and Needs attention when something is parked; `--json` emits
      the payload; `--project` and `--recent` filter.
- [ ] `--follow` streams, `--json` outputs and `wt sheet plan|record` are unchanged.
- [ ] `uv run pytest` passes; `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests:**
  - `tests/test_render.py`: each renderer's `.plain` equals the existing plain helper for
    representative events/messages/rows (transition, run.start, run.end, gate ok/fail,
    park, note, unknown type); `state_style` covers every `queue.STATES`; `warn` writes
    to stderr not stdout; `print_code` writes the exact text when the console is not a
    terminal and a Syntax when forced terminal.
  - `tests/test_rich_output_guard.py`: the AST guard over `src/wt`; a self-test that a
    synthetic module with a bare `print` is reported.
  - `tests/test_agentic_dashboard.py`: `dashboard_payload` over a temp data dir with
    campaigns in IMPLEMENTING (open run.start), GATING, PARKED, QUEUED (ready and blocked
    by a `depends_on`), MERGED (recent and older than `recent_hours`), ABANDONED;
    asserts sections, activity kinds and elapsed, `delivered` ordering and cutoff,
    totals, `spec_title`. CLI: `watch --once` contains each section header and the
    campaign ids; `--json` schema; `--project` filter; width 160 vs 80 both render.
  - Existing CLI tests for `projects config`, `campaign show|log`, `mailbox thread`,
    `queue tick`, `queue gates`, `queue events --text` and `test_watch_renders` keep
    passing (plain text unchanged when not a terminal).
- **Manual verification:** on a real terminal: `wt projects config`, `wt campaign log
  <id> --tail 15`, `wt mailbox thread steward:Meta-Tools:2`, `wt queue events --text --tail
  10`, `wt queue watch --once` (current data: SPEC-0179/0180 delivered) and a few seconds of
  `wt queue watch`; `wt projects config | cat` diffed against the pre-change output.
- **Regression guard:** full `uv run pytest`; `wt queue events --follow` / `wt mailbox
  follow` code paths untouched (no diff in `events.follow` / `mailbox.follow`).

## Rollout / migration

1. Land `wt.render` + tests, convert call sites, add the guard.
2. Add `dashboard_payload` + the new `watch`.
3. Ask the conductor to restart the runner (code under `src/wt/agentic/` changed).

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

(none)
