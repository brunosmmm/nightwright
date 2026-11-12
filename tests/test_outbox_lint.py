"""Outbound spec lint (SPEC-0561): nightwright.outbox_lint, `nw spec lint`, and
`tools/spec_lint.py --outbox`."""
import ast
import importlib.util
import json
import os
import pathlib

import pytest
from click.testing import CliRunner

from nightwright import outbox_lint as OL
from nightwright import specs as S
from nightwright import export_schemes as ES
from nightwright.cli import cli
from nightwright.export_schemes.canonical import parse_canonical
from nightwright import specmeta

pytestmark = pytest.mark.fast

REPO = pathlib.Path(__file__).resolve().parent.parent
GOOD = """---
id: ACME-0001
title: "Thing"
status: accepted
kind: feature
owner: user
created: 2026-11-12
target_project: acme
outcome: users can do the thing
---

## Context

Why.

## Decision

Do it.

## Design

How.

## Acceptance criteria

- [ ] I can do the thing.

## Test plan

- run pytest

## Definition of done

- [ ] done
"""


def _spec(root, name="ACME-0001-thing.md", text=GOOD, project="acme"):
    d = root / project
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text(text)
    return p


def _edit(old, new, text=GOOD):
    assert old in text, old
    return text.replace(old, new, 1)


def _codes(problems, sev=None):
    return sorted(p.code for p in problems if sev is None or p.severity == sev)


def _cfg(tmp_path, **extra):
    return {"data_dir": str(tmp_path), "outbox_dir": str(tmp_path / "outbox"), **extra}


# ---- acceptance 2 --------------------------------------------------------------------

def test_valid_spec_clean(tmp_path):
    p = _spec(tmp_path)
    assert OL.lint_file(p) == []
    assert OL.lint_outbox(tmp_path).ok


def test_jerome_shape_passes(tmp_path):
    text = (GOOD.replace("ACME-0001", "JEROME-0022").replace("target_project: acme",
                                                              "target_project: jerome")
            .replace("status: accepted\nkind: feature\n", "status: shipped\n")
            .replace("- [ ] I can", "- [x] I can").replace("- [ ] done", "- [x] done")
            + "")
    text = text.replace("outcome:", "target_spec_path: /somewhere/JEROME-0022-thing.md\noutcome:")
    p = _spec(tmp_path, "JEROME-0022-thing.md", text, project="jerome")
    assert _codes(OL.lint_file(p), "error") == []


# ---- error rules 1-11 ----------------------------------------------------------------

def test_rule_frontmatter(tmp_path):
    bad_yaml = _spec(tmp_path, "ACME-0002-a.md", "---\nid: [unclosed\n---\n\n## Context\n")
    scalar = _spec(tmp_path, "ACME-0003-b.md", "---\n- a\n- b\n---\nbody\n")
    none = _spec(tmp_path, "ACME-0004-c.md", "no frontmatter\n")
    good = _spec(tmp_path)
    for p in (bad_yaml, scalar, none):
        assert _codes(OL.lint_file(p)) == ["frontmatter"]
    # a broken file does not stop the rest of the run
    rep = OL.lint_outbox(tmp_path)
    assert len(rep.files) == 4
    assert [f for f in rep.files if f.path == str(good)][0].errors == []
    assert not rep.ok


def test_rule_required_keys(tmp_path):
    p = _spec(tmp_path, text=_edit("owner: user\n", ""))
    assert _codes(OL.lint_file(p), "error") == ["required-keys"]
    p = _spec(tmp_path, text=_edit("created: 2026-11-12", "created: yesterday"))
    assert _codes(OL.lint_file(p), "error") == ["required-keys"]
    # date object (unquoted) and ISO string both pass
    p = _spec(tmp_path, text=_edit("created: 2026-11-12", 'created: "2026-11-12"'))
    assert OL.lint_file(p) == []
    # target_repo is not required (export never reads it)
    assert "target_repo" not in GOOD and OL.lint_file(_spec(tmp_path)) == []


@pytest.mark.parametrize("old,new", [
    ("id: ACME-0001", "id: acme_1"),
    ("id: ACME-0001", "id: ACME-0009"),     # number != filename
    ("id: ACME-0001", "id: OTHER-0001"),    # prefix != project dir
    ("target_project: acme", "target_project: other"),
])
def test_rule_id_format(tmp_path, old, new):
    p = _spec(tmp_path, text=_edit(old, new))
    assert "id-format" in _codes(OL.lint_file(p), "error")


def test_rule_id_prefix_from_directory(tmp_path):
    text = GOOD.replace("ACME-0001", "COMFORTASSISTANT-0001").replace(
        "target_project: acme", "target_project: comfort-assistant")
    p = _spec(tmp_path, "COMFORTASSISTANT-0001-x.md", text, project="comfort-assistant")
    assert _codes(OL.lint_file(p), "error") == []


@pytest.mark.parametrize("old,new", [
    ("status: accepted", "status: bogus"),
    ("status: accepted", "status: superseded"),
    ("kind: feature", "kind: saga"),
])
def test_rule_status(tmp_path, old, new):
    p = _spec(tmp_path, text=_edit(old, new))
    assert _codes(OL.lint_file(p), "error") == ["status"]
    ok = _edit("status: accepted", "status: superseded\nsuperseded_by: ACME-0002")
    assert _codes(OL.lint_file(_spec(tmp_path, text=ok)), "error") == []


def test_rule_sections(tmp_path):
    p = _spec(tmp_path, text=_edit("## Definition of done\n\n- [ ] done\n", ""))
    assert _codes(OL.lint_file(p), "error") == ["sections"]
    p = _spec(tmp_path, text=_edit("Do it.", ""))
    assert _codes(OL.lint_file(p), "error") == ["scheme", "sections"]


def test_rule_test_plan_na_on_feature(tmp_path):
    p = _spec(tmp_path, text=_edit("- run pytest", "N/A — nothing to run"))
    assert "test-plan" in _codes(OL.lint_file(p), "error")
    assert "test-plan" in _codes(OL.lint_file(_spec(tmp_path, text=_edit(
        "- run pytest", "n/a"))), "error")


def test_policy_may_be_na(tmp_path):
    t = _edit("kind: feature", "kind: policy", _edit("- run pytest", "N/A — policy"))
    assert "test-plan" not in _codes(OL.lint_file(_spec(tmp_path, text=t)))


def test_rule_placeholder(tmp_path):
    p = _spec(tmp_path, text=_edit("How.", "<option> one"))
    assert _codes(OL.lint_file(p), "error") == ["placeholder"]


def test_placeholder_only_for_accepted_plus(tmp_path):
    p = _spec(tmp_path, text=_edit("status: accepted", "status: draft",
                                   _edit("How.", "<option> one")))
    assert _codes(OL.lint_file(p), "error") == []
    # code spans are ignored
    p = _spec(tmp_path, text=_edit("How.", "talks about `<option>` only"))
    assert OL.lint_file(p) == []


def test_rule_unchecked_is_warning(tmp_path):
    # real outbox copies keep stale boxes (pull-status mirrors frontmatter only): warning
    t = _edit("status: accepted", "status: done")
    probs = OL.lint_file(_spec(tmp_path, text=t))
    assert _codes(probs, "error") == []
    assert _codes(probs, "warning") == ["unchecked", "unchecked"]
    shipped = _edit("status: accepted", "status: shipped")
    assert "unchecked" in _codes(OL.lint_file(_spec(tmp_path, text=shipped)), "warning")
    assert "unchecked" not in _codes(OL.lint_file(_spec(tmp_path)))  # accepted: not checked


def test_duplicate_ids_across_files(tmp_path):
    a = _spec(tmp_path, "ACME-0001-a.md")
    _spec(tmp_path, "ACME-0001-b.md")
    _spec(tmp_path, "ACME-0002-c.md")  # same id inside, different number prefix
    rep = OL.lint_outbox(tmp_path)
    dup = [f for f in rep.files if "duplicate" in _codes(f.errors)]
    assert len(dup) == 3
    # linting a single file still finds its siblings
    assert "duplicate" in _codes(OL.lint_file(a), "error")
    # the same prefix in another project directory is not a duplicate
    _spec(tmp_path, "ACME-0001-a.md", project="other")
    solo = _spec(tmp_path / "x", "ACME-0007-z.md")
    assert "duplicate" not in _codes(OL.lint_file(solo))


def test_rule_target_spec_path(tmp_path):
    t = _edit("outcome:", "target_spec_path: /r/docs/specs/ACME-0001-other.md\noutcome:")
    assert _codes(OL.lint_file(_spec(tmp_path, text=t)), "error") == ["target-spec-path"]
    t = _edit("outcome:", "target_spec_path: /r/docs/specs/ACME-0001-thing.md\noutcome:")
    assert OL.lint_file(_spec(tmp_path, text=t)) == []


# ---- scheme rule (11) ----------------------------------------------------------------

def test_scheme_problems_surface(tmp_path):
    p = _spec(tmp_path, text=_edit("Do it.", ""))
    fm, body = specmeta.split_frontmatter(p.read_text())
    want = ES.get("nw-native").validate(parse_canonical(fm, body, specmeta))
    assert want
    got = [x.message for x in OL.lint_file(p) if x.code == "scheme"]
    assert got == want


class _StrictScheme(ES.get("nw-native").__class__):
    name = "strict-test"

    def validate(self, spec):
        return ["strict problem one",
                "missing product-fit outcome signal: set outcome"]


@pytest.fixture
def strict_scheme(monkeypatch):
    monkeypatch.setitem(ES._REGISTRY, "strict-test", _StrictScheme())


def test_scheme_resolved_by_target_project(tmp_path, strict_scheme):
    # the cfg key is target_project ("acme"), the directory is named differently
    text = GOOD.replace("ACME-0001", "DIFFDIR-0001")
    p = _spec(tmp_path, "DIFFDIR-0001-x.md", text, project="diffdir")
    cfg = _cfg(tmp_path, outbox_targets={"acme": {"scheme": "strict-test"}})
    probs = OL.lint_file(p, cfg=cfg)
    assert [x.message for x in probs if x.code == "scheme"] == ["strict problem one"]
    # keyed by directory name instead -> not picked up
    cfg2 = _cfg(tmp_path, outbox_targets={"diffdir": {"scheme": "strict-test"}})
    assert "scheme" not in _codes(OL.lint_file(p, cfg=cfg2))


def test_product_fit_problem_is_warning(tmp_path, strict_scheme):
    p = _spec(tmp_path)
    cfg = _cfg(tmp_path, outbox_targets={"acme": {"scheme": "strict-test"}})
    probs = OL.lint_file(p, cfg=cfg)
    assert [x.severity for x in probs if x.code == "outcome"] == ["warning"]
    assert [x.message for x in probs if x.code == "scheme"] == ["strict problem one"]


def test_unknown_scheme_is_error(tmp_path):
    p = _spec(tmp_path)
    cfg = _cfg(tmp_path, outbox_targets={"acme": {"scheme": "nope"}})
    probs = [x for x in OL.lint_file(p, cfg=cfg) if x.code == "scheme"]
    assert len(probs) == 1 and probs[0].severity == "error" and "nope" in probs[0].message


def test_tools_mode_checks_nw_native_only(tmp_path, strict_scheme):
    _spec(tmp_path / "outbox")
    cfg = _cfg(tmp_path, outbox_targets={"acme": {"scheme": "strict-test"}})
    with_cfg = OL.lint_outbox(tmp_path / "outbox", cfg=cfg)
    without = OL.lint_outbox(tmp_path / "outbox")
    assert not with_cfg.ok and without.ok


# ---- warnings 12-14 ------------------------------------------------------------------

def test_rule_outcome_warning(tmp_path):
    t = _edit("outcome: users can do the thing\n", "", _edit("- [ ] I can do", "- [ ] it does"))
    probs = OL.lint_file(_spec(tmp_path, text=t))
    assert _codes(probs) == ["outcome"] and probs[0].severity == "warning"
    assert OL.lint_outbox(tmp_path).ok and OL.lint_outbox(tmp_path).exit_code() == 0
    assert OL.lint_outbox(tmp_path).exit_code(strict=True) == 1


def test_rule_reference_warning(tmp_path):
    _spec(tmp_path, "ACME-0002-other.md", GOOD.replace("ACME-0001", "ACME-0002"))
    ok = _edit("kind: feature", "kind: feature\ndepends_on: [ACME-0002, SPEC-0001]")
    assert OL.lint_file(_spec(tmp_path, text=ok)) == []
    bad = _edit("kind: feature", "kind: feature\ndepends_on: [ACME-0099]")
    probs = OL.lint_file(_spec(tmp_path, text=bad))
    assert _codes(probs) == ["reference"] and probs[0].severity == "warning"


def test_rule_design_warning(tmp_path):
    probs = OL.lint_file(_spec(tmp_path, text=_edit("## Design\n\nHow.\n\n", "")))
    assert _codes(probs) == ["design"] and probs[0].severity == "warning"
    epic = _edit("kind: feature", "kind: epic")
    assert _codes(OL.lint_file(_spec(tmp_path, text=epic))) == ["design"]
    epic = _edit("## Design", "## Architecture / cross-cutting design", epic)
    assert OL.lint_file(_spec(tmp_path, text=epic)) == []


# ---- validate_outbound reconciliation ------------------------------------------------

def test_validate_outbound_reconciled(tmp_path):
    only_repo = _spec(tmp_path, "ACME-0001-a.md")
    assert any("target_repo" in x for x in S.validate_outbound([only_repo]))
    assert OL.lint_file(only_repo) == []
    no_plan = _spec(tmp_path, "ACME-0002-b.md",
                    _edit("## Test plan\n\n- run pytest\n\n", "").replace("ACME-0001", "ACME-0002"))
    assert any("Test plan" in x for x in S.validate_outbound([no_plan]))
    assert "sections" in _codes(OL.lint_file(no_plan), "error")
    no_dod = _spec(tmp_path, "ACME-0003-c.md",
                   _edit("## Definition of done\n\n- [ ] done\n", "").replace("ACME-0001",
                                                                             "ACME-0003"))
    assert not any("Definition of done" in x for x in S.validate_outbound([no_dod]))
    assert "sections" in _codes(OL.lint_file(no_dod), "error")


# ---- file selection / read-only ------------------------------------------------------

def test_non_spec_files_skipped(tmp_path):
    _spec(tmp_path)
    d = tmp_path / "acme"
    for n in ("INDEX.md", "PROVENANCE.md", "PROJ-0001-x.contract.md", "notes.md"):
        (d / n).write_text("garbage, no frontmatter\n")
    (tmp_path / "INDEX.md").write_text("# index\n")
    rep = OL.lint_outbox(tmp_path)
    assert [pathlib.Path(f.path).name for f in rep.files] == ["ACME-0001-thing.md"]


def test_lint_input_forms(tmp_path):
    p = _spec(tmp_path)
    for root in (tmp_path, p.parent, p):
        assert len(OL.lint_outbox(root).files) == 1
    assert OL.lint_outbox(tmp_path, project="nope").files == []
    assert len(OL.lint_outbox(tmp_path, ids=["ACME-0001"]).files) == 1
    assert OL.lint_outbox(tmp_path, ids=["ACME-0002"]).files == []


def test_lint_is_read_only(tmp_path):
    _spec(tmp_path)
    _spec(tmp_path, "ACME-0002-bad.md", "---\nid: [\n---\n")
    (tmp_path / "acme" / "INDEX.md").write_text("x")

    def snap():
        return {str(p): (p.read_bytes(), p.stat().st_mtime_ns)
                for p in tmp_path.rglob("*") if p.is_file()}
    os.utime(tmp_path / "acme" / "INDEX.md", (1, 1))
    before = snap()
    OL.lint_outbox(tmp_path)
    OL.lint_outbox(tmp_path, cfg=_cfg(tmp_path))
    assert snap() == before


# ---- CLI -----------------------------------------------------------------------------

@pytest.fixture
def outbox_cli(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    import nightwright.cli as cli_mod
    monkeypatch.setattr(cli_mod, "load_config", lambda: cfg)
    out = tmp_path / "outbox"
    _spec(out)
    return tmp_path, out


def _run(*args):
    return CliRunner().invoke(cli, ["spec", "lint", *args])


def test_cli_single_id(outbox_cli):
    _, out = outbox_cli
    _spec(out, "ACME-0002-bad.md", _edit("Do it.", "").replace("ACME-0001", "ACME-0002"))
    r = _run("ACME-0001")
    assert r.exit_code == 0, r.output
    assert "ACME-0002" not in r.output and "1 spec(s) linted" in r.output
    r = _run("ACME-0002")
    assert r.exit_code == 1 and "error [scheme]" in r.output


def test_cli_all_and_project(outbox_cli):
    _, out = outbox_cli
    _spec(out, "OTHER-0001-x.md", GOOD.replace("ACME-0001", "OTHER-0001").replace(
        "target_project: acme", "target_project: other"), project="other")
    assert "2 spec(s) linted" in _run().output
    r = _run("--project", "other")
    assert "1 spec(s) linted" in r.output and r.exit_code == 0


def test_cli_exit_codes(outbox_cli):
    _, out = outbox_cli
    assert _run("NOPE-0001").exit_code == 2
    r = _run("SPEC-0561")
    assert r.exit_code == 2 and "tools/spec_lint.py" in r.output
    _spec(out, "ACME-0002-w.md", _edit("## Design\n\nHow.\n\n", "").replace("ACME-0001",
                                                                           "ACME-0002"))
    assert _run("ACME-0002").exit_code == 0
    assert _run("ACME-0002", "--strict").exit_code == 1
    _spec(out, "ACME-0003-e.md", _edit("Do it.", "").replace("ACME-0001", "ACME-0003"))
    assert _run().exit_code == 1


def test_cli_json_shape(outbox_cli):
    _, out = outbox_cli
    _spec(out, "ACME-0002-e.md", _edit("Do it.", "").replace("ACME-0001", "ACME-0002"))
    r = _run("--json")
    assert r.exit_code == 1
    data = json.loads(r.output)  # nothing but the JSON object on stdout
    assert data["ok"] is False
    by_id = {f["id"]: f for f in data["files"]}
    assert by_id["ACME-0001"]["errors"] == [] and by_id["ACME-0001"]["warnings"] == []
    assert {e["code"] for e in by_id["ACME-0002"]["errors"]} >= {"scheme", "sections"}
    assert set(by_id["ACME-0002"]) == {"id", "path", "errors", "warnings"}


def _tools():
    spec = importlib.util.spec_from_file_location("spec_lint_tools", REPO / "tools" / "spec_lint.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_tools_outbox_mode_matches_cli(outbox_cli, monkeypatch, capsys):
    _, out = outbox_cli
    _spec(out, "ACME-0002-e.md", _edit("Do it.", "").replace("ACME-0001", "ACME-0002"))
    cli_r = _run()
    mod = _tools()
    monkeypatch.setattr("sys.argv", ["spec_lint.py", "--outbox", str(out)])
    code = mod.main()
    tools_out = capsys.readouterr().out
    assert code == cli_r.exit_code == 1
    assert tools_out.strip() == cli_r.output.strip()
    # a clean tree: both 0
    (out / "acme" / "ACME-0002-e.md").unlink()
    assert mod.main() == 0 and _run().exit_code == 0


# ---- module / docs -------------------------------------------------------------------

def test_module_has_no_jsonschema_import():
    tree = ast.parse((REPO / "src" / "nightwright" / "outbox_lint.py").read_text())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    assert "jsonschema" not in names


def test_docs_mention_spec_lint():
    for rel in ("docs/specs/SCHEMA.md", "skills/nw-export/SKILL.md"):
        assert "nw spec lint" in (REPO / rel).read_text(), rel
