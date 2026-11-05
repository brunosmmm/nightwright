---
id: SPEC-0439
title: "In-flight host backend switch (`nw queue backend --host` / `--all`)"
status: done
owner: user
created: 2026-11-05
updated: 2026-11-05
kind: feature
tags: [agentic, runner, cursor]
depends_on: [SPEC-0436, SPEC-0438, SPEC-0226]
source_idea: IDEA-1009
---

## Context

[SPEC-0435](./0435-cursor-agent-backend-for-the-agentic-queue.md) let a host flip happen
only by editing `config.yaml`, and refused `--active` bulk. `conductor-launch --runner
cursor` ([SPEC-0438](./0438-conductor-launch-steward-launch-runner-cursor.md)) moves the
conductor session, not personas. The human asked for an in-flight switch when Claude
credits run out; file-edit + next-spawn-only with no host CLI was the gap.

## Goals / Non-goals

**Goals**
- `nw queue backend --host cursor|claude|default --yes` writes user
  `agentic.runner` (delete the key on `default`). The running loop reloads (SPEC-0226).
- `nw queue backend --all cursor|claude|default --yes` pins or clears every
  non-terminal campaign.
- `conductor-launch --runner cursor|claude` (not dry-run) also writes host
  `agentic.runner` so launching the conductor on Cursor switches unpinned personas.
- Live persona processes are not SIGTERM'd; the next spawn uses the new resolution.

**Non-goals**
- Killing in-flight `claude -p` / `cursor-agent -p`.
- `nw queue runner` (name collision). Steward-launch does not write host config.

## Decision

Amend SPEC-0435's "no host CLI / no bulk" for the credit-exhaustion case. One command
family: existing per-campaign `backend CAMPAIGN`, plus `--host` and `--all`. Conductor
`--runner` is a host switch, not only a session switch.

## Design

`plan_host_backend` / `apply_host_backend`: load on-disk user `config.yaml` only
(backup, atomic replace, same idea as `apply_project_add`). Set `agentic.runner` to
`claude`/`cursor`, or delete the key for `default`. Journal `runner_changed`
`{host: true, from, to}`. Update in-memory `cfg["agentic"]["runner"]`.

`plan_backend_all` / `apply_backend_all`: `store.list_campaigns` (non-terminal);
reuse per-campaign plan/apply; skip no-ops (`from == to`).

CLI: first argument is the campaign id, or the backend when `--host`/`--all` is
set (`nw queue backend --host cursor`, `nw queue backend --all cursor`,
`nw queue backend CAMPAIGN cursor`). `--host --all` together is the full
in-flight switch (host default + every live pin). Campaign + `--all` is an error.

`conductor-launch --runner X` calls `apply_host_backend` before `run` (not on
`--dry-run`; dry-run prints `host: X`).

## Alternatives considered

- **Keep file-edit only** — what shipped; the human could not switch in flight from
  the CLI they were already using.
- **SIGTERM live personas** — loses a verdict in progress; still rejected.

## Acceptance criteria

- [x] `--host cursor --yes` writes `agentic.runner: cursor` in user config.yaml;
      `--host default --yes` removes the key. Dry-run writes nothing.
- [x] `--all cursor --yes` sets `state.runner` on every non-terminal campaign;
      terminal campaigns are untouched.
- [x] Per-campaign `backend CAMPAIGN cursor` is unchanged.
- [x] `conductor-launch --runner cursor` (not dry-run) writes host `agentic.runner:
      cursor`. Steward-launch does not.
- [x] No SIGTERM of a live persona. Shipping does not change compiled defaults.

## Test plan

- **Automated tests:** `tests/test_cursor_runner.py` — host write/delete; `--all`;
  noop skip. `tests/test_cursor_launch.py` — CLI `--host` / `--all` / campaign;
  conductor-launch writes host; steward does not; dry-run does not write.
  Executed 2026-11-05 (`309 passed` on those files plus `test_agentic_launch.py`).
- **Manual verification:** `nw queue backend --host cursor` (dry-run) shows the plan
  and does not write. Executed in CliRunner; live `--host --all cursor --yes` after
  merge.
- **Regression guard:** existing backend pin tests; `python3 tools/spec_lint.py`.

## Clock Log

IDEA-1009.

## Rollout / migration

No migration. Host is already `cursor` on this box from a hand edit.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; tests pass; manual noted.
- [x] Spec body matches what shipped.
- [x] `docs/LEDGER.md` regenerated; `spec_lint` exits 0.

## Open questions

None.
