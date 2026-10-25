---
id: SPEC-0166
title: "One wt-checkout resolver; editable install as the documented path"
status: done
owner: user
created: 2026-10-25
updated: 2026-10-25
milestone: "M1: packaging"
kind: feature
tags: [packaging, install, skills, bug]
source_idea: IDEA-001
---

## Context

Promoted from idea `IDEA-001`.

Three call sites find wt's own checkout by walking up from the installed module file. That
only works when the package runs **from** the checkout (`uv run wt …` or
`uv tool install --editable .`). After a plain `uv tool install .`, which is what README,
getting-started, `tui.md` and `completion.md` document, the module lives in the uv tool venv's
site-packages:

| Call site | Spec | After a non-editable install |
|---|---|---|
| `src/wt/skills.py:21` `_REPO_SKILLS_DIR` | SPEC-0017 | `wt skills install` prints "no skills found under skills/", installs nothing and exits 0. `wt skills list` prints nothing. `skills/` is not in the wheel. |
| `src/wt/rules.py:172` `discover_wt_checkout()` | SPEC-0042 | Meta-Tools research root is null ("could not discover wt checkout"), so explore on Meta-Tools ideas has no tree. |
| `src/wt/specs.py:47` `_package_specs_dir()` | SPEC-0079 | Without `specs_dir` in config, it already raises a clear "set `specs_dir`" error. This one is safe, but it is a third copy of the same anchoring. |

Reproduced 2026-10-25 (Fedora 43, uv 0.12.19). `uv tool install --reinstall --editable '.[tui]'`
fixes the first two. Only `docs/WORKFLOW.md:140` mentions `--editable`, as an aside.

## Goals / Non-goals

**Goals**
- One resolver for "where is wt's checkout", used by all three call sites.
- An explicit override for installs that are not editable: config key `wt_checkout`.
- When the checkout cannot be found, every caller fails **loudly with a fix hint**:
  reinstall with `--editable`, or set `wt_checkout`. No silent empty result.
- Docs make `uv tool install --editable .` (or `'.[tui]'`) the documented install, and say why.

**Non-goals**
- Shipping `skills/` or `docs/specs/` as package data. The specs are a live working tree, and
  `--link` skills symlink into the repo on purpose (SPEC-0017).
- A cwd or git-toplevel fallback. SPEC-0079 removed cwd-relative resolution so tests can never
  write to the live repo, and this spec keeps it that way.
- Changing `scripts/install-bundled-config.sh` (SPEC-0136). It already sets `specs_dir` and
  tells operators to use `uv run wt` from the extracted tree, which resolves from the checkout.
- Meta-Tools routing in `wt spec new` and the capture-time "unknown project" warning. They
  are related but separate (tracked in the ideas file).

## Decision

Add `wt_checkout(cfg) -> Path | None` in `src/wt/rules.py`, next to the existing
`discover_wt_checkout`, with this precedence: the `cfg["wt_checkout"]` config override, then
the module-relative walk, then `None`. `discover_wt_checkout()` stays as the module-walk step.
Skills, Meta-Tools research and the internal specs default all resolve through it, and each
reports a shared hint string when it gets `None`. Docs switch to the editable install.

## Design

### Resolver (`src/wt/rules.py`)

```python
CHECKOUT_HINT = ("reinstall from your wt checkout with `uv tool install --editable .` "
                 "or set `wt_checkout: /path/to/wt` in config.yaml")

def wt_checkout(cfg=None):
    """wt's own checkout root (SPEC-0166): config `wt_checkout` (validated: has src/wt +
    pyproject.toml) > module-relative walk (`discover_wt_checkout`) > None."""
```

- A configured `wt_checkout` path is expanded with `expanduser`. If it does not look like a wt
  checkout (`src/wt` dir and `pyproject.toml`), the resolver raises
  `ValueError("wt_checkout … is not a wt checkout")`. A typo should not silently fall back.
- `DEFAULT_CONFIG["wt_checkout"] = None`, with a comment.
- *As shipped:* the override branch is its own helper, `configured_wt_checkout(cfg)`. It
  returns a Path or None and raises on an invalid value. `wt_checkout` calls it, and so does
  `_specs_dir`, which needs only the config branch before its own module-walk step.
  `_package_specs_dir()` now uses `discover_wt_checkout()` instead of its own `parents[2]`.

### Skills (`src/wt/skills.py`, `src/wt/cli.py`)

- `repo_skills_dir(cfg=None)` returns `wt_checkout(cfg) / "skills"` or `None`. The
  `_REPO_SKILLS_DIR` module constant is removed. `repo_skills`, `install` and `status` take an
  optional `source` dir; when it is omitted they resolve it with `repo_skills_dir()`.
- CLI `wt skills install|list`: load the config, resolve the source, and raise
  `click.ClickException(f"cannot find wt's skills/ directory; {CHECKOUT_HINT}")` when it is
  `None`. A checkout that resolves but has an empty `skills/` keeps the current yellow notice.

### Meta-Tools research (`resolve_research_context`)

Uses `wt_checkout(cfg)`. The `None` note becomes
`f"could not discover wt checkout for internal Meta-Tools; {CHECKOUT_HINT}"`.

### Internal specs (`src/wt/specs.py::_specs_dir`)

Precedence: `cfg["specs_dir"]`, then `cfg["wt_checkout"]/docs/specs` (via the resolver's
config branch), then `_package_specs_dir()` (the module walk, unchanged so the SPEC-0079 test
monkeypatch still works), then the existing `ValueError`. The error text gains the hint.

### Docs

- README install, `docs/product/guides/getting-started.md`, `docs/product/capabilities/tui.md`,
  `docs/product/capabilities/completion.md` and `docs/WORKFLOW.md` use
  `uv tool install --editable .` / `--editable '.[tui]'`. They add one line explaining why:
  skills, Meta-Tools research and internal specs live in the checkout, and a `git pull` takes
  effect without reinstalling. They also mention the `wt_checkout` override.

## Alternatives considered

- **Package data for skills/specs** — rejected (Non-goals). It would drift from the repo, and
  `--link` mode needs a real repo path.
- **cwd / git-toplevel fallback** — rejected. It reintroduces the SPEC-0079 hazard of
  resolving against whatever repo the process happens to run in.
- **`--source` flag on `wt skills install`** — rejected for now. The config key covers every
  caller at once, and a flag would only fix one.
- **Docs-only change** — rejected. Silent "no skills found" and exit 0 would still mislead
  anyone who misses the docs.

## Acceptance criteria

- [x] `wt_checkout(cfg)` returns the config override when it is valid, raises on an invalid
      override, falls back to the module walk, and returns `None` when neither finds a checkout.
- [x] Without a resolvable checkout, `wt skills install` and `wt skills list` exit non-zero with
      a message containing `--editable` and `wt_checkout`.
- [x] With `wt_checkout` set, a **non-editable** install (wheel in a scratch venv) installs
      all skills and reports Meta-Tools `research.root` as the checkout.
- [x] Meta-Tools `research.note` carries the hint when unresolvable.
- [x] `_specs_dir` uses `wt_checkout/docs/specs` when `specs_dir` is unset; the existing
      SPEC-0079 tests still pass, and the error text carries the hint.
- [x] No module-relative `parents[2]` anchoring remains outside the resolver.
- [x] README, getting-started, tui, completion and WORKFLOW document the editable install and
      `wt_checkout`.
- [x] Full `uv run pytest` green; `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests:**
  - `tests/test_wt_checkout.py` (new): override valid/invalid, module-walk fallback
    (monkeypatch `discover_wt_checkout`), `None` path; `resolve_research_context` note hint;
    `_specs_dir` precedence with `wt_checkout`.
  - `tests/test_skills.py`: `install(source=tmp)` / `status(source=tmp)`. CLI
    `skills install --dest tmp` with the resolver patched to `None` exits non-zero with the hint,
    and the same for `skills list`. Existing tests stay on a tmp `--dest`.
  - Existing `tests/test_specs_dir.py` passes unchanged.
- **Manual verification:** build a non-editable install into a scratch venv
  (`uv venv` + `uv pip install .`) and point `WT_CONFIG_DIR` at a scratch config. Then:
  1. `wt skills install --dest <tmp>` fails with the hint.
  2. Add `wt_checkout: <repo>` to the scratch config.
  3. `wt skills install --dest <tmp>` installs all skills, and `wt projects --json` shows
     Meta-Tools `root` = repo.

  The user's real editable install and real `~/.claude/skills` are not touched.
- **Regression guard:** full `uv run pytest`.

### Test record (2026-10-25)

- **Automated:** `tests/test_wt_checkout.py` (new) has 17 tests: resolver precedence,
  `~` expansion, invalid override raising, `None`, no `parents[2]` left in
  skills/specs/rules, the Meta-Tools override and hint, `_specs_dir` precedence and hint,
  `install`/`status` with an explicit source, CLI `skills install|list` failing loudly
  (exit ≠ 0, hint, nothing installed), CLI install via the override, and an invalid override
  error. `tests/test_specs_dir.py`, `tests/test_skills.py` and `tests/test_research_context.py`
  pass unchanged. Full `uv run pytest`: **1158 passed, 82 skipped, 0 failed**.
- **Manual (non-editable):** `uv venv` + `uv pip install .` into a scratch venv (package in
  `site-packages`), with `WT_CONFIG_DIR`/`WT_DATA_DIR` pointed at scratch dirs.
  - Without the override: `wt skills install --dest <tmp>` fails with "cannot find wt's
    skills/ directory; reinstall … `--editable` … or set `wt_checkout`", exit 1. Meta-Tools
    research is `root: None` with the hint in its note.
  - With `wt_checkout: /home/bruno/work/wt`: all 12 skills linked, and Meta-Tools `root` is
    the checkout.
  - The user's real editable install still lists linked skills and resolves Meta-Tools from
    `~`.

## Clock Log

Internal work: clock on IDEA-001 with `wt idea clock-in` / `clock-out`.

## Rollout / migration

Code and docs land together. Existing non-editable installs see the new error with the fix
hint on the next `wt skills install`. No data migration.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

_None — decisions locked above._
