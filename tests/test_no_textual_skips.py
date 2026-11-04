"""SPEC-0409: no TUI test may be silently skipped or un-collected for a missing Textual/pyte.

An AST scan of every `tests/**/*.py` (skip constructs and `collect_ignore`), plus a collect-only
run of the TUI files that proves each one still contributes tests. Textual and pyte are
unconditional test requirements: a missing one must be an import error, never a skip.
"""
import ast
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.tree_guard  # SPEC-0421: scans the tree; always run by --guards

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent

SKIP_CALLS = frozenset({"importorskip", "skip", "skipif", "xfail"})
MODULE_NAMES = frozenset({"textual", "textual.*", "pyte"})
GATE_NAMES = frozenset({"needs_textual", "needs_pyte"})
AVAILABLE = "textual_available"


def _call_name(node: ast.Call) -> str:
    f = node.func
    return f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")


def _mentions_textual(tree: ast.AST, bare_strings: bool = True) -> bool:
    """`bare_strings=False` (used for `if` tests) ignores a plain "textual" string, which is
    ordinary text in a test that searches rendered output; only `find_spec("textual")` counts."""
    for n in ast.walk(tree):
        if isinstance(n, ast.Name) and n.id == AVAILABLE:
            return True
        if isinstance(n, ast.Attribute) and n.attr == AVAILABLE:
            return True
        if isinstance(n, ast.Call) and _call_name(n) == "find_spec":
            if any(isinstance(a, ast.Constant) and a.value in MODULE_NAMES for a in n.args):
                return True
        if (bare_strings and isinstance(n, ast.Constant) and isinstance(n.value, str)
                and n.value in MODULE_NAMES):
            return True
    return False


def _gate_name(name: str) -> bool:
    return name in GATE_NAMES or name.startswith("collect_ignore")


def scan_source(source: str, path: str) -> list[str]:
    """Violations in `source` as `path:line: message`, in line order."""
    out: set[tuple[int, str]] = set()
    for n in ast.walk(ast.parse(source)):
        if isinstance(n, ast.Call) and _call_name(n) in SKIP_CALLS and _mentions_textual(n):
            out.add((n.lineno, f"{_call_name(n)}() on textual/pyte"))
        elif isinstance(n, (ast.If, ast.IfExp)) and _mentions_textual(n.test, bare_strings=False):
            out.add((n.lineno, "conditional on textual/pyte availability"))
        names = []
        if isinstance(n, ast.Name):
            names = [n.id]
        elif isinstance(n, ast.Attribute):
            names = [n.attr]
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names = [n.name]
        elif isinstance(n, ast.alias):
            names = [n.asname or n.name]
        elif isinstance(n, ast.arg):
            names = [n.arg]
        for name in names:
            if _gate_name(name):
                out.add((getattr(n, "lineno", 0), f"forbidden name {name}"))
    return [f"{path}:{line}: {msg}" for line, msg in sorted(out)]


def scan_tree(root: Path) -> list[str]:
    found = []
    for p in sorted(root.rglob("*.py")):
        found += scan_source(p.read_text(), str(p.relative_to(root.parent)))
    return found


def test_tree_has_no_textual_or_pyte_skips():
    assert scan_tree(TESTS) == []


# Synthetic violating sources are string literals: the scan above never parses them as code.
VIOLATIONS = {
    "importorskip": 'import pytest\npytest.importorskip("textual")\n',
    "importorskip-pyte": 'import pytest\npyte = pytest.importorskip("pyte")\n',
    "skipif-decorator": ('import pytest\n@pytest.mark.skipif(not T.textual_available(), reason="x")\n'
                         'def test_a():\n    pass\n'),
    "pytestmark": 'import pytest\npytestmark = pytest.mark.skipif(not T.textual_available())\n',
    "skip": 'import pytest\ndef test_a():\n    if not T.textual_available():\n'
            '        pytest.skip("no textual")\n',
    "if-gate": ('from importlib.util import find_spec\n'
                'if not find_spec("textual"):\n    pass\n'),
    "needs_textual": 'needs_textual = object()\n',
    "needs_pyte-use": '@needs_pyte\ndef test_a():\n    pass\n',
    "collect_ignore": 'collect_ignore = ["test_tui_x.py"]\n',
    "collect_ignore_glob": 'collect_ignore_glob = ["test_tui_*.py"]\n',
}
# Expected 1-based line of the first violation.
VIOLATION_LINE = {"importorskip": 2, "importorskip-pyte": 2, "skipif-decorator": 2, "pytestmark": 2,
                  "skip": 3, "if-gate": 2, "needs_textual": 1, "needs_pyte-use": 1,
                  "collect_ignore": 1, "collect_ignore_glob": 1}


@pytest.mark.parametrize("kind", sorted(VIOLATIONS))
def test_guard_detects_each_violation_kind(kind):
    found = scan_source(VIOLATIONS[kind], "tests/x.py")
    assert found, kind
    assert found[0].startswith(f"tests/x.py:{VIOLATION_LINE[kind]}: "), found


def test_guard_allows_textual_text_in_a_search():
    src = 'def test_a(svg):\n    x = a if re.search("textual", svg) else b\n'
    assert scan_source(src, "tests/x.py") == []


def test_guard_allows_fallback_monkeypatch_and_platform_skip():
    ok = ('import os\nimport pytest\n'
          'def test_fallback(monkeypatch):\n'
          '    monkeypatch.setattr(T, "textual_available", lambda: False)\n'
          'pytestmark = pytest.mark.skipif(not hasattr(os, "forkpty"), reason="pty")\n'
          '@pytest.mark.skipif(os.name == "nt", reason="posix")\n'
          'def test_b():\n    pass\n')
    assert scan_source(ok, "tests/x.py") == []


def test_tui_files_collect_without_skips():
    files = sorted(p.relative_to(ROOT).as_posix() for p in TESTS.glob("test_tui_*.py"))
    assert files
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:xdist",
         "-p", "no:cacheprovider", "-o", "addopts=", *files],
        cwd=ROOT, capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr
    counts = dict.fromkeys(files, 0)
    for line in r.stdout.splitlines():
        head = line.split("::", 1)[0]
        if "::" in line and head in counts:
            counts[head] += 1
    assert [f for f, n in counts.items() if n == 0] == []
