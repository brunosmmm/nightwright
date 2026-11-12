---
id: SPEC-0571
title: "nw gate run --cmd: ad-hoc gates request Slurm resources (--cpus, --mem, --licenses), capped by box policy"
status: accepted
owner: user
created: 2026-11-12
updated: 2026-11-12
milestone: "M9: agentic workflow"
kind: feature
tags: [agentic, gates, slurm, cli]
depends_on: [SPEC-0381, SPEC-0504]
source_idea: IDEA-1314
---

## Context

`nw gate run --cmd CMD` (SPEC-0381, `cli_gate.py` `gate_run`, `gate_jobs.resolve`) builds its
gates as `{"name": "cmd-N", "cmd": ..., "timeout_s": ..., "results": {"format": "pytest"}}`.
`gate_resources.kind_of` classes any such gate `pytest`, so every ad-hoc gate gets the per-kind
default of 3 cpus / 3 GB (`gate_resources.DEFAULTS["pytest"]`, SPEC-0504); measured in
GATEJOB-2587: `SLURM_CPUS_PER_TASK=3`, 3072 MB. A gate may declare `resources: {cpus, mem,
licenses}` in `.nw/project.yaml`, and `gate_resources.gate_request` already honours a
`resources` key on any gate dict, but an ad-hoc gate has no way to set it.

Jerome's steward needs a one-off heavy job (a 386k-variable MIP baseline solved with HiGHS).
The only route today is a new named gate in `.nw/project.yaml`, and gates only have the
`loop`/`close` tiers with no per-gate selection, so that gate would also run on every close
(and the nightly tier). A one-off job should not become permanent project config.

The Slurm executor already clamps a request to what the partition and its nodes can ever hold
(`executor_slurm.sbatch_argv`) and drops licenses `scontrol show licenses` does not define; it
does not limit what a requester may *ask for* on a shared box.

## Goals / Non-goals

**Goals**
- `nw gate run --cmd ... [--cpus N] [--mem SIZE] [--licenses NAME[:N]]` sizes the job's Slurm
  request from the flags instead of the 3 cpu / 3 GB default.
- The box owner caps what ad-hoc requests may ask for, with defaults that keep today's
  behaviour for a request that sets no flag.
- A request over the cap is refused before anything is bundled or sent, with a message that
  names the cap and the setting that raises it.
- The flags are discoverable in `nw gate run --help` and in the gate-run agent docs.

**Non-goals**
- Selecting one named project gate (`--gate NAME`): a different problem (per-gate selection of
  configured gates) with its own tier semantics; it is not needed to run a one-off heavy
  command and is left for a separate idea.
- A per-gate resource request when several `--cmd` are given: a job runs its gates in sequence
  and its request is already the max across them (SPEC-0504), so the flags apply to the job.
- Changing the request of project (non `--cmd`) gates: those keep `resources:` in
  `.nw/project.yaml`.
- Changing Slurm limits, partitions or licenses (SPEC-0510 owns them).
- A `--time` flag: `--timeout-s` already sets the per-gate timeout and the job's time derives
  from it.

## Decision

Add `--cpus INT`, `--mem SIZE` and repeatable `--licenses NAME[:N]` to `nw gate run`. They are
valid only together with `--cmd` and become the `resources` mapping of every ad-hoc gate of the
job, so the existing `gate_resources.job_request` path carries them to the worker and the Slurm
executor without further change. Unset flags leave `resources` off the gate, i.e. today's
defaults. The coordinator host's `agentic.gate_jobs.adhoc` settings cap the request: `max_cpus`
(default 8), `max_mem` (default `16G`) and `licenses` (allowlist, default empty). An over-cap
request exits 2 (cannot start) before any bundle is cut. Caps are refused, never silently
clamped, so the requester learns the job would not get what it asked for; the executor's
partition clamp stays as the second line.

## Design

- **CLI** (`cli_gate.py` `gate_run`): new options `--cpus` (plain `int`, no click range, so a
  non-positive value reaches `gate_resources.check` and its message), `--mem` (string),
  `--licenses` (multiple). Help text: "Slurm resources for --cmd gates,
  capped by agentic.gate_jobs.adhoc; the default is 3 cpus / 3G". Passed to `J.resolve` as
  `cpus`, `mem`, `licenses`. Giving any of them without `--cmd` raises `click.UsageError`
  ("--cpus/--mem/--licenses need --cmd; project gates declare `resources:` in
  .nw/project.yaml"). The `gate_run` docstring gets a heavy-job example.
- **Validation** reuses `gate_resources.check` on the assembled mapping (`mem` must match
  `MEM_RE`, e.g. `8G`, `512M`; licenses match `LICENSE_RE`), so the flags accept exactly what
  `.nw/project.yaml` accepts. A `check` problem raises `JobError(2, ...)`.
- **Resolve** (`gate_jobs.resolve`, `cmds` branch): when any flag is set, each ad-hoc gate dict
  gains `"resources": {"cpus": N, "mem": "SIZE", "licenses": ["NAME[:N]", ...]}` with only the
  keys given. `gate_resources.gate_request` already reads it over the pytest defaults, so an
  unset key keeps its default (`--cpus 8` alone is 8 cpus / 3G). The flag validation and the
  cap checks (see Caps) run at the top of the `cmds` branch, before the `ineligible` /
  `commit_under_test` calls and `header_refusal` (`gate_jobs.py` `resolve`), so a refused
  run never cuts a commit or reaches the dispatch.
- **Caps**: `gate_jobs.DEFAULTS["adhoc"] = {"max_cpus": 8, "max_mem": "16G", "licenses": []}`;
  `gate_jobs.settings` overlays `agentic.gate_jobs.adhoc` and falls back per key on a bad value
  (a non-positive int, a `mem` failing `MEM_RE`, a non-list `licenses`), like the other
  settings there. `resolve` compares each cap only against the key the flags set: cpus against
  `--cpus`, `mem_mb` of `--mem` against `max_mem`, license names of `--licenses` against the
  allowlist; a key whose flag is unset is never checked. It raises `JobError(2, ...)` on a
  breach, naming the value, the cap and `agentic.gate_jobs.adhoc.<key>`. The cap applies to
  ad-hoc gates only: the pytest default (3 cpus / 3G) is never refused, even if a host sets
  a smaller cap, so `--mem 8G` under `max_cpus: 2` is accepted (cpus stays the default 3)
  while `--cpus 3` there is refused.
- **Dedup key**: `resolve` hashes the `gates` list, so `resources` is part of the idempotence
  key (SPEC-0381): the same command with a different `--cpus` is a different job, the same
  command and flags still reuses a running one.
- **Records**: each gate record already stores its request next to usage (SPEC-0504), so a
  GATEJOB's `nw gate status --json` gate entries and `nw gate stats` (SPEC-0514) show what was
  requested; no schema change.
- **Docs**: `docs/` pages and the `nw-orient` skill section that describe `nw gate run --cmd`
  (SPEC-0383) mention the flags and the cap keys.

## Alternatives considered

- **`--gate NAME` to run one named gate**: still needs a permanent config entry for a one-off
  job, and adds per-gate selection semantics to the tier model. The flags solve the reported
  need directly.
- **Clamp over-cap requests silently**: the job would run smaller than asked and a solver
  baseline could OOM or time out with no hint why; refusing is cheaper to diagnose.
- **Put the cap on the worker only** (`nw workers`): the refusal would come after bundling and
  submission. The coordinator already holds `agentic.gate_jobs` and validates before cutting a
  commit (`header_refusal`); the Slurm clamp remains the worker-side backstop.
- **Per-`--cmd` resources** (`--cmd 'x' --cpus 4 --cmd 'y'`): ordering-sensitive option
  grouping in click for no gain; the job request is the max anyway.

## Acceptance criteria

- [ ] `nw gate run --cmd C --cpus 8 --mem 12G --licenses lic:2` produces a job whose gate has
      `resources == {"cpus": 8, "mem": "12G", "licenses": ["lic:2"]}`, and
      `gate_resources.job_request` of it yields cpus 8, `mem_mb` 12288, licenses `{"lic": 2}`.
- [ ] A `--cmd` run with none of the flags has no `resources` on its gates and requests 3 cpus
      / 3072 MB, exactly as before.
- [ ] `--cpus 8` alone requests 8 cpus and 3072 MB; `--mem 8G` alone requests 3 cpus and
      8192 MB.
- [ ] `--cpus`, `--mem` or `--licenses` without `--cmd` exits 2 with a usage message naming
      `--cmd`; no job record is created.
- [ ] `--cpus 0`, `--mem lots`, `--mem 8` and a malformed license name exit 2 with the
      validation message of `gate_resources.check` (e.g. `cpus: must be an integer >= 1`);
      no job record is created.
- [ ] With defaults, `--cpus 9`, `--mem 17G` and `--licenses jerome_target` each exit 2 naming
      the value, the cap, and `agentic.gate_jobs.adhoc.max_cpus` / `.max_mem` / `.licenses`;
      `--cpus 8 --mem 16G` is accepted; the refusals happen before any bundle is cut or worker
      is contacted.
- [ ] With `agentic.gate_jobs.adhoc: {max_cpus: 16, max_mem: 64G, licenses: [jerome_target]}`,
      `--cpus 16 --mem 64G --licenses jerome_target` is accepted; a malformed cap value falls
      back to the default for that key.
- [ ] A host cap below the default request (e.g. `max_cpus: 2`) does not refuse a run that sets
      no flag, and `--mem 8G` alone is accepted (cpus stays the 3-cpu default) while
      `--cpus 3` is refused naming `agentic.gate_jobs.adhoc.max_cpus`.
- [ ] The same command with different `--cpus` creates a second job; the same command and
      flags while the first is live reuses it.
- [ ] `nw gate run --help` lists `--cpus`, `--mem` and `--licenses`.
- [ ] Project (non `--cmd`) gate runs are unchanged: their request still comes from
      `.nw/project.yaml` `resources:` or the kind defaults.

## Test plan

- **Automated tests** (extend existing modules; they keep their one module-level
  `pytestmark = pytest.mark.fast`, so no second tier is added):
  - `tests/test_gate_jobs.py` (the `gw` fixture and `CliRunner`, worker dispatch stubbed):
    resolve/CLI cases for flag-to-`resources` mapping, defaults unchanged, partial flags,
    flags without `--cmd`, invalid values, over-cap refusals (default caps and configured
    caps, bad cap config fallback, small cap not refusing a flagless run, and `--mem 8G` alone
    accepted / `--cpus 3` refused under `max_cpus: 2`), dedup-key behaviour, and `--help`
    listing the options. For every refusal and validation case the test monkeypatches
    `RG.commit_under_test` and the dispatch stub to raise if called, then asserts the exit
    code, the message, and that no `gatejobs/` job directory exists, which proves the
    refusal happens before any commit is cut or worker contacted.
  - `tests/test_gate_resources.py`: `job_request` of a gate dict carrying
    `resources: {cpus: 8, mem: "12G", licenses: ["lic:2"]}` gives 8 / 12288 / `{"lic": 2}`;
    a partial mapping keeps the pytest default for the missing key.
  - A guard that the job header's `payload.resources` for a flagged run passes
    `gate_resources.check_payload` (in `tests/test_gate_jobs.py`, stub `run_detached` inspects
    `state`/`pcfg`).
  - `python3 tools/test_tiers.py --check` exits 0 when `tools/test_tiers.py` exists.
  - Affected run: `python3 tools/affected_tests.py --run uv run pytest -q`.
- **Manual verification**: on a box with a Slurm worker, `nw gate run --cmd 'echo
  $SLURM_CPUS_PER_TASK $NW_GATE_MEM_MB' --cpus 6 --mem 8G --json` prints 6 and 8192 in the gate
  log (`nw gate logs`), and `nw gate run --cmd true --cpus 64` exits 2 naming
  `agentic.gate_jobs.adhoc.max_cpus`.
- **Regression guard**: the existing `tests/test_gate_jobs.py`, `tests/test_gate_resources.py`
  and `tests/test_cli_gate_slots.py` pass unchanged; `python3 tools/spec_lint.py` exits 0.

## Rollout / migration

1. Land the code; defaults keep every existing `--cmd` invocation as it is.
2. Document the flags and `agentic.gate_jobs.adhoc` in the gate docs and the `nw-orient` skill.
3. A box that wants jerome's heavy job raises `agentic.gate_jobs.adhoc.max_cpus` /
   `max_mem` (and lists `jerome_target` under `licenses` if wanted) in its host config.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (affected tests plus the touched files); the
      manual steps performed or noted as not possible on the box.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` regenerated (`python3 tools/spec_lint.py --write-ledger`).

## Open questions

- None.
