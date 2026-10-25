"""SPEC-0237: the builtin profile YAML files ship in the wheel."""
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv not installed")
def test_wheel_contains_the_builtin_profile_files(tmp_path):
    out = tmp_path / "dist"
    subprocess.run(["uv", "build", "--wheel", "--out-dir", str(out)], cwd=ROOT, check=True,
                   capture_output=True, text=True)
    [wheel] = out.glob("*.whl")
    names = set(zipfile.ZipFile(wheel).namelist())
    for profile in ("balanced", "frugal", "thorough"):
        assert f"wt/agentic/profile_defs/{profile}.yaml" in names
