---
id: SPEC-0446
title: "Real-fixture IdeaDesk Pilot harness and queue-window preparation"
status: accepted
owner: user
created: 2026-11-07
updated: 2026-11-07
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0405
tags: [tui, testing, agentic]
depends_on: [SPEC-0406, SPEC-0409, SPEC-0415]
source_idea: IDEA-967
---

## Context

Epic [SPEC-0405](./0405-rich-queue-tui-prioritized-live-queue-campaign.md) goal G7 is that TUI
tests drive the real `IdeaDesk` against sanitized real campaign data. Its child 3 was split.
[SPEC-0409](./0409-tui-test-harness-real-app-pilots-on-real-fixture.md) (done) removed the
Textual and pyte skip guards, added `tests/test_no_textual_skips.py`, and added the
`tests/tui_budget.py` runtime budget. [SPEC-0415](./0415-campaign-history-fixture-spec-0405-child.md)
(done) committed `tests/fixtures/agentic_history/` (eight closed campaigns, `MANIFEST.json`)
and `tests/history_scale.copy_fixture`.

This spec is the Pilot half of that remainder (IDEA-967): boot the real `IdeaDesk` on a
copy of the fixture, and move that copy's timestamps so the queue's 24 h delivered window
shows every fixture campaign in a determined order. Two other pieces of the same idea are
later specs, listed under Non-goals: the shared PTY text-snapshot path, and
`history_scale.clone_fixture`.

**What exists today**

- `IdeaDesk` (`src/nightwright/tui/app.py`) is the real app. `IdeaDesk(cfg, tab="queue")`
  opens the queue tab. Tests already drive it with `app.run_test` and
  `tests/waits.pilot_until`. Textual's `Pilot.double_click(widget, offset=(0, 0))` is
  `click(..., times=2)` and returns `True` when the selected widget was under the pointer.
  The queue table is `#queue` inside `#queue-view`.
- The queue shows terminal campaigns only when `queue.dashboard_payload` sees `updated_at`
  inside `recent_hours` (24) of wall-clock now. The TUI asks for `recent=20`
  (`agentic_model.load_queue`). The fixture's newest `updated_at` is
  `2026-11-03T21:14:52-05:00`. On any later day the delivered group is empty, so a Pilot
  pointed at a raw copy does not show a campaign id.
- On this fixture, every campaign's `updated_at` equals the `ts` of its last ledger
  `transition` into its current terminal state. `dashboard_payload` keeps a terminal
  campaign when that `updated_at` is at most 24 h old, sorts `delivered` by
  `finished_age_s` ascending (youngest finish first, and `finished_age_s` is the age of
  `updated_at`), and passes `state_since` as that transition `ts`.
  `rows_from_payload` ignores payload list order. For `delivered` it sorts by
  `state_since` epoch ascending, then reverses that key, so the newest finish is first
  ([SPEC-0410](./0410-live-queue-view-state-grouped-sections-with.md); pinned by
  `tests/test_tui_queue_groups.py`). That is the same order as the youngest-first payload.
  The header text is `delivered (N)`.
- The campaign column does not always paint the raw campaign id.
  `queue_tab._cells` prints `spec_id`, then the id with a leading `spec_id-` removed, and
  clips the result to 30 characters. `IDEA-606-20261101-df50` has `spec_id` `SPEC-0352`,
  so its painted ident is not the raw cid.
- There is no `tests/tui_harness.py`. `tests/history_scale.py` exposes `FIXTURE_DIR` and
  `copy_fixture(dst)` only.

## Goals / Non-goals

**Goals**

- **G1. Pilot harness.** `tests/tui_harness.py` copies the fixture into a tmp data dir,
  builds a cfg that cannot see the developer's real data or org, starts the real
  `IdeaDesk` under `run_test` at 160×48, waits with `pilot_until`, double-clicks through
  `Pilot.double_click`, and returns the in-process screen as plain text.
- **G2. Queue window.** One helper shifts that copy so all eight fixture campaigns fall
  inside the queue's 24 h delivered window, in one determined order, without touching the
  committed fixture.

**Non-goals**

- **Shared PTY renderer and the 160×48 queue text golden.** A later child of SPEC-0405,
  depending on this spec, extracts `render_pty` from `tests/test_tui_real_terminal.py`,
  keeps that file's 110×30 tree-mode and colour assertions and its `forkpty` skip, adds
  snapshot stabilization regenerated only when `NW_UPDATE_SNAPSHOTS` is `1`, and commits
  `tests/fixtures/tui_snapshots/queue_160x48.txt` for `tui --tab queue` against the shifted
  copy. This spec does not add `render_pty`, `stabilize`, `assert_snapshot`, or a snapshot
  directory.
- **Deterministic campaign scale-clone corpus.** A later child of SPEC-0405, depending on
  SPEC-0415, adds `place_bytes` and `clone_fixture` on `tests/history_scale.py` (hardlink
  inside the destination, copy fallback, collision-safe ids, 500 campaigns and at least
  50 000 record units). Its record rule is: retarget the record's existing campaign
  locator — replace top-level `campaign` when present, otherwise replace
  `source.campaign`; ledger findings are associated by their destination clone ledger and
  receive no new campaign field; fail if a supported source unit has neither locator. Its
  small-corpus test forces at least one appended finding, decision, event, mail, and
  escalation, and asserts that original lines remain a byte-for-byte prefix, targets are
  clone ids, sequence and id fields are max-plus-one and unique, the escalation filename
  equals its `id`, and timestamps use the target clone's delta. This spec does not add
  `clone_fixture`, those tests, or `tests/test_history_perf.py`. Timed history budgets
  stay with the cache spec that consumes the corpus.
- The Textual and pyte skip guards, `tests/test_no_textual_skips.py`, and
  `tests/tui_budget.py`. Those shipped in SPEC-0409. This spec must not bring a skip back.
- New TUI product behavior: no History tab, no Findings tab, no detail popup, and no
  double-click binding. SPEC-0410 still opens a campaign on Enter only. The harness
  checks that a double-click is delivered to `#queue`. It does not require a popup.
- Rewriting `tests/test_tui_queue_tab.py` or the other synthetic `test_tui_*` suites onto
  the fixture. They keep their own campaigns. One new file adopts the harness.
- A snapshot library such as `pytest-textual-snapshot`, and any SVG golden.
- Editing `tests/fixtures/agentic_history/`, the capture tool, production writers, or
  `tests/history_scale.py` beyond the `copy_fixture` SPEC-0415 already shipped.
- Importing `nightwright` from `tests/history_scale.py`.

## Decision

1. **The harness is test support, not a product seam.** `tests/tui_harness.py` is the only
   new way to boot `IdeaDesk` on the fixture. Production code grows no `NW_NOW`, no clock
   inject, and no snapshot hook. Wall-clock ages are made stable by shifting the copy.
2. **The queue window assigns finishes so the payload order and the row order name one
   first campaign.** For N manifest campaign ids sorted lexicographically, campaign `i`
   (0-based) is moved to `now - (N - i) hours`. The lexicographically first id is the
   oldest finish (`now - N hours`). The lexicographically last id is the youngest
   (`now - 1 hour`). `dashboard_payload` lists `delivered` youngest-first, and
   `rows_from_payload` lists that group newest-first, which is the same order. The first
   data row is the lexicographically last id, and the header is `delivered (8)`.
3. **The committed fixture stays read-only.** The helper refuses a `data_dir` inside
   `FIXTURE_DIR`. The Pilot session only reads the copy.

## Design

### 1. Harness: `tests/tui_harness.py`

```python
SIZE = (160, 48)   # width, height; run_test uses this

def fixture_cfg(tmp_path) -> dict
def prepare_queue_window(cfg, *, now=None) -> datetime
def visible_text(app) -> str
```

`fixture_cfg`:

- Calls `history_scale.copy_fixture(tmp_path / "data")`. `cfg["data_dir"]` is that copy
  (`…/data`, the directory that contains `agentic/`).
- Sets `config_dir`, `cache_dir`, `org_files`, `org_ideas_file`, `org_capture_file`, and
  `specs_dir` to other directories under `tmp_path`. Sets `org_todo_keywords` to
  `["TODO", "|", "DONE"]`, `org_idea_keywords` to
  `["IDEA", "INCUBATE", "SPECCED", "|", "PROMOTED", "DROPPED"]`, and
  `org_question_keywords` to `["OPEN", "|", "RESOLVED"]`. The ideas file is created with
  a single `#+TODO: IDEA | PROMOTED` line and no headlines, so the ideas pane mounts and
  stays empty.
- Sets `agentic.runner` to `fake` and `agentic.fake_script` to a script that exits 0.
- Sets `timezone` to `America/New_York` and `_tz` to that zone.
- Does not read `NW_DATA_DIR`, `NW_CONFIG_DIR`, `~/work/org`, or
  `~/.local/share/nightwright`.

`visible_text(app)` joins `app.screen.render_line(y).text.rstrip()` for every row of
`app.screen.size`. That is the in-process screen.

### 2. Queue window: `prepare_queue_window`

The TUI calls `dashboard_payload` without a `now`, so the helper moves timestamps on the
copy until the wall clock already in the process is the right `now`.

- Refuses, with `ValueError`, if the resolved `cfg["data_dir"]` is inside
  `history_scale.FIXTURE_DIR` (equal to it, or a path under it) or does not contain
  `agentic/campaigns/`.
- `now` defaults to `datetime.now().astimezone()`. The function returns that `now`.
- Campaigns are the `cid` values in `FIXTURE_DIR/MANIFEST.json` `campaigns`, sorted
  lexicographically. N is that count (8). A listed cid with no directory under the copy
  raises `ValueError`.
- For index `i` (0-based) the anchor is moved to `now - (N - i) hours`. The anchor is the
  `ts` of the last ledger event with `type == "transition"` and `to` equal to the
  campaign's current `state`. A campaign with no such event uses `state.json`
  `updated_at`, then `created_at`. The delta is `target - anchor`.
- That one delta is added to every ISO-8601 datetime token in that campaign's files and
  to every global-log line (`conductor/decisions.jsonl`, `events.jsonl`,
  `mailbox/*.jsonl`, `escalations/*.json`, `spec-reviews.jsonl`) whose top-level
  `campaign` or nested `source.campaign` equals that cid. A datetime token is
  `YYYY-MM-DDTHH:MM:SS` with optional fractional seconds and an optional `Z` or numeric
  offset. Date-only strings, campaign ids, numeric `duration_s`, costs, and ids are not
  modified. Lines that do not name that cid are copied byte for byte. `runner.jsonl` is
  not modified: the shifted copy is for the queue UI, not for runner-down history.
- Re-serialization of a rewritten token uses `timespec="seconds"` and the original offset
  (`Z` stays `Z`). The difference between the first and last ledger `ts` of a campaign
  stays within 1 second of the pre-shift difference. `runner.jsonl` bytes are unchanged.

After this, at that `now`:

- `dashboard_payload(..., recent=20)` puts all eight campaigns in `delivered`,
  youngest finish first. The first id in that list is the lexicographically last
  manifest cid (`now - 1 hour`). The last id is the lexicographically first cid
  (`now - N hours`).
- `rows_from_payload` orders `delivered` newest `state_since` first. Because the helper
  moves `updated_at` and the terminal transition by the same delta, and on this fixture
  those two timestamps start equal, that row order is the payload order. The first data
  row is the lexicographically last manifest cid. The header text is `delivered (8)`.

### 3. What the new tests drive

`tests/test_tui_fixture_harness.py` imports `textual` at module level. It does not call
`importorskip("textual")` and does not `skipif` on `textual_available`.

Queue-window checks call `prepare_queue_window` and then `dashboard_payload` /
`rows_from_payload` directly, with an explicit `now`, so the assignment does not depend
on how long the process has been alive.

Pilot checks build `fixture_cfg`, call `prepare_queue_window` (default `now`), start
`IdeaDesk(cfg, tab="queue")` with `run_test(size=SIZE)`, and `pilot_until` the screen
contains `delivered (8)`. The wait uses `waits.pilot_until`. Order is read from the
mounted `#queue-view` rows whose `kind` is `campaign` and whose `group` is `delivered`,
not from a raw cid substring of `visible_text`. `visible_text` must contain
`delivered (8)` and the clipped ident of the first of those rows. The clipped ident is
what `queue_tab._cells` paints: `spec_id`, a space, the campaign id with a leading
`spec_id-` removed when that prefix is present, then clipped to 30 characters.

`await pilot.double_click("#queue", offset=(2, 2))` returns `True`. A `Click` handler
installed on `#queue` for the call observes one click with `chain == 2`. `len(app.screen_stack)`
is unchanged and the app is still the `IdeaDesk` screen. No assertion that a campaign
screen was pushed.

The unshifted Pilot uses the same boot against `fixture_cfg` before
`prepare_queue_window`. It first requires that wall-clock now is more than 24 h after
`2026-11-03T21:14:52-05:00`; if it is not, the test fails with that fact rather than
skipping. `visible_text` then contains none of the manifest cids and no `delivered (`
header.

Hashes cover every file under the copy (`cfg["data_dir"]`) before `run_test` and after
the context exits: same relative paths, same sha256. `prepare_queue_window` runs before
the first hash. The session only looks. `cache_dir` is outside that tree.

## Alternatives considered

- **Keep the PTY golden and `clone_fixture` in this spec.** Each has its own API, tests,
  and failure mode. Review split them out so this spec can be accepted and implemented
  on its own.
- **Freeze time with an `NW_NOW` read in `dashboard_payload`.** That is a production seam
  for a test problem. Shifting the copy leaves the queue code on the wall clock.
- **Point the Pilot at the unshifted fixture and accept an empty queue.** The screen
  would not contain a real campaign id, which misses G1.
- **Assign `now - (i + 1) hours`.** That makes the lexicographically first id the
  youngest, so both the youngest-first payload and the newest-first rows would put that
  id first. The assignment this spec uses is `now - (N - i) hours`, which makes the
  lexicographically last id the youngest and therefore the first data row.
- **Treat the `rows_from_payload` epoch reverse as an inversion of the youngest-first
  payload.** The reverse turns an ascending `state_since` sort into newest-first, which
  matches the payload. Inverting the payload would put the oldest id first and would
  disagree with `tests/test_tui_queue_groups.py`.

## Acceptance criteria

- [ ] **AC1.** `fixture_cfg(tmp_path)` returns a data dir whose campaign-file sha256 values
  equal `MANIFEST.json`, with no inode shared with `FIXTURE_DIR`. `org_files`,
  `data_dir`, `config_dir`, `cache_dir`, `specs_dir`, `org_ideas_file`, and
  `org_capture_file` are under `tmp_path`. With `NW_DATA_DIR`, `NW_CONFIG_DIR`, and
  `HOME` pointed at empty sentinel directories, those sentinels gain no files and the
  returned paths stay under `tmp_path`.
- [ ] **AC2.** Before `prepare_queue_window`, `IdeaDesk` on that cfg, queue tab, 160×48,
  has `visible_text` that contains none of the manifest cids and no `delivered (` header.
  The test fails, and does not skip, when wall-clock now is not more than 24 h after
  `2026-11-03T21:14:52-05:00`.
- [ ] **AC3.** For N manifest cids sorted lexicographically, after
  `prepare_queue_window(cfg, now=now)`, campaign `i`'s anchor timestamp equals
  `now - (N - i) hours` within 1 second. `dashboard_payload(..., recent=20, now=now)`
  lists `delivered` youngest-first, and the first id in that list is the
  lexicographically last manifest cid. `rows_from_payload` of that payload has that same
  cid as its first `delivered` data row, and the delivered header count is 8.
- [ ] **AC4.** For one shifted campaign, the first-to-last ledger `ts` gap is unchanged
  within 1 second, and `runner.jsonl` is byte-identical across the shift. Global-log
  lines whose `campaign` and `source.campaign` are not that cid are byte-identical.
  `prepare_queue_window` raises `ValueError` when `data_dir` is inside `FIXTURE_DIR` and
  when `agentic/campaigns/` is missing. The committed fixture's manifest `--check` still
  exits 0.
- [ ] **AC5.** After `prepare_queue_window`, the Pilot screen's `visible_text` contains
  `delivered (8)` and the clipped ident of the first mounted `delivered` data row. That
  row's campaign id is the lexicographically last manifest cid, and the mounted
  `delivered` campaign ids are exactly the manifest cids.
- [ ] **AC6.** `double_click("#queue", offset=(2, 2))` returns `True`, the `Click`
  observed on `#queue` has `chain == 2`, and `len(app.screen_stack)` is unchanged. The
  app is still on the `IdeaDesk` screen.
- [ ] **AC7.** Campaign files and global logs under the copy are byte-identical after the
  Pilot session (same paths, same sha256). `tests/test_tui_fixture_harness.py` imports
  `textual` unconditionally. `tests/test_no_textual_skips.py` passes.

## Test plan

**Automated tests.** Run the listed focused files while implementing, then close with
`python3 tools/affected_tests.py --run uv run pytest -q` plus the touched test files; do
not run the full suite manually. No Textual skip.

- `tests/test_tui_fixture_harness.py`
  - `test_fixture_cfg_is_isolated` (AC1): manifest sha256 values, no shared inode, cfg
    paths under `tmp_path`, sentinel env dirs stay empty.
  - `test_unshifted_copy_hides_closed_campaigns` (AC2): `visible_text` has no manifest
    cid and no `delivered (` header; fails closed when the clock is inside the fixture
    window.
  - `test_queue_window_order_matches_assignment` (AC3): explicit `now`, anchor times,
    `dashboard_payload` youngest-first, `rows_from_payload` first delivered row is the
    lexicographically last cid, header count 8.
  - `test_prepare_queue_window_preserves_span_and_runner` (AC4): ledger gap within 1 s,
    `runner.jsonl` byte-identical, unrelated global-log lines byte-identical.
  - `test_prepare_queue_window_refuses_the_committed_fixture` (AC4): `ValueError` for
    `FIXTURE_DIR` and for a data dir with no `agentic/campaigns/`.
  - `test_shifted_queue_lists_every_fixture_campaign` (AC5): `delivered (8)`, mounted
    ids, clipped ident of the first data row present in `visible_text`.
  - `test_double_click_dispatches_to_the_queue_table` (AC6): return value `True`,
    `Click.chain == 2`, screen stack unchanged.
  - `test_pilot_session_does_not_write_the_copy` (AC7): sha256 of every file under the
    copy, before `run_test` and after the context exits, plus
    `python3 tools/capture_history_fixture.py --check` still exits 0.
- `tests/test_no_textual_skips.py` stays green (AC7). `tests/test_history_fixture.py`
  stays green. `copy_fixture` behavior is unchanged.

**Manual verification**

1. `python3 tools/affected_tests.py --run uv run pytest -q tests/test_tui_fixture_harness.py tests/test_no_textual_skips.py tests/test_history_fixture.py`
   is green. None of those tests are skipped for a missing Textual.
2. Confirm `python3 tools/capture_history_fixture.py --check` still exits 0.
3. Confirm `git status` shows no change under `tests/fixtures/agentic_history/`.

**Regression guard**

- SPEC-0409's guard and budget tests stay green. The new TUI file is inside the budget's
  `test_tui_*.py` scope. Each Pilot test stays under the per-test limit (30 s).
- `tests/test_tui_queue_tab.py` is not edited.
- `tests/test_tui_real_terminal.py` is not edited.
- `python3 tools/spec_lint.py` exits 0 after `--write-ledger`.

## Rollout / migration

1. Add `tests/tui_harness.py` (`fixture_cfg`, `prepare_queue_window`, `visible_text`).
2. Add `tests/test_tui_fixture_harness.py` and run the focused file until AC1–AC7 hold.
3. No data migration and no production code change. The PTY-golden spec and the
   scale-clone spec are written separately, with the dependencies and the contracts in
   Non-goals.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`python3 tools/affected_tests.py --run uv run pytest -q`, plus the files touched). Manual steps performed and noted.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` regenerated (`python3 tools/spec_lint.py --write-ledger`) and `python3 tools/spec_lint.py` exits 0.

## Open questions

None. Decided while revising:

| Question | Decision | Why |
|---|---|---|
| Which slice does SPEC-0446 keep? | The real-fixture Pilot harness and the queue window | The PTY golden and the scale-clone corpus are separate subsystems |
| Which campaign is the first delivered row? | The lexicographically last manifest cid | `now - (N - i) hours` makes that cid the youngest, and both the payload and `rows_from_payload` list youngest / newest first |
| How does the queue show closed campaigns from 2026-11-03? | By shifting the copy | A production clock override is a product seam for a test problem |
