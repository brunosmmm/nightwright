---
id: SPEC-0401
title: "Dashboard snapshot: nw.dashboard.v1, SnapshotCache and nw dashboard snapshot"
status: done
owner: user
created: 2026-11-03
updated: 2026-11-03
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0400
source_idea: IDEA-874
tags: [agentic, dashboard, remote, web]
depends_on: [SPEC-0222, SPEC-0232]
---

## Context

This is the first slice of child 1 of the epic
[SPEC-0400](./0400-remote-web-dashboard-live-view-of-active-queue.md), promoted from
`IDEA-874`. The human asked for "a live web-based dashboard of active items so that I can
consult this while I am away from home", reachable over Tailscale only.

Review of the first draft found child 1 too large for one campaign (16 acceptance criteria
over three subsystems with separate fixtures: the snapshot, the network server and its auth
gates, and the phone page in headless Chromium). It is split into three specs, in order:

1. **This spec:** the data layer. It defines `nw.dashboard.v1`, the shared
   watermark-driven `SnapshotCache`, the `nw dashboard snapshot` CLI and the epic's read-only
   and no-import invariants. It opens no socket.
2. **Dashboard server** (a later sibling spec): `nw serve --dashboard`, the bind gate,
   token/whois auth, Host/Origin checks, audit, and `GET /api/snapshot` with ETag/304 built on
   this spec's cache.
3. **Dashboard page** (a later sibling spec): the phone-first static UI with its in-page
   self-test, run in headless Chromium.

Everything the server and the page show comes from this spec's builder, so it ships first
and is useful alone: `nw dashboard snapshot` gives the human (or an SSH session from the
phone) the full dashboard view as text or JSON today.

What exists today, checked against the code on 2026-11-03:

- `queue.dashboard_payload` (`src/nightwright/agentic/queue.py:6811`,
  `nw.queue.dashboard.v1`) already carries what `nw queue watch` draws: `lease` (with
  `draining`), `runner_code`, `slots`, `gate_pools`, `active`, `needs_you`,
  `conductor_handling`, `waiting`, `backlog`, `delivered` and `totals.spend_today_usd`. It
  reports ages (`updated_age_s`, `state_age_s`, `park_age_s`, `heartbeat_age_s`,
  `finished_age_s`), not timestamps.
- `events.reader_holder` / `events.followers` (`src/nightwright/agentic/events.py:983`,
  `:1005`) give the conductor follower pid and lag and the steward followers. They only read:
  `reader_holder` takes and immediately releases a non-blocking `flock` on an existing lock
  file and never creates one.
- `events.jsonl` (`events.events_path`, `events.py:190`) is a **projection** of the campaign
  ledgers. Only `events.sync` writes it ("project new ledger entries"), and only followers
  and `conduct_status` call `sync`. A ledger append (cost, state change, park) therefore does
  not touch `events.jsonl` until some follower syncs.
- Each campaign lives in `store.campaign_dir(cfg, cid)` under `store.campaigns_dir(cfg)`
  (`store.py:37`) with an atomically replaced `state.json` and an append-only `ledger.jsonl`
  (`store.py:141`, `:145`).
- `cli_conduct.conduct_status` calls `events.sync`, which writes, and reads
  `read_since(cfg, 0)` over the whole event log. The dashboard must use neither.
  `cli_conduct._herdr_watch_status` is a safe read.
- `escalations.list_all(cfg, "open")` (`src/nightwright/agentic/escalations.py:78`) lists open
  escalations. The cheap `escalations_signature(cfg)` (stat calls only) lives in the TUI
  (`src/nightwright/tui/agentic_model.py:1024`).
- `lease.lease_path(cfg)` (`src/nightwright/agentic/lease.py:31`) is the runner lease file.
- `tests/test_herdr.py:506` (`test_queue_runner_runproc_never_import_herdr`) is the
  import-graph precedent for invariant 2.
- There is no `nightwright.agentic.dashboard` package and no `nw dashboard` command group.

## Goals / Non-goals

**Goals**
- G1. One `nw.dashboard.v1` document, built only from the existing payload builders, with a
  `rev` hash that ignores age-only movement.
- G2. A shared `SnapshotCache` that does no work without callers, checks a cheap watermark at
  most once per `refresh_s`, and rebuilds only when the watermark moved or `max_stale_s`
  passed. The watermark sees ledger and state changes directly, without waiting for a
  follower to sync `events.jsonl`.
- G3. `nw dashboard snapshot [--json]` prints the snapshot from the CLI, with no server,
  no tailscale and no token.
- G4. The epic's read-only invariant (1) and no-import invariant (2) are pinned by tests for
  the builder and the CLI.
- G5. `escalations_signature` moves out of the TUI into `escalations.signature`, so the TUI
  and the dashboard share one implementation.

**Non-goals**
- `nw serve --dashboard`: the `serve_cmd` mode switch (`--jobs`/`--dashboard` exclusive,
  per-mode bind default, TLS flags refused after the bind gate), `auth.py` (`run_tailscale`,
  `own_tailnet_ips`, `resolve_bind`, `check_bind`, `local_token`, the whois `Gate` with its
  60 s / 5 s cache, `check_host_origin`), `audit.py`, `server.py` (routes, headers,
  `GET /api/snapshot` with ETag/304, `server.json`), the fake `tailscale` fixture, and the
  e2e, bind-CLI, whois-e2e and over-HTTP read-only tests. That is the **dashboard server**
  sibling spec, which depends on this one.
- The phone page: `static/index.html`, `app.js`, `app.css` (sections, status dot, 15 s
  conditional poll, `visibilitychange`, `textContent`-only rendering, token fragment to
  `localStorage`, 401/403/stale banners, `?selftest=1`), the CSP and no-external-URL static
  checks, the headless Chromium test at 390×844 and 360×740, and the epic's manual steps 1–2.
  That is the **dashboard page** sibling spec, which depends on the server spec.
- Answering escalations and the full inbox entry (options, recommendation, facts, findings,
  unmet ACs, effects, host fixes for parks): epic child 2. This spec ships only the summary
  fields of each open escalation and the human-owned parks.
- SSE, a background snapshotter thread, `max_clients`, the campaign drilldown and the run
  tail: epic child 3. Child 3 puts a thread in front of this spec's cache.
- The systemd unit, `nw dashboard status|unit` and the `conduct-status` `dashboard` key:
  epic child 4.
- TLS, `tailscale cert`, service worker and push: epic child 5.
- Any change to `queue.dashboard_payload`'s output or to `nw queue watch`.

## Decision

1. **The snapshot reuses the existing builders.** `snapshot.build` calls
   `queue.dashboard_payload`, `events.reader_holder` / `events.followers`,
   `cli_conduct._herdr_watch_status` and `escalations.list_all(cfg, "open")`. It never calls
   `events.sync`, `events.follow`, `read_since(cfg, 0)`, `conduct_status`,
   `store.write_state`, any `mailbox` write or any queue mutation. If a builder turns out to
   write, the fix is a read-only path in that builder, not a fork in the dashboard.
2. **Ages stay ages; `rev` ignores them.** `dashboard_payload` reports `*_age_s`. Rather than
   fork it into timestamps, the snapshot keeps them and carries `generated_at`, from which a
   client derives `age_s + (now − generated_at)`. `rev` is the first 16 hex chars of the
   sha256 of the canonical JSON (`sort_keys=True`, `separators=(",", ":")`) of the snapshot
   with `generated_at`, `rev` and every key ending in `_age_s` removed, at any depth. A
   rebuild that only moved ages keeps the same `rev`. The server spec uses `rev` as the strong
   ETag.
3. **The watermark sees the campaign records, not only the projection.** `watermark(cfg)` is
   a tuple of stat calls only:
   - `(size, mtime_ns)` of `events.jsonl`;
   - `escalations.signature(cfg)`;
   - the `mtime_ns` of the lease file;
   - a campaign signature: sorted `(campaign_id, file_name, size, mtime_ns)` for the
     `state.json` and `ledger.jsonl` of every directory under `store.campaigns_dir(cfg)`, in
     the style of `escalations.signature`.

   Missing files count as `None`. A ledger append or state write therefore moves the
   watermark immediately, even when no follower has synced `events.jsonl`. The cost is one
   `stat` per campaign file per check, at most once per `refresh_s`, which is negligible next
   to `dashboard_payload` itself. Terminal campaigns stay in the scan: they are few per day,
   and `delivered` reads them.
4. **The cache is lazy and shared** (`snapshot.SnapshotCache`). It has no thread. On `get()`
   it checks the watermark at most once per `refresh_s` (default 5 s). It rebuilds only when
   the watermark moved or the cached snapshot is older than `max_stale_s` (default 30 s). One
   lock makes concurrent callers wait for a single build. With no `get()` it does no queue
   reads and no stat calls at all.
5. **One failing section does not fail the snapshot.** Each builder call is wrapped, and a
   section whose builder raises becomes `{"error": "<ExceptionType>: <message>"}`. The rest
   still builds.
6. **`nw dashboard snapshot [--json]`** builds the snapshot in-process with `snapshot.build`
   and prints it as JSON (`--json`) or as a short text summary. It needs no server, no
   tailscale and no token, and it writes nothing (it does not even create
   `agentic/dashboard/`).
7. **`escalations_signature` moves** unchanged from `tui/agentic_model.py` into
   `escalations.py` as `signature(cfg)`. The TUI calls it from there. One implementation.

## Design

### Modules

New package `src/nightwright/agentic/dashboard/`:

- `__init__.py`:
  - `settings(cfg) -> dict` reads `agentic.dashboard` with the defaults
    `{bind: "tailscale:8791", allowed_logins: [], refresh_s: 5, max_stale_s: 30}`. Malformed
    values fall back to the default per key, like `escalations.settings`
    (`escalations.py:30`). `refresh_s` and `max_stale_s` must be positive numbers. Keys of
    later children (`max_clients`, `tail_poll_s`, `push`) are accepted and ignored. `bind`
    and `allowed_logins` are parsed here so the server spec does not re-read config, but
    nothing in this spec uses them.
  - `dashboard_dir(cfg) -> Path` returns `<data_dir>/agentic/dashboard/` and creates it with
    mode 0700. Nothing in this spec calls it; the server spec does. It lives here so the
    read-only test can name the one directory the dashboard may write.
- `snapshot.py`:
  - `watermark(cfg) -> tuple`, as in Decision 3.
  - `build(cfg, now=None) -> dict`: one `nw.dashboard.v1` document (schema below), including
    `rev`.
  - `compute_rev(doc) -> str`, as in Decision 2.
  - `RECENT_TYPES`: `merged`, `handed_off`, `abandoned`, `run_failed`, `parked`, `resumed`,
    `escalation`, `escalation_answered`, `escalation_withdrawn`, `runner_dead`,
    `lease_stale`, `cost_warning`, `worker_failed`. A test asserts it is a subset of
    `events.EVENT_TYPES`; any name the implementation finds missing from `EVENT_TYPES` is
    dropped from the list, not added to `EVENT_TYPES`.
  - `recent_events(cfg, n=20)`: opens `events.jsonl`, seeks to `max(0, size − 256 KiB)`,
    drops the first partial line when the seek was not to 0, parses the remaining lines
    (skipping unparsable ones), keeps `RECENT_TYPES` and returns the last `n`.
  - `class SnapshotCache(cfg, clock=time.monotonic, builder=build)`: `get() -> dict`, plus
    `builds` and `watermark_checks` counters for tests.
- `src/nightwright/agentic/cli_dashboard.py`: the `nw dashboard` click group with
  `snapshot [--json]`, registered on the root CLI next to the other agentic groups. Child 4
  adds `status` and `unit`.
- `escalations.signature(cfg)`: moved from `tui/agentic_model.escalations_signature`. The TUI
  imports and calls it; the old name is removed from the TUI module and its callers are
  updated.

### `nw.dashboard.v1`

```
{schema: "nw.dashboard.v1", generated_at, rev,
 health: {runner: {lease, runner_code, draining},          # dp.lease, dp.runner_code, lease.draining
          conductor: {pid, lag, cursor_age_s},             # reader_holder / followers row "conductor"
          stewards: [{address, pid, lag}],                 # followers kind == "mailbox", held
          herdr_watch},                                    # cli_conduct._herdr_watch_status
 totals,                                                   # dp.totals (spend_today_usd as-is)
 slots, gate_pools,                                        # dp
 active, needs_you, conductor_handling, waiting,           # dp, unchanged rows
 backlog: {count, top: [≤5 dp.backlog rows]},
 delivered,                                                # dp
 recent_events: [≤20 {id, ts, type, campaign, spec_id, project, by, summary}],
 inbox: {escalations: [{id, created, by, kind, question, campaign, spec_id, project}],
         parks: [needs_you rows whose park reason is main_dirty or main_not_on_base]}}
```

`dp` is `queue.dashboard_payload(cfg, now=now)`, called once per build. `generated_at` is an
ISO-8601 UTC timestamp. `summary` is `"<type> <campaign label>"` followed by the event's
`data.reason` or `data.state` when present; it needs no text helper from another module.
`question` is the first line of the escalation's question. Child 2 extends each
`inbox.escalations` entry with options, recommendation, facts, findings, unmet ACs and
effects, and the parks with their host fix.

### `SnapshotCache.get()`

```
with lock:
    t = clock()
    if cached is None:                         -> build, remember (t, watermark)
    elif t - built_at >= max_stale_s:          -> build
    elif t - last_check >= refresh_s:
        last_check = t; watermark_checks += 1
        if watermark(cfg) != cached_wm:        -> build
    return cached
```

A build records `cached_wm = watermark(cfg)` taken **before** calling the builder, so a
change that lands during a build is seen at the next check rather than lost. The first build
counts one watermark check. Callers get the same dict object; they must not mutate it (the
server serializes it once per `rev`).

### `nw dashboard snapshot`

- `--json`: `json.dumps(build(cfg), indent=2)` on stdout, exit 0.
- Text form, one line each: an overall line (`runner <holder> heartbeat <age>s` plus
  `draining` / `stale code` when set; `conductor <pid|down>`), a counts line (`active N ·
  queued N · parked N · waiting N · backlog N · conductor-handling N`), `spend today $X.XX`,
  then one line per open escalation (`ESC-NNNN <campaign> <question>`) and per human-owned
  park, or `nothing needs you`.
- A section that came back as `{"error": …}` prints `<section>: error: <message>` and the
  command still exits 0; the build itself raising (no config, no data dir) exits 1 with the
  message.

## Alternatives considered

- **Watermark on `events.jsonl` only** (the first draft). `events.jsonl` moves only when a
  follower runs `events.sync`, and the dashboard must not call it. With the conductor down,
  which is exactly when the dashboard matters, a park or cost change would show only after
  `max_stale_s`. Statting the campaign records costs a few hundred `stat` calls at most every
  5 s.
- **Hashing the files' contents for the watermark.** Reads every ledger on every check; the
  stat tuple is enough because ledgers are append-only and `state.json` is replaced
  atomically (new mtime and usually new size).
- **Converting ages to timestamps in the snapshot.** `now − age` jitters by up to a second
  between builds, which would change `rev` on every rebuild. Excluding `*_age_s` from the
  hash and deriving display ages from `generated_at` gives a stable `rev` without forking the
  builder.
- **A snapshotter thread now.** Polling at 15 s does not need one, and a lazy cache already
  guarantees zero work with zero callers. Child 3 adds the thread when SSE needs a push
  source.
- **Building from `conduct_status`.** It calls `events.sync` (writes) and reads the whole
  event log. Rejected by the epic's invariant 1.
- **Shipping the server and page in this spec too.** The first draft did; review counted 16
  acceptance criteria over three subsystems with separate fixtures. Split as described in
  Context.

## Acceptance criteria

- [x] `snapshot.build` on the seeded fixture returns a document with every top-level key of
      the `nw.dashboard.v1` schema above and `schema == "nw.dashboard.v1"`. Its
      `totals.spend_today_usd` equals `queue.dashboard_payload`'s for the same data, its
      `inbox.escalations` lists exactly the open escalations, and its `inbox.parks` lists
      exactly the `main_dirty` / `main_not_on_base` parks. When one builder raises, that
      section is `{"error": …}` and every other section is still present and correct.
- [x] `rev` is unchanged between two builds whose inputs differ only in `*_age_s` values and
      `generated_at`, and changes when a campaign's cost changes.
- [x] `watermark(cfg)` includes the campaign signature of Decision 3: appending one line to a
      campaign's `ledger.jsonl` with no `events.jsonl` change moves it, and so does replacing
      a `state.json`, adding a campaign directory, writing an escalation record or touching
      the lease file. Two calls with no file change return equal tuples.
- [x] `SnapshotCache` (injected clock) performs 0 builds and 0 watermark checks with no
      `get()`. 20 threads × 10 `get()` within one `refresh_s` and an unchanged watermark
      cause exactly 1 build. A watermark move before `refresh_s` elapses is not seen; after
      `refresh_s` it causes a rebuild. With no change, `max_stale_s` causes a rebuild that
      yields the same `rev`.
- [x] At the cache level, a ledger-only change (one `ledger.jsonl` append that changes a
      campaign's cost or state, no `events.jsonl` line), followed by advancing the clock past
      `refresh_s`, makes `get()` return a snapshot with a new `rev`.
- [x] `recent_events` never reads before offset `size − 256 KiB` of `events.jsonl`, returns at
      most 20 events, all of `RECENT_TYPES`, in file order; `RECENT_TYPES` is a subset of
      `events.EVENT_TYPES`.
- [x] `nw dashboard snapshot --json` prints a document that passes the same schema-key check,
      with no server running, no `tailscale` on `PATH` and no token. Without `--json` it
      prints the text summary, including the `spend today` and counts lines.
- [x] Running `build` and `SnapshotCache.get()` repeatedly with rebuilds forced, and running
      `nw dashboard snapshot` (both forms), leaves every file under the data dir
      byte-identical and creates no file or directory (including `agentic/dashboard/`).
- [x] No module among `agentic/queue.py`, the runner, `runproc`, `events`, `escalations`,
      `lease`, `store` and `mailbox` imports `nightwright.agentic.dashboard`, directly or
      transitively.
- [x] `escalations.signature` exists with the old behaviour, `tui/agentic_model.py` no longer
      defines `escalations_signature`, and the TUI escalation inbox tests (SPEC-0250) pass
      unchanged.

## Test plan

**Automated tests.** Waits use `tests/waits.py` (`NW_TEST_WAIT_S`), never fixed sleeps.

- `tests/fixtures/dashboard_data.py`: seeds a temp `NW_DATA_DIR` with one active campaign
  (with cost and cap), a PARKED `human_decision` campaign with an open `ESC-0001`, a
  `main_dirty` park, a delivered (merged) campaign, a lease file and an `events.jsonl` larger
  than 256 KiB whose tail holds the recent events. The server and page specs reuse it.
- `tests/test_dashboard_snapshot.py` (unit):
  - `build` on the fixture has every schema key; `totals.spend_today_usd` equals
    `dashboard_payload`'s; the inbox escalations and parks sets are exact;
  - a monkeypatched `escalations.list_all` that raises yields `inbox.escalations ==
    {"error": …}` and the other sections are intact;
  - `rev`: two builds with `dashboard_payload` patched to differ only in `*_age_s` (nested)
    and different `now` → same `rev`; a ledger cost change → different `rev`;
  - `watermark`: equal on repeated calls; moved by a `ledger.jsonl` append with `events.jsonl`
    untouched (the reviewer's case, named `test_ledger_append_without_events_moves_watermark`),
    by a `state.json` replace, a new campaign dir, a new `ESC-*.json` and a lease touch;
  - `recent_events`: a wrapped `open` records every `seek` offset, all ≥ size − 256 KiB; at
    most 20 results, all in `RECENT_TYPES`, in order; a garbage line is skipped;
    `RECENT_TYPES <= events.EVENT_TYPES`;
  - `settings`: defaults, a malformed `refresh_s` falls back per key, unknown later-child keys
    are ignored.
- `tests/test_dashboard_cache.py` (unit, injected clock and counting builder):
  - no `get()` → `builds == 0` and `watermark_checks == 0`;
  - 20 threads × 10 `get()` at a fixed clock → `builds == 1`;
  - watermark moved, clock < `refresh_s` → cached; clock past `refresh_s` → rebuild;
  - nothing moved, clock past `max_stale_s` → rebuild with the same `rev`;
  - the ledger-only case on the real fixture and real `build`: append a ledger event that
    changes the active campaign's cost, write no `events.jsonl` line, advance past
    `refresh_s` → new `rev` (`test_ledger_only_change_yields_new_rev`).
- `tests/test_dashboard_cli.py` (real subprocess, `uv run nw dashboard snapshot`, with
  `NW_DATA_DIR` set to the fixture and `PATH` stripped of any `tailscale`): `--json` passes
  the schema-key check; the text form contains `spend today` and the counts line and lists
  `ESC-0001`.
- `tests/test_dashboard_readonly.py`: hash every file and list every directory under the
  seeded data dir; run `build` 50 times and `SnapshotCache.get()` with `refresh_s` /
  `max_stale_s` near zero under a stepping clock so every call rebuilds; run `nw dashboard
  snapshot` and `nw dashboard snapshot --json` as subprocesses; re-hash and re-list:
  identical, and `agentic/dashboard/` does not exist. The server spec re-runs the same check
  over HTTP.
- `tests/test_dashboard_imports.py`: in the style of
  `test_queue_runner_runproc_never_import_herdr`, import each listed queue-side module in a
  fresh interpreter and assert `nightwright.agentic.dashboard` is not in `sys.modules`; also
  assert it statically with `ast` over those modules' sources.
- `tests/test_escalations.py` (extended): `escalations.signature` is sorted
  `(name, size, mtime_ns)` of `ESC-*.json` and changes on a record write; `tui.agentic_model`
  has no `escalations_signature` attribute.
- Regression guard: the TUI escalation inbox tests (SPEC-0250), the `nw queue watch` /
  `dashboard_payload` tests, and the escalations tests run unchanged.

**Manual verification (on this host, recorded in this spec's As shipped).**
1. `uv run nw dashboard snapshot` against the real data dir: the health, counts, spend and
   needs-you lines match what `uv run nw queue watch` shows at the same moment.
2. `touch /tmp/nw-mark && uv run nw dashboard snapshot --json | python3 -m json.tool` succeeds,
   and `find <data_dir> -newer /tmp/nw-mark` afterwards lists nothing written by the command
   (files a live runner or follower touched in the meantime are told apart by their paths).

**Regression guard.** Affected tests via `python3 tools/affected_tests.py --run uv run pytest
-q`. The queue's close gate runs the affected set.

### Test record (2026-11-03)

- `uv run pytest -q -n 0 tests/test_dashboard_snapshot.py tests/test_dashboard_cache.py
  tests/test_dashboard_cli.py tests/test_dashboard_readonly.py tests/test_dashboard_imports.py`:
  26 passed (this includes `test_ledger_append_without_events_moves_watermark` and
  `test_ledger_only_change_yields_new_rev`).
- `uv run pytest -q -n 4 tests/test_agentic_escalations.py tests/test_tui_escalations_model.py
  tests/test_tui_escalations_tab.py tests/test_agentic_dashboard.py tests/test_tui_mailbox_tab.py
  tests/test_herdr.py`: 240 passed, 2 skipped (TUI escalation inbox, escalations, `dashboard_payload`
  and the herdr import precedent).
- `python3 tools/affected_tests.py --run uv run pytest -q -x -n 4` (121 test files): the first
  pass had 2462 passed and 1 failure, `test_export.py::test_internal_spec_lint_unaffected_by_outbox`,
  which failed only because the ledger was stale after the status edit. The run after
  `--write-ledger` is recorded below. The full suite is left to gating.
- After `python3 tools/spec_lint.py --write-ledger`, `python3 tools/affected_tests.py --run uv run
  pytest -q -n 4` gave 3122 passed and 4 skipped. `python3 tools/spec_lint.py` exits 0.
- Manual 1 (real data dir, 2026-11-03 20:47): `nw dashboard snapshot` printed `runner 1415509@…
  heartbeat 14s · conductor 1551100`, `active 7 · queued 0 · parked 4 · waiting 0 · backlog 1 ·
  conductor-handling 4` and `spend today $124.87`. `nw queue watch --once` at the same moment
  showed runner pid 1415509 with a 14s heartbeat, 7 active, 0 waiting, needs you 0, conductor 4,
  1 backlog and today $124.87. They match.
- Manual 2: `nw dashboard snapshot --json | python3 -m json.tool` succeeded. Afterwards
  `find $NW_DATA_DIR -newer /tmp/nw-mark` listed only `agentic/events.projector.json` and the
  `agentic/` directory, which the live conductor follower's `events.sync` rewrites. The command
  calls no `sync`, and the fixture read-only test, where no follower runs, finds no change at
  all. `agentic/dashboard/` does not exist.

### Test record (2026-11-03, loop 2: review fixes)

- `uv run pytest -q -n 0 tests/test_dashboard_snapshot.py tests/test_dashboard_cache.py
  tests/test_dashboard_cli.py tests/test_dashboard_readonly.py tests/test_dashboard_imports.py
  tests/test_agentic_followers.py`: 46 passed before the estimated-percent test was added. New
  tests include `test_concurrent_callers_share_one_build` (the first build blocks until the other
  19 callers wait on an instrumented lock), `test_watermark_is_taken_before_the_build`,
  `test_a_failing_build_keeps_the_move_visible`, `test_max_stale_real_build_keeps_the_rev`,
  `test_health_maps_conductor_and_held_stewards`, `test_health_rows_from_patched_followers`,
  `test_failing_followers_stewards_and_herdr_are_error_sections`,
  `test_rev_ignores_runner_heartbeat_and_tick_stamps`,
  `test_rev_ignores_estimated_gate_percent_only`,
  `test_follower_gate_mailbox_and_herdr_files_move_watermark`,
  `test_a_dead_follower_moves_watermark_without_any_write`, the `recent_events` missing-log,
  small-log and window-boundary tests, the `text_lines` branch tests, and in
  `tests/test_agentic_followers.py` `test_followers_lag_from_last_id_reads_no_log` and
  `test_reader_lock_retries_a_probe_and_still_refuses_a_holder`.
- Mutation checks: replacing the cache lock with a `nullcontext` fails the concurrency test;
  storing the watermark before calling the builder fails
  `test_a_failing_build_keeps_the_move_visible`; dropping the `held` filter in `_stewards` fails
  both health tests.
- `uv run pytest -q -n 4` over the dashboard tests plus `test_agentic_followers`,
  `test_agentic_events`, `test_agentic_mailbox`, `test_agentic_steward_notify`,
  `test_tui_events_tab`, `test_tui_events_model`, `test_herdr`, `test_agentic_escalations`,
  `test_tui_escalations_model` and `test_tui_escalations_tab`: 352 passed, 2 skipped.
- Live data dir (read-only builds): four `snapshot.build` calls 8 s apart while the runner ticked
  (`tick_at` moved) and three gates showed estimated progress gave the same `rev` each time.
  Before this fix, the same check gave four different `rev`s.
- `python3 tools/affected_tests.py --run uv run pytest -q -n 6`: 4053 passed, 4 skipped.
  `python3 tools/spec_lint.py` exits 0. The full suite is left to gating.

### Test record (2026-11-03, loop 3: review fixes)

- New tests: `test_unreadable_pid_files_do_not_fail_the_watermark`,
  `test_pid_alive_is_the_real_kill_probe`, `test_build_without_a_data_dir_raises` and the
  inf/nan cases in `test_settings_defaults_fallback_and_later_keys`
  (`tests/test_dashboard_snapshot.py`); `test_a_failing_watermark_never_fails_get`
  (`tests/test_dashboard_cache.py`); `test_a_config_without_a_data_dir_exits_1` and
  `test_text_prints_every_failed_section` (`tests/test_dashboard_cli.py`);
  `test_reader_holder_ignores_another_probe_on_a_dead_follower`
  (`tests/test_agentic_followers.py`). `test_a_dead_follower_moves_watermark_without_any_write`
  now controls liveness through a patched `gate_slots._pid_alive`, so it no longer depends on
  a real pid that could be reused.
- `uv run pytest -q -n 0` over the five dashboard test files plus `test_agentic_followers` and
  `test_agentic_events`: 91 passed.
- Mutation checks: catching only `OSError` in `_pid_of` fails the unreadable-pid test; making
  `reader_holder` return on the first busy probe fails the concurrent-probe test.
- `python3 tools/affected_tests.py --run uv run pytest -q -n 6`: the first run stopped when an
  xdist worker crashed in `tests/test_org_write_race.py::test_concurrent_processes_lose_nothing_and_never_reuse_an_id`.
  The previous gate also recorded that test as flaky, and it does not touch the dashboard.
  `tests/test_org_write_race.py` alone: 9 passed. The affected set with that one test
  deselected: 4215 passed, 5 skipped. The full suite is left to gating.

### As shipped (2026-11-03)

- **`rev` also ignores clock-derived keys without the `_age_s` suffix.** `dashboard_payload`
  derives `age_s` (queue rows and backlog), `elapsed_s` (run activity), `held_s` (gate hold),
  `waiting_s` and `waited_s` (journey) from "now". If they stayed in the hash, every rebuild would
  change `rev`. `snapshot.NOW_RELATIVE_KEYS` drops them from the hash in the same way as
  `*_age_s`, which is what Decision 2 intends.
- **`rev` ignores the runner's clock stamps and estimated gate percents (review, loop 2).** A live
  runner rewrites `heartbeat_at`, `tick_at`, `ticks`, `last_tick_s` and the other loop stamps in
  the lease on every heartbeat and tick, so the first build changed `rev` on every tick.
  `snapshot.LEASE_CLOCK_KEYS` (`heartbeat_at`, `tick_at`, `slot_freed_at`, `busy_ended_at`,
  `slots_full_at`, `progress_at`, `last_tick_s`, `ticks`) are dropped from
  `health.runner.lease` before hashing; `held`, `pid`, `stale`, `draining`, `busy`, `in_flight`
  and the rest still count. A gate progress `{percent, estimated: true}` is elapsed time over the
  previous gate's duration (`gate_progress.estimate`), so its `percent` is dropped too; a live
  (`estimated: false`) percent is real progress and still counts. On the live data dir, with a
  runner ticking and three gates running, four builds 8 s apart (tick_at moved) gave one `rev`.
- **The watermark is `(events.jsonl (size, mtime_ns), escalations.signature, lease mtime_ns,
  campaigns_signature, (cursors tree, gate-slots tree, mailbox tree, herdr state stat,
  liveness_signature))`.** `snapshot.campaigns_signature(cfg)` gives sorted `(campaign_id,
  file_name, size, mtime_ns)` for each campaign's `state.json` and `ledger.jsonl`, with
  `(None, None)` for a missing file. The fifth element (added after review) covers what `health`
  and `gate_pools` show: `(name, size, mtime_ns)` of the files under `agentic/cursors/`,
  `agentic/gate-slots/` (and its `w-*` pools) and `agentic/mailbox/`, the stat of the herdr watch
  state file, and `liveness_signature(cfg)`: sorted `(file, pid, alive)` for the pid in the lease,
  each follower `.pid`, each gate-slot holder and waiter and the herdr state. A process that dies
  releases its flock and writes nothing, so its pid's liveness (`kill(pid, 0)`) is what moves the
  watermark; a dead conductor follower is seen at the next check, not after `max_stale_s`. This
  departs from Decision 3's "stat calls only": it also reads those few small pid files and calls
  `kill(pid, 0)`. It still takes no lock and writes nothing. The lease mtime stays in, so a live
  runner's heartbeat causes a rebuild once per `refresh_s` while `rev` stays stable.
- **Cache:** `SnapshotCache(cfg, clock, builder, *, refresh_s=None, max_stale_s=None,
  watermark_fn=watermark)`. The keyword-only overrides exist for tests; `None` reads `settings`.
  When a watermark check finds a move, it reuses that watermark for the rebuild instead of
  statting twice. The watermark is still taken before the build, but it is stored together with
  the snapshot only after the builder returns, so a builder that raises leaves the old pair and
  the next check still sees the move.
- **Followers without a full log read.** `events.followers` gained `last_id=` /
  `lag_from_last_id=` (default off, so `conduct-status` and the TUI are unchanged). The snapshot
  passes the last id of the 256 KiB tail it already reads, and an events reader's lag is
  `last_id - after` (event ids are contiguous) instead of `len(read_since(cfg, after))`, which
  read the whole log for a stale cursor. `recent_events` and the lag share one tail read per
  build. `health.conductor` uses the conductor's `followers` row (one lock probe, not two) and
  probes `reader_holder` only when the conductor has no cursor file. `health.stewards` goes
  through `_safe` like every other section.
- **Lock probes no longer refuse a starting follower.** `reader_holder` takes and drops the
  reader's flock for an instant, and `reader_lock` used to refuse at once on `BlockingIOError`,
  so a follower starting during any probe (TUI, `conduct-status`, now the dashboard) exited with
  "already followed". `events.reader_lock` now retries for up to `READER_LOCK_TRIES` ×
  `READER_LOCK_RETRY_S` (10 × 20 ms) before refusing; a real holder is still refused.
- **`recent_events` keeps a whole line at the window start.** It no longer drops the first line
  of the window blindly. Every line is parsed, and when the seek was not to 0 the first line is
  kept only when it is a whole event record (`schema == nw.events.v1` and an int `id`). A line
  that starts exactly at `size − 256 KiB` is kept, and a cut one is skipped. It still never reads
  before that offset.
- **Text form:** the counts line uses `dashboard_payload`'s totals as follows: `queued` = `ready`,
  `parked` = `attention` (human plus conductor), `backlog` = `backlog`. A human-owned park prints
  as `park <label> <campaign> <reason>: <detail>`, or without `: <detail>` when there is no
  detail; the detail is printed verbatim (no trailing-character strip). A failed section prints
  `<section>: error: <message>` and the command exits 0. A failed build exits 1.
- **Test files:** the escalation signature tests went into the existing
  `tests/test_agentic_escalations.py`, because there is no `tests/test_escalations.py`. The CLI
  and read-only tests run the venv's `nw` entry point (`<venv>/bin/nw`, which is what `uv run nw`
  executes) as a real subprocess, so a nested `uv run` cannot sync or lock inside the gate
  sandbox.
- **Fixture:** besides the spec's list, `tests/fixtures/dashboard_data.py` seeds an answered
  `ESC-0002` and a withdrawn `ESC-0003` (left out of `inbox.escalations`) and a
  `main_not_on_base` park (`SPEC-0905`), so both `HUMAN_PARKS` reasons and the open-only filter
  are exercised.
- **TUI tests:** `tests/test_tui_escalations_model.py` and `tests/test_tui_escalations_tab.py`
  called `AM.escalations_signature`. They now call `ESC.signature`, and their assertions are
  unchanged. The rename forces this edit, because the AC also requires that the TUI module no
  longer defines the old name. `tui/mailbox_tab.py` calls `ESC.signature`.

### As shipped (2026-11-03, loop 3: review fixes)

- **The watermark never fails the cache.** `_pid_of` treats a file that is not valid UTF-8 as
  holding no pid. If `watermark` still raises, `SnapshotCache` counts that check as a move. It
  then rebuilds at most once per `refresh_s` and keeps returning a snapshot, and `get()` never
  raises from the watermark.
- **`reader_holder` tells another prober from a live follower.** When the lock is busy, the
  pid in `.pid` decides the result. If that pid is alive, `reader_holder` returns it at once.
  Otherwise the holder may be another probe holding the lock for an instant (the TUI,
  `conduct-status`, Supervisor, the dashboard), so it retries like `reader_lock` (up to
  `READER_LOCK_TRIES` × `READER_LOCK_RETRY_S`) before reporting the lock as held. If the lock
  is still busy after the retries, it returns the recorded pid, or -1 when there is none, as
  before.
- **The build needs a data dir.** `snapshot.build` raises when the config has no `data_dir` or
  the directory does not exist. `nw dashboard snapshot` then exits 1 with the message instead
  of printing a document of error sections. It still creates nothing.
- **Text form:** it also prints `health.stewards: error: …`, `health.herdr_watch: error: …`,
  `needs_you: error: …` and `conductor_handling: error: …`.
- **Settings:** `refresh_s` and `max_stale_s` must be finite, so `.inf` and `.nan` fall back to
  the defaults.

## Rollout / migration

1. Land the `nightwright.agentic.dashboard` package (`__init__`, `snapshot`), `cli_dashboard`,
   `escalations.signature` (TUI delegates) and the tests in one change. No data migration:
   nothing is written.
2. The dashboard server spec then adds `nw serve --dashboard` on top of `SnapshotCache`, and
   the dashboard page spec adds the static UI. Until then the human uses `nw dashboard
   snapshot`.
3. The `nw-orient` skill and the WORKFLOW "Remote dashboard" section come with epic child 4.
   This spec's `--help` text describes `nw dashboard snapshot`.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; the new tests and the affected tests pass; manual steps 1 and 2
      performed and noted.
- [x] No regressions (TUI escalation, escalations and `dashboard_payload` tests green).
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` regenerated; `python3 tools/spec_lint.py` exits 0.

## Open questions

_None._
