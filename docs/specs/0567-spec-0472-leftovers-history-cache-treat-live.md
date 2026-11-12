---
id: SPEC-0567
title: "History cache: a live-campaign-id set change invalidates stored terminals"
status: done
owner: user
created: 2026-11-12
updated: 2026-11-12
source_idea: IDEA-1144
kind: feature
depends_on: [SPEC-0472, SPEC-0536]
tags: [agentic, observability, history]
---

## Context

[SPEC-0472](./0472-history-cache-for-the-campaign-history-layer.md) shipped the history cache
(`src/nightwright/agentic/history_cache.py`). [SPEC-0536](./0536-history-cache-opt-in-replaces-spec-0472-decision-1.md)
made `cache=None` opt-in. The `loop_exhausted` review of campaign IDEA-1015-20261108-faf2 left
leftovers; this spec closes the production half. The test-only half is a separate spec (see
Non-goals).

**A change of the live-campaign-id set is not an input to every stored terminal.** The pure
reader resolves a global record's campaign from the record's own `campaign` field, or, when
that is absent, from a text link: `_campaign_text_link` (`history.py:534`) accepts the text only
when exactly one live id (a child directory of `store.campaigns_dir`) appears in the record's
title and text. The live set is the `campaigns` argument of `_global_records`, and that argument
is the only place the live set enters a campaign document. Today the cache reacts to a changed
set in disconnected places, each with a gap:

- `_prune_gone_campaigns` (called from `_sync_globals`, `_campaign_history_cached`,
  `_history_index_cached`, and `history_records`) runs only when an index id is no longer live,
  never when a directory was only *added*. It compares each survivor to
  `campaign_history(cache=False)` one at a time. `_apply_pending_index_writes` has an inline copy
  of the same prune.
- `_refresh_live` handles a directory that is live but absent from the index (`missing`). It
  re-resolves the global records but recomputes no stored terminal whose linked records changed.
  Example: a mail names `A-1` and links to it; once `A-1b` is live the text is ambiguous and the
  pure reader links it to nobody, but the stored `A-1` document and `records.jsonl` keep the link.
- `_merge_records(..., drop_campaign_all=cid, add=_records_for_campaign(doc, cid))` is called at
  six sites. It drops every record whose `campaign` is `cid`, global ones included, and re-adds
  only what that one document holds. A global record that *moved* from A to B on a live-set
  change is lost from `records.jsonl` when A is recomputed and B is not in the same pass.
- `_cold_fill` takes `cids = _campaign_ids(cfg)` once and recomputes every id in that snapshot.
  A directory removed mid-fill still gets `campaigns/<cid>.json`, a pending index write, and
  local records; the final `known - live` prune removes the file and row but not the records.
  A directory added mid-fill is missed, and `sources.json` is scanned with the stale `cids`.

## Goals / Non-goals

**Goals**

- One reconcile step, run whenever the live id set differs from the id set the cache indexed,
  that prunes gone campaigns, indexes new ones, re-resolves every cached global record against
  the new live set, and recomputes exactly the stored terminal campaigns whose stored document
  disagrees with the re-resolved records.
- No call site removes global records by `campaign` id. A recompute replaces a campaign's local
  records and refreshes its global records' header enrichment in place.
- `_cold_fill` skips an id whose directory is already gone and, when the live set changed during
  the fill, reconciles in memory before it commits.

**Non-goals**

- The test-only half of IDEA-1144 is **not** part of this spec; it becomes its own spec,
  "SPEC-0472 missing tests" (kind feature, test-only, `depends_on: SPEC-0472`): AC4 signature
  inputs (`utime` on `state.json`, `runs/`, `gates/`; a same-size ledger with a different mtime),
  mailbox delete and reply, flaky replace, `resolve_profile` reaching `loop_max`, a harmless
  `runner.jsonl` append that opens no ledger, a non-JSON `index.jsonl` line, `OSError` on a cache
  write and `rebuild_cache` raising, cached CLI counts and data-dir hash, `clone_fixture` id /
  escalation-filename / timestamp rules, the cache-tree file whitelist, and the real XDG cache
  sentinel. None of it is changed or relied on here.
- Any change to the three v1 schemas, `campaign_signature`'s five-tuple, link rules, the cache
  layout, the SPEC-0536 opt-in, or the SPEC-0472 budgets. No new stored key: the index id set
  stays the comparison point.
- Recomputing every terminal on a live-set change. Only the ones whose stored document differs.

## Decision

1. **One reconcile step, `_reconcile_live_set`.** Signature:
   `_reconcile_live_set(cfg, root, now, index_rows, records, sources, *, persist, _hold_guard=False)`
   returning `(index_rows, records, sources, touched)`, where `touched` is the set of campaign
   ids it pruned, computed, or recomputed. It first compares `live = _live_campaign_ids(cfg)`
   with the id set of `index_rows`; when equal it returns the inputs unchanged and `touched`
   empty (the fast path, no global resolve). Otherwise it runs, in this order, each input
   existing before it is used:
   1. **Prune gone ids.** For each id in `index_ids - live`: drop its index row, drop its
      *local* records (`source` in `_LOCAL_SOURCES`), `_safe_unlink_campaign_file`, and pop its
      entries from `sources["campaign_files"]`, `sources["campaign_rg_dirs"]`, and the
      per-campaign cursors that `_set_campaign_cursors` writes. Global records that name the id
      by field stay until step 3 re-resolves them.
   2. **Index new ids.** `gaps = []`;
      `global_recs = H._global_records(cfg, sorted(live), gaps)`; `runner_down`, windows, and
      shares as `_refresh_live` builds them today. For each id in `live - surviving index ids`
      (sorted): `_recompute_campaign(..., global_recs=global_recs, write_campaign=True)`; append
      its row; add `_local_records(doc)`; `_write_campaign_file`; `_set_campaign_cursors`
      (which gives the id its `campaign_files` and `campaign_rg_dirs` entries, so its next
      `campaign_history` read is a doc hit). A campaign computed here is **not** recomputed in
      step 5. This step passes the raw (un-enriched) `global_recs`, exactly as `_refresh_live`
      does, so it needs no header map.
   3. **Enrich once and replace the globals.** `new_globals =
      H._enrich_records(global_recs, _headers_for_enrich(index_rows))` where `index_rows` is
      now complete (survivors plus step 2 rows). Replace every non-local record in `records`
      with `new_globals`.
   4. **Diff against the stored documents.** For every id that is terminal in `index_rows`,
      live, and not computed in step 2: read `campaigns/<cid>.json` (a missing or non-dict file
      counts as differing). It differs when the sorted tuples
      `(id, campaign, linked_by, links, spec_id, project)` of its non-local records are not
      equal to those of the records in `new_globals` whose `campaign` is that id. The diff is
      taken against the *stored document*, not against the previous `records.jsonl`, because
      `_sync_globals` may already have replaced `records.jsonl` globals with the new live set
      before the reconcile runs.
   5. **Recompute the differing terminals.** `_recompute_terminal_documents` for that set; it
      writes the campaign files and index rows and sets their cursors, and the reconcile merges
      its result. Non-terminal campaigns are left to `_refresh_live`, which recomputes them on
      every call.
2. **Persistence contract.** `persist=False`: the step mutates only the data it was given and
   the files named in step 1, 2, and 5 (campaign files), never calls `_persist_index_records`,
   never writes `index.jsonl`, `records.jsonl`, or `sources.json`, and returns the data. The
   caller commits. `persist=True`: it snapshots `records_at_start = list(records)` and
   `index_at_start = list(index_rows)` on entry (callers pass what they read under the lock, so
   those equal the disk), and ends with exactly one `_persist_index_records(...)` carrying
   `sources`; `sources=None` is read from `sources.json` on entry. Who writes index, records, and
   `sources.json`:
   - Lock-holding callers (`_campaign_history_cached`, `_history_index_cached`,
     `history_records`): `persist=True`; the reconcile writes all three through the one
     `_persist_index_records` call. They then continue with `_stale_terminal_ids` and
     `_refresh_live` as today.
   - `_sync_globals` (already inside the lock): `persist=False`; its own later
     `_persist_index_records` commits. It ORs `bool(touched)` into `need_persist` and removes
     `touched` from `affected` so no id is recomputed twice.
   - `_apply_pending_index_writes`' pruned branch: its inline global re-resolve and survivor
     compare are replaced by a `persist=False` call on `merged_idx` and `records`; its own
     commit stays.
   - `_cold_fill`: see 4.
3. **A recompute replaces a campaign's local records only.** `drop_campaign_all` is deleted from
   `_merge_records`. A new helper `_merge_recomputed(records, doc, cid)` is used at every
   recompute site: `_merge_records(records, drop_campaign=cid, add=_local_records(doc))`, then
   `_enrich_records` in place on the non-local records of `records` whose `campaign` is `cid`,
   using `{cid: doc["header"]}`. Global records are therefore never dropped by a recompute; only
   their `spec_id`/`project` follow the owner's fresh header, which is what
   `_records_for_campaign` achieved before.
4. **`_cold_fill` is live-set aware.** Inside `_persist`, before each `_recompute_campaign` it
   checks that the campaign directory still exists and skips the id when it does not, still
   calling `progress(done, total)` so `done` reaches `total`. After the loop and before any
   commit it lists the live set again. If it differs from `set(cids)`, it runs
   `_reconcile_live_set(..., persist=False)` on `index_rows`, `all_records`, and the in-memory
   `sources`, appends `_append_pending_index_write` for each row the reconcile added, sets
   `cids = sorted(live)` **before** the `_scan_all_campaign_files` and
   `_scan_all_campaign_rg_dirs` calls, and then commits as today (the existing `known - live`
   prune stays). `_cold_fill` is the sole writer of the cache files in that call.
5. **Fast path untouched.** `_cache_refresh_quiet`, `_doc_hit` with `index_ids == live`, and the
   `index_ids - live` empty-check run before the lock exactly as today; the reconcile is entered
   only when `index_ids != live`.

## Design

### Invariant

*`records.jsonl` globals (records whose `source` is not in `_LOCAL_SOURCES`) are a function of
(the live id set, the global sources) only. Their membership and `campaign`/`links`/`linked_by`
are written solely by `_reconcile_live_set` and `_sync_globals` (plus `_cold_fill`'s initial
build); their `spec_id`/`project` enrichment is refreshed in place by `_merge_recomputed` from
the owner's header.* `_refresh_live` resolves `global_recs` fresh and passes them to
`_recompute_campaign`, but no longer writes them into `records.jsonl`; its non-terminal pass
therefore leaves `records.jsonl` equal to the pure result (tested), and its `missing` branch and
re-resolve block are deleted because the reconcile already ran and indexed every live id.
`_live_refresh_targets` still returns `(live, missing)`; `missing` is empty after a reconcile.

### Live-set-dependent fields of a terminal document

The live set enters a document through `_campaign_text_link` in `_global_records` and nowhere
else. The document fields that read it, and the diff term that covers each:

| Field | Depends on the live set via | Covered by |
|---|---|---|
| a global record's presence in `records` | `campaign` set by a text link, or lost to ambiguity | `id` and `campaign` terms (a record that appears or disappears from the owner's set differs) |
| `links.campaign` | set with `campaign` by `_campaign_text_link` and by `_enrich_records` | `links` term |
| `linked_by` (`field` / `text`) | `_campaign_text_link` result, `_enrich_records` rank | `linked_by` term |
| `spec_id`, `project` on a global record | `_enrich_records` from the owner's header, so they change when the owner changes | `spec_id`, `project` terms |
| count aggregates and any per-kind totals in the document | sums over `records` | follow from the record terms |
| document `header`, `gaps`, local records, signature | local files, runner log, global-source parse results (not the `campaigns` argument) | not live-set dependent |

The strong equality test (every stored terminal equals `campaign_history(cache=False)` except
`generated_at`, and `history_records(cache=True)` equals `cache=False` including order) is run
for add, remove, and add+remove, so a field missing from this table fails a test rather than
shipping stale.

### Call sites changed (`grep -n drop_campaign_all src/`: six call sites and the definition)

| Site | Change |
|---|---|
| `_merge_records` definition (line ~1760) | Remove the `drop_campaign_all` parameter. |
| `_recompute_terminal_documents` (~1069) | `_merge_recomputed(records, doc, cid)`. |
| `_prune_gone_campaigns` (~2494) | Replaced by `_reconcile_live_set` step 1; function removed, its five callers (`_sync_globals` ~3086, `_campaign_history_cached` ~3255, `_history_index_cached` ~3328, ~3377, `history_records` ~3494) call the reconcile as in Decision 2. |
| `_sync_globals` recompute loop (~3116) | `_merge_recomputed`; `touched` handling as in Decision 2. |
| `_refresh_live` (~3175 and ~3187) | Non-terminal loop uses `_merge_recomputed`; `missing` loop and global re-resolve block deleted. |
| `_campaign_history_cached` (~3288, `records_start`) | `_merge_recomputed`. |
| `_apply_pending_index_writes` (~1931) | Uses `drop_predicate` already; its re-resolve and survivor compare become a `persist=False` reconcile call. |
| `_cold_fill` `_persist` | Skip gone ids; final live-set check and reconcile; `cids` rebuilt before the `sources.json` scans. |

## Alternatives considered

- **Keep `drop_campaign_all` and patch each caller to re-add moved globals.** Six call sites
  would each need the re-add and a seventh would bring the bug back; removing the parameter
  makes the mistake unwritable.
- **Diff against the previous `records.jsonl` instead of the stored documents.** Cheaper, but
  `_sync_globals` can already have replaced the globals with the new live set, so the diff is
  empty while the stored terminals are stale. Reading the stored document is the only baseline
  that matches what a reader returns.
- **Store the live id set as a `sources.json` cursor.** The index id set already is the stored
  copy; a second copy can drift from it.
- **Recompute every terminal on any live-set change.** Correct, but it spends one pure
  derivation per campaign for one new directory and breaks SPEC-0472's "computed once" contract.
- **Compare every survivor to `campaign_history(cache=False)`** (today's
  `_prune_gone_campaigns`). Same cost problem; the stored-record diff finds the same set with
  file reads only.

## Acceptance criteria

- [x] **AC1. Added directory re-resolves text links.** With a warm cache, adding a live
  campaign directory that makes a mail's text link ambiguous leaves every stored terminal
  document and `history_records(cache=True)` equal to the `cache=False` results (minus
  `generated_at`), for the add case of the parametrised equality test.
- [x] **AC2. Removed directory re-resolves text links and cleans up.** Removing a live directory
  that made a link ambiguous gives the same equality (remove and add+remove cases); the gone id
  has no index row, `campaigns/<cid>.json`, local record, or `sources.json`
  `campaign_files`/`campaign_rg_dirs`/cursor entry, and a global record naming it by field is
  still returned.
- [x] **AC3. Moved globals survive; the invariant holds.** `drop_campaign_all` appears nowhere
  in `src/`; a global record that moves between campaigns on a live-set change is in
  `records.jsonl` exactly once, under its new owner; a `_refresh_live` pass for a non-terminal
  campaign leaves `records.jsonl` equal to the pure result.
- [x] **AC4. Only differing terminals recompute.** After a live-set change, a terminal whose
  stored global-record tuples already match the re-resolved ones is not recomputed (its campaign
  file is not rewritten), and every terminal whose document differs from pure is.
- [x] **AC5. Persistence contract.** With `persist=False` the reconcile writes no `index.jsonl`,
  `records.jsonl`, or `sources.json` and does not call `_persist_index_records`; with
  `persist=True` it calls it exactly once. After a reconcile that indexes or recomputes an id,
  that id's next `campaign_history(cache=True)` read is a doc hit (no recompute).
- [x] **AC6. `_cold_fill` skips gone ids.** An id whose directory vanishes during a cold fill
  writes no campaign file, no pending write, and no record; `progress` ends at
  `done == total`.
- [x] **AC7. `_cold_fill` indexes ids added mid-fill.** An id added during the fill is indexed,
  its text links and the others' equal pure after the fill returns, `sources.json` holds its
  `campaign_files` entry, and its next `campaign_history` read is a doc hit.
- [x] **AC8. Unchanged live set keeps the fast path.** With an unchanged set the reconcile
  returns early without resolving global records, and the warm `history_index` and warm
  `campaign_history` still pass the SPEC-0472 AC2 "hit" assertions and the AC8 budgets.

## Test plan

- **Automated tests** (all in `tests/test_history_cache.py`, which already carries its one
  module-level marker `pytestmark = pytest.mark.fast`; no new `tests/test_*.py`, so no new
  speed-tier marker; `H` is `nightwright.agentic.history`, `cfg`, `data_dir`, `cache_dir`,
  `_install_live_campaign`, and `_hashes` are the module helpers). "Equals pure" compares
  `campaign_history(..., cache=True)` with `cache=False` minus `generated_at`, and
  `history_records` with `cache=True` and `cache=False` including order.

| Test | What it asserts |
|---|---|
| `test_live_set_change_stored_terminals_equal_pure` | Parametrised `add`, `remove`, `add+remove`: mail text names `A-1`; the live set changes (`A-1b` added, removed, or one of each); every stored terminal and `records.jsonl` equal pure (AC1, AC2). |
| `test_live_set_add_unlinks_text_link` | The mail linked to `A-1` is unlinked once `A-1b` exists; the stored `A-1` terminal and `records.jsonl` show no link. |
| `test_live_set_remove_relinks_text_link` | Removing `A-1b` links the mail to `A-1`; the `A-1` document equals pure. |
| `test_gone_id_leaves_no_trace` | Removed id has no row, file, local record, or `sources.json` entry; a by-field global record naming it is still returned (AC2). |
| `test_moved_global_record_not_dropped` | A global record moves A to B; `records.jsonl` holds it once, under B; a test greps `src/` and finds no `drop_campaign_all` (AC3). |
| `test_refresh_live_leaves_globals_equal_pure` | A `_refresh_live` pass for a non-terminal campaign leaves `records.jsonl` equal to pure (AC3). |
| `test_recompute_touches_only_differing_terminals` | After a live-set change a spy on `H._campaign_history` runs only for ids whose pure document differs, and a terminal that already matches keeps its file mtime (AC4). |
| `test_reconcile_persist_contract` | Spy on `_persist_index_records` and the three file writes: zero calls and no writes with `persist=False`, one call with `persist=True`; an id the reconcile indexed or recomputed is a doc hit on the next read (AC5). |
| `test_cold_fill_skips_gone_id` | Patch `_recompute_campaign` to delete the next campaign directory; no `campaigns/<cid>.json`, no pending line, no local record of it, `progress` reaches `done == total` (AC6). |
| `test_cold_fill_live_set_added_mid_fill` | A directory added mid-fill is indexed, equals pure, has a `sources.json` `campaign_files` entry, and its next read is a doc hit (AC7). |
| `test_unchanged_live_set_skips_reconcile` | With an unchanged set, a spy shows `_reconcile_live_set` returns early and `H._global_records` is not called by it; warm hit assertions hold (AC8). |

- **Test-first check:** each new test is run once against the unchanged code; the tests for AC1
  to AC7 fail there and pass after the change. The existing prune tests
  (`test_removed_campaign_pruned`, `test_removed_campaign_pending_write_prunes_records`,
  `test_pending_prune_relinks_survivor_mail`, `test_live_header_change_refreshes_cached_global_records`)
  pass unchanged.
- **Manual verification:** on a copy of the fixture with the cache on, run
  `nw campaign history --list --json`, `mkdir` a campaign directory whose id extends an id named
  in a mail, run the command again; the output equals the same command with
  `agentic.history.cache` unset. Remove the directory and repeat.
- **Regression guard:** `python3 tools/affected_tests.py --run uv run pytest -q` plus
  `tests/test_history_cache.py`, `tests/test_history_fixture.py`, `tests/test_history_perf.py`,
  and `tests/test_dashboard_cache.py`; `python3 tools/test_tiers.py --check` exits 0 when
  `tools/test_tiers.py` exists; `python3 tools/spec_lint.py` exits 0; the perf test's ×3
  ceilings still hold.

### Test record (2026-11-12)

- `uv run pytest -q tests/test_history_cache.py` — 80 passed (the planned tests plus
  `test_cold_fill_skipped_gone_id_relinks_globals`, `test_refresh_live_keeps_owned_globals`,
  `test_merge_recomputed_keeps_globals_and_follows_header`, `test_cold_fill_dir_removed_during_its_recompute`).
- `test_cold_fill_skips_gone_id` spies on `_recompute_campaign`, `_append_pending_index_write` and
  `_write_campaign_file`; it fails with the skip removed. The skipped-gone-id relink test fails with
  the `set(cids) != live_now` check removed.
- `python3 tools/test_tiers.py --check` exits 0; `python3 tools/spec_lint.py` exits 0.
- The manual CLI run was not done by hand; the same add/remove fixture steps (before/after adding and removing a
  campaign directory) are asserted by the parametrised equality test.
- Full suite left to gating.

### As shipped (2026-11-12)

- `_reconcile_live_set` gained a keyword-only `force: bool = False` that skips the equal-set fast
  path. `_cold_fill` passes `force=True` and enters the reconcile when `set(cids) != live_now` as
  well as when the indexed set differs: after a gone id is skipped the indexed set equals the live
  set while `global_recs` and the terminals' documents were resolved against the stale `cids`.
- The `cold_filled` branch of `_history_index_cached` runs its reconcile inside
  `with _cache_write_guard(root)` (`_hold_guard=True`).

## Rollout / migration

No data migration. The cache is disposable; the next call after upgrade reconciles whatever
live-set drift exists. `nw campaign history --rebuild-cache` remains the full reset.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; affected tests pass; manual step performed.
- [x] No regressions (`tools/spec_lint.py`, `tools/test_tiers.py --check` when present).
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` regenerated.

## Open questions

- None.
