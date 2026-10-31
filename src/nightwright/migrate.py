"""`nw migrate-from-wt` (SPEC-0330): move a pre-rename `wt` install's config and data to the
nightwright locations and rewrite the operational paths that the rename moved.

What it does, idempotently (a second run finds nothing to do):

1. moves `<XDG_CONFIG_HOME|~/.config>/wt` -> the nightwright config dir and
   `<XDG_DATA_HOME|~/.local/share>/wt` -> the nightwright data dir (`paths.config_dir()` /
   `paths.data_dir()`); refuses, changing nothing, when a target exists and is not empty;
2. rewrites, in the (moved) config's YAML files, values naming the old data/config dir, the old
   checkout (`--old-checkout` -> `--new-checkout`) and `.wt-worktrees`; renames the config key
   `wt_checkout` -> `nw_checkout` and the export scheme `wt-native` -> `nw-native`;
3. rewrites, in every campaign `state.json` under `<data>/agentic/campaigns/`, each string value
   that IS a path (starts with one of the moved prefixes): the repo root, the worktree, gate
   cwds, research roots; `.wt-worktrees` path segments become `.nw-worktrees`. Branch names
   (`wt/<campaign>`) stay: their git refs exist. Ledgers and other history files are never
   rewritten: readers accept the legacy `wt.<x>.v1` schema ids;
4. rewrites the checkout prefix of the spec-reservation store's keys (`<git common dir>::<dir>`),
   so live reservations still match the moved repository.

Dry-run unless `apply=True`. It never touches `~/.claude` (that is `nw skills install`)."""
from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from . import paths

DEFAULT_OLD_CHECKOUT = "/home/bruno/work/wt"
DEFAULT_NEW_CHECKOUT = "/home/bruno/work/nightwright"
OLD_WORKTREE_DIR, NEW_WORKTREE_DIR = ".wt-worktrees", ".nw-worktrees"
_CONFIG_SUFFIXES = (".yaml", ".yml")


def legacy_config_dir() -> str:
    base = os.environ.get("XDG_CONFIG_HOME")
    return os.path.join(os.path.expanduser(base), "wt") if base else os.path.expanduser("~/.config/wt")


def legacy_data_dir() -> str:
    base = os.environ.get("XDG_DATA_HOME")
    return (os.path.join(os.path.expanduser(base), "wt") if base
            else os.path.expanduser("~/.local/share/wt"))


class MigrationRefused(Exception):
    """A precondition failed; nothing was changed."""


@dataclass
class Plan:
    old_config: Path
    new_config: Path
    old_data: Path
    new_data: Path
    old_checkout: str
    new_checkout: str
    moves: list = field(default_factory=list)        # [(src, dst)]
    rewrites: list = field(default_factory=list)     # [{"file", "changes": [(old, new)]}]
    notes: list = field(default_factory=list)

    def empty(self) -> bool:
        return not self.moves and not self.rewrites

    def as_dict(self) -> dict:
        return {"schema": "nw.migrate_from_wt.v1",
                "moves": [{"from": str(a), "to": str(b)} for a, b in self.moves],
                "rewrites": [{"file": str(r["file"]),
                              "changes": [{"from": a, "to": b} for a, b in r["changes"]]}
                             for r in self.rewrites],
                "notes": list(self.notes)}


def _nonempty(p: Path) -> bool:
    return p.is_dir() and any(p.iterdir())


def _path_prefixes(plan: Plan) -> list[tuple[str, str]]:
    """(old, new) path prefixes, longest first, so a nested prefix wins."""
    pairs = [(str(plan.old_data), str(plan.new_data)), (str(plan.old_config), str(plan.new_config)),
             (plan.old_checkout.rstrip("/"), plan.new_checkout.rstrip("/"))]
    home = os.path.expanduser("~")
    for a, b in list(pairs):            # the `~/...` spelling of each, as configs often use it
        if a.startswith(home + "/") and b.startswith(home + "/"):
            pairs.append(("~" + a[len(home):], "~" + b[len(home):]))
    pairs = [(a, b) for a, b in pairs if a and a != b]
    return sorted(set(pairs), key=lambda ab: -len(ab[0]))


def _rewrite_path(value: str, prefixes) -> str:
    """`value` when it is not a path under a moved prefix; else the moved path. A prefix only
    matches at a path boundary (`/home/x/wt` never matches `/home/x/wt-other`)."""
    out = value
    for a, b in prefixes:
        if out == a or out.startswith(a + "/"):
            out = b + out[len(a):]
            break
    if out.startswith(("/", "~")):
        out = re.sub(r"(?<=/)" + re.escape(OLD_WORKTREE_DIR) + r"(?=/|$)", NEW_WORKTREE_DIR, out)
    return out


def _rewrite_config_text(text: str, prefixes) -> tuple[str, list]:
    """Line-wise rewrite of a YAML config, keeping comments and layout."""
    changes, out = [], []
    for line in text.splitlines(keepends=True):
        new = line
        for a, b in prefixes:
            new = re.sub(re.escape(a) + r"(?=/|$|[\s\"',\]}#])", b.replace("\\", "\\\\"), new)
        new = re.sub(r"(?<=/)" + re.escape(OLD_WORKTREE_DIR) + r"(?=/|$|[\s\"',\]}#])",
                     NEW_WORKTREE_DIR, new)
        new = re.sub(r"^(\s*)wt_checkout(\s*:)", r"\1nw_checkout\2", new)
        new = re.sub(r"^(\s*scheme\s*:\s*[\"']?)wt-native\b", r"\1nw-native", new)
        if new != line:
            changes.append((line.rstrip("\n"), new.rstrip("\n")))
        out.append(new)
    return "".join(out), changes


def _walk_json(obj, prefixes, changes):
    if isinstance(obj, dict):
        return {k: _walk_json(v, prefixes, changes) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_walk_json(v, prefixes, changes) for v in obj]
    if isinstance(obj, str):
        new = _rewrite_path(obj, prefixes)
        if new != obj:
            changes.append((obj, new))
        return new
    return obj


def _config_files(root: Path):
    if not root.is_dir():
        return []
    return sorted(p for p in root.rglob("*") if p.is_file() and p.suffix in _CONFIG_SUFFIXES
                  and ".bak" not in p.name)


def _plan_rewrites(plan: Plan, config_root: Path, data_root: Path):
    """Rewrites computed against where the files are now (`*_root`), recorded at the path they
    will have after the moves."""
    prefixes = _path_prefixes(plan)
    for f in _config_files(config_root):
        text = f.read_text(encoding="utf-8")
        new, changes = _rewrite_config_text(text, prefixes)
        if changes:
            plan.rewrites.append({"file": plan.new_config / f.relative_to(config_root),
                                  "changes": changes, "kind": "text", "content": new})
    campaigns = data_root / "agentic" / "campaigns"
    states = sorted(campaigns.glob("*/state.json")) if campaigns.is_dir() else []
    for f in states:
        try:
            doc = json.loads(f.read_text(encoding="utf-8"))
        except ValueError:
            plan.notes.append(f"skipped {f}: not valid JSON")
            continue
        changes = []
        new = _walk_json(doc, prefixes, changes)
        if changes:
            plan.rewrites.append({"file": plan.new_data / f.relative_to(data_root),
                                  "changes": changes, "kind": "json", "content": new})
    store = data_root / "spec-reservations.json"
    if store.is_file():
        try:
            doc = json.loads(store.read_text(encoding="utf-8") or "{}")
        except ValueError:
            doc = None
            plan.notes.append(f"skipped {store}: not valid JSON")
        if isinstance(doc, dict) and isinstance(doc.get("reservations"), list):
            changes = []
            for r in doc["reservations"]:
                key = r.get("key") if isinstance(r, dict) else None
                if isinstance(key, str) and "::" in key:
                    repo, _, rel = key.partition("::")
                    moved = _rewrite_path(repo, prefixes)
                    if moved != repo:
                        r["key"] = f"{moved}::{rel}"
                        changes.append((key, r["key"]))
            if changes:
                plan.rewrites.append({"file": plan.new_data / store.name, "changes": changes,
                                      "kind": "json", "content": doc})


def plan(*, old_config=None, new_config=None, old_data=None, new_data=None,
         old_checkout=DEFAULT_OLD_CHECKOUT, new_checkout=DEFAULT_NEW_CHECKOUT) -> Plan:
    """What `apply` would do. Raises MigrationRefused when a target dir exists and is not empty
    while its source still exists."""
    p = Plan(old_config=Path(os.path.expanduser(old_config or legacy_config_dir())),
             new_config=Path(os.path.expanduser(new_config or paths.config_dir())),
             old_data=Path(os.path.expanduser(old_data or legacy_data_dir())),
             new_data=Path(os.path.expanduser(new_data or paths.data_dir())),
             old_checkout=os.path.expanduser(old_checkout),
             new_checkout=os.path.expanduser(new_checkout))
    for src, dst in ((p.old_config, p.new_config), (p.old_data, p.new_data)):
        if src.resolve() == dst.resolve():
            raise MigrationRefused(f"source and target are the same directory: {src}")
        if src.exists():
            if _nonempty(dst):
                raise MigrationRefused(
                    f"{dst} already exists and is not empty while {src} still exists; "
                    f"move or merge one of them by hand, then re-run")
            p.moves.append((src, dst))
        elif dst.exists():
            p.notes.append(f"{src} is gone and {dst} exists: already moved")
        else:
            p.notes.append(f"{src} does not exist: nothing to move")
    config_root = p.old_config if p.old_config.exists() else p.new_config
    data_root = p.old_data if p.old_data.exists() else p.new_data
    _plan_rewrites(p, config_root, data_root)
    return p


def _atomic_write(path: Path, text: str):
    tmp = path.with_name(f".{path.name}.migrate.tmp")
    tmp.write_text(text, encoding="utf-8")
    if path.exists():
        os.chmod(tmp, path.stat().st_mode & 0o777)
    os.replace(tmp, path)


def apply(p: Plan) -> Plan:
    """Carry out `p` (from `plan()`): the moves first, then the rewrites at the new paths."""
    for src, dst in p.moves:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():                 # plan() verified it is empty
            dst.rmdir()
        try:
            os.rename(src, dst)
        except OSError:
            shutil.move(str(src), str(dst))
    for r in p.rewrites:
        f = Path(r["file"])
        if r["kind"] == "json":
            _atomic_write(f, json.dumps(r["content"], indent=2, ensure_ascii=False) + "\n")
        else:
            _atomic_write(f, r["content"])
    return p
