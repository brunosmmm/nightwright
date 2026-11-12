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
from .legacy import schema_ok

from .specs import _NUM_RE, _specs_dir

STORE_SCHEMA = "nw.spec.reservations.store.v1"
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
    nums = set()
    for path in _worktree_paths(top):
        nums |= _dir_numbers(Path(path) / rel_dir)
    return nums


def _scan_refs(top):
    """`[(ref, commit sha)]` for every local and remote-tracking ref (SPEC-0569), symbolic
    `*/HEAD` refs skipped, from one `git for-each-ref`."""
    out = _git(top, "for-each-ref", "--format=%(refname) %(objectname)",
               "refs/heads/", "refs/remotes/") or ""
    refs = []
    for line in out.splitlines():
        ref, _, sha = line.partition(" ")
        if ref and sha and not ref.endswith("/HEAD"):
            refs.append((ref, sha))
    return refs


def _ref_files(top, rel_dir, sha, cache):
    """Filenames under `rel_dir` at commit `sha`; one `git ls-tree` per distinct commit."""
    if sha not in cache:
        listing = _git(top, "ls-tree", "-z", "--name-only", sha, "--", f"{rel_dir}/") or ""
        cache[sha] = [os.path.basename(n) for n in listing.split("\0") if n]
    return cache[sha]


class _RefScan:
    """One scan of every ref's spec filenames, built lazily on first use and shared by every
    consumer in a public call. `files` is `[(ref, [filename, ...])]`."""

    def __init__(self, top, rel_dir):
        self._top, self._rel_dir, self._files = top, rel_dir, None

    @property
    def files(self):
        if self._files is None:
            cache: dict = {}
            self._files = [(ref, _ref_files(self._top, self._rel_dir, sha, cache))
                           for ref, sha in _scan_refs(self._top)]
        return self._files

    def numbers(self):
        nums = set()
        for _, names in self.files:
            nums |= _numbers(names)
        return nums


def _worktree_paths(top):
    out = _git(top, "worktree", "list", "--porcelain") or ""
    paths = []
    for block in out.split("\n\n"):
        lines = block.splitlines()
        path = next((ln[len("worktree "):] for ln in lines if ln.startswith("worktree ")), None)
        if path is None or any(ln.split(" ", 1)[0] == "prunable" for ln in lines):
            continue
        if os.path.isdir(path):
            paths.append(os.path.realpath(path))
    return paths


def _number_files(top, rel_dir, scan):
    """`{number: {filename: [location, ...]}}` over every worktree's working tree (location
    `worktree:<path>`) and every local and remote-tracking ref (location: the ref name)."""
    out: dict = {}

    def add(name, location):
        if name.endswith(".md") and (m := _NUM_RE.match(name)):
            out.setdefault(int(m.group(1)), {}).setdefault(name, []).append(location)

    for path in _worktree_paths(top):
        try:
            names = [p.name for p in (Path(path) / rel_dir).glob("*.md")]
        except OSError:
            continue
        for name in names:
            add(name, f"worktree:{path}")
    for ref, names in scan.files:
        short = ref.split("/", 2)[2]
        for name in names:
            add(name, short)
    return out


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
    if (not isinstance(data, dict) or not schema_ok(data.get("schema"), STORE_SCHEMA)
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
    from .agentic import store as agentic_store       # lazy: plain `nw spec` skips the package
    try:
        # SPEC-0290: a split campaign reserves one number per part, keyed `<campaign>#k`
        return agentic_store.read_state(cfg, campaign.split("#")[0]).get("state") == "ABANDONED"
    except ValueError:
        return False


def _prune(cfg, records, key, scan):
    """Drop reservations whose number is committed on a ref (this key only) or whose campaign
    is ABANDONED. `scan` is a `_RefScan` (None outside git); it is only read, so a store with
    no record for the key costs no git command. Returns (live, changed)."""
    committed = None
    live = []
    for r in records:
        if r["key"] == key and scan is not None:
            if committed is None:
                committed = scan.numbers()
            if r["number"] in committed:
                continue
        if r.get("campaign") and _abandoned(cfg, r["campaign"]):
            continue
        live.append(r)
    return live, len(live) != len(records)


# ---- public API ----------------------------------------------------------------------------

def allocate(cfg, *, by, note=None, campaign=None, specs_dir=None) -> dict:
    """Reserve and return the next free spec number as a reservation record."""
    specs_dir, key, top, rel_dir = _resolve(cfg, specs_dir)
    scan = _RefScan(top, rel_dir) if top else None
    with _locked(cfg):
        records, changed = _prune(cfg, _load(cfg), key, scan)
        if campaign:
            for r in records:
                if r["key"] == key and r.get("campaign") == campaign:
                    if changed:
                        _save(cfg, records)
                    return r
        used = {r["number"] for r in records if r["key"] == key}
        if top:
            used |= _worktree_numbers(top, rel_dir) | scan.numbers()
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
    scan = _RefScan(top, rel_dir) if top else None
    with _locked(cfg):
        records, changed = _prune(cfg, _load(cfg), key, scan)
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
    scan = _RefScan(top, rel_dir) if top else None
    with _locked(cfg):
        records, _ = _prune(cfg, _load(cfg), key, scan)
        hit = next((r for r in records if r["key"] == key and r["number"] == n), None)
        if hit is None:
            _save(cfg, records)
            raise ValueError(f"SPEC-{n:04d} is not reserved under {key}")
        _save(cfg, [r for r in records if r is not hit])
        return hit


# ---- collision checks (SPEC-0569), read-only -----------------------------------------------

def _base_campaign(campaign):
    return campaign.split("#")[0] if campaign else None


def _tree_campaign(tree):
    """The base campaign id when `tree` is on branch `nw/<id>` (split suffix stripped)."""
    branch = (_git(tree, "rev-parse", "--abbrev-ref", "HEAD") or "").strip()
    return _base_campaign(branch[len("nw/"):]) if branch.startswith("nw/") else None


def _own_refs(tree):
    """Short names of the refs `tree` stands on: its local branch and its upstream (empty when
    detached or without an upstream). A slug rename there is not a collision."""
    own = set()
    for rev in ("HEAD", "@{upstream}"):
        name = (_git(tree, "rev-parse", "--abbrev-ref", rev) or "").strip()
        if name and name != "HEAD":
            own.add(name)
    return own


def collisions(cfg, *, specs_dir=None, tree=None, campaign=None) -> list:
    """Spec files of `tree` (default: the checkout holding `specs_dir`) whose number is used by
    a different filename on another worktree or ref ("filename"), or held by a live reservation
    of another campaign ("reservation"). Read-only: takes no lock and never creates or writes
    the store (`_save` replaces it atomically, so an unlocked read is whole); sorted by number.

    A leftover or renamed-slug ref counts: delete the stale branch to clear it. The tree's own
    branch and its upstream do not: a rename there is the tree's own change."""
    specs_dir, key, top, rel_dir = _resolve(cfg, specs_dir)
    if not top:
        return []
    tree = os.path.realpath(tree) if tree is not None else top
    campaign = _base_campaign(campaign) or _tree_campaign(tree)
    own = {f"worktree:{tree}"} | _own_refs(tree)
    scan = _RefScan(top, rel_dir)
    seen = _number_files(top, rel_dir, scan)
    records, _ = _prune(cfg, _load(cfg), key, scan)
    held = {r["number"]: r for r in records
            if r["key"] == key and r.get("campaign")
            and _base_campaign(r["campaign"]) != campaign}
    out = []
    for p in sorted(Path(tree, rel_dir).glob("*.md")):
        m = _NUM_RE.match(p.name)
        if not m:
            continue
        n = int(m.group(1))
        base = {"number": n, "id": f"SPEC-{n:04d}", "file": f"{rel_dir}/{p.name}"}
        others = {f: [loc for loc in locs if loc not in own]
                  for f, locs in seen.get(n, {}).items() if f != p.name}
        others = {f: locs for f, locs in others.items() if locs}
        if others:
            out.append({**base, "kind": "filename",
                        "other": sorted({loc for locs in others.values() for loc in locs}),
                        "other_files": sorted(others)})
        if n in held:
            out.append({**base, "kind": "reservation",
                        "other": [_base_campaign(held[n]["campaign"])]})
    return sorted(out, key=lambda c: (c["number"], c["kind"]))


def number_taken(cfg, number, *, specs_dir=None, exclude_campaign=None) -> list:
    """Locations (`worktree:<path>` or ref names) where any filename carries `number`, ignoring
    the worktree holding `specs_dir` and the campaign's own `nw/<id>` ref. Needs no file in the
    checked tree, never reads reservations or writes the store; `[]` means free of files."""
    n = parse_number(number)
    _, _, top, rel_dir = _resolve(cfg, specs_dir)
    if not top:
        return []
    own = f"worktree:{top}"
    base = _base_campaign(exclude_campaign)
    locs = set()
    for names in _number_files(top, rel_dir, _RefScan(top, rel_dir)).get(n, {}).values():
        for loc in names:
            if loc == own or (base and (loc == f"nw/{base}" or loc.endswith(f"/nw/{base}"))):
                continue
            locs.add(loc)
    return sorted(locs)
