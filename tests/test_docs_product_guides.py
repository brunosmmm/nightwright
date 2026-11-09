"""SPEC-0144: product guides are non-stub and cover core workflows."""
from pathlib import Path
import pytest

pytestmark = pytest.mark.fast

ROOT = Path(__file__).resolve().parent.parent
GUIDES = ROOT / "docs" / "product" / "guides"

REQUIRED_PHRASES = {
    "getting-started.md": ["uv tool install", "nw topics --unmapped", "nw idea"],
    "track-time.md": ["nw report --week", "nw snapshot", "passive"],
    "idea-to-ship.md": ["nw spec new", "nw spec generate", "nw next"],
    "outbound.md": ["nw projects add", "nw spec export", "pull-status"],
    "multi-project.md": ["nw projects --json", "fan"],
}


def test_guides_are_not_stubs():
    for name in REQUIRED_PHRASES:
        text = (GUIDES / name).read_text(encoding="utf-8")
        assert "**Status:** stub" not in text, f"{name} still stub"
        assert len(text) > 400, f"{name} too short"


def test_guides_cover_key_commands():
    for name, phrases in REQUIRED_PHRASES.items():
        text = (GUIDES / name).read_text(encoding="utf-8")
        for phrase in phrases:
            assert phrase in text, f"{name} missing {phrase!r}"


def test_guides_index_lists_five():
    index = (GUIDES / "index.md").read_text(encoding="utf-8")
    assert "**Status:** stub" not in index
    for slug in (
        "getting-started.md",
        "track-time.md",
        "idea-to-ship.md",
        "outbound.md",
        "multi-project.md",
    ):
        assert slug in index


