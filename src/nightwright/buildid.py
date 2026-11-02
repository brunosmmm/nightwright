"""Build identity (SPEC-0367): a SHA-256 over the contents of every file nightwright ships.

`__version__` is one constant for every build, and `git_sha` is `None` on a wheel install, so
neither can tell a worker running older code from the coordinator. The id is computed the same
way from a source tree, an installed copy or a built wheel: `path \\0 sha256(bytes) \\n` per
file in sorted relative-posix-path order. `__pycache__` and `*.pyc` are never part of it."""
from __future__ import annotations

import hashlib
import subprocess
import zipfile
from pathlib import Path

PREFIX = "b1:"
WHEEL_ROOT = "nightwright/"


def _skip(rel: str) -> bool:
    return "__pycache__" in rel.split("/") or rel.endswith(".pyc")


def _digest(entries) -> str:
    """`b1:<hex>` over `(relative posix path, bytes)` pairs, in sorted path order."""
    h = hashlib.sha256()
    for rel, data in sorted(entries, key=lambda e: e[0]):
        h.update(rel.encode() + b"\0" + hashlib.sha256(data).hexdigest().encode() + b"\n")
    return PREFIX + h.hexdigest()


def _git_files(pkg_dir: Path) -> list[str] | None:
    """What git tracks or would track under `pkg_dir` (hatch honours .gitignore), else None
    when `pkg_dir` is not in a work tree or git fails."""
    try:
        p = subprocess.run(["git", "ls-files", "-z", "--cached", "--others",
                            "--exclude-standard", "--", "."], cwd=pkg_dir,
                           capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if p.returncode != 0:
        return None
    files = [f for f in p.stdout.decode("utf-8", "surrogateescape").split("\0") if f]
    # an installed copy under a git-ignored venv lists nothing: that is not its work tree
    return files if "__init__.py" in files else None


def _walk(pkg_dir: Path) -> list[str]:
    return [p.relative_to(pkg_dir).as_posix() for p in pkg_dir.rglob("*") if p.is_file()]


def compute(pkg_dir: Path) -> str:
    pkg_dir = Path(pkg_dir)
    files = _git_files(pkg_dir)
    if files is None:
        files = _walk(pkg_dir)
    entries = []
    for rel in files:
        if _skip(rel):
            continue
        try:
            entries.append((rel, (pkg_dir / rel).read_bytes()))
        except OSError:                         # listed but deleted (git --cached): not shipped
            continue
    return _digest(entries)


def compute_wheel(wheel: Path) -> str:
    """The id of the `nightwright/` entries of a built wheel (not `*.dist-info`)."""
    entries = []
    with zipfile.ZipFile(wheel) as z:
        for n in z.namelist():
            if n.startswith(WHEEL_ROOT) and not n.endswith("/") and not _skip(n):
                entries.append((n[len(WHEEL_ROOT):], z.read(n)))
    return _digest(entries)


def local() -> str:
    import nightwright
    return compute(Path(nightwright.__file__).parent)
