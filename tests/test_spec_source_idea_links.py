"""SPEC-0570: IDEA-1024 belongs to SPEC-0463; SPEC-0458 (tail cards) has no source idea."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

from spec_lint import split_frontmatter  # noqa: E402

pytestmark = pytest.mark.fast


def _frontmatter(pattern):
    (path,) = (ROOT / "docs" / "specs").glob(pattern)
    fm, _ = split_frontmatter(path.read_text())
    return fm


def test_idea_1024_spec_is_the_periodic_resolver():
    fm = _frontmatter("0463-*.md")
    assert fm["source_idea"] == "IDEA-1024"
    assert fm["title"].startswith("Periodic deterministic resolution")


def test_tail_cards_spec_has_no_source_idea():
    assert "source_idea" not in _frontmatter("0458-*.md")
