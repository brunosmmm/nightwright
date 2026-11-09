"""SPEC-0269: guard the shared test waits (tests/waits.py). A private polling helper or a
bare numeric `.join(N)` before a result read is a load flake waiting to happen."""
import ast
import re
import threading
import time
from pathlib import Path

import pytest

import waits

pytestmark = [pytest.mark.fast, pytest.mark.tree_guard]

TESTS = Path(__file__).parent
HELPER_NAMES = {"until", "_until", "wait_for", "_wait_for"}
NUMERIC_JOIN = re.compile(r"\.join\(\s*\d+(\.\d+)?\s*\)")


def _modules():
    return [p for p in sorted(TESTS.glob("test_*.py")) if p.name != Path(__file__).name]


def private_helpers(source):
    return [n.name for n in ast.parse(source).body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in HELPER_NAMES]


def bare_joins(source):
    """Line numbers of numeric joins not followed (next non-blank line) by an is_alive() check."""
    lines, found = source.splitlines(), []
    for i, line in enumerate(lines):
        if NUMERIC_JOIN.search(line):
            nxt = next((x for x in lines[i + 1:] if x.strip()), "")
            if not (nxt.strip().startswith("assert") and "is_alive()" in nxt):
                found.append(i + 1)
    return found


POSITIVE_GRID_ASSERT = re.compile(r"""^\s*assert\s+(?:"[^"\n]*"|'[^'\n]*')\s+in\s+.*_grid\(""")


def pause_then_grid_assert(source):
    """Line numbers of a positive `assert "text" in ..._grid(app)` right after `await pilot.pause()`:
    a fixed pause is a load race; use `waits.pilot_until`. (Narrow on purpose: negative asserts and
    assigned screens are not flagged.)"""
    lines = source.splitlines()
    return [i + 1 for i in range(1, len(lines))
            if lines[i - 1].strip() == "await pilot.pause()" and POSITIVE_GRID_ASSERT.match(lines[i])]


def test_no_pilot_pause_before_positive_grid_assert():
    bad = {p.name: pause_then_grid_assert(p.read_text()) for p in _modules()}
    bad = {k: v for k, v in bad.items() if v}
    assert not bad, f"poll with waits.pilot_until, not a fixed pilot.pause(): {bad}"


def test_no_private_wait_helpers():
    bad = {p.name: private_helpers(p.read_text()) for p in _modules()}
    bad = {k: v for k, v in bad.items() if v}
    assert not bad, f"use tests/waits.py instead of a private wait helper: {bad}"


def test_no_bare_join_before_result_read():
    bad = {p.name: bare_joins(p.read_text()) for p in _modules()}
    bad = {k: v for k, v in bad.items() if v}
    assert not bad, f"use waits.join(t), not a numeric .join(N): {bad}"


def test_guard_scans_catch_the_shapes():
    assert private_helpers("def until(c):\n    pass\n") == ["until"]
    assert private_helpers("async def _wait_for(c):\n    pass\n") == ["_wait_for"]
    assert private_helpers("from waits import until\ndef f():\n    def until(c): pass\n") == []
    assert bare_joins('t.join(10)\nres = out["res"]\n') == [1]
    assert bare_joins("side.join(60)\n\nassert not side.is_alive()\n") == []
    assert bare_joins('x = "".join(parts)\n') == []
    assert pause_then_grid_assert('await pilot.pause()\nassert "a" in "\\n".join(_grid(app))\n') == [2]
    assert pause_then_grid_assert('await pilot.pause()\nassert "a" not in _grid(app)\n') == []


def test_until_and_join_bounds():
    assert waits.until(lambda: True)
    t0 = time.monotonic()
    assert waits.until(lambda: False, timeout=0.2) is False
    assert time.monotonic() - t0 < 1.0
    quick = threading.Thread(target=time.sleep, args=(0.1,))
    quick.start()
    waits.join(quick)
    stuck_ev = threading.Event()
    stuck = threading.Thread(target=stuck_ev.wait)
    stuck.start()
    try:
        with pytest.raises(AssertionError, match="still running"):
            waits.join(stuck, timeout=0.2)
    finally:
        stuck_ev.set()
        stuck.join()
