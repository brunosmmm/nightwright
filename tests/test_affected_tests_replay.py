"""SPEC-0357: replay the recorded 2026-11-01 loop-gate fallbacks through `tools/affected_tests.py`
against a temporary copy of the real repository tree (never the checkout under test)."""
import fnmatch
import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("affected_tests_replay_tool",
                                               ROOT / "tools" / "affected_tests.py")
AT = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(AT)

CASES = json.loads((ROOT / "tests" / "data" / "affected_fallback_replay.json").read_text())["cases"]


@pytest.fixture(scope="module")
def tree_copy(tmp_path_factory):
    """A copy of the working tree's files (tracked plus untracked, ignoring .gitignore'd)."""
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z", "--cached", "--others",
                          "--exclude-standard"], capture_output=True, text=True, check=True).stdout
    dest = tmp_path_factory.mktemp("replay") / "tree"
    for rel in filter(None, out.split("\0")):
        if not rel.startswith(("src/", "tests/", "tools/", ".nw/")) and rel not in (
                "pyproject.toml", "uv.lock"):
            continue
        src = ROOT / rel
        if src.is_file():
            (dest / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest / rel)
    return dest


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_recorded_fallback(tree_copy, tmp_path, case):
    copy = tmp_path / "tree"
    shutil.copytree(tree_copy, copy)
    for rel, text in case.get("stage", {}).items():
        if not (copy / rel).exists():                # created only in the copy
            (copy / rel).parent.mkdir(parents=True, exist_ok=True)
            (copy / rel).write_text(text)
    sel = AT.select(copy, case["changed"])
    assert sel.full is case["full"], sel.reason
    if case["full"]:
        return
    for t in case.get("must_include", []):
        assert t in sel.tests, (t, sel.reason)
    if "must_include_glob" in case:
        assert any(fnmatch.fnmatch(t, case["must_include_glob"]) for t in sel.tests), sel.tests
    if "max_tests" in case:
        assert len(sel.tests) <= case["max_tests"], sel.tests
    all_tests = [t for t in AT.import_graph(copy)[0].values() if AT._is_test_module(t)]
    # a case may widen the cap: a config file named by string in many tests legitimately fans out
    assert len(sel.tests) <= case.get("max_fraction", AT.MAX_FRACTION) * len(all_tests)


# --- SPEC-0421: the late-guard gap, real git, real tool, real pytest -------------------------------

_GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null"}

_LATE_GUARD = '''import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.tree_guard

TESTS = Path(__file__).parent


def test_no_numeric_join():
    bad = [p.name for p in TESTS.glob("test_*.py")
           if p.name != Path(__file__).name and re.search(r"\\.join\\(\\s*5\\s*\\)", p.read_text())]
    assert not bad, bad
'''


def _g(repo, *args):
    import os
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                       env={**os.environ, **_GIT_ENV})
    assert r.returncode == 0, r.stderr
    return r.stdout


def test_a_guard_that_landed_on_main_after_the_branch_was_cut_still_runs(tmp_path, monkeypatch):
    import os
    import sys
    for k in ("NW_SENTINEL", "NW_CAMPAIGN", "NW_AFFECTED_FULL", "NW_AFFECTED_STRICT", "NW_BASE"):
        monkeypatch.delenv(k, raising=False)
    r = tmp_path / "repo"
    (r / "tests").mkdir(parents=True)
    (r / "src" / "nightwright").mkdir(parents=True)
    (r / "src" / "nightwright" / "__init__.py").write_text("")
    (r / "tests" / "test_a.py").write_text("def test_a():\n    pass\n")
    (r / "pyproject.toml").write_text("[project]\nname='x'\n[tool.pytest.ini_options]\n"
                                      "markers = ['tree_guard: x']\n")
    _g(r, "init", "-q", "-b", "main")
    _g(r, "add", "-A")
    _g(r, "commit", "-qm", "init")
    _g(r, "checkout", "-qb", "feat")
    (r / "tests" / "test_late.py").write_text(
        "import threading\n\ndef test_late():\n    t = threading.Thread(target=lambda: None)\n"
        "    t.start()\n    t.jo" "in(5)\n")     # spelled apart: test_test_waits scans this file
    _g(r, "add", "-A")
    _g(r, "commit", "-qm", "feat: add a test with a numeric join")
    _g(r, "checkout", "-q", "main")                       # the guard lands on main afterwards
    (r / "tests" / "test_guard_join.py").write_text(_LATE_GUARD)
    _g(r, "add", "-A")
    _g(r, "commit", "-qm", "main: add the guard")
    _g(r, "checkout", "-q", "feat")
    _g(r, "rebase", "-q", "main")
    tool = str(ROOT / "tools" / "affected_tests.py")
    env = {**os.environ, **_GIT_ENV, "PYTHONDONTWRITEBYTECODE": "1"}
    run = [sys.executable, tool, "--repo", str(r)]
    pytest_cmd = ["--run", sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]
    # the gap (SPEC-0378 selection without the tree-reader rule or --guards): the branch merges green
    without = subprocess.run(run + ["--strict"] + pytest_cmd, capture_output=True, text=True, env=env)
    assert without.returncode == 0, without.stdout + without.stderr
    assert "test_guard_join" not in without.stdout
    guarded = subprocess.run(run + ["--guards"] + pytest_cmd, capture_output=True, text=True, env=env)
    assert guarded.returncode != 0, guarded.stdout + guarded.stderr
    assert "test_guard_join.py" in guarded.stdout and "test_late.py" in guarded.stdout
    assert "+1 guards" in guarded.stderr
    # the tree-reader rule alone also selects the guard, since it scans tests/ (no --guards)
    rule = subprocess.run(run + pytest_cmd, capture_output=True, text=True, env=env)
    assert rule.returncode != 0 and "test_guard_join.py" in rule.stdout
