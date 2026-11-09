"""SPEC-0145: capability showcase pages are non-stub."""
from pathlib import Path
import pytest

pytestmark = pytest.mark.fast

ROOT = Path(__file__).resolve().parent.parent
CAPS = ROOT / "docs" / "product" / "capabilities"

REQUIRED = {
    "report.md": ["nw report", "nw topics", "nw snapshot"],
    "ideas.md": ["nw ideas", "nw next", "nw idea"],
    "tui.md": ["nw tui", "tui"],
    "hub.md": ["nw hub", "--json"],
    "projects.md": ["nw projects", "nw spec export"],
    "sheet.md": ["nw sheet", "plan"],
    "clock.md": ["clock-in", "supplemental"],
    "completion.md": ["nw completion", "fish"],
    "release-archive.md": ["nw release-archive"],
    "tasks-agenda.md": ["nw tasks", "nw agenda", "nw add"],
    "meetings.md": ["nw refresh-meetings"],
    "skills.md": ["nw skills install", "/nw-orient"],
}


def test_capability_pages_not_stubs():
    for name in REQUIRED:
        text = (CAPS / name).read_text(encoding="utf-8")
        assert "**Status:** stub" not in text, name
        assert len(text) > 250, name


def test_capability_pages_key_phrases():
    for name, phrases in REQUIRED.items():
        text = (CAPS / name).read_text(encoding="utf-8")
        for phrase in phrases:
            assert phrase in text, f"{name} missing {phrase!r}"


def test_capabilities_index_not_stub_language():
    index = (CAPS / "index.md").read_text(encoding="utf-8")
    assert "IDEA-358" not in index
    assert "short scope card" not in index
    for name in REQUIRED:
        assert name in index


