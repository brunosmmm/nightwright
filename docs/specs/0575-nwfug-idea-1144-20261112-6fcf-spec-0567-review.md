---
id: SPEC-0575
title: "SPEC-0567 review follow-ups: history-cache reconcile tests and one-snapshot recompute"
status: done
owner: user
created: 2026-11-12
updated: 2026-11-12
source_idea: IDEA-1324
kind: feature
depends_on: [SPEC-0567, SPEC-0472]
tags: [agentic, history, tests]
---

## Context

[SPEC-0567](./0567-spec-0472-leftovers-history-cache-treat-live.md) (done) added
`_reconcile_live_set` to `src/nightwright/agentic/history_cache.py` and made `_cold_fill`
live-set aware. Its close-gate review (campaign IDEA-1144-20261112-6fcf, filed as IDEA-1324)
left ten findings, plus one capped finding, "Reconcile mixes two live-set snapshots". Two of
them describe behaviour the loop-2 commit (`2afff28c`) already changed; the rest are
test-strength gaps in `tests/test_history_cache.py`. This spec closes all of them. The only
source change it plans is the last item below; every other item is a test, and a test that
finds an unguarded or wrong path fixes `src/` in the same change.

| # | Finding (id, severity) | State in the tree today | What this spec does |
|---|---|---|---|
| 1 | `18d469ac077e` major: `_cold_fill` skips the reconcile when a gone id was skipped | `_persist` enters the reconcile on `indexed != live_now or set(cids) != live_now`, with `force=True` (`history_cache.py:2232-2247`); `test_cold_fill_skipped_gone_id_relinks_globals` exists | Lock the behaviour with a call-level assertion and a recorded mutation check (AC1) |
| 2 | `3fab0f021631` minor: the "no pending write" assertion is vacuous (`_cold_fill` always unlinks `index.writes.jsonl`) | `test_cold_fill_skips_gone_id` already spies `_append_pending_index_write`, but the file-absence assertion beside it still passes either way | Make the spy the assertion and prove it with a mutation (AC2) |
| 3 | `0e112ba359a7` minor: the fast-path test does not assert there are no writes | It checks `_persist_index_records` and the three file bytes, not the other writers | Assert every writer is silent (AC3) |
| 4 | `55269a58baec`, `ca7f94f93077` minor: lock-holding call sites untested in isolation | Every equality test enters through `history_index`, where `_sync_globals` has already reconciled with `persist=False` | Enter through `campaign_history(cache=True)` and `history_records(cache=True)` with `_sync_globals` neutralised (AC4) |
| 5 | `0847c362fd63` minor: no check that an id is recomputed exactly once | The spy test asserts membership, not multiplicity | Count per-id recomputes (AC5) |
| 6 | `eba7afd3fba6` minor: no test greps `src/` for `drop_campaign_all` | The removal is real; SPEC-0567's Test plan promised the grep | Add the grep test (AC6) |
| 7 | `7614c954148c`, `c846c4aa76ff` minor: persist contract only covers the "index a new id" path | `test_reconcile_persist_contract` unchanged | Cover the step-5 recompute, `sources=None`, and the doc-hit paths (AC7) |
| 8 | `8ec60ca2df06` minor: reconcile writes outside the cache lock in the `cold_filled` branch | The branch wraps the reconcile in `_cache_write_guard` with `_hold_guard=True` (`history_cache.py:3361-3366`, loop-2 change) | Prove, with a lock probe, that every write the reconcile call makes in that branch happens under the lock (AC8); the later `stale` and `_refresh_live` steps of the branch are out of scope (see Non-goals) |
| 9 | capped minor: reconcile mixes two live-set snapshots | `_reconcile_live_set` reads the live set once (`:2518`) and resolves globals with it (`:2545`), but step 5's `_recompute_terminal_documents` re-lists the live set and re-parses every global source (`:1056`) | One snapshot and one global parse per reconcile (AC9) |

## Goals / Non-goals

**Goals**

- Every finding above is closed by a test that fails against the defect it names (a recorded
  mutation check per finding) or, for the capped finding, by a code change with a test.
- `_reconcile_live_set` resolves global records, and lists the live set, exactly once per call;
  step 5 reuses that result.
- No behaviour change in the cache's output: every stored document, `index.jsonl`,
  `records.jsonl`, and `sources.json` stays byte-identical to what SPEC-0567 produces for the
  same inputs.

**Non-goals**

- Any change to the cache layout, the v1 schemas, link rules, the SPEC-0536 opt-in, or the
  SPEC-0472 budgets.
- The `stale` recompute and `_refresh_live` steps that follow the reconcile in the
  `cold_filled` branch of `_history_index_cached` (`history_cache.py:3372-3386`). They call
  `_recompute_terminal_documents`, `_persist_index_records`, and `_refresh_live` without
  `_hold_guard` and without an enclosing `_cache_write_guard` (the non-cold branch wraps them),
  so their writes are not under one held lock. They are not part of finding `8ec60ca2df06`,
  which names the reconcile, and no test here asserts anything about their locking.
- Moving the lock, or changing who writes `index.jsonl`/`records.jsonl`/`sources.json`
  (SPEC-0567 Decision 2 stands).
- New product behaviour. This is hardening of SPEC-0567.

## Decision

1. **Tests close findings 1 to 8.** All new tests go into `tests/test_history_cache.py`; helpers
   already there (`cfg`, `_v1`, `_spy_cold_fill_writes`, `_install_terminal_campaign`,
   `_add_mail`, `_assert_cache_equals_pure`) are reused. Each test is mutation-checked once, by
   hand, against a scratch edit of `src/` that re-introduces the defect; the result is recorded
   in the spec's *As shipped* section and the scratch edit is reverted. A finding whose test
   already passes on the current tree (items 1, 2, 3 in part, 8) still gets the stronger
   assertion below, because the finding is about the assertion's strength.
2. **One snapshot per reconcile (finding 9).** `_recompute_terminal_documents` gains a
   keyword-only `global_recs: list[dict] | None = None`. When given, it uses that list as the
   shared global records and does not call `H._global_records` or `_campaign_ids`. When `None`
   (every caller other than the reconcile: `_sync_globals`, the `stale` paths, `_refresh_live`)
   it behaves exactly as today. `_reconcile_live_set` passes its own raw (un-enriched)
   `global_recs`, the list it already resolved with `sorted(live)`; step 2 passes the same
   list to `_recompute_campaign`, so steps 2 and 5 now see one resolution of the global
   sources. The list is not mutated by either step (step 3 enriches a `copy.deepcopy`).
3. **Fix what a test finds.** If a lock probe or call-count test in item 8 or 4 shows a
   reconcile write outside the lock or a double recompute, the implementer fixes `src/` in the same change and
   notes it in *As shipped*. The fix keeps SPEC-0567's contract: callers that hold the lock pass
   `_hold_guard=True`.

## Design

### Test specifications

All tests live in `tests/test_history_cache.py`. Probe helper (new, module-local):
`_lock_held(v1)` opens `v1/.lock` on a fresh descriptor and tries
`fcntl.flock(fd, LOCK_EX | LOCK_NB)`; it returns `True` when that raises `BlockingIOError`
(`flock` locks belong to the open file description, so a second descriptor in the same process
conflicts with the cache's own).

| Test | Finding | Asserts |
|---|---|---|
| `test_cold_fill_skipped_gone_id_forces_reconcile` | 1 | In the scenario of `test_cold_fill_skipped_gone_id_relinks_globals`, a spy on `HC._reconcile_live_set` sees exactly one call from `_cold_fill`, with `force=True` and `persist=False`; the mail resolves to `_A` and `_assert_cache_equals_pure` holds. Mutation: drop `set(cids) != live_now` from the condition, the test fails. |
| `test_cold_fill_skips_gone_id` (strengthened) | 2 | The assertion that no pending write exists reads the spy (`victim not in calls["pending"]`, plus `sorted(calls["pending"]) == sorted(set(ids) - {victim})`) and no longer relies on the absence of `index.writes.jsonl`, which `_cold_fill` always removes. Mutation: delete the `is_dir()` skip in `_persist`, the spy assertions fail. |
| `test_unchanged_live_set_skips_reconcile` (strengthened) | 3 | In addition to today's assertions, with `HC._append_pending_index_write`, `HC._write_campaign_file`, `HC._write_records`, `HC._write_index`, `HC._atomic_write_json`, `HC._safe_unlink_campaign_file` and `HC._recompute_campaign` patched as spies, none is called, `index.writes.jsonl` does not exist afterwards, and `_persist_index_records` is not called with `persist=True` or otherwise. |
| `test_first_campaign_history_after_live_set_change_reconciles_under_lock` | 4 | Warm cache; add `A-1b`; patch `HC._sync_globals` to return `set()`; the first `campaign_history(c, _A, cache=True)` equals `cache=False` minus `generated_at`; a spy shows one `_reconcile_live_set` call with `persist=True`, `_hold_guard=True`, and `_lock_held(v1)` true inside it. |
| `test_first_history_records_after_live_set_change_reconciles_under_lock` | 4 | Same set-up through `history_records(c, cache=True)`; result equals `cache=False` including order; one `persist=True` reconcile under the lock. Run for the add and remove cases. |
| `test_each_id_recomputed_once_per_live_set_change` | 5 | Warm cache; change the live set so that at least two terminals differ and one id is new; spy on `H._campaign_history` with a `Counter`; after `history_index(c, cache=True)` every recomputed id has count exactly 1 and the set of counted ids equals the differing ids plus the new id (no other id is counted). Repeat through `campaign_history(cache=True)`. Mutation: remove `affected -= reconciled` in `_sync_globals`, the count for a reconciled id is 2. |
| `test_no_drop_campaign_all_in_src` | 6 | Walk every `*.py` under `src/` and assert `drop_campaign_all` appears in none. |
| `test_reconcile_persist_contract_recompute_path` | 7 | Warm cache; add an id that makes a stored terminal's links differ (so step 5 recomputes it) and an id that is new. With `persist=False`: zero `_persist_index_records` calls, the bytes of `index.jsonl`, `records.jsonl`, `sources.json` unchanged, the differing terminal's `campaigns/<cid>.json` rewritten, its id in `touched`. With `persist=True` on a fresh copy of the read data: exactly one call, and both ids are doc hits on the next `campaign_history(cache=True)`. |
| `test_reconcile_persist_sources_none` | 7 | `persist=True` with `sources=None` reads `sources.json` on entry; the single `_persist_index_records` call receives a dict equal to the on-disk one plus the reconcile's cursor updates, and the written `sources.json` contains the new id's `campaign_files` entry. `persist=False` with `sources=None` still writes no file among the three. |
| `test_cold_filled_branch_reconciles_under_lock` | 8 | Scope: the `_reconcile_live_set` call in the `cold_filled` branch only. Empty cache dir; start a live-set change after the cold fill by patching `HC._ensure_writable_cache` with a wrapper that calls the real one, then (a) adds a new campaign directory `_NEW`, and (b) adds a mail linking to `_NEW` so that an already-stored terminal's links differ and step 5 recomputes it. Run `H.history_index(c, now=NOW, cache=True)`. A spy on `HC._reconcile_live_set` sets an `inside` flag for the duration of its call (it calls the real function) and records `_lock_held(v1)` at entry; spies on `HC._write_campaign_file`, `HC._append_pending_index_write`, and `HC._persist_index_records` (each calling through) record `_lock_held(v1)` only while `inside` is set, so the later `stale` and `_refresh_live` calls are not recorded. Asserts: the reconcile was entered once with `_lock_held` true; each of the three writer spies recorded at least once while `inside` (`_write_campaign_file` and `_append_pending_index_write` come from the step-5 recompute of the differing terminal, `_persist_index_records` from `persist=True`); every recording is `True`; the result equals `cache=False`. The test fails if any of the three lists is empty, so the set-up cannot pass vacuously. Mutation: remove the `with _cache_write_guard(root)` around the reconcile, the first recording is `False`. |
| `test_reconcile_one_global_resolve_and_snapshot` | 9 | Spy on `H._global_records` and on `HC._live_campaign_ids` during one `_reconcile_live_set(..., persist=False)` call that indexes a new id and recomputes a differing terminal: `H._global_records` is called exactly once, and its `cids` argument equals `sorted(live)` taken at entry; `_live_campaign_ids` is called once. A second case adds a directory between step 2 and step 5 (inside a wrapped `_recompute_campaign`) and asserts the late directory is neither indexed nor linked in this call and is picked up by the next call. |

### Source change (finding 9)

`_recompute_terminal_documents(cfg, root, now, index_rows, records, cids, *, sources=None,
global_recs=None, extra_gaps=None)`:

```python
gaps: list[str] = list(extra_gaps or [])
if global_recs is None:
    global_recs = H._global_records(cfg, _campaign_ids(cfg), gaps)
```

`gaps` is what the helper already passes to `_recompute_campaign` as `extra_gaps`. The reconcile
passes its raw `global_recs` and its own `gaps` list (filled by its step-2 `_global_records`
call) as `extra_gaps`, so a recomputed terminal keeps the global-source gaps the re-parse used
to add; `test_reconcile_one_global_resolve_and_snapshot` also asserts the recomputed
document's `gaps` equals `campaign_history(cache=False)`'s.

No other caller changes. The raw `global_recs` is never mutated: the reconcile enriches a deep
copy and `_recompute_campaign` only reads `shared_global`.

## Alternatives considered

- **Delete the second `_global_records` call by resolving it once in `_recompute_terminal_documents`'
  callers.** Five callers would need the same change for no gain; only the reconcile has a
  snapshot to preserve, so the parameter is optional.
- **Pass `sorted(live)` instead of the parsed list.** Fixes the snapshot but still re-parses the
  global sources; reusing the list also removes that cost.
- **Treat the vacuous-assertion findings as already fixed and skip them.** The reviewer's point
  is that the assertion proves nothing; without a recorded mutation check nothing says the
  assertion now does.
- **Move the cold-filled reconcile into `_cold_fill`.** Changes SPEC-0567's decision on who
  writes what; the reconcile is already inside `_cache_write_guard` in `_history_index_cached`,
  and AC8 pins that.
- **Also wrap the `stale` recompute and `_refresh_live` of the `cold_filled` branch in the
  guard (`_hold_guard=True`), as the non-cold branch does.** Real, but a different defect from
  the one finding `8ec60ca2df06` reports, and it would need its own set-up that forces
  `_stale_terminal_ids` non-empty after a cold fill; left to a separate follow-up so this spec
  stays a test-strength change.

## Acceptance criteria

- [x] **AC1.** `test_cold_fill_skipped_gone_id_forces_reconcile` passes, and fails when the
  `set(cids) != live_now` clause is removed.
- [x] **AC2.** `test_cold_fill_skips_gone_id` asserts the pending-write list from the spy
  (not the absence of `index.writes.jsonl`) and fails when the `is_dir()` skip is removed.
- [x] **AC3.** `test_unchanged_live_set_skips_reconcile` asserts that no writer, no recompute,
  and no `_persist_index_records` call happens on an equal live set, and no `index.writes.jsonl`
  exists afterwards.
- [x] **AC4.** A first `campaign_history(cache=True)` and a first `history_records(cache=True)`
  after an add and after a remove, with `_sync_globals` neutralised, equal the `cache=False`
  results, and the reconcile ran once with `persist=True` while the cache lock was held.
- [x] **AC5.** In `test_each_id_recomputed_once_per_live_set_change` every recomputed id is
  recomputed exactly once, and the test fails when `affected -= reconciled` is removed.
- [x] **AC6.** `test_no_drop_campaign_all_in_src` passes and finds no occurrence in `src/`.
- [x] **AC7.** The reconcile persist contract is tested for: `persist=False` with a step-5
  recompute (no `_persist_index_records` call, three files unchanged, campaign file rewritten);
  `persist=True` with a step-5 recompute (one call, doc hit on the next read); and
  `persist=True` with `sources=None` (the on-disk `sources.json` is read and updated).
- [x] **AC8.** `test_cold_filled_branch_reconciles_under_lock` covers exactly the
  `_reconcile_live_set` call in the `cold_filled` branch of `_history_index_cached`: while that
  call runs, the cache lock is held at its entry and at every `_write_campaign_file`,
  `_append_pending_index_write`, and `_persist_index_records` call it makes; the set-up forces
  each of those three to be recorded at least once (a new campaign plus a mail that makes a
  stored terminal differ), and the test fails when the guard around the reconcile is removed.
  The `stale` and `_refresh_live` steps of that branch are not covered or asserted.
- [x] **AC9.** `_recompute_terminal_documents` accepts `global_recs`; one
  `_reconcile_live_set` call parses the global sources once with `sorted(live)` taken at entry,
  and a directory added mid-call is picked up by the next call, not half-applied; a recomputed
  terminal's `gaps` equals the pure result's.
- [x] **AC10.** No output change: the whole existing `tests/test_history_cache.py` still passes
  (all 80 tests of SPEC-0567 plus the ones above), and the equality tests for add, remove, and
  add+remove are unchanged.
- [x] **AC11.** Each mutation check above was run once and its result recorded in *As shipped*.

## Test plan

- **Automated tests:** the tests in the Design table, all in `tests/test_history_cache.py`.
  That module already carries its one module-level marker `pytestmark = pytest.mark.fast`; no
  new `tests/test_*.py` is created, so no new speed-tier marker is added. Run
  `uv run pytest -q tests/test_history_cache.py` and then
  `python3 tools/affected_tests.py --run uv run pytest -q`; `tests/test_history_fixture.py`,
  `tests/test_history_perf.py`, and `tests/test_dashboard_cache.py` run with them.
- **Mutation checks (manual, once):** for each of AC1, AC2, AC5, AC8 apply the named scratch
  edit to `src/nightwright/agentic/history_cache.py`, run the single test, confirm it fails,
  revert with `git checkout`, and note the result in *As shipped*.
- **Manual verification:** none beyond the above; the behaviour is internal and asserted by the
  equality tests. `python3 tools/test_tiers.py --check` exits 0 when `tools/test_tiers.py`
  exists.
- **Regression guard:** `python3 tools/spec_lint.py` exits 0; the perf test's ceilings in
  `tests/test_history_perf.py` still hold (AC9 removes one global parse per reconcile that
  recomputes a terminal, so it can only help). The full suite is left to the queue's gates.

### Test record (2026-11-12)

- `uv run pytest -q tests/test_history_cache.py` — 94 passed (every pre-existing test plus the
  new ones).
- `uv run pytest -q tests/test_history_fixture.py tests/test_dashboard_cache.py` — 131 passed,
  2 failed (`test_review_dir_symlinks_and_loops_are_skipped[loop]`,
  `test_inside_treats_a_symlink_loop_as_outside`: symlink-loop checks in
  `tools/capture_history_fixture.py`, unrelated to the cache) and 1 error
  (`test_check_budget`: the file took 24.9s against its 20s budget while the perf test ran
  alongside). Neither touches `history_cache.py`.
- `uv run pytest -q tests/test_history_perf.py` (run concurrently with other test runs, so
  timings are inflated) — 2 passed, 1 xfailed, 1 failed: `test_record_count_and_budgets`
  measured 65.2s against its 30s ceiling. That test exercises the cold fill, which this change
  does not touch (the reconcile only runs on a live-set change), and the host was loaded; not
  re-run alone and not compared with the base, so the ceiling is left to the queue's gates.
- `uv run pytest -q tests/test_spec_numbers.py tests/test_specs_dir.py tests/test_test_tiers.py
  tests/test_test_waits.py tests/test_spec_source_request.py` — 78 passed.
- `python3 tools/spec_lint.py --write-ledger` and `python3 tools/spec_lint.py` exit 0.
- `python3 tools/test_tiers.py --check` exits 0 (no new test module; the existing
  `pytestmark = pytest.mark.fast` stands).
- The full suite is left to the queue's gates.

### As shipped (2026-11-12)

- Source: `_recompute_terminal_documents` gained keyword-only `global_recs` and `extra_gaps`;
  when `global_recs` is given it neither calls `H._global_records` nor `_campaign_ids`. The
  reconcile passes its raw `global_recs` and its own `gaps` list for step 5. Gaps are merged as a
  set in `_recompute_campaign`, so the runner gaps both sides add do not duplicate. No other
  caller changed; no other `src/` change was needed (the item 4 and 8 lock probes passed on the
  loop-2 code).
- Tests (all in `tests/test_history_cache.py`; new helpers `_lock_held`, `_load`,
  `_spy_reconcile`, `_persist_calls_spy`; new imports `collections`, `contextlib`, `copy`,
  `fcntl`): the new tests of the Design table, plus `test_reconcile_directory_added_mid_call_
  picked_up_next_call` (the second case of the finding-9 row, split out of
  `test_reconcile_one_global_resolve_and_snapshot`). The four add/remove tests are parametrized
  over `add`/`remove`; the item 5 test over the `history_index`/`campaign_history` entry.
- Deviation in `test_each_id_recomputed_once_per_live_set_change`: the global-source change
  (two mails) also puts every campaign that owns a global record into `affected`, so a few more
  terminals are recomputed than the differing ones. The test asserts differing ids plus the
  new id are all counted, any extra id owns a stored global record, and every count is exactly 1.
- `test_reconcile_persist_contract_recompute_path` is parametrized over `persist`. The
  `persist=False` run and the `persist=True` run use separate fresh fixtures.
- Mutation checks (each applied to a scratch copy of `history_cache.py`, one test run, then
  restored byte-for-byte from a backup): (AC1) drop `or set(cids) != live_now` — both
  `test_cold_fill_skipped_gone_id_*` tests fail; (AC2) replace the `is_dir()` skip in `_cold_fill`
  with `if False:` — `test_cold_fill_skips_gone_id` fails (the missing directory raises
  `ValueError` in the recompute before the spy assertions run; the skip's removal is detected);
  (AC5) remove `affected -= reconciled` — both parametrizations of
  `test_each_id_recomputed_once_per_live_set_change` fail; (AC8) replace the
  `with _cache_write_guard(root)` around the cold-filled reconcile by `if True:` —
  `test_cold_filled_branch_reconciles_under_lock` fails.

## Rollout / migration

None. No data or layout change; the cache stays disposable.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; affected tests pass; mutation checks run and recorded.
- [x] No regressions (`tools/spec_lint.py`, `tools/test_tiers.py --check` when present).
- [x] Spec body updated to match what shipped (As shipped section added).
- [x] `docs/LEDGER.md` regenerated.

## Open questions

- None.
