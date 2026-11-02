---
id: SPEC-0384
title: "queue watch: standalone gate jobs get their own line so they are never truncated"
status: done
owner: user
created: 2026-11-02
updated: 2026-11-02
kind: feature
tags: [agentic, queue, watch, gates]
depends_on: [SPEC-0372, SPEC-0381]
source_idea: IDEA-801
---

## Context

SPEC-0381 appended running `nw gate run` jobs to the SPEC-0372 workers line, after the campaign
markers, on a single `no_wrap` ellipsis line. With a campaign on the box the line is already cut
off, so a running GATEJOB-NNNN was invisible in `nw queue watch` (IDEA-801).

## Goals / Non-goals

**Goals**
- A running or waiting gate job is always visible in the watch.

**Non-goals**
- No change to the `nw.queue.dashboard.v1` payload or `gate_pools.gate_jobs`.

## Decision

Render gate jobs on their own line directly below the workers line, shown only when there are
jobs. The workers line no longer carries them.

## Design

`_gate_jobs_line(p)` in `src/nightwright/agentic/cli_queue.py` builds ` gate jobs ` (dim) then
`<pool marker> ⚙ GATEJOB-N by <who> (project@branch)` per job. `_dash_header` appends it after
the workers line. Height budgeting measures the header panel (`_lines(head, width)`), so the
extra row is accounted for automatically. `--once` renders the same frame; `--json` is untouched.

## Alternatives considered

- **Jobs before campaigns on the same line** - still truncatable with several jobs or a narrow terminal.

## Acceptance criteria

- [x] With a campaign marker long enough to truncate the workers line, the job id is in the rendered gate jobs line.
- [x] No line is rendered when there are no jobs.
- [x] The dashboard JSON payload still carries `gate_jobs` unchanged.

## Test plan

- **Automated:** `tests/test_gate_jobs.py::test_gate_jobs_line_survives_a_long_campaign_marker` and the updated `test_dashboard_and_gate_slots_show_the_job`; `tests/test_agentic_watch_height.py` and `tests/test_agentic_watch_idle_cost.py` for frame/height regressions.
- **Manual:** `uv run nw queue watch --once` shows the line when a job runs.

## Rollout / migration

None.

## As shipped

As designed. The project@branch suffix is shown when the job carries them.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed.
- [x] Spec body matches what shipped.
- [x] `docs/LEDGER.md` updated.
