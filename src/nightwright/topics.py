"""Topic resolution: from an event's cwd/branch to a canonical BASE topic
(JIRA key / repo / repo:branch / loose label), with overrides applied first."""
import os
import re
import subprocess

KEY_RE = re.compile(r"([A-Z]{2,}-\d+)")
JIRA_RE = re.compile(r"^[A-Z]{2,}-\d+$")


def _decode_cursor_project_path(folder_name):
    """Recover the real filesystem path from a Cursor project folder name.

    Cursor encodes the workspace path by stripping the leading '/' and replacing
    every '/' with '-', so '/home/user/work/Demo-ATS' becomes
    'home-user-work-Demo-ATS'. Hyphens inside directory names create
    ambiguity; we resolve it by greedily walking the filesystem, trying
    progressively longer hyphen-joined segments until isdir() succeeds."""
    parts = folder_name.split("-")

    def walk(idx, current):
        if idx == len(parts):
            return current
        for end in range(idx + 1, len(parts) + 1):
            seg = "-".join(parts[idx:end])
            candidate = os.path.join(current, seg)
            if end == len(parts):
                return candidate          # last option — return as-is even if missing
            if os.path.isdir(candidate):
                result = walk(end, candidate)
                if result is not None:
                    return result
        return None

    return walk(0, "/")


class RepoResolver:
    """git rev-parse --show-toplevel, cached per cwd."""
    def __init__(self, cfg):
        self.cache = {}
        self.work = cfg["_work"]
        self.home = cfg["_home"]

    def repo(self, cwd):
        if cwd in self.cache:
            return self.cache[cwd]
        root = None
        if cwd and os.path.isdir(cwd):
            try:
                r = subprocess.run(["git", "-C", cwd, "rev-parse", "--show-toplevel"],
                                   capture_output=True, text=True, timeout=5)
                if r.returncode == 0:
                    root = r.stdout.strip()
            except Exception:
                root = None
        self.cache[cwd] = root
        return root

    def loose_label(self, cwd):
        # collapse to the top-level folder under work_root/home (repo-like grain);
        # subdirs of a loose project group together. git-init a folder to track it apart.
        for base in (self.work, self.home):
            if cwd and cwd.startswith(base + "/"):
                return os.path.relpath(cwd, base).split(os.sep)[0]
        return (cwd or "?").lstrip("/")


def base_topic(cwd, branch, rr):
    """(topic, unclassified) from JIRA key / repo / repo:branch / loose label."""
    if not cwd:
        return "UNCLASSIFIED", True
    m = KEY_RE.search((branch or "").upper())
    if m:
        return m.group(1), False
    root = rr.repo(cwd)
    if root:
        repo = os.path.basename(root)
        b = (branch or "").split("/")[-1]
        return (repo if b in ("main", "master", "HEAD", "") else f"{repo}:{b}"), False
    return rr.loose_label(cwd), True


def resolve_topic(ev, overrides, rr):
    """Resolve an event to a BASE topic (overrides applied; facets are a report-time lens)."""
    for ov in overrides:
        if ev["session"] == ov["session"] and ov["_start"] <= ev["ts"] <= ov["_end"]:
            return ov["topic"], False
    return base_topic(ev["cwd"], ev["branch"], rr)
