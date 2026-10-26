"""The spec writer's deterministic half (SPEC-0215): what wt checks, regenerates, rolls back
and commits around a `spec_writer` run. The persona edits one file in the campaign worktree;
everything here is plain git and file inspection, so a bad write never reaches the branch.

`queue.py` wires these into the SPEC_WRITING step: `_prepare_run` (main thread) calls
`snapshot` / `epic_context` / `write_rules`; the worker thread calls `validate_spec_change`,
`regenerate`, `commit_staged` or `rollback`."""
from __future__ import annotations

import datetime as dt
import json
import posixpath
import re
from pathlib import Path

from ..specmeta import first_placeholder, sections, split_frontmatter
from . import gitops as git

# Headings a written spec must carry (the linter's own list is longer; the rest is caught by
# the project's gates at GATING, not by a writer retry).
REQUIRED_HEADINGS = ("Context", "Goals / Non-goals", "Decision", "Design", "Acceptance criteria",
                     "Test plan")
_OPEN_BOX_RE = re.compile(r"^\s*- \[ \]", re.M)


def spec_id_of(number) -> str:
    return f"SPEC-{int(number):04d}"


# ---- worktree inspection -------------------------------------------------------------------

def untracked(worktree) -> list:
    """Untracked, non-ignored files (every file, never a bare directory), sorted."""
    out = git.git(worktree, "ls-files", "--others", "--exclude-standard", "-z").stdout
    return sorted(p for p in out.split("\0") if p)


def tracked_dirty(worktree) -> bool:
    """Staged or unstaged changes to tracked files."""
    return bool(git.git(worktree, "status", "--porcelain", "--untracked-files=no").stdout.strip())


def changed_paths(worktree, head_before, untracked_before) -> dict:
    """`{path: A|M|D}` of everything the run changed since `head_before`: the tracked diff
    (committed or not, renames split) plus the untracked files that were not there before."""
    raw = git.git(worktree, "-c", "core.quotepath=off", "diff", "--no-renames", "--name-status",
                  "-z", head_before).stdout.split("\0")
    out = {}
    i = 0
    while i + 1 < len(raw) and raw[i]:
        out[raw[i + 1]] = raw[i][0] if raw[i][0] in "AMD" else "M"
        i += 2
    # what the writer committed: a file committed and then deleted (or reverted) in the working
    # tree is absent from the diff above but still sits in HEAD
    raw = git.git(worktree, "-c", "core.quotepath=off", "diff", "--no-renames", "--name-status",
                  "-z", head_before, "HEAD").stdout.split("\0")
    i = 0
    while i + 1 < len(raw) and raw[i]:
        out.setdefault(raw[i + 1], "M" if (Path(worktree) / raw[i + 1]).exists() else "D")
        i += 2
    before = set(untracked_before or ())
    for p in untracked(worktree):
        if p not in before:
            out.setdefault(p, "A")
    return out


def snapshot(worktree, *, mode, number, path) -> dict:
    """The `state.spec_write` record taken before the writer runs. `untracked_before` is
    filled by the caller (first attempt only)."""
    return {"mode": mode, "number": int(number), "path": path,
            "head_before": git.rev_parse(worktree), "untracked_before": untracked(worktree)}


# ---- validation ----------------------------------------------------------------------------

def _normalize(o):
    """YAML parses unquoted dates to date objects; JSON Schema wants ISO strings (the linter's
    `_normalize`)."""
    if isinstance(o, dict):
        return {k: _normalize(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_normalize(v) for v in o]
    if isinstance(o, (dt.date, dt.datetime)):
        return o.isoformat()
    return o


def _schema_problems(worktree, fm) -> list:
    """Front matter against the repo's `docs/specs/spec.schema.json` (every violation). jsonschema
    is a dev-only dependency: without it, or without the schema file, only the explicit checks
    in `_front_matter_problems` apply."""
    schema_file = Path(worktree) / "docs" / "specs" / "spec.schema.json"
    try:
        import jsonschema
        schema = json.loads(schema_file.read_text(encoding="utf-8"))
    except (ImportError, OSError, ValueError):
        return []
    cls = jsonschema.validators.validator_for(schema)
    errors = sorted(cls(schema).iter_errors(_normalize(fm)), key=lambda e: list(e.absolute_path))
    return [f"front matter: {'.'.join(str(x) for x in e.absolute_path) or '(root)'}: {e.message}"
            for e in errors]


def _front_matter_problems(fm, spec_id, idea) -> list:
    out = []
    if fm.get("id") != spec_id:
        out.append(f"front matter: id must be {spec_id}, got {fm.get('id')!r}")
    if fm.get("status") != "proposed":
        out.append(f"front matter: status must be proposed, got {fm.get('status')!r}")
    if fm.get("source_idea") != (idea or {}).get("id"):
        out.append(f"front matter: source_idea must be {(idea or {}).get('id')!r}, "
                   f"got {fm.get('source_idea')!r}")
    if fm.get("owner") != "user":
        out.append(f"front matter: owner must be user, got {fm.get('owner')!r}")
    if fm.get("kind", "feature") != "feature":
        out.append(f"front matter: kind must be absent or feature, got {fm.get('kind')!r}")
    epic = ((idea or {}).get("epic") or "").strip() or None
    if epic and fm.get("parent") != epic:
        out.append(f"front matter: parent must be {epic} (the idea's :EPIC:), "
                   f"got {fm.get('parent')!r}")
    elif not epic and "parent" in fm:
        out.append(f"front matter: parent must be absent (the idea has no :EPIC:), "
                   f"got {fm.get('parent')!r}")
    return out


def _content_problems(worktree, text, spec_id, idea) -> list:
    try:
        fm, body = split_frontmatter(text)
    except Exception as e:                      # yaml.YAMLError and friends
        return [f"front matter: not valid YAML ({e})"]
    if not isinstance(fm, dict):
        return ["front matter: missing (a spec starts with a --- block)"]
    out = _schema_problems(worktree, fm) + _front_matter_problems(fm, spec_id, idea)
    secs = sections(body)
    for h in REQUIRED_HEADINGS:
        if h not in secs:
            out.append(f"body: missing section '## {h}'")
    if "Acceptance criteria" in secs and not _OPEN_BOX_RE.search(secs["Acceptance criteria"]):
        out.append("body: '## Acceptance criteria' has no '- [ ]' item")
    hit = first_placeholder(text)
    if hit:
        out.append(f"body: line {hit[0]} is a template placeholder: {hit[1]}")
    return out


def validate_spec_change(worktree, spec_write, idea) -> list:
    """Problems with what the writer left in `worktree` (empty = valid). Runs before any
    regeneration, so any changed path other than the spec file is a problem, generated files
    included. Edits to pre-existing untracked or ignored files are not detected."""
    worktree = Path(worktree)
    path, mode = spec_write["path"], spec_write["mode"]
    spec_id = spec_id_of(spec_write["number"])
    out = []
    if git.git(worktree, "merge-base", "--is-ancestor", spec_write["head_before"], "HEAD",
               check=False).returncode != 0:
        out.append("HEAD no longer descends from the commit the run started at "
                   "(the writer rewrote history)")
    changed = changed_paths(worktree, spec_write["head_before"], spec_write.get("untracked_before"))
    for p, st in sorted(changed.items()):
        if p == path:
            if st == "D":
                out.append(f"{path} was deleted")
        elif st == "D":
            out.append(f"{p} was deleted (only {path} may change)")
        else:
            out.append(f"{p} was changed (only {path} may change)")
    if path not in changed:
        out.append(f"no change made to {path}" if mode == "revise"
                   else f"{path} was not created")
    f = worktree / path
    if changed.get(path) != "D" and path in changed and f.is_file():
        out += _content_problems(worktree, f.read_text(encoding="utf-8"), spec_id, idea)
    return out


def verdict_problems(rr, spec_write) -> list:
    """Run-level problems: an error, a timeout, an unparseable verdict, or a `spec_path` that
    isn't the spec file."""
    if rr.timed_out:
        return ["the spec writer run timed out"]
    if rr.is_error or rr.exit_code != 0:
        return [f"the spec writer run failed (exit {rr.exit_code})"]
    if not rr.verdict_ok or not isinstance(rr.verdict, dict):
        return ["the spec writer returned no parseable verdict"]
    got = str(rr.verdict.get("spec_path") or "").strip()
    norm = posixpath.normpath(got) if got else ""
    if norm != spec_write["path"]:
        return [f"verdict spec_path {got!r} is not {spec_write['path']}"]
    return []


# ---- generated files, rollback, commit -----------------------------------------------------

def regenerate(worktree, entries, spec_write) -> tuple:
    """Run every project `merge.regenerate` command in order (`sh -c`, SPEC-0191's timeout).
    Returns `(problems, generated)`: a failing command or a changed path that is neither the
    spec file nor a declared `path` is a problem; `generated` = the declared paths that
    changed."""
    problems = []
    for e in entries or []:
        try:
            r = git._run(["sh", "-c", e["cmd"]], cwd=str(worktree), check=False,
                         timeout=git._DEFAULT_TIMEOUT)
        except git.GitError as err:
            problems.append(f"regenerate {e['path']}: {err.tail}")
            continue
        if r.returncode != 0:
            tail = git._tail((r.stdout or "") + (r.stderr or ""))
            problems.append(f"regenerate {e['path']} exited {r.returncode}: {tail}")
    declared = {e["path"] for e in entries or []}
    changed = changed_paths(worktree, spec_write["head_before"], spec_write.get("untracked_before"))
    for p in sorted(changed):
        if p != spec_write["path"] and p not in declared:
            problems.append(f"{p} was changed by regeneration but is not a declared generated path")
    return problems, sorted(p for p in changed if p in declared and p != spec_write["path"])


def rollback(worktree, spec_write) -> None:
    """Back to `head_before`; untracked files the run created are removed (recomputed now, so
    those a failing regenerate command made are included), worktree setup output stays."""
    created = [p for p in untracked(worktree)
               if p not in set(spec_write.get("untracked_before") or ())]
    git.git(worktree, "reset", "--hard", spec_write["head_before"])
    if created:
        git.git(worktree, "clean", "-f", "-d", "--", *created)


def commit_staged(worktree, paths, message) -> str | None:
    """Stage exactly `paths` and commit; None when nothing changed against HEAD (the writer
    committed the file itself and regeneration changed nothing)."""
    git.git(worktree, "add", "--", *paths)
    if git.git(worktree, "diff", "--cached", "--quiet", check=False).returncode == 0:
        return None
    git.git(worktree, "commit", "-q", "-m", message)
    return git.rev_parse(worktree)


# ---- prompt pieces -------------------------------------------------------------------------

def epic_context(worktree, epic) -> str:
    """The parent epic's text and its existing children's titles, from the worktree's
    `docs/specs/`. ValueError when no spec with that id and `kind: epic` exists."""
    epic_text, children = None, []
    for f in sorted((Path(worktree) / "docs" / "specs").glob("[0-9]*.md")):
        try:
            text = f.read_text(encoding="utf-8")
            fm, _ = split_frontmatter(text)
        except Exception:
            continue
        if not isinstance(fm, dict):
            continue
        if fm.get("id") == epic and fm.get("kind") == "epic":
            epic_text = (fm.get("title"), text)
        elif fm.get("parent") == epic:
            children.append(f"- {fm.get('id')}: {fm.get('title')}")
    if epic_text is None:
        raise ValueError(f":EPIC: {epic} is not an epic spec")
    return (f"## Parent epic {epic}: {epic_text[0]}\n\n"
            f"This idea is a child of the epic {epic}: the spec's front matter must carry "
            f"`parent: {epic}`.\n\nSpecs already under this epic:\n"
            + ("\n".join(children) if children else "(none yet)")
            + f"\n\nThe epic's text:\n\n{epic_text[1]}")


def write_problems_text(brief) -> str:
    problems = (brief or {}).get("write_problems") or []
    if not problems:
        return "No earlier attempt of yours was rejected."
    return ("Your last attempt was rejected. Fix every problem:\n"
            + "\n".join(f"- {p}" for p in problems))


def write_rules(mode, spec_write, idea, brief) -> str:
    path = spec_write["path"]
    epic = ((idea or {}).get("epic") or "").strip()
    if mode == "write":
        fm = [f"`id: {spec_id_of(spec_write['number'])}`", "`status: proposed`",
              f"`source_idea: {(idea or {}).get('id')}`", "`owner: user`", "`kind: feature`",
              "`title`", "`created` (today, YYYY-MM-DD)"]
        if epic:
            fm.append(f"`parent: {epic}`")
        return (f"Write mode: create exactly `{path}`, following the template. Its front matter "
                f"carries {', '.join(fm)} and no keys the schema does not know. Required "
                f"sections: {', '.join(REQUIRED_HEADINGS)} (and Definition of done).")
    rules = (f"Revise mode: edit only `{path}`; keep its id, `source_idea` and "
             "`status: proposed`; address every must_fix and major finding in the brief.")
    if (brief or {}).get("suggested_splits"):
        rules += (" The review suggests splitting: narrow the spec to the first suggested split "
                  "(or the one a human note names) and list the rest under Non-goals.")
    return rules
