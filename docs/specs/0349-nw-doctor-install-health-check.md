---
id: SPEC-0349
title: "`nw doctor`: read-only install health check with `nw.doctor.v1` JSON"
status: accepted
owner: user
created: 2026-11-01
updated: 2026-11-01
kind: feature
tags: [install, cli, diagnostics]
source_idea: IDEA-619
---

## Context

Getting nightwright working on a machine takes several pieces, and each one fails on its own
with its own message, far from where it was set up: the editable install or `nw_checkout:`
(SPEC-0166), `config.yaml` (an unset `timezone` silently defaults to `America/New_York`),
the org ideas file, the skills (`nw skills install`), shell completion (SPEC-0022), the
`agentic.conductor.prices` entry without which `conductor-launch` / `steward-launch` exit 1
(SPEC-0299/0319), each registered project's `.nw/project.yaml` and profiles (SPEC-0237), the
queue / mailbox dirs, the runner lease, a leftover pre-rename `~/.config/wt` (SPEC-0330),
Claude Code's `cleanupPeriodDays` deleting transcripts before `nw snapshot` archives them, and
the worker registry (SPEC-0337). Today the only way to check an install is to run commands
until one breaks. IDEA-619 asks for one command that checks all of it and says how to fix
what it finds.

## Goals / Non-goals

**Goals**
- `nw doctor [--json] [--project NAME] [--fix-hints]` runs every check below and reports
  each as `ok | warn | fail | skip`, with a fix hint for anything not ok.
- Read-only and offline: never writes config, data, org files or caches; makes no network
  call (SSH included).
- A stable `nw.doctor.v1` JSON shape that agents and CI can depend on; exit 0 when nothing
  fails, 1 when anything does.
- Each check is a small pure-ish function, tested on its own against a temporary HOME.
- Checks reuse the existing logic (skills status, launch price lookup, profile resolution,
  project config loading, lease status, worker registry parsing) rather than copying it.

**Non-goals**
- Auto-fixing (`--fix`). Doctor prints fixes; the user runs them.
- Live probes: SSH to workers (`nw workers check` already does that), GitHub API calls,
  running gates.
- A CI smoke test of a clean-checkout install. IDEA-619 mentions it; it is separate work
  (a workflow file, not a CLI change) and stays in the idea.
- Checking transcript or meeting content, or report correctness.

## Decision

Add a `nightwright.doctor` module with one function per check and a `run(cfg, *, project=None)`
that returns the check list, plus a top-level `nw doctor` command that renders it (Rich,
grouped) or dumps `nw.doctor.v1` JSON. Shared logic used by doctor and an existing command
moves into a named helper that both call. The root `cli` group tolerates a config load
failure when the subcommand is `doctor`, so a broken `config.yaml` is reported instead of
crashing before doctor starts.

## Design

### Module and command

- `src/nightwright/doctor.py`: `Check = dict(id, group, status, summary, detail, fix)`;
  one `check_*` function per check, each taking only what it needs (`cfg`, paths, an
  injectable `which` / `run` for subprocess tools so tests never use the real PATH) and
  returning one Check or a list. `run(cfg, *, project=None, config_error=None) -> list[Check]`
  calls them in a fixed order. A check that raises unexpectedly becomes a `fail` with the
  exception text, so a doctor bug never hides the other checks.
- `nw doctor` in `cli.py`: `--json`, `--project NAME` (with `shell_complete=C.complete_project`,
  as `REQUIRED_SHELL_COMPLETE_NAMES` requires for `project`; an unknown name is a usage
  error, exit 2), `--fix-hints`. Exit code: `1` if any check is `fail`, else `0`.
- Root group: `cli()` wraps `load_config()` in try/except **only when**
  `ctx.invoked_subcommand == "doctor"`; on error it sets `ctx.obj = None` and stores the
  message in `ctx.meta["nw.config_error"]`. Doctor then reports `config.parse` as fail and
  marks every check that needs `cfg` as `skip` ("config did not load"). Other commands keep
  failing exactly as they do now.

### `nw.doctor.v1`

```json
{"schema": "nw.doctor.v1", "ok": true,
 "counts": {"ok": 0, "warn": 0, "fail": 0, "skip": 0},
 "checks": [{"id": "tools.uv", "group": "tools", "status": "ok",
             "summary": "uv 0.9.4", "detail": null, "fix": null}]}
```

`id` is stable and dotted (`<group>.<name>`, projects as `project.<Name>.<what>`); `status`
is one of `ok|warn|fail|skip`; `summary` is one line; `detail` and `fix` are a string or null
(`fix` is null when status is `ok`/`skip`). `ok` is `counts.fail == 0`. The JSON Schema is the
`DOCTOR_SCHEMA` dict in `doctor.py`, a plain dict so the runtime does not import the dev-only
`jsonschema`; the test validates real output against it. Fields are only ever added
(additive evolution, like the `nw.*.v1` agentic schemas).

### Human output

Uses the existing helpers: `console` (`NwConsole`, so piped output never hard-wraps,
SPEC-0197) and `render.section_table` per group (title = group, columns: status glyph,
check, summary). Glyphs and styles: `✓` green, `!` yellow, `✗` red, `-` dim. Ok checks show
one line each; `detail` shows only for warn/fail. `--fix-hints` adds a `fix:` line under every
non-ok check (JSON always carries `fix`). Last line: `N ok · N warn · N fail · N skip`.

### Checks (group → id: rule)

| id | ok | warn | fail | skip |
|---|---|---|---|---|
| `tools.uv`, `tools.git`, `tools.claude` | on PATH; summary = first line of `--version` | — | missing, or `--version` fails or takes over 5 s | — |
| `tools.gh` | present with version | missing (only some merge policies need it) | — | — |
| `tools.gh_auth` | `gh auth token` exits 0 | — | a project's `merge.policy` is `pr`/`draft-pr` and gh is missing or has no token | no project needs PRs |
| `install.nw_on_path` | `shutil.which("nw")` resolves to the running entry point | resolves to a different `nw` (both paths in detail) | not on PATH (skills and agents call `nw`) | — |
| `install.checkout` | `rules.nw_checkout(cfg)` resolves (detail: editable or `nw_checkout:`) | unresolved and unset (fix: `CHECKOUT_HINT`) | `nw_checkout:` set but invalid (`configured_nw_checkout` raises) | — |
| `config.parse` | `config.yaml` exists and `load_config` worked | file absent (all defaults) | YAML error or bad timezone | — |
| `config.timezone` | `timezone` set in the raw user YAML | absent, so it defaults to `America/New_York` | — | config failed |
| `config.org_ideas_file` | file exists and is readable | missing (the first capture creates it) | exists but cannot be read | config failed |
| `data.dir` | data dir exists and `os.access(W_OK)` | absent but its nearest existing parent is writable | not writable | — |
| `skills.installed` | every `skills.status()` entry is `linked`/`copied` | any `not installed` / `copied (stale)` / `linked (other)` (fix: `nw skills install`) | — | no checkout (no source) |
| `completion.installed` | `default_install_path($SHELL)` matches `source_script` | missing, stale, or not ours | — | `$SHELL` not bash/zsh/fish |
| `agentic.prices` | every launch model has a price | — | a model without one (fix names `agentic.conductor.prices.<prefix>`) | — |
| `agentic.dirs` | agentic + mailbox dirs (or nearest existing parents) writable | — | not writable | — |
| `agentic.runner` | lease held, not stale | stale lease, or active campaigns and no runner (fix: `nw queue run --detach`) | — | no campaigns |
| `agentic.conductor` | a conductor session is registered (detail: newest id) | campaigns exist, no session registered | — | no campaigns |
| `agentic.workers` | `workers.read_strict` parses; detail: enabled/total; ssh ControlPath dir available | no usable ssh ControlPath dir | registry does not parse | no `workers.json` or empty |
| `project.<Name>.config` | `.nw/project.yaml` loads and validates; `gates_for` resolves for `loop` and `close`, at least one gate | no gates | invalid file, or root registered but missing on disk | project has no `.nw/project.yaml` |
| `project.<Name>.profiles` | every profile + `default_profile` resolves | — | any fails | no `.nw/project.yaml` |
| `legacy.wt_dirs` | no non-empty `~/.config/wt` / `~/.local/share/wt` | present (fix: `nw migrate-from-wt`) | — | — |
| `retention.cleanup_period` | `cleanupPeriodDays` ≥ 90, or newest `history.jsonl` day is younger than `cleanupPeriodDays − 7` | otherwise (default 30 when absent); fix: raise it in `~/.claude/settings.json` or run `nw snapshot` | — | — |

Projects are those in `rules.projects_payload(cfg)` with a resolvable research root;
`--project` limits the `project.*` checks (and the PR-policy scan for `tools.gh_auth`) to one.

### Reuse (no duplicated logic)

- **Prices**: add `launch.missing_prices(cfg) -> list[(role, model)]`, built on the same
  `settings(cfg, role)` + `CC.price_for` that `build_plan` uses, for roles conductor and
  steward. `check_price` stays as is; doctor calls `missing_prices`.
- **Profiles**: move the loop body of `profiles_check` into `profiles.check_all(cfg, *,
  project_root) -> list[{name, ok, error}]` (including the `default_profile` addition);
  `nw profiles check` and doctor both call it, and `nw profiles check` output is unchanged.
- **Skills**: `skills.status(dest, source=skills.repo_skills_dir(cfg))`, the same call
  `nw skills list` makes.
- **Projects / lease / workers / legacy / completion**: `project.project_config`,
  `project.gates_for`, `lease.lease_status`, `store.list_campaigns`, `conductor_cost.sessions`,
  `workers.read_strict` + `workers.ssh_dir`, `migrate.legacy_config_dir/legacy_data_dir`,
  `completion.default_install_path` + `source_script`, all called directly.

### Reasoning for the judgment calls

- **gh auth is checked with `gh auth token`, not `gh auth status`.** `gh auth status` calls the
  GitHub API, which breaks the no-network rule; `gh auth token` reads the stored credential
  locally. It shows a token exists, not that it is still valid; detail says so.
- **Workers are checked statically.** `nw workers check` probes over SSH, which is network.
  Doctor validates the registry and the ControlPath dir and points at `nw workers check` for
  the live probe in its detail. A `--probe` flag can come later if the static check is not
  enough.
- **Writability uses `os.access`, not a probe file**, so doctor writes nothing.
- **A missing price is `fail`** because the launch commands exit 1 without it; a missing
  ideas file, completion or skills only reduce what works, so they are `warn`.
- **Retention threshold 90 days / 7-day margin**: Claude's default is 30, so a weekly
  `nw snapshot` habit keeps it safe; tune from reports.
- **A missing `.nw/project.yaml` is `skip`**: outbox-only projects are valid and never run
  through the queue.

## Alternatives considered

- **Run each existing command (`nw profiles check --json`, `nw skills list`, …) as a
  subprocess and collect the results** — no refactors, but slow (a Python start per check),
  some of those commands have no JSON, and checks could not be tested alone. Rejected.
- **`nw doctor --fix`** — convenient, but it breaks the read-only guarantee and every fix
  already exists as its own command. Rejected for now.
- **Live SSH and `gh auth status` by default** — more thorough, but doctor would hang
  offline or behind a VPN and stop being safe to run anywhere. Rejected; the static checks
  point to the live commands.
- **Print checks as a flat list** — simpler, but 20+ lines are hard to scan. Rejected for
  grouped tables.

## Acceptance criteria

- [ ] `nw doctor` exists with `--json`, `--project NAME` (with a completer) and `--fix-hints`;
      `nw doctor --help` lists them; the completion guard (`tests/test_completion.py`) passes.
- [ ] Every check in the Design table is a separate function in `src/nightwright/doctor.py`
      and returns the statuses the table gives for its ok / warn / fail / skip cases.
- [ ] `--json` output validates against `doctor.DOCTOR_SCHEMA` (`schema: nw.doctor.v1`,
      `checks[]` with `id, group, status, summary, detail, fix`, `status ∈ ok|warn|fail|skip`),
      and `ok == (counts.fail == 0)`.
- [ ] Exit code is 0 when no check fails (warns allowed) and 1 when any check fails.
- [ ] Doctor is read-only: after a run against a temporary HOME, the files under HOME / XDG
      dirs are byte-identical and no new files exist; no check opens a socket or runs
      `ssh` / `gh auth status`.
- [ ] A malformed `config.yaml` gives `config.parse: fail` (with the YAML error) and exit 1
      instead of a traceback; other commands behave as before with that file.
- [ ] Doctor reuses the existing logic: `launch.missing_prices` and `profiles.check_all`
      exist and are what doctor and the existing commands call; `nw profiles check`
      output and exit codes are unchanged.
- [ ] `--project NAME` limits the `project.*` checks to that project; an unknown name exits 2.
- [ ] Human output is grouped by check group in `render.section_table` tables, shows `fix:`
      lines only with `--fix-hints`, and ends with the counts line.
- [ ] Docs: `doctor` is listed in `docs/product/reference/index.md` (the SPEC-0146 test
      passes); README.md and `docs/product/guides/getting-started.md` tell the reader to run
      `nw doctor` after installing.

## Test plan

- **Automated tests** (`tests/test_doctor.py`). An autouse fixture points `HOME`,
  `XDG_CONFIG_HOME`, `XDG_DATA_HOME`, `XDG_CACHE_HOME`, `NW_CONFIG_DIR`, `NW_DATA_DIR`,
  `NW_CACHE_DIR` and `SHELL` at `tmp_path`, and gives checks a fake `which` / `run` (or a
  `tmp_path/bin` of stub scripts on `PATH`), so no test reads the real home or the real
  tools.
  - **One test per check**, covering each row of the Design table: tools present / missing /
    hanging past the timeout; gh missing → warn; gh_auth skipped without PR policies and
    failing when `merge.policy: pr` with no token; nw on PATH same / different / missing;
    checkout resolved / unset / invalid `nw_checkout`; config absent / valid / malformed /
    bad timezone; timezone set / absent; ideas file present / missing / unreadable
    (chmod 000, skipped under root); data dir writable / absent-with-writable-parent /
    read-only; skills linked / not installed / stale copy (installed into the tmp dest
    with `skills.install`); completion matching / missing / stale / unknown shell; prices
    present / missing for the conductor and for a steward-only model override; agentic
    dirs; runner no queue → skip, held lease → ok, stale lease → warn, active campaigns
    without a lease → warn; conductor session present / absent; workers absent → skip,
    valid → ok, unparseable → fail; a project with valid `.nw/project.yaml`, an invalid one,
    one with no gates, one without the file → skip, a broken profile → fail; legacy `wt` dirs
    present → warn; cleanupPeriodDays absent with old history → warn, 365 → ok, 30 with a
    fresh history.jsonl → ok.
  - **Schema test**: `nw doctor --json` (CliRunner) validates against `DOCTOR_SCHEMA` with
    `jsonschema`; also assert `ok == (counts.fail == 0)` and the counts match the list.
  - **Exit-code test**: an environment with only warns → exit 0; add a missing price →
    exit 1; a malformed `config.yaml` → exit 1, `config.parse` fail, no traceback.
  - **Clean-environment test**: a tmp HOME set up correctly (config with `timezone` and
    prices, ideas file, skills installed, completion installed, stub tools) → no `warn`
    or `fail` apart from those the test lists by id (for example `install.nw_on_path`
    under the test runner), exit 0.
  - **Read-only test**: snapshot every file under tmp HOME (path → bytes) before and after
    `run(cfg)` and assert equality; patch `socket.socket` to raise and `subprocess.run` to
    reject `ssh` and `gh auth status`, and assert doctor still finishes.
  - **`--project` test**: only that project's `project.*` ids appear; unknown name → exit 2.
  - **Refactor guards**: existing `nw profiles check` tests and the launch price tests
    still pass; one new test each for `launch.missing_prices` and `profiles.check_all`.
- **Docs / guard tests**: `tests/test_completion.py` (REQUIRED_SHELL_COMPLETE_NAMES,
  SPEC-0039) and `tests/test_docs_product_reference.py` (SPEC-0146) pass with the new
  command.
- **Manual verification**: on the dev machine run `uv run nw doctor`,
  `uv run nw doctor --fix-hints`, `uv run nw doctor --json | python3 -m json.tool` and
  `uv run nw doctor --project Meta-Tools`; record in As shipped which checks were not ok and
  whether each finding was true. Then `chmod -w` a scratch `NW_DATA_DIR` and confirm
  `data.dir` fails and the exit is 1.
- **Regression guard**: affected tests while iterating
  (`python3 tools/affected_tests.py --run uv run pytest -q`); the full suite runs at the
  queue close gate.

## Docs

The implementing campaign updates, in the same change:
- `docs/product/reference/index.md`: a `doctor` row (required by the SPEC-0146 test).
- `docs/product/guides/getting-started.md`: after the Install block, "run `nw doctor` (add
  `--fix-hints` for the commands that fix each finding)".
- `README.md`: the same one-line step in whatever install / tour section main has when the
  campaign runs. The README rewrite (with a quick tour) is on branch `docs/readme-refresh`
  and may not be on main yet. The implementation must not depend on it: edit the README on
  main, and if the tour has landed, add `nw doctor` to it. No test checks for a particular
  README section.
- `docs/product/capabilities/agentic.md`: one line saying `nw doctor` checks prices, dirs,
  runner and workers.

## Clock Log

Use `nw idea clock-in IDEA-619` / `nw idea clock-out IDEA-619`.

## Rollout / migration

Additive: a new command and module, two extracted helpers whose callers behave the same, and
a root-group change that only affects `nw doctor`. No data or config migration.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; affected tests pass; manual steps performed and noted here.
- [ ] No regressions (full suite at the queue close gate).
- [ ] Spec body updated to match what shipped (As shipped section).
- [ ] `docs/LEDGER.md` regenerated; `python3 tools/spec_lint.py` exits 0.

## Open questions

_None._ (The CI clean-checkout smoke test in IDEA-619 is out of scope here; see Non-goals.)
