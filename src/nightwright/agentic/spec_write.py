"""The spec writer's deterministic half (SPEC-0215): what nightwright checks, regenerates, rolls back
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
    filled by the caller (first attempt only). `split` (SPEC-0290) is validated like `write`
    against `expected_fm`, which the caller adds. `amend` (SPEC-0279) also keeps `fm_before`, the
    spec's front matter at `HEAD`, which the amend may only touch in `updated`."""
    sw = {"mode": mode, "number": int(number), "path": path, "must_depend_on": [],
          "head_before": git.rev_parse(worktree), "untracked_before": untracked(worktree)}
    if mode == "amend":
        text = git.git(worktree, "show", f"HEAD:{path}").stdout
        fm, _ = split_frontmatter(text)
        sw["fm_before"] = _normalize(fm or {})
    return sw


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


def _must_depend(entries) -> list:
    """SPEC-0423: `must_depend_on` as `[(spec_id, prerequisite)]`; an entry is a spec id or a
    `{"spec_id", "ref"}` dict (the prerequisite it came from)."""
    out = []
    for e in entries or ():
        if isinstance(e, dict):
            out.append((str(e.get("spec_id")), str(e.get("ref") or e.get("spec_id"))))
        else:
            out.append((str(e), str(e)))
    return out


def _front_matter_problems(fm, spec_id, idea, must_depend_on=()) -> list:
    out = []
    if fm.get("id") != spec_id:
        out.append(f"front matter: id must be {spec_id}, got {fm.get('id')!r}")
    if fm.get("status") != "proposed":
        out.append(f"front matter: status must be proposed, got {fm.get('status')!r}")
    if fm.get("source_idea") != (idea or {}).get("id"):
        out.append(f"front matter: source_idea must be {(idea or {}).get('id')!r}, "
                   f"got {fm.get('source_idea')!r}")
    req = (idea or {}).get("source_request")
    if req:
        if fm.get("source_request") != req:
            out.append(f"front matter: source_request must be {req!r}, "
                       f"got {fm.get('source_request')!r}")
    elif "source_request" in fm:
        out.append("front matter: source_request must be absent (the idea has no :REQ:), "
                   f"got {fm.get('source_request')!r}")
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
    deps = fm.get("depends_on") or []
    deps = {str(d) for d in ([deps] if isinstance(deps, str) else deps)}
    for sid, ref in _must_depend(must_depend_on):          # SPEC-0423: the `after` prerequisites
        if sid not in deps:
            out.append(f"front matter: depends_on must include {sid} (prerequisite {ref})")
    return out


def _amend_problems(fm_before, fm) -> list:
    """SPEC-0279: an amend leaves every front-matter key as it was, except `updated`."""
    before, after = _normalize(fm_before or {}), _normalize(fm)
    out = []
    for k in sorted(set(before) | set(after) - {"updated"}):
        if k == "updated":
            continue
        if k not in after:
            out.append(f"front matter: {k} was removed (an amend may change only `updated`)")
        elif k not in before:
            out.append(f"front matter: {k} was added (an amend may change only `updated`)")
        elif before[k] != after[k]:
            out.append(f"front matter: {k} was changed from {before[k]!r} to {after[k]!r} "
                       "(an amend may change only `updated`)")
    return out


def _split_problems(fm, expected) -> list:
    """SPEC-0290: a split part carries the exact wiring `_prepare_spec_write` recorded in
    `spec_write["expected_fm"]` (`id`, `status`, `kind`, `supersedes`, `parent`, `depends_on`);
    an absent `parent` is expected when the original has none."""
    out = []
    for k in ("id", "status", "kind"):
        if fm.get(k) != expected[k]:
            out.append(f"front matter: {k} must be {expected[k]!r}, got {fm.get(k)!r}")
    for k in ("supersedes", "depends_on"):
        got = fm.get(k) or []
        got = [got] if isinstance(got, str) else list(got)
        if got != list(expected[k]):
            out.append(f"front matter: {k} must be {list(expected[k])!r}, got {fm.get(k)!r}")
    if fm.get("parent") != expected.get("parent"):
        want = repr(expected["parent"]) if expected.get("parent") else "absent"
        out.append(f"front matter: parent must be {want}, got {fm.get('parent')!r}")
    return out


def _content_problems(worktree, text, spec_id, idea, spec_write=None) -> list:
    try:
        fm, body = split_frontmatter(text)
    except Exception as e:                      # yaml.YAMLError and friends
        return [f"front matter: not valid YAML ({e})"]
    if not isinstance(fm, dict):
        return ["front matter: missing (a spec starts with a --- block)"]
    if (spec_write or {}).get("mode") == "amend":
        out = _schema_problems(worktree, fm) + _amend_problems(spec_write.get("fm_before"), fm)
    elif (spec_write or {}).get("mode") == "split":
        out = _schema_problems(worktree, fm) + _split_problems(fm, spec_write["expected_fm"])
    else:
        out = _schema_problems(worktree, fm) + _front_matter_problems(
            fm, spec_id, idea, (spec_write or {}).get("must_depend_on") or ())
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
        out.append(f"no change made to {path}" if mode in ("revise", "amend")
                   else f"{path} was not created")
    f = worktree / path
    if changed.get(path) != "D" and path in changed and f.is_file():
        out += _content_problems(worktree, f.read_text(encoding="utf-8"), spec_id, idea,
                                 spec_write)
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


_FM_BLOCK_RE = re.compile(r"\A---\n(.*?)\n---\n", re.S)


def supersede(worktree, original_rel, superseded_by) -> bool:
    """SPEC-0290: mark the original spec `superseded` by `superseded_by` and bump `updated`,
    rewriting only those front-matter lines (the body and every other key stay byte for byte).
    Idempotent: False when the file already says so. ValueError when it has no front matter."""
    path = Path(worktree) / original_rel
    text = path.read_text(encoding="utf-8")
    m = _FM_BLOCK_RE.match(text)
    if not m:
        raise ValueError(f"{original_rel}: no front matter")
    fm = split_frontmatter(text)[0] or {}
    today = dt.date.today().isoformat()
    if fm.get("status") == "superseded" and fm.get("superseded_by") == superseded_by:
        return False
    out, seen, in_old_by = [], set(), False
    for ln in m.group(1).split("\n"):
        key = ln.split(":", 1)[0] if re.match(r"[A-Za-z_]+:", ln) else None
        if in_old_by and key is None and re.match(r"\s|-", ln):
            continue                            # a block-list `superseded_by:` value line
        in_old_by = key == "superseded_by"
        if key == "status":
            out += ["status: superseded", f"superseded_by: {superseded_by}"]
        elif key == "updated":
            out.append(f"updated: {today}")
        elif key != "superseded_by":
            out.append(ln)
        seen.add(key)
    if "status" not in seen:
        raise ValueError(f"{original_rel}: no `status` in the front matter")
    if "updated" not in seen:
        out.append(f"updated: {today}")
    path.write_text("---\n" + "\n".join(out) + "\n---\n" + text[m.end():], encoding="utf-8")
    return True


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
        req = (idea or {}).get("source_request")
        if req:
            fm.insert(3, f"`source_request: {req}`")
        if epic:
            fm.append(f"`parent: {epic}`")
        must = _must_depend(spec_write.get("must_depend_on"))
        if must:                                            # SPEC-0423
            fm.append("`depends_on` must include: " + ", ".join(
                f"{sid} (prerequisite {ref})" if sid != ref else sid for sid, ref in must))
        summary_note = ""
        if req:
            summary_note = (
                " The Context section is the public summary in the idea JSON `summary` field "
                "for that request, the text other people have seen; the private log and open "
                "questions stay available for the rest of the spec and are not a replacement "
                "for that summary."
            )
        else:
            summary_note = " The front matter has no `source_request` key."
        return (f"Write mode: create exactly `{path}`, following the template. Its front matter "
                f"carries {', '.join(fm)} and no keys the schema does not know.{summary_note} "
                f"Required sections: {', '.join(REQUIRED_HEADINGS)} (and Definition of done).")
    if mode == "split":
        sp = spec_write["split"]
        efm = spec_write["expected_fm"]
        fm = [f"`id: {efm['id']}`", f"`status: {efm['status']}`", f"`kind: {efm['kind']}`",
              "`owner: user`", "`title`", "`created` (today, YYYY-MM-DD)",
              f"`supersedes: {json.dumps(efm['supersedes'])}`",
              f"`depends_on: {json.dumps(efm['depends_on'])}`"]
        fm.append(f"`parent: {efm['parent']}`" if efm.get("parent") else "no `parent` key")
        if sp.get("source_idea"):
            fm.append(f"`source_idea: {sp['source_idea']}`")
        others = "\n".join(f"- part {i}: {o['title']} — {o['scope']}"
                           for i, o in enumerate(sp["parts"], 1) if i != sp["k"])
        return (f"Split mode: create exactly `{path}`: part {sp['k']} of {sp['n']} of the spec "
                f"{sp['original']}, which is being replaced by its parts. Your scope: "
                f"{sp['parts'][sp['k'] - 1]['scope']}\nThe other parts are Non-goals of this "
                f"spec (list them there):\n{others}\nCarry over only this part's Context, "
                "Design, acceptance criteria and Test plan items from the original spec text, "
                f"following the template. Its front matter carries exactly {', '.join(fm)} and no "
                "keys the schema does not know. Required sections: "
                f"{', '.join(REQUIRED_HEADINGS)} (and Definition of done).")
    if mode == "amend":
        return (f"Amend mode: edit only `{path}`; leave every front-matter key unchanged except "
                "`updated`; address every finding in the brief.")
    rules = (f"Revise mode: edit only `{path}`; keep its id, `source_idea` and "
             "`status: proposed`; address every must_fix and major finding in the brief.")
    if (idea or {}).get("source_request"):
        rules += " Keep `source_request` when the idea has one."
    if (brief or {}).get("suggested_splits"):
        rules += (" The review suggests splitting: narrow the spec to the first suggested split "
                  "(or the one a human note names) and list the rest under Non-goals.")
    return rules
