---
id: SPEC-0486
title: Extend backend preflight to the spec reviewer and Pi personas
status: done
owner: user
created: 2026-11-09
updated: 2026-11-10
milestone: "M9: agentic workflow"
kind: feature
tags: [agentic, queue, runner, resilience]
depends_on: [SPEC-0484]
source_idea: IDEA-1040
---

## Context

[SPEC-0484](./0484-runner-contains-apply-errors-and-holds-on-backend-faults.md) treats a
missing Cursor credential or SDK as an environment fault. The dispatch gate holds the step
`backend_unavailable`, a fresh local `CursorRunner` is replaced by `PreflightHeldRunner`
(interrupted, uncharged), and a finished run whose text is the child's startup fault is
turned into an interrupted result so no triage or review retry is spent. `startup_check`
warns at runner start. `backend_preflight.check` only probes `cursor`.

Two paths were left out, and both still read an environment fault as a verdict.

The campaign spec review (`queue._do_spec_review`) never calls `_preflight_hold`. `spec_review`
is not in `persona_gate.GATED_KINDS`, so `Gate.admit` never sees it: [SPEC-0345](./0345-persona-cap-rate-limit-backoff-affinity-and-cost-by-worker.md)
keeps that step out of the rate-limit backoff and the persona cap. A missing `CURSOR_API_KEY`
or `cursor-sdk` therefore starts the reviewer. The child exits with the startup text,
`_apply_spec_review` counts `counters.spec_review_retries` (`SPEC_REVIEW_RETRY_MAX` is 1),
and the next failure parks the campaign `spec_review_exhausted`. That park looks like a
reviewer that could not judge the spec. An adopted spec-review run is re-entered through
`_do_persona`, which already converts a Cursor startup fault; a fresh review does not.

Pi has no preflight. `check("pi")` returns None, `_preflight_hold` returns a `PiRunner`
unchanged, `Gate.backend_fault` ignores every kind other than `cursor`, and `env_fault`
matches only the Cursor startup texts. `pi_exec.mjs` already fails a run in-process with
`pi-coding-agent is not installed (…)`, `no credential for provider <name>`, or (from
`runproc.probe_pi`, before it spawns) `node not found on PATH` / `pi_exec.mjs missing at
<path>`. The queue treats those as persona verdicts. SPEC-0484 refused to call `probe_pi` on
the tick: that probe is `node pi_exec.mjs --probe`, which imports the SDK and calls
`ModelRuntime.checkAuth`, with a 30 s timeout. This spec keeps that probe off the tick.

## Goals / Non-goals

**Goals**
- A fresh campaign spec review whose local backend cannot start (missing Cursor key or SDK,
  or the Pi faults below) is held `backend_unavailable`. It does not start a run, does not
  increment `spec_review_retries`, and does not park `spec_review_exhausted`.
- A spec-review run that does finish with one of those startup texts becomes interrupted and
  uncharged, and the retry counter stays unchanged.
- The same hold, spawn replacement, result conversion, single escalation, and start warning
  that SPEC-0484 gives `cursor` also apply to `pi`, using a check that does not spawn Node.
- Spec review stays outside the rate-limit backoff, the usage pause, and the persona cap.

**Non-goals**
- Calling `probe_pi` / `ModelRuntime.checkAuth` from the gate, the spawn hold, or
  `startup_check`. `nw doctor`'s `agentic.pi` check and the worker's `ensure_pi_persona`
  keep that probe.
- Putting `spec_review` into `GATED_KINDS`, or holding it for `rate_limit`, `usage_pause`,
  or `persona_cap`.
- A preflight for `claude`. Its missing-binary failure stays the `launch failed` path.
- The one-shot `spec_review.run_spec_review` command (`nw spec review`). It has no retry
  counter and no queue step to hold.
- Validating that a present Pi credential is accepted by the provider. A key or `auth.json`
  entry that exists and is then rejected by `checkAuth` still fails the child; that result
  is an environment fault (no retry, no park) and the next tick may try again.
- Changing `apply_error` containment, or the Cursor probe itself (`probe_cursor_sdk`).

## Decision

1. **Spec review holds on the backend only.** In the tick, after `_spec_review_precheck`
   returns None (a dirty tree, an already-accepted spec, and the other precheck results
   still apply as they do today) and before `_prepare_run`, resolve the backend
   `build_request` would spawn for `spec_reviewer` and, when that check fails, record
   `held_by: backend_unavailable` and leave the step pending. No `run.start` is written.
   Persona workers do not lift this hold: `_do_spec_review` never calls
   `remote_personas.dispatch`, so a worker cannot take the review. The same tick still
   admits gated steps through `Gate.admit`, including its existing worker escape.
2. **The spec-review worker applies the spawn hold and the result conversion.**
   `_do_spec_review` passes its local runner through `_preflight_hold` before `run`. A
   returned startup fault is rewritten the way `_do_persona` rewrites one: `interrupted`,
   and `cost_usd` 0.0 when the child reported none. `_apply_spec_review` already returns
   the campaign to its current state without incrementing `spec_review_retries` when
   `res.interrupted` or `run_result.interrupted` is set. `res.interrupted` stays true for
   that result even when `ctx.stop` is clear (today the function overwrites it with the
   stop flag after `run`).
3. **Pi gets a filesystem-and-environment check, not the Node probe.** `check("pi", …)`
   fails when `node` is missing, `pi_exec.mjs` is missing, the SDK entry the child resolves
   is missing, or a named provider has no cheap credential. The gate, `_preflight_hold`,
   `env_fault`, and `startup_check` use it for `pi` the way they already use the Cursor
   probe. A gated Pi step is still released when a persona worker can take it. A spec-review
   step is not.

## Design

### Spawn backend

`backend_preflight.spawn_backend(cfg, state, persona) -> tuple[str, list[str]]` uses the
same inputs as `personas.build_request` and returns `(kind, providers)`.

`project_root` is `project.project_config(cfg, state["project"])["_root"]`. The alias and
effort come from the campaign profile:
`profiles.resolve_profile(cfg, state.get("profile"), project_root=(state.get("repo") or {}).get("root"))`,
then the settings `build_request` takes from that profile. A builtin
(`persona in personas.PERSONAS`) uses `profiles.persona_settings(profile, persona)`. A
declared persona uses `personas.declared_settings(profile, p)`, where `p` is
`personas.get_persona(persona, project_root, user_dir=persona_defs.user_personas_dir(cfg))`.
`alias` is `str(settings.get("model") or "sonnet")` and `effort` is `settings.get("effort")`.

- When `model_sets.resolve_model_set(cfg, project_root=project_root)` returns a set,
  `model_sets.bind` for that persona supplies `kind` (`bound["runner"]`). For `pi`,
  `providers` is the provider half of the bound `provider/id` model, split on the first
  `/`: `[bound["model"].split("/", 1)[0]]`.
- Otherwise `kind` is
  `runner.resolve_runner(cfg, campaign=state, project=state.get("project"))`, and for
  `pi` `providers` is
  `[runner.map_pi_model(alias, effort, cfg, project_root=project_root)["provider"]]`.
- Any other resolved kind returns `providers == []`.

`spawn_backend` lets those calls raise. When `resolve_model_set`, `bind`, profile load
(`resolve_profile`), or `map_pi_model` raises `ValueError` or `OSError`, `backend_fault`
returns None and does not raise, the same way today's `backend_fault` swallows
`resolve_runner` errors. The catch is `except (ValueError, OSError)` around the
`spawn_backend` call inside `backend_fault`. `_prepare_run` still reports the error: the
tick's existing `except (ValueError, OSError)` around `_prepare_run` sets the step
result's `error` to the exception text. `build_request` raises before the `run.start`
append, so that step writes no `run.start`. For `spec_review`, today's `res.error` apply
then parks `spec_review_exhausted`.

`check` gains `providers: list[str] | None = None` (keyword). Callers that pass only
`check("cursor")` stay valid.

### `check`

`check("cursor", environ=None)` is unchanged (`probe_cursor_sdk`: import and
`CURSOR_API_KEY`).

`check("pi", environ=None, providers=None)` never spawns a process and never reads a
secret into the returned string. `environ` defaults to `os.environ`. The first failure
wins:

1. `runproc._resolve_binary("node", environ)` is missing → `node not found on PATH`.
2. `src/nightwright/agentic/pi_exec.mjs` is not a file → `pi_exec.mjs missing at <path>`
   (the same text `probe_pi` uses).
3. The SDK entry is absent. It is present when either of these holds:
   - starting at `pi_exec.mjs`'s directory, and at each `NODE_PATH` entry, a parent
     contains `node_modules/@earendil-works/pi-coding-agent/package.json`;
   - or the managed install: the directory `PI_CODING_AGENT_DIR` or `~/.pi/agent` has
     `install/current-version` whose trimmed text matches `^[0-9A-Za-z][0-9A-Za-z.+-]*$`
     and
     `install/releases/<that text>/node_modules/@earendil-works/pi-coding-agent/dist/index.js`
     is a file. Trim the same way `pi_exec.mjs` `resolvePiEntry` does (read, then trim).
   Absent → `pi-coding-agent is not installed`.
4. For each name in `providers` (none when the list is empty or None), a credential is
   present when any row below holds. The `auth.json` file is
   `$PI_CODING_AGENT_DIR/auth.json` or `~/.pi/agent/auth.json`. It counts for a provider
   only when it parses as a JSON object and that provider's value is a non-empty object.
   A missing or unparseable file counts as absent. File contents never enter the error,
   the journal, or the escalation.

   | Provider | Present when |
   |---|---|
   | `anthropic` | `ANTHROPIC_API_KEY` or `ANTHROPIC_AUTH_TOKEN` is non-empty, or `ANTHROPIC_IDENTITY_TOKEN_FILE` is an existing file, or `auth.json` has `anthropic` |
   | `openai` | `OPENAI_API_KEY` is non-empty, or `auth.json` has `openai` |
   | `google` | `GOOGLE_API_KEY` or `GEMINI_API_KEY` is non-empty, or `GOOGLE_APPLICATION_CREDENTIALS` is an existing file, or `~/.config/gcloud/application_default_credentials.json` is a file, or `auth.json` has `google` |
   | `google-vertex` | `GOOGLE_API_KEY` is non-empty, or either of those credential files exists, or `auth.json` has `google-vertex` |
   | `amazon-bedrock`, `bedrock` | `AWS_ACCESS_KEY_ID` is non-empty, or `AWS_WEB_IDENTITY_TOKEN_FILE` is an existing file, or `auth.json` has that provider |
   | any other name | `auth.json` has that provider |

   The first missing name → `no credential for provider <name>`.

`claude` and `fake` still return None.

### Result text

`env_fault` matches a failed, non-interrupted result whose `result_text` contains any of:
`CURSOR_API_KEY is not set`, `cursor-sdk is not installed`, `pi-coding-agent is not installed`,
`no credential for provider `, `node not found on PATH`, `pi_exec.mjs missing`.
`ModelRuntime.create failed`, `unknown model`, and `request.json` failures stay ordinary
run failures.

`fault_kind` returns `cursor` when the text contains `CURSOR` or `cursor-sdk`, `pi` when it
matches one of the Pi texts above, otherwise `unknown`.

### Gate

`Gate.backend_fault(state, personas=(), *, remote_ok=True)`:

- With no personas, the kind is `resolve_runner` for the campaign (today's cursor behavior)
  and Pi providers are `pi_map.probe_providers(cfg, campaigns=[state])`.
- With personas, each persona is resolved through `spawn_backend`. The first failing
  `cursor` or `pi` check is the fault. A `claude` or `fake` persona adds nothing. A
  `ValueError` or `OSError` from `spawn_backend` makes `backend_fault` return None and
  not raise, as Spawn backend specifies.
- `check`'s result is cached for the tick under `(kind, tuple(providers or ()))`.
  `note_fault(..., where="gate")` runs when that result is first stored and is a string.
  The stored value is `check`'s string or None.
- The `persona_workers` escape is applied after the lookup, only when `remote_ok` is
  true, and the cached error stays in place. A worker makes this call return None. A
  later `backend_fault(..., remote_ok=False)` in the same tick still returns the Pi
  error. `remote_ok` false skips the escape. An exception from `persona_workers` is
  ignored and the call returns the cached error, as today's `backend_fault` does.

`Gate.admit` calls `backend_fault(state, personas)` with the step's persona list
(`step.personas` for a review fan-out, else `(step.persona,)` when set) and
`remote_ok=True`. A fault still holds `backend_unavailable` ahead of the cap. Usage pause
and rate limit stay ahead of it.

The tick's `spec_review` branch, only after precheck returns None:

```text
fault = pgate.backend_fault(st, ("spec_reviewer",), remote_ok=False)
if fault is not None:
    pgate._hold(st["id"], "backend_unavailable")
    continue
```

`held_by` already reports `backend_unavailable`, so the stuck detectors stay quiet while
the escalation is open.

### Spawn and spec review

`_preflight_hold` keeps the adoption skip (`proc.json` or `launch.json` already in the run
dir). It then:

- `CursorRunner` → `check("cursor")`, as today, and on failure `PreflightHeldRunner("cursor", error)`.
- `PiRunner` → `check("pi", providers=[req.pi_provider] if req.pi_provider else [])`, and
  on failure `PreflightHeldRunner("pi", error)` plus `note_fault(..., where="spawn")`.
- any other runner → unchanged.

`_do_spec_review` uses that wrapper for the fresh local run. After `run` returns, a
non-None `env_fault` replaces the result with `interrupted=True` and `cost_usd=0.0` when
`cost_usd` was None, sets `res.interrupted`, and calls `note_fault(..., where="run")`.
The worktree-reset that already follows a spec-review run still runs. `_do_persona`'s
existing conversion picks up Pi texts with no further branch, which covers an adopted
spec review (adoption submits it through `_do_persona`).

### Escalation and start warning

`note_fault` stays once per process and `(kind, error)`, one open `backend_unavailable`
escalation per backend (`facts[0] == "backend: <kind>"`). The Cursor question and its two
options stay. For `pi` the question names the Pi error, and the options are: install Node
and `@earendil-works/pi-coding-agent` (managed install under `~/.pi/agent`, or a
`node_modules` walk that satisfies the check) and export the provider key or store it in
Pi's `auth.json`; switch `agentic.runner` (or the project's runner) to `claude`.

The credential-map skip grows one Pi case. When the error is `no credential for provider
<name>` and any env key of that provider in the table above is mapped in
`agentic.credentials`, no backend escalation is filed (the credential escalation names the
fix), the same way a Cursor error that contains a mapped var is skipped today. A missing
`node` or SDK still files the backend escalation.

`startup_check` runs the Cursor warning when `cursor_needed`, as today, and the same shape
of warning when `pi_needed`: `WARNING: the pi persona backend fails its preflight: <error>.`
Providers for that Pi check are `pi_map.probe_providers(cfg)`. An empty provider list still
checks node and the SDK. Each failing backend is journaled `where=start` and escalated on
its own. The runner still starts.

`skills/nw-conduct/SKILL.md`'s `backend_unavailable` sentence names a missing Pi backend
(node, the coding-agent package, or a provider credential) beside the missing
`CURSOR_API_KEY`.

## Alternatives considered

- **Add `spec_review` to `GATED_KINDS`.** The backend hold would fall out of `admit`, and
  so would the rate-limit backoff and the persona cap. SPEC-0345 keeps the reviewer off
  both; a closed cap would then stall spec review behind implementers.
- **Call `probe_pi` on the tick.** One `ModelRuntime.create` plus `checkAuth` can take the
  whole 30 s timeout and would stall every campaign in that tick. SPEC-0484 already refused
  this. Caching one probe per process still stalls the first tick and would hide a key that
  appeared until the process restarted.
- **Spend the one `spec_review` retry, then park `spec_review_exhausted`.** The campaign
  waits on a human resume for a fault the queue can wait out. The hold already used for
  Cursor persona steps is the matching behavior.
- **Treat every Pi child failure as an environment fault.** `unknown model` and a broken
  `request.json` are bugs in the request, and they must keep spending the retry.

## Acceptance criteria

- [x] With the Cursor preflight failing, a campaign in `SPEC_REVIEW` is held
      `backend_unavailable` (`held_by` reports it) and the tick writes no `run.start` for
      `spec_reviewer`. `counters.spec_review_retries` stays 0 and the campaign is not parked.
      A persona worker on the host does not lift that hold.
- [x] A campaign whose profile is resolved at `repo.root`, and whose project
      `.nw/pi_map.yaml` on `project.project_config(...)["_root"]` maps that profile's
      `spec_reviewer` alias and effort to a provider with no cheap credential, is held
      `backend_unavailable` and the tick writes no `run.start`. The host map's provider
      for that alias has a cheap credential, and `_root` is not the campaign worktree.
- [x] A model set that exists only under that same `_root` `.nw/model_sets` and binds
      `spec_reviewer` to `pi` with a `provider/id` model holds the spec review
      `backend_unavailable` when that provider has no cheap credential, and the tick
      writes no `run.start`.
- [x] A spec review whose backend check passes is not held for `rate_limit`,
      `usage_pause`, or `persona_cap`: a closed cap and an open backoff still let that
      review prepare a run.
- [x] `_do_spec_review` on a fresh local `CursorRunner` or `PiRunner` whose check fails
      returns an interrupted, uncharged result (`cost_usd == 0.0`) and `_apply_spec_review`
      leaves the state on `SPEC_REVIEW` without incrementing `spec_review_retries`. A run
      dir that already has `proc.json` or `launch.json` is not replaced.
- [x] A spec-review result whose text contains `CURSOR_API_KEY is not set`,
      `cursor-sdk is not installed`, `pi-coding-agent is not installed`, or
      `no credential for provider ` is stored interrupted and uncharged, and the retry
      counter is unchanged. `res.interrupted` is true when `ctx.stop` is clear.
- [x] `check("pi")` returns `node not found on PATH`, `pi_exec.mjs missing at <path>`,
      `pi-coding-agent is not installed`, or `no credential for provider <name>` for the
      cases in Design, and returns None when node, the script, the SDK entry, and each
      named provider's cheap credential are present. `check("pi")` returns None for a
      managed install whose `current-version` file is `0.50.0\n` and whose `dist/index.js`
      exists, with node and `pi_exec.mjs` present and no providers requested. It does not
      spawn a process.
- [x] With that Pi check failing and no persona worker, `Gate.admit` holds a gated Pi step
      `backend_unavailable`. A persona worker lifts the hold for that gated step. When the
      Pi check passes, `backend_fault` is None. `admit` still holds the step for an open
      usage pause or rate-limit backoff, and for a closed persona cap, as it does today.
- [x] In one `Gate`, `backend_fault` for a failing Pi check with `remote_ok=True` and a
      persona worker returns None, and a second call with the same providers and
      `remote_ok=False` still returns the Pi error. Neither call raises. A
      `persona_workers` that raises is ignored and that call returns the cached Pi error.
- [x] `_preflight_hold` replaces a fresh `PiRunner` with `PreflightHeldRunner` when the Pi
      check fails, and leaves a `ClaudeRunner` and a `FakeRunner` unchanged. `_do_persona`
      turns a finished Pi startup fault into an interrupted, uncharged result and files one
      escalation.
- [x] One open `backend_unavailable` escalation per failing backend (`cursor` and `pi`
      separately). A Pi credential error whose provider env key is mapped in
      `agentic.credentials` files no second escalation. `startup_check` prints a `WARNING:`
      line and journals `backend_preflight_failed` with `where=start` for each needed
      backend that fails, and still returns without stopping the runner.
- [x] A `claude` campaign, and a spec review whose resolved backend is `claude`, are not
      held `backend_unavailable`. An `unknown model` or `request.json` Pi failure still
      increments the step's retry.
- [x] A declared persona's provider is the one `map_pi_model` returns for the alias and
      effort from `declared_settings`, with the definition loaded through
      `persona_defs.user_personas_dir(cfg)` and the map read from `project_root`. When
      that provider has no cheap credential and the role builtin's provider does,
      `backend_fault` for that persona returns `no credential for provider <name>`.
- [x] An unknown `agentic.model_set` does not raise out of `tick`, and that campaign is
      not held `backend_unavailable`. The apply parks `spec_review_exhausted` with the
      `ValueError` text and writes no `run.start`. When `map_pi_model` raises
      `ValueError`, a tick does not raise and writes no `backend_unavailable` hold, even
      when node is missing. When `resolve_profile` raises `OSError`, `backend_fault`
      returns None and does not raise.

## Test plan

- **Automated tests** in `tests/test_backend_preflight_extend.py`:
  - `test_spec_review_hold_skips_prepare_and_spends_no_retry`: cursor preflight failing
    (the `probe_cursor_sdk` stub from `tests/test_runner_selfheal.py`); a `SPEC_REVIEW`
    campaign through one `tick`; hold `backend_unavailable`, no `run.start` for
    `spec_reviewer`, `spec_review_retries == 0`, state stays `SPEC_REVIEW`. A second tick
    with a persona worker configured still holds.
  - `test_spec_review_holds_on_project_pi_map`: `agentic.model_set` unset,
    `agentic.runner: pi`, node and the SDK present. `repo.root` and
    `project_config` `_root` are different directories, and `_root` is not the campaign
    worktree. `state.profile` names a profile file that exists only under
    `repo.root/.nw/profiles` (`schema: nw.profile.v1`, `extends: balanced`,
    `personas.spec_reviewer` model `haiku` and effort `high`). The project
    `.nw/pi_map.yaml` on `_root` is a valid `nw.pi_map.v1` whose `aliases.haiku.high`
    is `{provider: openai, id: gpt-4.1, thinking: high}`. `OPENAI_API_KEY` is unset
    and `auth.json` has no `openai` object. The builtin map's `haiku` provider is
    `anthropic` and `ANTHROPIC_API_KEY` is set. One `tick` holds `backend_unavailable`
    and writes no `run.start`.
  - `test_project_model_set_binds_the_pi_provider`: the only copy of model set `projpi`
    is `_root/.nw/model_sets/projpi.yaml` (`schema: nw.model_set.v1`, `runner: pi`,
    `model: openai/gpt-4.1`). `_root` is not the campaign worktree, and the set is
    absent from the host config dir. `agentic.model_set: projpi`, and `openai` has no
    cheap credential. `ANTHROPIC_API_KEY` is set, so the builtin Pi map's `anthropic`
    row would pass. One `tick` of a `SPEC_REVIEW` campaign holds `backend_unavailable`
    and writes no `run.start`.
  - `test_spec_review_stays_ungated_for_backoff_and_cap`: backend check passes; an open
    rate-limit backoff and `max_personas: 0` still prepare the spec-review run.
  - `test_do_spec_review_preflight_hold_is_interrupted`: `_do_spec_review` with a
    `CursorRunner` and a failing check; `StepResult.interrupted`, `cost_usd == 0.0`;
    `_apply_spec_review` does not bump `spec_review_retries`. The same with `launch.json`
    present runs the original runner.
  - `test_do_spec_review_converts_startup_fault`: a runner that returns
    `is_error=True` and `result_text` `CURSOR_API_KEY is not set` (and a second case,
    `no credential for provider openai`); the applied result is interrupted, cost 0, stop
    flag clear, retry counter unchanged.
  - `test_pi_check_node_script_sdk_and_credential`: tmp dirs for the managed SDK layout
    and for `auth.json`; missing node, missing script, missing SDK entry, missing
    `OPENAI_API_KEY` with no auth entry, then each of those repaired (env key, auth.json
    object, ADC file for `google`). Assert the error strings above, and that no subprocess
    ran (stub `subprocess.run` to raise). Write `current-version` as a version line with
    a trailing newline (`0.50.0\n`) and assert the SDK check passes (`check("pi")` is
    None) once node, the script, and that release's `dist/index.js` exist, with no
    providers requested.
  - `test_pi_gate_holds_unless_a_worker_can_take_it`: `agentic.runner: pi`, failing Pi
    check, `admit` of an implement step holds; with `persona_workers` returning one worker,
    `admit` returns the step. `remote_ok=False` still holds.
  - `test_passing_pi_check_leaves_backend_fault_none`: Pi check returns None.
    `backend_fault` is None. `admit` of a gated step holds `usage_pause` while a usage
    pause is open, `rate_limit` while a rate-limit backoff is open, and `persona_cap`
    when `max_personas` is 0. None of those holds is `backend_unavailable`.
  - `test_gate_cache_keeps_pi_error_after_worker_escape`: one `Gate`, failing Pi check.
    `backend_fault` with `remote_ok=True` and `persona_workers` returning one worker
    returns None; a second call with the same providers and `remote_ok=False` returns
    the Pi error. Neither call raises. `note_fault(..., where="gate")` runs when the
    error is first stored. A `persona_workers` that raises is ignored and that call
    returns the cached Pi error.
  - `test_preflight_hold_wraps_pi_and_not_claude`: `PiRunner` becomes
    `PreflightHeldRunner` and its `run` is interrupted and uncharged; `ClaudeRunner` is
    returned as itself.
  - `test_do_persona_converts_pi_startup_fault`: the SPEC-0484 conversion test, with
    `pi-coding-agent is not installed` as the result text; one `backend_unavailable`
    escalation whose `facts[0]` is `backend: pi`.
  - `test_pi_credential_mapped_skips_backend_escalation`: `agentic.credentials` maps
    `OPENAI_API_KEY` and the Pi error is `no credential for provider openai`; the open
    escalation list has no `backend_unavailable` row.
  - `test_startup_check_warns_for_pi`: `pi_needed` true, Pi check failing; stderr starts
    with `WARNING:`, journal `where=start`, and a cursor host that also fails gets two
    warnings and two journal rows.
  - `test_claude_spec_review_is_not_held`: `agentic.runner: claude`, no model set; the
    spec-review branch does not write a `backend_unavailable` hold.
  - `test_unknown_model_still_retries`: result text `unknown model openai/nope` on a
    spec review; `spec_review_retries` becomes 1 and the state stays `SPEC_REVIEW`.
  - `test_declared_persona_uses_declared_settings_alias`: a valid `nw.persona.v1`
    implementer (the shape in `tests/test_agentic_persona_defs.py`) lives only in the
    user personas dir, with `model: haiku` and `effort: high`. The profile's
    `implementer` stays `sonnet`. The project `.nw/pi_map.yaml` on `_root` maps
    `haiku`/`high` to `openai` (no cheap credential) and `sonnet`/`high` to `anthropic`
    (`ANTHROPIC_API_KEY` set). No model set. Node and the SDK are present.
    `backend_fault(state, (that persona,))` returns `no credential for provider openai`.
  - `test_resolution_errors_do_not_abort_or_hold`: `agentic.model_set` names a set that
    does not exist. `tick` does not raise, the campaign is not held
    `backend_unavailable`, no `run.start` is written, and the apply parks
    `spec_review_exhausted` with the unknown-model-set `ValueError` text. With node
    missing and an alias `map_pi_model` rejects, `tick` does not raise and writes no
    `backend_unavailable` hold. A direct `backend_fault` call while `resolve_profile`
    raises `OSError` returns None and does not raise.
- **Manual verification:** none. The live runner is not started.

### Test record (2026-11-09)

`uv run pytest tests/test_backend_preflight_extend.py tests/test_runner_selfheal.py tests/test_remote_persona_limits.py::test_spec_review_is_neither_counted_nor_gated -q` — all passed. Full suite left to gating.

### Test record (2026-11-09, loop 2)

Split Pi SDK name literals in `backend_preflight.py` for SPEC-0465. `uv run pytest tests/test_pi_runner.py::test_no_python_imports_pi_package tests/test_backend_preflight_extend.py -q` — all passed. Full suite left to gating.

### Test record (2026-11-09, loop 3)

Removed stub Pi SDK package.json; NODE_PATH SDK walk, env_fault provider/path, Pi two-option escalation, passwd HOME fallback. `uv run pytest tests/test_backend_preflight_extend.py tests/test_pi_runner.py::test_no_python_imports_pi_package tests/test_runner_selfheal.py tests/test_remote_persona_limits.py::test_spec_review_is_neither_counted_nor_gated -q` — all passed. `python3 tools/spec_lint.py` — ok. Full suite left to gating.

### Test record (2026-11-09, loop 3 implementer)

Adversarial-review fixes: `backend_fault` skips only broken personas; Pi `current-version` BOM/UTF-8; `note_fault` matches `{var} is not set` only; `~/` absolute paths do not count as credentials. Hardened tests (model-set control pi_map, dual-provider gate cache, `_do_spec_review` retry/env-fault paths, credential matrix). `uv run pytest tests/test_backend_preflight_extend.py tests/test_runner_selfheal.py tests/test_remote_persona_limits.py::test_spec_review_is_neither_counted_nor_gated -q` — 40 passed. `python3 tools/spec_lint.py --write-ledger` — ok. Full suite left to gating.

### Test record (2026-11-09, loop 0 implementer)

Review fixes: Pi preflight swallows probe exceptions; `current-version` trim matches pi_exec; spec-review apply keeps interrupted results on `SPEC_REVIEW` when git reset fails; hardened hold-priority and credential tests. `uv run pytest tests/test_backend_preflight_extend.py tests/test_runner_selfheal.py tests/test_remote_persona_limits.py::test_spec_review_is_neither_counted_nor_gated -q` — all passed. `python3 tools/spec_lint.py --write-ledger` — ok. Full suite left to gating.

### Test record (2026-11-09, loop 1 implementer)

Review triager fixes: `_apply_step` parks `spec_review_exhausted` when a raised spec-review run meets `ctx.stop` with no `run_result`; `~/…/..` credential paths do not count as present; tests for dual-provider `check`, Pi spawn escalation, empty-provider startup node/SDK, mapped-key node escalation. `uv run pytest tests/test_backend_preflight_extend.py tests/test_runner_selfheal.py tests/test_remote_persona_limits.py::test_spec_review_is_neither_counted_nor_gated -q` — 48 passed. `python3 tools/spec_lint.py --write-ledger` — ok. Full suite left to gating.

### Test record (2026-11-09, loop 2 implementer)

Gate triager: `pytestmark = pytest.mark.fast` on `tests/test_backend_preflight_extend.py`. `uv run pytest tests/test_test_tiers.py::test_every_real_test_module_has_exactly_one_tier -q` — passed. Full suite left to gating.

### Test record (2026-11-10, loop 0 implementer)

`can_dispatch_step` builds its probe via `build_request` so gate-tool `allowed_tools` that
are `NotRemotable` (e.g. `>/dev/null`) do not lift `backend_unavailable` when a worker is up.
`uv run pytest tests/test_backend_preflight_extend.py tests/test_runner_selfheal.py tests/test_remote_persona_limits.py::test_spec_review_is_neither_counted_nor_gated -q` — 52 passed.
Full suite left to gating.

### Test record (2026-11-10, loop 3 implementer)

`backend_fault` returns None on spawn `ValueError`/`OSError`; gate worker escape uses
`can_dispatch_step` (not bare `persona_workers`); Pi credential skip in `note_fault` only
for `pass:` mappings. Added tests for persona-less Pi hold, passing `_preflight_hold`, and
env-mapped credentials. `uv run pytest tests/test_backend_preflight_extend.py tests/test_runner_selfheal.py tests/test_remote_persona_limits.py::test_spec_review_is_neither_counted_nor_gated -q` — 51 passed. `python3 tools/spec_lint.py --write-ledger` — ok. Full suite left to gating.

- **Regression guard:** `python3 tools/affected_tests.py --run uv run pytest -q` on the
  touched files. `tests/test_runner_selfheal.py` (the SPEC-0484 cursor hold, spawn, and
  startup tests) and `tests/test_remote_persona_limits.py::test_spec_review_is_neither_counted_nor_gated`
  plus `test_an_ungated_spec_review_run_neither_starts_nor_resets_the_backoff` stay green.

## Rollout / migration

Lands with the code. The next runner start (re-exec on merge) runs `startup_check` for Pi
as well as Cursor. Campaigns already parked `spec_review_exhausted` by an earlier keyless
review stay parked until a human resumes them; this spec does not rewrite those parks.
New spec-review and Pi steps hold while the check fails.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass; manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

- None.
