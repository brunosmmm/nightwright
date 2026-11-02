"""SPEC-0371: declared personas (user / project `.nw/personas/*.yaml`) over temp dirs."""
import re
from pathlib import Path

import pytest

from nightwright.agentic import persona_defs as PD
from nightwright.agentic import personas as PS
from nightwright.agentic import profiles as PR

TEMPLATE = "Review {{spec_id}} in {{worktree}}:\n{{diff}}\n"


@pytest.fixture
def dirs(tmp_path):
    """(user personas dir, project root, project personas dir)."""
    user = tmp_path / "cfg" / "personas"
    root = tmp_path / "proj"
    pdir = root / ".nw" / "personas"
    user.mkdir(parents=True)
    pdir.mkdir(parents=True)
    return user, root, pdir


def _put(directory, name, role="reviewer", template=TEMPLATE, extra="", body=None):
    (directory / f"{name}.md").write_text(template, encoding="utf-8")
    text = body if body is not None else (
        f"schema: nw.persona.v1\nname: {name}\nrole: {role}\ntemplate: {name}.md\n{extra}")
    (directory / f"{name}.yaml").write_text(text, encoding="utf-8")


def _cats(persona):
    return persona.schema["properties"]["findings"]["items"]["properties"]["category"]["enum"]


def test_reviewer_loads_read_only_with_declared_categories(dirs):
    user, root, pdir = dirs
    _put(pdir, "security_reviewer", extra="category: [crypto, secrets, injection]\n")
    reg = PD.load_registry(root, user_dir=user)
    p = reg["security_reviewer"]
    assert p.read_only and p.role == "reviewer"
    assert _cats(p) == ["crypto", "secrets", "injection", "other"]
    assert p.template == TEMPLATE and Path(p.template_file).is_absolute()
    assert PD.validate_persona_defs(root, user_dir=user) == []


def test_reviewer_without_category_gets_the_default_enum_and_other_is_not_duplicated(dirs):
    user, root, pdir = dirs
    _put(pdir, "plain_reviewer")
    _put(pdir, "other_reviewer", extra="category: [a, other]\n")
    reg = PD.load_registry(root, user_dir=user)
    assert _cats(reg["plain_reviewer"]) == PS.ADVERSARIAL_CATEGORIES
    assert _cats(reg["other_reviewer"]) == ["a", "other"]
    assert PS.ADVERSARIAL_REVIEWER_SCHEMA["properties"]["findings"]["items"]["properties"][
        "category"]["enum"] is PS.ADVERSARIAL_CATEGORIES            # the builtin is untouched


def test_implementer_and_triager_roles(dirs):
    user, root, pdir = dirs
    _put(user, "my_impl", role="implementer", template="{{spec_text}}\n")
    _put(pdir, "my_triage", role="triager", template="{{triage_input}}\n")
    reg = PD.load_registry(root, user_dir=user)
    assert reg["my_impl"].schema == PS.IMPLEMENTER_SCHEMA and not reg["my_impl"].read_only
    assert reg["my_triage"].schema == PS.GATE_TRIAGER_SCHEMA and reg["my_triage"].read_only
    assert PD.reviewer_names(root, user_dir=user) == list(PS.REVIEWER_PERSONAS)


def test_reviewer_names_and_personas_for_order(dirs):
    user, root, pdir = dirs
    _put(pdir, "zeta_reviewer")
    _put(user, "alpha_reviewer")
    assert PD.reviewer_names(root, user_dir=user) == [
        *PS.REVIEWER_PERSONAS, "alpha_reviewer", "zeta_reviewer"]
    names = [p.name for p in PD.personas_for(root, user_dir=user)]
    assert names[:len(PS.PERSONAS)] == list(PS.PERSONAS)
    assert names[len(PS.PERSONAS):] == ["alpha_reviewer", "zeta_reviewer"]


@pytest.mark.parametrize("where", ["user", "project"])
def test_builtin_shadowing_is_refused(dirs, where):
    user, root, pdir = dirs
    _put(user if where == "user" else pdir, "conformance_reviewer")
    msgs = PD.validate_persona_defs(root, user_dir=user)
    assert len(msgs) == 1
    assert "conformance_reviewer.yaml" in msgs[0]
    assert "name 'conformance_reviewer' is a builtin persona; pick another name" in msgs[0]
    assert PD.load_registry(root, user_dir=user)["conformance_reviewer"] is \
        PS.PERSONAS["conformance_reviewer"]


def test_cross_layer_duplicate_names_both_files(dirs):
    user, root, pdir = dirs
    _put(user, "sec_reviewer")
    _put(pdir, "sec_reviewer")
    msgs = PD.validate_persona_defs(root, user_dir=user)
    assert len(msgs) == 1
    assert msgs[0].startswith("proj:.nw/personas/sec_reviewer.yaml")
    assert "already declared by user:sec_reviewer.yaml" in msgs[0]
    assert PD.load_registry(root, user_dir=user)["sec_reviewer"].template_file.startswith(
        str(user.resolve()))                                        # the lower layer wins


@pytest.mark.parametrize("extra,needle", [
    ("read_only: false\n", "read_only: unknown key"),
    ("output_schema: {}\n", "output_schema: unknown key"),
    ("schema_ref: x\n", "schema_ref: unknown key"),
    ("invariant: x\n", "invariant: unknown key"),
    ("category: not-a-list\n", "category: must be a list"),
    ("category: [Bad-Slug]\n", "category: 'Bad-Slug' must match"),
    ("category: [a, a]\n", "category: duplicate entries: a"),
    ("category: [" + ", ".join(f"c{i}" for i in range(25)) + "]\n", "category: at most 24"),
    ("description: [x]\n", "description: must be a string"),
])
def test_reviewer_key_rules(dirs, extra, needle):
    user, root, pdir = dirs
    _put(pdir, "sec_reviewer", extra=extra)
    msgs = PD.validate_persona_defs(root, user_dir=user)
    assert any(needle in m and m.startswith("proj:.nw/personas/sec_reviewer.yaml") for m in msgs), msgs
    assert "sec_reviewer" not in PD.load_registry(root, user_dir=user)


@pytest.mark.parametrize("body,needle", [
    ("schema: nw.persona.v0\nname: x_rev\nrole: reviewer\ntemplate: x_rev.md\n", "schema: must be"),
    ("schema: nw.persona.v1\nname: x_rev\nrole: boss\ntemplate: x_rev.md\n", "role: must be one of"),
    ("schema: nw.persona.v1\nname: x_rev\ntemplate: x_rev.md\n", "role: must be one of"),
    ("schema: nw.persona.v1\nname: other\nrole: reviewer\ntemplate: x_rev.md\n",
     "name: 'other' must equal the file stem 'x_rev'"),
    ("schema: nw.persona.v1\nname: X\nrole: reviewer\ntemplate: x_rev.md\n", "name: must match"),
    ("schema: nw.persona.v1\nname: x_rev\nrole: reviewer\n", "template: required"),
    ("schema: nw.persona.v1\nname: x_rev\nrole: reviewer\ntemplate: nope.md\n",
     "template: 'nope.md' is not a file"),
    ("schema: nw.persona.v1\nname: x_rev\nrole: reviewer\ntemplate: ../x_rev.md\n",
     "template: '../x_rev.md' must be a file name"),
    ("schema: nw.persona.v1\nname: x_rev\nrole: reviewer\ntemplate: sub/x.md\n",
     "must be a file name"),
    ("- a list\n", "<file>: must be a mapping"),
    ("key: [unclosed\n", "<file>:"),
    ("schema: nw.persona.v1\nname: x_rev\nrole: implementer\ntemplate: x_rev.md\ncategory: [a]\n",
     "category: only a reviewer-role persona"),
])
def test_definition_rules(dirs, body, needle):
    user, root, pdir = dirs
    _put(pdir, "x_rev", body=body)
    msgs = PD.validate_persona_defs(root, user_dir=user)
    assert any(needle in m for m in msgs), msgs
    assert "x_rev" not in PD.load_registry(root, user_dir=user)


def test_template_symlink_out_and_non_utf8(dirs, tmp_path):
    user, root, pdir = dirs
    outside = tmp_path / "outside.md"
    outside.write_text(TEMPLATE)
    (pdir / "link_rev.yaml").write_text(
        "schema: nw.persona.v1\nname: link_rev\nrole: reviewer\ntemplate: link.md\n")
    (pdir / "link.md").symlink_to(outside)
    _put(pdir, "bad_utf8")
    (pdir / "bad_utf8.md").write_bytes(b"\xff\xfe{{spec_id}}")
    msgs = PD.validate_persona_defs(root, user_dir=user)
    assert any("link_rev.yaml: template: 'link.md' resolves outside" in m for m in msgs), msgs
    assert any("bad_utf8.yaml: template: 'bad_utf8.md' is not valid UTF-8" in m for m in msgs), msgs


def test_definition_symlink_out_is_refused(dirs, tmp_path):
    user, root, pdir = dirs
    real = tmp_path / "elsewhere"
    real.mkdir()
    _put(real, "ghost_rev")
    (pdir / "ghost_rev.md").write_text(TEMPLATE)
    (pdir / "ghost_rev.yaml").symlink_to(real / "ghost_rev.yaml")
    msgs = PD.validate_persona_defs(root, user_dir=user)
    assert any("ghost_rev.yaml: <file>: resolves outside its directory" in m for m in msgs), msgs


def test_placeholder_rules_name_placeholder_line_and_role(dirs):
    user, root, pdir = dirs
    _put(pdir, "w_rev", template="ok {{spec_id}}\n{{idea_text}} and {{made_up}}\n")
    _put(pdir, "w_impl", role="implementer", template="{{diff}}\n")
    msgs = PD.validate_persona_defs(root, user_dir=user)
    assert any("w_rev.yaml: template: line 2: placeholder {{idea_text}} is out of role "
               "for role 'reviewer'" in m for m in msgs), msgs
    assert any("line 2: placeholder {{made_up}} is unknown for role 'reviewer'" in m
               for m in msgs), msgs
    assert any("w_impl.yaml: template: line 1: placeholder {{diff}} is out of role "
               "for role 'implementer'" in m for m in msgs), msgs


def test_all_problems_in_one_call_and_good_sibling_loads(dirs):
    user, root, pdir = dirs
    _put(pdir, "good_rev", extra="category: [crypto]\n")
    _put(pdir, "bad_one", extra="read_only: true\n")
    _put(pdir, "bad_two", role="boss")
    _put(user, "bad_three", template="{{nope}}\n")
    (pdir / "notes.txt").write_text("ignored")
    msgs = PD.validate_persona_defs(root, user_dir=user)
    assert len(msgs) == 3, msgs
    reg = PD.load_registry(root, user_dir=user)
    assert "good_rev" in reg and not {"bad_one", "bad_two", "bad_three"} & set(reg)


def test_only_the_passed_main_checkout_is_read(dirs, tmp_path):
    user, root, pdir = dirs
    worktree = tmp_path / "worktrees" / "camp"
    (worktree / ".nw" / "personas").mkdir(parents=True)
    _put(worktree / ".nw" / "personas", "rogue_rev")
    _put(pdir, "main_rev")
    reg = PD.load_registry(root, user_dir=user)
    assert "main_rev" in reg and "rogue_rev" not in reg
    assert PD.load_registry(None, user_dir=user).keys() == PS.PERSONAS.keys()


def test_missing_and_empty_dirs_are_fine(tmp_path):
    assert PD.validate_persona_defs(tmp_path / "nope", user_dir=tmp_path / "nouser") == []
    assert PD.load_registry(tmp_path / "nope", user_dir=tmp_path / "nouser").keys() == \
        PS.PERSONAS.keys()


def test_get_persona_builtin_only_without_a_project_root(dirs, monkeypatch, tmp_path):
    user, root, pdir = dirs
    _put(pdir, "sec_reviewer")
    monkeypatch.setenv("NW_CONFIG_DIR", str(tmp_path / "cfg"))
    with pytest.raises(ValueError, match="unknown persona 'sec_reviewer'"):
        PS.get_persona("sec_reviewer")
    assert PS.get_persona("sec_reviewer", root).role == "reviewer"
    assert PS.get_persona("implementer") is PS.PERSONAS["implementer"]
    with pytest.raises(ValueError):
        PS.get_persona("nope", root)


def test_declared_persona_in_another_project_is_not_visible(dirs, tmp_path):
    user, root, pdir = dirs
    _put(pdir, "sec_reviewer")
    other = tmp_path / "other"
    other.mkdir()
    assert "sec_reviewer" not in PD.load_registry(other, user_dir=user)


# ---- role placeholder subsets -----------------------------------------------------------

_TPL = Path(PS.__file__).resolve().parent / "personas"
_ROLE_TEMPLATES = {"implementer": ["implementer.md"],
                   "reviewer": ["conformance_reviewer.md", "test_reviewer.md",
                                "adversarial_reviewer.md"],
                   "triager": ["gate_triager.md"]}


@pytest.mark.parametrize("role", sorted(_ROLE_TEMPLATES))
def test_role_placeholders_cover_the_builtin_templates(role):
    for name in _ROLE_TEMPLATES[role]:
        used = set(re.findall(r"\{\{(\w+)\}\}", (_TPL / name).read_text(encoding="utf-8")))
        assert used <= PS.ROLE_PLACEHOLDERS[role], (name, used - PS.ROLE_PLACEHOLDERS[role])
    assert PS.ROLE_PLACEHOLDERS[role] <= set(PS.PLACEHOLDERS)
    for ph in ("idea_text", "template_text", "write_problems", "write_rules", "epic_text",
               "rebase_target", "conflicts", "base_diff", "landed", "regenerate"):
        assert ph not in PS.ROLE_PLACEHOLDERS[role]


# ---- inertness --------------------------------------------------------------------------

def test_declaring_a_persona_changes_no_builtin_table_or_profile(dirs, monkeypatch, tmp_path):
    user, root, pdir = dirs
    before = (dict(PS.PERSONAS), PS.REVIEWER_PERSONAS, dict(PS.ADOPT_REGISTRY))
    cfg = {"config_dir": str(tmp_path / "cfg"), "agentic": {"profiles": {}}}
    prof_before = PR.resolve_profile(cfg, "balanced", project_root=root)
    _put(pdir, "security_reviewer")
    PD.load_registry(root, user_dir=user)
    assert (dict(PS.PERSONAS), PS.REVIEWER_PERSONAS, dict(PS.ADOPT_REGISTRY)) == before
    assert "security_reviewer" not in PS.PERSONAS
    assert PR.resolve_profile(cfg, "balanced", project_root=root) == prof_before
    # a profile naming the declared persona is still rejected, as before (at resolve time)
    cfg["agentic"]["profiles"]["balanced"] = {"reviewers": ["security_reviewer"]}
    with pytest.raises(ValueError, match="security_reviewer"):
        PR.resolve_profile(cfg, "balanced", project_root=root)
