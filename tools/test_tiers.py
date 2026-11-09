#!/usr/bin/env python3
"""Per-module pytest tier marks from taxonomy and duration data (SPEC-0490).

Stdlib only (`ast`). Classify modules, rewrite `pytestmark` in `tests/test_*.py`, or verify the tree."""
from __future__ import annotations

import argparse
import ast
import re
import sys
from collections import defaultdict
from pathlib import Path

TESTS = "tests"
TIERS = frozenset({"fast", "integration", "tui", "remote", "perf"})
FAST_MODULE_SUM_S = 20.0
FAST_TEST_S = 2.0
DURATION_LINE = re.compile(r"^([\d.]+)s\s+\w+\s+(\S.+)$")

REMOTE_IMPORTS = frozenset({"remote_loopback", "remote_support", "remote_workers_golden"})
TUI_IMPORTS = frozenset({"tui_harness", "tui_budget"})


class Durations:
    def __init__(self, per_test: dict[str, float]):
        self.per_test = per_test
        self.module_sum: dict[str, float] = defaultdict(float)
        for nodeid, sec in per_test.items():
            mod = nodeid.split("::", 1)[0]
            self.module_sum[mod] += sec


def parse_durations_log(text: str) -> Durations:
    per_test = {}
    for line in text.splitlines():
        line = line.strip()
        m = DURATION_LINE.match(line)
        if not m:
            continue
        nodeid = m.group(2).strip()
        if "::" not in nodeid:
            continue
        per_test[nodeid] = float(m.group(1))
    return Durations(per_test)


def _stem(rel: str) -> str:
    return Path(rel).stem


def _parse_file(repo: Path, rel: str) -> ast.Module | None:
    try:
        return ast.parse((repo / rel).read_text(encoding="utf-8"), filename=rel)
    except (OSError, SyntaxError, UnicodeDecodeError, ValueError):
        return None


def _call_attr(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _imports_mod(tree: ast.Module, names: frozenset[str]) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                base = a.name.split(".", 1)[0]
                if base in names or (a.asname and a.asname in names):
                    return True
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.module.split(".", 1)[0] in names:
                return True
            for a in node.names:
                if a.name in names or (a.asname and a.asname in names):
                    return True
    return False


def _has_run_test(tree: ast.Module) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _call_attr(node) == "run_test":
            return True
    return False


def taxonomy_tier(rel: str, tree: ast.Module | None) -> str:
    stem = _stem(rel)
    if stem == "test_tui_budget":
        return "perf"
    if stem == "test_gate_timeout_tree":
        return "integration"
    if stem.startswith("test_tui_"):
        return "tui"
    if tree is not None and (_has_run_test(tree) or _imports_mod(tree, TUI_IMPORTS)):
        return "tui"
    if stem.startswith("test_remote_") or stem.startswith("test_pull_worker"):
        return "remote"
    if tree is not None and _imports_mod(tree, REMOTE_IMPORTS):
        return "remote"
    if stem.endswith("_e2e") or stem.endswith("_integration") or stem.endswith("_race"):
        return "integration"
    return "fast"


def classify_module(rel: str, tree: ast.Module | None,
                    durations: Durations | None) -> tuple[str, dict[str, float]]:
    tier = taxonomy_tier(rel, tree)
    overrides: dict[str, float] = {}
    if durations and tier == "fast":
        mod_sum = durations.module_sum.get(rel, 0.0)
        for nodeid, sec in durations.per_test.items():
            if nodeid.startswith(rel + "::") and sec > FAST_TEST_S:
                overrides[nodeid] = sec
        if mod_sum > FAST_MODULE_SUM_S:
            return "integration", {}
    return tier, overrides


def _is_pytest_mark(node, name: str) -> bool:
    if isinstance(node, ast.Attribute) and node.attr == name:
        v = node.value
        return isinstance(v, ast.Attribute) and v.attr == "mark" and isinstance(v.value, ast.Name) \
            and v.value.id == "pytest"
    if isinstance(node, ast.Call):
        return _is_pytest_mark(node.func, name)
    return False


def _tier_from_mark_node(node) -> str | None:
    for tier in TIERS:
        if _is_pytest_mark(node, tier):
            return tier
    if isinstance(node, (ast.List, ast.Tuple)):
        found = [_tier_from_mark_node(e) for e in node.elts]
        return next((t for t in found if t), None)
    return None


def _is_slow_mark(node) -> bool:
    return _is_pytest_mark(node, "slow")


def _collect_non_tier_marks(node) -> list[ast.AST]:
    if isinstance(node, (ast.List, ast.Tuple)):
        return [e for e in node.elts if _tier_from_mark_node(e) is None and not _is_slow_mark(e)]
    if _tier_from_mark_node(node) is None and not _is_slow_mark(node):
        return [node]
    return []


def _pytestmark_assign(tree: ast.Module) -> ast.Assign | None:
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "pytestmark" for t in node.targets):
            return node
    return None


def _pytest_import_lineno(tree: ast.Module) -> int | None:
    """Earliest module-level lineno of `import pytest` or `from pytest import …`."""
    best: int | None = None
    for node in tree.body:
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".", 1)[0] == "pytest":
                    ln = node.lineno
                    best = ln if best is None else min(best, ln)
        elif isinstance(node, ast.ImportFrom) and node.module and node.module.split(".", 1)[0] == "pytest":
            ln = node.lineno
            best = ln if best is None else min(best, ln)
    return best


def _ensure_pytest_import_before_mark(text: str, rel: str) -> str:
    tree = ast.parse(text, filename=rel)
    assign = _pytestmark_assign(tree)
    if assign is None or "pytestmark" not in text:
        return text
    imp = _pytest_import_lineno(tree)
    if imp is not None and imp < assign.lineno:
        return text
    bare = text.split("\n")
    bare.insert(assign.lineno - 1, "import pytest")
    new_text = "\n".join(bare)
    if text.endswith("\n"):
        new_text += "\n"
    return new_text


def _module_tiers(tree: ast.Module) -> list[str]:
    assign = _pytestmark_assign(tree)
    if assign is None:
        return []
    if isinstance(assign.value, (ast.List, ast.Tuple)):
        return [t for e in assign.value.elts if (t := _tier_from_mark_node(e))]
    t = _tier_from_mark_node(assign.value)
    return [t] if t else []


def _has_slow_attr(tree: ast.Module) -> list[str]:
    bad = []
    for node in ast.walk(tree):
        if _is_pytest_mark(node, "slow"):
            bad.append(f"slow attribute at line {getattr(node, 'lineno', '?')}")
    return bad


def check_repo(repo: Path) -> list[str]:
    errors = []
    for path in sorted((repo / TESTS).glob("test_*.py")):
        rel = path.relative_to(repo).as_posix()
        tree = _parse_file(repo, rel)
        if tree is None:
            errors.append(f"{rel}: cannot parse")
            continue
        tiers = _module_tiers(tree)
        if not tiers:
            errors.append(f"{rel}: no tier mark")
        elif len(tiers) > 1:
            errors.append(f"{rel}: multiple tier marks: {tiers}")
        elif tiers[0] not in TIERS:
            errors.append(f"{rel}: unknown tier {tiers[0]!r}")
        errors.extend(f"{rel}: {m}" for m in _has_slow_attr(tree))
    return errors


def _build_pytestmark(tier: str, preserved: list[ast.AST]) -> str:
    parts = [f"pytest.mark.{tier}"] + [ast.unparse(e) for e in preserved]
    if len(parts) == 1:
        return f"pytestmark = {parts[0]}"
    return f"pytestmark = [{', '.join(parts)}]"


def _insert_line_after_prelude(tree: ast.Module) -> int:
    """Line index (0-based) after the module docstring and import block."""
    last = 0
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            last = node.end_lineno or node.lineno
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, str):
            last = node.end_lineno or node.lineno
        else:
            break
    return last


def _func_from_nodeid(nodeid: str) -> str:
    return nodeid.split("::")[-1]


def _strip_slow_decorators(text: str, tier: str) -> str:
    if tier == "fast":
        return re.sub(r"^[ \t]*@pytest\.mark\.slow[^\n]*\n", "@pytest.mark.integration\n",
                      text, flags=re.MULTILINE)
    return re.sub(r"^[ \t]*@pytest\.mark\.slow[^\n]*\n", "", text, flags=re.MULTILINE)


def _add_integration_decorators(text: str, funcs: set[str]) -> str:
    if not funcs:
        return text
    out: list[str] = []
    for ln in text.split("\n"):
        m = re.match(r"^def (test_\w+)\(", ln) or re.match(r"^async def (test_\w+)\(", ln)
        if m and m.group(1) in funcs:
            prev = "\n".join(out[-5:])
            if "@pytest.mark.integration" not in prev:
                indent = re.match(r"^(\s*)", ln).group(1) or ""
                out.append(f"{indent}@pytest.mark.integration")
        out.append(ln)
    return "\n".join(out)


def apply_file(repo: Path, rel: str, tier: str, overrides: dict[str, float]) -> bool:
    path = repo / rel
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text, filename=rel)
    assign = _pytestmark_assign(tree)
    preserved = _collect_non_tier_marks(assign.value) if assign else []
    mark_line = _build_pytestmark(tier, preserved)
    bare = text.split("\n")
    if assign is not None:
        start, end = assign.lineno - 1, assign.end_lineno or assign.lineno
        new_text = "\n".join(bare[:start] + [mark_line] + bare[end:])
        if text.endswith("\n"):
            new_text += "\n"
    else:
        ins = _insert_line_after_prelude(tree)
        chunk = bare[:ins] + ([""] if ins and bare[ins - 1].strip() else []) + [mark_line] + bare[ins:]
        new_text = "\n".join(chunk)
        if text.endswith("\n"):
            new_text += "\n"
    new_text = _ensure_pytest_import_before_mark(new_text, rel)
    slow_funcs = {_func_from_nodeid(n) for n in overrides} if tier == "fast" else set()
    new_text = _strip_slow_decorators(new_text, tier)
    new_text = _add_integration_decorators(new_text, slow_funcs)
    if new_text != text:
        path.write_text(new_text, encoding="utf-8")
        return True
    return False


def classify_repo(repo: Path, durations: Durations | None) -> dict[str, tuple[str, dict[str, float]]]:
    return {p.relative_to(repo).as_posix(): classify_module(
        p.relative_to(repo).as_posix(), _parse_file(repo, p.relative_to(repo).as_posix()), durations)
        for p in sorted((repo / TESTS).glob("test_*.py"))}


def cmd_classify(repo: Path, log_path: Path) -> int:
    if not log_path.is_file() or not log_path.read_text(encoding="utf-8").strip():
        return 2
    durations = parse_durations_log(log_path.read_text(encoding="utf-8"))
    for rel, (tier, overrides) in sorted(classify_repo(repo, durations).items()):
        print(f"{tier}\t{rel}")
        for nid, sec in sorted(overrides.items()):
            print(f"  override integration\t{nid}\t{sec:.3f}s")
    return 0


def cmd_apply(repo: Path, log_path: Path) -> int:
    if not log_path.is_file():
        return 2
    raw = log_path.read_text(encoding="utf-8")
    if not raw.strip():
        return 2
    durations = parse_durations_log(raw)
    for rel, (tier, overrides) in classify_repo(repo, durations).items():
        apply_file(repo, rel, tier, overrides)
    return 0


def cmd_check(repo: Path) -> int:
    errors = check_repo(repo)
    for e in errors:
        print(e, file=sys.stderr)
    return 1 if errors else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    ap_repo = argparse.ArgumentParser(add_help=False)
    ap_repo.add_argument("--repo", default=".", type=Path)
    p_check = sub.add_parser("check", parents=[ap_repo])
    p_check.set_defaults(func=lambda a: cmd_check(a.repo.resolve()))
    p_classify = sub.add_parser("classify", parents=[ap_repo])
    p_classify.add_argument("durations_log", type=Path)
    p_classify.set_defaults(func=lambda a: cmd_classify(a.repo.resolve(), a.durations_log))
    p_apply = sub.add_parser("apply", parents=[ap_repo])
    p_apply.add_argument("durations_log", type=Path)
    p_apply.set_defaults(func=lambda a: cmd_apply(a.repo.resolve(), a.durations_log))
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
