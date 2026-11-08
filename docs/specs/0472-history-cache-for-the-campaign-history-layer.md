---
id: SPEC-0472
title: "History cache for the campaign history layer"
status: proposed
owner: user
created: 2026-11-08
source_idea: IDEA-1015
kind: feature
parent: SPEC-0405
depends_on: [SPEC-0415, SPEC-0406]
tags: [agentic, observability, history]
---

## Context

This is the cache half of the history layer in epic
[SPEC-0405](./0405-rich-queue-tui-prioritized-live-queue-campaign.md).
[SPEC-0406](./0406-shared-campaign-history-data-layer-nw-campaign.md) shipped the pure
derivation (`src/nightwright/agentic/history.py`), the three read-side schemas, the
`campaign_export` reuse, and the uncached CLIs `nw campaign history CID|--list` and
`nw campaign records`. It left the cache, `--rebuild-cache`, and the perf budgets for this
spec. [SPEC-0415](./0415-campaign-history-fixture-spec-0405-child.md) shipped the sanitized
fixture and `tests/history_scale.copy_fixture`. The scale clone that the budgets run on was
specified there and again in
[SPEC-0459](./0459-real-app-tui-test-harness-on-the-real-data.md) and was not written.
[SPEC-0411](./0411-closed-campaign-history-browser-tab.md) already assumes this cache: a cold
index build with progress, then a warm load.

An earlier attempt at this cache was abandoned. Its index refresh deleted a campaign whose
directory was gone, then merged a pending index write that still named that campaign, so the
row came back and a deleted campaign stayed in the history cache. This spec keeps incremental
index writes and makes that outcome impossible.

**What the pure layer does today.** `campaign_history`, `history_index`, and `history_records`
recompute from `cfg["data_dir"]` on every call. They write nothing. `campaign_signature`
(`history.py`) is the hex SHA-1 of `(ledger size, ledger mtime_ns, state.json mtime_ns, runs/
mtime_ns, gates/ mtime_ns)`. `history_index` lists immediate child directories of
`store.campaigns_dir` and sorts rows by `(closed_at or created_at, campaign id)` descending.
Global inputs the derivation actually reads are `conductor/decisions.jsonl`,
`escalations/ESC-*.json`, `mailbox/*.jsonl`, `reviews/<SPEC>/<sha>.json`,
`spec-reviews.jsonl`, `flaky-tests.json`, `sentinel/*.last.json`, and `runner.jsonl`
(`_runner_intervals`). `events.jsonl` is not an input. CLI cfg from `load_config` always has
`cache_dir`. The existing history tests build a cfg with only `data_dir`.

The epic's cache contract, which this spec follows, is a disposable tree under
`cfg["cache_dir"]`, outside the data dir, so SPEC-0400's "every other data-dir file
byte-identical" invariant holds. A terminal campaign is computed once. Append-only global
logs advance by offset. A shrink or an inode change re-reads that log from the start. Writes
are atomic. A corrupt or wrong-version cache is rebuilt. The product budgets, measured on a
500-campaign clone, are cold index ≤ 10 s, warm index ≤ 300 ms, warm campaign open ≤ 500 ms,
and a records query over at least 50 000 records ≤ 200 ms. Tests allow ×3 slack. The
live-queue repaint budget stays with the queue view.

## Goals / Non-goals

**Goals**

- A disposable cache at `<cache_dir>/history/v1/` that `campaign_history`, `history_index`,
  and `history_records` read and write, with results equal to the pure reader except the
  stored `generated_at` on a terminal cache hit.
- `history.rebuild_cache` and `nw campaign history --rebuild-cache`, which drop that tree
  and build it again.
- Incremental global-log reads, signature invalidation, and a full rebuild of a corrupt or
  wrong-version tree.
- `_merge_index_writes` applies pending index rows and then removes every pruned campaign id,
  so a deleted campaign's index row stays out, including when a pending write still names it.
- The four product budgets above, on a 500-campaign clone with at least 50 000 history
  records.
- An unwritable cache degrades to the pure result. `campaign_history` records the gap
  `cache: not writable`.

**Non-goals**

- The TUI queue, the detail popup, the History tab, and the Findings tab. They consume this
  layer later. The ≤ 50 ms live-queue repaint budget and the idle-queue rule (the Queue tab
  reads no file of a terminal campaign) stay with those specs.
- Any change to the three v1 schemas, to phase math, to link rules, or to
  `campaign_signature`'s five-tuple.
- Parsing or caching `events.jsonl`.
- Deleting campaign directories, editing ledgers, or changing retention. This spec only
  drops cache entries when a directory is already absent.
- A new database or a cache inside the data dir.
- Importing Textual, `nightwright.tui`, or a dashboard package from `history.py`.

## Decision

1. **The cache is a disposable directory beside the data dir.** Every cache file lives under
   `<cfg["cache_dir"]>/history/v1/`. The public functions gain a keyword-only `cache`
   argument. `cache=False` is the pure reader and writes nothing. `cache=None` (the default)
   uses the cache when `cfg["cache_dir"]` is a non-empty string, and is the pure reader
   otherwise, with no gap and no write. `cache=True` uses the cache, and an unwritable cache
   falls back to the pure result with the gap on `campaign_history`. Callers that omit
   `cache_dir`, including today's history tests, stay on the pure path.
2. **A terminal campaign is stored once and reused.** A campaign whose `state` is in
   `store.TERMINAL_STATES` is served from `campaigns/<cid>.json` while `campaign_signature`
   matches the stored signature and no global input that can change that document has moved.
   A non-terminal campaign is recomputed on every `campaign_history` and every
   `history_index`, because its open visit runs to `now`.
3. **`_merge_index_writes` drops pruned ids last.** The refresh keeps a pending write log so
   a cold build can resume. The merge copies the committed index, applies pending writes in
   order, and then deletes every id in `pruned`. The deletion is the last mutation of the
   row map. A write that names a pruned id does not appear in the result. The caller puts
   every known id whose campaign directory is gone into `pruned`, including ids that exist
   only in the pending log or only as a leftover `campaigns/<cid>.json`.
4. **Global logs move by cursor; affected campaigns are recomputed with the pure function.**
   Append-only files advance from the stored offset. A shrink or an inode change replaces
   that source's cached records from a full re-read. The campaigns those records touch are
   recomputed with `campaign_history(..., cache=False)` and stored again. Equivalence is
   that recompute, not a second derivation.
5. **The perf corpus is test support in `tests/history_scale.py`.** `clone_fixture` grows
   the SPEC-0415 fixture to 500 campaigns and at least 50 000 finding lines. It does not
   import `nightwright`. `tests/test_history_perf.py` is the budget oracle.

## Design

### Where the cache lives

```
<cache_dir>/history/v1/version                 # the single line "1\n"
<cache_dir>/history/v1/.lock                   # flock, not a logical cache entry
<cache_dir>/history/v1/campaigns/<cid>.json    # one nw.campaign.history.v1 document
<cache_dir>/history/v1/index.jsonl             # one nw.history.index.v1 row per line
<cache_dir>/history/v1/index.writes.jsonl      # pending index rows, one per line
<cache_dir>/history/v1/records.jsonl           # record lines, plus a cache-only search field
<cache_dir>/history/v1/sources.json            # per-source cursors
```

`<cid>.json` is written only when `cid` is a single path component other than `.` or `..`
and contains no slash and no NUL. Any other id is computed and left uncached. Orphan
`*.tmp` files in this tree are ignored and removed on the next locked refresh. The logical
cache is the seven entries above; deleting `history/v1/` is always safe.

`version` is exactly the bytes `1\n`. A missing version while any other logical file
exists, any other version text, or any unparsable logical file (a JSON document that
`json.loads` rejects, or a jsonl line that is not a complete JSON object) makes the tree
corrupt. The refresh then deletes `history/v1/` and builds it again. A corrupt tree is
never returned.

### Public functions

In `src/nightwright/agentic/history.py`:

| Name | Behavior |
|---|---|
| `campaign_history(cfg, cid, *, now=None, cache=None)` | Same return as today. A valid terminal file is returned without the lock and without a ledger read. A miss takes the lock, computes, writes `campaigns/<cid>.json`, and merges that index row. |
| `history_index(cfg, *, now=None, progress=None, cache=None)` | Same row shape and sort as today. `progress(done, total)` still runs once per campaign directory. |
| `history_records(cfg, *, campaign=None, agent=None, kind=None, project=None, severity=None, status=None, since=None, until=None, query=None, limit=None, cache=None)` | Same filter semantics as today. Returned dicts have the v1 keys only. |
| `rebuild_cache(cfg, *, now=None, progress=None) -> dict` | Deletes `history/v1/` and builds it. Returns `{"campaigns": N, "records": M}`. Requires `cfg["cache_dir"]`. Raises `OSError` when the tree cannot be created or written. |

`N` is the number of index rows. `M` is the number of record lines.

**Equality.** For the same `cfg`, filters, and `now`:

- `cache=False` returns the same dicts as today's functions, including `generated_at`,
  and creates no file anywhere.
- A terminal cache hit matches `cache=False` on every field except `generated_at`.
  `generated_at` stays the value stored when that document was computed
  (`store.now_iso` of the `now` used then).
- A non-terminal `campaign_history` matches `cache=False` on every field, including
  `generated_at`.
- Index rows match field for field, including `signature` and `records`.
- `history_records` matches in id order and in every returned field.

**Unwritable cache.** `cache=True`, or `cache=None` with `cache_dir` set, and the directory
cannot be created or a cache write raises `OSError`: `campaign_history`, `history_index`,
and `history_records` return the pure result and do not raise. `campaign_history` appends
`cache: not writable` to `gaps` when that string is absent. `rebuild_cache` raises
`OSError` and writes nothing under `data_dir`.

### Writes

Every logical replace is a temp file in the same directory, `fsync`, then `os.replace`.
The pending log is the exception: `index.writes.jsonl` is appended, one complete JSON line
ending in `\n` per row, and `fsync` after each append. It is unlinked only after
`index.jsonl` has been replaced. A crash before that replace leaves the pending lines for
the next refresh.

An exclusive `fcntl.flock` on `.lock` covers every full refresh and `rebuild_cache`. The
lock is taken before the directory listing that computes `pruned` and released after the
replaces. A hit on a complete `campaigns/<cid>.json` for a terminal campaign returns that
document with no lock and no ledger read. A miss, a non-terminal campaign, an invalid
signature, or a moved global cursor takes the lock, recomputes, and merges.

The data dir is not written. Cache paths are only under `cfg["cache_dir"]`. When that key
is missing, no path is written, including the process's real XDG cache.

### Refresh

Inside the lock:

1. If the tree is corrupt or the version mismatches, delete it and continue with an empty
   tree.
2. List `live`, the immediate child directory names of `store.campaigns_dir`, the same set
   `history_index` uses today.
3. Collect `known`: campaign ids in `index.jsonl`, in `index.writes.jsonl`, and the stems
   of `campaigns/*.json`. `pruned = known - live`. Recompute `pruned` from a fresh listing
   immediately before the merge, so a directory removed during the refresh is included.
4. Delete `campaigns/<cid>.json` for every id in `pruned`. Drop campaign-local records for
   those ids (Design, *Records*). Re-resolve text links on the cached global records
   against `live`, using the same helpers the pure reader uses (`_campaign_text_link` and
   `_enrich_records`). Recompute any surviving campaign whose linked global-record set
   changed.
5. For each id in `live`, reuse the stored terminal document when it is valid, otherwise
   compute with the pure function at this refresh's `now` and write `campaigns/<cid>.json`
   and one line to `index.writes.jsonl`. A pruned id is never appended.
6. `merged = _merge_index_writes(committed_rows, pending_rows, pruned)`.
7. Replace `index.jsonl` with `merged`, then unlink `index.writes.jsonl`.
8. Replace `records.jsonl` and `sources.json` to match the records and cursors of this
   refresh.

`history_index` returns `merged` (already in `history_index` order). `filter_index` stays
the CLI's job and stays pure.

### `_merge_index_writes`

```python
def _merge_index_writes(index_rows, writes, pruned) -> list[dict]:
```

- `index_rows` is the committed index, in file order.
- `writes` is the pending lines plus rows produced in this refresh, in append order.
- `pruned` is a collection of campaign id strings that must be absent.

Steps, in this order:

1. Build a map from `campaign` to row, starting from `index_rows`. A later duplicate in
   `index_rows` replaces an earlier one. A row whose `campaign` is not a non-empty string
   is skipped.
2. Apply `writes` in order. A later write for the same id replaces the row, including an
   id that was not in the base.
3. Delete every key in `pruned`. This step is last. A write applied in step 2 for a pruned
   id is gone. A base row for a pruned id is gone.
4. Return the remaining values sorted by `(closed_at or created_at, campaign id)`
   descending, the same key `history_index` uses (`_time_key`, then the id, `reverse=True`).

The function writes no file and does not recreate `campaigns/<cid>.json`. The refresh
deletes that file for pruned ids before the call and does not write it afterward.

Campaign-local records for a pruned id are those whose `source` is `ledger` or `run` and
whose `campaign` is that id (ids `ledger:<cid>:<seq>` and `run:<cid>:<run_id>`). They are
removed in the same locked refresh. Global records that still name the deleted id stay,
because the pure reader emits them from the global logs (`_base_record` copies
`row["campaign"]`) even when the directory is gone. A second refresh with the same pending
write still returns no index row for that id.

### Invalidation

A stored terminal document is valid when all of the following hold:

- `campaign_signature(cfg, cid)` equals the document's `signature`.
- The document's `header.state` is in `store.TERMINAL_STATES`.
- Every global cursor that the document depends on is unchanged, by the rules below.

A signature mismatch recomputes that campaign only. A non-terminal state recomputes on
every call and is still written through so the index row exists; it is not served as a hit
on the next call.

`sources.json` maps a path relative to `store.agentic_dir` to a cursor:

| Source | Cursor | On change |
|---|---|---|
| `conductor/decisions.jsonl`, `mailbox/<inbox>.jsonl`, `spec-reviews.jsonl`, `runner.jsonl` | `{size, mtime_ns, inode, offset}` | Append, shrink, or inode change, as below |
| `escalations/ESC-*.json`, `reviews/<SPEC>/<sha>.json`, `flaky-tests.json`, `sentinel/*.last.json` | `{size, mtime_ns, inode}` and no offset | Any change replaces that file's records from a full re-read |

Append-only rules, given a new `stat`:

- Inode differs, or `size` is less than the stored `size`: drop that file's cached records
  and parse from byte 0. This is the shrink and replace case.
- `size` equals the stored offset and the inode matches: read nothing.
- `size` is greater than the offset: read from the offset, parse only complete lines that
  end in `\n`, and set the offset to the byte after the last complete line. A trailing
  partial line stays unparsed. The stored `size` becomes the stat size, so the next call
  with the same size and a lagging offset finishes the partial line once it is complete.

New lines are parsed by the same helpers `_global_records` and `_runner_intervals` use.
Campaigns named by the new records, after text-link against `live`, are recomputed.
Campaign-less new records are inserted into `records.jsonl` only.

`runner.jsonl` growth recomputes a terminal campaign only when a newly parsed down
interval overlaps that campaign's `[created_at, closed_at]`. An append that lies entirely
after `closed_at` leaves the stored document in place, so a live runner log does not
rebuild every closed campaign. A shrink or inode change of `runner.jsonl` recomputes every
campaign whose wall clock overlaps any down interval in the full file, and recomputes a
campaign that previously had a non-zero `runner_down_s` so a removed interval disappears.

A mailbox file, escalation, review, or sentinel file that disappears drops the records
that came from it. A new file is parsed in full. `flaky-tests.json` is one file: any
change replaces every `flaky` record.

### Records file

Each `records.jsonl` line is the v1 record plus one cache-only key:

```json
{"search": "<casefold of title, a space, and text>"}
```

The string is `f"{title or ''} {text or ''}".casefold()`, the same string the pure
`history_records` searches. Callers receive the line with `search` removed. A query
casefolds the needle and tests it against `search`. Filters, date bounds, the newest-first
sort (`ts`, then `id`), and `limit` are unchanged.

The committed record set is the pure `history_records` result for this data dir: every
campaign-local record of a live campaign, every global record the pure reader emits
(including global records whose `campaign` directory is gone and records with no
campaign), with text links resolved against `live`.

### CLI

In `cli_campaign.py`, `nw campaign history` takes exactly one of `CAMPAIGN`, `--list`, and
`--rebuild-cache`. Zero or two or more is a usage error, exit 2. `--rebuild-cache` combined
with `--project`, `--spec`, `--since`, `--until`, `--min-cost`, or `--state` other than
`all` is exit 2. `--state all` is the click default and is accepted.

```
nw campaign history --rebuild-cache
nw campaign history --rebuild-cache --json
```

- Success, exit 0. Stdout text is `history cache rebuilt: <N> campaigns, <M> records`.
  `--json` prints `{"campaigns": N, "records": M}` and nothing else.
- Progress stays the SPEC-0406 line, on stderr, and only when stderr is a TTY:
  `history: <done>/<total>` every 50 campaigns. `--list` keeps that line.
- `OSError` from `rebuild_cache` prints `cache: not writable` on stderr and exits 1. The
  data dir is unchanged.
- An unknown `CAMPAIGN` still prints `campaign <cid> not found` on stderr and exits 1,
  and writes no `campaigns/<cid>.json`.
- `--list` and `CAMPAIGN` and `nw campaign records` use `cache=None`, so a normal cfg
  caches and a cfg without `cache_dir` stays pure. Their stdout shapes are unchanged.
- When a cached command falls back because the cache is unwritable, stderr includes the
  line `cache: not writable` once. `--json` stdout stays the document, the index
  envelope, or the records envelope.

### Scale clone

`tests/history_scale.py` gains `place_bytes(src, dst)` and
`clone_fixture(dst, *, campaigns=500, records=50_000, seed=0) -> Path`. Neither imports
`nightwright`. `dst` follows `copy_fixture`: it must be missing or empty, the returned
path is `dst`, and the data dir is `dst/agentic/`.

`place_bytes` hardlinks `src` to `dst` when both are files. On `OSError` it copies the
bytes. `clone_fixture` uses it for unchanged file bodies inside `dst`.

`clone_fixture` copies the committed fixture, then adds clone campaign directories until
`campaigns` directories exist. Clone ids are deterministic for `seed`, unique, and
distinct from every source id. It rewrites campaign locators with the SPEC-0459 rule:
replace a top-level `campaign` when present, otherwise replace `source.campaign`; a ledger
finding keeps no new `campaign` field and belongs to the clone whose ledger holds it; a
supported source row with neither locator raises. It keeps appending, on clones, at least
one finding, one decision, one `events.jsonl` event, one mail line, and one escalation,
until the corpus contains at least `records` ledger events with `type` `finding`. Original
lines of an appended file stay a byte-for-byte prefix. Appended sequence and id fields are
one above the previous maximum and unique. An escalation's filename is its `id`. Appended
timestamps shift by that clone's delta. The same `(dst contents of the fixture, campaigns,
records, seed)` produces the same bytes.

`events.jsonl` is cloned so the corpus stays a data dir. The cache does not read it.

### Budgets

On `clone_fixture(tmp, campaigns=500, records=50_000)`, with `cache_dir` outside that
tree:

| Operation | Product budget | Test ceiling (×3) |
|---|---|---|
| Cold `history_index` from an empty cache, including the writes | ≤ 10 s | ≤ 30 s |
| Warm `history_index` with no input changed | ≤ 300 ms | ≤ 900 ms |
| Warm `campaign_history` of one terminal campaign whose file is already stored | ≤ 500 ms | ≤ 1.5 s |
| `history_records` with a full-text `query` over a cache that holds at least 50 000 records | ≤ 200 ms | ≤ 600 ms |

Times are `time.perf_counter` around the call, after the corpus exists. The cold call is
the first `history_index`. The warm index call is the next `history_index` with the same
`now`. The open call is one `campaign_history` of a terminal clone. The query call is one
`history_records(..., query=<a needle present in one finding>)`. The TUI's paint time is
out of scope; the 500 ms row is this data load.

## Alternatives considered

- **Recompute on every call.** This is SPEC-0406, and it misses the warm-index and
  records-query budgets once hundreds of campaigns are closed.
- **Rewrite `index.jsonl` from `live` with no pending log.** A crash in a cold build
  restarts from zero, and the epic's resume path is the pending log. The abandoned failure
  was the merge order, which Decision 3 fixes, so the pending log stays.
- **Drop every record whose `campaign` equals a pruned id.** The pure reader still emits
  global records that name a missing directory. Dropping them would make `cache=True`
  disagree with `cache=False`.
- **SQLite under `cache_dir`.** The epic fixes a file tree, jsonl cursors, and atomic
  replace. A new store adds a dependency and a second layout.
- **Invalidate every terminal campaign when `runner.jsonl` grows.** The runner appends
  while the queue is up. Closed campaigns end before those lines, and rebuilding all of
  them drops the warm path.

## Acceptance criteria

- [ ] **AC1. Cache location and purity.** With `cache_dir` set outside `data_dir`, a
  cached `history_index` writes only under `<cache_dir>/history/v1/` and only the files
  in Design. A hash of every file under `data_dir` is unchanged. Deleting `history/v1/`
  and calling `history_index` again rebuilds a tree whose `version` is `1\n` and whose
  index rows equal the pure index. `import nightwright.agentic.history` loads neither
  `textual`, `nightwright.tui`, nor a dashboard module.
- [ ] **AC2. Cache switch.** `cache=False` matches the pure results, including
  `generated_at`, and creates no file. A cfg with no `cache_dir` does the same, and does
  not write the real XDG cache. `cache=True` on an unchanged terminal campaign returns a
  document equal to the pure document except `generated_at`, and the second call does not
  open that campaign's `ledger.jsonl`. Index rows and `history_records` results (with
  `search` removed) equal the pure results. A non-terminal campaign opened at two `now`
  values matches the pure documents, including `generated_at` and a changed `wall_s`, and
  opens the ledger both times. `history_index(..., progress=)` calls `progress` once
  per live campaign directory, with `done` running from 1 through `total`. After a cold
  `campaign_history` of one terminal id, the following `history_index` includes that row
  and does not open that campaign's ledger.
- [ ] **AC3. A deleted campaign stays out of the index.** After a cached index exists,
  removing one campaign directory, planting `index.writes.jsonl` with that campaign's
  index row, and planting `campaigns/<cid>.json`, the next `history_index` returns no row
  for that id, `index.jsonl` has no such line, and `campaigns/<cid>.json` is gone.
  Planting the same write again and calling `history_index` a second time still omits it.
  `_merge_index_writes(base, writes, pruned)` omits the id when `pruned` contains it, for
  a base that already has the row and for a base that does not, while a write for a
  different id that is not pruned is present. A ledger or run record of the deleted id is
  gone. A decision whose `campaign` field is the deleted id is still returned, equal to
  the pure `history_records`. A mail whose only campaign link was that id in the text has
  the link the pure reader produces against the surviving directories.
- [ ] **AC4. Invalidation and cursors.** Changing any input of `campaign_signature` makes
  the next terminal read recompute and store the new signature. Appending one complete
  line to `decisions.jsonl` advances `sources.json` `offset` to the new size, reads only
  the tail, and the cached records equal the pure records. Replacing that file so its
  inode changes, or truncating it so `size` shrinks, re-parses from byte 0 and drops the
  appended decision. A `runner.jsonl` append dated after a terminal campaign's
  `closed_at` does not open that campaign's ledger. A down interval that overlaps the
  campaign's wall clock changes `runner_down_s` to the pure value. Deleting one
  `mailbox/*.jsonl` drops only that file's mail records. Replacing `flaky-tests.json`
  replaces every `flaky` record. Both results equal the pure reader.
- [ ] **AC5. Corrupt and torn caches.** A `version` of `999`, a `campaigns/<cid>.json` of
  `{`, or an `index.jsonl` line that is not JSON causes the next cached call to delete
  the tree, write `version` `1\n`, and return the pure results. A temp file left beside
  the tree is removed and is not parsed. After a crash simulated by an `index.writes.jsonl`
  that exists while `index.jsonl` has the previous rows, the next refresh merges and then
  unlinks the writes file. The refresh calls `fcntl.flock` with `LOCK_EX` on `.lock`
  before it lists campaign directories.
- [ ] **AC6. Unwritable cache.** When `cache_dir` is a file, or the cache write raises
  `OSError`, `campaign_history(..., cache=True)` returns the pure document plus the gap
  `cache: not writable` and does not raise. `history_index` and `history_records` return
  the pure lists. `rebuild_cache` raises `OSError`. The data dir is unchanged.
- [ ] **AC7. CLI.** Exactly one of `CAMPAIGN`, `--list`, and `--rebuild-cache` is
  required; other combinations exit 2, including `--rebuild-cache` with `--project` or
  `--state merged`. `--rebuild-cache` exits 0, prints the count line, and
  `--rebuild-cache --json` prints `{"campaigns": N, "records": M}`. A second
  `--list --json` matches the pure index. An unknown id exits 1 with
  `campaign <cid> not found`. An unwritable rebuild exits 1 with `cache: not writable`
  on stderr. The data dir hash is unchanged across all of these.
- [ ] **AC8. Budgets.** On a 500-campaign `clone_fixture` with at least 50 000 derived
  history records, the four operations in *Budgets* finish within the ×3 ceilings.
  `clone_fixture` is deterministic for a fixed seed, uses `place_bytes`, and keeps the
  SPEC-0459 locator, prefix, id, escalation-filename, and timestamp rules. It imports
  nothing from `nightwright`.
- [ ] **AC9. Schema surface.** Returned campaign documents, index rows, and records pass
  `history_problems`, `index_problems`, and `record_problems_v1`. Returned records have
  no `search` key. `HISTORY_SCHEMA`, `HISTORY_INDEX_SCHEMA`, and `HISTORY_RECORD_SCHEMA`
  are unchanged.

## Test plan

**Automated: cache behavior.** `tests/test_history_cache.py`, on a `copy_fixture` data dir
and a separate `cache_dir` under `tmp_path`.

- AC1: cached `history_index`, hash the data dir before and after, list the cache tree,
  delete it, rebuild, compare rows to `history_index(..., cache=False)`. Assert the import
  set the way `tests/test_history.py` already does (`sys.modules` has no `textual`,
  `nightwright.tui`, or dashboard module).
- AC2: `cache=False` and a cfg with no `cache_dir` (monkeypatch `paths.cache_dir` to a
  sentinel and assert it is not created). Terminal second read: count `ledger.jsonl`
  opens and compare documents with `generated_at` removed, then assert the stored
  `generated_at` differs when the second call uses a later `now`. The following
  `history_index` does not open that ledger either, and its row is present. A
  `progress` callback records one call per live campaign with `done` from 1 through
  `total`. Non-terminal fixture or a synthetic live campaign: two `now` values, both
  ledger opens, full equality with `cache=False`. Index and records equality, including
  a `query`.
- AC3: two campaigns in the cache, delete one directory, write a pending index line and a
  campaign JSON for the deleted id, call `history_index` twice around a second plant, and
  assert the index, the file, and the campaign JSON. Call `_merge_index_writes` directly
  for both base shapes. Assert a ledger finding id of the deleted campaign is absent and
  a planted `decisions.jsonl` row with `"campaign": "<deleted>"` is present and equals
  the pure record. Plant a mailbox line whose text names only the deleted id and compare
  the link to `cache=False`.
- AC4: touch each signature input (append a ledger byte, `os.utime` on `state.json`,
  `runs/`, and `gates/`) and assert a recompute. Append one decision, assert the offset
  and that a wrapped reader sees a read starting at the old offset (monkeypatch the
  tail read). Replace the file and truncate it, assert a zero offset and the decision
  gone. Append a post-close runner line and assert no ledger open; insert an overlapping
  down interval and assert `runner_down_s`. Delete one mailbox file and replace
  `flaky-tests.json`, and compare both results to `cache=False`.
- AC5: write the three corrupt shapes, assert a rebuilt version and pure equality. Leave
  a `*.tmp` file and a pending writes file, assert the tmp is gone and the writes file
  is unlinked after a successful `index.jsonl` replace. Monkeypatch `fcntl.flock` and
  assert `LOCK_EX` on `.lock` happens before the campaign-directory listing.
- AC6: `cache_dir` pointing at a file, and a monkeypatched replace that raises `OSError`.
  Assert the gap, the pure bodies, the `rebuild_cache` exception, and the data-dir hash.
- AC9: run the three validators on cached outputs, assert `search` is absent on returned
  records, and assert each raw `records.jsonl` line's `search` equals
  `f"{title or ''} {text or ''}".casefold()`. Assert the three schema constants equal
  `nw.campaign.history.v1`, `nw.history.index.v1`, and `nw.history.record.v1`.

**Automated: CLI.** Extend `tests/test_cli_campaign_history.py` with a cfg that sets
`cache_dir` outside the fixture copy.

- AC7: the usage matrix (no args, both `--list` and an id, `--rebuild-cache` with an id,
  with `--list`, with `--project`, with `--state merged`) exits 2. `--rebuild-cache` and
  `--rebuild-cache --json` match the counts from `history_index` and `history_records`.
  Unknown id exits 1. Point `cache_dir` at a file and assert exit 1 and
  `cache: not writable`. Hash the fixture copy across the commands.
- The existing tests keep a cfg with no `cache_dir` and stay green, including
  `test_history_clis_do_not_write`.

**Automated: clone and budgets.** `tests/test_history_scale.py` for the helper,
`tests/test_history_perf.py` for the ceilings.

- AC8 helper: `place_bytes` matches bytes, and with `os.link` raising it copies and the
  destination inode differs. `clone_fixture(dst, campaigns=<fixture campaigns>+1,
  records=<fixture finding lines>+1, seed=1)` appends at least one finding, decision,
  event, mail, and escalation; each appended file's original bytes are a prefix; new ids
  are unique and max-plus-one; the escalation filename equals its `id`; timestamps differ
  by that clone's delta; a second call with the same seed matches bytes. A source row
  that has neither a top-level `campaign` nor `source.campaign` (other than a ledger
  line) raises. `python3 -c` import of `tests.history_scale` does not put `nightwright`
  in `sys.modules`.
- AC8 budgets: `clone_fixture(tmp, campaigns=500, records=50_000)` then the four timed
  calls. Assert `len(history_records(cfg)) >= 50_000` and 500 campaign directories
  before timing. Assert each elapsed time is within the ×3 ceiling in *Budgets*.

**Manual verification.** On a copy of the real data dir, with `cache_dir` pointed at a
temporary directory:

1. `uv run nw campaign history --rebuild-cache`. Expect a TTY progress line and
   `history cache rebuilt: <N> campaigns, <M> records`, exit 0.
2. `uv run nw campaign history --list --json` twice. The second run returns the same
   campaigns and finishes without walking every ledger (confirm with the progress line
   appearing immediately, or with `strace -e openat` showing no `ledger.jsonl` reads for
   a terminal id that was just built).
3. Remove one campaign directory from the copy. Run `--list --json` and expect that id
   absent. Run it again and expect it still absent.
4. Delete the temporary `history/v1` directory and run `--rebuild-cache` again. Expect a
   full build and the same campaign count as step 1 minus the directory removed in step 3.
5. Record the four outcomes in *As shipped*.

**Regression guard.** `tests/test_history.py`, `tests/test_cli_campaign_history.py`, and
`tests/test_history_fixture.py` pass unchanged. Iterate with
`python3 tools/affected_tests.py --run uv run pytest -q` on the history, CLI, and new
test files. `python3 tools/spec_lint.py` exits 0 once the ledger is regenerated. The full
suite stays on the queue's sentinel.

## Clock Log

Implementation sessions clock in and out on IDEA-1015 with `nw idea clock-in IDEA-1015`
and `nw idea clock-out IDEA-1015`.

## Rollout / migration

1. Land `place_bytes` and `clone_fixture` and their unit test. No production behavior
   changes in this step.
2. Land the cache inside `history.py` and the `--rebuild-cache` flag. Existing callers
   that pass no `cache_dir` stay on the pure path. Production `load_config` sets
   `cache_dir`, so the next `nw campaign history` builds the tree lazily.
3. No data-dir migration. Operators may delete `<cache_dir>/history/v1/` at any time.
   The next read rebuilds it.
4. SPEC-0411's cold-build and warm-load behavior reads this tree once this spec is
   `done`. This spec does not edit that spec.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass; manual steps performed and noted in
  *As shipped*.
- [ ] No regressions. The SPEC-0406 history tests pass with their cfgs unchanged.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` regenerated (`python3 tools/spec_lint.py --write-ledger`).

## Open questions

None. These were decided while writing the spec:

| Question | Decision |
|---|---|
| Where does the cache live? | `<cfg["cache_dir"]>/history/v1/`, and nowhere when `cache_dir` is omitted |
| What survives a deleted campaign directory? | The index row and the campaign-local records go; global records that name the id stay |
| How is the abandoned merge failure closed? | `_merge_index_writes` removes `pruned` after it applies writes |
| Are live campaigns served from the file? | No. `now` moves their open visit, so each call recomputes them |
| Who builds the 500-campaign corpus? | `tests/history_scale.clone_fixture`, added here because no spec has shipped it |
| Does the 500 ms budget include popup paint? | No. It is warm `campaign_history` of one stored terminal campaign |
