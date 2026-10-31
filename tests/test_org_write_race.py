"""SPEC-0190: idea writes under concurrency — a writer finds its headline by identity (never a
stale line number), refuses rather than editing another entry, and every org read-modify-write
holds one cross-process lock (no lost updates, no duplicate IDEA ids)."""
import ast
import multiprocessing as mp
import pathlib

import pytest

from test_assoc import _cfg
from wt import explore as X
from wt import org_write as W
from wt.org import load_tasks

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "wt"


def _idea(cfg, iid):
    return next(t for t in load_tasks(cfg) if t.properties.get("ID") == iid)


def _three(cfg):
    for text in ("first idea", "second idea", "third idea"):
        W.add_idea(cfg, text)
    return [_idea(cfg, f"IDEA-00{n}") for n in (1, 2, 3)]


def _remove_subtree(path, heading):
    """What a concurrent archive does: drop one top-level subtree from the file."""
    lines = open(path).read().splitlines(keepends=True)
    start = next(i for i, line in enumerate(lines) if line.startswith("* ") and heading in line)
    end = next((j for j in range(start + 1, len(lines)) if lines[j].startswith("* ")), len(lines))
    open(path, "w").write("".join(lines[:start] + lines[end:]))


def test_stale_line_relocates_to_the_same_idea(tmp_path):
    cfg = _cfg(tmp_path)
    first, second, third = _three(cfg)
    _remove_subtree(first.file, "first idea")        # IDEA-002/003 shift up; `third` is stale
    W.set_property(cfg, third, "KIND", "bug")
    assert _idea(cfg, "IDEA-003").properties.get("KIND") == "bug"
    assert "KIND" not in _idea(cfg, "IDEA-002").properties


def test_another_idea_at_the_stale_line_is_never_edited(tmp_path):
    """The IDEA-003 overwrite: after a shift, a *different* idea in the same state sits at the
    stale line. The old state-only drift check passed there; the identity check must not."""
    cfg = _cfg(tmp_path)
    first, second, third = _three(cfg)
    _remove_subtree(first.file, "first idea")
    assert _idea(cfg, "IDEA-003").line != third.line          # everything below shifted up
    lines = open(first.file).read().splitlines()
    assert lines[second.line - 1].startswith("* IDEA")       # a same-state headline is there…
    assert "second idea" not in lines[second.line - 1]        # …but it is not IDEA-002
    W.set_property(cfg, second, "KIND", "chore")
    assert _idea(cfg, "IDEA-002").properties.get("KIND") == "chore"
    assert "KIND" not in _idea(cfg, "IDEA-003").properties


def test_vanished_idea_refuses_and_leaves_the_file_alone(tmp_path):
    cfg = _cfg(tmp_path)
    first, second, third = _three(cfg)
    _remove_subtree(first.file, "second idea")
    before = open(first.file).read()
    for write in (lambda: W.set_property(cfg, second, "KIND", "bug"),
                  lambda: W.set_state(cfg, second, "INCUBATE"),
                  lambda: X._replace_section_body(cfg, second, X.SECTION_SUMMARY, "x")):
        with pytest.raises(ValueError, match="drift: IDEA-002"):
            write()
    with pytest.raises(ValueError, match="no task matched 'IDEA-002'"):
        X.append_log(cfg, "IDEA-002", "note")                # selector writers re-resolve
    assert open(first.file).read() == before


def test_section_writers_relocate_too(tmp_path):
    """explore's subtree writers (summary/log/questions) go through the same lookup."""
    cfg = _cfg(tmp_path)
    first, second, third = _three(cfg)
    _remove_subtree(first.file, "first idea")
    X._replace_section_body(cfg, third, X.SECTION_SUMMARY, "third summary")
    text = open(first.file).read()
    assert text.index("third summary") > text.index("third idea")
    assert text.index("third summary") > text.index("IDEA-003")
    assert "IDEA-002" in text and text.index("IDEA-002") < text.index("third idea")


def test_tasks_without_id_match_on_heading(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_task(cfg, "alpha task", state="TODO")
    W.add_task(cfg, "beta task", state="TODO")
    beta = next(t for t in load_tasks(cfg) if t.heading == "beta task")
    _remove_subtree(beta.file, "alpha task")
    W.set_state(cfg, beta, "DONE")
    states = {t.heading: t.state for t in load_tasks(cfg)}
    assert states == {"beta task": "DONE"}


def test_lock_is_reentrant_and_released(tmp_path):
    cfg = _cfg(tmp_path)
    with W.org_write_lock(cfg):
        with W.org_write_lock(cfg):
            W.add_idea(cfg, "nested")                # a writer inside the lock re-enters it
    import fcntl
    import os
    fd = os.open(W._org_lock_path(cfg), os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # free again once released
    finally:
        os.close(fd)


def _capture_many(args):
    cfg, n, who = args
    for i in range(n):
        W.add_idea(cfg, f"{who} idea {i}")


def _set_kinds(args):
    cfg, ids = args
    for iid in ids:
        W.set_idea_kind(cfg, iid, "bug")


def test_concurrent_processes_lose_nothing_and_never_reuse_an_id(tmp_path):
    cfg = _cfg(tmp_path)
    cfg = {k: v for k, v in cfg.items() if not k.startswith("_")}   # picklable for spawn
    ctx = mp.get_context("fork")
    with ctx.Pool(4) as pool:
        pool.map(_capture_many, [(cfg, 8, f"w{w}") for w in range(4)])
    ideas = [t for t in load_tasks(cfg) if t.is_idea]
    ids = [t.properties.get("ID") for t in ideas]
    assert len(ideas) == 32 and len(set(ids)) == 32
    by_worker = [ids[i::4] for i in range(4)]
    with ctx.Pool(4) as pool:
        pool.map(_set_kinds, [(cfg, chunk) for chunk in by_worker])
    assert all(t.properties.get("KIND") == "bug" for t in load_tasks(cfg) if t.is_idea)


def test_a_directory_scan_that_misses_the_ideas_file_still_loads_it(tmp_path, monkeypatch):
    """A directory scan racing another process's atomic rewrite of `ideas.org` can miss the file
    (btrfs, ~2% of scans): the concurrent-writers test then failed with 'no task matched'. wt's
    own files under a configured directory are listed by name, not left to the scan."""
    from wt import org as ORG
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "an idea")
    W.add_task(cfg, "a captured task", state="TODO")
    real_rglob = pathlib.Path.rglob
    monkeypatch.setattr(pathlib.Path, "rglob", lambda self, pat: [
        x for x in real_rglob(self, pat) if x.name not in ("ideas.org", "inbox.org")])
    ORG.invalidate_tasks_cache()
    headings = {t.heading for t in load_tasks(cfg)}
    assert {"an idea", "a captured task"} <= headings
    paths = ORG._iter_org_paths(cfg)
    assert len(paths) == len(set(paths)) == 2


def _decorated(fn: ast.FunctionDef) -> bool:
    return any((isinstance(d, ast.Name) and d.id == "org_writer") for d in fn.decorator_list)


def test_every_org_writer_holds_the_lock():
    """Any function that performs the backup + atomic write (or writes the ideas file directly)
    must be an @org_writer, so its read-modify-write runs under the lock."""
    missing = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef) or fn.name == "_atomic_backup_write":
                continue
            writes = any(isinstance(c, ast.Call)
                         and getattr(c.func, "id", getattr(c.func, "attr", None))
                         == "_atomic_backup_write"
                         for c in ast.walk(fn))
            if writes and not _decorated(fn):
                missing.append(f"{path.relative_to(SRC)}:{fn.lineno} {fn.name}")
    assert not missing, "decorate with @org_writer:\n  " + "\n  ".join(missing)
