"""Release tarball: git archive + filtered idea slice (SPEC-0133)."""
from __future__ import annotations

import datetime as dt
import os
import subprocess
import tarfile
import tempfile
from pathlib import Path

from .idea_export import export_idea_subtrees


def _git_root(cwd: str | None = None) -> Path:
    r = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=cwd, capture_output=True, text=True, check=False,
    )
    if r.returncode != 0:
        raise ValueError("not a git checkout (git rev-parse failed)")
    return Path(r.stdout.strip())


def _git_short_sha(cwd: str | None = None) -> str:
    r = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=cwd, capture_output=True, text=True, check=False,
    )
    if r.returncode != 0:
        raise ValueError("could not read HEAD sha")
    return r.stdout.strip()


def build_release_archive(cfg, output: str | None = None, *, project: str = "Meta-Tools",
                          all_done: bool = True, cwd: str | None = None) -> str:
    """Write a release `.tar.gz` of git HEAD + exported idea slice. Returns output path."""
    root = _git_root(cwd)
    sha = _git_short_sha(str(root))
    day = dt.date.today().isoformat().replace("-", "")
    prefix = f"work-tracking-{sha}"
    if not output:
        output = str(root.parent / f"work-tracking-{day}-{sha}.tar.gz")
    output = os.path.expanduser(output)

    with tempfile.TemporaryDirectory(prefix="nw-release-") as tmp:
        stage = Path(tmp) / prefix
        stage.mkdir()
        # Extract tracked tree only
        proc = subprocess.run(
            ["git", "archive", f"--prefix={prefix}/", "HEAD"],
            cwd=str(root), capture_output=True, check=False,
        )
        if proc.returncode != 0:
            raise ValueError(f"git archive failed: {proc.stderr.decode()[:200]}")
        # Unpack into tmp (creates prefix/…)
        import io
        with tarfile.open(fileobj=io.BytesIO(proc.stdout), mode="r:") as tf:
            tf.extractall(tmp, filter="data")

        ideas_dir = stage / "ideas"
        ideas_dir.mkdir(parents=True, exist_ok=True)
        export_idea_subtrees(
            cfg, str(ideas_dir / "ideas.org"), dry_run=False,
            project=project, all_done=all_done,
        )

        parent = os.path.dirname(output)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with tarfile.open(output, "w:gz") as out:
            out.add(str(stage), arcname=prefix)
    return output
