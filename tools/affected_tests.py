#!/usr/bin/env python3
"""Affected-test selection for nightwright's fix-loop gates (SPEC-0328).

Static test impact analysis, no coverage database: the changed files of the branch
(`git diff --name-only <merge-base(base, HEAD)>` plus uncommitted and untracked files) are
mapped to test modules through a static AST import graph over `src/nightwright` and `tests`:

- a changed test module selects itself;
- a changed src module selects its *nearest* tests: the test modules importing it directly,
  or, when none do, those at the first level of src importers that has any (breadth-first
  over the reverse import graph; `--depth N` always walks N src levels first). Imports
  anywhere in a module count, lazy function-level imports included. Hops through test
  helper modules (e.g. `test_agentic_e2e`) are free, and the CLI hubs (`HUBS`) are never
  walked past: they import nearly everything and half the test files import them;
- a changed test module selects itself and the test modules importing it;
- files that cannot be mapped safely select the FULL suite (see `FULL_TRIGGERS`), as does a src
  module no test reaches even through the hubs; `--strict` also caps the selection at
  `MAX_FRACTION` of test files (SPEC-0378: no cap otherwise, so a large selection runs as itself);
- (SPEC-0378, not with `--strict`) a nested `tests/<dir>/conftest.py` selects the tests below its
  directory; `pyproject.toml` / `uv.lock` select nothing when the diff against the merge base
  only touches metadata (`SAFE_PROJECT_KEYS`, `SAFE_TOOL_KEYS`) or the own package's lock version; a removed
  module selects the tests reaching the modules that still name it;
- (SPEC-0378, not with `--strict`) any other changed file (AGENTS.md, skills/**, docs/**) selects
  the test modules referencing it (the reference scan's needles, tests only); none selects
  nothing;
- (SPEC-0378) `NW_AFFECTED_FULL=1`, a non-empty `NW_SENTINEL`, or an `NW_CAMPAIGN` starting with
  `sentinel-` (the remote sentinel's job) force the full suite: the sentinel runs at the base
  itself, where the diff is empty;
- (SPEC-0357) a changed non-Python file under `tests/`, `src/nightwright/`, `.nw/` or `tools/`, and
  a changed `tools/*.py`, select the test modules that *reference* it (a string constant
  containing its basename, an import of its stem, or, when no module names the basename, a
  path-shaped parent-directory reference); an unreached src module selects the tests named
  after it (the name rule). `--strict` / `NW_AFFECTED_STRICT=1` restores the SPEC-0328 rules.

Usage:
    python3 tools/affected_tests.py [--base REF] [--repo DIR] [--include PATH]...
        prints the selection, one pytest path per line (`tests` for the full suite);
        the reason goes to stderr.
    python3 tools/affected_tests.py [--base REF] --run CMD [ARGS...]
        runs CMD with the selection appended (nothing appended for the full suite); an empty
        selection prints "no affected tests" and exits 0 without running CMD.
    --json [--closure] [--changed PATH]... (before --run): print the selection report as one JSON
        line on stdout (`--closure` adds the forward static closure; `--changed` replaces the git
        diff). With env NW_GATE_REPORT set the same report is also written to that path.

Stdlib only, so the gate can run it with the system `python3`.
"""
import argparse
import ast
import json
import os
import subprocess
import sys
from pathlib import Path

SRC_ROOT = "src"
SRC_PKG = "nightwright"
TESTS_DIR = "tests"
MAX_FRACTION = 0.40

# Path prefixes (or exact paths) whose change makes a static selection unsafe. SPEC-0378:
# `tests/waits.py` is mapped through its importers like any helper, and pyproject.toml /
# uv.lock go through `_config_change` (a metadata-only diff is not a trigger).
FULL_TRIGGERS = ("pyproject.toml", "uv.lock")
# pyproject.toml keys whose change cannot alter what the tests import or how pytest runs them.
SAFE_PROJECT_KEYS = frozenset({"version", "description", "readme", "authors", "maintainers",
                               "keywords", "classifiers", "urls", "license", "license-files"})
SAFE_TOOL_KEYS = frozenset({"ruff", "mypy", "black", "isort", "coverage", "codespell"})
SENTINEL_CAMPAIGN_PREFIX = "sentinel-"
# The SPEC-0328 triggers, restored by --strict / NW_AFFECTED_STRICT=1.
STRICT_FULL_TRIGGERS = ("pyproject.toml", "uv.lock", "tools/", ".nw/", "tests/waits.py")
TOOLS_DIR = "tools"
# Directories read through a parent-directory needle only below these (a bare `tests/`-level
# file has no meaningful directory reference).
TOP_DIRS = (TESTS_DIR, TOOLS_DIR, SRC_PKG, SRC_ROOT)
DATA_ROOTS = (f"{TESTS_DIR}/", f"{SRC_ROOT}/{SRC_PKG}/", ".nw/", f"{TOOLS_DIR}/")
# CLI wiring modules (see `_hub`); `nightwright.agentic.cli` also covers `nightwright.agentic.cli_*`.
HUBS = ("nightwright.cli", "nightwright.agentic.cli")


class Selection:
    def __init__(self, full, tests, reason):
        self.full = full
        self.tests = sorted(tests)
        self.reason = reason


def _git(repo, *args):
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout


def _merge_base(repo, base):
    return _git(repo, "merge-base", base, "HEAD").strip()


def _changed_since(repo, mb):
    out = set()
    for args in (("diff", "--name-only", f"{mb}...HEAD"), ("diff", "--name-only", "HEAD"),
                 ("ls-files", "--others", "--exclude-standard")):
        out.update(line.strip() for line in _git(repo, *args).splitlines() if line.strip())
    return sorted(out)


def changed_files(repo, base):
    """Repo-relative paths the branch changes against merge-base(base, HEAD), plus the
    working tree's uncommitted and untracked files."""
    return _changed_since(repo, _merge_base(repo, base))


def _module_name(rel):
    """Dotted module name for a repo-relative .py path under src/, tests/ or (stem only)
    tools/, else None."""
    p = Path(rel)
    if p.suffix != ".py":
        return None
    parts = list(p.with_suffix("").parts)
    if parts[0] == SRC_ROOT:
        parts = parts[1:]
    elif parts[0] == TESTS_DIR:
        # tests import each other by bare name (`from waits import ...`, `from test_x import ...`)
        parts = parts[1:]
    elif parts[0] == TOOLS_DIR and len(parts) == 2:
        return parts[1]                     # tools/X.py is module `X` (SPEC-0357)
    else:
        return None
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) if parts else None


def _scan(repo):
    """({module: rel_path}, {stem: rel_path}) for every module under src/nightwright and tests,
    plus tools/*.py under the stem name; the second dict holds the tools modules shadowed by a
    src/tests module of the same name. tools modules are never test modules and `nearest_tests`
    does not walk them; they are part of the import graph for the reference scan and the closure."""
    mods = {}
    tools = {}
    for top in (Path(SRC_ROOT) / SRC_PKG, Path(TESTS_DIR), Path(TOOLS_DIR)):
        root = Path(repo) / top
        if not root.is_dir():
            continue
        for path in root.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            rel = path.relative_to(repo).as_posix()
            name = _module_name(rel)
            if name:
                (tools if rel.startswith(TOOLS_DIR + "/") else mods)[name] = rel
    shadowed = {}
    for name, rel in tools.items():
        if name in mods:
            shadowed[name] = rel            # a src/tests module of the same name wins
        else:
            mods[name] = rel
    return mods, shadowed


def _parse(repo, rel):
    try:
        return ast.parse((Path(repo) / rel).read_text(encoding="utf-8"), filename=rel)
    except (SyntaxError, UnicodeDecodeError, OSError, ValueError):
        return None


def _imports(repo, rel, name, mods, tree=None, nodes=None):
    """Modules (keys of `mods`) the file at `rel` imports, anywhere in its body."""
    tree = tree or _parse(repo, rel)
    if tree is None:
        return set()
    is_pkg = rel.endswith("__init__.py")
    pkg = name if is_pkg else name.rpartition(".")[0]
    found = set()

    def add(dotted):
        # `import a.b.c` also imports a and a.b; record the deepest known module and parents.
        parts = dotted.split(".")
        for i in range(len(parts), 0, -1):
            cand = ".".join(parts[:i])
            if cand in mods:
                found.add(cand)

    for node in (nodes if nodes is not None else ast.walk(tree)):
        if isinstance(node, ast.Import):
            for a in node.names:
                add(a.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = pkg.split(".") if pkg else []
                up = node.level - 1
                if up > len(base):
                    continue
                base = base[:len(base) - up] if up else base
                target = ".".join(base + ([node.module] if node.module else []))
            else:
                target = node.module or ""
            if target:
                add(target)
            for a in node.names:              # `from pkg import submodule`
                if a.name != "*":
                    add(f"{target}.{a.name}" if target else a.name)
    found.discard(name)
    return found


def _path_chain_hits(node):
    """True when the left operand chain of a `/` expression mentions `__file__` or `parent`."""
    for n in ast.walk(node):
        if isinstance(n, ast.Name) and n.id == "__file__":
            return True
        if isinstance(n, ast.Attribute) and n.attr in ("parent", "parents"):
            return True
    return False


class _Info:
    """Reference data of one module: its string constants and `Path(...) / "x"` operands."""

    def __init__(self, nodes):
        self.consts = []
        self.pathparts = set()
        if nodes is not None:
            for n in nodes:
                if isinstance(n, ast.Constant) and isinstance(n.value, str):
                    self.consts.append(n.value)
                elif (isinstance(n, ast.BinOp) and isinstance(n.op, ast.Div)
                      and isinstance(n.right, ast.Constant) and isinstance(n.right.value, str)
                      and _path_chain_hits(n.left)):
                    self.pathparts.add(n.right.value)
        self.blob = "\0".join(self.consts)

    def has_dir_ref(self, parent):
        """Path-shaped reference to directory `parent` (a bare constant equal to it is a dict
        key, not a reference)."""
        if parent in self.pathparts:
            return True
        return any(c.endswith(parent + "/") or c.endswith("/" + parent) or f"/{parent}/" in c
                   for c in self.consts)


class Index:
    """The scanned tree: modules, forward/reverse import graph and reference data."""

    def __init__(self, repo, with_refs=True):
        self.repo = repo
        self.mods, self.shadowed = _scan(repo)
        self.forward = {}
        self.reverse = {m: set() for m in self.mods}
        self.info = {}
        for name, rel in self.mods.items():
            tree = _parse(repo, rel)
            nodes = list(ast.walk(tree)) if tree is not None else None   # one walk, two uses
            deps = _imports(repo, rel, name, self.mods, tree, nodes)
            self.forward[name] = deps
            for dep in deps:
                self.reverse[dep].add(name)
            if with_refs:
                self.info[name] = _Info(nodes)
        # a shadowed tools module is reached by the same `import X` as the module that shadows it
        self.shadow_deps = {n: _imports(repo, rel, n, self.mods) for n, rel in self.shadowed.items()}


def import_graph(repo):
    """(mods, reverse) where reverse[m] = modules that import m directly."""
    ix = Index(repo, with_refs=False)
    return ix.mods, ix.reverse


def _is_test_module(rel):
    p = Path(rel)
    return p.parts[0] == TESTS_DIR and p.name.startswith("test_") and p.suffix == ".py"


def _full_trigger(rel, strict=False):
    p = Path(rel)
    if p.name == "conftest.py" and (strict or _nested_conftest_dir(rel) is None):
        return "conftest.py changed"
    for t in (STRICT_FULL_TRIGGERS if strict else ()):
        if rel == t or (t.endswith("/") and rel.startswith(t)):
            return f"{rel} changed (static selection unsafe)"
    if strict:
        if rel.startswith(f"{SRC_ROOT}/{SRC_PKG}/") and p.suffix != ".py":
            return f"non-Python file under {SRC_ROOT}/{SRC_PKG} changed: {rel}"
        if p.parts[0] == TESTS_DIR and p.suffix != ".py":
            return f"test data changed: {rel}"
    return None


def _nested_conftest_dir(rel):
    """`tests/<dir>/...` of a nested conftest (its fixtures reach only the tests below it), None
    for the root `tests/conftest.py` or one outside `tests/`."""
    p = Path(rel)
    if p.name != "conftest.py" or len(p.parts) < 3 or p.parts[0] != TESTS_DIR:
        return None
    return p.parent.as_posix()


def _toml(text):
    import tomllib
    return tomllib.loads(text)


def _show(repo, rev, rel):
    """`rel` at `rev`, None when absent there."""
    try:
        return _git(repo, "show", f"{rev}:{rel}")
    except RuntimeError:
        return None


def _pyproject_unsafe(old, new):
    """Why a pyproject.toml change may alter the tests, or None for a metadata-only one."""
    if old.keys() - {"project", "tool"} != new.keys() - {"project", "tool"} or any(
            old.get(k) != new.get(k) for k in old.keys() | new.keys() if k not in ("project", "tool")):
        return "build-system or another top-level table changed"
    po, pn = old.get("project") or {}, new.get("project") or {}
    bad = sorted(k for k in po.keys() | pn.keys() if po.get(k) != pn.get(k) and k not in SAFE_PROJECT_KEYS)
    if bad:
        return f"[project] {', '.join(bad)} changed"
    to, tn = old.get("tool") or {}, new.get("tool") or {}
    bad = sorted(k for k in to.keys() | tn.keys() if to.get(k) != tn.get(k) and k not in SAFE_TOOL_KEYS)
    if bad:
        return f"[tool.{bad[0]}] changed" if len(bad) == 1 else f"[tool] {', '.join(bad)} changed"
    return None


def _own_name(repo):
    try:
        return ((_toml((Path(repo) / "pyproject.toml").read_text(encoding="utf-8"))
                 .get("project") or {}).get("name"))
    except (OSError, ValueError):
        return None


def _lock_unsafe(old, new, own):
    """Why a uv.lock change may alter the tests, or None when only the own package's `version`
    moved (a version bump)."""
    def strip(doc):
        doc = dict(doc)
        pk = []
        for pkg in doc.get("package") or []:
            if own and pkg.get("name") == own:
                pkg = {k: v for k, v in pkg.items() if k != "version"}
            pk.append(pkg)
        doc["package"] = pk
        return doc
    return None if strip(old) == strip(new) else "a dependency changed"


def _config_change(repo, rel, base):
    """SPEC-0378: None when the `pyproject.toml` / `uv.lock` change against `base` cannot alter
    the tests, else the full-suite reason. Unknown base (`--changed`), an unreadable or
    unparsable side, or a removed file is unsafe."""
    if base is None:
        return f"{rel} changed (no base to diff it against)"
    old, new = _show(repo, base, rel), None
    try:
        new = (Path(repo) / rel).read_text(encoding="utf-8")
    except OSError:
        pass
    if old is None or new is None:
        return f"{rel} added or removed"
    try:
        o, n = _toml(old), _toml(new)
    except ValueError as e:
        return f"{rel} unparsable: {e}"
    why = _pyproject_unsafe(o, n) if rel == "pyproject.toml" else _lock_unsafe(o, n, _own_name(repo))
    return f"{rel}: {why} (static selection unsafe)" if why else None


def _hub(name):
    """CLI wiring modules: they import nearly every module, and ~half the test files import
    them for `CliRunner`, so a change reaching a hub does not spread to the hub's importers."""
    return name in HUBS or any(name.startswith(h + "_") for h in HUBS if h != "nightwright.cli")


def nearest_tests(name, mods, reverse, depth=0, through_hubs=False):
    """Test files for a changed module `name`: a breadth-first walk of the reverse import
    graph that stops at the first src level holding tests (and never before `depth` src hops).
    Hops through test modules (shared helpers like `test_agentic_e2e`) are free: a test
    module reached is a hit and its importers are hits too. Hubs are not walked past unless
    `through_hubs` (SPEC-0378: the last resort before a full-suite fallback)."""
    def stop_at(m):
        return _hub(m) and m != name and not through_hubs

    def close_over_tests(frontier, seen, hits):
        todo = list(frontier)
        while todo:
            m = todo.pop()
            if stop_at(m):
                continue
            for imp in reverse.get(m, ()):
                if imp in seen or not mods[imp].startswith(TESTS_DIR + "/"):
                    continue
                seen.add(imp)
                if _is_test_module(mods[imp]):
                    hits.add(mods[imp])
                todo.append(imp)

    seen, hits = {name}, set()
    if _is_test_module(mods[name]):
        hits.add(mods[name])
    level, hop = [name], 0
    while level:
        close_over_tests(level, seen, hits)
        if hits and hop >= depth:
            break
        nxt = []
        for m in level:
            if stop_at(m):
                continue
            for imp in reverse.get(m, ()):
                if imp not in seen and not mods[imp].startswith((TESTS_DIR + "/", TOOLS_DIR + "/")):
                    seen.add(imp)
                    nxt.append(imp)
        level, hop = nxt, hop + 1
    return hits


def _referencing(ix, rel, wide=False):
    """Modules (names) that reference the file `rel` by needle: a string constant containing its
    basename, or an import of its stem (tools/X.py); when none names the basename, a path-shaped
    reference to its parent directory. `wide` (the closure) always adds the directory readers,
    since a closure may only err toward more paths."""
    p = Path(rel)
    refs = {m for m, info in ix.info.items() if p.name in info.blob}
    if p.suffix == ".py" and p.parts[0] == TOOLS_DIR and len(p.parts) == 2 and p.stem in ix.mods:
        # by name: when tests/X.py shadows tools/X.py, `import X` after a sys.path insert resolves
        # to the same module name, so the importers of that name are the importers of the tool
        refs |= ix.reverse.get(p.stem, set())
    if refs and not wide:
        return refs
    parent = p.parent.as_posix()
    if parent in (".", *TOP_DIRS, f"{SRC_ROOT}/{SRC_PKG}"):
        return refs                          # no directory-level reference for a top-level file
    return refs | {m for m, info in ix.info.items() if info.has_dir_ref(p.parent.name)}


def _name_rule(ix, rel, name):
    """Test files named after the unreached src module `name` (SPEC-0357 name rule)."""
    leaf = name.rpartition(".")[2]
    tokens = {t for t in (leaf, leaf.removeprefix("cli_"), leaf.lstrip("_"),
                          leaf.removeprefix("cli_").lstrip("_")) if t}
    runs = [t.split("_") for t in tokens]
    cmds = {t.replace("_", "-") for t in tokens}
    hits = set()
    for m, mrel in ix.mods.items():
        if not _is_test_module(mrel):
            continue
        parts = Path(mrel).stem.split("_")
        if any(parts[i:i + len(r)] == r for r in runs for i in range(len(parts) - len(r) + 1)):
            hits.add(mrel)
        elif name in ix.info[m].blob or any(c in cmds for c in ix.info[m].consts):
            hits.add(mrel)
    return hits


def _reaching(ix, name, depth):
    """Tests reaching src module `name`: nearest, else the name rule, else nearest through the
    hubs (SPEC-0378); empty when none."""
    rel = ix.mods[name]
    return (nearest_tests(name, ix.mods, ix.reverse, depth) or _name_rule(ix, rel, name)
            or nearest_tests(name, ix.mods, ix.reverse, depth, through_hubs=True))


def _removed(ix, rel, depth):
    """SPEC-0378: (tests, None) for a removed module `rel`: the tests reaching the modules that
    still name it (an import or string naming its leaf, as a word), or (None, reason) when such a
    src module reaches no test. Nothing names it: nothing to test."""
    import re
    p = Path(rel)
    leaf = p.stem if p.stem != "__init__" else p.parent.name
    word = re.compile(rf"\b{re.escape(leaf)}\b")
    hits = set()
    for m, mrel in ix.mods.items():
        try:
            text = (Path(ix.repo) / mrel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if not word.search(text):
            continue
        if mrel.startswith(TESTS_DIR + "/"):
            hits |= nearest_tests(m, ix.mods, ix.reverse, 0)
        elif mrel.startswith(f"{SRC_ROOT}/"):
            reach = _reaching(ix, m, depth)
            if not reach:
                return None, f"{rel} removed and {mrel}, which names it, reaches no test"
            hits |= reach
        else:                               # a tools module: the tests referencing it
            for t in _referencing(ix, mrel):
                if ix.mods[t].startswith(TESTS_DIR + "/"):
                    hits |= nearest_tests(t, ix.mods, ix.reverse, 0)
    return hits, None


def select(repo, changed, max_fraction=None, depth=0, strict=False, base=None):
    """The Selection for `changed` (repo-relative paths) in `repo`. `base` (the merge-base sha)
    lets a pyproject.toml / uv.lock change be read (SPEC-0378). `max_fraction` None: no cap,
    except `strict`, which keeps `MAX_FRACTION`."""
    changed = [rel for rel in changed if rel]
    if max_fraction is None and strict:
        max_fraction = MAX_FRACTION
    for rel in changed:
        why = _full_trigger(rel, strict)
        if not why and not strict and rel in FULL_TRIGGERS:
            why = _config_change(repo, rel, base)
        if why:
            return Selection(True, [], why)
    ix = Index(repo, with_refs=not strict)
    mods, reverse = ix.mods, ix.reverse
    by_rel = {rel: m for m, rel in mods.items()}
    all_tests = sorted(rel for rel in mods.values() if _is_test_module(rel))
    selected = set()
    rules = []
    for rel in changed:
        if rel in FULL_TRIGGERS:
            rules.append(f"{rel}: metadata only")
            continue
        name = by_rel.get(rel)
        p = Path(rel)
        top = p.parts[0]
        is_src = rel.startswith(f"{SRC_ROOT}/{SRC_PKG}/")
        if not strict and (sub := _nested_conftest_dir(rel)):
            selected |= {t for t in all_tests if t.startswith(sub + "/")}
            rules.append(f"tests below {sub} for {rel}")
            continue
        if p.suffix == ".py" and (is_src or top in (TESTS_DIR, TOOLS_DIR)) \
                and not (Path(repo) / rel).exists():
            if strict:
                # a removed/renamed module: its old importers cannot be found statically
                return Selection(True, [], f"{rel} removed (importers unknown)")
            hits, why = _removed(ix, rel, depth)
            if why:
                return Selection(True, [], why)
            selected |= hits
            rules.append(f"importers of removed {rel}")
            continue
        scan_class = (not strict) and (
            (p.suffix != ".py" and (is_src or top in (TESTS_DIR, ".nw", TOOLS_DIR)))
            or (top == ".nw") or (top == TOOLS_DIR))
        if scan_class:
            refs = _referencing(ix, rel)
            hits = set()
            for m in sorted(refs):
                if mods[m].startswith(TESTS_DIR + "/"):
                    hits |= nearest_tests(m, mods, reverse, 0)
                elif is_src and p.suffix != ".py" and mods[m].startswith(f"{SRC_ROOT}/"):
                    # package data reader: its nearest tests, else the name rule; a reader no
                    # test reaches is a full fallback, not silently dropped
                    reader = _reaching(ix, m, depth)
                    if not reader:
                        return Selection(True, [], f"no test reaches {mods[m]} (reads {rel})")
                    hits |= reader
            if not hits:
                return Selection(True, [], f"no test references {rel}")
            selected |= hits
            rules.append(f"reference scan for {rel}")
            continue
        if name is None:
            if not strict:
                # SPEC-0378: a file outside the mapped roots (AGENTS.md, skills/**, docs/**) selects
                # the tests that reference it, if any; none is not a fallback (prose, mostly)
                hits = set()
                for m in sorted(_referencing(ix, rel)):
                    if mods[m].startswith(TESTS_DIR + "/"):
                        hits |= nearest_tests(m, mods, reverse, 0)
                if hits:
                    selected |= hits
                    rules.append(f"reference scan for {rel}")
            continue                        # docs, specs, ...: no test module mapped
        hits = nearest_tests(name, mods, reverse, depth)
        if not hits and rel.startswith(f"{SRC_ROOT}/"):
            if not strict:
                hits = _name_rule(ix, rel, name)
                if hits:
                    rules.append(f"name rule for {rel}")
                else:
                    hits = nearest_tests(name, mods, reverse, depth, through_hubs=True)
                    if hits:
                        rules.append(f"through the hubs for {rel}")
            if not hits:
                return Selection(True, [], f"no test reaches {rel}")
        selected |= hits
    if max_fraction is not None and all_tests and len(selected) > max_fraction * len(all_tests):
        return Selection(True, [], f"{len(selected)}/{len(all_tests)} test files affected "
                                   f"(> {max_fraction:.0%})")
    reason = (f"{len(selected)}/{len(all_tests)} test files affected by "
              f"{len(changed)} changed files")
    if rules:
        reason += " (" + "; ".join(rules) + ")"
    return Selection(False, selected, reason)


def _tracked_data_files(repo):
    """Tracked non-Python files under tests/, src/nightwright/, .nw/ and tools/, plus the
    tools/*.py scripts (a test may run one by subprocess without importing it)."""
    try:
        files = [f for f in _git(repo, "ls-files").splitlines() if f]
    except RuntimeError:
        files = [p.relative_to(repo).as_posix() for p in Path(repo).rglob("*") if p.is_file()]
    return sorted(f for f in files
                  if f.startswith(DATA_ROOTS) and "__pycache__" not in f
                  and (not f.endswith((".py", ".pyc")) or f.startswith(TOOLS_DIR + "/"))
                  and (Path(repo) / f).is_file())


def closure(repo, sel):
    """Sorted repo-relative paths the selected tests read (forward static closure), or None when
    the selection is the full suite: the selected files, the modules they import transitively
    (src, tests, tools) and every tracked data file a closure module references by needle."""
    if sel.full:
        return None
    ix = Index(repo)
    by_rel = {rel: m for m, rel in ix.mods.items()}
    todo = [by_rel[t] for t in sel.tests if t in by_rel]
    seen = set(todo)
    out = {t for t in sel.tests if (Path(repo) / t).exists()}
    # candidate files with the modules that read them; a file joins the closure once one of its
    # readers is in it, and a tools script that joins brings its own imports (fixed point)
    cands = {f: _referencing(ix, f, wide=True) for f in _tracked_data_files(repo)}
    while todo:
        m = todo.pop()
        deps = set(ix.forward.get(m, ())) | set(ix.shadow_deps.get(m, ()))
        if m in ix.shadowed:
            out.add(ix.shadowed[m])
        for f, readers in cands.items():
            if m in readers and f not in out:
                out.add(f)
                tool = by_rel.get(f)
                if tool:
                    deps.add(tool)
        for dep in deps:
            if dep not in seen:
                seen.add(dep)
                todo.append(dep)
    return sorted(out | {ix.mods[m] for m in seen})


def _git_out(repo, *args):
    try:
        return _git(repo, *args).strip() or None
    except RuntimeError:
        return None


def write_report(path, report):
    """Best effort: a write error is ignored; a missing parent directory is created once."""
    try:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, sort_keys=True) + "\n", encoding="utf-8")
    except OSError:
        pass


def normalize_changed(repo, paths):
    """Canonical repo-relative posix paths for `--changed`: `./x`, `a//b` and absolute paths
    inside `repo` become `x`, `a/b`; empty values are dropped, and a value holding several
    newline-joined paths (`$(git diff --name-only ...)`) is split."""
    out = set()
    for raw in (line.strip() for value in paths for line in value.splitlines()):
        if not raw:
            continue
        p = Path(raw)
        if p.is_absolute():
            try:
                p = p.resolve().relative_to(Path(repo).resolve())
            except ValueError:
                pass
        norm = os.path.normpath(p.as_posix()) if p.parts else ""
        if norm and norm != ".":
            out.add(Path(norm).as_posix())
    return sorted(out)


def forced_full():
    """SPEC-0378: why the environment forces the full suite, or None: `NW_AFFECTED_FULL` set
    true, a non-empty `NW_SENTINEL` (the local sentinel run), or an `NW_CAMPAIGN` naming the
    remote sentinel job (`sentinel-<project>`; a worker forwards only NW_CAMPAIGN / NW_SPEC_ID /
    NW_BASE)."""
    if os.environ.get("NW_AFFECTED_FULL", "").strip().lower() not in ("", "0", "false", "no"):
        return "NW_AFFECTED_FULL is set"
    if os.environ.get("NW_SENTINEL", "").strip():
        return f"sentinel run ({os.environ['NW_SENTINEL'].strip()})"
    if os.environ.get("NW_CAMPAIGN", "").startswith(SENTINEL_CAMPAIGN_PREFIX):
        return f"sentinel run ({os.environ['NW_CAMPAIGN']})"
    return None


def _strict_env():
    return os.environ.get("NW_AFFECTED_STRICT", "").strip().lower() not in ("", "0", "false", "no")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--base", default=os.environ.get("NW_BASE") or "main",
                    help="base ref (default: $NW_BASE, else main)")
    ap.add_argument("--repo", default=".")
    ap.add_argument("--max-fraction", type=float, default=None,
                    help=f"full suite above this fraction of test files (default: no cap; "
                         f"{MAX_FRACTION} with --strict)")
    ap.add_argument("--include", action="append", default=[], metavar="PATH",
                    help="test path always selected (a smoke set); repeatable")
    ap.add_argument("--depth", type=int, default=0,
                    help="src import hops always walked before stopping at the nearest tests")
    ap.add_argument("--strict", action="store_true",
                    help="SPEC-0328 behaviour exactly (also: NW_AFFECTED_STRICT=1)")
    ap.add_argument("--json", action="store_true",
                    help="print the selection report as one JSON line on stdout")
    ap.add_argument("--closure", action="store_true",
                    help="with --json: add the forward static closure of the selection")
    ap.add_argument("--changed", action="append", default=None, metavar="PATH",
                    help="repo-relative changed path replacing the git diff; repeatable")
    ap.add_argument("--run", nargs=argparse.REMAINDER,
                    help="command to run with the selection appended (must come last)")
    args = ap.parse_args(argv)
    repo = Path(args.repo).resolve()
    strict = args.strict or _strict_env()
    base_sha = None
    try:
        if (forced := forced_full()) is not None:
            changed, sel = [], Selection(True, [], forced)
        elif args.changed is not None:
            changed = normalize_changed(repo, args.changed)
            sel = select(repo, changed, args.max_fraction, args.depth, strict)
        else:
            mb = _merge_base(repo, args.base)
            changed = _changed_since(repo, mb)
            base_sha = mb
            sel = select(repo, changed, args.max_fraction, args.depth, strict, base=mb)
    except RuntimeError as e:
        changed, base_sha = [], None
        sel = Selection(True, [], f"cannot diff against {args.base!r}: {e}")
    if not sel.full and args.include:
        sel.tests = sorted(set(sel.tests) | set(args.include))
    label = "full suite" if sel.full else "affected"
    print(f"affected_tests: {label}: {sel.reason}", file=sys.stderr, flush=True)
    report_path = os.environ.get("NW_GATE_REPORT")
    if report_path or args.json:
        report = {"full": sel.full, "reason": sel.reason, "tests": [] if sel.full else sel.tests,
                  "include": list(args.include), "changed": changed, "base_sha": base_sha,
                  "head_sha": _git_out(repo, "rev-parse", "HEAD"),
                  "head_tree": _git_out(repo, "rev-parse", "HEAD^{tree}")}
        if report_path:
            write_report(report_path, report)
        if args.json:
            if args.closure:
                report["closure"] = closure(repo, sel)
            print(json.dumps(report, sort_keys=True), flush=True)
    if args.run is None:
        if not args.json:
            print(TESTS_DIR if sel.full else "\n".join(sel.tests))
        return 0
    if not args.run:
        ap.error("--run needs a command")
    if not sel.full and not sel.tests:
        print("no affected tests", file=sys.stderr if args.json else sys.stdout, flush=True)
        return 0
    cmd = list(args.run) + ([] if sel.full else sel.tests)
    print("affected_tests: running " + " ".join(cmd), file=sys.stderr, flush=True)
    os.chdir(repo)
    os.execvp(cmd[0], cmd)


if __name__ == "__main__":
    sys.exit(main())
