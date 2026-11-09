"""SPEC-0146: product reference covers every top-level nightwright command."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
import pytest

pytestmark = pytest.mark.fast

ROOT = Path(__file__).resolve().parent.parent
REF = ROOT / "docs" / "product" / "reference" / "index.md"
README = ROOT / "README.md"


def _live_top_level_commands() -> list[str]:
    proc = subprocess.run(
        ["uv", "run", "nw", "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    lines = proc.stdout.splitlines()
    # Click prints "Commands:" then indented "  name  desc"
    try:
        start = next(i for i, line in enumerate(lines) if line.strip() == "Commands:")
    except StopIteration as exc:
        raise AssertionError("nw --help missing Commands: section") from exc
    names: list[str] = []
    for line in lines[start + 1 :]:
        if not line.strip():
            break
        m = re.match(r"\s{2}(\S+)\s+", line)
        if m:
            names.append(m.group(1))
    assert names, "parsed zero commands from nw --help"
    return names


def test_reference_lists_every_top_level_command():
    text = REF.read_text(encoding="utf-8")
    assert "**Status:** stub" not in text
    for name in _live_top_level_commands():
        assert f"`{name}`" in text, f"reference missing top-level command {name!r}"


def test_readme_has_no_commands_encyclopedia_table():
    readme = README.read_text(encoding="utf-8")
    # Old encyclopedia used a markdown table under ## Commands with | Command | Purpose |
    assert "| Command | Purpose |" not in readme
    assert "docs/product/reference" in readme or "Command reference" in readme
