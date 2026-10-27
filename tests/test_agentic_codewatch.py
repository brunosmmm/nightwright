"""SPEC-0231: code-change detection shared by the runner and the followers."""
from wt.agentic import codewatch as CW
from wt.agentic import queue as Q


def test_queue_reexports_helpers():
    assert Q.code_checkout is CW.code_checkout
    assert Q.code_changes is CW.code_changes
    assert Q._git_out is CW._git_out
    assert Q._CODE_PATHS == CW._CODE_PATHS == ("src/wt", "pyproject.toml")


def test_code_changes_paths_argument(code_repo):
    base = code_repo.head()
    code_repo.commit({"skills/wt-conduct/SKILL.md": "b\n"})
    head, changed = CW.code_changes(code_repo.root, base)
    assert changed == []                           # the runner's paths ignore skills
    head2, changed = CW.code_changes(code_repo.root, base, CW.FOLLOWER_PATHS)
    assert head2 == head and changed == ["skills/wt-conduct/SKILL.md"]


def test_watcher_quiet_without_change_and_before_interval(code_repo):
    w = CW.Watcher(code_repo.root, 10, now=100.0)
    assert w.poll(101.0) is None
    code_repo.commit({"src/wt/x.py": "x = 2\n"})
    assert w.poll(105.0) is None                   # interval not elapsed yet
    assert w.poll(111.0)["paths"] == ["src/wt/x.py"]


def test_watcher_no_head_change(code_repo):
    w = CW.Watcher(code_repo.root, 10, now=0.0)
    assert w.poll(50.0) is None


def test_watcher_docs_only_moves_baseline(code_repo):
    w = CW.Watcher(code_repo.root, 10, now=0.0)
    code_repo.commit({"docs/a.md": "b\n"})
    assert w.poll(11.0) is None
    docs_head = code_repo.head()
    assert w.sha == docs_head
    code_repo.commit({"src/wt/x.py": "x = 3\n"})
    got = w.poll(22.0)
    assert got["from_sha"] == docs_head and got["to_sha"] == code_repo.head()
    assert got["paths"] == ["src/wt/x.py"]


def test_watcher_src_commit_has_empty_skills_changed(code_repo):
    w = CW.Watcher(code_repo.root, 10, now=0.0)
    before = code_repo.head()
    code_repo.commit({"src/wt/x.py": "x = 2\n"})
    got = w.poll(11.0)
    assert got == {"from_sha": before, "to_sha": code_repo.head(),
                   "paths": ["src/wt/x.py"], "skills_changed": []}


def test_watcher_skill_commit_names_skill_dirs(code_repo):
    w = CW.Watcher(code_repo.root, 10, now=0.0)
    code_repo.commit({"skills/wt-conduct/SKILL.md": "b\n", "skills/wt-steward/SKILL.md": "c\n"})
    got = w.poll(11.0)
    assert got["skills_changed"] == ["wt-conduct", "wt-steward"]


def test_watcher_pyproject_counts(code_repo):
    w = CW.Watcher(code_repo.root, 10, now=0.0)
    code_repo.commit({"pyproject.toml": "[project]\n"})
    assert w.poll(11.0)["paths"] == ["pyproject.toml"]


def test_watcher_disabled(code_repo, tmp_path):
    for w in (CW.Watcher(None, 10, now=0.0), CW.Watcher(code_repo.root, 0, now=0.0),
              CW.Watcher(tmp_path / "nogit", 10, now=0.0)):
        assert not w.enabled
        assert w.poll(1000.0) is None


def test_watcher_reports_diff_failure_once(code_repo, monkeypatch):
    """SPEC-0262: a failed `git diff` is a change with unknown paths, reported once; the
    baseline moves, so the same failing diff is never retried forever."""
    w = CW.Watcher(code_repo.root, 10, now=0.0)
    base = w.sha
    code_repo.commit({"src/wt/x.py": "x = 2\n"})
    monkeypatch.setattr(CW, "_changed_paths", lambda *a: None)
    got = w.poll(11.0)
    assert got == {"from_sha": base, "to_sha": code_repo.head(), "paths": [],
                   "skills_changed": [], "diff_failed": True}
    assert w.sha == code_repo.head()
    assert w.poll(22.0) is None                          # same head: nothing more


def test_code_changes_failed_diff_is_unknown_paths(code_repo, monkeypatch):
    base = code_repo.head()
    code_repo.commit({"docs/a.md": "b\n"})
    monkeypatch.setattr(CW, "_changed_paths", lambda *a: None)
    assert CW.code_changes(code_repo.root, base) == (code_repo.head(), None)
    assert CW.code_changes(code_repo.root, code_repo.head()) == (code_repo.head(), [])


def test_code_changed_line_carries_diff_failed():
    from wt.agentic import events as E
    import json
    change = {"from_sha": "a" * 40, "to_sha": "b" * 40, "paths": [], "skills_changed": [],
              "diff_failed": True}
    assert json.loads(E.code_changed_line("r", change))["data"]["diff_failed"] is True
    assert E.code_changed_line("r", change, as_json=False).rstrip().endswith(
        "skills=- diff_failed (re-arm)")
    plain = {k: v for k, v in change.items() if k != "diff_failed"}
    assert "diff_failed" not in E.code_changed_line("r", plain, as_json=False)


def test_watcher_non_ascii_skill_path(code_repo):
    w = CW.Watcher(code_repo.root, 10, now=0.0)
    code_repo.commit({"skills/café/SKILL.md": "b\n"})
    got = w.poll(11.0)
    assert got["paths"] == ["skills/café/SKILL.md"] and got["skills_changed"] == ["café"]
