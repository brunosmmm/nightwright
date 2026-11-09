"""SPEC-0424 AC6: no code path writes `~/.claude/settings.json` except `agent_permissions.py`
(`nw skills install`, SPEC-0320). Scans the src tree, so it is a tree guard (SPEC-0421)."""
import re
from pathlib import Path

import pytest

pytestmark = [pytest.mark.fast, pytest.mark.tree_guard]

ROOT = Path(__file__).resolve().parents[1]


def test_only_agent_permissions_writes_claude_settings():             # SPEC-0424 AC6
    """Only `agent_permissions.py` builds a `settings.json` path and writes files; the others
    that build one (doctor's retention check) only read it; help texts merely name it."""
    src = ROOT / "src" / "nightwright"
    builders = {}
    for f in src.rglob("*.py"):
        text = f.read_text(encoding="utf-8")
        if re.search(r'/\s*"settings\.json"', text):
            builders[f.relative_to(src).as_posix()] = bool(
                re.search(r"open\(|write_text|write_bytes|fdopen|os\.replace", text))
    assert "agent_permissions.py" in builders
    writers = {name for name, writes in builders.items() if writes}
    assert writers <= {"agent_permissions.py"}, writers
    assert builders.get("doctor.py") is not True
