"""SPEC-0370: the persona registry as data: equality with the old hand-written tables, the strict
validator, the three layers and the builtin integrity."""
import ast
from pathlib import Path

import pytest
import yaml

from nightwright.agentic import persona_registry as R
from nightwright.agentic import personas as PS

pytestmark = pytest.mark.fast

AGENTIC = Path(PS.__file__).resolve().parent

# Frozen copy of the pre-SPEC-0370 tables: (name, template, schema const, read_only, invariant const)
OLD_PERSONAS = [
    ("implementer", "implementer.md", "IMPLEMENTER_SCHEMA", False, "INVARIANT"),
    ("gate_triager", "gate_triager.md", "GATE_TRIAGER_SCHEMA", True, "INVARIANT"),
    ("conformance_reviewer", "conformance_reviewer.md", "CONFORMANCE_REVIEWER_SCHEMA", True,
     "INVARIANT"),
    ("test_reviewer", "test_reviewer.md", "TEST_REVIEWER_SCHEMA", True, "INVARIANT"),
    ("adversarial_reviewer", "adversarial_reviewer.md", "ADVERSARIAL_REVIEWER_SCHEMA", True,
     "INVARIANT"),
    ("spec_reviewer", "spec_reviewer.md", "SPEC_REVIEWER_SCHEMA", True, "SPEC_REVIEW_INVARIANT"),
    ("spec_writer", "spec_writer.md", "SPEC_WRITER_SCHEMA", False, "SPEC_WRITER_INVARIANT"),
    ("review_triager", "review_triager.md", "REVIEW_TRIAGER_SCHEMA", True, "INVARIANT"),
    ("divergence_triager", "divergence_triager.md", "DIVERGENCE_TRIAGER_SCHEMA", True, "INVARIANT"),
    ("conflict_resolver", "conflict_resolver.md", "CONFLICT_RESOLVER_SCHEMA", False,
     "RESOLVER_INVARIANT"),
]
OLD_REVIEWERS = ("conformance_reviewer", "test_reviewer", "adversarial_reviewer")
OLD_ADOPT = {
    "implementer": ("implement", ()),
    "gate_triager": ("triage", ()),
    "review_triager": ("review", ("review_triager",)),
    "divergence_triager": ("divergence", ("divergence_triager",)),
    "conformance_reviewer": ("review", None),
    "test_reviewer": ("review", None),
    "adversarial_reviewer": ("review", None),
}


def test_derived_tables_equal_the_old_ones():
    assert list(PS.PERSONAS) == [row[0] for row in OLD_PERSONAS]
    for name, template, schema, read_only, invariant in OLD_PERSONAS:
        p = PS.PERSONAS[name]
        assert (p.name, p.template_file, p.read_only) == (name, template, read_only)
        assert p.schema is getattr(PS, schema)
        assert p.invariant is getattr(PS, invariant)
        assert p.template == (AGENTIC / "personas" / template).read_text(encoding="utf-8")
    assert PS.REVIEWER_PERSONAS == OLD_REVIEWERS
    assert PS.ADOPT_REGISTRY == OLD_ADOPT
    assert list(PS.ADOPT_REGISTRY) == list(OLD_ADOPT)


def test_no_hand_written_tables():
    tree = ast.parse((AGENTIC / "personas.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id in ("PERSONAS", "REVIEWER_PERSONAS",
                                                        "ADOPT_REGISTRY"):
                    assert not isinstance(node.value, (ast.Tuple, ast.Dict, ast.List)), t.id
    outside = []
    for node in tree.body:
        calls = [n for n in ast.walk(node) if isinstance(n, ast.Call)
                 and getattr(n.func, "id", None) == "Persona"]
        if calls and not (isinstance(node, ast.FunctionDef) and node.name == "_build_persona"):
            outside.append(node)
    assert not outside


def test_builtin_integrity():
    files = sorted((AGENTIC / "persona_defs").glob("*.yaml"))
    assert [f.stem for f in files] == sorted(r[0] for r in OLD_PERSONAS)
    assert not list((AGENTIC / "persona_defs").glob("*.yml"))
    defs = R.load_builtin(PS.KNOWN)               # raises when any builtin is invalid
    assert len({d.order for d in defs}) == len(defs) == 10
    for d in defs:
        text = d.template_path.read_text(encoding="utf-8")
        assert d.template_path.parent == AGENTIC / "personas"
        assert set(R._PLACEHOLDER_RE.findall(text)) <= set(PS.PLACEHOLDERS)


# ---- validator -----------------------------------------------------------------------------

def _doc(**over):
    doc = {"schema": "nw.persona.v1", "name": "sec", "role": "reviewer", "template": "sec.md",
           "description": "security review"}
    doc.update(over)
    return {k: v for k, v in doc.items() if v is not None}


def _validate(tmp_path, doc, layer="project", stem="sec", template="Hello {{diff}}"):
    if template is not None:
        (tmp_path / "sec.md").write_text(template, encoding="utf-8")
    return R.validate_persona_doc(doc, origin="x.yaml", layer=layer, stem=stem,
                                  known=PS.KNOWN, directory=tmp_path)


def test_valid_custom_doc(tmp_path):
    assert _validate(tmp_path, _doc(category=["security"])) == []


@pytest.mark.parametrize("over,layer,stem,template,expect", [
    ({"schema": "nw.persona.v2"}, "project", "sec", "x", "schema: must be nw.persona.v1"),
    ({"name": "other"}, "project", "sec", "x", "must equal the file stem"),
    ({"name": "Bad-Name"}, "project", "Bad-Name", "x", "name: must be a string matching"),
    ({"bogus": 1}, "project", "sec", "x", "bogus: unknown key"),
    ({"output_schema": "implementer"}, "project", "sec", "x",
     "output_schema: allowed only in a builtin"),
    ({"order": 5}, "project", "sec", "x", "order: allowed only in a builtin"),
    ({"invariant": "default"}, "user", "sec", "x", "invariant: allowed only in a builtin"),
    ({"read_only": True}, "user", "sec", "x", "read_only: allowed only in a builtin"),
    ({"adopt": {"step": "review", "personas": []}}, "user", "sec", "x",
     "adopt: allowed only in a builtin"),
    ({"role": "system"}, "project", "sec", "x", "role: 'system' is allowed only in a builtin"),
    ({"template": None}, "project", "sec", "x", "template: must be a bare file name"),
    ({"template": "../sec.md"}, "project", "sec", "x", "template: must be a bare file name"),
    ({"template": "gone.md"}, "project", "sec", "x", "template: gone.md not found"),
    ({}, "project", "sec", "Hi {{nonsense}}", "unknown placeholders: {{nonsense}}"),
    ({"category": "security"}, "project", "sec", "x", "category: must be a list"),
    ({"description": " "}, "project", "sec", "x", "description: must be a non-empty string"),
])
def test_validator_negatives(tmp_path, over, layer, stem, template, expect):
    errs = _validate(tmp_path, _doc(**over), layer=layer, stem=stem, template=template)
    assert any(expect in e and e.startswith("x.yaml: ") for e in errs), errs


def test_multi_error_file_reports_everything(tmp_path):
    errs = _validate(tmp_path, _doc(schema="nope", name="other", bogus=1, role="system",
                                    order=3, category="x"), template="{{zzz}}")
    text = "\n".join(errs)
    for expect in ("schema:", "file stem", "bogus: unknown key", "role: 'system'", "order:",
                   "category:", "unknown placeholders"):
        assert expect in text, text
    assert len(errs) >= 7


def test_non_mapping_doc(tmp_path):
    assert _validate(tmp_path, ["a"]) == ["x.yaml: <file>: must be a YAML mapping"]


def test_builtin_keys_validated(tmp_path):
    base = yaml.safe_load((AGENTIC / "persona_defs" / "conformance_reviewer.yaml").read_text())
    ok = R.validate_persona_doc(base, origin="b", layer="builtin", stem="conformance_reviewer",
                                known=PS.KNOWN)
    assert ok == []
    bad = dict(base, output_schema="nope", invariant="nope", order="x", read_only=True,
               adopt={"step": "review", "personas": []})
    text = "\n".join(R.validate_persona_doc(bad, origin="b", layer="builtin",
                                            stem="conformance_reviewer", known=PS.KNOWN))
    for key in ("output_schema:", "invariant:", "order:", "read_only:", "adopt:"):
        assert key in text, text


def test_duplicate_builtin_order_and_yml(tmp_path, monkeypatch):
    d = tmp_path / "defs"
    d.mkdir()
    for n in ("conformance_reviewer", "test_reviewer"):
        doc = yaml.safe_load((AGENTIC / "persona_defs" / f"{n}.yaml").read_text())
        doc["order"] = 7
        (d / f"{n}.yaml").write_text(yaml.safe_dump(doc))
    (d / "extra.yml").write_text("x: 1\n")
    monkeypatch.setattr(R, "_DEFS_DIR", d)
    entries = R._builtin_entries(PS.KNOWN)
    problems = [p for e in entries for p in e.errors]
    assert any("order: 7 duplicates conformance_reviewer" in p for p in problems)
    assert any("extra.yml: <file>: rename to .yaml" in p for p in problems)
    with pytest.raises(ValueError):
        R.load_builtin(PS.KNOWN)


# ---- layers --------------------------------------------------------------------------------

def _custom(directory, name, **over):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.md").write_text("Review {{diff}}", encoding="utf-8")
    doc = _doc(name=name, template=f"{name}.md", **over)
    (directory / f"{name}.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")


def test_layers_scope_origin_and_precedence(tmp_path):
    cfg = {"config_dir": str(tmp_path / "cfg")}
    proj = tmp_path / "proj"
    _custom(tmp_path / "cfg" / "personas", "sec", description="user one")
    _custom(proj / ".nw" / "personas", "sec", description="project one")
    _custom(tmp_path / "cfg" / "personas", "only_user")
    reg = R.load_registry(cfg, project_root=proj, known=PS.KNOWN)
    scopes = [(e.name, e.scope) for e in reg.entries if e.scope != "builtin"]
    assert scopes == [("only_user", "user"), ("sec", "user"), ("sec", "project")]
    winner = reg.resolved()["sec"]
    assert winner.scope == "project" and winner.defn.description == "project one"
    assert winner.origin == str(proj / ".nw" / "personas" / "sec.yaml")
    assert reg.resolved()["only_user"].scope == "user"
    # the derived module tables never see custom definitions
    assert "sec" not in PS.PERSONAS and "sec" not in PS.REVIEWER_PERSONAS
    assert "sec" not in PS.ADOPT_REGISTRY
    assert [d.name for d in reg.builtin_defs()] == list(PS.PERSONAS)


def test_custom_persona_is_refused_by_profiles_and_never_derived(tmp_path):
    """A valid user/project reviewer is registered, yet no module table nor a profile accepts it."""
    from nightwright.agentic import profiles as PF
    cfg = {"config_dir": str(tmp_path / "cfg")}
    proj = tmp_path / "proj"
    _custom(tmp_path / "cfg" / "personas", "sec_user")
    _custom(proj / ".nw" / "personas", "sec")
    reg = R.load_registry(cfg, project_root=proj, known=PS.KNOWN)
    assert {"sec", "sec_user"} <= set(reg.resolved())
    assert all(reg.resolved()[n].defn is not None for n in ("sec", "sec_user"))
    # personas.py builds its tables from the builtin layer alone, never from load_registry
    tree = ast.parse((AGENTIC / "personas.py").read_text(encoding="utf-8"))
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert "load_builtin" in attrs
    # (`get_persona` may reach the SPEC-0371 declared registry lazily; the tables never do)
    pr_attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
                and isinstance(n.value, ast.Name) and n.value.id == "PR"}
    assert not ({"load_registry", "load_layers"} & (pr_attrs | {"load_layers"} & names))
    assert PS.REVIEWER_PERSONAS == R.reviewer_personas(R.load_builtin(PS.KNOWN))
    # profile validation and resolution refuse the custom names
    assert "sec" not in PF._persona_names()
    problems = PF.validate_profile_doc({"personas": {"sec": {}}}, origin="p.yaml", in_file=False)
    assert any("personas.sec: unknown persona" in p for p in problems)
    with pytest.raises(ValueError, match="unknown reviewer persona 'sec'"):
        PF._check_reviewers({"name": "p", "reviewers": ["sec"], "personas": {"sec": {}}})


def test_refused_entries_never_displace_a_valid_one(tmp_path):
    cfg = {"config_dir": str(tmp_path / "cfg")}
    d = tmp_path / "cfg" / "personas"
    _custom(d, "implementer")                 # shadows a builtin
    _custom(d, "sec")
    (d / "sec.yml").write_text("x: 1\n")       # conflicts with sec.yaml
    reg = R.load_registry(cfg, known=PS.KNOWN)
    res = reg.resolved()
    assert res["implementer"].scope == "builtin" and res["implementer"].defn is not None
    assert res["sec"].path.suffix == ".yaml" and res["sec"].defn is not None
    refused = [e for e in reg.entries if e.refused]
    assert sorted(e.path.name for e in refused) == ["implementer.yaml", "sec.yml"]
    assert all(e.errors for e in refused)


def test_non_string_builtin_keys_do_not_crash_the_validator():
    doc = _doc(output_schema=["x"], invariant=["y"], role="system", read_only=True,
               adopt={"step": ["z"], "personas": []}, order=1)
    errs = R.validate_persona_doc(doc, origin="o", layer="builtin", stem="sec_reviewer",
                                  known=PS.KNOWN, directory=None)
    assert any("output_schema" in e for e in errs) and any("invariant" in e for e in errs)
    assert any("adopt" in e for e in errs)


def test_project_layer_only_from_project_root(tmp_path):
    cfg = {"config_dir": str(tmp_path / "cfg")}
    other = tmp_path / "worktree"
    _custom(other / ".nw" / "personas", "sec")
    reg = R.load_registry(cfg, project_root=None, known=PS.KNOWN)
    assert "sec" not in reg.resolved()
    reg = R.load_registry(cfg, project_root=tmp_path / "elsewhere", known=PS.KNOWN)
    assert "sec" not in reg.resolved()


def test_user_file_named_like_a_builtin_is_refused(tmp_path):
    cfg = {"config_dir": str(tmp_path / "cfg")}
    _custom(tmp_path / "cfg" / "personas", "implementer")
    reg = R.load_registry(cfg, known=PS.KNOWN)
    user = [e for e in reg.entries if e.scope == "user"][0]
    assert user.defn is None
    assert any("shadows builtin persona" in e for e in user.errors)


def test_bad_stem_and_yml_files_are_broken_definitions(tmp_path):
    cfg = {"config_dir": str(tmp_path / "cfg")}
    d = tmp_path / "cfg" / "personas"
    d.mkdir(parents=True)
    (d / "Bad-Stem.yaml").write_text("x: 1\n")
    (d / "oldstyle.yml").write_text("x: 1\n")
    (d / "notes.txt").write_text("ignored")
    reg = R.load_registry(cfg, known=PS.KNOWN)
    user = {e.name: e for e in reg.entries if e.scope == "user"}
    assert set(user) == {"Bad-Stem", "oldstyle"}
    assert "must match" in user["Bad-Stem"].errors[0]
    assert "rename to .yaml" in user["oldstyle"].errors[0]


def test_derivations_from_defs():
    defs = R.load_builtin(PS.KNOWN)
    assert R.reviewer_personas(defs) == OLD_REVIEWERS
    assert list(R.adopt_registry(defs)) == list(OLD_ADOPT)
