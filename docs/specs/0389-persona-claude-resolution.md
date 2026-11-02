---
id: SPEC-0389
title: One claude resolver for claude-check and the persona job exec; missing claude is a job error
status: done
owner: user
created: 2026-11-02
updated: 2026-11-02
tags: [workers, remote-gates]
depends_on: [SPEC-0344, SPEC-0387]
---

## Context

IDEA-811. A remote persona job on worker `box` (account `nw`, forced-command ssh key) failed in ~5 ms with
`[Errno 2] No such file or directory: 'claude'` although `nw worker claude-check` passed. On the box
claude lives at `~/.local/bin/claude`, which a non-login ssh `PATH` lacks. Both paths used the bare name
`claude` through the process `PATH` (`worker.py` `claude_check` -> `_version_out("claude")`; the job's
`runproc.main` -> `Popen(["claude", ...])`), so any `PATH` difference between the two processes (the job is
a detached grandchild spawned from `worker_job._spawn`) made the check pass and the job fail, and the
failure surfaced as a persona verdict failure (`exit.json` error), charged as a run.

## Goals / Non-goals

**Goals**
- One resolver for claude (PATH, then `~/.local/bin` and other standard install dirs) used by claude-check
  and the job exec.
- Missing claude fails the job as a SPEC-0387 `job_error` frame, not a persona verdict.

**Non-goals**
- A configurable claude path; installing claude.

## Decision

`runproc.resolve_claude()` is the only lookup. `claude_check` runs the resolved absolute path (and fails
when none), `runproc.main` execs the resolved path with its dir appended to the child's `PATH`
(`with_claude`), and `remote_personas.run_job` raises before `started` when the selected runner is the
claude runner and nothing resolves; `worker_job` turns that into an `error` frame with `job_error: true`,
`stage: run`, `filename: claude` (SPEC-0387 `error_detail`).

## Design

- `runproc.resolve_claude(environ=None)`: `shutil.which` on `PATH`, then `~/.local/bin`, `~/.claude/local`,
  `~/.npm-global/bin`, `~/.bun/bin`, `~/.volta/bin`, `/usr/local/bin`, `/opt/homebrew/bin`, `/usr/bin`.
- `runproc.with_claude(argv, env)`: absolute `argv[0]`, dir appended to `PATH`; `FileNotFoundError` if none.
- `worker.claude_check`: uses the resolver for both `--version` and the `--auth` probe.

## Alternatives considered

- **Prepend `~/.local/bin` to the worker's `PATH` globally** - affects gates; a resolver is narrower.

## Acceptance criteria

- [x] With claude only in `~/.local/bin` and a `PATH` lacking it, `claude_check` is ok and the job exec resolves the same binary.
- [x] With no claude, `claude_check` is not ok and `run_job` raises before any frame, yielding a `job_error` frame.

## Test plan

- **Automated tests:** `tests/test_claude_resolve.py` (resolver, claude_check ok/fail, `with_claude`, `run_job` raises with no frame and `error_detail` marks `job_error`).
- **Regression guard:** `tests/test_remote_personas.py`.
- **Manual verification:** none against the real box (excluded by instruction); the caller runs `nw workers check box` after the worker update.

## Rollout / migration

Ship in a worker update (`nw workers update`).

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; affected tests pass.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` regenerated.

## Open questions

None.
