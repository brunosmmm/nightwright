"""SPEC-0490: the tier guard over the real tree, and `tools/test_tiers.py` on temp trees."""
from __future__ import annotations

import importlib.util
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

pytestmark = [pytest.mark.fast, pytest.mark.tree_guard]  # SPEC-0421: scans the tests tree

TESTS = Path(__file__).parent
ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "test_tiers.py"
_spec = importlib.util.spec_from_file_location("test_tiers_tool", TOOL)
TT = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(TT)

PYPROJECT = ("[project]\nname='x'\n[tool.pytest.ini_options]\naddopts = ['--strict-markers']\n"
             "markers = ['fast: f', 'integration: i', 'tui: t', 'remote: r', 'perf: p',\n"
             "  'tree_guard: g']\n")


def _tool(*args):
    return subprocess.run([sys.executable, str(TOOL), *map(str, args)], capture_output=True,
                          text=True)


def _tree(tmp_path, files: dict[str, str]) -> Path:
    r = tmp_path / "repo"
    (r / "tests").mkdir(parents=True)
    (r / "pyproject.toml").write_text(PYPROJECT)
    for name, body in files.items():
        (r / "tests" / name).write_text(textwrap.dedent(body))
    return r


# --- the guard over the real tree --------------------------------------------------------------

def test_every_real_test_module_has_exactly_one_tier():
    assert sorted(TESTS.glob("test_*.py"))
    r = _tool("--check", "--repo", ROOT)
    assert r.returncode == 0, r.stdout + r.stderr


def test_real_tree_slow_marks_are_gone_but_the_fixture_text_stays():
    assert "pytest.mark.slow" in (TESTS / "test_affected_tests.py").read_text()   # GUARD_FILES
    assert "mark.slow" not in (TESTS / "test_gate_timeout_tree.py").read_text()
    pyproject = (ROOT / "pyproject.toml").read_text()
    for tier in TT.TIERS:
        assert f'"{tier}:' in pyproject
    assert '"slow:' not in pyproject and "--strict-markers" in pyproject


def test_real_tree_taxonomy():
    tiers = {rel: TT.taxonomy_tier(rel, TT._parse_file(ROOT, rel)) for rel in TT.module_paths(ROOT)}
    assert len(tiers) == len(set(tiers))
    name = {Path(rel).name: t for rel, t in tiers.items()}
    assert [n for n, t in name.items() if t == "perf"] == ["test_tui_budget.py"]
    for n in ("test_herdr.py", "test_agentic_gates.py", "test_pull_workers_security.py"):
        assert name[n] != "perf", n
    for n, t in name.items():
        if n.startswith("test_tui_") and n != "test_tui_budget.py":
            assert t == "tui", n
        if n.startswith(("test_remote_", "test_pull_worker")):
            assert t == "remote", n
        if Path(n).stem.endswith(("_e2e", "_integration", "_race")) and \
                not n.startswith(("test_remote_", "test_tui_")):
            assert t == "integration", n
        if ".run_test(" in (TESTS / n).read_text() and n not in ("test_tui_budget.py",
                                                                 Path(__file__).name):
            assert t == "tui", n
    for n in ("test_idea_project.py", "test_clock_json_parity.py", "test_question_rationale.py"):
        assert name[n] == "tui", n
    assert name["test_remote_workers_integration.py"] == "remote"
    assert name["test_dashboard_e2e.py"] == "integration"
    assert name["test_gate_timeout_tree.py"] == "integration"


def test_real_tree_marks_match_the_taxonomy_for_the_named_tiers():
    """A module the taxonomy puts in tui/remote/perf carries that tier mark."""
    for rel in TT.module_paths(ROOT):
        tier = TT.taxonomy_tier(rel, TT._parse_file(ROOT, rel))
        if tier in ("tui", "remote", "perf"):
            assert f"pytest.mark.{tier}" in (ROOT / rel).read_text(), rel


# --- --check ----------------------------------------------------------------------------------

@pytest.mark.parametrize("body, why", [
    ("def test_a():\n    pass\n", "no tier"),
    ("import pytest\npytestmark = [pytest.mark.fast, pytest.mark.tui]\n", "more than one tier"),
    ("import pytest\npytestmark = pytest.mark.quick\n", "unknown tier"),
    ("import pytest\npytestmark = pytest.mark.fast\n@pytest.mark.slow\ndef test_a():\n    pass\n",
     "pytest.mark.slow"),
])
def test_check_fails_on_each_bad_shape(tmp_path, body, why):
    r = _tree(tmp_path, {"test_bad.py": body, "test_ok.py": "import pytest\npytestmark = pytest.mark.fast\n"})
    out = _tool("--check", "--repo", r)
    assert out.returncode == 1 and why in out.stdout and "tests/test_bad.py" in out.stdout
    assert "test_ok.py" not in out.stdout


def test_check_passes_helpers_strings_and_non_tier_marks(tmp_path):
    r = _tree(tmp_path, {
        "helpers.py": "X = 1\n",                                      # not test_*.py: exempt
        "test_str.py": ('import pytest\npytestmark = [pytest.mark.fast, pytest.mark.skipif(False, reason="r")]\n'
                        'S = "pytestmark = pytest.mark.slow"\n'),
        "test_guard.py": "import pytest\npytestmark = (pytest.mark.tree_guard, pytest.mark.remote)\n",
    })
    out = _tool("--check", "--repo", r)
    assert out.returncode == 0, out.stdout


# --- durations and the budget ---------------------------------------------------------------

def test_durations_log_sums_phases_per_test():
    d = TT.parse_durations_log(textwrap.dedent("""\
        ============================= slowest durations ==============================
        1.50s call     tests/test_a.py::test_x[p1]
        0.60s setup    tests/test_a.py::test_x[p1]
        0.10s teardown tests/test_a.py::test_y
        (3 durations < 0.005s hidden.)
        5 passed in 3.0s
        """))
    assert d.per_test == {"tests/test_a.py::test_x[p1]": 2.1, "tests/test_a.py::test_y": 0.1}
    assert d.module_sum["tests/test_a.py"] == pytest.approx(2.2)


def test_budget_20s_per_module_and_2s_per_test(tmp_path):
    r = _tree(tmp_path, {"test_mod.py": "import pytest\npytestmark = pytest.mark.fast\n"})
    tree = TT._parse_file(r, "tests/test_mod.py")
    log = "3.00s call tests/test_mod.py::test_slow[a]\n2.00s call tests/test_mod.py::test_edge\n"
    tier, ov = TT.classify_module("tests/test_mod.py", tree, TT.parse_durations_log(log))
    assert tier == "fast" and set(ov) == {"tests/test_mod.py::test_slow[a]"}
    log = "".join(f"1.90s call tests/test_mod.py::test_{i}\n" for i in range(11))   # 20.9 s
    tier, ov = TT.classify_module("tests/test_mod.py", tree, TT.parse_durations_log(log))
    assert tier == "integration" and ov == {}
    log = "".join(f"2.00s call tests/test_mod.py::test_{i}\n" for i in range(10))    # exactly 20
    assert TT.classify_module("tests/test_mod.py", tree, TT.parse_durations_log(log))[0] == "fast"


def test_precedence_order(tmp_path):
    r = _tree(tmp_path, {
        "test_tui_budget.py": "import tui_harness\n",                  # explicit wins: perf
        "test_gate_timeout_tree.py": "def test_a():\n    pass\n",       # explicit: integration
        "test_tui_x_race.py": "",                                      # tui before integration
        "test_pilot.py": "async def test_a(app):\n    await app.run_test()\n",
        "test_rem.py": "from remote_loopback import x\n",
        "test_remote_x_integration.py": "",                            # remote before integration
        "test_pull_workers_x.py": "",
        "test_tuiish_remote.py": "import remote_support\nimport tui_harness\n",   # tui first
        "test_foo_race.py": "",
        "test_bar_e2e.py": "",
        "test_plain.py": "import time\nt = time.monotonic() - time.perf_counter()\n",
    })
    got = {rel.split("/")[1]: TT.taxonomy_tier(rel, TT._parse_file(r, rel))
           for rel in TT.module_paths(r)}
    assert got == {"test_tui_budget.py": "perf", "test_gate_timeout_tree.py": "integration",
                   "test_tui_x_race.py": "tui", "test_pilot.py": "tui", "test_rem.py": "remote",
                   "test_remote_x_integration.py": "remote", "test_pull_workers_x.py": "remote",
                   "test_tuiish_remote.py": "tui", "test_foo_race.py": "integration",
                   "test_bar_e2e.py": "integration", "test_plain.py": "fast"}


def test_classify_prints_one_line_per_module_and_the_overrides(tmp_path):
    r = _tree(tmp_path, {"test_a.py": "def test_x():\n    pass\n", "test_b_race.py": ""})
    log = tmp_path / "d.log"
    log.write_text("2.50s call tests/test_a.py::test_x\n")
    out = _tool("--classify", log, "--repo", r)
    assert out.returncode == 0
    assert out.stdout.splitlines() == ["fast\ttests/test_a.py",
                                       "  integration\ttests/test_a.py::test_x\t2.50s",
                                       "integration\ttests/test_b_race.py"]
    assert _tool("--classify", tmp_path / "nope.log", "--repo", r).returncode == 2


# --- --apply ----------------------------------------------------------------------------------

SHAPES = {
    "test_skip.py": """\
        import pytest

        pytestmark = pytest.mark.skipif(False, reason="x")


        def test_one():
            pass
        """,
    "test_guard.py": """\
        import pytest

        pytestmark = pytest.mark.tree_guard


        def test_one():
            pass
        """,
    "test_list.py": """\
        import pytest

        pytestmark = [pytest.mark.slow, pytest.mark.usefixtures("tmp_path"), pytest.mark.tree_guard]


        def test_one():
            pass
        """,
    "test_late.py": """\
        \"\"\"Doc.\"\"\"
        import pytest

        X = 1

        pytestmark = (pytest.mark.skipif(False, reason="y"),)


        def test_one():
            pass
        """,
    "test_slow_fn.py": """\
        import pytest


        @pytest.mark.slow
        def test_one():
            pass


        class TestK:
            @pytest.mark.slow
            def test_two(self):
                pass
        """,
    "test_heavy_slow_race.py": """\
        import pytest


        @pytest.mark.slow
        def test_one():
            pass
        """,
    "test_bare.py": """\
        from pathlib import Path


        def test_one():
            assert Path(".")
        """,
    "test_over.py": """\
        import pytest


        @pytest.mark.parametrize("n", [1, 2])
        def test_big(n):
            pass


        def test_small():
            pass
        """,
}


def test_apply_rewrites_every_shape_and_is_idempotent(tmp_path):
    r = _tree(tmp_path, SHAPES)
    log = tmp_path / "d.log"
    log.write_text("1.00s call tests/test_over.py::test_big[1]\n"
                   "2.50s call tests/test_over.py::test_big[2]\n"
                   "0.01s call tests/test_over.py::test_small\n")
    assert _tool("--apply", log, "--repo", r).returncode == 0
    text = {n: (r / "tests" / n).read_text() for n in SHAPES}
    assert 'pytestmark = [pytest.mark.fast, pytest.mark.skipif(False, reason=\'x\')]' in text["test_skip.py"]
    assert "pytestmark = [pytest.mark.fast, pytest.mark.tree_guard]" in text["test_guard.py"]
    assert ("pytestmark = [pytest.mark.fast, pytest.mark.usefixtures('tmp_path'), "
            "pytest.mark.tree_guard]") in text["test_list.py"]
    assert "pytestmark = [pytest.mark.fast, pytest.mark.skipif(False, reason='y')]" in text["test_late.py"]
    assert text["test_late.py"].index("X = 1") < text["test_late.py"].index("pytestmark")
    assert "@pytest.mark.integration\ndef test_one" in text["test_slow_fn.py"]
    assert "    @pytest.mark.integration\n    def test_two" in text["test_slow_fn.py"]
    assert "pytestmark = pytest.mark.integration" in text["test_heavy_slow_race.py"]
    assert "@pytest.mark" not in text["test_heavy_slow_race.py"].replace("pytestmark = pytest.mark", "")
    assert "import pytest" in text["test_bare.py"] and "pytestmark = pytest.mark.fast" in text["test_bare.py"]
    assert ('@pytest.mark.parametrize("n", [1, 2])\n@pytest.mark.integration\ndef test_big'
            in text["test_over.py"])
    assert text["test_over.py"].count("pytest.mark.integration") == 1
    for n, t in text.items():
        assert "mark.slow" not in t, n
        assert t.endswith("\n") and not t.endswith("\n\n"), n
    assert _tool("--apply", log, "--repo", r).returncode == 0                  # idempotent
    assert {n: (r / "tests" / n).read_text() for n in SHAPES} == text
    assert _tool("--check", "--repo", r).returncode == 0
    coll = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q",
                           "-p", "no:cacheprovider", "-p", "no:xdist", "tests"],
                          cwd=r, capture_output=True, text=True)
    assert coll.returncode == 0, coll.stdout + coll.stderr                    # --strict-markers


def test_apply_without_a_log_exits_2_and_writes_nothing(tmp_path):
    r = _tree(tmp_path, SHAPES)
    before = {n: (r / "tests" / n).read_text() for n in SHAPES}
    empty = tmp_path / "empty.log"
    empty.write_text("  \n")
    assert _tool("--apply", tmp_path / "missing.log", "--repo", r).returncode == 2
    assert _tool("--apply", empty, "--repo", r).returncode == 2
    assert {n: (r / "tests" / n).read_text() for n in SHAPES} == before


def test_apply_hoists_pytest_above_an_early_pytestmark(tmp_path):
    r = _tree(tmp_path, {"test_late_imp.py": """\
        pytestmark = pytest.mark.fast


        def test_one():
            import pytest
            assert pytest
        """})
    log = tmp_path / "d.log"
    log.write_text("0.01s call tests/test_late_imp.py::test_one\n")
    assert _tool("--apply", log, "--repo", r).returncode == 0
    text = (r / "tests" / "test_late_imp.py").read_text()
    assert text.index("import pytest") < text.index("pytestmark =")
