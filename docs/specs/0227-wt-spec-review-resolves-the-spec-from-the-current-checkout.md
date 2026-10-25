---
id: SPEC-0227
title: "wt spec review resolves the spec from the current checkout"
status: done
owner: user
created: 2026-10-25
updated: 2026-10-25
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0169
tags: [agentic, specs, review, worktree, cli]
source_idea: IDEA-095
depends_on: [SPEC-0210]
---

## Context

Child of epic [SPEC-0169](./0169-mini-agentic-workflow-queue-campaigns-headless.md),
promoted from bug `IDEA-095` ("wt spec review ignores the current worktree").

`wt spec review SPEC-ID` finds the file with `specs.resolve_spec_path(cfg, spec_id)`, which
for an internal id looks in `specs._specs_dir(cfg)`. That is `cfg["specs_dir"]`, or the
registered wt checkout's `docs/specs`, located from the package and never from the cwd
(SPEC-0079, on purpose). On 2026-10-25 the command was run in the worktree
`/home/bruno/work/wt-close-spec`, on branch `close-spec`, where the revised SPEC-0220 had
sha `3bccae51`. It reviewed the stale copy in the main checkout, sha `17050d6b`, wasted
about $0.24, and returned findings that had already been fixed. Spec authors have to review
a revision before it reaches `main`. Otherwise every revision round moves `main`, which
caused a `base_moving` park the same day.

[SPEC-0210](./0210-spec-review-store-in-the-data-dir-any-repo-reviews.md) already moves
reviews into the data-dir store keyed by spec id and sha, and adds `--path FILE`. It also
roots the reviewer at the spec file's git toplevel (`spec_root`). So a worktree review no
longer has to write into that worktree, and `wt queue add` accepts it once the identical
text reaches main. What is still missing is which file a **SPEC-ID** resolves to, and a
clear statement of which file was reviewed.

## Goals / Non-goals

**Goals**
- `wt spec review SPEC-ID` run inside a checkout of the project's repository, main or any
  linked worktree, reviews that checkout's copy of the spec.
- Run anywhere else, it reviews the registered checkout's copy, as today.
- It always prints the reviewed path and sha before the run starts.

**Non-goals**
- Changing how other commands resolve specs (`wt spec status`, `verify`, `queue add`, …).
  SPEC-0079's package-anchored rule stays for them.
- Outbound ids (`<PROJ>-NNNN`). They live in the data-dir outbox, which has no checkout.
- Anything SPEC-0210 owns: the store, the journal, `--path` semantics, `spec_root`.

## Decision

Add `specs.resolve_spec_path_for_cwd(cfg, spec_id, cwd=None) -> (path, origin)`, used only
by `wt spec review`. For an internal id it takes the registered specs dir (`_specs_dir`) and
its git repository R. If the cwd is inside a git checkout whose
`git rev-parse --git-common-dir` (resolved to an absolute path) equals R's, the checkout is
the same repository. The spec is then looked up at `<cwd toplevel>/<specs dir relative to
R's toplevel>`. Otherwise it resolves exactly as today. When the cwd checkout is the same
repository but has no file for the id, the command fails. It does not fall back: a silent
fallback to another copy is the bug this spec fixes. The CLI prints `reviewing <path> @
<sha12> (<origin>)` first, where origin is `worktree <branch>`, `main checkout` or
`registered checkout`.

## Design

- **`resolve_spec_path_for_cwd`** (`src/wt/specs.py`):
  1. `_is_outbound_spec_id(spec_id)` → `(resolve_spec_path(cfg, spec_id), "outbox")`.
  2. `reg_dir = _specs_dir(cfg)`, `reg_top = git -C reg_dir rev-parse --show-toplevel`, and
     `reg_common = git -C reg_dir rev-parse --path-format=absolute --git-common-dir`. If
     `reg_dir` is not in a git repository, go to step 5.
  3. `cur_top` / `cur_common`: the same two commands with `-C cwd`. If either fails, go to
     step 5.
  4. `cur_common == reg_common` (compared as resolved `Path`s) → same repository.
     - `cur_top == reg_top` → origin `main checkout`. That is the label for the registered
       checkout itself, which is the main checkout in practice.
     - otherwise origin `worktree <branch>`, from `git -C cwd rev-parse --abbrev-ref HEAD`
       (`detached` when it prints `HEAD`).
     Then `return _find_spec(cur_top / reg_dir.relative_to(reg_top), spec_id), origin`. A
     `ValueError` from `_find_spec` is re-raised as: `no spec file for SPEC-X in this
     checkout (<cur_top>, <origin>); the registered checkout has <path> — run from there, or
     use wt spec review --path FILE`. The second clause appears only when the registered copy
     exists.
  5. Fallback → `(resolve_spec_path(cfg, spec_id), "registered checkout")`, as today.
  The git calls use `--path-format=absolute` (git ≥ 2.31) and fall back to resolving a
  relative `--git-common-dir` against `cwd` when the flag is not supported. Each call has a
  timeout of 5 s, like `project._git_toplevel`.
  - Choice: compare `--git-common-dir`, not the toplevel and not the remote URL. Linked
    worktrees share the main repository's common dir, so this identifies exactly "a checkout
    of this repo". Two separate clones of the same remote are different repositories with
    different branches and should not be mixed. Such a clone gets the fallback, and `--path`
    covers it.
  - Choice: with `cfg["specs_dir"]` set (tests, custom layouts), the same rule applies to
    the git repository that contains it. Nothing special-cases the wt repository.
- **CLI** (`spec_review_cmd` in `src/wt/cli.py`, after SPEC-0210's `[SPEC-ID] [--path
  FILE]` shape):
  - SPEC-ID → `path, origin = resolve_spec_path_for_cwd(cfg, spec_id)`, then
    `run_spec_review(cfg, path, spec_id)`. The reviewer root is SPEC-0210's
    `spec_root(path)`, which is the worktree for a worktree path. `review_spec` stays
    unchanged for library callers.
  - `--path FILE` → origin `path`. Behaviour otherwise as SPEC-0210 specifies.
  - Before the run, one line: `reviewing docs/specs/0220-….md @ 3bccae51c0de (worktree
    close-spec)  /home/bruno/work/wt-close-spec`, with the path relative to the checkout
    toplevel and the absolute toplevel after it. It goes to stdout normally and to stderr
    with `--json`, so the JSON stays parseable.
  - The `wt.spec.review.v1` payload printed with `--json`, and the stored record, gain
    `spec_path` (absolute) and `origin`. Both are additive. SPEC-0210 already adds
    `repo_root`.
- **Admission interplay (no code change):** SPEC-0210's `check_admissible` looks up the
  store by the sha of the registered checkout's file. A review made in a worktree counts once
  the same text is on main (same sha), and an older review never counts for a newer text.

## Alternatives considered

- **Make `resolve_spec_path` itself cwd-aware.** That would undo SPEC-0079's protection
  against tests resolving the live repository from the cwd, for every command. Rejected.
  Only review needs it.
- **Fall back to the registered checkout when the worktree lacks the file.** That
  reproduces the IDEA-095 failure (reviewing a different text without noticing). Rejected
  in favour of an error that names both places.
- **Match by remote URL.** It would mix separate clones with diverging branches, and it
  fails for repositories without a remote. Rejected.
- **Require `--path` inside worktrees.** It works today with SPEC-0210, but it is the
  ergonomic trap that caused the incident. Rejected as the only answer, kept as the
  explicit option.

## Acceptance criteria

- [x] Run inside a linked worktree of the registered repository (`git worktree add`),
      `wt spec review SPEC-X` reviews `<worktree>/docs/specs/NNNN-*.md`. The store record is
      keyed by that file's sha. The reviewer's `cwd` is the worktree root. The record has
      `origin: "worktree <branch>"` and the worktree `spec_path`.
- [x] Run in the main checkout, it reviews the main checkout's file (`origin: main
      checkout`). Run outside any git repository, or inside a different repository
      (different `--git-common-dir`, including a separate clone of the same remote), it
      reviews the registered checkout's file (`origin: registered checkout`), as today.
- [x] Inside a same-repository checkout that has no file for the id, the command exits
      non-zero with a message naming that checkout, the registered copy's path when it
      exists, and `--path`, and no review runs (FakeRunner cursor unchanged).
- [x] Before the run it prints `reviewing <rel path> @ <sha12> (<origin>)  <toplevel>`, to
      stdout, or to stderr with `--json`, and `--json` output stays valid JSON with
      `spec_path` and `origin`. `--path FILE` prints the same line with origin `path`.
- [x] Outbound ids resolve from the outbox as before. With `cfg["specs_dir"]` pointing into a
      tmp git repository, the worktree rule applies relative to that repository.
- [x] Other spec commands still resolve through `resolve_spec_path` (a test runs `wt spec
      status SPEC-X` inside a worktree with a different text and gets the registered
      file).
- [x] `uv run pytest` passes; `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests:** new `tests/test_spec_review_cwd.py`. It uses CliRunner with a
  FakeRunner `spec_reviewer` script, a tmp git repository as `cfg["specs_dir"]`'s repository
  (with `docs/specs/SCHEMA.md` and `0001-x.md`), and `monkeypatch.chdir` for the cwd.
  - `test_worktree_copy_is_reviewed`: `git worktree add ../wt2 -b rev`, then edit the spec
    in `wt2`. From `wt2`, the store key is the `wt2` file's sha, `origin == "worktree rev"`,
    and the FakeRunner request's `cwd` is `wt2`.
  - `test_main_checkout_copy`: from the main checkout, the main sha and `origin == "main
    checkout"`.
  - `test_outside_repo_and_other_repo_fall_back`: from `tmp_path / "elsewhere"` (no git)
    and from a separate `git clone` of the repository, the registered file is reviewed.
  - `test_missing_in_worktree_errors_no_fallback`: the spec is deleted in the worktree.
    Exit code ≠ 0, the message contains the worktree path, the registered path and
    `--path`, and the FakeRunner cursor is untouched.
  - `test_reviewed_line_stdout_and_json_stderr`: plain mode prints the line to stdout.
    `--json` gives parseable stdout with `spec_path` / `origin`, and the line goes to
    stderr.
  - `test_path_option_prints_origin_path`.
  - `test_outbound_id_unaffected`.
  - `test_other_spec_commands_unchanged`: `wt spec status SPEC-0001` in the worktree reads
    the registered file.
  - `test_detached_head_origin`: a worktree at a detached HEAD gives `worktree detached`.
- **Existing suites unchanged:** `tests/test_agentic_spec_review.py`,
  `tests/test_spec_review_store.py` (SPEC-0210), `tests/test_specs*.py`.
- **Regression guard:** full `uv run pytest`.
- **Manual verification:** in a spec-writing worktree of wt, `wt spec review SPEC-NNNN`
  prints `(worktree <branch>)` with that worktree's sha. `wt queue add SPEC-NNNN` after the
  merge (same text) is admitted without a new review.

### Test record (2026-10-25)

- New `tests/test_spec_review_cwd.py` (10 tests, all of the Test plan's names plus
  `test_specs_dir_in_tmp_repo_rule_is_relative_to_that_repo`): pass.
- Existing `tests/test_agentic_spec_review.py` and `tests/test_spec_review_store.py` pass; four
  `json.loads(r.output)` assertions on `--json` runs became `json.loads(r.stdout)`, because the
  reviewed-path line now goes to stderr and CliRunner's `output` interleaves both streams.
- Full `uv run pytest`: green after regenerating the ledger; `python3 tools/spec_lint.py` exits 0.
- Manual: `resolve_spec_path_for_cwd` run from this campaign's worktree against the real
  registered checkout returned the worktree copy with origin `worktree wt/SPEC-0227-…`. The
  post-merge `wt queue add` step was not run live (no merge yet); it relies on SPEC-0210's
  sha-keyed `check_admissible`, covered by `tests/test_spec_review_store.py`.

### As shipped

As designed. Small additions: `reviewing_line()` in `agentic/spec_review.py` builds the printed
line; `run_spec_review` and `review_payload` take optional `origin` / `spec_path` (omitted for
library callers such as `review_spec`, so their records do not carry them). Git probes live in
`specs._git_out` / `specs._git_checkout`.

## Rollout / migration

Lands after SPEC-0210, which provides the store, `--path` and `spec_root`. No data
migration. Reviews already stored keep their keys, and only the new records carry `origin` /
`spec_path`.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

_None — decisions locked above._
