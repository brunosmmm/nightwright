"""SPEC-0490: guard and unit tests for `tools/test_tiers.py`."""
from __future__ import annotations

import importlib.util
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

pytestmark = [pytest.mark.fast, pytest.mark.tree_guard]

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("test_tiers", ROOT / "tools" / "test_tiers.py")
TT = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(TT)


def test_check_real_tree():
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "test_tiers.py"), "check"],
                       cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def _mini_repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    (r / "tests").mkdir(parents=True)
    (r / "pyproject.toml").write_text("[project]\nname='x'\n")
    return r


def test_classify_precedence(tmp_path):
    r = _mini_repo(tmp_path)
    (r / "tests" / "test_foo_race.py").write_text("pytestmark = pytest.mark.fast\n")
    (r / "tests" / "test_tui_x.py").write_text("import pytest\npytestmark = pytest.mark.fast\n")
    (r / "tests" / "test_remote_x.py").write_text("import pytest\npytestmark = pytest.mark.fast\n")
    (r / "tests" / "test_run_test.py").write_text(
        "class H:\n    def run_test(self): pass\n"
        "def x(h):\n    h.run_test()\nimport pytest\npytestmark = pytest.mark.fast\n")
    (r / "tests" / "test_rem.py").write_text("from remote_loopback import x\npytestmark = pytest.mark.fast\n")
    log = tmp_path / "d.log"
    log.write_text("0.01s call tests/test_foo_race.py::test_a\n")
    assert TT.taxonomy_tier("tests/test_foo_race.py", TT._parse_file(r, "tests/test_foo_race.py")) == "integration"
    assert TT.taxonomy_tier("tests/test_tui_x.py", TT._parse_file(r, "tests/test_tui_x.py")) == "tui"
    assert TT.taxonomy_tier("tests/test_run_test.py", TT._parse_file(r, "tests/test_run_test.py")) == "tui"
    assert TT.taxonomy_tier("tests/test_rem.py", TT._parse_file(r, "tests/test_rem.py")) == "remote"


def test_apply_shapes(tmp_path):
    r = _mini_repo(tmp_path)
    cases = {
        "test_skip.py": "import pytest\npytestmark = pytest.mark.skipif(True, reason='x')\n",
        "test_guard.py": "import pytest\npytestmark = pytest.mark.tree_guard\n",
        "test_list.py": "import pytest\npytestmark = [pytest.mark.skipif(True, reason='y')]\n",
        "test_late.py": "x = 1\nimport pytest\npytestmark = pytest.mark.tree_guard\n",
        "test_slow_fn.py": "import pytest\n@pytest.mark.slow\ndef test_one(): pass\n",
    }
    for name, body in cases.items():
        (r / "tests" / name).write_text(body)
    log = tmp_path / "d.log"
    log.write_text("\n".join(f"0.01s call tests/{n}::test_one" for n in cases))
    assert subprocess.run([sys.executable, str(ROOT / "tools" / "test_tiers.py"), "apply", str(log),
                         "--repo", str(r)], cwd=ROOT).returncode == 0
    assert subprocess.run([sys.executable, str(ROOT / "tools" / "test_tiers.py"), "apply", str(log),
                         "--repo", str(r)], cwd=ROOT).returncode == 0
    assert "fast" in (r / "tests" / "test_skip.py").read_text()
    assert "tree_guard" in (r / "tests" / "test_guard.py").read_text()
    assert "slow" not in (r / "tests" / "test_slow_fn.py").read_text()
    miss = tmp_path / "missing.log"
    assert subprocess.run([sys.executable, str(ROOT / "tools" / "test_tiers.py"), "apply", str(miss),
                           "--repo", str(r)], cwd=ROOT).returncode == 2


def test_budget_rules(tmp_path):
    r = _mini_repo(tmp_path)
    (r / "tests" / "test_mod.py").write_text("import pytest\npytestmark = pytest.mark.fast\n")
    log = tmp_path / "big.log"
    log.write_text(textwrap.dedent("""\
        3.00s call tests/test_mod.py::test_slow_one
        0.01s call tests/test_mod.py::test_fast
        """))
    tier, ov = TT.classify_module("tests/test_mod.py", TT._parse_file(r, "tests/test_mod.py"),
                                  TT.parse_durations_log(log.read_text()))
    assert tier == "fast" and "test_slow_one" in {TT._func_from_nodeid(k) for k in ov}
    log.write_text("25.00s call tests/test_mod.py::test_only\n")
    tier, _ = TT.classify_module("tests/test_mod.py", TT._parse_file(r, "tests/test_mod.py"),
                                 TT.parse_durations_log(log.read_text()))
    assert tier == "integration"
