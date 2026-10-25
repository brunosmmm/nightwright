"""Spec-number allocator (SPEC-0208): reservations that see every branch and worktree.

`specs.next_spec_number` only lists one directory, so concurrent writers (sessions, headless
spec writers in campaign worktrees) collide. `allocate` instead takes the max over every
worktree's working tree, every local branch and the live reservations for the same
(repository, spec directory) key, under a cross-process flock, and records the new number in
`<data_dir>/spec-reservations.json`.

A reservation is pruned once its number is committed on a local branch (the branch scan then
keeps it used) or its campaign is ABANDONED. Outside a git repository the key is the realpath
of the directory and nothing but `release` prunes."""
import contextlib
import datetime as dt
import fcntl
import json
import os
import re
import subprocess
from pathlib import Path

from .specs import _NUM_RE, _specs_dir

STORE_SCHEMA = "wt.spec.reservations.store.v1"
_SPEC_ID_RE = re.compile(r"^(?:SPEC-)?(\d+)$", re.I)


# ---- key + scan ----------------------------------------------------------------------------

def _git(cwd, *args):
    """stdout of `git -C cwd args`, or None when git fails or is missing."""
    try:
        r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    except OSError:
        return None
    return r.stdout if r.returncode == 0 else None


def _resolve(cfg, specs_dir):
    """(specs_dir, key, top, rel_dir); `top`/`rel_dir` are None outside a git repository."""
    specs_dir = Path(os.path.realpath(specs_dir if specs_dir is not None else _specs_dir(cfg)))
    probe = specs_dir
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    top = (_git(probe, "rev-parse", "--show-toplevel") or "").strip()
    common = (_git(probe, "rev-parse", "--path-format=absolute", "--git-common-dir")
              or "").strip()
    if not top or not common:
        return specs_dir, str(specs_dir), None, None
    top = os.path.realpath(top)
    rel_dir = os.path.relpath(specs_dir, top)
    return specs_dir, f"{os.path.realpath(common)}::{rel_dir}", top, rel_dir


def _numbers(names):
    return {int(m.group(1)) for n in names if (m := _NUM_RE.match(n))}


def _dir_numbers(d):
    try:
        return _numbers(p.name for p in Path(d).glob("*.md"))
    except OSError:
        return set()


def _worktree_numbers(top, rel_dir):
    out = _git(top, "worktree", "list", "--porcelain") or ""
    nums = set()
    for block in out.split("\n\n"):
        lines = block.splitlines()
        path = next((ln[len("worktree "):] for ln in lines if ln.startswith("worktree ")), None)
        if path is None or any(ln.split(" ", 1)[0] == "prunable" for ln in lines):
            continue
        if os.path.isdir(path):
            nums |= _dir_numbers(Path(path) / rel_dir)
    return nums


def _branch_numbers(top, rel_dir):
    refs = (_git(top, "for-each-ref", "--format=%(refname)", "refs/heads/") or "").split()
    nums = set()
    for ref in refs:
        listing = _git(top, "ls-tree", "-z", "--name-only", ref, "--", f"{rel_dir}/") or ""
        nums |= _numbers(os.path.basename(n) for n in listing.split("\0") if n)
    return nums


# ---- store ---------------------------------------------------------------------------------

def _paths(cfg):
    d = Path(cfg["data_dir"])
    return d / "spec-reservations.json", d / "spec-reservations.lock"


@contextlib.contextmanager
def _locked(cfg):
    store, lock = _paths(cfg)
    store.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)                      # closing drops the flock


def _load(cfg) -> list:
    store, _ = _paths(cfg)
    fix = f"inspect {store}, then delete it"
    try:
        text = store.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    if not text.strip():
        return []
    try:
        data = json.loads(text)
    except ValueError as e:
        raise ValueError(f"spec reservation store {store} is not valid JSON ({e}); {fix}")
    if (not isinstance(data, dict) or data.get("schema") != STORE_SCHEMA
            or not isinstance(data.get("reservations"), list)):
        raise ValueError(f"spec reservation store {store} has the wrong schema "
                         f"(want {STORE_SCHEMA}); {fix}")
    return data["reservations"]


def _save(cfg, records):
    store, _ = _paths(cfg)
    tmp = store.with_name(f"{store.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps({"schema": STORE_SCHEMA, "reservations": records}, indent=2)
                   + "\n", encoding="utf-8")
    os.replace(tmp, store)


def _abandoned(cfg, campaign) -> bool:
    from .agentic import store as agentic_store       # lazy: plain `wt spec` skips the package
    try:
        return agentic_store.read_state(cfg, campaign).get("state") == "ABANDONED"
    except ValueError:
        return False


def _prune(cfg, records, key, top, rel_dir):
    """Drop reservations whose number is committed on a local branch (this key only) or whose
    campaign is ABANDONED. Returns (live, changed)."""
    committed = _branch_numbers(top, rel_dir) if top else set()
    live = []
    for r in records:
        if r["key"] == key and r["number"] in committed:
            continue
        if r.get("campaign") and _abandoned(cfg, r["campaign"]):
            continue
        live.append(r)
    return live, len(live) != len(records)


# ---- public API ----------------------------------------------------------------------------

def allocate(cfg, *, by, note=None, campaign=None, specs_dir=None) -> dict:
    """Reserve and return the next free spec number as a reservation record."""
    specs_dir, key, top, rel_dir = _resolve(cfg, specs_dir)
    with _locked(cfg):
        records, changed = _prune(cfg, _load(cfg), key, top, rel_dir)
        if campaign:
            for r in records:
                if r["key"] == key and r.get("campaign") == campaign:
                    if changed:
                        _save(cfg, records)
                    return r
        used = {r["number"] for r in records if r["key"] == key}
        if top:
            used |= _worktree_numbers(top, rel_dir) | _branch_numbers(top, rel_dir)
        else:
            used |= _dir_numbers(specs_dir)
        number = max(used, default=0) + 1
        rec = {"number": number, "id": f"SPEC-{number:04d}", "key": key, "by": by,
               "note": note, "campaign": campaign,
               "at": dt.datetime.now().astimezone().isoformat(timespec="seconds")}
        _save(cfg, records + [rec])
        return rec


def list_reservations(cfg, *, specs_dir=None, all_keys=False) -> list:
    """Live reservations for the key (every key with `all_keys`), ordered by number."""
    _, key, top, rel_dir = _resolve(cfg, specs_dir)
    with _locked(cfg):
        records, changed = _prune(cfg, _load(cfg), key, top, rel_dir)
        if changed:
            _save(cfg, records)
    return sorted((r for r in records if all_keys or r["key"] == key),
                  key=lambda r: (r["number"], r["key"]))


def parse_number(number) -> int:
    m = _SPEC_ID_RE.match(str(number).strip())
    if not m:
        raise ValueError(f"not a spec number: {number!r} (want NNNN or SPEC-NNNN)")
    return int(m.group(1))


def release(cfg, number, *, specs_dir=None) -> dict:
    """Delete the reservation for `number` under the key; ValueError when there is none."""
    n = parse_number(number)
    _, key, top, rel_dir = _resolve(cfg, specs_dir)
    with _locked(cfg):
        records, _ = _prune(cfg, _load(cfg), key, top, rel_dir)
        hit = next((r for r in records if r["key"] == key and r["number"] == n), None)
        if hit is None:
            _save(cfg, records)
            raise ValueError(f"SPEC-{n:04d} is not reserved under {key}")
        _save(cfg, [r for r in records if r is not hit])
        return hit
