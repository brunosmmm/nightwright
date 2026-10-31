"""SPEC-0328: `tools/affected_tests.py`, static affected-test selection for loop gates.

Every case builds a small temp git repo shaped like wt (src/wt + tests), branches off main,
changes files, and checks the selection (or the full-suite fallback)."""
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("affected_tests", ROOT / "tools" / "affected_tests.py")
AT = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(AT)

ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
       "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}

FILES = {
    "pyproject.toml": "[project]\nname='wt'\n",
    "src/wt/__init__.py": "",
    "src/wt/base.py": "X = 1\n",
    "src/wt/mid.py": "from . import base\n",
    "src/wt/top.py": "from .mid import base\n",
    "src/wt/lazy.py": "def f():\n    from wt import base\n    return base\n",
    "src/wt/lonely.py": "Y = 2\n",
    "src/wt/cli.py": "from . import top, lonely\n",          # a hub
    "src/wt/data.md": "doc\n",
    "src/wt/agentic/__init__.py": "",
    "src/wt/agentic/core.py": "from .. import base\n",
    "tests/conftest.py": "",
    "tests/waits.py": "",
    "tests/helpers.py": "from wt import top\n",
    "tests/test_base.py": "from wt import base\n",
    "tests/test_mid.py": "import wt.mid\n",
    "tests/test_top.py": "from wt.top import base\n",
    "tests/test_via_helper.py": "from helpers import *\n",
    "tests/test_cli.py": "from wt.cli import top\n",
    "tests/test_core.py": "def test():\n    from wt.agentic import core\n",
    "tests/test_lazy.py": "from wt import lazy\n",
    "docs/note.md": "hi\n",
}


@pytest.fixture(autouse=True)
def _git_env(monkeypatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True).stdout


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    for rel, text in FILES.items():
        p = r / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    _git(r, "init", "-q", "-b", "main")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "init")
    _git(r, "checkout", "-qb", "feat")
    return r


def _change(repo, *rels, commit=True):
    for rel in rels:
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text((p.read_text() if p.exists() else "") + "# changed\n")
    if commit:
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "change")


def _select(repo, **kw):
    return AT.select(repo, AT.changed_files(repo, "main"), **kw)


def test_changed_files_include_committed_uncommitted_and_untracked(repo):
    _change(repo, "src/wt/base.py")
    _change(repo, "src/wt/mid.py", commit=False)
    (repo / "tests" / "test_new.py").write_text("")
    assert AT.changed_files(repo, "main") == ["src/wt/base.py", "src/wt/mid.py",
                                              "tests/test_new.py"]


def test_changed_files_ignore_main_moving_on(repo):
    _git(repo, "checkout", "-q", "main")
    _change(repo, "src/wt/lonely.py")                 # someone else's change on main
    _git(repo, "checkout", "-q", "feat")
    _change(repo, "src/wt/top.py")
    assert AT.changed_files(repo, "main") == ["src/wt/top.py"]


def test_changed_src_module_selects_its_direct_importers(repo):
    _change(repo, "src/wt/base.py")
    sel = _select(repo, max_fraction=1.0)
    # the nearest level only: test_base imports base; mid/top/lazy/core's tests are further out
    assert not sel.full and sel.tests == ["tests/test_base.py"]


def test_module_without_direct_tests_selects_the_nearest_transitive_ones(repo):
    _change(repo, "src/wt/agentic/core.py")
    assert _select(repo, max_fraction=1.0).tests == ["tests/test_core.py"]   # in-function import
    _git(repo, "reset", "-q", "--hard", "main")
    (repo / "src/wt/agentic/deep.py").write_text("X = 0\n")
    (repo / "src/wt/agentic/core.py").write_text("from .. import base\nfrom . import deep\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "deep")
    _git(repo, "checkout", "-qb", "feat2")
    _change(repo, "src/wt/agentic/deep.py")           # deep <- core <- test_core
    assert _select(repo, max_fraction=1.0).tests == ["tests/test_core.py"]


def test_transitive_through_test_helpers(repo):
    _change(repo, "src/wt/top.py")
    sel = _select(repo, max_fraction=1.0)
    # test_top imports top; helpers imports top and test_via_helper imports helpers;
    # test_cli reaches top only through the cli hub, which is not walked past.
    assert sel.tests == ["tests/test_top.py", "tests/test_via_helper.py"]


def test_depth_walks_further_src_levels(repo):
    _change(repo, "src/wt/base.py")
    sel = _select(repo, max_fraction=1.0, depth=1)
    assert "tests/test_mid.py" in sel.tests and "tests/test_lazy.py" in sel.tests


def test_changed_test_selects_itself(repo):
    _change(repo, "tests/test_mid.py")
    assert _select(repo, max_fraction=1.0).tests == ["tests/test_mid.py"]


def test_changed_helper_selects_its_importing_tests(repo):
    _change(repo, "tests/helpers.py")
    assert _select(repo, max_fraction=1.0).tests == ["tests/test_via_helper.py"]


def test_docs_only_change_selects_nothing(repo):
    _change(repo, "docs/note.md")
    sel = _select(repo)
    assert not sel.full and sel.tests == []


@pytest.mark.parametrize("rel", ["tests/conftest.py", "tests/waits.py", "pyproject.toml",
                                 "uv.lock", "tools/x.py", ".wt/project.yaml", "src/wt/data.md",
                                 "tests/sub/conftest.py", "tests/data/fixture.json"])
def test_unsafe_changes_fall_back_to_the_full_suite(repo, rel):
    _change(repo, "src/wt/base.py", rel)
    sel = _select(repo, max_fraction=1.0)
    assert sel.full and sel.tests == [], sel.reason


def test_src_module_no_test_reaches_falls_back(repo):
    (repo / "src/wt/orphan.py").write_text("Z = 0\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "orphan")
    sel = _select(repo, max_fraction=1.0)
    assert sel.full and "orphan" in sel.reason


def test_module_reached_only_through_the_hub_falls_back(repo):
    _change(repo, "src/wt/lonely.py")                 # only cli imports it
    sel = _select(repo, max_fraction=1.0)
    assert sel.full and "no test reaches" in sel.reason


def test_removed_module_falls_back(repo):
    _git(repo, "rm", "-q", "src/wt/lonely.py")
    _git(repo, "commit", "-qm", "rm")
    assert _select(repo, max_fraction=1.0).full


def test_selection_over_the_fraction_falls_back(repo):
    _change(repo, "src/wt/base.py")                   # 1 of 7 test files
    assert not _select(repo, max_fraction=0.2).full
    sel = _select(repo, max_fraction=0.1)
    assert sel.full and "> 10%" in sel.reason


def test_unknown_base_falls_back_via_cli(repo, capsys):
    assert AT.main(["--repo", str(repo), "--base", "no-such-ref"]) == 0
    assert capsys.readouterr().out.strip() == "tests"


def _cli(repo, *args):
    return subprocess.run([sys.executable, str(ROOT / "tools" / "affected_tests.py"),
                           "--repo", str(repo), *args], capture_output=True, text=True)


def test_cli_prints_paths_and_runs_with_the_selection_appended(repo):
    _change(repo, "tests/test_mid.py")
    r = _cli(repo)
    assert r.returncode == 0 and r.stdout.split() == ["tests/test_mid.py"]
    assert "affected" in r.stderr
    r = _cli(repo, "--include", "tests/test_base.py", "--run", "echo", "RUN")
    assert r.returncode == 0 and r.stdout.strip() == "RUN tests/test_base.py tests/test_mid.py"


def test_cli_full_suite_runs_the_command_bare_and_empty_runs_nothing(repo):
    _change(repo, "tests/conftest.py")
    r = _cli(repo, "--run", "echo", "RUN")
    assert r.stdout.strip() == "RUN" and "full suite" in r.stderr
    _git(repo, "reset", "-q", "--hard", "main")
    _git(repo, "checkout", "-qB", "feat")
    _change(repo, "docs/note.md")
    r = _cli(repo, "--run", "false")
    assert r.returncode == 0 and r.stdout.strip() == "no affected tests"
