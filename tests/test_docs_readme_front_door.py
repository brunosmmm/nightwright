"""SPEC-0147: root README is a slim front door into docs/product."""
from pathlib import Path
import pytest

pytestmark = pytest.mark.fast

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
TIME = ROOT / "docs" / "product" / "concepts" / "time-and-retention.md"


def test_readme_is_slim_front_door():
    lines = README.read_text(encoding="utf-8").splitlines()
    assert len(lines) <= 120, f"README too long: {len(lines)} lines"
    text = "\n".join(lines).lower()
    assert "passive" in text and "idea" in text
    assert "uv sync" in text or "uv tool install" in text
    assert "docs/product" in text
    assert "scripts/docs-serve.sh" in text or "mkdocs" in text
    assert "| command | purpose |" not in text
    assert "cursor" in text and "cursor-agent" in text


def test_time_and_retention_page_exists():
    text = TIME.read_text(encoding="utf-8").lower()
    assert "snapshot" in text
    assert "cleanupperioddays" in text or "cleanup" in text
    assert "gap_minutes" in text or "gap" in text


