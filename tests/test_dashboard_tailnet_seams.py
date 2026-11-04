"""SPEC-0416: the `own` test seam is not reachable from the CLI, a flag, the environment or config."""
import ast
import inspect
from pathlib import Path

from nightwright.agentic import cli_worker
from nightwright.agentic.dashboard import server as DS

SRC = Path(DS.__file__).parent
CLI = Path(cli_worker.__file__)


def _calls_named(tree, name):
    return [n for n in ast.walk(tree) if isinstance(n, ast.Call)
            and (getattr(n.func, "attr", None) == name or getattr(n.func, "id", None) == name)]


def test_serve_cmd_and_serve_dashboard_pass_no_own():
    tree = ast.parse(CLI.read_text())
    fns = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
           and n.name in ("serve_cmd", "_serve_dashboard")]
    assert {f.name for f in fns} == {"serve_cmd", "_serve_dashboard"}
    for f in fns:
        for call in _calls_named(f, "make_server") + _calls_named(f, "resolve_bind"):
            assert not any(k.arg in ("own", "own_fn") for k in call.keywords), ast.dump(call)
        for n in ast.walk(f):
            assert not (isinstance(n, ast.Name) and n.id in ("own", "own_fn")), ast.dump(n)
    assert any(_calls_named(f, "resolve_bind") for f in fns)


def test_make_server_has_own_keyword_only():
    assert [a.arg for a in ast.parse(inspect.getsource(DS.make_server)).body[0].args.kwonlyargs] \
        == ["cache", "own"]


def test_no_environment_read_feeds_the_seam():
    """`own` is read only by `make_server` (and `own_fn` by `resolve_bind`); neither, nor the
    whole of `auth.py`, reads the environment."""
    assert "environ" not in inspect.getsource(DS.make_server)
    assert "getenv" not in inspect.getsource(DS.make_server)
    auth_src = (SRC / "auth.py").read_text()
    assert "getenv" not in auth_src and "os.environ" not in auth_src


def test_no_dashboard_module_runs_a_tailscale_form_but_ip():
    for p in SRC.glob("*.py"):
        for n in ast.walk(ast.parse(p.read_text())):
            if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "run_tailscale":
                assert [a.value for a in n.args[0].elts] == ["ip"], (p, ast.dump(n))


def test_reference_doc_lists_bind_tailscale_and_exit_3():
    doc = (Path(__file__).parent.parent / "docs/product/reference/index.md").read_text()
    row = next(ln for ln in doc.splitlines() if ln.startswith("| `serve` |"))
    assert "--bind tailscale" in row and "exits **3**" in row
    assert "naming `tailscale up`. Exactly one" in row
