"""Agent workflow skills installer (SPEC-0017): repo-versioned playbooks under `skills/<name>/
SKILL.md` (thin markdown over the `nw` CLI — see docs/specs/0017-agent-workflow-skills.md) get
installed into `~/.claude/skills/` (or any --dest) so a fresh agent session can drive `nw`
without reading its code.

`install()` is idempotent: default mode symlinks `dest/<name>` straight at the repo's
`skills/<name>/` dir (so repo edits propagate immediately); `--copy` makes a portable snapshot
and stamps it with a content hash (in a `.nw-skill.json` marker) so `status()` can report a
stale copy once the repo source has moved on. Installing refuses to clobber a same-named
directory at dest that isn't already nightwright-managed, unless force=True.

Tests MUST pass a tmp `dest` — never install into (or list) the real ~/.claude/skills."""
import hashlib
import json
import os
import shutil
from pathlib import Path

_MARKER = ".nw-skill.json"


def repo_skills_dir(cfg=None):
    """`<nightwright checkout>/skills`, or None when the checkout cannot be resolved (SPEC-0166: config
    `nw_checkout` or a `uv run` / editable install)."""
    from .rules import nw_checkout
    root = nw_checkout(cfg)
    return root / "skills" if root else None


def default_dest():
    return Path.home() / ".claude" / "skills"


def repo_skills(source=None):
    """Sorted [(name, path)] for each <source>/<name>/ dir that has a SKILL.md. `source`
    defaults to `repo_skills_dir()`."""
    d = Path(source) if source else repo_skills_dir()
    if d is None or not d.is_dir():
        return []
    return [(p.name, p) for p in sorted(d.iterdir())
            if p.is_dir() and (p / "SKILL.md").is_file()]


def _dir_hash(path):
    """Stable content hash of a skill dir, used to detect a stale --copy install."""
    h = hashlib.sha256()
    for f in sorted(path.rglob("*")):
        if f.is_file():
            h.update(f.relative_to(path).as_posix().encode())
            h.update(f.read_bytes())
    return h.hexdigest()


def _is_our_symlink(target, src):
    return target.is_symlink() and target.resolve() == src.resolve()


def _is_our_copy(target):
    return (target / _MARKER).is_file()


def _remove(target):
    if target.is_symlink() or target.is_file():
        target.unlink()
    elif target.is_dir():
        shutil.rmtree(target)


def install(dest=None, mode="link", force=False, source=None):
    """Install every repo skill into dest/<name>. mode is "link" (default, symlink to the
    repo) or "copy" (snapshot + hash marker). Returns [(name, action)] where action is one of
    linked/copied/relinked/updated/up-to-date. Raises ValueError if dest/<name> exists and
    isn't already nightwright-managed and force is falsy."""
    if mode not in ("link", "copy"):
        raise ValueError(f"unknown mode: {mode!r} (expected 'link' or 'copy')")
    dest = Path(dest or default_dest())
    dest.mkdir(parents=True, exist_ok=True)
    results = []
    for name, src in repo_skills(source):
        target = dest / name
        existed = target.exists() or target.is_symlink()
        ours = existed and (_is_our_symlink(target, src) or _is_our_copy(target))
        if existed and not ours and not force:
            raise ValueError(
                f"{target} already exists and is not a nightwright-managed skill directory "
                f"(pass --force to overwrite)")

        if mode == "copy":
            if existed and _is_our_copy(target):
                current = json.loads((target / _MARKER).read_text(encoding="utf-8"))
                if current.get("hash") == _dir_hash(src):
                    results.append((name, "up-to-date"))
                    continue
            if existed:
                _remove(target)
            shutil.copytree(src, target)
            (target / _MARKER).write_text(
                json.dumps({"source": str(src), "hash": _dir_hash(src)}), encoding="utf-8")
            results.append((name, "updated" if existed else "copied"))
        else:
            if existed and _is_our_symlink(target, src):
                results.append((name, "up-to-date"))
                continue
            if existed:
                _remove(target)
            target.symlink_to(src, target_is_directory=True)
            results.append((name, "relinked" if existed else "linked"))
    return results


LEGACY_PREFIX, PREFIX = "wt-", "nw-"
_LEGACY_MARKER = ".wt-skill.json"


def _points_at_repo_skill(link: Path, src_dir: Path, new_name: str) -> bool:
    """True when the symlink `link` targets this repo's skills dir, or (the checkout having
    moved, or the dir having been renamed `wt-x` -> `nw-x`) its target path is
    `.../skills/wt-<x>` for a skill this repo now ships as `nw-<x>`."""
    raw = Path(os.readlink(link))
    if not raw.is_absolute():
        raw = link.parent / raw
    try:
        if raw.resolve().parent == src_dir.resolve():
            return True
    except OSError:
        pass
    return raw.parent.name == "skills" and raw.name == LEGACY_PREFIX + new_name[len(PREFIX):]


def remove_legacy(dest=None, source=None) -> list[str]:
    """SPEC-0330: remove pre-rename `dest/wt-<x>` installs of skills this repo now ships as
    `nw-<x>`: a symlink into this repo's skills dir, or one whose target is `.../skills/wt-<x>`
    (the old checkout, usually dangling after the move), or a `--copy` install stamped with the
    old `.wt-skill.json` marker. Anything else named `wt-*` is left alone. Returns removed names."""
    dest = Path(dest or default_dest())
    src_dir = Path(source) if source else repo_skills_dir()
    if src_dir is None or not dest.is_dir():
        return []
    removed = []
    for name, _src in repo_skills(src_dir):
        if not name.startswith(PREFIX):
            continue
        old = dest / (LEGACY_PREFIX + name[len(PREFIX):])
        if old.is_symlink():
            if _points_at_repo_skill(old, src_dir, name):
                old.unlink()
                removed.append(old.name)
        elif old.is_dir() and (old / _LEGACY_MARKER).is_file():
            shutil.rmtree(old)
            removed.append(old.name)
    return removed


def status(dest=None, source=None):
    """[{name, source, state}] for every repo skill, describing dest/<name>'s install state:
    "not installed", "linked", "linked (other)" (a symlink to something else), "copied",
    "copied (stale)", or "occupied (not ours)" (a plain dir we didn't create)."""
    dest = Path(dest or default_dest())
    out = []
    for name, src in repo_skills(source):
        target = dest / name
        entry = {"name": name, "source": str(src)}
        if not (target.exists() or target.is_symlink()):
            entry["state"] = "not installed"
        elif target.is_symlink():
            entry["state"] = "linked" if _is_our_symlink(target, src) else "linked (other)"
        elif _is_our_copy(target):
            current = json.loads((target / _MARKER).read_text(encoding="utf-8"))
            entry["state"] = ("copied" if current.get("hash") == _dir_hash(src)
                              else "copied (stale)")
        else:
            entry["state"] = "occupied (not ours)"
        out.append(entry)
    return out
