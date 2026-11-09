"""SPEC-0350: per-gate `paths` globs; the close re-gate skips a gate the branch cannot affect."""
from pathlib import Path

import pytest

from test_agentic_close import _events, _git_env, _pcfg, world  # noqa: F401
from nightwright.agentic import close as CL
from nightwright.agentic import gitops as git
from nightwright.agentic import project as P

pytestmark = pytest.mark.fast

ROOT = Path(__file__).resolve().parent.parent
DOGFOOD = P.load_project_config(ROOT / ".nw" / "project.yaml")


@pytest.mark.parametrize("pat,path,ok", [
    ("src/**", "src/a/b.py", True),
    ("src/**", "tests/a.py", False),
    ("docs/**", "docs/product/x.md", True),
    ("README.md", "README.md", True),
    ("README.md", "docs/README.md", True),          # no slash: basename at any depth
    ("docs/specs/0003-*.md", "docs/specs/0003-x.md", True),
    ("docs/specs/0003-*.md", "docs/specs/sub/0003-x.md", False),   # * stays in one segment
    ("**/x.py", "x.py", True),
    ("**/x.py", "a/b/x.py", True),
    ("docs/", "docs/a/b.md", True),                 # trailing slash: the directory
    ("a?.md", "ab.md", True),
    ("a?.md", "a/.md", False),
    ("pyproject.toml", "sub/pyproject.toml", True),
])
def test_path_matches(pat, path, ok):
    assert P.path_matches([pat], path) is ok


def test_split_gates_semantics():
    gates = [{"name": "a"}, {"name": "b", "paths": ["src/**"]}, {"name": "c", "paths": ["docs/**"]}]
    run, skipped = P.split_gates_by_paths(gates, ["docs/x.md"])
    assert [g["name"] for g in run] == ["a", "c"]
    assert [(g["name"], p) for g, p in skipped] == [("b", ["src/**"])]
    for unknown in (None, []):                      # fail closed
        run, skipped = P.split_gates_by_paths(gates, unknown)
        assert [g["name"] for g in run] == ["a", "b", "c"] and skipped == []


def test_paths_validation():
    def check(paths):
        P.validate({"schema": "nw.project.v1", "project": "x",
                    "gates": [{"name": "g", "cmd": "true", "paths": paths}]})
    for bad in ("src/**", [], [1]):
        with pytest.raises(ValueError, match="gates\\[0\\].paths"):
            check(bad)
    check(["src/**"])


# ---- repo's own config -------------------------------------------------------------------

def _gates():
    return {g["name"]: g for g in DOGFOOD["gates"]}


def test_dogfood_gate_paths_cover_what_they_must():
    g = _gates()
    assert "paths" not in g["spec-lint"]
    docs = g["docs-tests"]["paths"]
    for p in ("README.md", "mkdocs.yml", "docs/product/x.md", "docs/specs/x.md"):
        assert P.path_matches(docs, p), p
    full = g["pytest"]["paths"]
    for p in ("src/nightwright/x.py", "tests/test_x.py", "pyproject.toml", "uv.lock",
              "skills/nw-orient/SKILL.md", "tools/spec_lint.py", "docs/specs/TEMPLATE.md",
              "docs/specs/SCHEMA.md"):
        assert P.path_matches(full, p), p


def _runs(changed):
    close = P.gates_for(DOGFOOD, "close")
    run, _ = P.split_gates_by_paths(close, changed)
    return [g["name"] for g in run]


def test_dogfood_sample_changed_path_sets():
    assert _runs(["README.md"]) == ["spec-lint", "docs-tests"]
    assert _runs(["mkdocs.yml", "docs/product/guides/x.md", "docs/specs/0350-x.md",
                  "docs/LEDGER.md"]) == ["spec-lint", "docs-tests"]
    assert _runs(["src/nightwright/cli.py", "README.md"]) == ["spec-lint", "docs-tests", "pytest"]
    assert "pytest" in _runs(["docs/specs/SCHEMA.md"])
    assert "pytest" in _runs(["uv.lock"])
    assert _runs([]) == _runs(None) == ["spec-lint", "docs-tests", "pytest"]


# ---- close re-gate -----------------------------------------------------------------------

def _two_gates(world, marker, paths):
    pcfg = _pcfg(world)
    pcfg["gates"] = [
        {"name": "always", "cmd": "true", "required": True},
        {"name": "guarded", "cmd": f"touch {marker}", "required": True, "paths": paths},
    ]
    return pcfg


def test_close_skips_a_gate_whose_paths_match_nothing(world):
    marker = world["tmp"] / "ran"
    out = CL.prepare_close(world["cfg"], world["state"], _two_gates(world, marker, ["src/**"]))
    assert out.outcome is None and not marker.exists()
    assert [r.name for r in out.gate_run.results] == ["always"]
    (ev,) = _events(out, "gate.skipped")
    assert ev["gate"] == "guarded" and ev["patterns"] == ["src/**"] and ev["changed_count"] >= 2


def test_close_runs_a_gate_whose_paths_match(world):
    marker = world["tmp"] / "ran"
    out = CL.prepare_close(world["cfg"], world["state"],
                           _two_gates(world, marker, ["feature.txt"]))
    assert marker.exists() and _events(out, "gate.skipped") == []


def test_close_runs_every_gate_when_changed_paths_are_unknown(world, monkeypatch):
    marker = world["tmp"] / "ran"

    def boom(*a, **k):
        raise git.GitError(["git", "diff"], 1, "boom")
    pcfg = _two_gates(world, marker, ["src/**"])
    monkeypatch.setattr(git, "changed_files", boom)
    out = CL.prepare_close(world["cfg"], world["state"], pcfg)
    assert marker.exists() and _events(out, "gate.skipped") == []
