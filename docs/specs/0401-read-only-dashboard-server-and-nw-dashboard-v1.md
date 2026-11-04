---
id: SPEC-0401
title: "Dashboard snapshot: nw.dashboard.v1, SnapshotCache and nw dashboard snapshot"
status: proposed
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

- [ ] `snapshot.build` on the seeded fixture returns a document with every top-level key of
      the `nw.dashboard.v1` schema above and `schema == "nw.dashboard.v1"`. Its
      `totals.spend_today_usd` equals `queue.dashboard_payload`'s for the same data, its
      `inbox.escalations` lists exactly the open escalations, and its `inbox.parks` lists
      exactly the `main_dirty` / `main_not_on_base` parks. When one builder raises, that
      section is `{"error": …}` and every other section is still present and correct.
- [ ] `rev` is unchanged between two builds whose inputs differ only in `*_age_s` values and
      `generated_at`, and changes when a campaign's cost changes.
- [ ] `watermark(cfg)` includes the campaign signature of Decision 3: appending one line to a
      campaign's `ledger.jsonl` with no `events.jsonl` change moves it, and so does replacing
      a `state.json`, adding a campaign directory, writing an escalation record or touching
      the lease file. Two calls with no file change return equal tuples.
- [ ] `SnapshotCache` (injected clock) performs 0 builds and 0 watermark checks with no
      `get()`. 20 threads × 10 `get()` within one `refresh_s` and an unchanged watermark
      cause exactly 1 build. A watermark move before `refresh_s` elapses is not seen; after
      `refresh_s` it causes a rebuild. With no change, `max_stale_s` causes a rebuild that
      yields the same `rev`.
- [ ] At the cache level, a ledger-only change (one `ledger.jsonl` append that changes a
      campaign's cost or state, no `events.jsonl` line), followed by advancing the clock past
      `refresh_s`, makes `get()` return a snapshot with a new `rev`.
- [ ] `recent_events` never reads before offset `size − 256 KiB` of `events.jsonl`, returns at
      most 20 events, all of `RECENT_TYPES`, in file order; `RECENT_TYPES` is a subset of
      `events.EVENT_TYPES`.
- [ ] `nw dashboard snapshot --json` prints a document that passes the same schema-key check,
      with no server running, no `tailscale` on `PATH` and no token. Without `--json` it
      prints the text summary, including the `spend today` and counts lines.
- [ ] Running `build` and `SnapshotCache.get()` repeatedly with rebuilds forced, and running
      `nw dashboard snapshot` (both forms), leaves every file under the data dir
      byte-identical and creates no file or directory (including `agentic/dashboard/`).
- [ ] No module among `agentic/queue.py`, the runner, `runproc`, `events`, `escalations`,
      `lease`, `store` and `mailbox` imports `nightwright.agentic.dashboard`, directly or
      transitively.
- [ ] `escalations.signature` exists with the old behaviour, `tui/agentic_model.py` no longer
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

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; the new tests and the affected tests pass; manual steps 1 and 2
      performed and noted.
- [ ] No regressions (TUI escalation, escalations and `dashboard_payload` tests green).
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` regenerated; `python3 tools/spec_lint.py` exits 0.

## Open questions

_None._
