"""SPEC-0142: docs/product IA tree + home narrative keywords."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PRODUCT = ROOT / "docs" / "product"

REQUIRED = [
    "index.md",
    "concepts/index.md",
    "guides/index.md",
    "guides/getting-started.md",
    "guides/track-time.md",
    "guides/idea-to-ship.md",
    "guides/outbound.md",
    "guides/multi-project.md",
    "capabilities/index.md",
    "capabilities/report.md",
    "capabilities/ideas.md",
    "capabilities/tui.md",
    "capabilities/hub.md",
    "capabilities/projects.md",
    "capabilities/sheet.md",
    "capabilities/clock.md",
    "capabilities/completion.md",
    "capabilities/release-archive.md",
    "capabilities/tasks-agenda.md",
    "capabilities/meetings.md",
    "capabilities/skills.md",
    "reference/index.md",
    "contribute/index.md",
]


def test_docs_product_ia_tree_exists():
    missing = [p for p in REQUIRED if not (PRODUCT / p).is_file()]
    assert not missing, f"missing docs/product paths: {missing}"


def test_home_locks_dual_capability_and_clock_wording():
    home = (PRODUCT / "index.md").read_text(encoding="utf-8").lower()
    assert "passive" in home and "time" in home
    assert "idea" in home and ("ship" in home or "spec" in home)
    assert "verify" in home or "test plan" in home
    assert "clock-in" in home or "clock in" in home
    assert "supplemental" in home
    assert "not an idea catalog" not in home
    assert "is a cli" not in home  # CLI is one surface, not the whole product
    assert "tui" in home or "desk" in home
    assert home.find("ideas") < home.find("passive time")


def test_agentic_docs_cover_cursor_backend():
    agentic = (PRODUCT / "capabilities" / "agentic.md").read_text(encoding="utf-8")
    operate = (PRODUCT / "loop" / "operate.md").read_text(encoding="utf-8")
    loop = (PRODUCT / "loop" / "index.md").read_text(encoding="utf-8")
    for text in (agentic, operate, loop):
        assert "cursor" in text.lower()
    assert "cursor-agent" in agentic
    assert "nw queue backend" in agentic
    assert "--host" in agentic and "--all" in agentic
    assert "Persona" in (PRODUCT / "concepts" / "index.md").read_text(encoding="utf-8")


def test_glossary_has_core_terms():
    gloss = (PRODUCT / "concepts" / "index.md").read_text(encoding="utf-8").lower()
    for term in ("topic", "idea", "spec", "outbound", "hub", "desk", "clock-in", "facet"):
        assert term in gloss, f"glossary missing {term!r}"
