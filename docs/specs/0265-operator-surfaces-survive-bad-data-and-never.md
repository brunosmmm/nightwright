---
id: SPEC-0265
title: "Operator surfaces survive bad data and never render untrusted text as markup"
status: accepted
kind: feature
owner: user
created: 2026-10-27
source_idea: IDEA-332
parent: SPEC-0169
tags: [tui, cli, review-follow-up]
---

## Context

This spec comes from triaging the accumulated `wtfug:` review follow-ups: minor findings
reviewers raised on merged specs SPEC-0202..0259. Each finding kept here was re-checked
against `main` on 2026-10-27 and is still true. The operator surfaces (the `wt tui` desk
tabs and `wt spec review --show`) either crash or garble what they show when a record is
malformed or when LLM-written text contains `[...]`. A crashed desk or a garbled review gives
the conductor and the human a wrong picture of the queue.

Kept findings:

- **IDEA-332** (SPEC-0241)
  - `wtfu:a6543c64d209`: *Action/reload workers only catch ValueError/OSError*. Textual's
    default `exit_on_error` then exits the whole app.
  - `wtfu:e3010edf3115`: *Queue row titles reach DataTable as markup strings*.
  - From the unlisted part of campaign SPEC-0241-20261025-040e: *queue_signature's iterdir
    is unguarded and runs inside the timer callback*.
- **IDEA-342** `wtfu:d2c0e956976b` (SPEC-0246): *Campaign reload worker only catches
  OSError/ValueError; any other exception takes down the whole TUI*. `paint()` and
  `show_detail()` run outside even that guard.
- **IDEA-350** `wtfu:4ae359d75ee0` (SPEC-0251): *Duplicate event ids crash the desk via
  DataTable DuplicateKey outside the load guard*. Also, from the unlisted part of campaign
  SPEC-0251-20261025-92e4: *event_time lets OverflowError escape for out-of-range
  timestamps*, which is called from `_cells` outside the guard.
- **IDEA-349** `wtfu:1fb38e00ef59` (SPEC-0249): *Malformed field types crash in paint*.
  `paint_inboxes` / `paint_threads` run outside the mailbox load guard.
- **IDEA-329** `wtfu:efcbd592ab32` and `wtfu:c660060de4ef` (SPEC-0242; also unlisted in
  campaign SPEC-0242-20261025-9c79): *Stored finding text is rendered as Rich markup, so a
  stray `[/` crashes --show every time*. `_print_review` passes `model`, `rationale`,
  finding cells and split `title`/`scope` to Rich with markup on.

Dropped from these ideas: test-only gaps, timing-flake notes, cosmetic Esc/focus behaviour,
performance notes (2000-row repaint, O(n²) threads), and the re-plan-at-apply concern
(IDEA-332 `988e72b5b7b6`). The apply functions re-check state under the item lock.

## Goals / Non-goals

**Goals**
- No exception raised while loading or painting a desk tab ends the `wt tui` process. The
  tab shows the error in its status line or as an error notification, keeps its previous
  rows, and retries on the next poll.
- Untrusted strings are always rendered as plain text, never parsed as markup. Untrusted
  strings are spec/idea titles, campaign ids, event text, and review fields written by
  personas.

**Non-goals**
- New TUI features, key bindings, or Esc/focus changes.
- Performance work on large event streams or mailboxes.
- Changing `wt queue watch` / other CLI renderers that already pass `markup=False`.

## Decision

Each worker coroutine gets one outer `except Exception` that shows the error and returns. The
load-specific excepts stay where they are. Every untrusted DataTable cell becomes
`rich.text.Text`. `_print_review` builds its lines with `rich.markup.escape` on every stored
field. The structural markup it adds itself (bold, colours) stays. The events tab drops a
duplicate id instead of crashing, and `event_time` tolerates `OverflowError`.

## Design

1. **Queue tab** (`src/wt/tui/queue_tab.py`).
   - `_reload_async`: wrap the whole body, including `AM.queue_signature` and `paint()`, in
     `try/except Exception as e`. On error, write `queue failed: <escaped e>` to
     `#queue-status` and keep `self.rows`.
   - `_action_flow`: after the existing `(ValueError, OSError)` clause, add
     `except Exception as e: self.app.notify(f"queue action failed: {escape(repr(e))}",
     severity="error")`.
   - `_poll`: guard `AM.queue_signature` with `try/except Exception`. On error, call
     `reload()`, which then reports it.
   - `AM.queue_signature` (`src/wt/tui/agentic_model.py`): wrap `root.iterdir()` in
     `try/except OSError` and treat the error as an empty list.
   - `paint`: pass `Text(R._clip(campaign, 24))`, `Text(r.spec or "")` and
     `Text(R._clip(r.title, what_w))` as cells instead of `str`.

2. **Campaign screen** (`src/wt/tui/campaign_screen.py` `_reload_async`). Same pattern. The
   `except Exception` covers `AM.load_campaign`, `paint(previous)` and the detail render.
   `show_detail` itself catches `Exception` and shows
   `(detail unavailable: <escaped error>)` in the detail pane.

3. **Events tab** (`src/wt/tui/events_tab.py`, `agentic_model.event_time`).
   - `_load_async`: the outer `try` also covers `self._append(rows)`. On error, write
     `events failed: …` to the status line and do not advance `_last_id` or `_signature`.
   - `_append` / `_repaint` skip a row whose `str(r.id)` is already a row key. Track the
     keys in a set rebuilt on `table.clear()`, and count skipped duplicates in the status
     line (`N duplicate ids skipped`).
   - `event_time`: catch `(TypeError, ValueError, OverflowError)`.

4. **Mailbox tab** (`src/wt/tui/mailbox_tab.py` `_reload_async`). Add an outer
   `except Exception` around the assignment and `paint_inboxes()` / `paint_threads()`. On
   error, show a `mailbox failed: …` notification, as the load error already does.
   `_guarded` gets the same catch-all for the send flows.

5. **`wt spec review --show` rendering** (`src/wt/cli.py` `_print_review`).
   - Wrap every value read from the stored review in `escape(str(...))` before it goes into
     an f-string with markup. These values are `spec_id`, `verdict`, `model`, `size`,
     `tone`, `rationale`, `pick["profile"]`, `pick["reason"]`, and split `title`/`scope`.
   - Build finding table cells as `Text(str(f.get(k) or ""))`.
   - `escape` is the `rich.markup.escape` already used in `cli.py`.

## Alternatives considered

- **`run_worker(..., exit_on_error=False)`.** This hides the failure entirely. The tab
  freezes with no message. An explicit catch shows the error and keeps the desk usable.
- **Strip `[`/`]` from titles.** This loses information (AC checkboxes like `[x]`).
  Rendering as plain text keeps the text exact.

## Acceptance criteria

- [ ] A `state.json` that makes `paint()` raise (for example a non-str `title`) or a
      `KeyError` from `load_queue` leaves `wt tui` running, with `queue failed:` in the
      queue tab status line. The campaign screen behaves the same way (`campaign failed:`).
- [ ] An `OSError` from iterating the campaigns dir during `_poll` does not end the app.
- [ ] `events.jsonl` holding two records with the same id, or a record with ts
      `0001-01-01T00:00:00+00:00`, renders without crashing. The duplicate is skipped and
      counted.
- [ ] A malformed mailbox record that makes `paint_threads` raise shows a `mailbox failed:`
      notification, and the app keeps running.
- [ ] A queue row whose title is `Fix [x] and [/bold] stray` shows exactly that text in the
      DataTable cell.
- [ ] `wt spec review SPEC-X --show` on a stored review whose finding title is
      `[/oops] keep [x]` and whose model is `[red]m` exits 0 and prints both strings
      verbatim.

## Test plan

- **Automated tests:**
  - `tests/test_tui_queue_tab.py::test_paint_error_keeps_the_app_alive`: monkeypatch
    `QueueView.paint` (or `AM.load_queue`) to raise `KeyError`. In the pilot, reload. Assert
    `app.is_running` and that `#queue-status` contains `queue failed`.
  - `tests/test_tui_queue_tab.py::test_signature_oserror_is_contained`: monkeypatch
    `AM.queue_signature` to raise `OSError` and call `_poll()`. The app is still running.
  - `tests/test_tui_queue_tab.py::test_titles_render_verbatim`: give a campaign the title
    `Fix [x] and [/bold] stray`. Assert the cell is a `Text` whose `.plain` equals the
    title.
  - `tests/test_tui_campaign_screen.py::test_reload_error_keeps_the_app_alive`:
    monkeypatch `CampaignScreen.paint` to raise `TypeError`. Assert the app is still
    running and the status line shows `campaign failed`.
  - `tests/test_tui_events_tab.py::test_duplicate_ids_and_bad_ts_do_not_crash`: write two
    records with `id: 5` and one with `ts: "0001-01-01T00:00:00+00:00"`. The pilot loads.
    The row count equals the number of unique ids and the status line shows
    `1 duplicate ids skipped`.
  - `tests/test_tui_mailbox_tab.py::test_paint_error_notifies`: monkeypatch
    `paint_threads` to raise `TypeError`. A notification with `mailbox failed` appears and
    the app keeps running.
  - `tests/test_spec_review_show.py::test_show_renders_markup_like_text_verbatim`: store a
    review with the strings above. Run `wt spec review --show` via CliRunner. Exit code is
    0, and the output contains `[/oops] keep [x]` and `[red]m`.
- **Sabotage checks** (each must fail its test; record the outcome in the spec): remove the
  outer `except Exception` in `queue_tab._reload_async`; revert the queue title cell to
  `str`; remove the duplicate-key skip; drop `escape` on finding/model in `_print_review`.
- **Regression guard:** run the full `uv run pytest`. All existing `tests/test_tui_*.py` and
  `tests/test_spec_review_show.py` tests must pass unchanged.
- **Manual verification:** run `uv run wt tui --tab queue` against the live data dir,
  read-only, and confirm that rows render and that switching tabs works. Note the result
  in the spec.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

- (none)

## Clock Log

