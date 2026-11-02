---
id: SPEC-0373
title: "Gate progress percent in queue watch and gate-slots"
status: done
owner: user
created: 2026-11-02
updated: 2026-11-02
milestone: "M9: agentic workflow"
kind: feature
tags: [agentic, queue, dashboard, gates]
depends_on: [SPEC-0309, SPEC-0372]
---

## Context

A running gate showed only elapsed time. IDEA-772 asks for a percent complete, from the gate's output.

## Goals / Non-goals

**Goals**
- A compact bar and percent for a running gate in `nw queue watch` (the `now` line of a gating card and the per-campaign gating entry of the workers strip, next to the pool marker) and `nw queue gate-slots` (table column, `progress` in `--json`).

**Non-goals**
- Streaming a worker's gate output to the controller while it runs.

## Decision

Parse the last pytest `[ NN%]` marker from the tail of the gate's live log. Without one, show an estimate (elapsed over the campaign's previous `gate` event `duration_s`) marked `~NN%`.

## Design

`src/nightwright/agentic/gate_progress.py`: `parse_percent`, a stat-cached tail read (`(mtime_ns, size)` key, so an unchanged log costs one stat), `live_percent` (newest `gates/*.log` written since the hold began, so a previous run's log is ignored), `estimate` (capped at 99), `progress`, `bar`/`label` (`▕████░░░░▏ 42%`, `~42%` when estimated). `queue.py` adds `gate_hold.progress` and a `progress` on each `gate_pools` campaign; `cli_queue.py` renders them; `cli_gate_slots.py` adds the column and JSON key. A remote gate's logs reach the controller only when it ends, so remote gates (pool is a worker) get the estimate only.

## Alternatives considered

- **Worker streams percent frames** - protocol change across worker_job/remote_gates; deferred.

## Acceptance criteria

- [x] The last `[ NN%]` in a local gate's live log shows as a bar in the watch card, the gating strip and `gate-slots`.
- [x] With no marker, or for a remote gate, an estimate shows as `~NN%`; with no previous gate nothing shows.
- [x] Nothing is shown unless a gate is running.

## Test plan

- **Automated tests:** `tests/test_gate_progress.py` (parser, bar, live-then-estimate fallback).
- **Regression guard:** `tests/test_agentic_watch_idle_cost.py`, `tests/test_cli_gate_slots.py`.

## As shipped

Implemented as designed. Tests were minimal by explicit human waiver: only the three new tests plus the idle-cost and gate-slots test files ran (10 passed); no full suite, no affected-tests run. Not exercised against a live remote gate or a real running gate in a live watch; remote percent is an estimate only. The watch fingerprint does not include gate logs, so a live percent updates at the next change or the 15 s idle refresh.

## Definition of done

- [x] Acceptance criteria met.
- [x] Test plan executed (minimal, by waiver).
- [x] Spec body matches what shipped.
- [x] `docs/LEDGER.md` regenerated.
