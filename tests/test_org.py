"""Org parser + Task model (SPEC-0005): per-file #+TODO done-resolution, grouped-tag
splitting, priority/CLOSED/properties capture, topic_key extraction (headline + property
override), id fallback, project derivation, source expansion, and list-replace config."""
import os

from nightwright import config as C
from nightwright.org import Task, _iter_org_paths, _split_keywords, filter_tasks, load_tasks
import pytest

pytestmark = pytest.mark.fast

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "org")


def _cfg(org_files=None, keywords=None):
    return {
        "org_files": org_files if org_files is not None else [FIXTURES],
        "org_todo_keywords": keywords or ["TODO", "|", "DONE"],
    }


def _by_key(tasks):
    return {t.topic_key: t for t in tasks if t.topic_key}


def _find(tasks, sub):
    return next(t for t in tasks if sub in t.heading)


# ---- keyword splitting ------------------------------------------------------

def test_split_keywords_with_bar():
    assert _split_keywords(["TODO", "INPROGRESS", "|", "DONE", "CANCELED"]) == (
        ["TODO", "INPROGRESS"], ["DONE", "CANCELED"])


def test_split_keywords_without_bar():
    assert _split_keywords(["TODO", "NEXT"]) == (["TODO", "NEXT"], [])


# ---- source expansion -------------------------------------------------------

def test_iter_org_paths_dir_recurses_and_dedupes():
    paths = _iter_org_paths(_cfg())
    names = sorted(os.path.basename(p) for p in paths)
    assert names == ["agenda.org", "estimations.org", "inbox.org", "overrides.org",
                     "planned.org"]
    # subdir file was found via recursion
    assert any(p.endswith(os.path.join("sub", "overrides.org")) for p in paths)


def test_iter_org_paths_single_file():
    f = os.path.join(FIXTURES, "inbox.org")
    assert _iter_org_paths(_cfg([f])) == [f]


# ---- per-file done resolution ----------------------------------------------

def test_is_done_uses_each_files_own_keywords():
    tasks = load_tasks(_cfg())
    # estimations.org: DONE/CANCELED are done keys
    assert _find(tasks, "old finished ticket").is_done is True
    assert _find(tasks, "abandoned work").is_done is True
    # estimations.org: INPROGRESS is active
    assert _by_key(tasks)["DEMO-504"].is_done is False
    # inbox.org: DISCARDED is a done key there (its own #+TODO), REFILE is active
    assert _find(tasks, "old inbox note").is_done is True
    assert _find(tasks, "pre-SP2 device-tree").is_done is False
    # agenda.org: NONACTIONABLE is a done key there
    assert _find(tasks, "Someday maybe idea").is_done is True


def test_fallback_keywords_for_file_without_todo_header():
    # sub/overrides.org declares no #+TODO -> uses seeded fallback (TODO | DONE)
    tasks = load_tasks(_cfg())
    assert _find(tasks, "plain done under fallback").is_done is True
    assert _find(tasks, "comes from a property").is_done is False


# ---- tags, priority, closed, properties ------------------------------------

def test_grouped_tags_split_into_set():
    tasks = load_tasks(_cfg())
    t = _by_key(tasks)["DEMO-504"]
    assert {"projects", "demo"} <= t.tags


def test_priority_and_closed_and_properties():
    tasks = load_tasks(_cfg())
    t = _by_key(tasks)["DEMO-504"]
    assert t.priority == "A"
    assert t.properties["OriginalPoints"] == "2.5"
    assert t.scheduled.isoformat() == "2026-07-20"
    assert t.deadline.isoformat() == "2026-07-25"
    done = _find(tasks, "old finished ticket")
    assert done.closed.isoformat() == "2026-07-01"


# ---- topic_key extraction ---------------------------------------------------

def test_topic_key_from_headline():
    tasks = load_tasks(_cfg())
    assert _by_key(tasks)["DEMO-504"].heading.startswith("DEMO-504")
    assert _find(tasks, "DEMO-506").topic_key == "DEMO-506"


def test_topic_key_property_override_beats_headline():
    tasks = load_tasks(_cfg())
    t = _find(tasks, "TOPIC wins")           # heading mentions ABC-99, TOPIC=DEMO-777
    assert t.topic_key == "DEMO-777"
    j = _find(tasks, "comes from a property")
    assert j.topic_key == "OVERRIDE-1"


# ---- id + project -----------------------------------------------------------

def test_id_uses_property_else_file_line():
    tasks = load_tasks(_cfg())
    assert _by_key(tasks)["DEMO-504"].id == "est-504"      # :ID: property
    fallback = _by_key(tasks)["DEMO-506"]                  # no :ID:
    assert fallback.id == f"{fallback.file}:{fallback.line}"


def test_project_is_nearest_level1_ancestor_else_file_stem():
    tasks = load_tasks(_cfg())
    # agenda.org: nested under a level-1 category heading
    assert _find(tasks, "Ship the drone perf fix").project == "Active Projects"
    assert _find(tasks, "File expense report").project == "Administrative"
    # estimations.org: top-level task -> file stem
    assert _by_key(tasks)["DEMO-504"].project == "estimations"


# ---- filter_tasks -----------------------------------------------------------

def test_filter_tasks_compose():
    tasks = load_tasks(_cfg())
    inprog = filter_tasks(tasks, state="INPROGRESS")
    assert all(t.state == "INPROGRESS" for t in inprog) and inprog
    demo = filter_tasks(tasks, tag="demo", done=False)
    assert demo and all("demo" in t.tags and not t.is_done for t in demo)
    keyed = filter_tasks(tasks, has_key=True)
    assert keyed and all(t.topic_key for t in keyed)
    assert filter_tasks(tasks, priority="A") == [_by_key(tasks)["DEMO-504"]]


# ---- load_tasks mtime cache (SPEC-0134) -------------------------------------

def test_load_tasks_cache_hit_skips_orgparse(tmp_path, monkeypatch):
    """Unchanged corpus: second load_tasks must not call orgparse.load again."""
    import orgparse
    from nightwright.org import invalidate_tasks_cache

    org = tmp_path / "org"
    org.mkdir()
    (org / "a.org").write_text("* TODO one\n")
    cfg = _cfg([str(org)])
    invalidate_tasks_cache()

    real_load = orgparse.load
    calls = []

    def counting_load(path, *args, **kwargs):
        # orgparse.load re-enters itself with an open file handle. Count path opens only,
        # and unpatch for the real call so the recursive entry keeps the outer `env=`.
        if isinstance(path, (str, os.PathLike)):
            calls.append(1)
        monkeypatch.setattr(orgparse, "load", real_load)
        try:
            return real_load(path, *args, **kwargs)
        finally:
            monkeypatch.setattr(orgparse, "load", counting_load)

    monkeypatch.setattr(orgparse, "load", counting_load)
    a = load_tasks(cfg)
    b = load_tasks(cfg)
    assert len(calls) == 1
    assert [t.heading for t in a] == [t.heading for t in b] == ["one"]
    # Callers get a fresh list (mutating the return must not poison the cache).
    a.append(a[0])
    assert len(load_tasks(cfg)) == 1


def test_load_tasks_cache_miss_on_mtime_bump(tmp_path, monkeypatch):
    import time
    from nightwright.org import invalidate_tasks_cache

    org = tmp_path / "org"
    org.mkdir()
    f = org / "a.org"
    f.write_text("* TODO one\n")
    cfg = _cfg([str(org)])
    invalidate_tasks_cache()
    assert [t.heading for t in load_tasks(cfg)] == ["one"]

    time.sleep(0.01)
    f.write_text("* TODO two\n* TODO three\n")
    assert [t.heading for t in load_tasks(cfg)] == ["two", "three"]


def test_invalidate_tasks_cache_forces_reparse(tmp_path, monkeypatch):
    import orgparse
    from nightwright.org import invalidate_tasks_cache

    org = tmp_path / "org"
    org.mkdir()
    (org / "a.org").write_text("* TODO one\n")
    cfg = _cfg([str(org)])
    invalidate_tasks_cache()

    real_load = orgparse.load
    calls = []

    def counting_load(path, *args, **kwargs):
        if isinstance(path, (str, os.PathLike)):
            calls.append(1)
        monkeypatch.setattr(orgparse, "load", real_load)
        try:
            return real_load(path, *args, **kwargs)
        finally:
            monkeypatch.setattr(orgparse, "load", counting_load)

    monkeypatch.setattr(orgparse, "load", counting_load)
    load_tasks(cfg)
    load_tasks(cfg)
    assert len(calls) == 1
    invalidate_tasks_cache()
    load_tasks(cfg)
    assert len(calls) == 2


def test_load_tasks_cache_miss_on_keyword_change(tmp_path, monkeypatch):
    """Keyword lists shape is_idea / is_done — changing them must bust the cache."""
    import orgparse
    from nightwright.org import invalidate_tasks_cache

    org = tmp_path / "org"
    org.mkdir()
    (org / "a.org").write_text("* IDEA one\n")
    # IDEA must be in org_todo_keywords for orgparse to treat it as a TODO token.
    cfg = _cfg([str(org)], keywords=["IDEA", "TODO", "|", "DONE"])
    cfg["org_idea_keywords"] = ["IDEA", "|", "DONE"]
    invalidate_tasks_cache()

    real_load = orgparse.load
    calls = []

    def counting_load(path, *args, **kwargs):
        if isinstance(path, (str, os.PathLike)):
            calls.append(1)
        monkeypatch.setattr(orgparse, "load", real_load)
        try:
            return real_load(path, *args, **kwargs)
        finally:
            monkeypatch.setattr(orgparse, "load", counting_load)

    monkeypatch.setattr(orgparse, "load", counting_load)
    assert load_tasks(cfg)[0].is_idea is True
    assert len(calls) == 1
    cfg = dict(cfg)
    cfg["org_idea_keywords"] = []          # same files, different vocabulary
    assert load_tasks(cfg)[0].is_idea is False
    assert len(calls) == 2


# ---- config list-replace semantics -----------------------------------------

def test_default_config_has_org_keys():
    assert C.DEFAULT_CONFIG["org_files"] == ["~/work/org"]
    assert "|" in C.DEFAULT_CONFIG["org_todo_keywords"]


def test_user_org_files_list_replaces_default(monkeypatch, tmp_path):
    # a user config.yaml org_files list must REPLACE the default, not merge
    monkeypatch.setattr(C, "_load_yaml", lambda *a, **k: {"org_files": ["/custom/a.org"]})
    monkeypatch.setattr(C.paths, "config_dir", lambda: str(tmp_path))
    monkeypatch.setattr(C.paths, "data_dir", lambda: str(tmp_path))
    monkeypatch.setattr(C.paths, "cache_dir", lambda: str(tmp_path))
    cfg = C.load_config()
    assert cfg["org_files"] == ["/custom/a.org"]


