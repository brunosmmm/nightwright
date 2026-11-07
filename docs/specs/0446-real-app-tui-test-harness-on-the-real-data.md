---
id: SPEC-0446
title: "Real-app TUI test harness on the real-data fixture"
status: proposed
owner: user
created: 2026-11-07
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
`tests/tui_budget.py` runtime budget. It left the rest, because the fixture did not exist yet:
the Pilot harness, the plain-text pyte snapshots, and the scale-clone helper. This spec is
that second half (IDEA-967).

The fixture landed separately.
[SPEC-0415](./0415-campaign-history-fixture-spec-0405-child.md) (done) committed
`tests/fixtures/agentic_history/` (eight closed campaigns, `MANIFEST.json`, sanitization) and
`tests/history_scale.copy_fixture`. It deferred `clone_fixture`. SPEC-0406's cache-and-perf
follow-up is what times a 500-campaign, about-50 000-record corpus; it cannot do that until
this helper exists. This spec ships the helper and does not ship those timings.

**What exists today**

- `IdeaDesk` (`src/nightwright/tui/app.py`) is the real app. `nw tui --tab queue` opens the
  queue. Tests already drive it with `app.run_test` and `tests/waits.pilot_until`. Textual's
  `Pilot.double_click(widget, offset=(0, 0))` exists (it is `click(..., times=2)`). The queue
  table is `#queue` inside `#queue-view`.
- The queue shows terminal campaigns only when `queue.dashboard_payload` sees them inside
  `recent_hours` (24) of wall-clock now, and the TUI asks for `recent=20`
  (`agentic_model.load_queue`). The fixture's newest ledger timestamp is
  `2026-11-03T21:14:52-05:00`. On any later day the delivered group is empty, so a Pilot pointed
  at a raw copy does not show a campaign id.
- Ages render through `cli_queue._ago` (`45s`, `11m`, `3h`, `2d`). Delivered rows say
  `finished Nh ago`. The status line is `header_line` (`runner … · hb … · today $…`). With no
  lease file in the fixture the runner is `down` and `hb` is `-`.
- [SPEC-0115](./0115-desk-tree-mode-is-not-a-tree-needs-real-expand.md)'s oracle is
  `tests/test_tui_real_terminal.py`: a pty, `pyte.Screen`, and a condition wait
  (`tests/waits.WAIT_S`), at 110×30, against a throwaway org. It is one slow test on purpose.
  Nothing in the tree writes a text golden. `NW_UPDATE_SNAPSHOTS=1` is already the regenerate
  switch for the queue-CLI text goldens in `tests/test_agentic_queue_visibility.py`.
- `tests/history_scale.py` exposes `FIXTURE_DIR` and `copy_fixture(dst)` only. It imports
  nothing from `nightwright`. Campaign ids match `store.new_campaign_id`:
  `<prefix>-<YYYYMMDD>-<4 hex>`. Escalation ids match `ESC-(\d{4,})`. Mail ids are
  `{inbox}:{seq}`.
- There is no `tests/tui_harness.py` and no `clone_fixture`.

## Goals / Non-goals

**Goals**

- **G1. Pilot harness.** `tests/tui_harness.py` copies the fixture into a tmp data dir, builds
  a cfg that cannot see the developer's real data or org, starts the real `IdeaDesk` under
  `run_test` at 160×48, waits with `pilot_until`, double-clicks through `Pilot.double_click`,
  and returns the screen as plain text.
- **G2. Queue window.** One helper shifts the copy so all eight fixture campaigns fall inside
  the queue's 24 h delivered window, in a deterministic order, without touching the committed
  fixture.
- **G3. Pyte text snapshot.** One 160×48 golden of the queue, rendered by the SPEC-0115 pty
  path, regenerated only with `NW_UPDATE_SNAPSHOTS=1`. The existing tree-mode pty test calls
  the same pump and keeps its assertions.
- **G4. Scale clone.** `tests/history_scale.clone_fixture` builds a deterministic,
  collision-safe corpus of 500 campaigns and at least 50 000 record units for the later perf
  spec, using dst-local hardlinks with a copy fallback.

**Non-goals**

- The Textual and pyte skip guards, `tests/test_no_textual_skips.py`, and `tests/tui_budget.py`.
  Those shipped in SPEC-0409. This spec must not bring a skip back. The `forkpty` skip in the
  real-terminal test stays; SPEC-0409 allows it.
- History cache, `--rebuild-cache`, and the timed budgets (cold index, warm load, popup open,
  50 000-record query, live-queue repaint). The cache spec consumes `clone_fixture` and asserts
  the budgets. This spec does not time them and does not add `tests/test_history_perf.py`.
- New TUI product behavior: no History tab, no Findings tab, no detail popup, no double-click
  binding. SPEC-0410 still opens a campaign on Enter only. The harness exposes `double_click`;
  it does not require a popup.
- Rewriting `tests/test_tui_queue_tab.py` or the other synthetic `test_tui_*` suites onto the
  fixture. They keep their own campaigns. One new file adopts the harness.
- A snapshot library such as `pytest-textual-snapshot`, and any SVG golden.
- Editing `tests/fixtures/agentic_history/`, the capture tool, or production writers.
- Importing `nightwright` from `tests/history_scale.py`.

## Decision

1. **The harness is test support, not a product seam.** `tests/tui_harness.py` is the only new
   way to boot `IdeaDesk` on the fixture. Production code grows no `NW_NOW`, no clock inject,
   and no snapshot hook. Wall-clock ages are made stable by shifting the copy. `stabilize`
   masks the heartbeat, today's spend, and the finished-age token before a golden is compared.
2. **One pyte pump, two callers.** The pty loop moves out of
   `tests/test_tui_real_terminal.py` into `render_pty`. The tree test passes the same loaded
   check, keys, and 110×30 size it uses today. The new golden uses 160×48 and
   `tui --tab queue` against the shifted copy.
3. **`clone_fixture` extends `tests/history_scale.py`.** SPEC-0415 already put `copy_fixture`
   there and named this function as the follow-up. The eight committed campaigns are copied
   byte for byte and then left alone. Extra campaigns and extra record units are append-only.
   Ids are rewritten as whole campaign-id tokens, longest first. A leftover raw
   substring of the source id is replaced only when it is not inside a different campaign id.
4. **The perf corpus is volume, not a second fixture.** Clones are valid JSON that readers
   accept. They are not required to preserve finding links, triage, or runner-down overlap.
   The unshifted `copy_fixture` result remains the corpus for anything that checks real
   history numbers.

## Design

### 1. Harness: `tests/tui_harness.py`

```python
SIZE = (160, 48)   # width, height; run_test and the golden both use this

def fixture_cfg(tmp_path) -> dict
def prepare_queue_window(cfg, *, now=None) -> datetime
def visible_text(app) -> str
def stabilize(text) -> str
def assert_snapshot(name, text) -> None
# SNAPSHOT_DIR: tests/fixtures/tui_snapshots, a module global tests may monkeypatch
def render_pty(env, *, loaded, steps, cols, rows, argv_tail=("tui",)) -> tuple[list[str], list]
```

`fixture_cfg`:

- Calls `history_scale.copy_fixture(tmp_path / "data")`. `cfg["data_dir"]` is that copy
  (`…/data`, the directory that contains `agentic/`).
- Sets `config_dir`, `cache_dir`, `org_files`, `org_ideas_file`, `org_capture_file`, and
  `specs_dir` to other directories under `tmp_path`. The ideas file is created with a single
  `#+TODO: IDEA | PROMOTED` line and no headlines, so the ideas pane mounts and stays empty.
- Sets `agentic.runner` to `fake` and `agentic.fake_script` to a script that exits 0.
- Sets `timezone` to `America/New_York` and `_tz` to that zone.
- Does not read `NW_DATA_DIR`, `~/work/org`, or `~/.local/share/nightwright`.

`visible_text(app)` joins `app.screen.render_line(y).text.rstrip()` for every row of
`app.screen.size`. That is the in-process screen, the same shape as pyte's `screen.display`.

`stabilize` masks only:

- `hb <token>s` becomes `hb <hb>s`
- `today $<amount>` becomes `today $<spend>`
- `finished <N><s|m|h|d> ago` becomes `finished <age> ago`

Campaign ids, the `delivered (N)` header, column titles, states, and titles are left as
rendered. `_ago` is coarse and the window stagger is whole hours, so a run that finishes
within 30 minutes of `prepare_queue_window` does not cross an hour bucket; the mask is what
keeps a heartbeat or a midnight spend-today from failing the golden.

`assert_snapshot(name, text)` compares `stabilize(text)` to `SNAPSHOT_DIR / f"{name}.txt"`.
`SNAPSHOT_DIR` is `tests/fixtures/tui_snapshots` next to this file's package. When
`NW_UPDATE_SNAPSHOTS` is `1` it writes that file (creating the directory) and then compares,
so the run still fails if the write did not land. Any other value compares only.

`render_pty` is the SPEC-0115 loop moved verbatim in behavior:

- `pty.fork`, child env `TERM=xterm-256color`, `COLUMNS`, `LINES`, plus the caller's `env`.
- Child argv is `[sys.executable, "-m", "nightwright.cli", *argv_tail]`.
- Parent sets the pty size, feeds `pyte.ByteStream`, and waits on `loaded(lines)` and then
  each `(key, ready)` with `waits.WAIT_S`. No fixed sleep before a positive assertion.
- Returns `(lines, cells)` as today. The `finally` block still sends `q` and reaps the child.
- Unknown multi-character keys still raise. `ENTER`, `ESC`, `SPACE`, and `TAB` stay the named
  keys.

`tests/test_tui_real_terminal.py` deletes its private `_render` and calls `render_pty`. Its
org, its `loaded` check (`IDEA-001` in the left pane), its keys, its 110×30 size, and its
assertions stay. The `forkpty` skip stays.

### 2. Queue window: `prepare_queue_window`

The TUI calls `dashboard_payload` without a `now`, so the helper moves timestamps on the copy
until the wall clock already in the process is the right `now`.

- Refuses, with `ValueError`, if `cfg["data_dir"]` is inside `history_scale.FIXTURE_DIR` or
  does not contain `agentic/campaigns/`.
- `now` defaults to `datetime.now().astimezone()`.
- Campaigns are the manifest cids, sorted lexicographically. For index `i` (0-based) the
  delta is whatever moves that campaign's last ledger `transition` into its current terminal
  state to `now - (i + 1) hours`. A campaign with no such event uses `state.json`
  `updated_at`, then `created_at`.
- That one delta is added to every ISO-8601 timestamp in that campaign's files and to every
  global-log line (`conductor/decisions.jsonl`, `events.jsonl`, `mailbox/*.jsonl`,
  `escalations/*.json`, `spec-reviews.jsonl`) whose `campaign` or `source.campaign` equals
  that cid. Numeric `duration_s`, costs, and ids are not modified. `runner.jsonl` is not
  modified: the shifted copy is for the queue UI, not for runner-down history.
- Re-serialization uses `timespec="seconds"` and the original offset. The difference between
  the first and last ledger `ts` of a campaign stays within 1 second of the pre-shift
  difference. `runner.jsonl` bytes are unchanged.
- Returns `now`.

After this, `dashboard_payload(..., recent=20)` at that `now` puts all eight campaigns in
`delivered`, youngest finish first. `rows_from_payload` reverses that group, so the
lexicographically first cid is the first data row and the header text is `delivered (8)`.

### 3. What the new tests drive

`tests/test_tui_fixture_harness.py`:

- Builds `fixture_cfg`, calls `prepare_queue_window`, starts `IdeaDesk(cfg, tab="queue")`
  with `run_test(size=SIZE)`, and `pilot_until` the screen contains `delivered (8)` and every
  manifest cid. The wait uses `waits.pilot_until`.
- Asserts the first data row under the delivered header is the lexicographically first
  manifest cid. Reads that from `visible_text` (the cid is on the first content line that
  contains a manifest cid).
- `await pilot.double_click("#queue", offset=(2, 2))` returns `True`. The app is still
  running afterwards. No assertion that a screen was pushed or not pushed.
- Hashes every file under the copy before `run_test` and after the context exits. Same
  relative paths, same sha256. The session only looks.
- Pyte: write a tiny `config.yaml` under the cfg's `config_dir` with the same org and data
  paths, spawn `render_pty` with `argv_tail=("tui", "--tab", "queue")`, `cols=160`,
  `rows=48`, and `loaded` true once `stabilize` of the screen contains `delivered (8)`.
  `assert_snapshot("queue_160x48", "\n".join(lines))`. The child env sets `NW_DATA_DIR` and
  `NW_CONFIG_DIR` to the copy and the temp config, and `NW_CACHE_DIR` to the temp cache.
  The committed golden is `tests/fixtures/tui_snapshots/queue_160x48.txt`.

The in-process test does not compare itself to the golden. Pyte and `render_line` differ by
cursor and chrome; both must contain `delivered (8)` and every manifest cid after `stabilize`.

### 4. Scale clone: `history_scale.clone_fixture`

```python
def place_bytes(path: Path, data: bytes, pool: dict[str, Path]) -> None
def clone_fixture(dst: Path, *, campaigns: int = 500, records: int = 50_000,
                  seed: int = 0) -> Path
```

`place_bytes` writes `data` to `path`. `pool` maps the sha256 of `data` to a path already
under the destination. When the hash is present and that path exists, it `os.link`s it onto
`path`. On `OSError` it writes the bytes instead. It never links to a path outside `dst`.
The caller registers the new path in `pool`.

`clone_fixture`:

- `dst` must not exist or must be empty, else `FileExistsError` (same rule as `copy_fixture`).
- `campaigns` smaller than the manifest's campaign count, or `campaigns > 2000`, or
  `records > 200_000`, raises `ValueError` naming the limit. Defaults are the perf corpus.
- Copies the eight originals with `copy_fixture`'s plain copies (never a link to
  `FIXTURE_DIR`). Their campaign directories stay byte-identical to the committed fixture
  for the rest of the call. Their bytes are then registered in the `place_bytes` pool.
- Appends clones until `dst/agentic/campaigns` has `campaigns` directories.
  - Source campaign rotates through the originals in sorted-cid order.
  - Clone index `n` starts at 1. The new id keeps the source prefix, uses the source date
    plus `n` days, and takes the 4 hex from
    `sha256(f"{seed}:{n}:{source_cid}:{k}")` for `k = 0, 1, …` until the full id is not
    already taken and neither id contains the other as a campaign-id span.
  - A campaign-id span is the same match `store._campaign_id_spans` uses
    (`-<YYYYMMDD>-<4 hex>` extended left over id characters). Replacement changes a span
    whose text equals the source id, longest span first. Any remaining raw substring of the
    source id is replaced too, unless that occurrence sits inside a different campaign-id
    span, in which case `clone_fixture` raises `ValueError`. Generated ids differ in the
    date, so the full source id is not a substring of the new id. After the rewrite a byte
    scan of the clone directory does not contain the source id.
  - Every ISO-8601 timestamp in the clone moves by `n` days. Differences between ledger
    timestamps stay within 1 second. Files are placed with `place_bytes`.
- A **record unit** is one of: a `conductor/decisions.jsonl` line, an `events.jsonl` line, a
  `mailbox/*.jsonl` line, an `escalations/ESC-*.json` file, a ledger line whose JSON `type`
  is `finding`. Originals already count. Copied clone ledgers count. While the destination
  has fewer than `records` units, append more, cycling finding, decision, event, mail,
  escalation. Each appended unit is a deep copy of an original unit (round-robin over
  originals only), then:
  - `campaign` becomes the next clone id, never an original id. Findings are appended to
    that clone's ledger, not to an original ledger.
  - `seq`, event `id`, and mail `seq` become one greater than the max already in that file.
    Mail `id` stays `{inbox}:{seq}`. Escalation files are the next `ESC-{n:04d}` (`n` past
    9999 still matches `ESC-(\d{4,})`) and their `id` field matches the filename.
  - `ts` / `created` move by that clone's delta.
  - Original log lines stay a byte-for-byte prefix of the file. New lines are appended.
- `records` below the count already produced by the campaign copies adds nothing and deletes
  nothing.
- Same `dst` arguments and `seed` produce byte-identical output. A different `seed` produces
  a different set of clone ids. No network and no git. The module still imports nothing from
  `nightwright`.

Clones do not retarget `followup` keys, triage waives, or `runner.jsonl`. A test that wants
the real links uses `copy_fixture` alone.

## Alternatives considered

- **`tests/scale_clone.py` as SPEC-0409 sketched.** SPEC-0409 said that filename was not a
  constraint. SPEC-0415 already owns `tests/history_scale.py` and named `clone_fixture` as
  the follow-up, next to `copy_fixture` and `FIXTURE_DIR`.
- **Freeze time with an `NW_NOW` read in `dashboard_payload`.** That is a production seam for
  a test problem. Shifting the copy leaves the queue code on the wall clock.
- **Point the Pilot at the unshifted fixture and accept an empty queue.** The golden would
  not contain a real campaign id, which misses G1.
- **Byte-compare the in-process `render_line` text to the pyte golden.** Cursor and chrome
  differ. The golden is the pty screen; the Pilot test asserts the campaign ids and the
  `delivered (8)` header on its own text.
- **Move `tests/test_tui_queue_tab.py` onto the fixture.** Those tests park, resume, and
  abandon synthetic campaigns. The fixture is closed, sanitized, and shared. Mixing them
  would slow the file and put writes on the only real-data copy.
- **Count `records` by calling `history.campaign_history`.** `history_scale.py` must stay
  free of `nightwright`. The unit definition is source rows the history layer turns into
  records. The cache spec measures derived rows itself.

## Acceptance criteria

- [ ] **AC1.** `fixture_cfg(tmp_path)` returns a data dir whose campaign-file sha256 values
  equal the manifest, with no inode shared with `FIXTURE_DIR`. `org_files` and `data_dir`
  are under `tmp_path`. `IdeaDesk` on that cfg, queue tab, 160×48, reaches `delivered (8)`
  only after `prepare_queue_window`; before the shift the same text contains none of the
  manifest cids.
- [ ] **AC2.** After `prepare_queue_window`, `visible_text` contains every manifest cid and
  the first manifest cid in lexicographic order is the first of those hits. `double_click`
  on `#queue` at offset `(2, 2)` returns `True`. For one campaign, the first-to-last ledger
  `ts` gap is unchanged within 1 second, and `runner.jsonl` is byte-identical across the
  shift. Campaign files and global logs under the copy are byte-identical after the Pilot
  session. The committed fixture's manifest hashes are unchanged.
  `prepare_queue_window` raises `ValueError` when `data_dir` is inside `FIXTURE_DIR`.
- [ ] **AC3.** `render_pty` of `tui --tab queue` at 160×48 against the shifted copy matches
  `tests/fixtures/tui_snapshots/queue_160x48.txt` after `stabilize`. With
  `NW_UPDATE_SNAPSHOTS=1` the test writes that file. Without it, a one-byte drift fails and
  names the file. The golden contains `delivered (8)` and every manifest cid.
- [ ] **AC4.** `tests/test_tui_real_terminal.py` still passes its tree-mode and colour
  assertions at 110×30, calling `render_pty`. Its only skip is the `forkpty` skip.
- [ ] **AC5.** `clone_fixture(dst)` (the defaults) creates 500 campaign directories and at
  least 50 000 record units, finishes in under 60 s, and leaves each original campaign
  directory byte-identical to the manifest. Two calls with the same seed are byte-identical;
  `seed=1` yields a different clone-id set. A clone directory's bytes do not contain its
  source cid. No file under `dst` shares an inode with `FIXTURE_DIR`.
- [ ] **AC6.** `place_bytes` hardlinks when the destination already holds the same bytes, and
  writes a normal file when `os.link` raises `OSError`. `clone_fixture` raises `ValueError`
  for `campaigns` below the manifest count, for `campaigns > 2000`, and for
  `records > 200_000`. `history_scale.py` imports nothing from `nightwright`.
- [ ] **AC7.** On a clone of `campaigns=12, records=400`, `store.list_campaigns(...,
  include_terminal=True)` lists every campaign directory, `queue.dashboard_payload` returns
  without raising, and `history.campaign_history` on one clone returns
  `schema == "nw.campaign.history.v1"`. The first and last ledger `ts` of that clone differ
  from the source's first and last by the same amount, within 1 second.
- [ ] **AC8.** `tests/test_no_textual_skips.py` passes. The new TUI test file imports
  `textual` unconditionally. `python3 tools/spec_lint.py` exits 0 once the ledger is
  regenerated, and the affected tests pass.

## Test plan

**Automated tests.** Plain `uv run pytest`. No Textual skip. The `forkpty` skip stays on the
pty tests only.

- `tests/test_tui_fixture_harness.py`
  - `test_unshifted_copy_hides_closed_campaigns` (AC1): `visible_text` of the queue has no
    manifest cid.
  - `test_shifted_queue_lists_every_fixture_campaign` (AC1, AC2): `delivered (8)`, every
    manifest cid, lexicographically first cid first.
  - `test_double_click_hits_the_queue_table` (AC2): return value `True`.
  - `test_pilot_session_does_not_write_the_copy` (AC2): sha256 of every file under the copy,
    before and after, plus the committed fixture's manifest `--check` still exits 0.
  - `test_prepare_queue_window_refuses_the_committed_fixture` (AC2).
  - `test_pyte_queue_snapshot` (AC3): `render_pty`, `assert_snapshot("queue_160x48", ...)`.
    Skip only on `not hasattr(os, "forkpty")`.
  - `test_snapshot_updates_only_with_env` (AC3): a tmp golden path is not written unless
    `NW_UPDATE_SNAPSHOTS=1`. This calls `assert_snapshot` with the name pointed at `tmp_path`
    by monkeypatching the snapshot directory the helper uses (one module global,
    `SNAPSHOT_DIR`).
- `tests/test_tui_real_terminal.py` (AC4): unchanged assertions, now via `render_pty`.
- `tests/test_history_scale.py`
  - `test_clone_small_is_deterministic` (AC5, AC7): `campaigns=12, records=400`, two dirs,
    equal file hashes; `seed=1` differs in clone ids; original dirs match the manifest;
    source cid absent from the clone dir; ledger timestamp gap preserved within 1 s;
    `list_campaigns`, `dashboard_payload`, and `history.campaign_history` as in AC7.
  - `test_clone_defaults_hit_the_perf_corpus` (AC5): defaults, 500 dirs, at least 50 000
    record units, under 60 s, no inode shared with `FIXTURE_DIR`.
  - `test_place_bytes_links_and_falls_back` (AC6): same bytes share an inode; `os.link`
    patched to raise `OSError` writes a distinct inode with the same bytes.
  - `test_clone_rejects_limits` (AC6): the three `ValueError` cases.
  - `test_history_scale_imports_no_nightwright` (AC6): AST of `tests/history_scale.py`, every
    import is outside `nightwright`.
- `tests/test_no_textual_skips.py` and `tests/test_history_fixture.py` stay green (AC2, AC8).
  `copy_fixture` behavior is unchanged.

**Manual verification**

1. `uv run pytest -q tests/test_tui_fixture_harness.py tests/test_history_scale.py
   tests/test_tui_real_terminal.py tests/test_no_textual_skips.py tests/test_history_fixture.py`
   is green. The pty tests are not skipped on this Linux host.
2. Run the snapshot test once with `NW_UPDATE_SNAPSHOTS=1` and commit
   `tests/fixtures/tui_snapshots/queue_160x48.txt`. Run it again without the variable and
   confirm it passes without modifying the file (`git status` clean for that path).
3. Confirm `python3 tools/capture_history_fixture.py --check` still exits 0.

**Regression guard**

- SPEC-0409's guard and budget tests stay green. The new TUI file is inside the budget's
  `test_tui_*.py` scope; one Pilot plus one pty spawn must stay under the per-test limit
  (30 s). The 500-campaign clone lives in `tests/test_history_scale.py`, outside that budget.
- `tests/test_tui_queue_tab.py` is not edited.
- `python3 tools/spec_lint.py` exits 0 after `--write-ledger`.

## Rollout / migration

1. Add `place_bytes` and `clone_fixture` and `tests/test_history_scale.py`. No TUI change.
2. Add `tests/tui_harness.py`, move the pty pump, and point
   `tests/test_tui_real_terminal.py` at it. Run that file and confirm the tree assertions
   still pass.
3. Add `prepare_queue_window`, the Pilot tests, and the golden. Generate the golden once
   with `NW_UPDATE_SNAPSHOTS=1` and commit it.
4. No data migration and no production code change. The cache spec calls
   `clone_fixture(dst)` with the defaults when it adds the timed budgets.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`python3 tools/affected_tests.py --run uv run
  pytest -q`, plus the files touched). Manual steps performed and noted.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` regenerated (`python3 tools/spec_lint.py --write-ledger`) and
  `python3 tools/spec_lint.py` exits 0.

## Open questions

None. Decided while writing:

- **Where does the clone live?** On `tests/history_scale.py`, next to `copy_fixture`.
- **How does the queue show closed campaigns from 2026-11-03?** By shifting the copy, not by
  a production clock override.
- **What is a record unit?** A decision line, an event line, a mail line, an escalation
  file, or a ledger `finding` line. Derived `nw.history.record.v1` rows are the cache spec's
  count.
- **Does this spec assert the 10 s / 300 ms / 500 ms / 200 ms / 50 ms budgets?** No. It
  builds the corpus those budgets run on.
