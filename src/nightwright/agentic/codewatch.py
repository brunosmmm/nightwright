"""Code-change detection for long-lived nightwright processes (SPEC-0194, SPEC-0231).

The runner (`queue.run`) and the event/mailbox followers run from a git checkout of nightwright. When a
merge moves that checkout's HEAD they hold stale code: the runner re-execs, a follower exits
with a `code_changed` line so its session re-arms the Monitor (and re-reads changed skills).
Lives apart from `queue.py` because `events.py` cannot import `queue.py` at module load."""
from __future__ import annotations

import subprocess
import time
from pathlib import Path

_CODE_PATHS = ("src/nightwright", "pyproject.toml")          # runner (SPEC-0194)
FOLLOWER_PATHS = _CODE_PATHS + ("skills",)           # followers (SPEC-0231)

AUTO = object()          # follower `code_root` default: resolve via code_checkout()


def code_checkout() -> Path | None:
    """The git checkout this nightwright package is imported from, or None (not a checkout)."""
    root = Path(__file__).resolve().parents[3]
    return root if (root / ".git").exists() and (root / "src" / "nightwright").is_dir() else None


def _git_out(root, *args) -> str | None:
    try:
        r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                           timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def _changed_paths(root, since: str, head: str, paths) -> list[str] | None:
    """`paths` changed between two shas, or None when `git diff` fails (unlike an empty diff)."""
    diff = _git_out(root, "-c", "core.quotePath=false", "diff", "--name-only", since, head,
                    "--", *paths)
    return None if diff is None else [p for p in diff.splitlines() if p]


def code_changes(root, since: str, paths=_CODE_PATHS) -> tuple[str | None, list[str] | None]:
    """(current HEAD, `paths` changed since `since`) for the checkout at `root`. The paths are
    None when HEAD moved but `git diff` failed: a change with unknown paths (SPEC-0262)."""
    head = _git_out(root, "rev-parse", "HEAD")
    if not head or head == since:
        return head, []
    return head, _changed_paths(root, since, head, paths)


class Watcher:
    """Polled by a follower loop: reports once when nightwright's code (or skills) changed on disk."""

    def __init__(self, root, check_s, now=None):
        self.root = root
        self.check_s = float(check_s or 0)
        self.sha = None
        if root is not None and self.check_s > 0:
            self.sha = _git_out(root, "rev-parse", "HEAD")
        self._last = time.monotonic() if now is None else now

    @property
    def enabled(self) -> bool:
        return self.sha is not None

    def poll(self, now_monotonic) -> dict | None:
        if not self.enabled or now_monotonic - self._last < self.check_s:
            return None
        self._last = now_monotonic
        head = _git_out(self.root, "rev-parse", "HEAD")
        if not head or head == self.sha:
            return None
        changed = _changed_paths(self.root, self.sha, head, FOLLOWER_PATHS)
        old, self.sha = self.sha, head
        if changed is None:                  # git diff failed: a change, reported once (SPEC-0262)
            return {"from_sha": old, "to_sha": head, "paths": [], "skills_changed": [],
                    "diff_failed": True}
        if not changed:                      # docs/specs/tests only: not our code
            return None
        skills = sorted({p.split("/")[1] for p in changed
                         if p.startswith("skills/") and p.count("/") >= 2})
        return {"from_sha": old, "to_sha": head, "paths": changed[:50],
                "skills_changed": skills}
