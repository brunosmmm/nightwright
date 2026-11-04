"""SPEC-0328 / SPEC-0357 / SPEC-0378: `tools/affected_tests.py`, static affected-test selection for
loop and close gates.

Every case builds a small temp git repo shaped like nightwright (src/nightwright + tests), branches off main,
changes files, and checks the selection (or the full-suite fallback)."""
import importlib.util
import json
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
    "pyproject.toml": "[project]\nname='nw'\n",
    "src/nightwright/__init__.py": "",
    "src/nightwright/base.py": "X = 1\n",
    "src/nightwright/mid.py": "from . import base\n",
    "src/nightwright/top.py": "from .mid import base\n",
    "src/nightwright/lazy.py": "def f():\n    from nightwright import base\n    return base\n",
    "src/nightwright/lonely.py": "Y = 2\n",
    "src/nightwright/cli.py": "from . import top, lonely\n",          # a hub
    "src/nightwright/data.md": "doc\n",
    "src/nightwright/agentic/__init__.py": "",
    "src/nightwright/agentic/core.py": "from .. import base\n",
    "tests/conftest.py": "",
    "tests/waits.py": "",
    "tests/helpers.py": "from nightwright import top\n",
    "tests/test_base.py": "from nightwright import base\n",
    "tests/test_mid.py": "import nightwright.mid\n",
    "tests/test_top.py": "from nightwright.top import base\n",
    "tests/test_via_helper.py": "from helpers import *\n",
    "tests/test_cli.py": "from nightwright.cli import top\n",
    "tests/test_core.py": "def test():\n    from nightwright.agentic import core\n",
    "tests/test_lazy.py": "from nightwright import lazy\n",
    "docs/note.md": "hi\n",
}


@pytest.fixture(autouse=True)
def _git_env(monkeypatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    # SPEC-0378: the sentinel (which runs this file too) forces the full suite through these
    for k in ("NW_SENTINEL", "NW_CAMPAIGN", "NW_AFFECTED_FULL", "NW_AFFECTED_STRICT", "NW_BASE"):
        monkeypatch.delenv(k, raising=False)


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
    _change(repo, "src/nightwright/base.py")
    _change(repo, "src/nightwright/mid.py", commit=False)
    (repo / "tests" / "test_new.py").write_text("")
    assert AT.changed_files(repo, "main") == ["src/nightwright/base.py", "src/nightwright/mid.py",
                                              "tests/test_new.py"]


def test_changed_files_ignore_main_moving_on(repo):
    _git(repo, "checkout", "-q", "main")
    _change(repo, "src/nightwright/lonely.py")                 # someone else's change on main
    _git(repo, "checkout", "-q", "feat")
    _change(repo, "src/nightwright/top.py")
    assert AT.changed_files(repo, "main") == ["src/nightwright/top.py"]


def test_changed_src_module_selects_its_direct_importers(repo):
    _change(repo, "src/nightwright/base.py")
    sel = _select(repo, max_fraction=1.0)
    # the nearest level only: test_base imports base; mid/top/lazy/core's tests are further out
    assert not sel.full and sel.tests == ["tests/test_base.py"]


def test_module_without_direct_tests_selects_the_nearest_transitive_ones(repo):
    _change(repo, "src/nightwright/agentic/core.py")
    assert _select(repo, max_fraction=1.0).tests == ["tests/test_core.py"]   # in-function import
    _git(repo, "reset", "-q", "--hard", "main")
    (repo / "src/nightwright/agentic/deep.py").write_text("X = 0\n")
    (repo / "src/nightwright/agentic/core.py").write_text("from .. import base\nfrom . import deep\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "deep")
    _git(repo, "checkout", "-qb", "feat2")
    _change(repo, "src/nightwright/agentic/deep.py")           # deep <- core <- test_core
    assert _select(repo, max_fraction=1.0).tests == ["tests/test_core.py"]


def test_transitive_through_test_helpers(repo):
    _change(repo, "src/nightwright/top.py")
    sel = _select(repo, max_fraction=1.0)
    # test_top imports top; helpers imports top and test_via_helper imports helpers;
    # test_cli reaches top only through the cli hub, which is not walked past.
    assert sel.tests == ["tests/test_top.py", "tests/test_via_helper.py"]


def test_depth_walks_further_src_levels(repo):
    _change(repo, "src/nightwright/base.py")
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
                                 "uv.lock", "tools/x.py", ".nw/project.yaml", "src/nightwright/data.md",
                                 "tests/sub/conftest.py", "tests/data/fixture.json"])
def test_unsafe_changes_fall_back_to_the_full_suite_in_strict_mode(repo, rel):
    # the SPEC-0328 behaviour, restored by --strict (SPEC-0357)
    _change(repo, "src/nightwright/base.py", rel)
    sel = _select(repo, max_fraction=1.0, strict=True)
    assert sel.full and sel.tests == [], sel.reason


@pytest.mark.parametrize("rel", ["tests/conftest.py", "uv.lock"])    # root conftest; a lock added
def test_unmappable_changes_still_fall_back_to_the_full_suite(repo, rel):
    _change(repo, "src/nightwright/base.py", rel)
    sel = _select(repo, max_fraction=1.0)
    assert sel.full and sel.tests == [], sel.reason


@pytest.mark.parametrize("rel", ["tools/x.py", ".nw/project.yaml", "src/nightwright/data.md",
                                 "tests/data/fixture.json"])
def test_unreferenced_data_and_tools_fall_back_when_nothing_maps(repo, rel):
    _change(repo, "src/nightwright/base.py", rel)
    sel = _select(repo, max_fraction=1.0)
    assert sel.full and sel.tests == [] and "no test references" in sel.reason, sel.reason


def test_src_module_no_test_reaches_falls_back(repo):
    (repo / "src/nightwright/orphan.py").write_text("Z = 0\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "orphan")
    sel = _select(repo, max_fraction=1.0)
    assert sel.full and "orphan" in sel.reason


def test_module_reached_only_through_the_hub_selects_the_hub_tests(repo):
    # SPEC-0378: was the full suite; now the tests reaching it through the hub
    _change(repo, "src/nightwright/lonely.py")                 # only cli imports it
    sel = _select(repo)
    assert not sel.full and sel.tests == ["tests/test_cli.py"]
    assert "through the hubs for src/nightwright/lonely.py" in sel.reason
    assert _select(repo, strict=True).full                     # strict keeps SPEC-0328


def test_removed_module_selects_the_tests_of_what_still_names_it(repo):
    # SPEC-0378: was the full suite; cli.py still imports lonely, so test_cli
    _git(repo, "rm", "-q", "src/nightwright/lonely.py")
    _git(repo, "commit", "-qm", "rm")
    sel = _select(repo)
    assert not sel.full and sel.tests == ["tests/test_cli.py"], sel.reason
    assert _select(repo, strict=True).full


def test_selection_over_the_fraction_falls_back(repo):
    _change(repo, "src/nightwright/base.py")                   # 1 of 7 test files
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


# --- SPEC-0357: narrowed fallbacks, reference scan, name rule, report and JSON mode ---------------

EXTRA = {
    ".nw/project.yaml": "project: x\n",
    "tests/data/golden.json": "{}\n",
    "tests/data/other.json": "{}\n",
    "tests/fixtures/a.txt": "a\n",
    "tests/test_golden.py": 'from pathlib import Path\nP = Path(__file__).parent / "data" / "golden.json"\n',
    "tests/test_cfg.py": 'def test():\n    open(".nw/project.yaml")\n',
    "tests/test_tool_import.py": "import tool_x\n",
    "tests/test_tool_sub.py": 'import subprocess\nsubprocess.run(["python3", "tools/other.py"])\n',
    "tools/tool_x.py": "T = 1\n",
    "tools/other.py": "O = 1\n",
    "src/nightwright/reader.py": 'from . import base\nNAME = "project.yaml"\n',
    "tests/test_reader.py": "from nightwright import reader\n",
    "src/nightwright/pkg/__init__.py": "",
    "src/nightwright/pkg/data/x.md": "x\n",
    "src/nightwright/pkg/loader.py": 'from pathlib import Path\nP = Path(__file__).parent / "data" / "x.md"\n',
    "tests/test_loader.py": "from nightwright.pkg import loader\n",
    "tests/test_dictkey.py": 'D = {}\nD["data"]\n',
    "tests/test_dirpath.py": 'from pathlib import Path\nP = Path(__file__).parent / "data"\n',
    "tests/test_dirstr.py": 'ROOT = "tests/fixtures"\n',
    "tests/test_agentic_widgets.py": "",
    "tests/test_cowidgets.py": "",
    "tests/test_cmd_only.py": 'X = "cli-gizmos"\n',
    "tests/test_notcmd.py": 'X = "my-gizmos-cmd"\n',
    "tests/test_dot_ref.py": 'X = "nightwright.cli_dotty"\n',
}


@pytest.fixture
def repo2(tmp_path):
    r = tmp_path / "repo2"
    for rel, text in {**FILES, **EXTRA}.items():
        p = r / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    _git(r, "init", "-q", "-b", "main")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "init")
    _git(r, "checkout", "-qb", "feat")
    return r


def _sel2(repo, *rels, **kw):
    _change(repo, *rels)
    kw.setdefault("max_fraction", 1.0)
    return _select(repo, **kw)


def test_project_yaml_change_selects_the_tests_that_name_it(repo2):
    sel = _sel2(repo2, ".nw/project.yaml")
    assert not sel.full and sel.tests == ["tests/test_cfg.py"]
    assert "reference scan for .nw/project.yaml" in sel.reason


def test_src_module_with_the_project_yaml_constant_adds_no_tests(repo2):
    sel = _sel2(repo2, ".nw/project.yaml")
    assert "tests/test_reader.py" not in sel.tests


def test_test_data_change_selects_its_reader_not_the_directory_readers(repo2):
    sel = _sel2(repo2, "tests/data/golden.json")
    assert not sel.full and sel.tests == ["tests/test_golden.py"]    # test_dirpath: dir needle unused


def test_directory_needle_is_path_shaped_and_only_used_without_a_basename_reader(repo2):
    sel = _sel2(repo2, "tests/data/other.json")
    assert sel.tests == ["tests/test_dirpath.py", "tests/test_golden.py"]
    assert "tests/test_dictkey.py" not in sel.tests                  # a dict key "data" is no reference
    _git(repo2, "reset", "-q", "--hard", "main")
    sel = _sel2(repo2, "tests/fixtures/a.txt")                       # constant ending in /fixtures
    assert sel.tests == ["tests/test_dirstr.py"]


def test_tools_change_selects_importers_and_subprocess_path_readers(repo2):
    sel = _sel2(repo2, "tools/tool_x.py")
    assert sel.tests == ["tests/test_tool_import.py"]                # stem import
    assert all(t.startswith("tests/") for t in sel.tests)
    _git(repo2, "reset", "-q", "--hard", "main")
    assert _sel2(repo2, "tools/other.py").tests == ["tests/test_tool_sub.py"]   # "tools/other.py"


def test_tools_modules_are_indexed_under_the_stem_but_are_not_tests(repo2):
    mods, reverse = AT.import_graph(repo2)
    assert mods["tool_x"] == "tools/tool_x.py"
    assert reverse["tool_x"] == {"test_tool_import"}
    assert "tests/test_tool_import.py" in AT.nearest_tests("tool_x", mods, reverse)


def test_package_data_change_selects_the_nearest_tests_of_the_reading_src_module(repo2):
    sel = _sel2(repo2, "src/nightwright/pkg/data/x.md")
    assert sel.tests == ["tests/test_loader.py"]
    assert "reference scan for src/nightwright/pkg/data/x.md" in sel.reason


def test_name_rule_for_a_src_module_no_test_reaches(repo2):
    (repo2 / "src/nightwright/cli_widgets.py").write_text("W = 1\n")
    _git(repo2, "add", "-A")
    _git(repo2, "commit", "-qm", "widgets")
    sel = _select(repo2, max_fraction=1.0)
    assert not sel.full and sel.tests == ["tests/test_agentic_widgets.py"]   # not test_cowidgets
    assert "name rule for src/nightwright/cli_widgets.py" in sel.reason


def test_name_rule_matches_command_names_by_equality_and_dotted_names_by_containment(repo2):
    for mod in ("cli_gizmos", "cli_dotty"):
        (repo2 / f"src/nightwright/{mod}.py").write_text("W = 1\n")
    _git(repo2, "add", "-A")
    _git(repo2, "commit", "-qm", "mods")
    sel = _select(repo2, max_fraction=1.0)
    assert sel.tests == ["tests/test_cmd_only.py", "tests/test_dot_ref.py"]  # test_notcmd only contains


def test_name_rule_miss_falls_back(repo2):
    (repo2 / "src/nightwright/orphan.py").write_text("Z = 0\n")
    _git(repo2, "add", "-A")
    _git(repo2, "commit", "-qm", "orphan")
    sel = _select(repo2, max_fraction=1.0)
    assert sel.full and "no test reaches src/nightwright/orphan.py" in sel.reason


def test_strict_restores_spec_0328_selection(repo2, monkeypatch):
    for rel in (".nw/project.yaml", "tools/tool_x.py", "tests/data/golden.json",
                "src/nightwright/pkg/data/x.md"):
        _git(repo2, "reset", "-q", "--hard", "main")
        assert _sel2(repo2, rel, strict=True).full, rel
    _git(repo2, "reset", "-q", "--hard", "main")
    (repo2 / "src/nightwright/cli_widgets.py").write_text("W = 1\n")
    _git(repo2, "add", "-A")
    _git(repo2, "commit", "-qm", "widgets")
    sel = _select(repo2, max_fraction=1.0, strict=True)
    assert sel.full and "no test reaches" in sel.reason
    _git(repo2, "reset", "-q", "--hard", "main")
    _change(repo2, ".nw/project.yaml")
    r = _cli(repo2)
    assert r.stdout.strip() == "tests/test_cfg.py"
    monkeypatch.setenv("NW_AFFECTED_STRICT", "1")
    assert _cli(repo2).stdout.strip() == "tests"
    monkeypatch.delenv("NW_AFFECTED_STRICT")
    assert _cli(repo2, "--strict").stdout.strip() == "tests"


def test_selection_above_the_fraction_still_falls_back_for_data(repo2):
    sel = _sel2(repo2, "tests/data/other.json", max_fraction=0.05)
    assert sel.full and "> 5%" in sel.reason


def test_smoke_set_is_added_to_every_narrowed_selection(repo2):
    _change(repo2, ".nw/project.yaml")
    r = _cli(repo2, "--include", "tests/test_base.py")
    assert r.stdout.split() == ["tests/test_base.py", "tests/test_cfg.py"]


def test_report_file_has_the_keys_and_does_not_change_output(repo2, tmp_path, monkeypatch):
    _change(repo2, ".nw/project.yaml")
    out = tmp_path / "sub" / "report.json"
    monkeypatch.setenv("NW_GATE_REPORT", str(out))
    r = _cli(repo2, "--include", "tests/test_base.py")
    assert r.returncode == 0 and r.stdout.split() == ["tests/test_base.py", "tests/test_cfg.py"]
    rep = json.loads(out.read_text())
    assert set(rep) == {"full", "reason", "tests", "include", "changed", "base_sha", "head_sha",
                        "head_tree"}
    assert rep["full"] is False and rep["tests"] == ["tests/test_base.py", "tests/test_cfg.py"]
    assert rep["include"] == ["tests/test_base.py"] and rep["changed"] == [".nw/project.yaml"]
    assert rep["head_sha"] == _git(repo2, "rev-parse", "HEAD").strip()
    assert rep["head_tree"] == _git(repo2, "rev-parse", "HEAD^{tree}").strip()
    assert rep["base_sha"] == _git(repo2, "rev-parse", "main").strip()


def test_unwritable_report_path_is_ignored(repo2, tmp_path, monkeypatch):
    _change(repo2, "tests/test_mid.py")
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setenv("NW_GATE_REPORT", str(blocker / "report.json"))   # parent is a file
    r = _cli(repo2)
    assert r.returncode == 0 and r.stdout.split() == ["tests/test_mid.py"]


def test_report_for_full_selection_has_empty_tests(repo2, tmp_path, monkeypatch):
    _change(repo2, "tests/conftest.py")
    out = tmp_path / "r.json"
    monkeypatch.setenv("NW_GATE_REPORT", str(out))
    _cli(repo2)
    rep = json.loads(out.read_text())
    assert rep["full"] is True and rep["tests"] == []


def test_json_prints_one_object_and_closure_lists_what_the_tests_read(repo2):
    _change(repo2, "tests/data/golden.json", "tests/test_tool_import.py")
    r = _cli(repo2, "--json", "--closure")
    assert r.returncode == 0 and len(r.stdout.splitlines()) == 1
    rep = json.loads(r.stdout)
    assert rep["tests"] == ["tests/test_golden.py", "tests/test_tool_import.py"]
    assert {"tests/data/golden.json", "tools/tool_x.py"} <= set(rep["closure"])
    assert rep["closure"] == sorted(rep["closure"])
    assert "closure" not in json.loads(_cli(repo2, "--json").stdout)


def test_closure_follows_imports_transitively_and_is_null_when_full(repo2):
    r = json.loads(_cli(repo2, "--json", "--closure", "--changed", "tests/test_top.py").stdout)
    assert {"tests/test_top.py", "src/nightwright/top.py", "src/nightwright/mid.py",
            "src/nightwright/base.py"} <= set(r["closure"])
    r = json.loads(_cli(repo2, "--json", "--closure", "--changed", "pyproject.toml").stdout)
    assert r["full"] is True and r["closure"] is None and r["tests"] == []


def test_changed_selects_without_resolving_a_base(repo2):
    r = _cli(repo2, "--json", "--base", "no-such-ref", "--changed", ".nw/project.yaml")
    rep = json.loads(r.stdout)
    assert rep["full"] is False and rep["tests"] == ["tests/test_cfg.py"]
    assert rep["base_sha"] is None and rep["changed"] == [".nw/project.yaml"]


def test_unresolvable_base_gives_a_full_report_with_null_base_sha(repo2):
    rep = json.loads(_cli(repo2, "--json", "--base", "no-such-ref").stdout)
    assert rep["full"] is True and "cannot diff against" in rep["reason"] and rep["base_sha"] is None


def test_json_run_puts_the_json_first_and_prints_nothing_else_on_stdout(repo2):
    r = _cli(repo2, "--json", "--changed", ".nw/project.yaml", "--run", "echo", "RUN")
    lines = r.stdout.splitlines()
    assert json.loads(lines[0])["tests"] == ["tests/test_cfg.py"]
    assert lines[1:] == ["RUN tests/test_cfg.py"]
    r = _cli(repo2, "--json", "--changed", "docs/note.md", "--run", "false")
    assert r.returncode == 0 and len(r.stdout.splitlines()) == 1
    assert "no affected tests" in r.stderr and "no affected tests" not in r.stdout


# --- SPEC-0357 review follow-ups: strict parity, --run report, reasons, normalization ----------------

def _commit_new(repo, files):
    for rel, text in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "new")


@pytest.mark.parametrize("strict", [False, True], ids=["default", "strict"])
@pytest.mark.parametrize("change,depth,expected", [
    ("src/nightwright/base.py", 0, ["tests/test_base.py"]),                       # direct importers
    ("src/nightwright/agentic/core.py", 0, ["tests/test_core.py"]),               # lazy import
    ("src/nightwright/top.py", 0, ["tests/test_top.py", "tests/test_via_helper.py"]),   # helper hop, hub stop
    ("src/nightwright/base.py", 1, ["tests/test_base.py", "tests/test_core.py", "tests/test_lazy.py",
                                    "tests/test_mid.py", "tests/test_reader.py"]),     # depth
    ("tests/helpers.py", 0, ["tests/test_via_helper.py"]),
    ("tests/test_mid.py", 0, ["tests/test_mid.py"]),
    ("docs/note.md", 0, []),
])
def test_spec_0328_selection_is_identical_in_strict_and_default_mode(repo2, strict, change, depth, expected):
    _change(repo2, change)
    sel = _select(repo2, max_fraction=1.0, depth=depth, strict=strict)
    assert not sel.full and sel.tests == expected, sel.reason


@pytest.mark.parametrize("rel", ["src/nightwright/lonely.py", "src/nightwright/agentic/core.py"])
def test_removed_and_hub_only_modules_are_full_in_strict_mode_only(repo2, rel):
    if rel.endswith("core.py"):
        _git(repo2, "rm", "-q", rel)
        _git(repo2, "commit", "-qm", "rm")
    else:
        _change(repo2, rel)                                        # reached only through the hub
    assert _select(repo2, max_fraction=1.0, strict=True).full
    assert not _select(repo2).full                                 # SPEC-0378 default


@pytest.mark.parametrize("strict", [False, True], ids=["default", "strict"])
def test_fraction_cap_applies_in_both_modes(repo2, strict):
    _change(repo2, "src/nightwright/base.py")
    assert _select(repo2, max_fraction=0.01, strict=strict).full


def test_reasons_name_the_rule_for_test_data_and_tools(repo2):
    assert "reference scan for tests/data/golden.json" in _sel2(repo2, "tests/data/golden.json").reason
    _git(repo2, "reset", "-q", "--hard", "main")
    assert "reference scan for tools/tool_x.py" in _sel2(repo2, "tools/tool_x.py").reason


def test_reason_joins_every_rule_that_fired(repo2):
    _commit_new(repo2, {"src/nightwright/cli_widgets.py": "W = 1\n"})
    sel = _sel2(repo2, ".nw/project.yaml")
    assert not sel.full
    assert sel.reason.endswith("(reference scan for .nw/project.yaml; "
                               "name rule for src/nightwright/cli_widgets.py)")
    assert sel.tests == ["tests/test_agentic_widgets.py", "tests/test_cfg.py"]


def test_removed_tools_module_selects_the_tests_that_name_it(repo2):
    _git(repo2, "rm", "-q", "tools/tool_x.py")
    _git(repo2, "commit", "-qm", "rm")
    sel = _select(repo2)
    assert not sel.full and sel.tests == ["tests/test_tool_import.py"], sel.reason


def test_one_mapped_plus_one_unmapped_file_is_full(repo2):
    sel = _sel2(repo2, "tests/data/golden.json", ".nw/unref.yaml")
    assert sel.full and "no test references .nw/unref.yaml" in sel.reason


def test_name_rule_strips_a_leading_underscore(repo2):
    _commit_new(repo2, {"src/nightwright/_gadget.py": "G = 1\n", "tests/test_gadget_stuff.py": ""})
    sel = _select(repo2, max_fraction=1.0)
    assert sel.tests == ["tests/test_gadget_stuff.py"]
    assert "name rule for src/nightwright/_gadget.py" in sel.reason


def test_a_tests_module_wins_over_a_tools_module_of_the_same_stem(repo2):
    _commit_new(repo2, {"tests/tool_x.py": "S = 1\n"})
    mods, _ = AT.import_graph(repo2)
    assert mods["tool_x"] == "tests/tool_x.py"
    assert "tools/tool_x.py" not in mods.values()


def test_package_data_reader_no_test_reaches_is_not_dropped(repo2):
    # loader (tested) and orph2 (no test reaches it, no name match) both read x.md
    _commit_new(repo2, {"src/nightwright/orph2.py": 'NAME = "x.md"\n'})
    sel = _sel2(repo2, "src/nightwright/pkg/data/x.md")
    assert sel.full and "no test reaches src/nightwright/orph2.py" in sel.reason
    _git(repo2, "reset", "-q", "--hard", "main")
    _commit_new(repo2, {"src/nightwright/orph2.py": 'NAME = "x.md"\n', "tests/test_orph2.py": ""})
    sel = _sel2(repo2, "src/nightwright/pkg/data/x.md")                 # name rule rescues the reader
    assert not sel.full and sel.tests == ["tests/test_loader.py", "tests/test_orph2.py"]


def test_changed_paths_are_normalized(repo2):
    assert AT.normalize_changed(repo2, ["./pyproject.toml", "a//b.py", "", " ", ".",
                                        str(repo2 / "uv.lock"), "./x/../y.md"]) == [
        "a/b.py", "pyproject.toml", "uv.lock", "y.md"]
    for path in ("./pyproject.toml", str(repo2 / "pyproject.toml"), "./uv.lock"):
        rep = json.loads(_cli(repo2, "--json", "--changed", path).stdout)
        assert rep["full"] is True and rep["changed"] in (["pyproject.toml"], ["uv.lock"]), path
    r = _cli(repo2, "--changed", "./pyproject.toml", "--run", "echo", "RUN")
    assert r.stdout.strip() == "RUN"                                   # the full suite, not "no affected tests"


def test_empty_changed_value_does_not_crash(repo2):
    r = _cli(repo2, "--json", "--changed", "")
    assert r.returncode == 0 and json.loads(r.stdout)["changed"] == []
    sel = AT.select(repo2, [""], max_fraction=1.0)                     # direct callers too
    assert not sel.full and sel.tests == []
    assert not AT.select(repo2, [""], strict=True).full
    r = _cli(repo2, "--json", "--changed", "", "--changed", ".nw/project.yaml")
    assert json.loads(r.stdout)["tests"] == ["tests/test_cfg.py"]


@pytest.mark.parametrize("change,include,full,tests,stdout", [
    ("tests/test_mid.py", ["tests/test_base.py"], False, ["tests/test_base.py", "tests/test_mid.py"],
     "RUN tests/test_base.py tests/test_mid.py"),                      # narrowed (+ smoke set)
    ("docs/note.md", [], False, [], "no affected tests"),              # empty selection
    ("tests/conftest.py", ["tests/test_base.py"], True, [], "RUN"),    # full suite
])
def test_report_is_written_on_the_run_path(repo2, tmp_path, monkeypatch, change, include, full, tests,
                                           stdout):
    _change(repo2, change)
    out = tmp_path / "gate" / "report.json"
    args = [a for i in include for a in ("--include", i)] + ["--run", "echo", "RUN"]
    base = _cli(repo2, *args)
    monkeypatch.setenv("NW_GATE_REPORT", str(out))
    r = _cli(repo2, *args)
    assert (r.returncode, r.stdout) == (base.returncode, base.stdout)       # report changes nothing
    assert r.returncode == 0 and r.stdout.strip() == stdout
    rep = json.loads(out.read_text())
    assert set(rep) == {"full", "reason", "tests", "include", "changed", "base_sha", "head_sha",
                        "head_tree"}
    assert rep["full"] is full and rep["tests"] == tests and rep["include"] == include
    assert rep["changed"] == [change]


# --- SPEC-0357 review loop 2: closure precision/width, tools hops, smoke on empty, newline paths ---

def _closure(repo, *changed):
    return json.loads(_cli(repo, "--json", "--closure", "--changed", *changed).stdout)["closure"]


def test_closure_does_not_include_files_the_tests_do_not_read(repo2):
    clo = _closure(repo2, "tests/data/golden.json")
    assert {"tests/test_golden.py", "tests/data/golden.json"} <= set(clo)
    # (a same-named `data` directory elsewhere is a known over-inclusion of the directory needle)
    for unread in (".nw/project.yaml", "tests/test_cfg.py", "tools/other.py",
                   "src/nightwright/base.py", "tests/fixtures/a.txt"):
        assert unread not in clo, unread


def test_closure_reaches_a_data_file_through_a_src_closure_module(repo2):
    clo = _closure(repo2, "tests/test_loader.py")
    assert {"tests/test_loader.py", "src/nightwright/pkg/loader.py",
            "src/nightwright/pkg/data/x.md"} <= set(clo)
    assert ".nw/project.yaml" not in clo and "tests/fixtures/a.txt" not in clo


def test_closure_includes_directory_readers_even_when_another_module_names_the_basename(repo2):
    # test_golden names golden.json, test_dirpath only has Path(__file__).parent / "data"
    clo = _closure(repo2, "tests/test_dirpath.py")
    assert {"tests/data/golden.json", "tests/data/other.json"} <= set(clo)
    assert "tests/fixtures/a.txt" not in clo


def test_closure_includes_tools_run_by_subprocess_and_their_imports(repo2):
    _commit_new(repo2, {"tools/other.py": "import tool_x\n"})
    clo = _closure(repo2, "tests/test_tool_sub.py")
    assert {"tools/other.py", "tools/tool_x.py"} <= set(clo)
    assert "tests/test_tool_import.py" not in clo


def test_tools_stem_shadowed_by_a_tests_module_still_maps_importers_and_closure(repo2):
    _commit_new(repo2, {"tests/tool_x.py": "S = 1\n"})                  # shadows tools/tool_x.py
    sel = _sel2(repo2, "tools/tool_x.py")
    assert not sel.full and sel.tests == ["tests/test_tool_import.py"]
    assert "tools/tool_x.py" in _closure(repo2, "tests/test_tool_import.py")


def test_nearest_tests_does_not_walk_past_a_tools_module(repo2):
    # tools/t.py imports a src module and a test imports t: changing base.py must not select
    # that test through the tool
    _commit_new(repo2, {"tools/t.py": "from nightwright import base\n",
                        "tests/test_via_tool.py": "import t\n"})
    mods, reverse = AT.import_graph(repo2)
    assert "t" in reverse["nightwright.base"] and "test_via_tool" in reverse["t"]
    assert "tests/test_via_tool.py" not in AT.nearest_tests("nightwright.base", mods, reverse, depth=3)
    sel = AT.select(repo2, ["src/nightwright/base.py"], max_fraction=1.0, depth=3)
    assert "tests/test_via_tool.py" not in sel.tests and "tests/test_base.py" in sel.tests


def test_smoke_set_runs_when_the_branch_maps_to_nothing(repo2):
    _change(repo2, "docs/note.md")
    r = _cli(repo2, "--include", "tests/test_base.py", "--run", "echo", "RUN")
    assert r.returncode == 0 and r.stdout.strip() == "RUN tests/test_base.py"
    assert "no affected tests" not in r.stdout


def test_newline_joined_changed_paths_are_split(repo2):
    assert AT.normalize_changed(repo2, ["docs/note.md\npyproject.toml\n", "uv.lock"]) == [
        "docs/note.md", "pyproject.toml", "uv.lock"]
    rep = json.loads(_cli(repo2, "--json", "--changed", "docs/note.md\npyproject.toml").stdout)
    assert rep["full"] is True and rep["changed"] == ["docs/note.md", "pyproject.toml"]
    r = _cli(repo2, "--changed", ".nw/project.yaml\ndocs/note.md", "--run", "echo", "RUN")
    assert r.stdout.strip() == "RUN tests/test_cfg.py"


# --- SPEC-0378: the close gate runs the selection; narrowed fallbacks ----------------------------------

def _rewrite(repo, rel, text):
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", f"rewrite {rel}")


def _sel_base(repo, **kw):
    return AT.select(repo, AT.changed_files(repo, "main"), base=AT._merge_base(repo, "main"), **kw)


def test_no_fraction_cap_by_default(repo2):
    # the 40% cap made 29 of the last 200 main commits run the full suite; it is gone
    _commit_new(repo2, {f"tests/test_x{i}.py": "" for i in range(30)})
    sel = _select(repo2)
    assert not sel.full and len(sel.tests) == 30
    assert _select(repo2, strict=True).full                    # strict keeps MAX_FRACTION
    assert _select(repo2, max_fraction=0.1).full               # an explicit cap still applies


def test_nested_conftest_selects_the_tests_below_it(repo):
    _commit_new(repo, {"tests/sub/__init__.py": "", "tests/sub/test_a.py": "", "tests/sub/deep/test_b.py": ""})
    _git(repo, "branch", "-f", "main", "HEAD")
    _change(repo, "tests/sub/conftest.py")
    sel = _select(repo)
    assert not sel.full and sel.tests == ["tests/sub/deep/test_b.py", "tests/sub/test_a.py"]
    assert _select(repo, strict=True).full


def test_waits_selects_its_importers_not_the_suite(repo):
    _commit_new(repo, {"tests/test_waiter.py": "from waits import wait_for\n"})
    _git(repo, "branch", "-f", "main", "HEAD")
    _change(repo, "tests/waits.py")
    sel = _select(repo)
    assert not sel.full and sel.tests == ["tests/test_waiter.py"]


PYPROJECT = """[project]
name = "nw"
version = "1.0"
description = "d"
dependencies = ["a>=1"]

[tool.ruff]
line-length = 100

[tool.pytest.ini_options]
addopts = "-q"
"""


@pytest.mark.parametrize("old,new,full", [
    ('version = "1.0"', 'version = "1.1"', False),
    ('description = "d"', 'description = "e"', False),
    ("line-length = 100", "line-length = 110", False),               # [tool.ruff]
    ('dependencies = ["a>=1"]', 'dependencies = ["a>=2"]', True),
    ('addopts = "-q"', 'addopts = "-x"', True),                     # [tool.pytest]
    ('[project]', '[build-system]\nrequires = ["x"]\n\n[project]', True),
    ('name = "nw"', 'name = "nw"\n[[[', True),                       # unparsable
])
def test_pyproject_metadata_only_changes_select_nothing(repo, old, new, full):
    _rewrite(repo, "pyproject.toml", PYPROJECT)
    _git(repo, "branch", "-f", "main", "HEAD")
    _rewrite(repo, "pyproject.toml", PYPROJECT.replace(old, new, 1))
    sel = _sel_base(repo)
    assert sel.full is full, sel.reason
    if not full:
        assert sel.tests == [] and "metadata only" in sel.reason
    assert _select(repo, strict=True).full                     # strict: any pyproject change


LOCK = """version = 1

[[package]]
name = "nw"
version = "1.0"
source = { editable = "." }

[[package]]
name = "dep"
version = "2.0"
"""


@pytest.mark.parametrize("old,new,full", [
    ('name = "nw"\nversion = "1.0"', 'name = "nw"\nversion = "1.1"', False),   # own version bump
    ('name = "dep"\nversion = "2.0"', 'name = "dep"\nversion = "2.1"', True),  # a dependency moved
])
def test_uv_lock_own_version_bump_selects_nothing(repo, old, new, full):
    _rewrite(repo, "pyproject.toml", PYPROJECT)
    _rewrite(repo, "uv.lock", LOCK)
    _git(repo, "branch", "-f", "main", "HEAD")
    _rewrite(repo, "uv.lock", LOCK.replace(old, new, 1))
    sel = _sel_base(repo)
    assert sel.full is full, sel.reason


def test_config_change_without_a_base_is_full(repo):
    sel = AT.select(repo, ["pyproject.toml"])
    assert sel.full and "no base" in sel.reason


def test_removed_module_nothing_names_selects_nothing(repo):
    _git(repo, "rm", "-q", "src/nightwright/agentic/core.py", "tests/test_core.py")
    _git(repo, "commit", "-qm", "rm")
    sel = _select(repo)
    assert not sel.full and sel.tests == [], sel.reason


def test_removed_module_named_by_an_untested_src_module_is_full(repo):
    _commit_new(repo, {"src/nightwright/gone.py": "G = 1\n", "src/nightwright/orphan.py": "from . import gone\n"})
    _git(repo, "branch", "-f", "main", "HEAD")
    _git(repo, "rm", "-q", "src/nightwright/gone.py")
    _git(repo, "commit", "-qm", "rm")
    sel = _select(repo)
    assert sel.full and "reaches no test" in sel.reason


def test_unmapped_root_file_selects_the_tests_that_name_it(repo):
    _commit_new(repo, {"AGENTS.md": "contract\n", "tests/test_agents_doc.py": 'P = "AGENTS.md"\n'})
    _git(repo, "branch", "-f", "main", "HEAD")
    _change(repo, "AGENTS.md")
    sel = _select(repo)
    assert not sel.full and sel.tests == ["tests/test_agents_doc.py"]
    assert "reference scan for AGENTS.md" in sel.reason


@pytest.mark.parametrize("env", [{"NW_AFFECTED_FULL": "1"}, {"NW_SENTINEL": "Meta-Tools"},
                                 {"NW_CAMPAIGN": "sentinel-Meta-Tools"}])
def test_sentinel_environment_forces_the_full_suite(repo, monkeypatch, env):
    _change(repo, "tests/test_mid.py")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    r = _cli(repo, "--run", "echo", "RUN")
    assert r.returncode == 0 and r.stdout.strip() == "RUN" and "full suite" in r.stderr
    assert AT.forced_full()


@pytest.mark.parametrize("env", [{}, {"NW_AFFECTED_FULL": "0"}, {"NW_CAMPAIGN": "Meta-Tools-0042-x"},
                                 {"NW_SENTINEL": " "}])
def test_campaign_environment_does_not_force_the_full_suite(repo, monkeypatch, env):
    _change(repo, "tests/test_mid.py")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    r = _cli(repo, "--run", "echo", "RUN")
    assert r.stdout.strip() == "RUN tests/test_mid.py"
    assert AT.forced_full() is None


# --- SPEC-0421: tree guards (`--guards`) and the tree-reader rule ---------------------------------

GUARD_FILES = {
    "tests/test_guard_one.py": ("import pytest\n\npytestmark = pytest.mark.tree_guard\n\n"
                                "def test_x():\n    pass\n"),
    "tests/test_guard_list.py": ("import pytest\n\npytestmark = [pytest.mark.slow, pytest.mark.tree_guard]\n"),
    "tests/test_guard_tuple.py": "import pytest\n\npytestmark = (pytest.mark.tree_guard,)\n",
    "tests/test_not_guard.py": ("import pytest\n\npytestmark = pytest.mark.slow\n\n"
                                "@pytest.mark.tree_guard\ndef test_x():\n    pass\n"),
    "tests/test_tests_reader.py": ("from pathlib import Path\n\nTESTS = Path(__file__).parent\n"
                                   "FILES = sorted(TESTS.glob('test_*.py'))\n"),
    "tests/test_root_reader.py": ("from pathlib import Path\n\nROOT = Path(__file__).resolve().parents[1]\n"
                                  "SRC = ROOT / 'src' / 'nightwright'\n"
                                  "def f():\n    return list(SRC.rglob('*.py'))\n"),
    "tests/test_walk_reader.py": ("import os\nfrom pathlib import Path\n\n"
                                  "SRC = Path(__file__).parent.parent / 'src' / 'nightwright'\n"
                                  "def f():\n    return list(os.walk(SRC))\n"),
    "tests/test_tmp_glob.py": ("def test_x(tmp_path):\n    assert not list(tmp_path.glob('*.py'))\n"
                               "    assert not list(tmp_path.rglob('*'))\n"),
    "tests/test_sub_glob.py": ("from pathlib import Path\n\nD = Path(__file__).parent / 'data'\n"
                               "X = list(D.glob('*.json'))\n"),
}


@pytest.fixture
def grepo(tmp_path):
    r = tmp_path / "repo"
    for rel, text in {**FILES, **GUARD_FILES}.items():
        p = r / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    _git(r, "init", "-q", "-b", "main")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "init")
    _git(r, "checkout", "-qb", "feat")
    return r


GUARDS = ["tests/test_guard_list.py", "tests/test_guard_one.py", "tests/test_guard_tuple.py"]


def test_guards_finds_single_list_and_tuple_marks_and_ignores_a_function_mark(grepo):
    assert AT._guards(AT.Index(grepo)) == GUARDS


def test_tree_roots_detects_scans_of_the_tests_and_src_trees_only(grepo):
    ix = AT.Index(grepo)
    roots = {rel: ix.info[m].tree_roots for m, rel in ix.mods.items() if rel.startswith("tests/test_")}
    assert roots["tests/test_tests_reader.py"] == {"tests"}
    assert roots["tests/test_root_reader.py"] == {"src/nightwright"}
    assert roots["tests/test_walk_reader.py"] == {"src/nightwright"}
    assert roots["tests/test_tmp_glob.py"] == set()           # tmp_path is not the tree
    assert roots["tests/test_sub_glob.py"] == set()           # a subdirectory is not a root
    assert roots["tests/test_base.py"] == set()


def test_guards_flag_adds_every_guard_to_a_docs_only_selection(grepo):
    _change(grepo, "docs/note.md")
    plain = _cli(grepo, "--json")
    rep = json.loads(plain.stdout)
    assert rep["tests"] == [] and "guards" not in rep
    r = _cli(grepo, "--guards", "--json")
    rep = json.loads(r.stdout)
    assert rep["guards"] == GUARDS and rep["tests"] == GUARDS
    assert "+3 guards" in r.stderr


def test_guards_flag_runs_the_command_for_an_empty_selection_but_not_a_full_one(grepo):
    _change(grepo, "docs/note.md")
    r = _cli(grepo, "--run", "echo", "RUN")
    assert r.stdout.strip() == "no affected tests"
    r = _cli(grepo, "--guards", "--run", "echo", "RUN")
    assert r.stdout.strip() == "RUN " + " ".join(GUARDS)
    _git(grepo, "reset", "-q", "--hard", "main")
    _git(grepo, "checkout", "-qB", "feat")
    _change(grepo, "tests/conftest.py")                      # full suite: nothing appended
    r = _cli(grepo, "--guards", "--run", "echo", "RUN")
    assert r.stdout.strip() == "RUN" and "full suite" in r.stderr and "guards" not in r.stderr


def test_guards_come_after_include_and_are_deduplicated(grepo):
    _change(grepo, "tests/test_guard_one.py")
    r = _cli(grepo, "--guards", "--include", "tests/test_base.py", "--run", "echo", "RUN")
    out = r.stdout.split()
    assert out[0] == "RUN" and "tests/test_base.py" in out
    assert out.count("tests/test_guard_one.py") == 1


def test_added_test_file_selects_the_tests_that_scan_tests(grepo):
    (grepo / "tests" / "test_new.py").write_text("def test_x():\n    pass\n")
    sel = _select(grepo)
    assert {"tests/test_new.py", "tests/test_tests_reader.py"} <= set(sel.tests)
    assert "tests/test_walk_reader.py" not in sel.tests       # scans src, not tests
    assert "tests/test_tmp_glob.py" not in sel.tests
    assert any("tree readers of tests for tests/test_new.py" in s for s in sel.reason.split("; "))


def test_changed_src_file_selects_the_tests_that_scan_src(grepo):
    _change(grepo, "src/nightwright/lonely.py")
    sel = _select(grepo)
    assert {"tests/test_root_reader.py", "tests/test_walk_reader.py"} <= set(sel.tests)
    assert "tests/test_tests_reader.py" not in sel.tests


def test_tree_reader_rule_is_off_in_strict_mode(grepo):
    (grepo / "tests" / "test_new.py").write_text("def test_x():\n    pass\n")
    sel = _select(grepo, strict=True, max_fraction=1.0)
    assert sel.tests == ["tests/test_new.py"]
