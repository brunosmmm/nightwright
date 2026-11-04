"""SPEC-0415: helpers over the committed campaign-history fixture (`tests/fixtures/agentic_history/`).

`copy_fixture(dst)` gives a writable data dir (`dst/agentic/...`) usable as `cfg["data_dir"]`.
It uses plain copies, never links, so a test may write to them without touching the committed
fixture. No network or git calls; nothing imported from `nightwright`."""
from __future__ import annotations

import shutil
from pathlib import Path

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "agentic_history"


def copy_fixture(dst: Path) -> Path:
    """Copy `FIXTURE_DIR/agentic/` into `dst/agentic/` (MANIFEST.json is not copied) and return
    `dst`. `dst` must not exist or must be empty, else `FileExistsError`."""
    dst = Path(dst)
    if dst.exists() and any(dst.iterdir()):
        raise FileExistsError(f"{dst} is not empty")
    dst.mkdir(parents=True, exist_ok=True)
    shutil.copytree(FIXTURE_DIR / "agentic", dst / "agentic", copy_function=shutil.copyfile)
    return dst
