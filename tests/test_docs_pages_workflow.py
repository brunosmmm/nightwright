"""SPEC-0160: GitHub Pages publish workflow + site_url."""
from pathlib import Path
import pytest

pytestmark = pytest.mark.fast

ROOT = Path(__file__).resolve().parent.parent


def test_docs_pages_workflow_exists_and_deploys_via_actions():
    path = ROOT / ".github" / "workflows" / "docs-pages.yml"
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert "mkdocs build --strict" in text
    assert "actions/upload-pages-artifact" in text
    assert "actions/deploy-pages" in text
    assert "workflow_dispatch" in text
    assert "github-pages" in text


def test_mkdocs_site_url_points_at_github_pages():
    text = (ROOT / "mkdocs.yml").read_text(encoding="utf-8")
    assert "site_url: https://brunosmmm.github.io/nightwright/" in text
