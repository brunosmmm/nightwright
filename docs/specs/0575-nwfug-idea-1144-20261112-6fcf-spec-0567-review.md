---
id: SPEC-0575
title: "SPEC-0567 review follow-ups: history-cache reconcile tests and one-snapshot recompute"
status: proposed
owner: user
created: 2026-11-12
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
| 8 | `8ec60ca2df06` minor: reconcile writes outside the cache lock in the `cold_filled` branch | The branch wraps the reconcile in `_cache_write_guard` with `_hold_guard=True` (`history_cache.py:3361-3366`, loop-2 change) | Prove, with a lock probe, that every write in that branch happens under the lock (AC8) |
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
3. **Fix what a test finds.** If a lock probe or call-count test in item 8 or 4 shows a write
   outside the lock or a double recompute, the implementer fixes `src/` in the same change and
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
| `test_cold_filled_branch_reconciles_under_lock` | 8 | Empty cache dir; start a live-set change after the cold fill by patching `HC._ensure_writable_cache` with a wrapper that calls the real one, then adds a campaign directory and returns its result. Run `H.history_index(c, now=NOW, cache=True)`. Spies on `_reconcile_live_set`, `_write_campaign_file`, `_append_pending_index_write`, and `_persist_index_records`, called after the wrapper returns, record `_lock_held(v1)`; every recording is `True`, at least one of each of the first and last was recorded, and the result equals `cache=False`. Mutation: remove the `with _cache_write_guard(root)` around the reconcile, the first recording is `False`. |
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
  writes what; the lock probe shows the present placement is correct.

## Acceptance criteria

- [ ] **AC1.** `test_cold_fill_skipped_gone_id_forces_reconcile` passes, and fails when the
  `set(cids) != live_now` clause is removed.
- [ ] **AC2.** `test_cold_fill_skips_gone_id` asserts the pending-write list from the spy
  (not the absence of `index.writes.jsonl`) and fails when the `is_dir()` skip is removed.
- [ ] **AC3.** `test_unchanged_live_set_skips_reconcile` asserts that no writer, no recompute,
  and no `_persist_index_records` call happens on an equal live set, and no `index.writes.jsonl`
  exists afterwards.
- [ ] **AC4.** A first `campaign_history(cache=True)` and a first `history_records(cache=True)`
  after an add and after a remove, with `_sync_globals` neutralised, equal the `cache=False`
  results, and the reconcile ran once with `persist=True` while the cache lock was held.
- [ ] **AC5.** In `test_each_id_recomputed_once_per_live_set_change` every recomputed id is
  recomputed exactly once, and the test fails when `affected -= reconciled` is removed.
- [ ] **AC6.** `test_no_drop_campaign_all_in_src` passes and finds no occurrence in `src/`.
- [ ] **AC7.** The reconcile persist contract is tested for: `persist=False` with a step-5
  recompute (no `_persist_index_records` call, three files unchanged, campaign file rewritten);
  `persist=True` with a step-5 recompute (one call, doc hit on the next read); and
  `persist=True` with `sources=None` (the on-disk `sources.json` is read and updated).
- [ ] **AC8.** In `test_cold_filled_branch_reconciles_under_lock` every campaign-file write,
  pending-write append, and `_persist_index_records` call made by the `cold_filled` branch
  happens while the cache lock is held, and the test fails when the guard is removed.
- [ ] **AC9.** `_recompute_terminal_documents` accepts `global_recs`; one
  `_reconcile_live_set` call parses the global sources once with `sorted(live)` taken at entry,
  and a directory added mid-call is picked up by the next call, not half-applied; a recomputed
  terminal's `gaps` equals the pure result's.
- [ ] **AC10.** No output change: the whole existing `tests/test_history_cache.py` still passes
  (all 80 tests of SPEC-0567 plus the ones above), and the equality tests for add, remove, and
  add+remove are unchanged.
- [ ] **AC11.** Each mutation check above was run once and its result recorded in *As shipped*.

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

## Rollout / migration

None. No data or layout change; the cache stays disposable.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; affected tests pass; mutation checks run and recorded.
- [ ] No regressions (`tools/spec_lint.py`, `tools/test_tiers.py --check` when present).
- [ ] Spec body updated to match what shipped (As shipped section added).
- [ ] `docs/LEDGER.md` regenerated.

## Open questions

- None.
