"""SPEC-0367: the build id (`nightwright.buildid`) and `nw worker info`'s `build_id`."""
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from nightwright import buildid
from nightwright.agentic import worker as W

pytestmark = pytest.mark.fast


def _pkg(root: Path, files=None) -> Path:
    pkg = root / "nightwright"
    pkg.mkdir()
    for rel, data in (files or {"__init__.py": "v = 1\n", "sub/mod.py": "x = 1\n"}).items():
        (pkg / rel).parent.mkdir(parents=True, exist_ok=True)
        (pkg / rel).write_text(data)
    return pkg


def test_stable_and_prefixed(tmp_path):
    pkg = _pkg(tmp_path)
    a = buildid.compute(pkg)
    assert a.startswith("b1:") and len(a) == 3 + 64 and buildid.compute(pkg) == a


def test_pycache_and_pyc_are_ignored(tmp_path):
    pkg = _pkg(tmp_path)
    a = buildid.compute(pkg)
    (pkg / "__pycache__").mkdir()
    (pkg / "__pycache__" / "mod.cpython-312.pyc").write_bytes(b"\0\1")
    (pkg / "sub" / "stray.pyc").write_bytes(b"\2")
    assert buildid.compute(pkg) == a


def test_a_byte_change_add_or_remove_changes_the_id(tmp_path):
    pkg = _pkg(tmp_path)
    a = buildid.compute(pkg)
    (pkg / "sub" / "mod.py").write_text("x = 2\n")
    b = buildid.compute(pkg)
    (pkg / "new.py").write_text("")
    c = buildid.compute(pkg)
    (pkg / "new.py").unlink()
    (pkg / "sub" / "mod.py").unlink()
    d = buildid.compute(pkg)
    assert len({a, b, c, d}) == 4


def test_a_rename_changes_the_id(tmp_path):
    pkg = _pkg(tmp_path)
    a = buildid.compute(pkg)
    (pkg / "sub" / "mod.py").rename(pkg / "sub" / "mod2.py")
    assert buildid.compute(pkg) != a


@pytest.mark.skipif(not shutil.which("git"), reason="git needed")
def test_git_ignored_stray_file_is_ignored_inside_a_work_tree(tmp_path):
    pkg = _pkg(tmp_path)
    plain = buildid.compute(pkg)                    # no repo: a directory walk
    for args in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True)
    (tmp_path / ".gitignore").write_text("*.log\nstray.txt\n")
    assert buildid.compute(pkg) == plain
    (pkg / "stray.txt").write_text("ignored")
    (pkg / "debug.log").write_text("ignored")
    assert buildid.compute(pkg) == plain            # ignored files are not shipped by hatch
    (pkg / "kept.txt").write_text("untracked but shipped")
    assert buildid.compute(pkg) != plain


def test_a_git_failure_falls_back_to_the_walk(tmp_path, monkeypatch):
    pkg = _pkg(tmp_path)
    walk = buildid.compute(pkg)

    def boom(*a, **k):
        raise OSError("no git")
    monkeypatch.setattr(subprocess, "run", boom)
    assert buildid.compute(pkg) == walk


def test_an_empty_git_listing_is_not_a_work_tree(tmp_path, monkeypatch):
    """An installed copy under a git-ignored venv lists nothing: walk it instead."""
    pkg = _pkg(tmp_path)
    walk = buildid.compute(pkg)
    done = subprocess.CompletedProcess([], 0, stdout=b"", stderr=b"")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: done)
    assert buildid.compute(pkg) == walk


def test_compute_wheel_equals_compute_of_the_installed_tree(tmp_path):
    pkg = _pkg(tmp_path)
    whl = tmp_path / "nightwright-0.1.0-py3-none-any.whl"
    with zipfile.ZipFile(whl, "w") as z:
        for p in sorted(pkg.rglob("*")):
            if p.is_file():
                z.write(p, "nightwright/" + p.relative_to(pkg).as_posix())
        z.writestr("nightwright-0.1.0.dist-info/METADATA", "Name: nightwright\n")
        z.writestr("nightwright/__pycache__/x.cpython-312.pyc", b"\0")
    assert buildid.compute_wheel(whl) == buildid.compute(pkg)


@pytest.mark.skipif(not shutil.which("uv"), reason="uv needed")
def test_a_wheel_built_with_uv_matches_the_working_tree(tmp_path):
    root = Path(buildid.__file__).resolve().parents[2]
    if not (root / "pyproject.toml").is_file():
        pytest.skip("not a source checkout")
    out = tmp_path / "out"
    p = subprocess.run(["uv", "build", "--wheel", "--out-dir", str(out), str(root)],
                       capture_output=True, text=True)
    if p.returncode != 0:
        pytest.skip(f"uv build unavailable offline: {p.stderr[-200:]}")
    (whl,) = out.glob("*.whl")
    assert buildid.compute_wheel(whl) == buildid.local()


def test_worker_info_reports_a_build_id(tmp_path):
    info = W.info({"data_dir": str(tmp_path)})
    assert info["build_id"].startswith("b1:") and info["build_id"] == buildid.local()
