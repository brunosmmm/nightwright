---
id: SPEC-0173
title: "Deterministic gate executor and wt queue gates"
status: accepted
owner: user
created: 2026-10-25
updated: 2026-10-25
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0169
tags: [agentic, gates, tests]
source_idea: IDEA-020
depends_on: [SPEC-0170, SPEC-0171]
---

## Context

Child of epic [SPEC-0169](./0169-mini-agentic-workflow-queue-campaigns-headless.md)
(promoted from `IDEA-020`).

Gates are the loop's source of truth: the project's own test suite, linter or any other
command declared in `.wt/project.yaml` (SPEC-0170). They must run deterministically, in the
campaign's worktree, with a timeout, and leave logs a triager or a human can read. Their
result decides whether a campaign moves to close or to triage (SPEC-0176) and is re-run on
the rebased tree before a merge (SPEC-0175).

## Goals / Non-goals

**Goals**
- Run every configured gate, in order, every time (no short-circuit) so triage sees the
  full picture.
- Per-gate log files under the campaign's `gates/` directory; a bounded `tail` in the
  result for prompts and `wt campaign show`.
- Timeouts kill the whole process group, not just the shell.
- `required` decides blocking; optional failures are recorded, never block.
- `wt queue gates CAMPAIGN`: run the gates on a campaign's worktree now, log a `gate`
  event, change no state (for debugging a parked item).

**Non-goals**
- Parsing test-runner output (JUnit etc.). Exit code is the verdict; the log is the detail.
- Retries, flaky tracking, warn/escalate rungs (later wave: gate ladder v2).
- Deciding what happens after a failure (SPEC-0176).

## Decision

Add `src/wt/agentic/gates.py` with `GateResult`, `GateRun`, `run_gate` and `run_all`.
Commands run through `subprocess.Popen(cmd, shell=True, cwd=…, start_new_session=True)` so
a timeout can `os.killpg` the group; output is combined into one log file per gate. The
environment carries `WT_CAMPAIGN`, `WT_DATA_DIR`, `WT_CONFIG_DIR`, `WT_SPEC_ID`,
`WT_WORKTREE`, `WT_BASE` so gate scripts (and anything they call) address the same wt
store the loop uses.

## Design

```python
@dataclass(frozen=True)
class GateResult:
    name: str; required: bool; ok: bool; exit_code: int | None; timed_out: bool
    duration_s: float; log: str          # path relative to the campaign dir
    tail: str                            # last 60 lines

@dataclass(frozen=True)
class GateRun:
    seq: int; ok: bool; results: tuple[GateResult, ...]; started_at: str; duration_s: float
```

- `run_gate(gate, worktree, env, log_path, placeholders) -> GateResult`: `cmd =
  project.expand_cmd(gate["cmd"], **placeholders)`; `cwd = worktree / gate["cwd"]`; the
  log file gets a header line `# <name>: <cmd> (cwd <cwd>)`; on `TimeoutExpired`
  (`gate["timeout_s"]`) `os.killpg(pgid, SIGTERM)`, wait 5 s, `SIGKILL`, `timed_out=True`,
  `ok=False`; `ok = exit_code == 0`.
- `run_all(project_cfg, campaign_state, cfg, *, seq) -> GateRun`: iterates
  `project_cfg["gates"]`, writes `gates/<seq:03>-<name>.log` under the campaign dir
  (SPEC-0171 paths), `ok = all(r.ok for r in results if r.required)`.
- `blocking_failures(run) -> list[GateResult]`; `tail(path, n=60) -> str`.
- `as_payload(run) -> dict` for the `gate` ledger event and for `last_gate` in state.
- `wt queue gates CAMPAIGN [--json]` (in `src/wt/agentic/cli.py`, under the `queue` group
  created here as an otherwise empty group that SPEC-0176 fills): loads state and project
  config, runs `run_all` with the next gate `seq`, appends a `gate` event with
  `by="human"`, prints a table `gate | required | result | duration | log`; `--json` emits
  `{"schema": "wt.campaign.gate.v1", "campaign", **as_payload(run)}`. No state transition
  and `last_gate` is **not** updated (debug runs must not steer the queue).

## Alternatives considered

- **Short-circuit on first required failure** — rejected. Triage and humans want all
  failures at once; gates are cheap relative to a persona run.
- **`shell=False` with `shlex.split`** — rejected. Project gate commands are shell lines by
  nature (`uv run pytest -q -x`, pipes); `start_new_session` plus `killpg` contains them.
- **Streaming logs to the console** — not in wave 1; the log file plus `tail` serves both
  the prompt and the human.

## Acceptance criteria

- [ ] A passing, a failing and a timing-out gate produce the documented `GateResult`
      (exit code, `timed_out`, bounded `tail`, log path); the timing-out gate's child
      process group is gone afterwards.
- [ ] `run_all` runs every gate even after a required failure; `ok` is false only when a
      required gate fails; optional failures are present in `results`.
- [ ] Placeholders in `cmd` expand; the environment contains the six `WT_*` variables.
- [ ] Log files exist at `gates/<seq>-<name>.log` with the header line and the command's
      combined output.
- [ ] `wt queue gates CAMPAIGN` appends exactly one `gate` event, leaves `state.json`'s
      `state` and `last_gate` unchanged, and `--json` emits `wt.campaign.gate.v1`.
- [ ] Full `uv run pytest` green; `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests:** `tests/test_agentic_gates.py` with `sh -c` gates: `exit 0`, `exit 3`,
  `sleep 30` under `timeout_s: 1` (asserts the group is dead via the recorded pgid), an
  `env` gate that prints `WT_CAMPAIGN`, a placeholder gate that echoes `{spec_id}`; a
  project config with one required and one optional failing gate; the CLI through
  `CliRunner` with a campaign created via SPEC-0171's store in a tmp data dir.
- **Manual verification:** in a scratch campaign dir, `wt queue gates <id>` against this
  repo's two gates and inspect the logs.
- **Regression guard:** full `uv run pytest`.

## Clock Log

Internal work: clock on IDEA-020 with `wt idea clock-in` / `clock-out`.

## Rollout / migration

Library plus one read-mostly command. Nothing runs gates unattended until SPEC-0176.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

_None — decisions locked above._
