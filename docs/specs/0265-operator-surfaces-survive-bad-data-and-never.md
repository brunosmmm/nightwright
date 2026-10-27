---
id: SPEC-0265
title: "Operator surfaces survive bad data and never render untrusted text as markup"
status: done
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
  tab shows the error in its status line or as an error notification and retries on the
  next poll. When a paint fails, the table may be left empty or partial (the paint clears
  it first) and `self.rows` already holds the new data; the error is the signal, and the
  next successful poll repaints. We do not restore old rows: that would need a second
  paint, which can fail the same way.
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
     `#queue-status`. As written, `self.rows, self.header = rows, header` runs before
     `paint`, so a paint failure leaves the new data in `self.rows`; that is accepted (see
     Goals).
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
   - Timeline cells: `paint` (lines ~134-136) passes `e.ts[...]` and
     `R._clip(e.text.replace("\n", " "), text_w)` as `str`, so a `[/` in ledger event text
     breaks the screen. Both become `Text(...)`.
   - Other paint call sites: `paint()` is also called from the filter keys (~line 176) and
     `on_data_table_row_highlighted` (~168-170) outside the worker. Those paths run only
     on data the last successful reload already painted, and `show_detail` guards itself;
     the filter-key `paint()` is wrapped in the same `try/except Exception` (status line
     `campaign failed: …`). `QueueView.paint` calls at queue_tab.py ~169 get the same
     wrap. Other TUI tabs' paint sites (app.py) are out of scope.

3. **Events tab** (`src/wt/tui/events_tab.py`, `agentic_model.event_time`).
   - `_load_async`: the outer `try` also covers `self._append(rows)`. On error, write
     `events failed: …` to the status line. Today `_signature`/`_file_size`/`_inode` are
     assigned before `_append` (events_tab.py ~170) and `_last_id` inside `_append` before
     painting, so a failed paint would mark rows consumed forever. Move all four
     assignments to after a successful `_append`, and on an `_append` failure restore
     `loaded`/`shown` to their values before the call.
   - Dedupe in the data, not at the table: `_append` first filters `rows` against the set
     of ids already in `loaded` and within `rows` itself, before anything is added to
     `loaded` or `shown`. `table`, `shown` and `loaded` stay one-to-one, so
     `current_row()` (`self.shown[table.cursor_row]`), cursor restore and
     `move_cursor(row=len(self.shown) - 1)` stay correct. The duplicate count
     (`self._dupes`) is cumulative since the last reset (zeroed where `loaded` is reset)
     and shown in the status line as `N duplicate ids skipped`.
   - `event_time`: catch `(TypeError, ValueError, OverflowError)`. `fromisoformat(ts)
     .astimezone()` overflows for year 1 only when the local offset is behind UTC, so the
     test uses a timestamp that overflows in any timezone.

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

- [x] A `state.json` that makes `paint()` raise (for example a non-str `title`) or a
      `KeyError` from `load_queue` leaves `wt tui` running, with `queue failed:` in the
      queue tab status line. The campaign screen behaves the same way (`campaign failed:`).
- [x] An `OSError` from iterating the campaigns dir during `_poll` does not end the app.
- [x] `events.jsonl` holding two records with the same id, or a record with ts
      `9999-12-31T23:59:59-14:00` (overflows in any timezone), renders without crashing. The duplicate is skipped and
      counted.
- [x] A malformed mailbox record that makes `paint_threads` raise shows a `mailbox failed:`
      notification, and the app keeps running.
- [x] A queue row whose title is `Fix [x] and [/bold] stray`, and a campaign-screen timeline
      row whose event text is `see [/oops] and [x]`, show exactly that text in the
      DataTable cell.
- [x] `wt spec review SPEC-X --show` on a stored review whose finding title is
      `[/oops] keep [x]` and whose model is `[red]m` exits 0 and prints both strings
      verbatim.

## Test plan

- **Automated tests:**
  - `tests/test_tui_queue_tab.py::test_paint_error_keeps_the_app_alive`: monkeypatch
    `AM.load_queue` to raise `KeyError` (and, separately, write a non-str `title` so
    `paint` raises inside the worker). Do not patch `QueueView.paint` itself: it is also
    called on mount and resize, outside the worker, and would crash the pilot for an
    unrelated reason. In the pilot, reload. Assert
    `app.is_running` and that `#queue-status` contains `queue failed`.
  - `tests/test_tui_queue_tab.py::test_signature_oserror_is_contained`: monkeypatch
    `AM.queue_signature` to raise `OSError` and call `_poll()`. The app is still running.
  - `tests/test_tui_queue_tab.py::test_titles_render_verbatim`: give a campaign the title
    `Fix [x] and [/bold] stray`. Assert the cell is a `Text` whose `.plain` equals the
    title.
  - `tests/test_tui_campaign_screen.py::test_reload_error_keeps_the_app_alive`:
    monkeypatch `CampaignScreen.paint` to raise `TypeError`. Assert the app is still
    running and the status line shows `campaign failed`.
  - `tests/test_tui_campaign_screen.py::test_timeline_text_renders_verbatim`: an event with
    text `see [/oops] and [x]`. Assert the cell is a `Text` whose `.plain` equals it.
  - `tests/test_tui_events_tab.py::test_duplicate_ids_and_bad_ts_do_not_crash`: write two
    records with `id: 5` and one with `ts: "9999-12-31T23:59:59-14:00"`. The pilot loads.
    The row count equals the number of unique ids, `len(shown) == table.row_count`, the
    detail pane of the row after the duplicate is that row's event, and the status line
    shows `1 duplicate ids skipped`.
  - `tests/test_tui_events_tab.py::test_append_failure_is_retried`: make the first
    `_append` raise; assert `_last_id` and `_signature` did not advance and the next poll
    shows the rows.
  - `tests/test_tui_mailbox_tab.py::test_paint_error_notifies`: monkeypatch
    `paint_threads` to raise `TypeError`. A notification with `mailbox failed` appears and
    the app keeps running.
  - `tests/test_spec_review_show.py::test_show_renders_markup_like_text_verbatim`: store a
    review with the strings above. Run `wt spec review --show` via CliRunner. Exit code is
    0, and the output contains `[/oops] keep [x]` and `[red]m`.
- **Sabotage checks** (each must fail its test; record the outcome in the spec): remove the
  outer `except Exception` in `queue_tab._reload_async`; revert the queue title cell to
  `str`; remove the duplicate-key skip; drop `OverflowError` from `event_time`; revert the
  campaign timeline cell to `str`; drop `escape` on finding/model in `_print_review`.
- **Regression guard:** run the full `uv run pytest`. All existing `tests/test_tui_*.py` and
  `tests/test_spec_review_show.py` tests must pass unchanged.
- **Manual verification:** run `uv run wt tui --tab queue` against the live data dir,
  read-only, and confirm that rows render and that switching tabs works. Note the result
  in the spec.

### Test record (2026-10-27)

- New tests, all passing: `test_tui_queue_tab.py::{test_paint_error_keeps_the_app_alive,
  test_signature_oserror_is_contained, test_queue_signature_survives_an_unlistable_campaigns_dir,
  test_titles_render_verbatim}`, `test_tui_campaign_screen.py::{test_reload_error_keeps_the_app_alive,
  test_timeline_text_renders_verbatim}`, `test_tui_events_tab.py::{test_duplicate_ids_and_bad_ts_do_not_crash,
  test_append_failure_is_retried}`, `test_tui_mailbox_tab.py::test_paint_error_notifies`,
  `test_spec_review_show.py::test_show_renders_markup_like_text_verbatim`.
- Sabotage checks, each made its test fail and was then reverted: outer `except Exception` in
  `queue_tab._reload_async`; queue title cell back to `str`; duplicate skip; `OverflowError` in
  `event_time`; campaign timeline cell back to `str`; `escape` on the model in `_print_review`.
- Manual: a headless pilot of `IdeaDesk(tab="queue")` on the live data dir (read-only) rendered
  16 queue rows with the header in the status line, and switching tabs 1 → 3 kept the app running.
- Full gate: `python3 tools/spec_lint.py --write-ledger && python3 tools/spec_lint.py` OK; `uv run pytest -q -x`: 3249 passed, 3 skipped.

### As shipped

- Deviations/additions beyond Design: `_poll` of the campaign screen got the same signature guard
  as the queue tab; `on_resize` paints on both screens go through a guarded `_safe_paint`; the
  mailbox `_guarded` catch-all reports `mailbox action failed: …`.
- Events tab: a failed `_append` restores `loaded`/`shown`/`_dupes`/`_last_id` and sets `_dirty`, so
  the retry rebuilds the table in full (the half-painted table would otherwise raise DuplicateKey).
  `_last_id` is now assigned at the end of `_append` and `_signature`/`_file_size`/`_inode` after it
  returns. The duplicate count is cumulative since the last reset.
- Campaign timeline: the seq and time cells are `Text` too, not only the text cell.
- Review fix: `MailboxView.on_resize` repaints inside the same catch-all (`mailbox failed:`
  notification), since a failed paint leaves its data in `self.threads`. `AM.age_s` now keeps
  `astimezone()` inside its try and also catches `OverflowError`. `CampaignScreen._reload_async`
  resets `_signature` to `None` on failure so the next poll retries. Tests:
  `test_resize_after_paint_error_keeps_the_app_alive`,
  `test_age_s_tolerates_out_of_range_naive_timestamps` (mailbox tab); the four TUI test files
  pass (97 passed).

- Review round 2: `QueueView._reload_async` now resets `_signature` to `None` on failure (as the
  campaign screen does), so the next poll retries instead of the 15s periodic reload. New tests:
  `test_an_unexpected_action_error_is_an_error_notification` (queue `_action_flow` catch-all),
  `test_send_flow_catch_all_notifies` (mailbox `_guarded` catch-all),
  `test_a_failed_reload_is_retried_by_the_next_poll` (queue), `test_a_failed_paint_is_retried_by_the_next_poll`
  and `test_a_signature_error_in_poll_is_contained` (campaign screen). `test_age_s_...` now fixes `TZ`
  and asserts `None` for the out-of-range naive inputs, plus a stub whose `astimezone` raises
  `OverflowError`. Sabotage (each made its test fail, then reverted): the queue `_signature = None`
  reset, both `except Exception` catch-alls, the campaign `_signature = None` reset, and `age_s`
  with `astimezone()` outside the `try`.

- Review round 3: events `_append` takes `_last_id` from the highest id in the batch, not the last
  raw row, so a trailing lower-id duplicate no longer inflates the duplicate count; mailbox
  `_reload_async` resets `_signature` to `None` on failure (next poll retries); the queue
  `_action_flow` `(ValueError, OSError)` toast is escaped. New tests:
  `test_show_detail_error_is_shown_in_the_pane`, `test_filter_key_and_resize_paint_errors_are_contained`
  (campaign screen), `test_resize_and_paint_errors_are_contained`, `test_a_value_error_notification_is_escaped`
  (queue), `test_append_failure_after_a_reset_restores_the_old_rows_and_retries`,
  `test_a_trailing_lower_duplicate_id_does_not_inflate_the_count` (events),
  `test_a_failed_paint_is_retried_by_the_next_poll` (mailbox). Sabotage: `rows[-1].id` for `_last_id`
  made the trailing-duplicate test fail, then reverted. The four TUI test files: 109 passed.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

- (none)

## Clock Log

