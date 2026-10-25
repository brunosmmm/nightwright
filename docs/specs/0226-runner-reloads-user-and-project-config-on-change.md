---
id: SPEC-0226
title: "Queue runner reloads user and project config on change"
status: done
owner: user
created: 2026-10-25
updated: 2026-10-25
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0169
tags: [agentic, runner, queue, config, profiles, watchdog]
source_idea: IDEA-099
depends_on: [SPEC-0194, SPEC-0213, SPEC-0224]
---

## Context

Child of epic [SPEC-0169](./0169-mini-agentic-workflow-queue-campaigns-headless.md),
promoted from bug `IDEA-099` ("queue runner never reloads user config: profile/slot changes
need a manual restart").

`wt queue run` gets its `cfg` once, from `config.load_config()` in the CLI root
(`cli.py:57`), and keeps it for the life of the process. The only refresh is the
[SPEC-0194](./0194-runner-follows-merged-code-re-exec-between-ticks.md) re-exec after a code
change. Every profile lookup reads that dict: `_Ctx.profile` → `resolve_profile(cfg, name)`,
`slots_for(cfg)`, persona model, effort and budget in `PS.build_request`. So an edit to
`~/.config/wt/config.yaml` (slots, a profile's persona model or budget) is ignored until a
restart. On 2026-10-25 the steward told the conductor "nothing needs restarting" after
switching `gate_triager` to Opus. That was wrong for every runner-spawned persona.

The project side is different. `.wt/project.yaml` is already re-read on every tick, because
`tick()` builds a fresh `_Ctx` and `_Ctx.project` calls `P.project_config`, which parses the
file. An invalid file parks the affected campaigns `config_invalid` and recovers them once it
loads again (SPEC-0194). What is missing there is visibility: nothing records that a project
config changed, or that it broke.

## Goals / Non-goals

**Goals**
- Edits to the user config take effect at the next tick, without a restart, for admissions
  and newly planned runs. In-flight steps keep the settings they were planned with.
- Every effective change is journaled as `config_reloaded`, with the changed keys, for the
  user config and for each project config.
- An invalid user config never replaces the working one. The runner keeps the old config and
  the conductor gets a watchdog event.

**Non-goals**
- Changing the SPEC-0194 behaviour for an invalid `.wt/project.yaml` (park `config_invalid`,
  auto-recover). That is a past decision. This spec only adds the journal record and the
  event.
- Hot-applying settings bound at start: the lease TTL and the heartbeat interval. They are
  reported as `restart_required`.
- Watching any other file (mappings, registry) or reloading in short-lived commands.

## Decision

`run()` gains `config_loader` (a callable returning a fresh cfg). The CLI passes
`load_config`. Tests and library callers that pass none get today's behaviour. Before each
tick the loop calls `ConfigWatch.check()`. It stats the user config file and the
`.wt/project.yaml` of every project with a non-terminal campaign, and compares
`(mtime_ns, size)` with the last seen. On a user-config change it loads and validates a
new cfg. If valid, the loop uses it from this tick on and journals `config_reloaded`. If
invalid, it keeps the old cfg, journals `config_load_failed`, and records the error in
the lease info under its path, from which the watchdog emits `runner_config_invalid`. A
missing, empty or non-mapping user file counts as invalid: the runner never falls back to
pure defaults. On a project-config
change it parses once to compute the changed keys and journals `config_reloaded` (or the
invalid path). The per-tick re-read already applies the change.

## Design

### `queue.py`: `ConfigWatch`

- `ConfigWatch(cfg, loader)` remembers `user_path = Path(cfg["config_dir"]) /
  "config.yaml"`, its stat key `(mtime_ns, size)` (or `None` when the file is missing), the
  last good cfg, and per project `{path: (stat key, last good parsed doc)}`. Project docs are
  seeded lazily on first sight, with no record.
- `check(cfg, lease, campaigns) -> cfg`, called in `run()` before each `tick(...)`:
  - **User config:** stat changed → `new = loader()`, then `validate_runner_cfg(new,
    campaigns)`:
    - `resolve_profile(new, name)` succeeds for `agentic.default_profile` and for every
      profile named by a non-terminal campaign;
    - `slots` resolves to an int ≥ 1 for each of those profiles;
    - `agentic.poll_interval_s` and `agentic.lease_ttl_s` are numbers > 0. `lease_ttl_s` is
      checked even though it only applies after a restart, because a bad value would crash
      `_heartbeat_while` / `lease_status` after the next re-exec;
    - `agentic.runner` is `claude` or `fake`.

    **Before** calling the loader, the watch reads the raw file itself. A file that is
    missing (when one was seen before), empty or whitespace only, or whose `yaml.safe_load`
    is not a mapping, is invalid. `load_config` would otherwise silently return pure defaults
    (`_load_yaml` returns `{}`), for example when an editor or a `>` redirect truncates the
    file before writing it. Truncation is transient: the next tick sees the new stat key and
    loads the complete file. If no user file existed at start and none exists now, nothing
    has changed. The raw read, `loader()` and `validate_runner_cfg` all run inside one
    `except Exception`, so a bad timezone (`ZoneInfoNotFoundError`), a non-string path
    (`TypeError`), a YAML error or anything else is recorded as invalid with
    `f"{type(e).__name__}: {e}"` and never escapes into `run()`'s crash handler.
    - valid and any key changed → journal `config_reloaded {scope: "user", path, changed,
      restart_required}`, remove the user path's entry from the lease `config_errors` if it
      is there, and return `new`.
      Valid but no key changed (a `touch`, or a whitespace edit): no record. The stat key is
      updated either way;
    - invalid → journal `config_load_failed {scope: "user", path, error}` once per stat key,
      set the user path's entry in the lease `config_errors`, and return the **old** cfg.
  - **Lease `config_errors`:** a dict `path → {scope, error, since, mtime_ns}`. Each file
    sets and clears only its own entry. All writes use SPEC-0224's thread-safe lease API:
    `lease.update_info(config_errors={**current, path: entry})`, a new dict every time, and
    clearing writes a new dict without that path (`pop_info("config_errors")` when it becomes
    empty). The heartbeat thread never sees a dict being changed. Several broken files
    (user + projects) are reported at once, and fixing one never hides another.
  - **Project configs:** for each project of a non-terminal campaign, resolve
    `P.config_path(P.project_root(cfg, project))`. A stat change → `P.load_project_config`:
    - ok → journal `config_reloaded {scope: "project:<name>", path, changed}` when keys
      changed;
    - any `Exception` → journal `config_load_failed {scope: "project:<name>", path, error}`
      once per stat key and set that path's `config_errors` entry. Behaviour is otherwise
      unchanged: the next tick parks affected campaigns `config_invalid` per SPEC-0194. A
      later valid parse clears that path's entry only.
    Errors from `project_root` (an unregistered project) are left to the tick, which already
    reports them.
- **Changed keys** (`config_diff(old, new) -> list[str]`): flatten both dicts to dotted
  paths, recursing into mappings only (lists compare whole). Drop keys starting with `_`
  and the runtime paths `config_dir`, `data_dir` and `cache_dir`. Return the sorted paths
  whose values differ, plus added and removed ones, capped at 50 with a trailing `"…"`. For
  example, `agentic.profiles.balanced.personas.gate_triager.model`.
- **`restart_required`**: the changed keys under `agentic.lease_ttl_s`. That is the only
  runner setting bound at start (lease TTL and heartbeat interval, SPEC-0204).
  `agentic.poll_interval_s` is applied: `run()` recomputes `interval` from the new cfg
  unless `--poll-interval` was given on the command line.
- **Apply semantics.** `run()` rebinds its local `cfg` to the returned dict. The next
  `tick(cfg, …)` builds a new `_Ctx`, so admissions use the new `slots_for(cfg)` and new
  runs use the new profiles (model, effort, budget, timeout in `run.start`). An in-flight
  `_Flight` keeps its own `ctx`, and with it the old cfg and resolved profiles, which
  SPEC-0213 already stores per flight. Its run continues unchanged, and its harvest decisions
  (`decide_reviews`, caps) use the profile it was planned with. Choice: one profile for the
  whole of a step is easier to reason about than a step started on one profile and judged on
  another. The next planned step uses the new one. `journal`, `events` and lease writes take
  the new cfg. Their paths come from env/XDG and cannot change.

### Watchdog (`events.py`)

- `lease_status` exposes `config_errors` (or `{}`) when the lease is held.
- `watchdog_checks` emits, for each `config_errors` entry,
  `("runner_config_invalid:<path>:<mtime_ns>", "runner_config_invalid", None, {scope, path,
  error, since})`. That is one condition per broken file version, and each clears when its
  own entry goes away.
- `EVENT_TYPES["runner_config_invalid"] = ("scope", "path", "error")`.
- `skills/wt-conduct/SKILL.md` row: `runner_config_invalid` with scope `user` → the human's
  config file is broken. The runner keeps the last good config, so nothing is stopped. Tell
  the human the path and the error in one line, and do not edit their config. With scope
  `project:<P>` → as `parked config_invalid`: if a steward is armed, mail it the error,
  otherwise tell the human.

### Journal and display

- New runner-journal kinds `config_reloaded` and `config_load_failed` (`wt.runner.log.v1`,
  additive). The second one is deliberately not named `config_invalid`, which is already a
  park reason in campaign ledgers, so `runner-log` greps stay unambiguous. `wt queue
  runner-log` renders them as one line: `config reloaded (user):
  agentic.profiles.balanced.slots, …` and `config load failed (project:wt): <error>`.

## Alternatives considered

- **Re-exec on config change (as SPEC-0194 does for code).** That would bring back the
  drain stall IDEA-100 is about, for a change that needs no new code. Rejected.
- **inotify / watchdog library.** A new dependency and a thread, for a file read once per
  tick anyway. `stat` per tick costs microseconds. Rejected.
- **Re-read the user config every tick, with no mtime check.** It costs a YAML parse per
  tick and gives no change point to journal. Rejected.
- **Keep the last good project config when `.wt/project.yaml` is invalid (as for the user
  config).** That would reverse SPEC-0194's deliberate `config_invalid` park, which stops
  gates from running under a config the human is mid-way through editing. It would need a
  superseding spec. Out of scope.
- **Apply the new profile to in-flight steps' harvest too.** A step would be planned with
  one pass score or cap and judged with another. Rejected.

## Acceptance criteria

- [x] With the runner running (`config_loader` given), editing the user config's profile
      persona model and the profile `slots` takes effect at the next tick. The next
      `run.start` carries the new model, and admission uses the new slot count. A persona
      run already in flight keeps its old model, and its harvest decision uses the profile it
      was planned with.
- [x] Each effective user-config change journals exactly one `config_reloaded` with `scope:
      user`, the sorted dotted `changed` keys (no `_*` or runtime path keys) and
      `restart_required` (`agentic.lease_ttl_s` when it changed). A `touch` or an edit that
      changes no key journals nothing.
- [x] A user config that is invalid leaves the old cfg in use, and the next ticks admit and
      run as before. Invalid covers: fails to parse; not a mapping; empty, truncated or
      deleted; bad timezone or path type; fails `validate_runner_cfg` (unknown profile for a
      live campaign, `slots` < 1, bad `poll_interval_s` / `lease_ttl_s`, unknown runner). The
      runner never crashes and never reloads pure defaults. It journals one
      `config_load_failed` per broken file version, sets that path's lease `config_errors`
      entry, and `watchdog_checks` emits one `runner_config_invalid`. Fixing the file
      reloads it, journals `config_reloaded`, and clears only that path's entry and condition.
- [x] Editing a live campaign's project `.wt/project.yaml` journals `config_reloaded` with
      `scope: project:<name>` and the changed keys. An invalid one journals
      `config_load_failed` and raises `runner_config_invalid`, while the SPEC-0194
      `config_invalid` park and recovery behave as before. With the user config and a project
      config broken at once, both conditions are raised, and fixing one leaves the other.
- [x] `agentic.poll_interval_s` changes apply to the inter-tick wait unless
      `--poll-interval` was given.
- [x] `run()` without `config_loader`, and `wt queue tick`, never reload (existing tests are
      unaffected). `wt queue run` passes `load_config`.
- [x] `wt queue runner-log` renders both new record kinds, and the conductor skill has the
      `runner_config_invalid` row.
- [x] `uv run pytest` passes; `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests:** new `tests/test_agentic_config_reload.py`, offline, with FakeRunner
  and tmp XDG dirs. The loader is a real `load_config` with `XDG_CONFIG_HOME` pointed at
  `tmp_path`, so the real merge path is exercised. File edits use `os.utime` with a bumped
  `mtime_ns` so that a same-size edit is detected deterministically.
  - `test_profile_model_change_applies_to_next_run`: campaign A's implementer is in flight
    (FakeRunner `sleep_s`). The config changes `implementer.model`. A's in-flight run's
    `run.start.model` is the old one, and the next run (A's triager or B's implementer)
    carries the new one.
  - `test_inflight_harvest_uses_planning_profile`: a reviewer is in flight under
    `review.pass_score: 3` and returns score 3. The config changes it to 5 before the
    harvest. The campaign passes review (the old threshold), and the next review pass uses 5.
  - `test_slots_change_applies_to_admission`: slots 1 → 2 mid-run. A second ready item is
    admitted at the next tick.
  - `test_reload_journal_changed_keys_and_restart_required`: changing `lease_ttl_s` and a
    persona budget gives one record with both keys and `restart_required ==
    ["agentic.lease_ttl_s"]`. A `touch` gives no record.
  - `test_invalid_user_config_keeps_old` (parametrized): broken YAML, a list document, a
    scalar document, an empty file, a truncated (zero-byte) file, a deleted file,
    `timezone: Bogus/Zone`, `work_root: 5`, `lease_ttl_s: "x"`, and an unknown default
    profile. For each: `run()` does not crash, the ticks keep admitting with the old slots,
    there is one `config_load_failed` per version, the lease `config_errors` has the user
    path, and `watchdog_checks` gives one `runner_config_invalid`. Fixing the file gives
    `config_reloaded` and an empty condition.
  - `test_two_broken_files_reported_independently`: the user config and a project yaml are
    broken together, which gives two conditions. Fixing the project file clears only its
    own condition.
  - `test_config_errors_updates_race_heartbeat`: the heartbeat thread runs in a tight loop
    while `config_errors` entries are set and cleared. No exception, and the file is always
    valid JSON.
  - `test_project_config_change_journaled`: editing `gates` in the project yaml journals
    `scope: project:<name>` with `gates…` keys. An invalid yaml journals `config_load_failed`,
    raises the event, and the campaign parks `config_invalid` (SPEC-0194 path unchanged).
  - `test_poll_interval_reload_respects_cli_override`.
  - `test_no_loader_no_reload`: `run()` without a loader ignores file edits.
  - `test_config_diff`: flattening, `_`/runtime keys dropped, list compared whole, the cap.
  - `test_runner_log_renders_config_records`.
- **Existing suites unchanged:** `tests/test_agentic_reexec.py` (SPEC-0194 config parks),
  `tests/test_agentic_profiles.py`, `tests/test_agentic_nonblocking.py`,
  `tests/test_agentic_events.py`, `tests/test_agentic_logs.py`.
- **Regression guard:** full `uv run pytest`.
- **Manual verification:** with the live runner up, change a persona model in
  `~/.config/wt/config.yaml`. `wt queue runner-log --tail 3` shows `config reloaded (user):
  agentic.profiles.<p>.personas.<persona>.model`, and the next `run.start` of that persona
  (`wt campaign log <id>`) carries the new model.

### Test record (2026-10-25)

- `uv run pytest tests/test_agentic_config_reload.py`: 28 passed (all tests named in the plan;
  `test_invalid_user_config_keeps_old` runs 13 cases, plus extra tests for validation, the
  removed-profile case and `wt queue run` passing `load_config`).
- `uv run pytest -q -x`: 2056 passed, 1 skipped (full suite, including the existing
  reexec, profiles, nonblocking, events and logs suites).
- `python3 tools/spec_lint.py`: exits 0 after `--write-ledger`.
- Review round 1: added tests for the SKILL.md row, one record per broken version, fix order of
  two broken files, leftover lease errors, dropped project entries, first-sight broken project
  yaml, project name mismatch, the truncation race, and the deferred re-exec. Full suite
  re-run: 2065 passed, 1 skipped.
- Manual verification: the loop-level behaviour is exercised by `test_run_applies_reload_between_ticks`
  and `test_poll_interval_reload_respects_cli_override`, which call the real `Q.run` with the real
  `load_config` over a tmp config dir. The check against the live production runner
  (`wt queue runner-log --tail 3` after editing a persona model) happens once this merges and the
  runner re-execs; it was not possible from the campaign worktree.

### As shipped

As designed, plus the review-round changes below. Small choices made while building:

- `ConfigWatch` lives in `queue.py` next to `run()`, with `config_diff` and
  `validate_runner_cfg` as module-level functions.
- A project config that is already broken the first time the watch sees it is reported
  (`config_load_failed` + lease entry); a later fix then clears the entry without a
  `config_reloaded` record, since there is no good document to diff against.
- Lease `config_errors` entries for projects that no longer have a live campaign are dropped,
  and entries left in the lease by the image before a SPEC-0194 re-exec are cleared on the first
  check.
- Review round 1 changes. A SPEC-0194 re-exec while the user config is broken would start an
  image that runs `load_config()` unprotected (crash, or pure defaults for an empty file) and
  whose first check clears the condition. Instead of re-validating in the new image, `run()`
  defers the code-change re-exec while `ConfigWatch.user_invalid()` is true and journals
  `reexec_deferred {reason: "user_config_invalid", to_sha}` once per head (new additive runner
  journal kind, rendered by `runner-log`). `code_sha` is kept, so the re-exec happens on the
  first check after the file is fixed. The deferral sees a break one tick late: a commit that
  lands in the same tick as the break can still re-exec.
- `_reload_user` re-stats the file after `loader()`; if the stat key moved (a truncating write
  in between) the result is dropped without a record and the file is re-read next tick.
- `_check_projects` applies the same `project:` name check as `P.project_config`; a mismatch
  is reported as an invalid project config.
- `test_inflight_harvest_uses_planning_profile` checks that a review scoring 88 passes under the
  `pass_score: 80` it was planned with after the config raised it to 95; it does not run a second
  review pass.

## Rollout / migration

No data migration. The live runner picks this up through its SPEC-0194 re-exec. The first
`ConfigWatch` seeds its stat keys silently, so starting the runner journals nothing.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

_None — decisions locked above._
