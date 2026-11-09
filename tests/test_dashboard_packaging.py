"""SPEC-0413: the page files ship in the built wheel. Builds offline (no network): needs
hatchling in uv's cache, and says so when it is not."""
import shutil
import subprocess
import zipfile
from pathlib import Path

from waits import WAIT_S
import pytest

pytestmark = pytest.mark.fast

ROOT = Path(__file__).resolve().parent.parent
PREFIX = "nightwright/agentic/dashboard/"


def test_wheel_contains_the_page_files(tmp_path):
    uv = shutil.which("uv")
    assert uv, "uv is required to build the wheel for this test"
    r = subprocess.run([uv, "build", "--wheel", "--offline", "--out-dir", str(tmp_path)], cwd=ROOT,
                       capture_output=True, text=True, timeout=max(WAIT_S, 120))
    assert r.returncode == 0, ("`uv build --wheel --offline` failed (hatchling must be in uv's "
                               f"cache for this test):\n{r.stdout}\n{r.stderr}")
    wheels = list(tmp_path.glob("*.whl"))
    assert len(wheels) == 1
    names = set(zipfile.ZipFile(wheels[0]).namelist())
    for f in ("assets.py", "static/index.html", "static/app.js", "static/app.css"):
        assert PREFIX + f in names, f


