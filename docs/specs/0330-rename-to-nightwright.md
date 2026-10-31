---
id: SPEC-0330
title: "Rename wt to nightwright (CLI nw): hard cut, legacy-readable stored data, migrate-from-wt, cutover runbook"
status: accepted
owner: user
created: 2026-10-31
updated: 2026-10-31
milestone: "M9: agentic workflow"
kind: feature
source_idea: IDEA-426
tags: [rename, packaging, cli, migration, agentic, skills]
---

## Context

The tool was named `wt` ("work time") after its passive time-tracking origin. It is now mostly
a spec-to-merge loop that builds work while the human is away. On 2026-10-30 the human chose the
name **nightwright** (an overnight maker), with **`nw`** as the short form everywhere. PyPI and
npm had `nightwright` free; PyPI `nw` is taken, so the distribution is `nightwright` and the
console script is `nw` (IDEA-426, Summary).

The live runner, conductor and stewards run from the main checkout (`/home/bruno/work/wt`) as an
editable install. The rename therefore ships as a branch (`feat/rename-nightwright`) that is
ready to land, plus a migration command and a runbook. The steward and the human carry out the
cutover together. The branch is not merged before then.

## Goals / Non-goals

**Goals**
- Rename every user-facing name: the package, the CLI, env vars, config and data dirs,
  per-project config, the agentic branch/commit/worktree names, skills, allow rules, docs and
  tools.
- Keep every stored record written before the rename readable.
- Provide one idempotent, dry-run-by-default command that moves an existing install.
- Provide an exact, ordered cutover runbook.

**Non-goals**
- A `wt` alias, or any fallback from `WT_*` env vars or `~/.config/wt` (hard cut).
- Rewriting history: ledgers, events, mailbox, decisions, reviews, exports, the ideas file and
  the bodies of past specs (`docs/specs/NNNN-*.md`) keep their `wt` text.
- Renaming the branches of existing campaigns (their git refs exist).

## Decision

Human decisions, 2026-10-31 (IDEA-426 Log):

1. **Hard cut.** There is no `wt` console script and no `WT_*` fallback. Only the `nightwright`
   and `nw` names exist.
2. **Scope is everything:** code, docs, skills, the GitHub repo (`brunosmmm/wt` becomes
   `brunosmmm/nightwright`) and the local checkout (`/home/bruno/work/wt` becomes
   `/home/bruno/work/nightwright`).
3. **Steward's call on stored data:** readers keep accepting the legacy `wt.<x>.v1` schema
   ids, and new writes use `nw.<x>.v1`. Stored data is a format, not a user-facing name, so
   history files are not rewritten.

Implementation decisions (agent's call, recorded here):

- Prose says **nightwright**; commands, code spans and short identifiers say **`nw`**.
- The config key `wt_checkout` becomes `nw_checkout` (hard cut). The migration renames the key
  in the moved `config.yaml`.
- The export scheme `wt-native` becomes `nw-native` (hard cut). The migration rewrites a
  `scheme: wt-native` config value.
- The actor id that nightwright itself records (`by`) becomes `"nw"`.
- Follow-up idea markers become `nwfu:` and `nwfug:`. Readers also match `wtfu:` and `wtfug:`, so
  open follow-up groups filed before the cutover are found again and not filed twice.
- Commit subjects are `nw(<campaign>): …`. The journey view strips both `nw(…):` and `wt(…):`.
- Sheet manifests: the column owner `"wt"` is read as `"nw"`, because user manifests are stored
  config.
- `.wt-skill.json` (the `--copy` install marker) becomes `.nw-skill.json`.
- Local variables named `wt`, `wts`, `wt_path`, `branch_wt` and similar abbreviate **worktree**,
  not the tool. They are kept.

## Design

### Inventory of renamed surfaces

| Surface | Before | After |
|---|---|---|
| Package dir | `src/wt/` | `src/nightwright/` (`git mv`) |
| Dist name / version lookup | `wt` | `nightwright` (`importlib.metadata.version("nightwright")`) |
| Console script | `wt = "wt.cli:cli"` | `nw = "nightwright.cli:cli"`; no `wt` |
| Hatch wheel packages | `src/wt` | `src/nightwright` |
| Module entry (`python -m`) | `-m wt` | `-m nightwright` (runner re-exec) |
| Imports, `monkeypatch` targets | `wt.<mod>` | `nightwright.<mod>` |
| Program name, help, docstrings, errors | `wt …` | `nw …` (error prefix `nw:`) |
| Root click group class | `WtRootGroup` | `NwRootGroup`; `WtConsole` → `NwConsole` |
| Completion | `_WT_COMPLETE`, `wt.fish`, bash `wt`, zsh `_wt`, fish fn `_wt_completion` | `_NW_COMPLETE`, `nw.fish`, `nw`, `_nw`, `_nw_completion` (from Click's prog name) |
| Env vars | `WT_CONFIG_DIR`, `WT_DATA_DIR`, `WT_CACHE_DIR`, `WT_MAILBOX_ADDR`, `WT_LAUNCH_KEYS`, `WT_TEST_WAIT_S`, `WT_CAMPAIGN`, `WT_SPEC_ID`, `WT_WORKTREE`, `WT_BASE`, `WT_REBASE_TARGET`, `WT_PERSONA_RUN`, `WT_RUNNER_LOG`, `WT_RUNNER_LEASE_FD`, `WT_RUNPROC_GRACE_S`, `WT_AGENTIC_RUNNER`, `WT_AGENTIC_FAKE_SCRIPT`, `WT_INSTALL_*` | `NW_*` (same suffixes) |
| Config dir | `$XDG_CONFIG_HOME/wt`, `~/.config/wt` | `…/nightwright` (`paths.APP`) |
| Data dir | `$XDG_DATA_HOME/wt`, `~/.local/share/wt` | `…/nightwright` |
| Cache dir | `~/.cache/wt` | `~/.cache/nightwright` |
| Checkout config key | `wt_checkout` | `nw_checkout` (`rules.nw_checkout`, `discover_nw_checkout`) |
| Checkout detection | `src/wt` + `pyproject.toml` | `src/nightwright` + `pyproject.toml` (`rules`, `codewatch`) |
| Per-project config | `.wt/project.yaml`, `.wt/profiles/` | `.nw/project.yaml`, `.nw/profiles/`; this repo's `.wt/` is `git mv`'d to `.nw/` |
| Campaign branches | `wt/<campaign>` | `nw/<campaign>` (new campaigns only) |
| Commit prefix | `wt(<campaign>): …` | `nw(<campaign>): …` |
| Worktree root | `<repo>/../.wt-worktrees/<project>` | `<repo>/../.nw-worktrees/<project>` |
| Follow-up markers | `wtfu:`, `wtfug:` | `nwfu:`, `nwfug:` (legacy matched on read) |
| Schema ids (all JSON/JSONL/YAML writes) | `wt.<x>.v1` | `nw.<x>.v1` (legacy accepted on read) |
| Export scheme | `wt-native` (`wt_native.py`, `WtNativeScheme`) | `nw-native` (`nw_native.py`, `NwNativeScheme`) |
| Campaign export manifest key | `wt_version` | `nw_version` |
| Sheet column owner | `"wt"` | `"nw"` (`"wt"` read as `"nw"`) |
| Skills | `skills/wt-*`, `name: wt-*`, `/wt-xyz` | `skills/nw-*`, `name: nw-*`, `/nw-xyz` |
| Skill copy marker | `.wt-skill.json` | `.nw-skill.json` |
| Allow rules (SPEC-0320) | `Bash(wt <g> *)`, `Bash(WT_MAILBOX_ADDR=* wt <g> *)` | `Bash(nw <g> *)`, `Bash(NW_MAILBOX_ADDR=* nw <g> *)` |
| Temp/thread names | `.wt-export-`, `.wt-portable-`, `.wt-org-`, `wt-release-`, `wt-step`, `wt-lease-heartbeat` | `.nw-…`, `nw-…` |
| Docs | README, AGENTS.md, docs/README.md, docs/WORKFLOW.md, docs/product/**, docs/specs/TEMPLATE*.md, SCHEMA.md, mkdocs.yml (`site_name`, `site_url`) | nightwright / `nw` |
| Docs captures | `cli-wt-dashboard.svg`, `wt&#160;…` inside SVGs | `cli-nw-dashboard.svg`; command text `nw` (same width, so the layout is unchanged) |
| Tools | `tools/affected_tests.py` (`SRC_PKG`, `src/wt`, `.wt/` trigger), `tools/spec_lint.py` | `nightwright`, `src/nightwright`, `.nw/` |
| Tests | every import, path and env var; `tests/waits.py` reads `NW_TEST_WAIT_S`; `test_wt_checkout.py` → `test_nw_checkout.py` | |
| Personas, profile defs, project template | `wt …` commands, `schema: wt.profile.v1` | `nw …`, `schema: nw.profile.v1` |

**Left as `wt` on purpose:** the bodies of historical specs `docs/specs/NNNN-*.md`, the
generated `docs/LEDGER.md` (built from their titles), `docs/design/**` (dated design evidence),
`ideas/*.org` (the shared ideas file), the legacy-reader constants (`legacy.py`,
`followups.LEGACY_*`, `agent_permissions.LEGACY_RULES`, `skills.LEGACY_PREFIX`/`_LEGACY_MARKER`,
`journey._COMMIT_PREFIX`, `sheet_schemes._LEGACY_OWNERS`), `migrate.py` and the tests that
exercise them, and local variables that abbreviate *worktree*.

### Legacy readers

`src/nightwright/legacy.py` provides `schema_ok(value, schema)`, which accepts `nw.<x>.v1` and
`wt.<x>.v1`. It is used by every reader that checks a schema id: ledger and run, gate and
runner records (`agentic/schemas.py`), the spec-reservation store (`spec_numbers._load`),
`.nw/project.yaml` (`agentic/project.validate`) and profile files (`agentic/profiles`). All
other stored JSON (events, mailbox, decisions, escalations, reviews, exports) is read without a
schema check, so legacy records load unchanged. `followups` matches both marker spellings
(`_LINE_RE`, `_SINGLE_RE`, `find_marker`, `open_group_keys`). `journey._COMMIT_PREFIX` strips
both `nw(…)` and `wt(…)` subjects.

### `nw skills install` cleanup (SPEC-0320 follow-on)

- `agent_permissions.sync(path)` merges the missing `ALLOW_RULES`. It removes every entry of
  `LEGACY_RULES` (the exact `Bash(wt <g> *)` and `Bash(WT_MAILBOX_ADDR=* wt <g> *)` strings the
  old install added) and nothing else. It returns `{added, removed}` and writes atomically, only
  when something changed. `install()` still returns the added rules.
- `skills.remove_legacy(dest, source)` removes `dest/wt-<x>` only when `nw-<x>` is a skill this
  repo ships **and** one of these holds:
  - it is a symlink into this repo's `skills/`;
  - it is a symlink whose target is `…/skills/wt-<x>` (the old checkout, usually dangling after
    the move);
  - it is a copy stamped with the old `.wt-skill.json` marker.

  Any other `wt-*` entry is left alone. The CLI reports each removed skill and rule.

### `nw migrate-from-wt [--yes] [--old-checkout P] [--new-checkout P] [--old-config-dir D] [--old-data-dir D] [--json]`

`src/nightwright/migrate.py`. It is a dry run unless `--yes`, and idempotent: a second run
prints `nothing to migrate`.

1. **Move** `$XDG_CONFIG_HOME/wt` (else `~/.config/wt`) to `paths.config_dir()`, and
   `$XDG_DATA_HOME/wt` (else `~/.local/share/wt`) to `paths.data_dir()`. It refuses, changing
   nothing, when a source still exists and its target exists and is not empty. An empty target
   is replaced. `os.rename` is used, with `shutil.move` across filesystems.
2. **Config.** In every `*.yaml`/`*.yml` of the config dir (`*.bak*` backups are skipped), it
   rewrites line by line and keeps comments:
   - the old data dir and config dir paths, in absolute and `~/` spellings;
   - the old checkout (`--old-checkout`, default `/home/bruno/work/wt`) to the new one
     (`--new-checkout`, default `/home/bruno/work/nightwright`), matching only at a path
     boundary, so `/home/bruno/work/wt-rename` is untouched;
   - `/.wt-worktrees` path segments to `/.nw-worktrees`;
   - the key `wt_checkout:` to `nw_checkout:`, and `scheme: wt-native` to `nw-native`.
3. **Campaign state.** In every `<data>/agentic/campaigns/*/state.json`, each string value that
   *is* a path under a moved prefix is rewritten, as is any `.wt-worktrees` segment of a path
   value. Free text that merely mentions a path is kept. This covers `repo.root`,
   `repo.worktree`, `last_gate.results[].cwd` and `idea.research.root`. Branch names
   (`wt/<cid>`), schema ids and ledgers are not touched.
4. **Spec-reservation store.** The `<git common dir>::<spec dir>` keys under the old checkout
   move to the new one, so live reservations keep matching (SPEC-0208).
5. It prints every move and every distinct `- old` / `+ new` change, or a JSON plan
   (`nw.migrate_from_wt.v1`) with `--json`.

It never touches `~/.claude`. That is the job of `nw skills install`.

## Cutover runbook

The paths are this machine's. Run the steps in order. **HUMAN** means the human runs the step.
**STEWARD** means the Meta-Tools steward session runs it, or guides the human through it. Until
step 5, the old `wt` binary is the live one.

1. **STEWARD** — drain the queue and stop the runner. Queue no new work. Park any QUEUED
   campaign that should not be admitted (`wt queue park <cid>`). Wait until nothing is in
   flight, then stop the runner:
   ```sh
   wt queue list                             # repeat until no campaign is in an active state
   wt queue stop --wait-guard                # SIGTERM the lease holder, wait for release
   wt queue list                             # no runner lease
   ```
2. **HUMAN** — exit the conductor session and every steward session (`/exit` in each Claude
   Code session). Make sure no `claude` process is left in `/home/bruno/work/wt` or in a campaign
   worktree.
3. **STEWARD** — land the branch: rebase it once more, then fast-forward main:
   ```sh
   cd /home/bruno/work/wt || exit 1
   git status --short                        # must be clean (untracked plans/ is fine)
   git -C /home/bruno/work/wt-rename rebase main
   git merge --ff-only feat/rename-nightwright
   python3 tools/spec_lint.py --write-ledger && python3 tools/spec_lint.py
   ```
4. **HUMAN** — move the checkout and the worktree root, then repair the worktrees:
   ```sh
   cd /home/bruno/work || exit 1
   git -C /home/bruno/work/wt worktree list  # note every linked worktree
   git -C /home/bruno/work/wt worktree remove /home/bruno/work/wt-rename   # the build worktree
   mv /home/bruno/work/wt /home/bruno/work/nightwright
   mv /home/bruno/work/.wt-worktrees /home/bruno/work/.nw-worktrees
   cd /home/bruno/work/nightwright || exit 1
   git worktree repair $(git worktree list --porcelain | awk '/^worktree /{print $2}' \
       | grep -v '^/home/bruno/work/nightwright$' \
       | sed 's#/home/bruno/work/.wt-worktrees/#/home/bruno/work/.nw-worktrees/#')
   git worktree list                         # every entry resolves; none "prunable"
   git worktree prune --dry-run              # expect only worktrees that are really gone
   ```
   `git worktree repair <paths…>` rewrites both directions: each worktree's `.git` file, which
   points at `nightwright/.git/worktrees/<n>`, and each `gitdir` back-pointer. The comfort-assistant
   worktrees under `.nw-worktrees/comfort-assistant/` belong to *that* repo, so repair them there
   too:
   ```sh
   git -C /home/bruno/work/comfort-assistant worktree repair \
       /home/bruno/work/.nw-worktrees/comfort-assistant/*
   ```
5. **HUMAN** — reinstall the tool:
   ```sh
   uv tool uninstall wt
   uv tool install --editable /home/bruno/work/nightwright
   hash -r; command -v nw && ! command -v wt   # nw present, wt gone
   ```
6. **HUMAN** — migrate config and data (dry run first, then apply):
   ```sh
   nw migrate-from-wt --old-checkout /home/bruno/work/wt --new-checkout /home/bruno/work/nightwright
   nw migrate-from-wt --old-checkout /home/bruno/work/wt --new-checkout /home/bruno/work/nightwright --yes
   nw migrate-from-wt                        # expect: nothing to migrate
   ```
7. **HUMAN** — reinstall skills and allow rules. This also removes the stale `wt-*` links and
   the old `Bash(wt …)` rules:
   ```sh
   nw skills install
   nw skills list                            # all nw-* linked; allow rules present
   ls ~/.claude/skills | grep '^wt-' || echo "no wt-* left"
   rm -f ~/.config/fish/completions/wt.fish && nw completion install --shell fish
   ```
   Also check `~/.claude/settings.json` by hand for hand-added `wt` rules (for example
   `Bash(uv run wt *)`). Install removes only the rules it added itself.
8. **HUMAN** — rename the GitHub repo and point origin at it:
   ```sh
   gh repo rename nightwright -R brunosmmm/wt --yes
   git -C /home/bruno/work/nightwright remote set-url origin git@github.com:brunosmmm/nightwright.git
   git -C /home/bruno/work/nightwright fetch origin && git -C /home/bruno/work/nightwright push origin main
   ```
   Use the https form if origin used https (`git remote -v` before changing it).
9. **STEWARD** (comfort-assistant steward, or the human) — move that repo's project config:
   ```sh
   cd /home/bruno/work/comfort-assistant || exit 1
   git mv .wt .nw
   sed -i 's/^schema: wt\.project\.v1/schema: nw.project.v1/; s/^# wt /# nightwright /; s/: wt opens a PR/: nightwright opens a PR/' .nw/project.yaml
   git commit -m "chore: .wt -> .nw (nightwright rename, SPEC-0330)" && git push
   ```
   The `schema` edit is optional, because `wt.project.v1` is still accepted.
10. **HUMAN** — move Claude Code's per-project memory dir so the next session in the new path
    finds it:
    ```sh
    mv ~/.claude/projects/-home-bruno-work-wt ~/.claude/projects/-home-bruno-work-nightwright
    grep -l 'wt' ~/.claude/projects/-home-bruno-work-nightwright/memory/*.md   # list only; do not edit
    ```
    The memory notes still say `wt`. Read them as `nw` (an AGENTS.md note can say so). They are
    not edited here.
11. **STEWARD** — restart:
    ```sh
    cd /home/bruno/work/nightwright || exit 1
    nw queue run --detach
    nw queue conductor-launch
    nw queue steward-launch Meta-Tools
    nw queue steward-launch comfort-assistant
    ```
12. **STEWARD** — verify:
    ```sh
    nw --help | head -1                       # Usage: nw …
    nw queue                                  # runner alive, old campaigns listed with their paths
    nw campaign show <an old MERGED campaign>  # legacy wt.* ledger reads cleanly
    nw mailbox list --to conductor | head     # legacy messages readable
    nw spec reserve --for cutover-check   # then: nw spec release <that number> --yes
    uv run --directory /home/bruno/work/nightwright pytest -q -n 2 tests/test_rename_nightwright.py
    ```
    Then set this spec to `done` (with an *As shipped* note of anything that deviated), run
    `python3 tools/spec_lint.py --write-ledger`, and commit.

**Rollback (before step 8):** `mv` the two directories back, `git -C /home/bruno/work/wt reset
--hard <pre-merge sha>`, run `git worktree repair` again, then `uv tool uninstall nightwright &&
uv tool install --editable /home/bruno/work/wt`. Move `~/.config/nightwright` and
`~/.local/share/nightwright` back with `mv`; the moved data is wt-readable except for files the
new code wrote in between, which carry `nw.*` schema ids that `wt` rejects in the five checked
readers.

## Alternatives considered

- **A `wt` alias and `WT_*` fallback for a deprecation window** (IDEA-426's first plan). The
  human rejected it: there is a single install and a coordinated cutover, so an alias only
  carries dead names forward.
- **Rewriting stored history to `nw.*`.** This touches append-only ledgers, which other tools may
  hash or diff. Accepting legacy ids on read costs one helper.
- **Renaming existing campaign branches.** The refs, the open PRs and the ledgers name them.
  New campaigns use `nw/`.

## Acceptance criteria

- [x] `pyproject.toml` builds the `nightwright` dist with only the `nw` console script;
      `uv run nw --help` works in the worktree's own venv.
- [x] No module, env var, config/data dir, per-project path, skill, allow rule, completion name or
      CLI text says `wt`, except the justified leftovers in the inventory.
- [x] Legacy `wt.<x>.v1` ids pass every schema-checking reader; new writes use `nw.<x>.v1`.
- [x] `wtfu:`/`wtfug:` markers and `wt(<cid>):` commit subjects are still recognised.
- [x] `nw skills install` removes exactly the old `Bash(wt …)`/`Bash(WT_MAILBOX_ADDR=* wt …)`
      rules and the stale `wt-*` skill links or copies of this repo, and nothing else.
- [x] `nw migrate-from-wt` is a dry run by default, applies with `--yes`, is idempotent, refuses a
      non-empty target, rewrites config, campaign-state paths and reservation keys at path
      boundaries only, keeps branch names, ledgers and free text, and never touches `~/.claude`.
- [x] The full suite passes on the branch (`-n 2`).
- [ ] The cutover runbook has been executed, and *As shipped* records any deviation (this flips
      the spec to `done`).

## Test plan

- **Automated tests:** `tests/test_rename_nightwright.py`:
  - the hard cut: pyproject, no `WT_*` fallback, `nw-` skill names and frontmatter, `.nw/` and
    `.nw-worktrees`;
  - the legacy readers: `legacy.schema_ok`, ledger and run records, project.yaml, the
    reservation store, follow-up markers, the journey commit prefix, the sheet owner;
  - skills and allow-rule cleanup on a tmp dest and settings file (only ours removed;
    idempotent);
  - the migration on a tmp `$HOME`: dry run changes nothing; `--yes` moves and rewrites, keeps
    comments, backups, ledgers, free text, branch names and schema ids, and respects path
    boundaries; a second run is a no-op; a non-empty target is refused; an empty target is
    replaced; `~/.claude` is untouched; the `--json` plan.

  Every existing test is updated for the new names (imports, paths, env vars, `NW_TEST_WAIT_S`).
- **Manual verification:** a dry run, then `--yes`, then a re-run of `nw migrate-from-wt`
  against a full copy of the real `~/.config/wt` and `~/.local/share/wt` in a scratch `$HOME`.
  Results: 154 campaign states and the reservation store were rewritten (552 path changes); the
  re-run printed `nothing to migrate`; the only `.wt-worktrees` and `/work/wt` strings left are
  in free text (gate tails, review details).
- **Regression guard:** the full suite on the branch with `-n 2` under the host flock;
  `python3 tools/spec_lint.py` exits 0.

## Rollout / migration

See *Cutover runbook*. The branch is not merged before step 3 of the runbook.

## Definition of done

- [ ] Acceptance criteria all met (the last one at cutover).
- [x] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [x] No regressions.
- [ ] Spec body updated to match what shipped (after the cutover).
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

None. The cutover timing is the human's call.
