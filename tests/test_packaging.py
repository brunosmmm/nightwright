"""SPEC-0237: the builtin profile YAML files ship in the wheel."""
import shutil
import subprocess
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_wheel_contains_the_builtin_profile_files(tmp_path):
    """Not skipped without uv: the wheel-content criterion must fail loudly, not go unchecked."""
    assert shutil.which("uv"), "uv is required to build the wheel (the project's tooling)"
    out = tmp_path / "dist"
    subprocess.run(["uv", "build", "--wheel", "--out-dir", str(out)], cwd=ROOT, check=True,
                   capture_output=True, text=True)
    [wheel] = out.glob("*.whl")
    names = set(zipfile.ZipFile(wheel).namelist())
    for profile in ("balanced", "frugal", "thorough"):
        assert f"nightwright/agentic/profile_defs/{profile}.yaml" in names
