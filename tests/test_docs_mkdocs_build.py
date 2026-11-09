"""SPEC-0143: MkDocs Material builds docs/product strictly."""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.fast

ROOT = Path(__file__).resolve().parent.parent


def test_mkdocs_yml_and_serve_script_exist():
    assert (ROOT / "mkdocs.yml").is_file()
    serve = ROOT / "scripts" / "docs-serve.sh"
    assert serve.is_file()
    assert "mkdocs serve" in serve.read_text(encoding="utf-8")


@pytest.mark.integration
def test_mkdocs_build_strict():
    if shutil.which("uv") is None:
        pytest.skip("uv not on PATH")
    with tempfile.TemporaryDirectory() as tmp:
        site = Path(tmp) / "site"
        proc = subprocess.run(
            ["uv", "run", "mkdocs", "build", "--strict", "--site-dir", str(site)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, proc.stdout + "\n" + proc.stderr
        assert (site / "index.html").is_file()
