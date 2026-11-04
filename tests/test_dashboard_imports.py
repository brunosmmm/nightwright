"""SPEC-0401 / SPEC-0400 invariant 2: no queue-side module imports the dashboard package,
directly or transitively (in the style of `test_queue_runner_runproc_never_import_herdr`)."""
import ast
import subprocess
import sys
from pathlib import Path

import nightwright

QUEUE_SIDE = ("queue", "runner", "runproc", "events", "escalations", "lease", "store", "mailbox")


def test_queue_side_modules_never_import_the_dashboard():
    mods = ", ".join(f"nightwright.agentic.{m}" for m in QUEUE_SIDE)
    code = (f"import sys, {mods};"
            "print(sorted(m for m in sys.modules if m.startswith('nightwright.agentic.dashboard')"
            " or m == 'nightwright.agentic.cli_dashboard'))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         check=True, timeout=120).stdout.strip()
    assert out == "[]"


def _imports(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = "." * node.level + (node.module or "")
            yield base
            yield from (f"{base}.{a.name}" if base else a.name for a in node.names)


def test_queue_side_sources_name_no_dashboard_import():
    root = Path(nightwright.__file__).parent / "agentic"
    for m in QUEUE_SIDE:
        names = list(_imports(root / f"{m}.py"))
        bad = [n for n in names if "dashboard" in n.split(".")]
        assert not bad, (m, bad)


# ---- SPEC-0414: the server modules ---------------------------------------------------------

SERVE_SIDE = ("pull_jobs", "cli_worker")


def _module_level_imports(path):
    """Imports that run when the module is imported (not those inside a function body)."""
    def walk(nodes):
        for node in nodes:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if isinstance(node, ast.Import):
                yield from (a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                base = "." * node.level + (node.module or "")
                yield base
                yield from (f"{base}.{a.name}" if base else a.name for a in node.names)
            else:
                for field in ("body", "orelse", "finalbody", "handlers"):
                    yield from walk(getattr(node, field, []) or [])
    yield from walk(ast.parse(path.read_text(encoding="utf-8")).body)


def test_pull_jobs_and_cli_worker_import_no_dashboard_at_module_level():
    root = Path(nightwright.__file__).parent / "agentic"
    for m in SERVE_SIDE:
        bad = [n for n in _module_level_imports(root / f"{m}.py") if "dashboard" in n.split(".")]
        assert not bad, (m, bad)
    mods = ", ".join(f"nightwright.agentic.{m}" for m in QUEUE_SIDE + SERVE_SIDE)
    code = (f"import sys, {mods};"
            "print(sorted(m for m in sys.modules if m.startswith('nightwright.agentic.dashboard')"
            " or m == 'nightwright.agentic.cli_dashboard'))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         check=True, timeout=120).stdout.strip()
    assert out == "[]"


def test_snapshot_does_not_import_auth_or_server():
    code = ("import sys, nightwright.agentic.dashboard.snapshot;"
            "print(sorted(m for m in sys.modules if m in ("
            "'nightwright.agentic.dashboard.auth', 'nightwright.agentic.dashboard.server', "
            "'nightwright.agentic.dashboard.audit')))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         check=True, timeout=120).stdout.strip()
    assert out == "[]"


def test_queue_side_modules_never_load_the_page_assets():
    """SPEC-0413: `assets` is a dashboard module like `snapshot`: no queue-side import pulls it."""
    mods = ", ".join(f"nightwright.agentic.{m}" for m in QUEUE_SIDE)
    code = (f"import sys, {mods};"
            "print('nightwright.agentic.dashboard.assets' in sys.modules)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         check=True, timeout=120).stdout.strip()
    assert out == "False"
