---
id: SPEC-0567
title: "History cache leftovers: live-id set change invalidates stored terminals, plus the missing SPEC-0472 tests"
status: proposed
owner: user
created: 2026-11-12
source_idea: IDEA-1144
kind: feature
depends_on: [SPEC-0472, SPEC-0536]
tags: [agentic, observability, history, tests]
---

## Context

[SPEC-0472](./0472-history-cache-for-the-campaign-history-layer.md) shipped the history cache
(`src/nightwright/agentic/history_cache.py`). [SPEC-0536](./0536-history-cache-opt-in-replaces-spec-0472-decision-1.md)
made `cache=None` opt-in through `agentic.history.cache` (true) and a non-empty `cache_dir`;
`cache=True` always uses the cache and `cache=False` never does. The `loop_exhausted` review of
campaign IDEA-1015-20261108-faf2 left two kinds of leftovers. This spec closes both.

**1. A change of the live-campaign-id set is not an input to every stored terminal.** The pure
reader resolves a global record's campaign two ways: the record's own `campaign` field, or, when
that is absent, a text link. `_campaign_text_link` (`history.py`) accepts the text only when
exactly one live id appears in the record's title and text. So the set of live ids (the child
directories of `store.campaigns_dir`) is an input to every terminal document that holds a global
record, whether or not that campaign's own files moved. Today the cache reacts to a changed set
in three disconnected places, and each one has a gap:

- `_prune_gone_campaigns` runs only when an index id is no longer live. It re-resolves global
  records, but compares each survivor to `campaign_history(cache=False)` one at a time, and
  never runs when a directory was only *added*.
- `_refresh_live` handles a directory that is live but absent from the index (`missing`). It
  re-resolves the cached global records, but recomputes no stored terminal whose linked global
  records changed. Example: a mail names `A-1` and, before the new directory `A-1b` exists,
  links to `A-1`. Once `A-1b` is live, the text is ambiguous and the pure reader links it to
  nobody, but the stored `A-1` terminal document and `records.jsonl` still hold the link.
- `_merge_records(..., drop_campaign_all=cid, add=...)` is called from
  `_recompute_terminal_documents`, `_refresh_live`, `_campaign_history_cached`, and
  `_prune_gone_campaigns`. It drops every record whose `campaign` is `cid`, global records
  included, and re-adds only what that one recomputed document holds. A global record that has
  *moved* from campaign A to campaign B (because the live set changed) and is dropped under A's
  recompute is lost from `records.jsonl` when B's document is not rebuilt in the same pass.
- `_cold_fill` takes `cids = _campaign_ids(cfg)` once and recomputes every id in that snapshot.
  A directory removed during the fill still gets `campaigns/<cid>.json`, a pending index write,
  and its local records in `all_records`. The final `known - live` prune removes the file and
  the row, but not the records, and the work was wasted.

**2. SPEC-0472's acceptance criteria are only partly covered by tests.** Its AC4 signature
inputs (`utime` on `state.json`, `runs/`, `gates/`; a same-size ledger with a different mtime),
mailbox delete and reply, a flaky replace, a `resolve_profile` change reaching `loop_max`, a
harmless `runner.jsonl` append that opens no ledger, a non-JSON `index.jsonl` line, an `OSError`
from a cache write, `rebuild_cache` raising, cached CLI counts and the data-dir hash,
`clone_fixture`'s id, escalation-filename and timestamp rules, an exact cache-tree file list,
and a sentinel for the real XDG cache all lack a test that fails when the behavior regresses.

## Goals / Non-goals

**Goals**

- One reconcile step, run under the cache lock whenever the live id set differs from the id set
  the cache last indexed, that (a) drops gone campaigns' local records and files, (b) re-resolves
  every cached global record against the new live set, and (c) recomputes exactly the stored
  terminal campaigns whose pure `campaign_history(..., cache=False)` document would differ from
  the stored one (ignoring `generated_at`).
- No call site removes global records by `campaign` id. Local records of one campaign are
  replaced by that campaign's recompute; global records are replaced only by the re-resolve.
- `_cold_fill` skips an id whose directory is already gone and, when the live set changed during
  the fill, runs the same reconcile before it commits.
- Tests for each leftover listed in Context 2, in the existing test modules.

**Non-goals**

- Any change to the three v1 schemas, to `campaign_signature`'s five-tuple, to link rules, to
  the cache layout, to the SPEC-0536 opt-in, or to the SPEC-0472 budgets.
- Making a changed live set a *cursor* in `sources.json`. The index id set is already the
  comparison point (`index_ids != live`); no new stored key is added.
- Recomputing every terminal on a live-set change. Only the ones whose pure document differs.
- Production changes for the test-only items: they expose no new production defect unless a new
  test fails, and then the failure is fixed in the same change.

## Decision

1. **The live id set is an input to every stored terminal, checked by one reconcile step.**
   A new `_reconcile_live_set(cfg, root, now, index_rows, records, *, _hold_guard)` in
   `history_cache.py` replaces the live-set handling in `_prune_gone_campaigns` and the
   `missing` handling in `_refresh_live`. It runs under the cache write lock whenever
   `index_ids != _live_campaign_ids(cfg)` (an id added, an id removed, or both). Order:
   1. For each id in `pruned = index_ids - live` (also ids only in `index.writes.jsonl` or only
      as `campaigns/<cid>.json`, as `_known_campaign_ids` collects them): delete the file and
      drop that id's *local* records (`source` `ledger` or `run`). Global records that name the
      id by field stay until step 2 replaces them.
   2. Recompute the global record set with `_global_records(cfg, sorted(live), gaps)` and
      `_enrich_records`, with headers from the surviving index rows plus rows indexed in step 4.
      Replace every non-local record in `records` with that result.
   3. Build the affected set: every campaign id that owns a global record whose
      `(id, campaign, linked_by, links, spec_id, project)` differs between the record set before
      and after step 2, taking both the old and the new owner. Of those, keep the ids that are
      indexed with a terminal outcome and still live.
   4. Ids that are live but not indexed are computed and stored (what `missing` does today).
   5. Each affected terminal is recomputed with `_recompute_campaign` and stored again. The set
      is allowed to be larger than the pure diff, never smaller: an id whose stored document
      differs from the pure document in any field other than `generated_at` must be in it.
2. **A recompute replaces a campaign's local records only.** `drop_campaign_all` is removed from
   `_merge_records`. Every call site uses `drop_campaign=<cid>` (local records of that campaign)
   and adds `_local_records(doc)`. Global records are replaced only by step 2 above and by
   `_sync_globals`, which already replaces them by source or kind. A global record that names a
   deleted campaign by field stays, as SPEC-0472 requires.
3. **`_cold_fill` is live-set aware.** Before each `_recompute_campaign` it checks that the
   campaign directory still exists, and skips the id when it does not (still calling
   `progress(done, total)` for it, so `done` reaches `total`). After the loop, and before the
   index and records commit, it lists the live set again; if the set differs from the starting
   snapshot it runs `_reconcile_live_set` with `_hold_guard=True` on the data it holds, so the
   committed tree already matches the pure reader.
4. **The test-only items are added to the existing modules.** No new module and no new
   speed tier: `tests/test_history_cache.py`, `tests/test_cli_campaign_history.py`, and
   `tests/test_history_scale.py` already carry `pytestmark = pytest.mark.fast`.

## Design

### Reconcile trigger

`_live_campaign_ids` memoizes the listing by the campaigns directory `st_mtime_ns`; that stays.
The comparison `index_ids != live` is already computed on every path that reaches the lock
(`_history_index_cached`, `_campaign_history_cached`). Those paths call
`_reconcile_live_set` in place of `_prune_gone_campaigns` and of the `_live_refresh_targets`
`missing` half. `_live_refresh_targets` keeps returning the non-terminal ids that are recomputed
on every call. The quiet-hit shortcuts (`_cache_refresh_quiet`, `_doc_hit` with
`index_ids == live`) are unchanged: a set that did not change takes the old fast path, so the
warm index and warm open budgets are untouched.

### What "differs" means

The affected set is computed from the record sets, not from a pure recompute per campaign, so a
500-campaign cache with one new directory does not run 500 pure derivations. The assertion in
the tests is the strong one: every stored terminal equals
`campaign_history(cfg, cid, now=..., cache=False)` except for `generated_at`, and
`history_records(cache=True)` equals `history_records(cache=False)`. If the cheap diff ever
misses a campaign, that test fails and the diff is widened, not the test relaxed.

### Call sites changed

| Site | Change |
|---|---|
| `_merge_records` | Remove `drop_campaign_all`; keep `drop_campaign` (local only) and `drop_predicate`. |
| `_recompute_terminal_documents`, `_refresh_live`, `_campaign_history_cached` | `drop_campaign=cid`, `add=_local_records(doc)`. |
| `_prune_gone_campaigns` | Becomes the pruning half of `_reconcile_live_set`; gone ids drop local records only; the global re-resolve replaces the rest. |
| `_cold_fill` `_persist` | Skip gone ids; final live-set check and reconcile; `all_records` takes `_local_records(doc)` only for ids still live. |

### Tests (existing modules, additive)

`tests/test_history_cache.py` (fast). `H` is `nightwright.agentic.history`, `NOW` the module
constant, `cfg(data_dir, cache_dir)` the module helper. Every "equals pure" assertion compares
`campaign_history(..., cache=True)` with `cache=False` minus `generated_at`, and
`history_records` with `cache=True` and `cache=False` including order.

| Test | What it asserts |
|---|---|
| `test_live_set_add_unlinks_text_link` | Mail text names `A-1`; the cache links it to `A-1`; adding live directory `A-1b` makes the link ambiguous; the stored `A-1` terminal and `records.jsonl` equal the pure ones. |
| `test_live_set_remove_relinks_text_link` | The reverse: removing `A-1b` makes the mail link to `A-1`; the `A-1` terminal document equals pure. |
| `test_moved_global_record_not_dropped` | A global record moves A to B on a live-set change; `records.jsonl` still holds it, under B; `grep` finds no `drop_campaign_all` in `src/`. |
| `test_recompute_touches_only_differing_terminals` | After a live-set change, a spy on `H._campaign_history` is called only for ids whose pure document differs, and a terminal whose document is unchanged keeps its file mtime. |
| `test_cold_fill_skips_gone_id` | A directory removed mid-fill (patch `_recompute_campaign` to delete the next dir) leaves no `campaigns/<cid>.json`, no pending line, no local record of it, and `progress` reaches `done == total`. |
| `test_cold_fill_live_set_added_mid_fill` | A directory added mid-fill is indexed and its text links match pure after the fill returns. |
| `test_signature_input_utime_state_json` / `_runs` / `_gates` | `os.utime` on `state.json`, `runs/`, `gates/` each changes the signature, the next terminal read recomputes and stores the new signature. |
| `test_same_size_ledger_mtime_recomputes` | Rewrite `ledger.jsonl` to the same size with a different `mtime_ns`; the next read recomputes. |
| `test_mailbox_delete_and_reply` | Deleting one `mailbox/*.jsonl` drops only its mail records; a reply flips the earlier message's status to the pure value. |
| `test_flaky_replace_replaces_flaky_records` | Replacing `flaky-tests.json` replaces every `flaky` record and equals pure. |
| `test_profile_layer_change_updates_loop_max` | Changing a layer `resolve_profile` reads makes the terminal header's `loop_max` equal pure. |
| `test_harmless_runner_append_opens_no_ledger` | A `runner.jsonl` append after `closed_at` that adds no gap and no overlapping down interval opens no `ledger.jsonl` (spy on `store.read_ledger` and `builtins.open`). |
| `test_non_json_index_line_rebuilds` | A non-JSON `index.jsonl` line deletes the tree, writes `version` `1\n`, and returns the pure index. |
| `test_cache_write_oserror_falls_back` | Patch `os.replace` to raise `OSError`: `campaign_history(cache=True)` returns pure plus `cache: not writable`, `history_index` and `history_records` return pure lists, nothing under `data_dir` changes. |
| `test_rebuild_cache_raises_oserror` | `rebuild_cache` with an unwritable `cache_dir` raises `OSError` and writes nothing under `data_dir`. |
| `test_cache_tree_file_whitelist` | After a cold index, a records query, and a campaign open, the relative file set under `cache_dir` is a subset of `history/v1/{version,.lock,index.jsonl,records.jsonl,sources.json}` and `history/v1/campaigns/<cid>.json`; `index.writes.jsonl` and `*.tmp` are absent; no file exists outside `history/v1/`. |
| `test_real_xdg_cache_untouched` | `XDG_CACHE_HOME` and `HOME` point at a sentinel temp directory; with a cfg without `cache_dir`, and with `cache=False`, every public function leaves the sentinel empty. |

`tests/test_cli_campaign_history.py` (fast): `test_cached_cli_counts_and_hash` runs
`--rebuild-cache`, `--rebuild-cache --json`, then `--list --json` with cache on. The count line
and the JSON `campaigns` and `records` equal `len(history_index(cache=False))` and
`len(history_records(cache=False))`, the `--list --json` rows equal the pure index, and the
data-dir hash is unchanged across all three.

`tests/test_history_scale.py` (fast): `test_clone_minted_ids_unique_and_new` (ids unique, none
equal to a source id), `test_clone_escalation_filename_is_id` (each appended escalation's
filename equals its `id`), `test_clone_appended_ids_one_above_max` (appended `seq` and ids are
one above the previous maximum and unique), `test_clone_appended_timestamps_shift_by_delta`
(each appended timestamp equals the source's plus that clone's delta), and
`test_clone_locator_rule_raises` (a supported source row with neither `campaign` nor
`source.campaign` raises). Where an existing test already covers a rule, the new test is the
one that fails when the rule is broken; the old one stays.

## Alternatives considered

- **Keep `drop_campaign_all` and patch each caller to re-add the moved global records.** Four
  call sites would each need the same re-add, and a fifth would bring the bug back.
  Removing the parameter makes the mistake unwritable.
- **Store the live id set as a `sources.json` cursor.** The index id set already is the stored
  copy; a second copy can drift from it.
- **Recompute every terminal on any live-set change.** Correct and simple, but it spends 500
  pure derivations on one new directory and breaks the "computed once" contract of SPEC-0472.
- **Compare every survivor to `campaign_history(cache=False)`** (what `_prune_gone_campaigns`
  does today). Same cost problem; the record-diff finds the same set cheaply.

## Acceptance criteria

- [ ] **AC1. Added directory re-resolves text links.** With a warm cache, adding a live campaign
  directory that makes a mail's text link ambiguous leaves every stored terminal document and
  `history_records(cache=True)` equal to the `cache=False` results (minus `generated_at`).
- [ ] **AC2. Removed directory re-resolves text links.** Removing a live directory that made a
  link ambiguous gives the same equality; the gone id has no row, file, or local record, and a
  global record naming it by field is still returned.
- [ ] **AC3. Moved globals survive.** `drop_campaign_all` appears nowhere in `src/`; a global
  record that moves between campaigns on a live-set change is in `records.jsonl` exactly once,
  under its new owner.
- [ ] **AC4. Only differing terminals recompute.** After a live-set change, a terminal whose pure
  document is unchanged is not recomputed (its stored file is not rewritten), and every terminal
  whose pure document differs is.
- [ ] **AC5. `_cold_fill` skips gone ids.** An id whose directory vanishes during a cold fill
  writes no campaign file, no pending write, and no record; `progress` ends at `done == total`.
  An id added during the fill is indexed and its links match pure.
- [ ] **AC6. Unchanged live set keeps the fast path.** With an unchanged set the warm
  `history_index` and warm `campaign_history` still pass the SPEC-0472 AC2 "hit" assertions and
  the AC8 budgets.
- [ ] **AC7. Signature inputs.** `utime` on `state.json`, `runs/`, `gates/`, and a same-size
  ledger with a different mtime each cause a recompute and store the new signature.
- [ ] **AC8. Global-source cases.** Mailbox delete and reply, flaky replace, a profile-layer
  change to `loop_max`, and a harmless `runner.jsonl` append (no ledger open) each match the
  pure result as the tests above state.
- [ ] **AC9. Corrupt and failing writes.** A non-JSON `index.jsonl` line rebuilds to
  `version` `1\n` and the pure index; an `OSError` on a cache write falls back to the pure
  results with `cache: not writable` on `campaign_history`; `rebuild_cache` raises `OSError`.
- [ ] **AC10. CLI counts and purity.** Cached `--rebuild-cache`, `--rebuild-cache --json`, and
  `--list --json` report counts and rows equal to the pure layer, and the data-dir hash is
  unchanged.
- [ ] **AC11. Cache tree and XDG.** The cache tree holds only the whitelisted files, and no
  public function writes to the real XDG cache when `cache_dir` is absent or `cache=False`.
- [ ] **AC12. Clone rules.** The `clone_fixture` tests for minted ids, escalation filenames,
  appended sequence ids, timestamp shift, and the locator rule pass, and
  `tests/history_scale.py` still imports nothing from `nightwright`.

## Test plan

- **Automated tests:** the tests named in *Design / Tests*, in `tests/test_history_cache.py`
  (extended, already `pytestmark = pytest.mark.fast`), `tests/test_cli_campaign_history.py`
  (extended, `fast`), and `tests/test_history_scale.py` (extended, `fast`). No new
  `tests/test_*.py` module, so no new speed-tier marker. Fixtures reuse `data_dir`, `cache_dir`,
  `cfg`, `_install_live_campaign`, and `_hashes`. Run with
  `python3 tools/affected_tests.py --run uv run pytest -q` plus those three files, and
  `tests/test_history_fixture.py`, `tests/test_history_perf.py`, `tests/test_dashboard_cache.py`
  as regression guards.
- **Test-first check:** each new test in *Tests* is run once against the unchanged code; the
  live-set tests (AC1 to AC5) fail there and pass after the change. The test-only items (AC7 to
  AC12) may pass at once; for each one the implementer breaks the guarded behavior by hand once
  (for example stat the wrong file in the signature, drop `fsync`, skip the whitelist) to see
  the test fail, and records that in the spec's *As shipped* note.
- **Manual verification:** on a copy of the fixture, `nw campaign history --list --json`, then
  `mkdir` a campaign directory whose id extends an id named in a mail, run the command again;
  the output equals the same command with `agentic.history.cache` unset.
- **Regression guard:** `python3 tools/test_tiers.py --check` exits 0; `python3
  tools/spec_lint.py` exits 0; the perf test's ×3 ceilings still hold; the SPEC-0472 tests pass
  unchanged.

## Rollout / migration

No data migration. The cache is disposable; the next call after upgrade reconciles whatever
live-set drift exists. `nw campaign history --rebuild-cache` remains the full reset.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; affected tests pass; manual step performed.
- [ ] No regressions (`tools/spec_lint.py`, `tools/test_tiers.py --check`).
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` regenerated.

## Open questions

- None.
