"""SPEC-0223: use-case oriented docs — the two pillars (ideas → specs, the agentic loop), a
bootstrap path, diagrams on every pillar page, and states / park reasons kept in sync with the
code."""
import re
from pathlib import Path

import pytest

from wt.agentic import queue as Q

ROOT = Path(__file__).resolve().parent.parent
PRODUCT = ROOT / "docs" / "product"
PILLAR_PAGES = [
    "ideas/index.md", "ideas/capture.md", "ideas/explore.md", "ideas/spec.md",
    "loop/index.md", "loop/principles.md", "loop/states.md", "loop/convergence.md",
    "loop/operate.md",
]
NEW_PAGES = PILLAR_PAGES + ["guides/bootstrap.md"]


def _text(rel):
    return (PRODUCT / rel).read_text(encoding="utf-8")


@pytest.mark.parametrize("rel", NEW_PAGES)
def test_new_pages_exist_are_substantial_and_in_nav(rel):
    assert len(_text(rel)) > 1500, rel
    nav = (ROOT / "mkdocs.yml").read_text(encoding="utf-8")
    assert rel in nav, f"{rel} not in mkdocs.yml nav"


@pytest.mark.parametrize("rel", PILLAR_PAGES)
def test_pillar_pages_have_a_diagram(rel):
    assert "```mermaid" in _text(rel), rel


def test_state_machine_page_names_every_state():
    text = _text("loop/states.md")
    missing = [s for s in Q.STATES if not re.search(rf"\b{s}\b", text)]
    assert not missing, f"loop/states.md does not document {missing}"
    assert "stateDiagram-v2" in text


def test_operate_page_names_every_park_reason():
    text = _text("loop/operate.md")
    missing = [r for r in sorted(Q.PARK_REASONS) if f"`{r}`" not in text]
    assert not missing, f"loop/operate.md park table misses {missing}"


def test_home_links_both_pillars_and_bootstrap():
    home = _text("index.md")
    for link in ("ideas/index.md", "loop/index.md", "guides/bootstrap.md"):
        assert link in home, link
