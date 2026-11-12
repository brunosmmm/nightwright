---
id: SPEC-0563
title: "SPEC-0477 follow-up: Findings & decisions and Mail panes, minor review findings"
status: proposed
owner: user
created: 2026-11-12
kind: feature
source_idea: IDEA-1203
tags: [tui, agentic, observability]
depends_on: [SPEC-0477]
---

## Context

[SPEC-0477](./0477-campaign-detail-findings-and-mail.md) shipped the Findings & decisions
pane, the Mail pane and `answer_escalation` in campaign SPEC-0477-20261109-e461. Its
loop-3 review ended at score 72 with 0 majors, and the loop closed with minor findings
and nits left unfixed (some waived by the triager, some carried as advisory). This spec
collects the ones that are still true in the code at `main` and fixes them. None changes
what SPEC-0477 promised; each makes the shipped behavior match the SPEC-0477 text more
closely or covers a promise that has no test.

What is still true, from reading `src/nightwright/tui/campaign_records.py`,
`campaign_detail.py`, `escalation_flow.py` and `mailbox_tab.py`:

1. **A corrupt mail record id can blank the popup.** `mailbox_line` catches `OSError`,
   `ValueError` and `UnicodeError`, so `f.seek(int(offset))` with a huge offset raises
   `OverflowError` out of it. `_id_order` calls `int(offset)` after `str.isdigit()`, and
   `"²".isdigit()` is true while `int("²")` raises `ValueError`. Both run inside
   `_reload_async`'s outer try, so the initial load takes the "history unavailable" path
   and hides Overview and Timeline too. SPEC-0477 says a bad mailbox line "does not raise".
2. **The time cell is not control-character cleaned.** `_timeline_time` returns the raw
   `str(ts)` when `fromisoformat` fails; the Findings and Mail tables wrap that in `Text`
   without `AM._clean`, while every other cell and both detail panes are cleaned (loop 3
   of SPEC-0477 added `_clean` there).
3. **Keyed filter tokens match the string `None`.** `match_record` compares
   `str(rec.get(key)) != value`, so a record with a null `severity` or `status` matches
   `severity:None` and `status:None`. SPEC-0477 says keyed tokens match "exactly, on the
   raw value"; a null has no value to match.
4. **The error guard is duplicated.** `escalation_flow.guarded` copies the body of
   `MailboxView._guarded`. SPEC-0477 asks the popup to use "the same `_guarded` notifies as
   the mailbox tab", and two copies can drift.
5. **A stale docstring.** The `CampaignDetail` class docstring still says "three
   placeholder panes" (`campaign_detail.py`, the class docstring); no placeholder pane is
   left.
6. **Test gaps.** Reviewers named paths with no test: `focus={"tab": "mail"}` selecting the
   newest thread; the Mail row's blank-status cell and the literal-markup check on the table's
   first-line cell; reload clamping to the last row (Findings and Mail) and the all-rows-gone
   `no mail` state; `d` when the resolving record exists but the filter hides it; `i` pressed
   twice and a reload while the follow-up line is shown; the popup's confirm-stage cancel and
   the `EscRow` handed to `apply_withdraw`; a direct `answer_escalation` call with a stub
   owner; the read-only test's "filter" step, which types `w:` and so filters nothing; and
   the placeholder-absence loop, which reads `content` from `DataTable` widgets that
   have none.

## Goals / Non-goals

**Goals**

- Make a malformed mail record id or mailbox offset fail soft: the Mail pane falls back
  as SPEC-0477 already specifies and the rest of the popup stays up.
- Clean control characters from the time cells in both tables.
- Make a null field never match a keyed filter token.
- Keep one copy of the escalation error guard.
- Close the test gaps listed under Context.

**Non-goals**

- Moving mailbox reads off the event loop. The triager waived this in loops 2 and 3: the
  reads are one seek and `readline` per thread on a local file, and the Findings `i` path
  already uses a thread only because it opens an idea file. Revisit only if a measured stall
  appears.
- Changing the priority `ctrl+k`, `ctrl+j`, `pagedown`/`pageup`, `up` and `down` bindings
  while `#records-filter` is focused. They were waived as harmless and are SPEC-0477's
  documented keyboard model.
- The popup being dismissed while an apply is in flight (waived).
- Any new key, column, record field, or writer. SPEC-0477's non-goals stand.

## Decision

Fix items 1 to 5 in place, with the smallest edit each, and add tests for item 6. No
schema, key binding, or visible label changes, except that a time cell that is not an ISO
timestamp now shows with control characters stripped, and `severity:None` / `status:None`
stop matching records whose field is null.

## Design

### Fail-soft mailbox ids

- `campaign_records.mailbox_line` adds `OverflowError` to its `except` tuple. A negative
  offset already raises `ValueError` from `seek` on a file object; it stays caught.
- `_id_order` returns `(str(rec_id), -1, "")` unless the part after the last colon is
  ASCII decimal digits: it tests `offset.isascii() and offset.isdigit()`, then converts
  inside `try` / `except ValueError` and falls back to the same tuple. It never raises.
  Ordering for well-formed ids (`mailbox:x:99` before `mailbox:x:100`) is unchanged.

### Time cell

`_timeline_time` returns `AM._clean(str(ts))` on its fallback path. The ISO path is
unchanged. This covers every caller (Timeline, Findings, Mail).

### Keyed filter

In `match_record`, a keyed token compares only when the field is not `None`:
`value = rec.get(key)` and the token fails when `value is None or str(value) != value_of_token`.
Exact, case-sensitive matching is unchanged, and a field that holds the string `"None"`
still matches `severity:None`. A bare word is unchanged.

### One guard

`MailboxView._guarded(self, flow)` becomes `await escalation_flow.guarded(self, flow)`; its
`try` / `except` body is removed from `mailbox_tab.py`. `guarded` keeps its current body and
notifies. `_busy` is still reset in `finally`, on the same `owner`. Other `MailboxView`
flows (`_reply_flow`, `_new_flow`) keep calling `self._guarded`.

### Docstring

The `CampaignDetail` class docstring reads "overview, timeline, runs, findings & decisions,
and mail" (the five tabs), with no mention of placeholder panes.

### Tests

All new tests extend `tests/test_tui_campaign_records.py`, which already carries its one
module-level marker `pytestmark = pytest.mark.tui` (SPEC-0490: `test_tui_*`); no new
test module and no second marker. See the Test plan.

## Alternatives considered

- **Wrap the whole Mail refresh in a try/except instead of hardening the helpers.** It
  would hide a real bug in the table build and leave the helpers' own "does not raise"
  contract broken, so each helper stays fail-soft.
- **Stringify `None` as a blank for filter matching.** `severity:` with an empty value is
  then ambiguous with an unmatched token; null simply matching nothing is simpler.
- **Have the mailbox tab import the popup guard under a different name.** Keeping
  `MailboxView._guarded` as a one-line delegate leaves every existing call site and test
  untouched.
- **Move mailbox reads to `asyncio.to_thread` now.** Waived by the triager and larger than
  a minor fix; see Non-goals.

## Acceptance criteria

- [ ] **AC1. Malformed ids.** `mailbox_line` returns `None` for a record id whose offset is
  `99999999999999999999999999` and for one whose offset is `²` (and for a negative offset),
  without raising. `_id_order("mailbox:x:²")` and `_id_order("mailbox:x:99999999999999999999")`
  return a tuple without raising, and `mailbox:x:99` still sorts before `mailbox:x:100`.
  Opening the popup on a campaign whose mail record has such an id shows Overview and
  Timeline, shows the Mail row with the history `text`, `agent` as `from`, and blank `to`
  and kind, and does not show "history unavailable".
- [ ] **AC2. Time cell.** A record whose `ts` is `"\x1b[31mnow"` renders a Findings time cell
  and a Mail time cell with no `\x1b` in `.plain`. A valid ISO `ts` renders as before.
- [ ] **AC3. Null never matches.** On records with a null `severity` and a null `status`,
  `severity:None` and `status:None` keep no row (and neither token shows `unmatched:`,
  since the keys are known); on a record whose `severity` is the string `"None"`,
  `severity:None` keeps it. `severity:high`, `status:fixed`, and bare words behave as in
  SPEC-0477.
- [ ] **AC4. One guard.** `MailboxView._guarded` delegates to `escalation_flow.guarded`;
  `mailbox_tab.py` contains no second `except (ValueError, OSError)` / `mailbox action
  failed` block. A `ValueError`, an `OSError` and a `RuntimeError` raised by a flow give
  the same notify text and severity and leave `_busy` false through the mailbox tab and
  through the popup; the existing mailbox choose, text, withdraw and error tests pass.
- [ ] **AC5. Docstring.** The `CampaignDetail` class docstring has no "placeholder".
- [ ] **AC6. Focus on Mail.** Opening the popup with `focus={"tab": "mail"}` makes
  `tab-mail` active with the newest thread's row selected and its messages in the detail.
- [ ] **AC7. Mail cells.** A Mail row whose latest record has a null `status` has a blank
  status cell; a mail body containing `[bold]` shows those characters literally in the
  table's first-line cell, as `Text` with no style spans from the markup.
- [ ] **AC8. Reload clamp.** Removing the last Findings row, and removing the last Mail
  thread, then reloading, selects the new last row; removing every Mail thread shows the
  `no mail` state without an exception.
- [ ] **AC9. `d` and `i` edges.** `d` when `links.resolved_by` names a record the active
  filter hides notifies `no decision` and leaves the selection. `i` pressed twice on one
  record leaves exactly one `follow-up:` line. A `reload()` while the line is shown keeps
  the line on the same selected record. The `i` worker finishing after the selection moved
  appends nothing.
- [ ] **AC10. Escalation confirm.** In the popup, choosing `cancel` at the final confirm
  for `c`, `t` and `w` writes nothing and plans once; a confirmed `w` passes `apply_withdraw`
  a plan built from the `EscRow` whose `id` is `links.escalation`. A direct
  `await answer_escalation(stub_owner, cfg, row, action)` with a stub owner (`.app`,
  `.reload`) for `choose`, `text` and `withdraw` calls `reload` once on success and not on
  cancel, and `withdraw` appends the parked suffix only when the campaign is `PARKED` with
  park reason `human_decision`.
- [ ] **AC11. Test hygiene.** The read-only test's filter step uses a token that filters
  rows (it changes the visible row count). The placeholder-absence check asserts the
  `#campaign-records` and `#campaign-mail` tables have the expected row count and that the
  detail widgets' text lacks `placeholder:`; it does not read `content` from a `DataTable`.
- [ ] **AC12. Unchanged behavior.** SPEC-0477's acceptance criteria still hold: its tests in
  `tests/test_tui_campaign_records.py`, `tests/test_tui_mailbox_tab.py`,
  `tests/test_tui_escalations_tab.py` and `tests/test_tui_campaign_detail_ac.py` pass.

## Test plan

Automated, in `tests/test_tui_campaign_records.py` on the fixture copy and `tmp_path`. The
module already has its one marker, `pytestmark = pytest.mark.tui`; this spec adds no new
test module and no second tier. `python3 tools/test_tiers.py --check` exits 0 when
`tools/test_tiers.py` exists.

1. **Model (AC1, AC3):** `CR.mailbox_line` with the oversized, `²` and negative offsets and
   a nonexistent stem returns `None`; `CR._id_order` on the same ids does not raise and
   keeps `99` before `100`; `CR.match_record` with null and `"None"` severity/status.
2. **Pane (AC1, AC2, AC7):** write a mail record with a huge-offset id into the fixture
   history, open the popup and assert Overview and Timeline render, the Mail row shows the
   fallback, and no "history unavailable". Put `\x1b[31mnow` in a record `ts` and assert no
   `\x1b` in the Findings and Mail time cells. Assert the blank status cell and the literal
   `[bold]` first-line cell on a Mail row.
3. **Guard (AC4, AC5):** parametrize over `ValueError`, `OSError` and `RuntimeError` for the
   mailbox tab and the popup and assert identical notify text and severity and `_busy`
   false; assert the source of `mailbox_tab.py` does not contain `mailbox action failed`
   (it lives only in `escalation_flow.py`); assert the `CampaignDetail` docstring
   has no "placeholder".
4. **Focus and reload (AC6, AC8):** open with `focus={"tab": "mail"}` and assert the active
   tab, selected thread and detail. Remove the last Findings row and the last Mail thread,
   `reload()`, assert the new last row is selected; remove all Mail threads and assert the
   `no mail` state.
5. **Edges (AC9):** hide the `resolved_by` row with a filter and press `d`; press `i`
   twice and count `follow-up:` lines; `reload()` with the line shown; move the selection
   before the `i` worker finishes (patch `CR.idea_title` to wait on an event) and assert no
   line is appended.
6. **Escalation (AC10):** drive `c`, `t` and `w` to the final confirm and choose `cancel`,
   assert no apply and no answer file; assert the plan passed to `apply_withdraw` carries the
   `links.escalation` id; call `answer_escalation` directly with a stub owner for the three
   actions, a cancelled confirm, and a `PARKED` / `human_decision` and a non-parked campaign.
7. **Hygiene (AC11):** change the read-only test's filter to `kind:<fixture kind>` and assert
   the row count changes and the directory hash is unchanged; rewrite the placeholder loop.
8. **Regression (AC12):** run the affected tests
   (`python3 tools/affected_tests.py --run uv run pytest -q`) plus
   `tests/test_tui_campaign_records.py`, `tests/test_tui_mailbox_tab.py`,
   `tests/test_tui_escalations_tab.py`, `tests/test_tui_campaign_detail_ac.py`. The gate
   deselects `tui`-marked tests in its default marker expression, so the implementer runs
   the `tui` tests of these modules explicitly with `-m tui` and records the counts.
   `python3 tools/spec_lint.py` exits 0.

Manual: none beyond the Pilot drives; the touched surfaces are fail-soft helpers and tests.

## Rollout / migration

No data migration and no config. Lands as one change; the only user-visible effects are
the fail-soft Mail fallback, cleaned time cells, and null fields no longer matching
`severity:None` / `status:None`.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (affected tests and the explicit `-m tui`
  run of the four modules); the results are recorded in the spec.
- [ ] No regressions: affected tests green at the close gate; `python3 tools/spec_lint.py`
  exits 0 and `python3 tools/test_tiers.py --check` exits 0.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` regenerated (`python3 tools/spec_lint.py --write-ledger`).

## Open questions

None.
