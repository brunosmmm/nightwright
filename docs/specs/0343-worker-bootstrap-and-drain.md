---
id: SPEC-0343
title: "Worker bootstrap and drain"
status: accepted
owner: user
created: 2026-11-01
updated: 2026-11-01
source_idea: IDEA-559
parent: SPEC-0310
tags: [agentic, remote, workers, ops]
depends_on: [SPEC-0341]
---

## Context

Adding a worker today means installing nightwright on it by hand and writing the
forced-command `authorized_keys` line yourself. Taking one out of service means disabling it
and hoping no job is running. With jobs that outlive the connection
([SPEC-0340](./0340-worker-async-job-protocol.md),
[SPEC-0341](./0341-coordinator-re-attach-job-records-and-adoption.md)), the second needs a
graceful path.

This is the fourth part of the split of
[SPEC-0332](./0332-worker-owned-async-jobs.md). It is independent of the retry work
([SPEC-0342](./0342-lost-job-retry-across-workers-and-escalation.md)) and can land in
parallel with it.

## Goals / Non-goals

**Goals**
- `nw workers bootstrap` installs nightwright on a new worker and registers it.
- `nw workers drain` takes a worker out of service without losing running jobs.

**Non-goals**
- Persona prerequisites (`claude` login): SPEC-0344's `check --auth`.
- Automatic worker upgrades.

## Decision

Bootstrap installs a wheel of the coordinator's own checkout, so both sides run the same
code. Drain marks the worker `draining`; the queue disables it once its last remote job
record is final.

## Design

- **`nw workers bootstrap NAME --ssh DEST [--slots N]`**
  1. It builds a wheel of the coordinator's checkout (`uv build --wheel`) and copies it
     with `scp`.
  2. It runs `uv tool install --force <wheel>` over a normal (not forced-command) SSH
     session. This needs the human's own key to the worker account.
  3. It runs `nw worker info` to check the install.
  4. It prints the forced-command `authorized_keys` line, built from the key the coordinator
     uses for workers (`agentic.workers.key`, default `~/.ssh/id_ed25519.pub`).
  5. With `--install-key` it calls `nw workers add` (with check); without it, it calls
     `nw workers add --no-check` and prints `run nw workers check NAME after installing the
     key line`. The `add` runs after the key is installed, so `check` can succeed.

  `--install-key` appends the key line to `~/.ssh/authorized_keys` on the worker, unless the
  line is already present.
- **`nw workers drain NAME`** stops new jobs from being placed on the worker
  (`draining: true`, added to the registry schema). Its running jobs finish. `nw workers
  list` shows `draining (k jobs)`. On each tick the queue sets `enabled: false` and clears
  `draining` for a draining worker with no non-final `remote-jobs` record (SPEC-0341).

## Alternatives considered

- **Install from a package index.** The coordinator and worker could then differ in version;
  a wheel of the checkout guarantees the same code.
- **Disable immediately on drain.** That would orphan running jobs.

## Acceptance criteria

- [ ] `nw workers bootstrap` builds, copies and installs the wheel, checks `nw worker info`,
      prints the `authorized_keys` line and registers the worker (`--install-key` adds the
      line; running it twice does not duplicate it).
- [ ] Without `--install-key`, the worker is added with `--no-check` and the next step is
      printed.
- [ ] `nw workers drain` stops new placements on the worker; running jobs finish; the queue
      then sets `enabled: false` and clears `draining`.
- [ ] `nw workers list` shows `draining (k jobs)`.

## Test plan

- **Automated tests** (`tests/test_workers_bootstrap_drain.py`, offline):
  - **bootstrap:** the ssh/scp/uv calls go through a recording fake `ssh_argv`; assert the
    sequence, plus that `--install-key` is idempotent and the no-key path uses `--no-check`;
  - **drain:** a draining worker gets no new job, its running job finishes, and the worker
    then becomes disabled; `list` output shows the draining count.
- **Manual verification:**
  1. `nw workers bootstrap box --ssh user@box --install-key` on a clean machine, then
     `nw workers check box`.
  2. Start a gate on `box`, run `nw workers drain box`, and confirm the gate finishes and
     the worker ends up disabled.
- **Regression guard:** affected tests while iterating; SPEC-0337's registry tests run
  unchanged.

## Clock Log

Use `nw idea clock-in IDEA-559` / `nw idea clock-out IDEA-559`.

## Rollout / migration

1. New commands only. The registry gains an optional `draining` field; absent means false.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

None. Decided while splitting SPEC-0332:
- Bootstrap installs a wheel of the coordinator's own checkout.
- The queue (not the human) disables a drained worker, on a tick.
