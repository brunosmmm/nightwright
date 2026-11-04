---
id: SPEC-0336
title: "Gate worker job runner and forced-command dispatcher"
status: accepted
owner: user
created: 2026-10-31
updated: 2026-10-31
source_idea: IDEA-564
parent: SPEC-0310
tags: [agentic, remote, workers, gates, security]
---

## Context

[SPEC-0310](./0310-remote-workers-run-gates-persona-runs-and.md) runs gates on a second
machine over SSH. Its first slice was reviewed as one spec (SPEC-0331), and the review
recommended splitting it into three parts, ordered worker, then registry and pools, then
dispatch. This spec is the **worker** part: everything that runs on the worker host. It has
no coordinator logic and can be tested by piping a job into `nw worker gate-job` locally.

A gate run today is `gates.run_all` (`src/nightwright/agentic/gates.py:386`), which takes a
gate slot (`gate_slots.acquire`, `gate_slots.py:162`) and runs each gate through
`run_gate_with_retries` (`gates.py:275`) in the campaign worktree. Each gate writes
`gates/<seq>-<name>.log` and a `nw.gate.v1` sidecar `.json` (plus `.r<n>` retry files). The
worker reuses `run_gate_with_retries` unchanged.

## Goals / Non-goals

**Goals**
- `nw worker gate-job` reads one `nw.job.v1` header and a git bundle on stdin, checks the
  commit out in a throwaway worktree of a per-repo object cache, runs the gate list it was
  sent, and streams `nw.job.frame.v1` frames on stdout.
- The gates see the same base branch names a local run sees, so `$NW_BASE`, `{base}` and the
  affected-tests gate (SPEC-0309) work.
- The SSH key a worker accepts reaches only the worker subcommands (`nw worker
  ssh-command`).
- An honest threat model: because `gate-job` runs the commands it is sent, the key is
  equivalent to shell access as the worker user. Containment is a dedicated unprivileged
  user, and the docs make that mandatory.

**Non-goals**
- The registry, `nw workers`, slot pools: [SPEC-0337](./0337-worker-registry-and-multi-pool-gate-slots.md).
- Choosing a worker, bundling, fallback and reporting on the coordinator:
  [SPEC-0338](./0338-remote-gate-dispatch-with-local-fallback.md).
- Jobs that outlive the connection: [SPEC-0332](./0332-worker-owned-async-jobs.md).

## Decision

Add a `nw worker` command group (logic in `src/nightwright/agentic/worker.py`, click
commands in `cli_worker.py`, registered beside `queue`) with `info`, `have`, `gate-job`, `gc`
and `ssh-command`. The job protocol is one JSON header line, then exactly `bundle_bytes`
bytes of bundle, on stdin; stdin stays open and EOF before `done` means cancel. Output is
one JSON frame per line on stdout.

## Design

### The job header (`nw.job.v1`, kind `gate`)

```
{schema: "nw.job.v1", job_id, kind: "gate", campaign, spec_id, repo_key, commit: sha,
 base: {name, sha}, have: [...], bundle_bytes, env, timeout_s, attempt: 0,
 payload: {seq, tier, gates: [...resolved gate dicts...], gate_budget_s}}
```

- `job_id` and `repo_key` match `[A-Za-z0-9._-]+`.
- `gates` is the already-resolved list (the coordinator applies `campaign_gate_config` and
  `gates_for(tier)`). The worker never reads `.nw/project.yaml`.
- `env` is an **allowlist**: `NW_CAMPAIGN`, `NW_SPEC_ID`, `NW_BASE`. Any other key is refused
  with an `error` frame (`stage: "validate"`), and nothing runs. The worker sets
  `NW_WORKTREE` (its tree), `NW_DATA_DIR` and `NW_CONFIG_DIR` (its own) and `TMPDIR` (a
  per-job dir). Nothing else from the coordinator crosses.
- `base` is the coordinator's `repo.base` name and the sha it resolved to. The bundle
  contains `refs/nw/gate-jobs/<job_id>-base` at that sha as well as the job ref, except when
  the worker already has the object (SPEC-0338 leaves such refs out). An empty bundle is
  sent as `bundle_bytes: 0`, and the worker then skips the fetch.
- `schema` must equal `nw.job.v1`; any other value gives `error` with `stage: "validate"`.
  That string is the protocol check.
- `timeout_s` is the coordinator's own wall-clock budget, and the worker ignores it. The gate
  deadline is the slot hold time plus `payload.gate_budget_s`.
- `have` is informational: the shas the coordinator assumed were present when it cut the
  bundle. The worker does not read it. A missing prerequisite or object surfaces as `error`
  with `stage: "checkout"`.
- `attempt` is only echoed into `job.json`.
- A header `NW_BASE`, if present, must equal `base.name`, else `stage: "validate"`.

### Frames (`nw.job.frame.v1`)

| `type` | Payload |
|---|---|
| `accepted` | none |
| `slot_wait` | `waited_s` (emitted once the wait passes 30 s) |
| `started` | `slot_wait_s` (the worker now holds its slot) |
| `gate` | `result` (`asdict(GateResult)`), `files` (`{name: gzip+base64}`) |
| `done` | `ok`, `started_at`, `duration_s`, `slot_wait_s`, `interrupted` |
| `error` | `stage`, `error` |

### Worker store

Under `<worker data_dir>/agentic/worker/`:
- `repos/<repo_key>.git`: bare object caches;
- `repos/<repo_key>.lock`: an `fcntl.flock` lock. The bundle fetch, setting
  `refs/nw/have/*`, updating `repos/<repo_key>.have.json` (written to a temp file, then
  `os.replace`) and `gc`'s deletions and pruning all happen under it;
- `jobs/<job_id>/`: `job.json`, `job.lock`, `tree/`, `out/`, `frames.jsonl` (a copy of
  every frame sent), `tmp/`. Every gate-job holds a flock on `job.lock` from the claim until
  it exits, and `gc` never deletes a job dir whose `job.lock` is held.

### `gate-job` steps

1. **Validate** the header: schema, protocol major, kind `gate`, charsets, env allowlist.
2. **Claim** `jobs/<job_id>/` with an exclusive `mkdir`. An existing dir is a duplicate job:
   `error` with `stage: "claim"`.
3. **Check out.**
   - Under the repo lock: `git init --bare` the cache if absent; read the bundle into the
     job dir; `git fetch <bundle> 'refs/nw/gate-jobs/*:refs/nw/gate-jobs/*'` (skipped when
     `bundle_bytes` is 0); set `refs/nw/have/<sha>` for the job's commit and its base sha,
     so the objects stay reachable after the job refs are deleted (the coordinator's `have`
     query relies on them); update `have.json`.
   - `git clone --shared --no-checkout repos/<repo_key>.git <job>/tree`. The objects are
     read through alternates and the refs are **private to the job**, so concurrent jobs
     for one repo never share branch refs. (A `git worktree add` would put `refs/heads/*`
     in the shared cache, where two jobs with different bases would overwrite each other's
     base branch.) Then `git -C <job>/tree checkout --detach <commit>`.
   - Inside `<job>/tree`, create `refs/heads/<name>` at `base.sha`, and also
     `refs/remotes/origin/<rest>` when `name` is `origin/<rest>`. `NW_BASE` and `{base}`
     therefore resolve exactly as they do locally, and `git merge-base $NW_BASE HEAD` works.
4. **Accept and take a slot.** Emit `accepted`. Take the worker host's own gate slot
   (`gate_slots.acquire` on the worker's data dir, campaign id = the job's campaign), so the
   worker's own queue, if any, and remote jobs share its CPU budget. Emit `slot_wait` if the
   wait passes 30 s, and `started` once the slot is held. The 30 s threshold is read from
   `NW_WORKER_SLOT_WAIT_EVENT_S` (default 30), so tests can lower it. `cancel_event` is
   passed as `stop=` to `gate_slots.acquire`, so EOF during the wait ends it; the worker
   then emits `done` with `interrupted: true` and no `started`. The gate deadline is the
   slot hold time plus `payload.gate_budget_s`, as `_run_all_held` does.
5. **Run each gate**: `run_gate_with_retries(gate, tree, env, <job>/out/<seq>-<name>.log,
   placeholders, log_rel="gates/<seq>-<name>.log", stop=cancel_event, deadline=...)`.
   - Placeholders: `{worktree: <job>/tree, base: base.name, spec_id: header.spec_id,
     campaign: header.campaign}`.
   - Env: the worker process's own environment (PATH, HOME), plus the allowlisted header
     keys, plus `NW_WORKTREE`, `NW_DATA_DIR`, `NW_CONFIG_DIR` and `tmp_env(<job>/tmp)`.
   After each gate emit one `gate` frame with the
   `GateResult` and every file the gate wrote (`.log`, `.r<n>.log` and their `.json`
   sidecars). A log over 16 MiB is cut to its last 16 MiB and its sidecar gets
   `"truncated_bytes": n`.
6. **Finish.** Emit `done`, `rm -rf <job>/tree`, delete the job refs under the repo lock,
   keep `job.json` and `frames.jsonl`, run `gc`.

A reader thread watches stdin after the bundle. EOF before `done` sets `cancel_event` and
calls `gates.terminate_all()`; the worker emits `done` with `interrupted: true` if it can and
exits. Any exception becomes an `error` frame `{stage, error}`.

Exit codes: 0 after emitting `done`, whatever `ok` is (including `interrupted: true`); 3
after an `error` frame at any stage, including `validate` and `claim`. gate-job never exits
without one of the two frames unless stdout is closed. The coordinator (SPEC-0338) uses
this to tell a transport failure from a gate failure.

### Other subcommands

- `nw worker info [--json]`: `{schema: "nw.worker.info.v1", protocol: "nw.job.v1", version,
  git_sha, data_dir, git, uv, free_bytes}`.
- `nw worker have --repo KEY SHA...`: prints the given shas present in the cache. `have`
  validates `--repo` against the `repo_key` charset itself.
- `nw worker gc [--keep-days 2]`: deletes job dirs (except those whose `job.lock` is held)
  and `refs/nw/have/*` refs older than N days. Each `have` ref's last-used time is kept in
  `repos/<repo_key>.have.json` (updated whenever a job sets or reports the ref). Then, under
  the repo lock, it runs `git -C repos/<repo_key>.git gc --prune=now --quiet` for each repo
  whose refs changed, so deleted refs actually free their objects.
- `nw worker ssh-command`: the forced command. It reads `SSH_ORIGINAL_COMMAND`, `shlex.split`s
  it, and `exec`s the worker's own `nw` only when the words are `nw worker <sub> <args>` (the
  first word may be `nw` or any path ending in `/nw`) and the args match this grammar
  exactly:
  - `info` [`--json`];
  - `have` `--repo` `<[A-Za-z0-9._-]+>` `<[0-9a-f]{40,64}>`...;
  - `gate-job` (no arguments);
  - `gc` [`--keep-days` `<[0-9]+>`].
  Any other token (another subcommand, `;`, an empty command, `nw queue ...`, unknown
  options, `--repo ../x`) exits 2 with `refused: <reason>` on stderr and runs nothing.

### Docs

`docs/WORKFLOW.md` gets a "Remote gate worker" section:
- **Mandatory**: a dedicated, unprivileged user on the worker with no sudo and no access to
  other data. The key is equivalent to shell access as that user, because a gate is a
  command.
- `uv tool install` of nightwright as that user.
- The key line, with an absolute path:
  `command="/home/user/.local/bin/nw worker ssh-command",restrict ssh-ed25519 ...`
  (`restrict` disables forwarding and the pty).

## Alternatives considered

- **Let the worker read the gate list from the checked-out `.nw/project.yaml`.** It would
  diverge from the coordinator's campaign overrides. Sending the resolved list keeps the
  worker dumb.
- **Fetch the base from a remote on the worker.** Needs credentials; the base ref rides in
  the bundle instead, costing nothing when the worker already has its objects.
- **Claim the threat model is "only worker subcommands".** True of the SSH surface but
  misleading, since a gate is arbitrary code. The spec says so and makes the dedicated user
  mandatory.

## Acceptance criteria

- [ ] Piping a valid header and bundle into `nw worker gate-job` runs the sent gates in a
      job worktree and emits `accepted`, `started`, one `gate` frame per gate (with the
      `.log`, `.json` and `.r<n>` files) and `done`.
- [ ] A gate running `git merge-base $NW_BASE HEAD` on the worker succeeds and prints the
      same sha it prints in the coordinator's worktree.
- [ ] A header whose `env` has a key outside the allowlist yields an `error` frame with
      `stage: "validate"`, and nothing runs. A reused `job_id` yields `stage: "claim"`.
- [ ] Two concurrent gate-jobs for the same `repo_key` with different `base.sha` for the
      same `base.name` each see their own base in `git rev-parse $NW_BASE`.
- [ ] EOF on stdin mid-gate ends the gate process and emits `done` with `interrupted: true`;
      EOF during the slot wait emits `done` with `interrupted: true` and no `started`.
- [ ] With the worker's slot held by another process, the worker emits `slot_wait` and then
      `started` only once the slot frees.
- [ ] gate-job exits 0 after `done` (pass, fail or interrupted) and 3 after an `error` frame.
- [ ] A log over 16 MiB arrives cut to 16 MiB with `truncated_bytes` in its sidecar.
- [ ] `nw worker have` reports the job's commit and base after the job ends (the
      `refs/nw/have/*` refs). After `nw worker gc --keep-days 0`, `have` reports none of
      them, `git cat-file -e <commit>` fails in the cache, and the job dirs are gone, except
      that a live job's dir survives a concurrent `gc`.
- [ ] `nw worker info --json` prints the documented keys.
- [ ] `nw worker ssh-command` execs the four allowed subcommands and refuses everything else
      with exit 2, running nothing.
- [ ] `docs/WORKFLOW.md` documents the mandatory dedicated user and the absolute-path key
      line.

## Test plan

- **Automated** (`tests/test_worker.py`, offline): a temp git repo with a fake project; the
  test builds a header and bundle itself and runs `nw worker gate-job` as a subprocess with
  `NW_DATA_DIR`/`NW_CONFIG_DIR` pointing at a temp worker data dir. Cases: pass, fail, retry
  ladder (`retries: 1`, a gate that fails once, `.r1` files in the frames), base-ref
  `merge-base` gate, two concurrent jobs with different bases for one `base.name`, env
  refusal, a bad `schema`, duplicate `job_id`, an empty bundle (`bundle_bytes: 0`) against a
  warm cache, stdin EOF during a `sleep 600` gate (waited with `tests/waits.py`), held worker
  slot gives `slot_wait`/`started` (with `NW_WORKER_SLOT_WAIT_EVENT_S=0.5`), EOF while
  waiting for the slot, exit codes, 16 MiB truncation (a gate writing 17 MiB), `have` after
  the job, `gc --keep-days 0` (refs and objects gone), `gc` during a live `sleep` job (its
  dir survives), `info --json`.
- **Automated** (`tests/test_worker_ssh_command.py`): allowed commands are exec'd (with
  `os.execvp` patched); refused: other subcommands, shell metacharacters, empty
  `SSH_ORIGINAL_COMMAND`, `nw queue run`, unknown options, `have --repo ../x`.
- **Manual:** check that `docs/WORKFLOW.md` has a "Remote gate worker" section stating that
  the dedicated unprivileged user is mandatory and showing the
  `command="/home/user/.local/bin/nw worker ssh-command",restrict` key line.
- **Regression guard:** full `uv run pytest`; nothing outside `nw worker` changes.

## Clock Log

Use `nw idea clock-in IDEA-564` / `nw idea clock-out IDEA-564`.

## Rollout / migration

Inert until a coordinator sends it a job (SPEC-0338). Installing it on a worker is the manual
`uv tool install` documented above.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`).
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

None. Decided while writing this spec:
- The worker gets the base as a ref in the bundle plus `base: {name, sha}` in the header.
- `refs/nw/have/<sha>` keeps cached objects reachable; `gc` ages them out.
- `started` marks the slot hold, so the coordinator's timeout excludes the worker's queueing.
- Each job gets a `git clone --shared` of the cache (private refs) rather than a worktree,
  per the review: worktrees share `refs/heads/*`.
- The cache, `have.json` and `gc` are serialized by a per-repo flock; `gc` prunes objects.
- Each log is capped at 16 MiB.
