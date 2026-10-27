"""spec_lint reports duplicate spec ids and duplicate number prefixes (SPEC-0274)."""
import importlib.util
import pathlib

LINT = pathlib.Path(__file__).resolve().parent.parent / "tools" / "spec_lint.py"


def _linter():
    spec = importlib.util.spec_from_file_location("spec_lint", LINT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write(path, sid, title="x"):
    path.write_text(
        f"---\nid: {sid}\ntitle: \"{title}\"\nstatus: draft\nkind: feature\n"
        f"owner: user\ncreated: 2026-01-01\n---\n\n## Context\n\nx\n"
    )


def _problems(mod, tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "SPECS", tmp_path)
    return mod.validate(mod.load_specs())


def test_duplicate_id_fails(tmp_path, monkeypatch):
    mod = _linter()
    _write(tmp_path / "0001-a.md", "SPEC-0001")
    _write(tmp_path / "0001-b.md", "SPEC-0001")
    probs = [p for p in _problems(mod, tmp_path, monkeypatch) if "duplicate id SPEC-0001" in p]
    assert any(p.startswith("0001-a.md") and "0001-b.md" in p for p in probs)
    assert any(p.startswith("0001-b.md") and "0001-a.md" in p for p in probs)


def test_duplicate_prefix_fails(tmp_path, monkeypatch):
    mod = _linter()
    _write(tmp_path / "0001-a.md", "SPEC-0001")
    (tmp_path / "0001-b.md").write_text("no frontmatter\n")
    probs = _problems(mod, tmp_path, monkeypatch)
    assert any("duplicate number prefix 0001" in p for p in probs)


def test_unique_ids_no_duplicate_problem(tmp_path, monkeypatch):
    mod = _linter()
    _write(tmp_path / "0001-a.md", "SPEC-0001")
    _write(tmp_path / "0002-b.md", "SPEC-0002")
    assert not any("duplicate" in p for p in _problems(mod, tmp_path, monkeypatch))
