---
id: SPEC-0521
title: "Retire pull workers: remove the coordinator job API, pull_jobs, pull_worker and nw serve --jobs"
status: draft
owner: user
created: 2026-11-09
updated: 2026-11-09
milestone: "M9: agentic workflow"
parent: SPEC-0501
tags: [agentic, workers, cleanup]
depends_on: [SPEC-0516]
---

## Context

SPEC-0334 added pull workers for boxes behind NAT: a coordinator-hosted job API (`nw serve
--jobs`, `pull_jobs.py`, 1176 lines) that workers poll (`pull_worker.py`, 501 lines), with
`test_pull_workers*.py` (~1,900 lines). Both Slurm nodes are on the LAN, the coordinator is a
laptop that sleeps (it cannot serve a job API), and `workers.json` registers no pull worker
(only `box`, checked 2026-11-09). Research verdict: retire.

## Goals / Non-goals

**Goals**
- Remove the pull worker code, CLI and docs; a registry entry with `pull: true` is refused with
  a clear message.

**Non-goals**
- Any NAT support (none is needed).

## Decision

At ship this spec supersedes SPEC-0334 (`superseded_by: SPEC-0521`). Draft until the two-node
cluster runs (SPEC-0516), so no pull fallback is needed during the migration.

## Design

- Delete `pull_jobs.py`, `pull_worker.py`, `nw serve --jobs`, `nw workers token`, the `pull`
  and `url` registry fields, the `rotated` loss handling in `gates._retry`, and their tests.
- `workers.normalize` rejects `pull: true` with "pull workers were retired (SPEC-0521)".

## Alternatives considered

- **Keep it dormant.** ~1,700 source + ~1,900 test lines maintained for a case nobody uses.

## Acceptance criteria

- [ ] No module imports `pull_jobs`/`pull_worker`; `nw serve --jobs` and `nw workers token` are gone.
- [ ] A `pull: true` registry entry is refused with the message.
- [ ] SPEC-0334 marked superseded; ledger regenerated.

## Test plan

- **Automated tests:** removed tests deleted; a registry test for the refusal; the CLI help
  tests updated.
- **Manual verification:** `nw workers list` and a campaign gate on the cluster still work.
- **Regression guard:** affected tests of `workers.py`, `gates.py`, the CLI.

## Rollout / migration

After SPEC-0516. Supersedes SPEC-0334 at ship.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; affected tests pass; manual steps performed and recorded.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Risks

- None beyond deletion scope; nothing registered uses it.

## Open questions

- None.
