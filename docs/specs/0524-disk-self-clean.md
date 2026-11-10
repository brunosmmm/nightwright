---
id: SPEC-0524
title: "Finished work cleans up after itself: pytest tmp retention and an hourly disk sweep"
status: accepted
owner: user
created: 2026-11-09
updated: 2026-11-09
milestone: "M9: agentic workflow"
kind: feature
tags: [agentic, disk, tests, bug]
depends_on: [SPEC-0287, SPEC-0381]
---

## Context

The dev machine hit ENOSPC twice on 2026-11-09 (`/tmp` is a 7.7 GB tmpfs) and
`/tmp/pytest-of-bruno` kept piling up. Read-only measurement (2026-11-09):

| Consumer | Size | Growth source |
|---|---|---|
| `/home/bruno/work/.nw-worktrees/Meta-Tools` (about 20 campaign worktrees, ~200 MB each) | 4.0 GB | campaign worktrees, removed only by manual `nw queue gc --yes` |
| `<agentic>/campaigns/*` (275 campaigns, 263 terminal), mostly `runs/*/stream.jsonl` | 1.6 GB | persona run replay logs, never pruned |
| `~/work/nw-watch-cpu-tmp` (a `pytest-of-bruno` basetemp, 1.2 GB, plus data) | 1.6 GB | an agent's ad-hoc test run with a custom basetemp |
| `~/.cache/nw-spec0492-tmp`, `nw-spec0500-tmp` | 1.2 GB | agent-made scratch dirs (outside the product code) |
| `<agentic>/sentinel/Meta-Tools` (worktree) + `.logs` (630 files) | 281 MB | full-suite sentinel; logs never pruned |
| `<agentic>/gatejobs` (1338 records) | 191 MB | swept only inside `nw gate run` |
| `/tmp/pytest-of-bruno` (5 retained sessions) | 33 MB | pytest default `all` retention, keeps 3 sessions of every tmp_path |
| other `/tmp` | ~1.3 GB | claude/grok/cursor tooling, outside the product |

Already self-cleaning: campaign `tmp/` on terminal states (SPEC-0287) and killed-runner residue
(SPEC-0253), worker-box job dirs (`worker.gc`, started after each job). The worker box could
not be measured: the ssh policy allows only `nw worker ...`.

## Goals / Non-goals

**Goals**
- A passing test run leaves no `tmp_path` directories behind.
- Finished work's bulky leftovers are removed without a human command, bounded by age.

**Non-goals**
- Removing campaign worktrees automatically (`nw queue gc` owns it; it lives next to gate-admission
  code in `queue.py`, which is being changed elsewhere). Agent scratch dirs under `~/.cache`.

## Decision

1. `pyproject.toml`: `tmp_path_retention_policy = "failed"`, `tmp_path_retention_count = 1`.
   Passing tests' tmp dirs are deleted at session end; only the last failing session is kept.
2. New `agentic/disk_clean.py` `sweep(cfg)`, run at most hourly from the runner tick and on demand
   by `nw queue disk-clean [--run-days N]`. It removes: terminal campaigns' `tmp/`; terminal
   campaigns' `runs/*/stream.jsonl` older than `run_days` (14); sentinel `*.logs` files older than
   `log_days` (14), always keeping the newest 40; final gate-job records past
   `agentic.gate_jobs.retention_days`. Never touched: live campaigns, `run.json`, `result.json`,
   `request.json`, `prompt.md`, ledger, state. Config: `agentic.disk_clean.{enabled,run_days,
   log_days,interval_s}`; the sweep never raises into the tick.

## Design

`queue._tick` calls `disk_clean.sweep(cfg)` right after the existing `sweep_campaign_tmp` loop
(one line; the rate limit is the mtime of `<agentic>/.disk-clean`). `nw queue disk-clean` forces
a sweep and prints what was freed.

## Alternatives considered

- **Custom `--basetemp` in addopts** - breaks xdist and parallel agents sharing one dir.
- **Deleting `runs/` wholesale** - loses the audit records; only the replay stream is dropped.

## Acceptance criteria

- [ ] pytest config sets retention policy `failed`, count 1.
- [ ] A sweep removes a terminal campaign's `tmp/` and its `stream.jsonl` older than `run_days`,
      keeps `run.json`/`result.json`/`prompt.md`/`state.json`.
- [ ] A live (non-terminal) campaign and a young stream are untouched.
- [ ] Sentinel logs: newest 40 and anything younger than `log_days` stay.
- [ ] The sweep is rate-limited, can be disabled, and bad settings fall back to defaults.
- [ ] `nw queue disk-clean` exists and the runner tick calls the sweep.

## Test plan

- **Automated:** `tests/test_disk_clean.py` (all criteria above, tmp dirs only).
- **Manual:** `nw queue disk-clean` on this machine; expect freed MB and no live campaign touched.
- **Regression guard:** `python3 tools/spec_lint.py`; affected tests for `queue.py` were not run
  (full suite waived by the human for this fast merge).

## Rollout / migration

Existing leftovers are removed by the first sweep after the runner restarts, or immediately with
`nw queue disk-clean`; worktrees with `nw queue gc --yes`.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated.

## As shipped

Shipped as designed. Ran: `tests/test_disk_clean.py` (6 passed), `tests/test_gate_jobs.py` and
the tests mentioning `campaign_tmp` (145 passed in total), `nw queue disk-clean --help`,
`python3 tools/spec_lint.py` (exit 0). Not run: the full suite and the close gate (waived by the
human for a fast merge); the whole affected-tests set for `queue.py` was started and stopped
after 3 min as too heavy. The manual `nw queue disk-clean` run on the real data dir was not
performed (deletions are the human's). Status stays `accepted` until the queue's close gate runs.
Not fixed: campaign worktrees (still `nw queue gc --yes`), agent scratch under `~/.cache/nw-spec*-tmp`,
and the worker box was unmeasurable over ssh.
