---
id: SPEC-0338
title: "Remote gate dispatch with local fallback"
status: in-progress
owner: user
created: 2026-10-31
updated: 2026-10-31
source_idea: IDEA-558
parent: SPEC-0310
tags: [agentic, remote, workers, gates, security]
depends_on: [SPEC-0336, SPEC-0337]
supersedes: [SPEC-0331]
---

## Context

Gates take about half of active campaign time (SPEC-0310 Context). Since
[SPEC-0309](./0309-faster-gates-parallel-pytest-affected-tests-only.md), the host-wide
gate-slot pool (`agentic.gates.slots`, default 2) is the ceiling. A third campaign that
reaches GATING waits for a slot (`gate_waiting`). The human has a second machine that sits
idle.

This is the thin first slice of
[SPEC-0310](./0310-remote-workers-run-gates-persona-runs-and.md). Its review recommended a
split into three parts, so [SPEC-0331](./0331-remote-gates-over-ssh-on-one-worker-host.md)
is superseded by this spec and its two prerequisites. This spec is the last part,
**dispatch**: the one that makes gates run remotely. It builds on:
- [SPEC-0336](./0336-gate-worker-job-runner-and-forced-command.md): `nw worker gate-job`,
  the `nw.job.v1` header, the frames (including `started`), the base ref, the forced
  command;
- [SPEC-0337](./0337-worker-registry-and-multi-pool-gate-slots.md): `workers.json`,
  `nw workers`, `gate_slots.acquire(pools=...)`, `workers.gate_pools(cfg)`.

A gate run is `gates.run_all(project_cfg, campaign_state, cfg, *, seq, stop, tier)`
(`src/nightwright/agentic/gates.py:386`). It takes a slot, then `_run_all_held` claims
`gate-<seq>` (the SPEC-0253 loop: `store.child_tmp(..., exclusive=True)`, next seq on
collision), runs each gate with `run_gate_with_retries` (`gates.py:275`), writes
`gates/<seq>-<name>.log` and `.json` (plus `.r<n>`), clears the claim and returns a
`GateRun`. Callers: `queue._do_gate` (`queue.py:1946`, tier `loop`); `close.prepare_close`
(defined at `close.py:410`, calls `run_all` at 551, tier `close`); `nw queue gates`
(`cli_queue.py:1142`). Everything downstream reads the `GateRun`, the `gate` event and those
files.

## Goals / Non-goals

**Goals**
- A gate run can execute on a registered worker instead of locally. It produces the same
  `GateRun`, the same `gates/` files and the same `gate` event, plus a `worker` key.
- Remote first, local fallback: any transport failure runs the same gates locally in the
  same call, under the same seq, so turning a worker on can never lose a gate.
- With no worker registered, the code path and every output are exactly today's.
- A project or a single gate can opt out of remote execution.

**Non-goals**
- Jobs that outlive the SSH connection, leases, retry on another worker:
  [SPEC-0332](./0332-worker-owned-async-jobs.md).
- Persona runs on workers: [SPEC-0333](./0333-remote-persona-runs-with-bundle-write-back.md).
- Installing nightwright on the worker (manual, documented in SPEC-0336).

## Decision

`run_all` claims the seq first, then acquires a slot over `workers.gate_pools(cfg)`. A local
hold runs today's body. A remote hold runs `remote_gates.run`, which bundles the commit
under test and its base, runs `ssh <dest> nw worker gate-job`, streams the header and
bundle, reads frames, writes the returned files at the paths a local run uses, and builds
the `GateRun`. Any transport failure falls back to the local body with the same claim.

## Design

### Seq claim and the split of `_run_all_held`

- The SPEC-0253 claim loop moves from `_run_all_held` into `run_all`, before `acquire`.
  `_run_all_held` receives the claimed `seq` and `owner` and no longer claims or clears.
- `run_all` clears the claim exactly once, in its `finally`.
- `job_id` is `<campaign>-gate-<seq:03d>-a0` with the claimed seq.
- On fallback the coordinator keeps the claim, deletes only the bundle from the child tmp
  dir, and calls `_run_all_held` with the same seq and owner.
- With no workers the observable behaviour is unchanged (the claim happens a slot-wait
  earlier, which only narrows SPEC-0253's race).

### Eligibility

Eligibility is evaluated only when `workers.gate_pools(cfg)` contains at least one remote
pool (and not under `--local`). With no enabled worker, `run_all` acquires local only, runs
no eligibility git command and appends no `finding`.

A run uses `workers.gate_pools(cfg)` only when all hold; otherwise it acquires local only:
- the worktree has no untracked, non-ignored files (`git ls-files --others
  --exclude-standard` empty), since the bundle carries commits only;
- the project does not set `gates.remote: false` in `.nw/project.yaml`;
- no gate in the resolved list sets `remote: false` (for gates that read ignored files such
  as `.env` or local caches).

The first ineligible run of a campaign appends one `finding` `{persona: "gates", severity:
"info", gate: "remote-eligibility", title: <reason>, detail: <up to 5 untracked paths or the
opted-out gate>, files: [...]}`. Neither `gates` nor `info` is in divergence's counted sets
(`divergence.py:17-20`), so it never feeds the divergence detector. "First" is decided by
scanning the campaign ledger for an existing `finding` with `gate: "remote-eligibility"`.

`project.py` accepts the new keys (`gates.remote: bool`, per-gate `remote: bool`, both
default true) in its validation.

### Commit under test and bundle

1. `sha` is `git stash create` in the worktree when tracked files are modified (the pattern
   of `queue._save_worktree_changes`; the stash stack is untouched), else HEAD.
2. `base_sha` = `git rev-parse <repo.base>` in the coordinator repo.
3. `have`: `ssh ... nw worker have --repo KEY <base_sha> <merge-base HEAD base>`. `KEY` is
   the lowercased project name plus the first 12 hex of sha256 of the coordinator repo's
   realpath.
4. `git update-ref refs/nw/gate-jobs/<job_id> <sha>` and
   `refs/nw/gate-jobs/<job_id>-base <base_sha>`, then
   `git bundle create <child tmp>/job.bundle refs/nw/gate-jobs/<job_id>
   refs/nw/gate-jobs/<job_id>-base ^<have>...` (full when `have` is empty). Only `have`
   shas that are ancestors of `sha` or `base_sha` are passed as `^` exclusions. A ref whose
   tip is in `have` is left out of the bundle, and the worker relies on the header's
   `commit`/`base.sha` for it, since both objects are already present. When nothing remains
   to bundle (HEAD equals the base on a warm cache), the coordinator sends `bundle_bytes: 0`
   and no bundle, and the worker skips the fetch (SPEC-0336). Both refs are deleted in a
   `finally` once the bundle is written or the step fails.
5. The header (SPEC-0336) carries `base: {name: repo.base, sha: base_sha}`, the resolved
   gate list, the env allowlist values, and `timeout_s` = `gate_budget_s` + the sum over
   gates of `timeout_s` x (1 + `retries`), capped at 4 h.

### `remote_gates.run(worker, project_cfg, campaign_state, cfg, seq, owner, stop)`

- Starts `worker.ssh_argv + [worker.ssh, worker.nw, "worker", "gate-job"]`, writes the
  header and bundle, keeps stdin open, reads frames on the step's thread.
- **Timeouts.** Before `started`, it allows at most `worker_slot_wait_s` (default 300 s,
  `agentic.workers.slot_wait_s`); past that it closes stdin (cancel) and falls back
  **without** cooldown. From `started`, the kill timer is `timeout_s` + 120 s. The run's
  `slot_wait_s` is the coordinator's wait plus the worker's.
- Each `gate` frame's files are written into `gates/` under the campaign dir with the names
  the worker reported. `GateResult.log` is rewritten to its campaign-relative path, `tail`
  recomputed from the local copy; `cwd` keeps the worker path.
- On `stop` (SPEC-0229): close stdin, wait 10 s for `done`, then kill SSH; the result is an
  interrupted `GateRun`.
- After `done`, the remote slot is released as for a local run. The registry is not
  written: a cooldown is only set by a transport failure and expires on its own.

### Transport failure and fallback

A transport failure is, before `done`: SSH exit 255, a protocol mismatch, an `error` frame,
a malformed frame, EOF, or a coordinator timeout. A failure of the `have` SSH call is also a
transport failure (`stage: "have"`, with cooldown). On one, the coordinator:
1. deletes every `gates/<seq>-*` file this run wrote;
2. appends `worker_failed` `{worker, stage, error, job_id}` to the campaign ledger and the
   queue events (new `EVENT_TYPES` entry; informational, the conductor takes no action);
3. sets `cooldown_until` = now + 600 s for the worker (SPEC-0337 `workers.update`);
4. releases the remote slot, acquires a local slot, and runs `_run_all_held` under the same
   seq and claim. The fallback acquire uses local pools only and passes `stop` and `on_wait`
   as `run_all` does. If stopped while waiting it returns an interrupted `GateRun` with the
   claimed seq, and the claim is cleared. The run's `slot_wait_s` is the sum of the
   coordinator's remote wait, the worker's reported wait (if `started` was seen) and the
   fallback wait.

A failure of any coordinator-side git step (`stash create`, `update-ref`, `bundle create`)
falls back to local **without** cooldown (`worker_failed` with `stage: "bundle"`).

An `error` with `stage: "claim"` (a duplicate job id after a coordinator crash) and the
pre-`started` slot-wait timeout fall back without cooldown. A **gate failure** (`done` with
`ok: false`) is a result, not a transport failure: no fallback.

### Reporting

- `GateRun` gains `worker: str | None = None`; `as_payload` (`gates.py:454`) adds
  `"worker"` only when set, so local `gate` events stay byte-identical.
- `nw queue gates` names the worker in its table title; `nw campaign show` in the gate line.
- `nw queue gates CAMPAIGN` gains `--worker NAME` and `--local` (never a worker).
  `--worker NAME` uses only that worker's pool and ignores its cooldown. It errors if the
  worker is unknown or disabled, and an ineligible run errors instead of gating locally. A
  transport failure still falls back locally.

## Alternatives considered

- **rsync the worktree.** Copies `.venv`, caches and build output and needs rsync and a
  shell on both ends. A bundle against a warm cache is a few KB.
- **A shared git remote the worker fetches from.** Needs push credentials or a server.
- **Tar outputs at the end instead of per-gate frames.** A drop loses everything and
  progress is invisible.
- **Retry remotely once before falling back.** A host that just failed usually fails again;
  local is known to work.
- **No opt-out, eligibility only.** Gates that read ignored files would go falsely red
  remotely with no recourse; `remote: false` is cheap.

## Acceptance criteria

- [x] With no `workers.json`, and with an empty one, the `gate` event payload, the `gates/`
      files and the `GateRun` are identical to before (golden test recorded **before**
      `run_all` is modified, timestamps and durations masked), including for a project with
      an untracked file and `gates.remote: false`, where no `finding` is appended.
- [x] With one registered worker, a gate run takes a remote slot and runs on the worker. The
      coordinator's `gates/<seq>-<name>.{log,json}` and `.r<n>` files match what the worker
      wrote; the `gate` event has `worker: <name>` and otherwise a local event's shape;
      `nw queue gates` and `nw campaign show` name the worker, and `nw queue gates --worker
      NAME` / `--local` behave as specified.
- [x] A remote gate running `git merge-base $NW_BASE HEAD` prints the same sha as locally.
- [x] A tree with modified tracked files gates the `git stash create` commit. A tree with
      untracked non-ignored files, a project with `gates.remote: false`, and a run with a
      `remote: false` gate each gate locally and record one `finding`.
- [x] From the second gate run of a campaign on, `have` is non-empty and the bundle is
      smaller than a full one; a run whose HEAD equals the base (empty bundle) and a second
      run with the base ref left out both gate remotely without falling back.
- [x] Each transport failure (unreachable host, a failing `have` call, `error` frame,
      malformed frame, drop mid-run, coordinator timeout) yields `worker_failed`, a
      cooldown, and a local run of the same gates under the same seq, with no leftover
      remote files. A `claim` error, a failing bundle step and a worker slot wait past
      `worker_slot_wait_s` fall back without cooldown.
- [x] A worker whose slot is held for longer than `timeout_s` + 120 s but less than
      `worker_slot_wait_s` does not trip the run timeout once it emits `started`.
- [x] A gate that fails on the worker is reported as a failure and is not re-run locally.
- [x] A stop during a remote gate ends the worker's gate process and returns an interrupted
      `GateRun`.
- [ ] Manual: one campaign's GATING run and its close re-gate ran on the human's second
      machine.

## Test plan

- **Automated** (`tests/test_remote_gates.py`, offline). Loopback transport: `ssh_argv` set
  to a script that drops the destination and runs the remote command locally through
  `SSH_ORIGINAL_COMMAND` and `nw worker ssh-command`, with `NW_DATA_DIR`/`NW_CONFIG_DIR` at
  a second temp data dir. Fake project in a temp git repo with tiny shell gates.
  - First, before touching `run_all`: record the golden payload and files of the no-worker
    path; then assert it for no registry and an empty registry.
  - Remote pass and fail; retry ladder (`.r1` files); the `merge-base` gate.
  - Dirty tracked tree gates the stash commit; untracked file, `gates.remote: false` and a
    `remote: false` gate each gate locally with one `finding`.
  - Second run's bundle excludes the base; a worktree whose HEAD equals the base, gated
    twice (empty bundle), gates remotely.
  - Transport failures: `ssh_argv` exits 255; fails only on `nw worker have`; emits garbage;
    dies after the first `gate` frame; coordinator timeout. Each: `worker_failed`,
    cooldown, local run, same seq, no stray files. Duplicate `job_id`: claim error,
    fallback without cooldown. A failing `git bundle create` (patched): fallback without
    cooldown, no `refs/nw/gate-jobs/*` left.
  - Stop set during the fallback local wait: interrupted `GateRun`, claim cleared.
  - Worker slot held (a held flock in the worker data dir) with small `timeout_s` and
    larger `worker_slot_wait_s`: the run completes; with `worker_slot_wait_s` small, it
    falls back without cooldown.
  - Stop mid-run with a `sleep 600` gate (waited with `tests/waits.py`).
  - `nw queue gates --worker` / `--local` via `CliRunner` (including `--worker` on a
    cooled-down worker, which still dispatches); `nw campaign show` output names the worker.
- **Manual verification:**
  1. On the second machine, follow the SPEC-0336 docs (dedicated user, `uv tool install`,
     forced-command key).
  2. `nw workers add box --ssh nw@box --slots 2`; `nw workers check box` passes.
  3. `nw queue gates <campaign>`: the table title names `box`.
  4. Let one real campaign gate there, including its close re-gate.
  5. Pull the network mid-gate: `worker_failed` and the local rerun.
  6. Record gate minutes per campaign before and after in As shipped: the SPEC-0310
     baseline.
- **Regression guard:** full `uv run pytest`; existing gate, gate-slot and close tests run
  unchanged with no registry.

### Test record (2026-11-01)

- Golden first: `tests/data/remote_gates_golden.json` was recorded with `NW_RECORD_GOLDEN=1`
  against the unmodified `run_all` (a fake project with an untracked file and
  `remote_gates: false`, no registry). After the change it still matches for no registry, an
  empty file and an empty `workers` list, and no `finding` is appended.
- `uv run pytest -q tests/test_remote_gates.py`: 26 passed. Covered: remote pass/fail, the
  retry ladder (`.r1` files) with the coordinator's files byte-identical to the worker's
  `frames.jsonl`, the `merge-base $NW_BASE HEAD` gate, the stash commit of a dirty tree with
  the stash stack untouched, the three ineligibility cases (one `finding` over two runs,
  `gates`/`info` outside divergence's sets), a second run's smaller bundle without the base
  ref, HEAD equal to the base gated twice (`bundle_bytes: 0`), transport failures (host
  unreachable, `gate-job` exit 255, `have` failing, garbage frames, a drop after the first
  `gate` frame, coordinator timeout) each with `worker_failed`, a cooldown, a local run under
  seq 1 and no remote files or `refs/nw/gate-jobs/*` left. Also: duplicate job id, a failing
  `git bundle create` and a worker slot wait past `slot_wait_s` falling back without
  cooldown; a worker slot held past `timeout_s` + margin not tripping the run timeout; a stop
  during the fallback local wait (interrupted, claim cleared); a stop mid-run killing the
  worker's `sleep 600`; `nw queue gates --worker` on a cooled-down worker, `--local`, an
  unknown worker, an ineligible `--worker` run; `nw campaign show` naming the worker.
- `python3 tools/affected_tests.py --run uv run pytest -q -x -n 4`: the selection came out as
  the whole suite (4762 passed, 3 skipped). `python3 tools/spec_lint.py`: OK.
- Review fixes (loop 2): `uv run pytest -q tests/test_remote_gates.py`: 35 passed. Added: an
  `error` frame with stage `checkout` (worker_failed, cooldown, local run under seq 1); an
  `error` frame with stage `validate` (fallback without cooldown); gate names the worker
  refuses (`unit tests`, `lint:ruff`, `py/unit`, `ok` + `ok.r1`) staying local with one
  `finding`, nothing bundled or sent, no cooldown, and `--worker` erroring; a base name the
  worker refuses and a seq of 1000 staying local; an `error` frame and a garbage frame after a
  stop ending the run as interrupted with no `worker_failed`. Each new test was checked to
  fail with its fix reverted. `uv run pytest -q -n 4` over the gate, gate-slot, worker and
  workers test files: 359 passed. The full suite is left to the gate.
- Review fixes (loop 3): `uv run pytest -q -n 4 tests/test_remote_gates.py`: 47 passed. Added: a
  drop after a gate frame carrying the worker's `.r1` files of a gate that failed remotely and
  passes locally on its first try (no `001-g.r1.*` left, every `.json` with the local cwd); a
  direct test of `job_timeout_s` (budget + Σ `timeout_s` x (1 + `retries`), the gate's 1200 s
  default, the 4 h cap); a worker-side interruption (the loopback `gate-job` getting SIGTERM
  mid-gate, and a fake `done` with `interrupted: true`) falling back with `worker_failed`
  stage `interrupted`, a cooldown and a local run under seq 1; malformed `slot_wait_s`,
  `duration_s`, `ok` and `interrupted` values in `started`/`done` falling back as `protocol`;
  a registry whose only worker is disabled gating locally with no `finding` despite an
  untracked file, and `--worker` on it erroring; a fallback run's `slot_wait_s` including the
  fallback's local wait. The deletion, interruption and malformed-value tests were checked to
  fail with their fix reverted. The full suite is left to the gate.
- Review fixes (loop 4): `uv run pytest -q -n 4 tests/test_remote_gates.py tests/test_worker.py
  tests/test_worker_ssh_command.py tests/test_agentic_dogfood_config.py
  tests/test_agentic_project.py`: 273 passed (`test_remote_gates.py`: 54). Added: a gate that
  writes a tracked file and a new file, run with a worker, falls back with `worker_failed` stage
  `tree` and no cooldown, and leaves the coordinator's worktree (`git status` and file contents)
  identical to a twin repo gated with no worker at all; a generator whose output is already
  current stays remote (`changed: []`); `tree_changes` lists a rename once and a path with a
  space; a `done` without `changed`, with a non-list or non-string `changed`, and a gate frame
  whose `attempts` is not iterable each fall back as `protocol`. The tree and malformed-frame
  tests were checked to fail with their fix reverted. The full suite is left to the gate.
- Manual verification (steps 1-6, the second machine) has **not** been run: it needs the
  human's machine. The last criterion stays open and the spec stays `in-progress` until then.

### As shipped (2026-11-01)

- **`gates.remote` is the top-level `remote_gates: false`.** `gates:` in `.nw/project.yaml`
  is a list, so it cannot carry a `remote` key. The project opt-out is a top-level boolean
  `remote_gates` (default true); the per-gate `remote: false` is as designed. The `remote`
  key is stripped from the gate list sent in the job header, so the worker's validation is
  unchanged.
- An unreachable host fails at the `have` call, before `gate-job`, so its `worker_failed`
  says `stage: "have"`. A `gate-job` connection that ends before `done` says `stage: "ssh"`,
  malformed frames `protocol`, the coordinator timeout `timeout`, the slot wait `slot_wait`,
  and an `error` frame carries the worker's stage. `worker_failed` also carries `cooldown`.
- A worker removed from the registry between the slot and the dispatch falls back without
  cooldown (`stage: "registry"`). A claimed seq outside 0..999 (the header's range) stays
  local, with the same one-time `finding`.
- `worker_failed` is projected into the queue events and held by the quiet conductor
  (`quiet.RULES`, the nw-conduct skill's held list). docs/WORKFLOW.md ("Remote gate worker")
  describes dispatch, the opt-outs and `--worker`/`--local`.
- **A header the worker would refuse stays local.** Eligibility also builds a dry job header
  (placeholder shas and cache key) and runs the worker's own `validate_header` on it. A gate
  name outside `[A-Za-z0-9._-]+`, a gate name that collides with another gate's retry files,
  a base branch `base_name_ok` refuses, or a seq outside 0..999 is ineligible: one
  `finding` (title "the worker would refuse this job", detail the refusal), and `--worker`
  errors. These come from the coordinator's own config and would repeat on every run, so they
  must not cost the worker a cooldown. As a backstop, an `error` frame with stage `validate`
  falls back **without** cooldown, like `claim`.
- Once a stop has closed stdin, an `error` frame or a malformed frame ends the run as
  interrupted, with no `worker_failed`, no cooldown and no local re-run.
- The `worker_slot_wait_s` bound before `started` restarts when the header and bundle have
  been written, so upload time does not count against the worker's slot wait. The upload
  itself is still bounded by `worker_slot_wait_s` from the start.
- A `done` frame with `interrupted: true` that no coordinator stop asked for (the worker got
  SIGTERM/SIGINT/SIGHUP: its host is going away) is a transport failure, `stage:
  "interrupted"`, with cooldown and the local re-run under the same seq. After a stop it stays
  an interrupted `GateRun`. Worker-supplied `slot_wait_s`/`duration_s` must be finite,
  non-negative numbers and `ok`/`interrupted` booleans (`ok` required); anything else is a
  `protocol` failure.
- **A gate's writes to the tree re-run the gates locally.** The worker's checkout is
  throwaway and only logs come back, so a gate that writes the tree (`spec_lint.py
  --write-ledger` in this repo's own spec-lint gate, a formatter, a code generator) would
  otherwise pass on the worker while the coordinator commits the unwritten tree. The worker's
  `done` frame (SPEC-0336's `_run_gates`) now carries `changed`: the paths `git status
  --porcelain --untracked-files=all` reports in its checkout after the gates (at most 20;
  `null` when git cannot tell). A non-empty list is a fallback **without** cooldown
  (`worker_failed` stage `tree`) and the local re-run writes the worktree as it always has; a
  missing or malformed `changed` is a `protocol` failure (an outdated worker). The dogfood
  `.nw/project.yaml` keeps spec-lint remote-eligible: a current ledger is rewritten unchanged
  (no fallback), a stale one sends the run home. docs/WORKFLOW.md says so and that a gate that
  writes the tree on every run should set `remote: false`.
- A gate frame whose result builds but whose `attempts`/`flaky` is not iterable is a
  `protocol` failure, not an exception out of `run_all`.
- The run-timeout margin (`RUN_TIMEOUT_MARGIN_S`, 120 s) and `job_timeout_s` are module-level
  in `remote_gates.py`, so tests can shrink them.

## Clock Log

Use `nw idea clock-in IDEA-558` / `nw idea clock-out IDEA-558`.

## Rollout / migration

1. The code ships inert: no `workers.json` means no change.
2. The human registers the second machine (the manual steps above).
3. `nw workers off box` (SPEC-0337) returns gates to local at any time; in-flight remote
   runs finish.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

None. Decided while writing this spec:
- The SSH connection is the job's lifetime in this slice; a dropped gate is re-run locally.
- Code and the base move as bundles, not through rsync or a shared remote.
- The seq is claimed before the slot, so fallback reuses it.
- The coordinator falls back to local without a remote retry; cooldown 600 s.
- The worker's slot wait is bounded separately (`worker_slot_wait_s` 300 s) from the run
  timeout, which starts at `started`.
- `gates.remote: false` and per-gate `remote: false` opt out.
- Eligibility runs only when a remote pool exists; the ineligibility `finding` uses
  `persona: gates`, `severity: info`, outside divergence's counted sets.
- Coordinator-side git failures fall back without cooldown; a `have` failure is a transport
  failure.
