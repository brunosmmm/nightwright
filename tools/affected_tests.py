#!/usr/bin/env python3
"""Affected-test selection for wt's fix-loop gates (SPEC-0328).

Static test impact analysis, no coverage database: the changed files of the branch
(`git diff --name-only <merge-base(base, HEAD)>` plus uncommitted and untracked files) are
mapped to test modules through a static AST import graph over `src/wt` and `tests`:

- a changed test module selects itself;
- a changed src module selects its *nearest* tests: the test modules importing it directly,
  or, when none do, those at the first level of src importers that has any (breadth-first
  over the reverse import graph; `--depth N` always walks N src levels first). Imports
  anywhere in a module count, lazy function-level imports included. Hops through test
  helper modules (e.g. `test_agentic_e2e`) are free, and the CLI hubs (`HUBS`) are never
  walked past: they import nearly everything and half the test files import them;
- a changed test module selects itself and the test modules importing it;
- files that cannot be mapped safely select the FULL suite (see `FULL_TRIGGERS`), as does an
  empty mapping for a changed src module or a selection above `MAX_FRACTION` of test files.

Usage:
    python3 tools/affected_tests.py [--base REF] [--repo DIR] [--include PATH]...
        prints the selection, one pytest path per line (`tests` for the full suite);
        the reason goes to stderr.
    python3 tools/affected_tests.py [--base REF] --run CMD [ARGS...]
        runs CMD with the selection appended (nothing appended for the full suite); an empty
        selection prints "no affected tests" and exits 0 without running CMD.

Stdlib only, so the gate can run it with the system `python3`.
"""
import argparse
import ast
import os
import subprocess
import sys
from pathlib import Path

SRC_ROOT = "src"
SRC_PKG = "wt"
TESTS_DIR = "tests"
MAX_FRACTION = 0.40

# Path prefixes (or exact paths) whose change makes a static selection unsafe.
FULL_TRIGGERS = ("pyproject.toml", "uv.lock", "tools/", ".wt/", "tests/waits.py")
# CLI wiring modules (see `_hub`); `wt.agentic.cli` also covers `wt.agentic.cli_*`.
HUBS = ("wt.cli", "wt.agentic.cli")


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


def changed_files(repo, base):
    """Repo-relative paths the branch changes against merge-base(base, HEAD), plus the
    working tree's uncommitted and untracked files."""
    mb = _git(repo, "merge-base", base, "HEAD").strip()
    out = set()
    for args in (("diff", "--name-only", f"{mb}...HEAD"), ("diff", "--name-only", "HEAD"),
                 ("ls-files", "--others", "--exclude-standard")):
        out.update(line.strip() for line in _git(repo, *args).splitlines() if line.strip())
    return sorted(out)


def _module_name(rel):
    """Dotted module name for a repo-relative .py path under src/ or tests/, else None."""
    p = Path(rel)
    if p.suffix != ".py":
        return None
    parts = list(p.with_suffix("").parts)
    if parts[0] == SRC_ROOT:
        parts = parts[1:]
    elif parts[0] == TESTS_DIR:
        # tests import each other by bare name (`from waits import ...`, `from test_x import ...`)
        parts = parts[1:]
    else:
        return None
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) if parts else None


def _scan(repo):
    """{module: rel_path} for every module under src/wt and tests."""
    mods = {}
    for top in (Path(SRC_ROOT) / SRC_PKG, Path(TESTS_DIR)):
        root = Path(repo) / top
        if not root.is_dir():
            continue
        for path in root.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            rel = path.relative_to(repo).as_posix()
            name = _module_name(rel)
            if name:
                mods[name] = rel
    return mods


def _imports(repo, rel, name, mods):
    """Modules (keys of `mods`) the file at `rel` imports, anywhere in its body."""
    try:
        tree = ast.parse((Path(repo) / rel).read_text(encoding="utf-8"), filename=rel)
    except (SyntaxError, UnicodeDecodeError, OSError):
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

    for node in ast.walk(tree):
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


def import_graph(repo):
    """(mods, reverse) where reverse[m] = modules that import m directly."""
    mods = _scan(repo)
    reverse = {m: set() for m in mods}
    for name, rel in mods.items():
        for dep in _imports(repo, rel, name, mods):
            reverse[dep].add(name)
    return mods, reverse


def _is_test_module(rel):
    p = Path(rel)
    return p.parts[0] == TESTS_DIR and p.name.startswith("test_") and p.suffix == ".py"


def _full_trigger(rel):
    p = Path(rel)
    if p.name == "conftest.py":
        return "conftest.py changed"
    for t in FULL_TRIGGERS:
        if rel == t or (t.endswith("/") and rel.startswith(t)):
            return f"{rel} changed (static selection unsafe)"
    if rel.startswith(f"{SRC_ROOT}/{SRC_PKG}/") and p.suffix != ".py":
        return f"non-Python file under {SRC_ROOT}/{SRC_PKG} changed: {rel}"
    if p.parts[0] == TESTS_DIR and p.suffix != ".py":
        return f"test data changed: {rel}"
    return None


def _hub(name):
    """CLI wiring modules: they import nearly every module, and ~half the test files import
    them for `CliRunner`, so a change reaching a hub does not spread to the hub's importers."""
    return name in HUBS or any(name.startswith(h + "_") for h in HUBS if h != "wt.cli")


def nearest_tests(name, mods, reverse, depth=0):
    """Test files for a changed module `name`: a breadth-first walk of the reverse import
    graph that stops at the first src level holding tests (and never before `depth` src hops).
    Hops through test modules (shared helpers like `test_agentic_e2e`) are free: a test
    module reached is a hit and its importers are hits too. Hubs are not walked past."""
    def close_over_tests(frontier, seen, hits):
        todo = list(frontier)
        while todo:
            m = todo.pop()
            if _hub(m) and m != name:
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
            if _hub(m) and m != name:
                continue
            for imp in reverse.get(m, ()):
                if imp not in seen and not mods[imp].startswith(TESTS_DIR + "/"):
                    seen.add(imp)
                    nxt.append(imp)
        level, hop = nxt, hop + 1
    return hits


def select(repo, changed, max_fraction=MAX_FRACTION, depth=0):
    """The Selection for `changed` (repo-relative paths) in `repo`."""
    for rel in changed:
        why = _full_trigger(rel)
        if why:
            return Selection(True, [], why)
    mods, reverse = import_graph(repo)
    by_rel = {rel: m for m, rel in mods.items()}
    all_tests = sorted(rel for rel in mods.values() if _is_test_module(rel))
    selected = set()
    for rel in changed:
        name = by_rel.get(rel)
        if name is None:
            if rel.endswith(".py") and (rel.startswith(f"{SRC_ROOT}/{SRC_PKG}/")
                                        or rel.startswith(f"{TESTS_DIR}/")):
                if not (Path(repo) / rel).exists():
                    # a removed/renamed module: its old importers cannot be found statically
                    return Selection(True, [], f"{rel} removed (importers unknown)")
            continue                        # docs, specs, ...: no test module mapped
        hits = nearest_tests(name, mods, reverse, depth)
        if not hits and rel.startswith(f"{SRC_ROOT}/"):
            return Selection(True, [], f"no test reaches {rel}")
        selected |= hits
    if all_tests and len(selected) > max_fraction * len(all_tests):
        return Selection(True, [], f"{len(selected)}/{len(all_tests)} test files affected "
                                   f"(> {max_fraction:.0%})")
    return Selection(False, selected,
                     f"{len(selected)}/{len(all_tests)} test files affected by "
                     f"{len(changed)} changed files")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--base", default=os.environ.get("WT_BASE") or "main",
                    help="base ref (default: $WT_BASE, else main)")
    ap.add_argument("--repo", default=".")
    ap.add_argument("--max-fraction", type=float, default=MAX_FRACTION)
    ap.add_argument("--include", action="append", default=[], metavar="PATH",
                    help="test path always selected (a smoke set); repeatable")
    ap.add_argument("--depth", type=int, default=0,
                    help="src import hops always walked before stopping at the nearest tests")
    ap.add_argument("--run", nargs=argparse.REMAINDER,
                    help="command to run with the selection appended")
    args = ap.parse_args(argv)
    repo = Path(args.repo).resolve()
    try:
        sel = select(repo, changed_files(repo, args.base), args.max_fraction, args.depth)
    except RuntimeError as e:
        sel = Selection(True, [], f"cannot diff against {args.base!r}: {e}")
    if not sel.full and args.include:
        sel.tests = sorted(set(sel.tests) | set(args.include))
    label = "full suite" if sel.full else "affected"
    print(f"affected_tests: {label}: {sel.reason}", file=sys.stderr, flush=True)
    if args.run is None:
        print(TESTS_DIR if sel.full else "\n".join(sel.tests))
        return 0
    if not args.run:
        ap.error("--run needs a command")
    if not sel.full and not sel.tests:
        print("no affected tests", flush=True)
        return 0
    cmd = list(args.run) + ([] if sel.full else sel.tests)
    print("affected_tests: running " + " ".join(cmd), file=sys.stderr, flush=True)
    os.chdir(repo)
    os.execvp(cmd[0], cmd)


if __name__ == "__main__":
    sys.exit(main())
