"""SPEC-0185 guard: human output goes through Rich. A bare `print`, `click.echo`,
`sys.stdout.write` or `sys.stderr.write` in `src/wt` must be a listed machine-output path."""
import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "wt"

# (module relative to src/wt, enclosing function) -> why it may write raw
ALLOWED = {
    ("config.py", "<module>"): "pyyaml missing: fails before rich can be relied on",
    ("report.py", "_dump_json"): "JSON emitter",
    ("explore.py", "dump_idea_show_json"): "JSON emitter",
    ("explore.py", "print_idea_show"): "idea show --plain / piped: exact org text",
    ("cli.py", "projects_list_cmd"): "JSON emitter (--json)",
    ("cli.py", "projects_add_cmd"): "JSON emitter (--json)",
    ("cli.py", "projects_rename_cmd"): "JSON emitter (--json)",
    ("cli.py", "spec_status_cmd"): "JSON emitter (--json)",
    ("cli.py", "spec_verify_cmd"): "JSON emitter (--json)",
    ("cli.py", "spec_review_cmd"): "JSON emitter (--json)",
    ("cli.py", "sheet_plan_cmd"): "JSON-only by contract (SPEC-0125)",
    ("cli.py", "sheet_record_cmd"): "JSON-only by contract (SPEC-0125)",
    ("cli.py", "_print_completion"): "shell script for eval",
    ("agentic/cli_projects.py", "projects_config_cmd"): "JSON emitter (--json)",
    ("agentic/cli_queue.py", "on_tick"): "queue run --json-lines stream",
    ("agentic/cli_campaign.py", "campaign_prompt"): "raw prompt meant to be piped",
}


def _is_raw_write(call: ast.Call) -> bool:
    f = call.func
    if isinstance(f, ast.Name) and f.id == "print":
        return True
    if isinstance(f, ast.Attribute):
        if f.attr == "echo" and isinstance(f.value, ast.Name) and f.value.id == "click":
            return True
        if (f.attr == "write" and isinstance(f.value, ast.Attribute)
                and f.value.attr in ("stdout", "stderr")
                and isinstance(f.value.value, ast.Name) and f.value.value.id == "sys"):
            return True
    return False


def raw_writes(path: Path, rel: str):
    """Yield (rel, innermost function, lineno) for each raw write in one module."""
    tree = ast.parse(path.read_text(encoding="utf-8"))

    def walk(node, func):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                yield from walk(child, child.name)
            else:
                if isinstance(child, ast.Call) and _is_raw_write(child):
                    yield rel, func, child.lineno
                yield from walk(child, func)

    yield from walk(tree, "<module>")


def test_no_unlisted_raw_writes():
    bad = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        for rel_, func, line in raw_writes(path, rel):
            if (rel_, func) not in ALLOWED:
                bad.append(f"src/wt/{rel_}:{line} in {func}()")
    assert not bad, ("raw output outside the allowlist — print through wt.console / "
                     "wt.render (warn, print_code, …) or, for a machine path, add it to "
                     "ALLOWED with a reason:\n  " + "\n  ".join(bad))


def test_allowlist_has_no_stale_entries():
    seen = set()
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        seen.update((r, f) for r, f, _ in raw_writes(path, rel))
    assert not set(ALLOWED) - seen, set(ALLOWED) - seen


def test_guard_catches_a_bare_print(tmp_path):
    p = tmp_path / "m.py"
    p.write_text("import sys\ndef f():\n    print('hi')\n    sys.stderr.write('x')\n")
    assert [(f, line) for _, f, line in raw_writes(p, "m.py")] == [("f", 3), ("f", 4)]
