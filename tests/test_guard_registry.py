"""SPEC-0421: the `tree_guard` registry. A test module that scans the tests or src tree is marked
`pytestmark = pytest.mark.tree_guard`, so `tools/affected_tests.py --guards` always runs it at the
loop and close gates; this test keeps the marks and the detector in agreement."""
import importlib.util
import tomllib
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.tree_guard

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("affected_tests_registry", ROOT / "tools" / "affected_tests.py")
AT = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(AT)

# module -> why it scans a tree root without being a guard (none today)
GUARD_EXEMPT: dict[str, str] = {}
GUARDED_ROOTS = {"tests", "src/nightwright", "src"}


def _scan(repo):
    """(detected tree scanners, marked guards) of the test modules under `repo`."""
    ix = AT.Index(repo)
    scanners = {ix.mods[m] for m, i in ix.info.items()
                if AT._is_test_module(ix.mods[m]) and i.tree_roots & GUARDED_ROOTS}
    return scanners, set(AT._guards(ix))


def test_every_tree_scanner_is_marked_or_exempt():
    scanners, marked = _scan(ROOT)
    missing = sorted(scanners - marked - set(GUARD_EXEMPT))
    assert not missing, f"scan the tests/src tree but lack `pytestmark = pytest.mark.tree_guard`: {missing}"


def test_every_marked_module_is_a_detected_scanner():
    scanners, marked = _scan(ROOT)
    assert not sorted(marked - scanners), "stale tree_guard marks (no scan detected)"


def test_no_module_is_both_marked_and_exempt():
    _, marked = _scan(ROOT)
    assert not sorted(marked & set(GUARD_EXEMPT))
    assert all(reason.strip() for reason in GUARD_EXEMPT.values())


def test_the_named_guards_are_registered():
    _, marked = _scan(ROOT)
    assert {"tests/test_test_waits.py", "tests/test_no_textual_skips.py",
            "tests/test_rich_output_guard.py", "tests/test_guard_registry.py"} <= marked


def test_pytest_gates_pass_guards():
    gates = {g["name"]: g for g in yaml.safe_load((ROOT / ".nw" / "project.yaml").read_text())["gates"]}
    for name in ("pytest-affected", "pytest"):
        assert "--guards" in gates[name]["cmd"], name


def test_marker_is_registered_in_pyproject():
    markers = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["pytest"]["ini_options"]["markers"]
    assert any(m.startswith("tree_guard:") for m in markers)


def _copy_tests(tmp_path):
    dest = tmp_path / "repo"
    (dest / "tests").mkdir(parents=True)
    for p in (ROOT / "tests").glob("test_test_waits.py"):
        (dest / "tests" / p.name).write_text(p.read_text())
    return dest


def test_removing_the_mark_is_reported(tmp_path):
    dest = _copy_tests(tmp_path)
    scanners, marked = _scan(dest)
    assert scanners == marked == {"tests/test_test_waits.py"}
    f = dest / "tests" / "test_test_waits.py"
    f.write_text(f.read_text().replace("pytestmark = pytest.mark.tree_guard", "pass"))
    scanners, marked = _scan(dest)
    assert scanners - marked == {"tests/test_test_waits.py"}     # test (a) would fail


def test_a_new_unmarked_globbing_module_is_reported(tmp_path):
    dest = _copy_tests(tmp_path)
    (dest / "tests" / "test_new_guard.py").write_text(
        "from pathlib import Path\n\nFILES = sorted((Path(__file__).parent).glob('test_*.py'))\n")
    scanners, marked = _scan(dest)
    assert scanners - marked == {"tests/test_new_guard.py"}


def test_loop_gate_runs_on_a_tool_that_predates_the_flag(tmp_path):
    import subprocess
    gates = {g["name"]: g for g in yaml.safe_load((ROOT / ".nw" / "project.yaml").read_text())["gates"]}
    cmd = gates["pytest-affected"]["cmd"].replace("uv run pytest -q -x -n 2;", "echo RAN;", 1)
    (tmp_path / "tools").mkdir()
    # an old tool: no --guards in its help, and it rejects the flag
    (tmp_path / "tools" / "affected_tests.py").write_text(
        "import sys\nif '--help' in sys.argv:\n    print('usage: affected_tests [--include P]')\n"
        "    sys.exit(0)\nif '--guards' in sys.argv:\n    sys.exit(2)\nprint('OLD OK', *sys.argv[1:])\n")
    r = subprocess.run(["sh", "-c", cmd], cwd=tmp_path, capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout.startswith("OLD OK"), r.stderr
    # the real tool gets the flag
    (tmp_path / "tools" / "affected_tests.py").write_text(
        "import sys\nprint('--guards' if '--guards' in sys.argv[1:] else 'NO', *sys.argv[1:2])\n"
        "sys.exit(0) if '--help' not in sys.argv else print('--guards')\n")
    r = subprocess.run(["sh", "-c", cmd], cwd=tmp_path, capture_output=True, text=True)
    assert r.returncode == 0 and "--guards" in r.stdout, r.stdout + r.stderr
