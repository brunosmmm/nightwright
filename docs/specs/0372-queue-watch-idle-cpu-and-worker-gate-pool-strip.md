---
id: SPEC-0372
title: "queue watch: near-zero idle CPU, and a workers / gate-pool strip"
status: done
owner: user
created: 2026-11-02
updated: 2026-11-02
milestone: "M9: agentic workflow"
kind: feature
tags: [agentic, queue, dashboard, performance, workers]
depends_on: [SPEC-0326, SPEC-0351]
---

## Context

A long-running `nw queue watch` burned about 65% of a core while idle. Profiling against a copy
of a real data dir (208 campaigns) put one `dashboard_payload` at about 5 s of CPU, refreshed
every 2 s: `resolve_profile` re-read and re-parsed the builtin profile YAML (pure-Python
PyYAML) once per campaign per refresh (about 15 s under cProfile), and `list_campaigns`
re-parsed every `state.json` six times per refresh. The watch is a Rich `Live` loop (not
Textual). It also ran Rich's 4 Hz auto-refresh thread over a static frame.

Separately, with remote gate workers (SPEC-0337/0343) the dashboard did not show the workers or
which campaigns gate remotely, locally, or wait for a slot.

## Goals / Non-goals

**Goals**
- Idle watch CPU stays low; unchanged data costs a few hundred stats, not a rebuild.
- A slim workers strip: per worker name, on/off/cooldown, slots held n/N; local pool n/N;
  per GATING campaign marker: remote (worker name), local, or waiting.

**Non-goals**
- Reachability probes (those shell out to ssh); the dot shows registry state only.
- Changing the layout when no worker is registered.

## Decision

1. Cache by file signature: `profiles._read_dir` (per directory, keyed on name/mtime_ns/size of
   its files) and `store.list_campaigns` state parsing (per `state.json` on mtime_ns/size).
   Both hand out private copies (pickle round trip), so callers may mutate.
2. The watch loop sleeps `--interval`, takes a stat fingerprint of the files the dashboard
   derives from (`_watch_fingerprint`), and rebuilds the payload only when it changed or
   `IDLE_REFRESH_S` (15 s, keeps ages fresh) elapsed. `Live(auto_refresh=False)`; updates
   refresh explicitly.
3. `dashboard_payload` gains `gate_pools` (None without workers) built from the occupancy
   it already reads plus the worker registry (no probe, no subprocess), and
   `gate_hold.pool` on active cards. The header gets one extra line only when workers exist:
   `workers 🖥️ box1 ● on 1/2  🖥️ box2 ○ off 0/4  🏠 local 1/2 │ 🖥️ box1 C1  🏠 C2  ⏳ C3`.
   A running-gates card names its pool in the `now` line.

## Design

`agentic/profiles.py` (`_DIR_CACHE`), `agentic/store.py` (`_STATE_CACHE`),
`agentic/queue.py` (`_gate_pools_view`, `gate_hold.pool`), `agentic/cli_queue.py`
(`_watch_fingerprint`, `IDLE_REFRESH_S`, `_workers_line`, `pool_marker`).

## Alternatives considered

- **inotify** — new dependency / platform code; a stat fingerprint is enough.
- **C YAML loader** — not guaranteed present; caching removes the parse entirely.

## Acceptance criteria

- [x] Re-resolving a profile or re-listing campaigns parses nothing while the files are
      unchanged, and picks up an edit.
- [x] 29 idle ticks of `queue watch` build the payload once.
- [x] With a registered worker the header shows each worker (state, held/slots), the local pool
      held/slots, and 🖥️ name / 🏠 / ⏳ per gating campaign; with none it is unchanged.
- [x] Idle `nw queue watch` on a 208-campaign data dir uses far less CPU than before.

## Test plan

- **Automated:** `tests/test_agentic_watch_idle_cost.py` (parse-once and invalidation for both
  caches, fingerprint, idle payload count over 30 ticks, workers strip render with a remote,
  a local and a waiting campaign, no-worker unchanged).
- **Manual:** run `nw queue watch` under a pty against a copy of a real data dir
  (`NW_DATA_DIR`), sample the process CPU over 40 s.
- **Regression:** affected tests, dashboard/watch tests, `spec_lint`.

## As shipped

Idle CPU of `nw queue watch` (pty 160x50, 208 campaigns, 40 s sample): before 67.3% of one
core, after 1.9% (the remainder is the 15 s keep-fresh rebuild, now about 0.9 s each). Shipped
as designed; a reachability dot was left out (registry state only).

Test plan: the new `tests/test_agentic_watch_idle_cost.py` (6 tests) and the dashboard, watch-height,
profile and spec-lint tests (228 passed) were run. The `affected_tests.py` run was waived by the
human for this change ("this needs no testing, expedite"); the close gate's full suite was not run.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; affected tests pass (full suite left to the close gate).
- [x] No regressions (dashboard and watch tests pass).
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` regenerated.
- [x] `python3 tools/spec_lint.py` exits 0.
