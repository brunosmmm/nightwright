#!/usr/bin/env python3
"""Per-module pytest tier marks from the taxonomy and a durations log (SPEC-0490).

Stdlib only (`ast`). `--classify LOG` prints one tier per `tests/test_*.py` module plus the
function-level overrides; `--apply LOG` rewrites the modules in place with the same
classification; `--check` verifies the tree (exit 1 and lists each bad module)."""
from __future__ import annotations

import argparse
import ast
import re
import sys
import tomllib
from collections import defaultdict
from pathlib import Path

TESTS = "tests"
TIERS = ("fast", "integration", "tui", "remote", "perf")
HEAVY = frozenset(TIERS) - {"fast"}
FAST_MODULE_SUM_S = 20.0
FAST_TEST_S = 2.0
# `--durations=0` lines: "12.34s call     tests/test_x.py::test_y[param]"
DURATION_LINE = re.compile(r"^([\d.]+)s\s+(setup|call|teardown)\s+(\S.*)$")
PYTEST_BUILTIN_MARKS = frozenset({"skip", "skipif", "xfail", "usefixtures", "parametrize",
                                  "filterwarnings"})
REMOTE_IMPORTS = frozenset({"remote_loopback", "remote_support", "remote_workers_golden"})
TUI_IMPORTS = frozenset({"tui_harness", "tui_budget"})
EXPLICIT = {"test_tui_budget": "perf", "test_gate_timeout_tree": "integration"}


# --- durations ---------------------------------------------------------------------------------

class Durations:
    """Per-test seconds (setup + call + teardown summed) and per-module sums."""

    def __init__(self, per_test: dict[str, float]):
        self.per_test = per_test
        self.module_sum: dict[str, float] = defaultdict(float)
        for nodeid, sec in per_test.items():
            self.module_sum[nodeid.split("::", 1)[0]] += sec


def parse_durations_log(text: str) -> Durations:
    per_test: dict[str, float] = defaultdict(float)
    for line in text.splitlines():
        m = DURATION_LINE.match(line.strip())
        if m and "::" in m.group(3):
            per_test[m.group(3).strip()] += float(m.group(1))
    return Durations(dict(per_test))


# --- classification ----------------------------------------------------------------------------

def _parse_file(repo: Path, rel: str) -> ast.Module | None:
    try:
        return ast.parse((repo / rel).read_text(encoding="utf-8"), filename=rel)
    except (OSError, SyntaxError, UnicodeDecodeError, ValueError):
        return None


def _imports_any(tree: ast.Module, names: frozenset[str]) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(a.name.split(".", 1)[0] in names for a in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.module.split(".", 1)[0] in names:
                return True
            if any(a.name in names for a in node.names):
                return True
    return False


def _calls_run_test(tree: ast.Module) -> bool:
    return any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
               and n.func.attr == "run_test" for n in ast.walk(tree))


def taxonomy_tier(rel: str, tree: ast.Module | None) -> str:
    """Rules 1-4 of the taxonomy plus `fast`; the first match wins (durations not consulted)."""
    stem = Path(rel).stem
    if stem in EXPLICIT:
        return EXPLICIT[stem]
    if stem.startswith("test_tui_") or (tree is not None and (
            _calls_run_test(tree) or _imports_any(tree, TUI_IMPORTS))):
        return "tui"
    if stem.startswith(("test_remote_", "test_pull_worker")) or (
            tree is not None and _imports_any(tree, REMOTE_IMPORTS)):
        return "remote"
    if stem.endswith(("_e2e", "_integration", "_race")):
        return "integration"
    return "fast"


def classify_module(rel: str, tree: ast.Module | None,
                    durations: Durations | None) -> tuple[str, dict[str, float]]:
    """(tier, {nodeid: seconds} of fast-module tests over the 2 s budget)."""
    tier = taxonomy_tier(rel, tree)
    if tier != "fast" or durations is None:
        return tier, {}
    if durations.module_sum.get(rel, 0.0) > FAST_MODULE_SUM_S:
        return "integration", {}
    return "fast", {n: s for n, s in durations.per_test.items()
                    if n.startswith(rel + "::") and s > FAST_TEST_S}


def module_paths(repo: Path) -> list[str]:
    return [p.relative_to(repo).as_posix() for p in sorted((repo / TESTS).glob("test_*.py"))]


def classify_repo(repo: Path, durations: Durations | None) -> dict[str, tuple[str, dict]]:
    return {rel: classify_module(rel, _parse_file(repo, rel), durations)
            for rel in module_paths(repo)}


# --- mark inspection ---------------------------------------------------------------------------

def _mark_name(node) -> str | None:
    """`pytest.mark.NAME` or `pytest.mark.NAME(...)` -> NAME, else None."""
    if isinstance(node, ast.Call):
        node = node.func
    if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Attribute)
            and node.value.attr == "mark" and isinstance(node.value.value, ast.Name)
            and node.value.value.id == "pytest"):
        return node.attr
    return None


def _pytestmark_assign(tree: ast.Module) -> ast.Assign | None:
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "pytestmark" for t in node.targets):
            return node
    return None


def _elements(value) -> list:
    return list(value.elts) if isinstance(value, (ast.List, ast.Tuple)) else [value]


def _registered_marks(repo: Path) -> set[str]:
    try:
        doc = tomllib.loads((repo / "pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return set()
    lines = doc.get("tool", {}).get("pytest", {}).get("ini_options", {}).get("markers", [])
    return {str(m).split(":", 1)[0].split("(", 1)[0].strip() for m in lines}


def check_repo(repo: Path) -> list[str]:
    """One line per problem; `[]` means the tree is clean. Helper modules are exempt."""
    known = PYTEST_BUILTIN_MARKS | _registered_marks(repo)
    errors = []
    for rel in module_paths(repo):
        tree = _parse_file(repo, rel)
        if tree is None:
            errors.append(f"{rel}: cannot parse")
            continue
        assign = _pytestmark_assign(tree)
        names = [n for e in (_elements(assign.value) if assign else [])
                 if (n := _mark_name(e))]
        tiers = [n for n in names if n in TIERS]
        unknown = [n for n in names if n not in TIERS and n not in known and n != "slow"]
        if unknown:
            errors.append(f"{rel}: unknown tier {', '.join(unknown)}")
        if not tiers:
            errors.append(f"{rel}: no tier")
        elif len(tiers) > 1:
            errors.append(f"{rel}: more than one tier: {', '.join(tiers)}")
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and _mark_name(node) == "slow":
                errors.append(f"{rel}:{node.lineno}: pytest.mark.slow")
    return errors


# --- rewriting ---------------------------------------------------------------------------------

def _after_prelude(tree: ast.Module) -> int:
    """1-based last line of the leading docstring / import block (0 when there is none)."""
    last = 0
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)) or (
                isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)):
            last = node.end_lineno or node.lineno
        else:
            break
    return last


def _has_module_pytest_import(tree: ast.Module, before: int) -> bool:
    for node in tree.body:
        if node.lineno >= before:
            break
        if isinstance(node, ast.Import) and any(a.name == "pytest" and not a.asname
                                                for a in node.names):
            return True
    return False


def _rewrite_pytestmark(text: str, rel: str, tier: str) -> str:
    tree = ast.parse(text, filename=rel)
    lines = text.split("\n")
    assign = _pytestmark_assign(tree)
    kept = [ast.unparse(e) for e in (_elements(assign.value) if assign else [])
            if _mark_name(e) not in (*TIERS, "slow")]
    parts = [f"pytest.mark.{tier}", *kept]
    stmt = f"pytestmark = {parts[0]}" if len(parts) == 1 else f"pytestmark = [{', '.join(parts)}]"
    if assign is not None:
        start, end = assign.lineno - 1, assign.end_lineno or assign.lineno
        head = lines[start].encode()[:assign.col_offset].decode()
        tail = lines[end - 1].encode()[assign.end_col_offset or 0:].decode()   # a comment
        lines[start:end] = [head + stmt + tail]
        at = start + 1
    else:
        at = _after_prelude(tree)
        block = ([""] if at else []) + [stmt]
        if at < len(lines) and lines[at].strip():
            block.append("")
        lines[at:at] = block
        at += len(block)
    out = "\n".join(lines)
    tree = ast.parse(out, filename=rel)
    if not _has_module_pytest_import(tree, _pytestmark_assign(tree).lineno):
        ln = _pytestmark_assign(tree).lineno - 1
        lines = out.split("\n")
        lines[ln:ln] = ["import pytest", ""]
        out = "\n".join(lines)
    return out


def _is_slow_deco(d: ast.AST) -> bool:
    d = d.func if isinstance(d, ast.Call) else d
    return isinstance(d, ast.Attribute) and d.attr == "slow" and _dotted(d.value) == "pytest.mark"


def _dotted(n: ast.AST) -> str:
    if isinstance(n, ast.Name):
        return n.id
    if isinstance(n, ast.Attribute):
        return f"{_dotted(n.value)}.{n.attr}"
    return ""


def _rewrite_function_marks(text: str, tier: str, slow_funcs: set[str]) -> str:
    """Rewrite real decorator nodes only (a `@pytest.mark.slow` inside a string is left alone)."""
    lines = text.split("\n")
    edits: list[tuple[int, int, list[str]]] = []          # (start, end) line slice, 0-based
    for node in ast.walk(ast.parse(text)):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        indent = " " * node.col_offset
        has_integration = False
        for d in node.decorator_list:
            if _is_slow_deco(d):
                repl = [f"{indent}@pytest.mark.integration"] if tier == "fast" else []
                edits.append((d.lineno - 1, d.end_lineno or d.lineno, repl))
                has_integration = has_integration or tier == "fast"
            elif _dotted(d) == "pytest.mark.integration":
                has_integration = True
        if node.name in slow_funcs and node.name.startswith("test") and not has_integration:
            edits.append((node.lineno - 1, node.lineno - 1, [f"{indent}@pytest.mark.integration"]))
    for start, end, repl in sorted(edits, key=lambda e: (e[0], e[1]), reverse=True):
        lines[start:end] = repl
    return "\n".join(lines)


def _func_name(nodeid: str) -> str:
    return nodeid.split("::")[-1].split("[", 1)[0]


def apply_file(repo: Path, rel: str, tier: str, overrides: dict[str, float]) -> bool:
    path = repo / rel
    text = path.read_text(encoding="utf-8")
    new = _rewrite_pytestmark(text, rel, tier)
    new = _rewrite_function_marks(new, tier, {_func_name(n) for n in overrides})
    new = new.rstrip("\n") + "\n"
    if new != text:
        path.write_text(new, encoding="utf-8")
        return True
    return False


# --- commands ----------------------------------------------------------------------------------

def _read_log(path: Path) -> str | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    return raw if raw.strip() else None


def cmd_classify(repo: Path, log: Path) -> int:
    raw = _read_log(log)
    if raw is None:
        print(f"test_tiers: missing or empty durations log {log}", file=sys.stderr)
        return 2
    for rel, (tier, overrides) in classify_repo(repo, parse_durations_log(raw)).items():
        print(f"{tier}\t{rel}")
        for nid, sec in sorted(overrides.items()):
            print(f"  integration\t{nid}\t{sec:.2f}s")
    return 0


def cmd_apply(repo: Path, log: Path) -> int:
    raw = _read_log(log)
    if raw is None:
        print(f"test_tiers: missing or empty durations log {log}; nothing written",
              file=sys.stderr)
        return 2
    changed = [rel for rel, (tier, ov) in classify_repo(repo, parse_durations_log(raw)).items()
               if apply_file(repo, rel, tier, ov)]
    print(f"test_tiers: rewrote {len(changed)} module(s)", file=sys.stderr)
    return 0


def cmd_check(repo: Path) -> int:
    errors = check_repo(repo)
    for e in errors:
        print(e)
    return 1 if errors else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1],
                    help="repository root (default: this checkout)")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="verify every module has one tier")
    mode.add_argument("--classify", type=Path, metavar="DURATIONS_LOG")
    mode.add_argument("--apply", type=Path, metavar="DURATIONS_LOG")
    args = ap.parse_args(argv)
    repo = args.repo.resolve()
    if args.check:
        return cmd_check(repo)
    if args.classify is not None:
        return cmd_classify(repo, args.classify)
    return cmd_apply(repo, args.apply)


if __name__ == "__main__":
    sys.exit(main())
