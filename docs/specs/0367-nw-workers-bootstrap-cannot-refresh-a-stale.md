---
id: SPEC-0367
title: "nw workers bootstrap refreshes a stale worker install (build id, --reinstall, stale warning, loud gate rejection)"
status: accepted
owner: user
created: 2026-11-01
updated: 2026-11-01
kind: feature
source_idea: IDEA-675
tags: [agentic, remote, workers, ops]
depends_on: [SPEC-0354, SPEC-0336, SPEC-0338]
---

## Context

[SPEC-0354](./0354-nw-workers-bootstrap.md) made `nw workers bootstrap` skip the install step
when the worker already runs the coordinator's version. `step_install`
(`src/nightwright/agentic/workers_bootstrap.py`) does `info.get("version") == plan.version`, and
`version` is `nightwright.__version__`, the constant `"0.1.0"` in `src/nightwright/__init__.py`
and `pyproject.toml`. Every build of this repo has the same version string, so a worker running
older code (the IDEA-645 case: its `done` frame has no `changed` key) reports `skipped (already
0.1.0)` and is never refreshed. The stale-install failure SPEC-0354 was meant to fix is not
fixed. The human asked for this urgently (via the steward): bootstrap must refresh a stale
same-version worker with no manual sudo beyond the existing `--admin` step.

Related gaps found on the same path:

- `nw worker info --json` (`worker.info`, `src/nightwright/agentic/worker.py`) already reports
  a `git_sha`, but it comes from `git rev-parse HEAD` in the package directory. A wheel
  install (`uv tool install`) lives in `site-packages`, outside any git repo, so the sha is
  `null` on exactly the workers that matter. `workers._check` only compares shas when both
  sides have one, so a stale worker passes `nw workers check` with at most a version note.
- A stale worker rejects or garbles a job (an unknown gate key fails `validate_header`'s
  `validate_project`; an old `done` frame fails `_check_changed`). `remote_gates._dispatch`
  turns both into a `RemoteFailure`, `run` records a `worker_failed` event and
  `gates.run_all` quietly re-runs the gates locally. Nothing tells the human the worker is the
  problem, so the fleet looks healthy while remote gates never run.

## Goals / Non-goals

**Goals**
- A build identity that exists on wheel installs and changes whenever any shipped file
  changes: reported by `nw worker info --json`, compared by bootstrap instead of the version
  string.
- `nw workers bootstrap` refreshes a worker whose build id differs from, or is missing next
  to, the coordinator's, with no manual step beyond the existing `--admin` one.
- `--reinstall` forces the install step regardless of the comparison.
- `nw workers check` warns `worker stale` when the build ids differ or the worker reports
  none.
- The remote gate path detects a worker-side rejection (config/schema/protocol) and says so
  loudly, with the fix, instead of only falling back to local.
- The test plan includes one real remote gate against the real box, not only fakes.

**Non-goals**
- No auto-update of workers from `nw workers check` or from the gate path; the human (or
  steward) runs `bootstrap`. Nothing in this spec changes a worker without a bootstrap run.
- No version bump policy, no change to `__version__`.
- No change to the `nw.job.v1` / `nw.job.frame.v1` wire protocols or to which frames a
  healthy worker sends.
- No change to cooldown rules for existing failure stages (see Design, "Rejection").

## Decision

The build id is a SHA-256 over the contents of every file nightwright ships (relative path and
bytes, sorted), computed by one function on both sides. `nw worker info --json` gains a
`build_id` key. Bootstrap skips the install only when the worker's `build_id` equals the one
the coordinator would install; a missing or different id reinstalls. `--reinstall` skips the
comparison. `nw workers check` replaces the sha comparison with a build-id comparison and
words it `worker stale`. A worker-side rejection becomes a recorded `rejected` failure that
writes a one-per-campaign-and-worker finding and a stderr line naming the worker, the
worker's message and the `nw workers bootstrap` fix, then falls back to local as before.

## Design

**Build id.** New module `src/nightwright/buildid.py`:

- `compute(pkg_dir: Path) -> str`: `"b1:" + sha256 hex` over, for each file in sorted
  relative-posix-path order, `path \0 sha256(bytes) \n`. Excluded: `__pycache__` directories
  and `*.pyc`. Inside a git checkout (`pkg_dir` is under a work tree) the file list is
  `git ls-files --cached --others --exclude-standard -- .` run in `pkg_dir`, which matches
  what hatch ships (it honours `.gitignore`), so a stray ignored file in a dev checkout does
  not make the coordinator look different from its own wheel. Outside git (an installed
  copy) it walks the directory. A git failure falls back to the walk.
- `compute_wheel(wheel: Path) -> str`: the same digest over the zip entries under
  `nightwright/` (so `*.dist-info` is excluded), so the id of a built wheel equals the id of
  the directory it installs to.
- `local() -> str`: `compute(Path(nightwright.__file__).parent)`.

`worker.info` adds `"build_id": buildid.local()`. The `nw.worker.info.v1` schema keeps its
name (the key is additive; consumers already ignore unknown keys). Computing it reads the
package once (~1 MB), which is acceptable for `info`'s 30 s probe budget.

**Bootstrap** (`workers_bootstrap.py`):

- `Plan` gains `build_id: str | None` and `reinstall: bool`. `prepare(...)` takes
  `reinstall=False`; the CLI gains `--reinstall` ("rebuild and reinstall even when the worker
  reports this checkout's build").
- `step_install` computes `want = buildid.local()` and calls `_remote_info`. It skips only
  when `not plan.reinstall` and `info.get("build_id") == want`; the status is
  `skipped (already <build_id[:12]>)`. In every other case it installs, and its detail says
  why: `no build id (older worker)`, `build <old12> -> <new12>`, or `--reinstall`. After
  `uv build` it sets `plan.build_id = buildid.compute_wheel(wheel)`; on a skip it sets
  `plan.build_id = want`. If the wheel's id differs from `want` the install still proceeds
  and a note records the difference (a dirty checkout), so `verify` judges the installed
  wheel, not the working tree.
- `step_verify` requires `info["build_id"] == plan.build_id` (message `worker reports build
  <got>, expected <want>`); the existing `version` check stays. A worker that installs but
  still reports an older build therefore fails the bootstrap instead of passing.
- `dry_plan` shows the comparison and the `--reinstall` effect (`# always`, instead of `# only
  when ...`). The install keeps using `uv tool install --force` on the worker, through
  `--admin` exactly as today; no new privileged command is introduced.

**Check** (`workers._check`): the `elif info.get("git_sha") ...` branch is replaced. After the
version comparison, if `mine["build_id"]` exists: a worker `build_id` that is absent or
different adds the warning `worker stale: build <w12|none> differs from the coordinator's
<c12>; run nw workers bootstrap NAME --ssh <ssh> --reinstall`. Warnings keep exit 0, as in
SPEC-0337. The sha comparison is dropped (a build id subsumes it and is present on wheels); the
`git_sha` key stays in `info` as a debugging aid. `nw workers check --json` carries the
warning in `warnings` unchanged in shape.

**Rejection** (`remote_gates.py`, `gates.py`):

- `RemoteFailure` gains `rejected: bool = False`. `_dispatch` sets it for an `error` frame
  whose stage is `validate`, and for every `protocol`-stage failure (`_frame`, `_num`,
  `_flag`, the `done`/`changed` checks, `_take_gate` field checks): the worker understood the
  job but cannot run it or speaks an older dialect. Transport stages (`ssh`, `upload`,
  `timeout`, `slot_wait`, `interrupted`, `have`, `bundle`) are never `rejected`. Cooldown
  rules are unchanged (`validate` stays no-cooldown, `protocol` stays cooldown), since a
  coordinator-side header problem is already kept local by `header_refusal`.
- `_record_failure` adds `"rejected": true` to the `worker_failed` event for such failures
  and calls `_record_rejection(cfg, cid, worker, f)`: one `finding` per (campaign, worker)
  (ledger scan, like `_record_ineligible`), `persona: gates`, `severity: info`,
  `gate: worker-rejected`, title `worker <name> rejected the gate job; its install may be
  stale`, detail = the worker's message plus `run nw workers check <name>, then nw workers
  bootstrap <name> --ssh <ssh> --reinstall`.
- `gates.run_all`, at the existing SPEC-0338 fallback, prints one stderr line when
  `out.failure.rejected`: `nw: worker <name> rejected the remote gate job (<stage>: <error
  first 200 chars>); running gates locally. The worker is probably stale: nw workers
  bootstrap <name> ...`. Every rejected run prints it (the finding and event dedupe; the
  stderr line does not), so a human watching a queue sees each fallback.

## Alternatives considered

- **Compare `git_sha`** — it is `null` on wheel installs, the exact case that breaks, and a
  dirty working tree has no sha.
- **Bump `__version__` per change** — a manual step that is what failed here, and a version
  bump on every merge is churn.
- **Always reinstall on every bootstrap** — `uv build` plus an scp plus an install on every
  re-run of an idempotent command; SPEC-0354 deliberately skips done steps.
- **Hash the wheel file** — wheels are not byte-reproducible across builds (timestamps), and
  the worker has only the installed files. Hashing file contents on both sides is.
- **Cooldown every rejected worker** — contradicts the `validate` stage's existing "says
  nothing about the host" reasoning (a coordinator-side header problem); the new `rejected`
  events give the evidence to revisit it later.

## Acceptance criteria

- [ ] `buildid.compute` is stable across calls, ignores `__pycache__`/`*.pyc`, and changes
      when any shipped file's bytes change or a file is added or removed; `compute_wheel` of a
      built wheel equals `compute` of the directory it installs to.
- [ ] `nw worker info --json` reports `build_id`, and on a wheel install (no git) it is
      non-null.
- [ ] Bootstrap with a worker reporting the same `version` but a different `build_id`
      reinstalls (status `done`), and verify passes only if the worker then reports the new
      build id.
- [ ] Bootstrap with a worker reporting no `build_id` reinstalls and says `no build id (older
      worker)`.
- [ ] Bootstrap with an equal `build_id` skips (`skipped (already ...)`), and `--reinstall`
      reinstalls anyway; `--dry-run --reinstall` shows the install commands as unconditional.
- [ ] Verify fails when the install leaves the worker on an older build id.
- [ ] No new privileged remote command: the install path is still the SPEC-0354 one, and
      `--admin` works as before.
- [ ] `nw workers check` prints `warning: worker stale: ...` (exit 0) for a worker with a
      different or missing build id, and no stale warning for an equal one; the sha warning is
      gone.
- [ ] A worker `error` frame at stage `validate` (e.g. an unknown gate key) or a `protocol`
      failure (a `done` without `changed`) yields: a `worker_failed` event with `rejected:
      true`, one `worker-rejected` finding per campaign and worker, a stderr line naming the
      worker and the bootstrap fix, and the gates still run locally with the same result.
- [ ] Transport failures (`ssh`, `upload`, `timeout`) are not marked `rejected` and print no
      such line.
- [ ] The stale same-version worker from the IDEA-645 case on the real box is refreshed by one
      `nw workers bootstrap` run (with the existing `--admin`), after which a real remote gate
      runs on it and returns `worker` set on the gate run.

## Test plan

- **Automated tests**
  - `tests/test_buildid.py` (new): `compute` stability, `__pycache__`/`.pyc` ignored,
    byte change/add/remove changes the id, git-ignored stray file ignored inside a temp git
    repo, git failure falls back to the walk, and `compute_wheel` equals `compute` of the
    extracted `nightwright/` tree for a wheel built with `uv build` (or a zip made in the
    test). `worker.info(cfg)["build_id"]` is a `b1:` string.
  - `tests/test_workers_bootstrap.py` (extend): the fake `nw worker info` reports a
    `build_id`. Cases: same version + different id reinstalls; no `build_id` key reinstalls
    with the "older worker" detail; equal id skips (replaces the old `test_rerun_skips`
    expectation of version-only matching); `--reinstall` reinstalls on an equal id; `--dry-run
    --reinstall` output; verify fails when the fake install leaves the old id; dirty-checkout
    note when the wheel id differs from the working tree's. The existing
    `test_version_mismatch_reinstalls` stays and still passes.
  - `tests/test_workers_registry.py` (extend): `check` warns `worker stale` for a different
    id and for a missing id, is silent for an equal id, keeps exit 0, and no longer emits the
    sha warning.
  - `tests/test_remote_gates.py` (extend): a fake worker answering an `error` frame
    `stage: validate` ("unknown gate key"), and one whose `done` lacks `changed`; assert the
    `worker_failed` event has `rejected: true`, one `worker-rejected` finding per campaign and
    worker across two runs, the stderr line (captured with `capfd`) names the worker and
    `nw workers bootstrap`, and the fallback result equals the local run. A parametrized
    transport case (`ssh` failure, `upload` timeout) asserts `rejected` is false and no line
    is printed.
  - `python3 tools/affected_tests.py --run uv run pytest -q`, `python3 tools/spec_lint.py`.
- **Manual verification (real box, required)** — fakes are not verification of an external
  CLI integration. On the real worker that the steward found stale (`nw workers list` names
  it; the same-version, no-`done.changed` install):
  1. `nw workers check <name>` — expect `warning: worker stale` (the box has no build id, so
     the message says `none`), exit 0.
  2. `nw workers bootstrap <name> --ssh <user@host> --admin <admin@host> --install-key`
     (the same flags as the original bootstrap) — expect install `done` (detail `no build id
     (older worker)`), verify `done`, key and register `skipped`, with no password prompt
     and no manual sudo.
  3. `nw workers check <name>` — expect `ok` and no stale warning.
  4. Re-run step 2 — expect install `skipped (already b1:...)`; run it again with
     `--reinstall` — expect install `done`.
  5. Run one real remote gate on the box: `nw queue gates --worker <name>` for a small
     campaign (or the project's gate command through the queue), and confirm the resulting
     gate run carries `worker: <name>` and no `worker_failed` event. Record the commands, the
     build id and the run id in this spec's test record.
  6. Rejection, for real: before step 2, while the box is still stale, run one remote gate
     (`nw queue gates --worker <name>`) and confirm the `worker-rejected` finding and the
     stderr line appear and the gates still ran locally. If the box has already been
     refreshed, record that this step was skipped and that the rejection path was proven by
     the automated tests alone.
- **Regression guard:** the SPEC-0354, SPEC-0337 and SPEC-0338 suites
  (`test_workers_bootstrap.py`, `test_workers_registry.py`, `test_remote_gates.py`) pass; the
  full suite at the queue's close gate; healthy-worker gate output is unchanged.

## Rollout / migration

1. Land the build id and the `info` key. Workers on the old build do not report it, so the
   first `nw workers check` after landing shows `worker stale` on every worker: that is the
   intended signal.
2. Run `nw workers bootstrap` once per registered worker (the steward does the box, with its
   existing `--admin`). No registry change.
3. No data migration; the `rejected` event key and the `worker-rejected` finding are additive.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed, including the real-box manual steps; automated tests pass
      (affected tests while iterating, the full suite at the close gate); manual steps
      recorded in the spec.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

None.
