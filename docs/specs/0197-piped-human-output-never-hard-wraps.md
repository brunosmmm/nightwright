---
id: SPEC-0197
title: "Piped human output never hard-wraps lines"
status: done
owner: user
created: 2026-10-25
updated: 2026-10-25
milestone: "M4: cli ergonomics"
kind: feature
tags: [cli, rich]
source_idea: IDEA-058
---

## Context

Promoted from bug `IDEA-058`. When stdout is not a terminal, the shared Rich console
(`src/wt/console.py`, `console = Console()`) uses 80 columns and hard-wraps every
`console.print` of text. So `~/.local/share/wt/agentic/runner.log` (the conductor's
`nohup wt queue run >> runner.log`) and anything piped to `grep`/`awk` (`wt campaign log`,
`wt queue tick`, `wt mailbox thread`, the dry-run `plan` blocks) see one record split
across lines.

Revised 2026-10-25 after `wt spec review` (`docs/specs/reviews/SPEC-0197.json`, verdict
revise). The first design decided TTY-ness once at import (untestable) and widened piped
output to 200 columns, which would have changed `report.py`'s width-budgeted idea table and
the watch dashboard. This version changes only how *text* lines wrap, decided per call.

## Goals / Non-goals

**Goals**
- A line-shaped renderable — a `str`, a `rich.text.Text`, or a `Group` made only of those (for
  example `render.plan`) — printed through the shared console while it is not a terminal is
  never split across output lines.
- Everything else is byte-for-byte unchanged: tables, panels and the console width (Rich's
  default, which already honours `$COLUMNS`), and all terminal output.

**Non-goals**
- Table and panel layout when piped (cells still wrap at the console width).
- The runner's structured journal (`runner.jsonl`, SPEC-0192) and `--json` outputs; neither
  goes through text wrapping. `runner.log` keeps its format, minus the broken lines.

## Decision

Replace the shared `Console()` with a small subclass, `WtConsole`. Its `print` defaults
`soft_wrap=True` when the caller didn't pass `soft_wrap`, the console is not a terminal
(`self.is_terminal`, evaluated at call time), and every object is line-shaped. Rich then emits
the text with no inserted line breaks and no cropping.

## Design

- `src/wt/console.py`:
  - `def _line_shaped(obj) -> bool`: True for `str` and `Text`, and for a `Group` whose
    `renderables` are all line-shaped (recursively); False for anything else (Table, Panel,
    Syntax, Columns, …).
  - `class WtConsole(Console)` overrides `print(self, *objects, soft_wrap=None, **kw)`. When
    `soft_wrap is None and not self.is_terminal and objects and all(_line_shaped(o) …)`, it
    sets `soft_wrap = True`, then calls `super().print(*objects, soft_wrap=soft_wrap, **kw)`.
  - `console = WtConsole()`. The module stays import-safe: nothing is read from the
    environment at import, and Rich resolves `file` and `is_terminal` lazily, so tests that
    swap `sys.stdout` (CliRunner) or build their own `WtConsole(file=…, force_terminal=…)`
    exercise both branches whatever pytest's own stdout is.
- `render.err_console` already passes `soft_wrap=True`; leave it.
- No call-site changes: every renderer from SPEC-0185 already returns `Text` or a `Group` of
  `Text`.

## Alternatives considered

- **`soft_wrap=True` / `width=200` fixed at construction** (the first draft) — decided at
  import (untestable under `pytest -s`), and it widened every piped table, changing
  `report.py` and the watch dashboard.
- **`soft_wrap=True` at each call site** — easy to forget, and the SPEC-0185 guard can't
  enforce it.
- **`soft_wrap` for every renderable when piped** — Rich passes `no_wrap`/`crop=False` into
  table and panel rendering, changing their layout.

## Acceptance criteria

- [x] On a `WtConsole(file=StringIO(), width=80, force_terminal=False)`, each of these prints
      as exactly one line, with the full text: a 200-character `str`; `render.tick_row` of a
      row with a 150-character reason; `render.campaign_event` of an event with long
      values; `render.message` of a 300-character mailbox message; `render.plan` with a
      150-character value (one line per key).
- [x] On the same console with `force_terminal=True`, the 200-character `str` still wraps
      onto more than one line (terminal behaviour unchanged).
- [x] A `rich.table.Table` printed on both piped consoles produces the same output as a plain
      `rich.console.Console` with the same settings (tables unchanged).
- [x] Through CliRunner, `wt campaign log` on a campaign whose ledger has a note longer than
      200 characters prints that note's event on one line, and `wt mailbox thread` prints a
      300-character message on one line.
- [x] `soft_wrap=False` passed explicitly is respected (wraps when piped).
- [x] `uv run pytest` passes; `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests:** `tests/test_console_piped.py`:
  - one test per renderable in AC 1, on a piped `WtConsole`, asserting the output's line count
    and that the full text appears on one line;
  - AC 2 with `force_terminal=True`;
  - AC 3: render one table with `WtConsole` and with `Console` (same width, `force_terminal`
    and `color_system=None`) and assert equal `export_text()`;
  - AC 4 via the `test_agentic_e2e` harness (`wt campaign note` with a long text, then
    `wt campaign log`), plus `wt mailbox send` and `wt mailbox thread` with a long message;
  - AC 5 explicit override.
- **Regression guard:** full `uv run pytest` (the existing CLI tests run piped).

### Test record (2026-10-25)

- `uv run pytest tests/test_console_piped.py` — 10 passed (AC 1–5: one test per renderable, terminal
  wrap, table parity on piped and terminal consoles, explicit `soft_wrap=False`, CliRunner
  `wt campaign log` / `wt mailbox thread`).
- `uv run pytest -q` — 1684 passed, 1 skipped. `python3 tools/spec_lint.py` — exit 0.

### As shipped

- `WtConsole` as designed. One addition outside Design: the three footer/legend hint lines under
  the idea tables in `src/wt/report.py` (`flat view…`, `full text…`, kind legend) now pass
  `soft_wrap=False` explicitly. They are plain `str`s, so without it they stopped wrapping when
  piped and broke the existing width-budget tests (`test_idea_table_width`,
  `test_idea_freshness`, `test_idea_state_color`, `test_ideas`), which assert no line exceeds the
  console width. This keeps the idea-table output byte-for-byte unchanged, per the Goals.

## Rollout / migration

Ships through the agentic queue (balanced profile). No data migration.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

(none)
