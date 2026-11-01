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
    assert len(sel.tests) <= AT.MAX_FRACTION * len(all_tests)
