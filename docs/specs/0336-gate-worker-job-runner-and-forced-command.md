---
id: SPEC-0336
title: "Gate worker job runner and forced-command dispatcher"
status: done
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
  `command="/home/nw/.local/bin/nw worker ssh-command",restrict ssh-ed25519 ...`
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

- [x] Piping a valid header and bundle into `nw worker gate-job` runs the sent gates in a
      job worktree and emits `accepted`, `started`, one `gate` frame per gate (with the
      `.log`, `.json` and `.r<n>` files) and `done`.
- [x] A gate running `git merge-base $NW_BASE HEAD` on the worker succeeds and prints the
      same sha it prints in the coordinator's worktree.
- [x] A header whose `env` has a key outside the allowlist yields an `error` frame with
      `stage: "validate"`, and nothing runs. A reused `job_id` yields `stage: "claim"`.
- [x] Two concurrent gate-jobs for the same `repo_key` with different `base.sha` for the
      same `base.name` each see their own base in `git rev-parse $NW_BASE`.
- [x] EOF on stdin mid-gate ends the gate process and emits `done` with `interrupted: true`;
      EOF during the slot wait emits `done` with `interrupted: true` and no `started`.
- [x] With the worker's slot held by another process, the worker emits `slot_wait` and then
      `started` only once the slot frees.
- [x] gate-job exits 0 after `done` (pass, fail or interrupted) and 3 after an `error` frame.
- [x] A log over 16 MiB arrives cut to 16 MiB with `truncated_bytes` in its sidecar.
- [x] `nw worker have` reports the job's commit and base after the job ends (the
      `refs/nw/have/*` refs). After `nw worker gc --keep-days 0`, `have` reports none of
      them, `git cat-file -e <commit>` fails in the cache, and the job dirs are gone, except
      that a live job's dir survives a concurrent `gc`.
- [x] `nw worker info --json` prints the documented keys.
- [x] `nw worker ssh-command` execs the four allowed subcommands and refuses everything else
      with exit 2, running nothing.
- [x] `docs/WORKFLOW.md` documents the mandatory dedicated user and the absolute-path key
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
  `command="/home/nw/.local/bin/nw worker ssh-command",restrict` key line.
- **Regression guard:** full `uv run pytest`; nothing outside `nw worker` changes.

### Test record (2026-10-31)

- `uv run pytest -q -n 4 tests/test_worker.py tests/test_worker_ssh_command.py
  tests/test_gate_slots.py tests/test_cli_gate_slots.py tests/test_agentic_gates.py
  tests/test_completion.py`: 142 passed, three runs in a row. `tests/test_worker.py` covers
  every case the plan lists (pass, fail, retry ladder with `.r1` files, `merge-base`/`{base}`,
  two concurrent jobs with different bases for `main`, env refusal, bad `schema` and an
  `NW_BASE` mismatch, duplicate `job_id`, empty bundle on a warm cache plus a cold-cache
  checkout error, EOF mid `sleep 600`, held slot with `NW_WORKER_SLOT_WAIT_EVENT_S=0.5`, EOF
  during the slot wait, exit codes, 17 MiB truncation, `have` after the job, `gc --keep-days 0`
  with `git cat-file -e` failing, `gc` during a live job, `info --json`).
  `tests/test_worker_ssh_command.py` covers the six allowed forms and 24 refusals, including
  `have --repo ../x`.
- CLI-related files (`tests/test_*cli*.py`, permissions, project config): 171 passed.
- `python3 tools/spec_lint.py`: OK after `--write-ledger`.
- Manual: `docs/WORKFLOW.md` has a "Remote gate worker" section. It says the dedicated
  unprivileged user is mandatory and shows the
  `command="/home/nw/.local/bin/nw worker ssh-command",restrict` key line. Also ran
  `nw worker info --json` and `SSH_ORIGINAL_COMMAND="nw worker info" nw worker ssh-command`
  (exec'd) and `SSH_ORIGINAL_COMMAND="nw queue run"` (exit 2) by hand.
- The full `uv run pytest` regression run is left to gating.
- Loop 2 (gate fix): added the `worker` row to `docs/product/reference/index.md`.
  `tests/test_docs_*.py` + `tests/test_completion.py`: 74 passed. Other tests that walk the
  CLI (`test_agentic_detach`, `test_agentic_personas`, `test_hub`, `test_agentic_queue_cli`,
  `test_workflow`, `test_spec_status`, `test_json_cli`, `test_skills`,
  `test_idea_capture_guard`): 297 passed.
- Loop 3 (gate fix): allowlisted the `nw worker` machine/protocol raw writes in
  `tests/test_rich_output_guard.py` (`worker_info`, `worker_have`, `worker_gc`, `gate_job`,
  `_fail`, `ssh_command`). `tests/test_rich_output_guard.py`: 3 passed. Full
  `uv run pytest -q -n 4`: 4528 passed, 3 skipped.
- Loop 4 (review fixes): `uv run pytest -q -n 4 tests/test_worker.py
  tests/test_worker_ssh_command.py tests/test_gate_slots.py tests/test_cli_gate_slots.py
  tests/test_agentic_gates.py tests/test_rich_output_guard.py`: 157 passed, three runs in a row.
  New cases: 25 bad-header refusals (job_id/repo_key/campaign/spec_id outside the charset or
  with a trailing newline, kind, commit, base, seq, gate names `../../evil` and `a/b`, bad
  `timeout_s`/`cwd`/`retries`/`results`, an unknown gate key, duplicate gate names), each exit 3,
  stage `validate`, only an `error` frame, no job dir and no repo cache; stdin short of
  `bundle_bytes` (stage `read`, exit 3, the job dir keeps only `job.json`/`frames.jsonl`/
  `job.lock` and gc then removes it); the worker's own `NW_CAMPAIGN`/`NW_SPEC_ID`/`NW_BASE`
  never reach a gate; EOF mid-gate, SIGTERM and SIGINT mid-gate each kill the gate's
  `sleep 600` child (checked by pid) and send `done` interrupted; SIGTERM before the slot gives
  stage `signal`; truncation keeps the tail (an end marker survives, a head marker is cut);
  gc removes orphan job refs, keeps fresh have refs and the job dir under the default
  `--keep-days 2`, and removes them once aged three days; gc keeps the refs and objects of a
  job claimed between its scan of `jobs/` and its repo phase; the slot waiter reads
  `remote:C-1`; `own_nw()` returns an executable `nw` argv[0] and ssh-command execs it;
  ssh-command refuses quoted words ending in a newline.
  `python3 tools/spec_lint.py`: OK. The full suite is left to gating.
- Loop 5 (review fixes): `uv run pytest -q -n 4 tests/test_worker.py
  tests/test_worker_ssh_command.py tests/test_gate_slots.py tests/test_cli_gate_slots.py
  tests/test_agentic_gates.py tests/test_rich_output_guard.py tests/test_test_waits.py`:
  163 passed, three runs in a row. The concurrent-jobs test now pins `slots: 2`. New cases: a
  SIGTERM delivered while the have-ref `update-ref` runs under the repo lock (in-process
  `gate_job`) lets that git finish (the ref exists, no `.lock` is left), releases the repo lock,
  then ends with stage `signal`, exit 3, and its cleanup does not hang; stale
  `refs/nw/have/<sha>.lock` and `packed-refs.lock` files in the cache are cleared by the next
  job, which passes. `python3 tools/spec_lint.py`: OK. The full suite is left to gating.
- Loop 6 (review fix): the same targeted set, now 165 tests, ran 43 times; 42 runs passed. One
  early run had a single failure that did not show up again in the next 42 runs (with `-n 4`,
  `-n 6` and `-n 8`), so it was not identified. New cases: a cache set up the way an older
  cache could be (`gc.autoPackLimit=1`, `gc.auto=6700`, `maintenance.auto=true`, and
  `fetch`/`transfer.unpackLimit=1` so each fetch keeps its own pack) gets two more
  `_checkout`s. Afterwards there is no `gc.pid` or `gc.log`, at least two packs are left (no
  gc repacked them), and the config reads `gc.auto=0` and `maintenance.auto=false`. With the
  fix reverted, this test fails because auto gc merges the packs into one. A second new case:
  when `git gc` fails, the repo is not listed in `pruned`. `python3 tools/spec_lint.py`: OK.
  The full suite is left to gating.
- Loop 7 (review fixes): new cases in `tests/test_worker.py`: an `origin/main` base
  (`merge-base "$NW_BASE" HEAD`, `{base}` and `refs/remotes/origin/main` all give the
  coordinator's shas, no ambiguity warning); `NW_WORKTREE`/`NW_DATA_DIR`/`NW_CONFIG_DIR` are the
  worker's own and `TMPDIR` is `jobs/<id>/tmp`; a gate running `cat` gets EOF at once (`n=0`)
  while the job stream stays open, and the stream's EOF still cancels the next gate; the retry
  ladder is bounded by a small `payload.gate_budget_s`, by the worker's own `gate_budget_s`
  when the payload has none (both directions), and the budget clock starts at the slot hold
  (2.5 s of slot wait do not count against a 2 s budget); SIGHUP mid-gate joins SIGTERM and
  SIGINT; `have` on a repo with no cache prints nothing and creates nothing; `have`
  re-creates a deleted `refs/nw/have/<sha>`, refreshes stale last-used times, and a
  `gc --keep-days 1` afterwards keeps them; `gc --keep-days` with 400 digits exits 0 and keeps
  everything; the cache fetch passes neither git 2.29 flag; refusals for `x.r1` beside `x`,
  `NaN`/`Infinity`/`0` budgets, NUL in an env value and in a gate cmd; `docs/WORKFLOW.md` has
  the "Remote gate worker" section with the mandatory dedicated user and the key line. With
  each fix reverted in turn, the origin, slot-hold and stdin tests fail.
  `uv run pytest -q -n 4 tests/test_worker.py`: 71 passed. The targeted set (now 184 tests) passed in all 12 repeat runs (`-n 4`, `-n 6`, `-n 8`), with no failure seen; the loop 6 one-off failure was not reproduced.
  `python3 tools/spec_lint.py`: OK. The full suite is left to gating.

- Loop 8 (review fixes): new cases in `tests/test_worker.py`: base names git refuses
  (`main.lock`, `feature/`, `a//b`, `x/.y`, `main.`, `a/b.lock/c`) are `validate` refusals with
  no job dir and no cache (the base-name cases now also set `env.NW_BASE`, so the NW_BASE
  mismatch check no longer refuses them first); `base_name_ok` agrees with
  `git check-ref-format` on 20 names; a SIGTERM handled right after `_checkout` returns gives
  `error` stage `signal`, exit 3, and one handled once the stage is `slot` gives `done`
  interrupted, exit 0; a cache with a garbage `config` sorting before `proj` is listed in
  `errors`, `nw worker gc --keep-days 0` exits 1, and `proj` is still pruned (refs and objects
  gone). The budget tests no longer depend on wall-clock margins: in the "no rerun" cases the
  first run's 1 s sleep alone exceeds the 0.2 s budget, and the slot-hold case waits 4.5 s
  for the slot against a 4 s budget with two 0.1 s runs (about 3.8 s of slack). With the
  `src/` changes reverse-applied, the signal, broken-cache and six base-name cases fail.
  The targeted set (`tests/test_worker.py tests/test_worker_ssh_command.py
  tests/test_gate_slots.py tests/test_cli_gate_slots.py tests/test_agentic_gates.py
  tests/test_rich_output_guard.py tests/test_test_waits.py`, 213 tests) passed in repeat runs
  with `-n 4`, `-n 6` and `-n 8`. `python3 tools/spec_lint.py`: OK. The full suite is left to
  gating.
- Loop 9 (review fixes): new cases in `tests/test_worker.py`:
  - With stdout full and unread (a 3 MB random `gate` frame), SIGTERM or stdin EOF still ends
    the job: exit 0, the next gate never runs, and the worker's slot can then be taken.
  - With `NW_WORKER_FRAME_STALL_S=1` and no signal and no EOF, the job gives up on stdout
    ("stdout stalled" on stderr), cancels the next gate and exits 0.
  - The finish gc runs detached: the job exits 0 while another cache's repo lock is held, and
    the orphan job ref is gone once the lock is released.
  - `job_id` `.` and `..`, `repo_key` `..` and `campaign` `...` are `validate` refusals.
  In `tests/test_worker_ssh_command.py`:
  - The allowed-command cases now patch only `os.execvp`, with `sys.argv[0]` set to an
    executable tmp `nw`, and assert the exec argv is that path plus `worker <sub>` for all
    six forms.
  - `nw worker ssh-command` run as a subprocess exits 2 with `refused:` and an empty stdout
    for `nw queue run`, `info; id` and an empty command, and execs `/elsewhere/nw worker info
    --json`, which prints `nw.worker.info.v1`.
  With the old `worker.py` restored, all 8 new `test_worker.py` cases fail.
  Test hygiene: in-process `gate_job` tests could leave `gates.terminate_all()`'s sticky stop
  set in the pytest process (from a helper thread started by the slot-stage signal or the
  stdin watcher). Later gate tests in the same xdist worker then saw `interrupted`; this is
  where the occasional `test_gate_slots`/`test_agentic_gates` failure under `-n 8` came from,
  and is probably the loop 6 one-off. The test_worker autouse fixture now waits for those
  threads and calls `gates.reset()`.
  Targeted set (`tests/test_worker.py tests/test_worker_ssh_command.py tests/test_gate_slots.py
  tests/test_cli_gate_slots.py tests/test_agentic_gates.py tests/test_rich_output_guard.py
  tests/test_test_waits.py`, 225 tests): 8 of 8 runs passed after that fix (`-n 4/6/8`).
  `python3 tools/spec_lint.py`: OK. The full suite is left to gating.


### As shipped (2026-10-31)

- The bundle is read from stdin into `jobs/<id>/job.bundle` **before** the repo lock is taken.
  Only the init, fetch, have refs and `have.json` run under it, so a slow upload does not
  block other jobs for the repo.
- The job refs deleted at finish are the `refs/nw/gate-jobs/*` heads the bundle listed
  (recorded in `refs.json`). `gc` also deletes job refs that no live job owns (left by a
  crash), and it never deletes the have ref of a sha that a live job's `job.json` names.
- `have` sets or refreshes `refs/nw/have/<sha>` and its last-used time for each sha it
  reports, so a sha the coordinator leaves out of a bundle survives until the job arrives.
- Gate env: `NW_CAMPAIGN`, `NW_SPEC_ID` and `NW_BASE` default to the header's `campaign`,
  `spec_id` and `base.name`, and the header's `env` is applied over them. Values from the
  worker's own environment never leak into these keys.
- The only change outside `nw worker`: `gate_slots.acquire` takes an optional `wait_event_s`
  (default `WAIT_EVENT_S`, so behaviour is unchanged). The worker passes
  `NW_WORKER_SLOT_WAIT_EVENT_S` through it.
- Finish also removes `out/`, `tmp/` and the bundle. `frames.jsonl` keeps the files the gates
  sent.
- Extra validation: gate names must match `[A-Za-z0-9._-]+` (they become paths), and
  `payload.seq` must be 0..999. `payload.gate_budget_s` is optional and falls back to the
  worker's own `gate_budget_s`.
- Extra error stages: `read` for stdin ending short of `bundle_bytes`, and `slot` for an
  exception during the slot/gate phase.
- `done.ok` follows local `run_all`: every required gate is ok. `interrupted` is reported
  separately, and an interrupt during the slot wait gives `ok: false`.
- Every frame carries `schema`, `type`, `job_id` and `at`.
- The stdin watcher reads the raw fd. A daemon thread blocked in the buffered reader made the
  interpreter abort at exit (SIGABRT), which the concurrent-jobs test caught.
- `ssh-command` execs the worker's own `nw` (`sys.argv[0]` when it is an `nw` executable,
  else `python -m nightwright`), never the path the client sent. `nw worker gc` prints a JSON
  summary.
- Validation (loop 4): `campaign` and `spec_id` must also match `[A-Za-z0-9._-]+`. The gate list
  is checked with the same rules as a local `.nw/project.yaml` gate list
  (`project.validate`: types of `timeout_s`, `cwd`, `retries`, `results`, `retry_cmd`, unknown
  keys, unique names), so a malformed gate is a `validate` refusal before anything runs, and
  duplicate names (which would share `out/<seq>-<name>.*` files) never reach the run. All the
  charset and sha regexes end in `\Z`, so a trailing newline no longer matches.
- Slot identity (deviation from "campaign id = the job's campaign"): the worker takes its slot
  as `remote:<campaign>`, so a remote job's hold or waiter marker never reads as one of the
  worker host's own local campaigns in `gate_slots.holding()`/`waiting()`.
- A `worker/jobs.lock` flock makes a claim (mkdir, `job.lock`, `job.json`) atomic against gc's
  scans of `jobs/`. gc reads the live jobs again for each repo under that repo's lock plus the
  jobs lock, so a job claimed after gc's first scan keeps its have refs, job refs and objects
  even with `--keep-days 0`. Lock order is repo lock, then jobs lock; a job never holds both.
- SIGTERM, SIGINT and SIGHUP are a cancel. Before the slot stage the job ends with an `error`
  frame (`stage: "signal"`, exit 3). From the slot stage on, the slot wait or the live gates stop
  (`terminate_all`, run on a helper thread because the interrupted main thread may hold the
  gates lock) and `done` goes out with `interrupted: true` (exit 0).
- Pre-slot signals (loop 5): the handler raises at once only while the job is blocked reading
  stdin (the header or the bundle), where no lock is held and no git runs. Anywhere else before
  the slot stage it only records the signal, and the job raises it at the next check between
  steps (after validation, after the claim, after the bundle read, after the cache update, after
  the checkout). A signal therefore never lands in a lock's release or SIGKILLs a running git.
  Every git that writes the cache runs under the repo lock, so a git lock file found while
  holding it is stale (left by a git killed hard); the checkout, `have` and `gc` delete
  `HEAD.lock`, `config.lock`, `packed-refs.lock`, `shallow.lock` and `refs/**/*.lock` before
  they write.
- No git auto maintenance in the cache (loop 6): the cache config is set to `gc.auto=0` and
  `maintenance.auto=false` under the repo lock, by the checkout (new and existing caches) and by
  `gc`. As a result the worker's own `gc` is the only gc that runs in the cache, and it runs
  under the repo lock that `_clear_stale_locks` depends on. `gc` lists a repo in `pruned` only
  when `git gc --prune=now` exits 0. A pending signal is also checked after the clone and
  checkout.
- Older git (loop 7): the cache `fetch` passes no flag newer than git 2.29 (the loop 6
  `--no-auto-maintenance`, and `--no-write-fetch-head`, are gone; the config above already
  stops auto gc, and a `FETCH_HEAD` written under the repo lock is harmless). Nothing the
  worker runs needs a recent git.
- Gate stdin (loop 7): once the bundle is read, the watcher reads a private `dup` of the job
  stream's fd and, when that fd is the process's fd 0, fd 0 is replaced by `/dev/null`. Gates
  inherit fd 0, so a gate that reads stdin gets EOF at once, as under a local run, instead of
  blocking on (or eating from) the coordinator's open stream. `run_gate` is unchanged.
- `origin/<rest>` base (loop 7): the job tree also sets `core.warnAmbiguousRefs=false`, because
  `origin/<rest>` then names both `refs/heads/origin/<rest>` and `refs/remotes/origin/<rest>`
  (same sha); without it every resolve of `$NW_BASE` warns on stderr, which a local checkout
  never does.
- Validation (loop 7): `payload.gate_budget_s` must be finite (`NaN`/`Infinity`, which
  `json.loads` accepts, are refused); env values and a gate's string fields must not contain
  NUL; a gate named `<other gate>.r<n>` is refused, because its files would be that gate's
  retry `<n>` files. `gc --keep-days` above 100000 days counts as 100000 (a larger value would
  overflow the float clock).
- Design step 4 says "campaign id = the job's campaign"; the shipped id is
  `"remote:" + the job's campaign`, per the slot identity note above (the Design text is
  frozen at acceptance, so this note is the authority).
- Loop 8: the base name is checked in full at `validate` (`base_name_ok`: git's
  check-ref-format rules that the BASE_RE charset can break), so a name git refuses never
  reaches the fetch. A signal handled after the checkout returns but before the stage turns
  `slot` is still pre-slot (`error`, stage `signal`, exit 3): the job checks for it right after
  switching the stage, and only signals received before the switch count. `gc` runs each
  repo on its own; a cache that fails (a git error, an OS error) is listed in a new `errors`
  key (`[{repo, error}]`), the repos after it still run, and `nw worker gc` exits 1 after
  printing the JSON when `errors` is not empty.
- Loop 9, frame writes: a frame goes to stdout's fd in `PIPE_BUF` pieces, and each piece is
  written only after `select` reports the fd writable. The main thread (which may hold the gate
  slot) therefore never blocks in `write`. If the fd stays full, the write gives up when
  `cancel` is set (stdin EOF or a signal), or when there has been no progress for
  `NW_WORKER_FRAME_STALL_S` (default 300 s). After that, stdout counts as closed. A stall also
  sets `cancel` and kills the live gates, so the remaining gates do not run, the job ends
  (exit 0, `job.json` state `done`) and its slot is released. `frames.jsonl` still gets every
  frame.
- Loop 9, the finish gc (step 6 "run gc") is started **detached**: `nw worker gc` runs in its
  own session with no stdio of the job's, pointed at the job's data and config dirs. The job's
  exit, and so its exit code, no longer waits on other repos' locks or on a
  `git gc --prune=now`. `NW_WORKER_FINISH_GC=0` turns it off. The worker tests set this,
  because a background gc would race the counts that the gc tests assert, and one test turns
  it back on. The tree, scratch and job-ref removal still run synchronously before exit.
- Loop 9, names: `job_id`, `repo_key`, `campaign` and `spec_id` (and `have --repo`, including
  under `ssh-command`) must also not consist only of dots. `.` and `..` name the store's own
  dirs, so a `job_id: ".."` header is now a `validate` refusal rather than a `claim` one.


## Clock Log

Use `nw idea clock-in IDEA-564` / `nw idea clock-out IDEA-564`.

## Rollout / migration

Inert until a coordinator sends it a job (SPEC-0338). Installing it on a worker is the manual
`uv tool install` documented above.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`).
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

None. Decided while writing this spec:
- The worker gets the base as a ref in the bundle plus `base: {name, sha}` in the header.
- `refs/nw/have/<sha>` keeps cached objects reachable; `gc` ages them out.
- `started` marks the slot hold, so the coordinator's timeout excludes the worker's queueing.
- Each job gets a `git clone --shared` of the cache (private refs) rather than a worktree,
  per the review: worktrees share `refs/heads/*`.
- The cache, `have.json` and `gc` are serialized by a per-repo flock; `gc` prunes objects.
- Each log is capped at 16 MiB.
