---
id: SPEC-0197
title: "Piped human output never hard-wraps lines"
status: accepted
owner: user
created: 2026-10-25
updated: 2026-10-25
milestone: "M4: cli ergonomics"
kind: feature
tags: [cli, rich]
source_idea: IDEA-058
---

## Context

Promoted from bug `IDEA-058`. When stdout is not a terminal, the shared Rich console (`src/wt/console.py`, `console = Console()`) falls back to 80 columns and hard-wraps every `console.print`. So `~/.local/share/wt/agentic/runner.log` (the conductor's `nohup wt queue run >> runner.log`) and anything piped to `grep`/`awk` (`wt campaign log`, `wt queue tick`, `wt mailbox thread`) see one record split across lines. Tables should still lay out sensibly when piped.

## Goals / Non-goals

**Goals**
- One-line renderables (event lines, tick rows, messages, plan lines) are never split across lines when stdout is not a terminal.
- Tables and panels still render at a sane width when piped.

**Non-goals**
- Changing terminal (TTY) behaviour.
- `--json` outputs (unaffected; they never go through Rich).

## Decision

When not a terminal, the shared console uses `soft_wrap=True` (Rich then never inserts line breaks into printed text), and its width comes from `$COLUMNS` if set, else 200, so tables don't collapse to 80.

## Design

- `wt/console.py`: build the console as `Console(soft_wrap=not tty, width=None if tty else int(os.environ.get('COLUMNS') or 200))`, where `tty = sys.stdout.isatty()`. Keep `console` a module-level singleton (tests replace `sys.stdout`; Rich resolves `file` lazily).
- `render.err_console` already has `soft_wrap=True`; leave it.
- Callers that print tables keep working: `soft_wrap` only affects text wrapping, and tables use the console width.

## Alternatives considered

- **`print(..., soft_wrap=True)` at each call site** — easy to forget; the SPEC-0185 guard can't enforce it.
- **Plain `print` when piped** — loses the shared renderers and the guard.

## Acceptance criteria

- [ ] A tick row, a campaign event line and a mailbox message longer than 80 characters each print as exactly one line through CliRunner (not a TTY).
- [ ] `wt queue list` and `wt projects` piped still render their tables with every column on one line per row at the default width.
- [ ] TTY rendering is unchanged (a forced-terminal console of width 80 still wraps prose).
- [ ] `uv run pytest` passes; `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests:** `tests/test_console_piped.py`: `wt campaign log` on a campaign with a long note, `wt queue tick` with a long row, and `wt mailbox thread` with a long message each produce lines with no mid-record break (assert the full text appears on one line); `wt queue list` table rows each on one line; a forced-terminal Console still wraps. Update any existing test that relied on 80-column wrapping.
- **Regression guard:** full `uv run pytest`.

## Rollout / migration

Ships through the agentic queue (balanced profile). No data migration.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

(none)
