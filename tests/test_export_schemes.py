"""Pluggable export schemes (SPEC-0016): registry, wt-native, plain-md TemplateScheme,
user-config manifests (SPEC-0058), and static boilerplate placeholders (SPEC-0059).
Uses TMP outbox + TMP --to targets only.
"""
import importlib.util
import pathlib
import re

import pytest
from zoneinfo import ZoneInfo

from wt import export as E
from wt import export_schemes as ES
from wt import specs as S

ROOT = pathlib.Path(__file__).resolve().parent.parent
REAL_SPECS = ROOT / "docs" / "specs"
LINT = ROOT / "tools" / "spec_lint.py"


def _linter():
    spec = importlib.util.spec_from_file_location("spec_lint_schemes", LINT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _cfg(tmp_path, outbox_targets=None):
    specs_dir = tmp_path / "specs"
    specs_dir.mkdir()
    import shutil
    shutil.copy(REAL_SPECS / "TEMPLATE.md", specs_dir / "TEMPLATE.md")
    outbox = tmp_path / "outbox"
    return {"specs_dir": str(specs_dir), "outbox_dir": str(outbox),
            "outbox_targets": outbox_targets or {}, "timezone": "America/New_York",
            "_tz": ZoneInfo("America/New_York")}


def _scaffold(cfg, project, title, **extra_fm):
    path, outbound_id = S.scaffold_outbound(cfg, project, title=title)
    if extra_fm:
        text = path.read_text()
        fm, body = text.split("---\n", 2)[1:]
        lines = fm.rstrip("\n").splitlines()
        for k, v in extra_fm.items():
            lines.append(f"{k}: {v}")
        text = "---\n" + "\n".join(lines) + "\n---\n" + body
        path.write_text(text)
    return path, outbound_id


def _fill_acceptance_and_test_plan(path, acceptance_items, test_plan_lines):
    text = path.read_text()
    accept_block = "\n".join(f"- [ ] {a}" for a in acceptance_items)
    text = text.replace(
        "## Acceptance criteria\n\n<What must be TRUE for this to be correct. Each item must "
        "be objectively checkable.>\n\n- [ ] …\n- [ ] …",
        f"## Acceptance criteria\n\n{accept_block}")
    tp_block = "\n".join(test_plan_lines)
    text = text.replace(
        "## Test plan\n\n<HOW we prove the acceptance criteria. Required for feature/behavior "
        "specs; for a pure\npolicy/doc spec write \"N/A — <reason>\". Cover:>\n\n"
        "- **Automated tests:** which tests, where (`tests/…`), what they assert; fixtures "
        "needed.\n- **Manual verification:** exact commands/steps to run and the expected "
        "result.\n- **Regression guard:** how we confirm existing behavior is unchanged.",
        f"## Test plan\n\n{tp_block}")
    path.write_text(text)
    return path


# ---- registry ----------------------------------------------------------------------

def test_registry_available_includes_builtins():
    names = {s.name for s in ES.available()}
    assert "wt-native" in names
    assert "plain-md" in names


def test_registry_get_unknown_raises():
    with pytest.raises(ValueError, match="unknown export scheme"):
        ES.get("nope")


def test_registry_get_returns_registered_scheme():
    assert ES.get("wt-native").name == "wt-native"


# ---- wt-native (regression guard against SPEC-0015 shape) ---------------------------

def test_wt_native_is_default_and_matches_spec0015_shape(tmp_path):
    cfg = _cfg(tmp_path)
    src_path, outbound_id = _scaffold(cfg, "acme", "Adversarial reviewer")
    target = tmp_path / "target-repo"

    dest_spec, dest_contract = E.export_spec(cfg, outbound_id, to=str(target))

    assert dest_spec == target / "docs" / "specs" / src_path.name
    assert dest_spec.exists() and dest_contract.exists()
    text = dest_spec.read_text()
    assert "source_outbound_id" in text and "source_content_hash" in text
    assert "target_project" not in text

    # explicit --scheme wt-native reproduces the same output byte-for-byte
    target2 = tmp_path / "target-repo-explicit"
    dest_spec2, dest_contract2 = E.export_spec(cfg, outbound_id, scheme="wt-native",
                                               to=str(target2))
    assert dest_spec2.read_text() == text
    # the contract carries a seconds-resolution timestamp; mask it so a second boundary
    # between the two exports can't fail the comparison
    def _masked(p):
        return re.sub(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", "<ts>", p.read_text())
    assert _masked(dest_contract2) == _masked(dest_contract)


def test_wt_native_validate_blocks_missing_context(tmp_path):
    cfg = _cfg(tmp_path)
    path, outbound_id = _scaffold(cfg, "acme", "No context")
    text = path.read_text().replace(
        "## Context\n\n<Why are we doing this? What exists today? What's the problem or "
        "opportunity?>", "## Context\n\n")
    path.write_text(text)

    with pytest.raises(ValueError, match="fails validation"):
        E.export_spec(cfg, outbound_id, to=str(tmp_path / "target"))
    assert not (tmp_path / "target").exists()



def _feature_fixture(cfg, tmp_path, task_base="1.2"):
    path, outbound_id = _scaffold(cfg, "demo-target", "Adversarial reviewer",
                                  scope_paths="src/review/")
    _fill_acceptance_and_test_plan(
        path,
        ["I can review a session for contradictions", "Reviewer flags contradictions", "Reviewer output is deterministic"],
        ["- **Automated tests:** `uv run pytest tests/test_review.py`",
         "- **Manual verification:** run `wt review --fix` and confirm no crash.",
         "- **Regression guard:** `uv run pytest` full suite green."])
    return path, outbound_id



# ---- --emit narrows the run ----------------------------------------------------------

# ---- non-clobbering: owned task-spec file --------------------------------------------

# ---- declarative TemplateScheme (manifest tier, no Python) ---------------------------

def test_template_scheme_plain_md_renders_manifest_remap(tmp_path):
    cfg = _cfg(tmp_path)
    _, outbound_id = _feature_fixture(cfg, tmp_path)
    target = tmp_path / "plain-target"

    written = E.export_spec(cfg, outbound_id, scheme="plain-md", to=str(target))
    assert len(written) == 1
    text = written[0].read_text()
    assert "## Overview" in text
    assert "## Goals" in text
    assert "## Acceptance" in text
    assert "- [ ] Reviewer flags contradictions" in text
    assert "## Verification" in text


def test_template_scheme_validate_blocks_missing_required_field(tmp_path):
    cfg = _cfg(tmp_path)
    path, outbound_id = _scaffold(cfg, "acme", "No acceptance")
    text = path.read_text().replace(
        "## Acceptance criteria\n\n<What must be TRUE for this to be correct. Each item must "
        "be objectively checkable.>\n\n- [ ] …\n- [ ] …",
        "## Acceptance criteria\n\n")
    path.write_text(text)

    with pytest.raises(ValueError, match="fails validation"):
        E.export_spec(cfg, outbound_id, scheme="plain-md", to=str(tmp_path / "target"))



# ---- SPEC-0037: explicit task_id pin ------------------------------------------------

def test_wt_native_ignores_task_id(tmp_path):
    cfg = _cfg(tmp_path)
    path, outbound_id = _scaffold(cfg, "acme", "Native ignore pin")
    text = path.read_text()
    path.write_text(text.replace("---\n", "---\ntask_id: \"1.3.2\"\n", 1)
                    if "task_id:" not in text
                    else text)
    # simpler: append via _scaffold pattern
    text = path.read_text()
    fm, body = text.split("---\n", 2)[1:]
    if "task_id:" not in fm:
        path.write_text("---\n" + fm.rstrip() + "\ntask_id: \"1.3.2\"\n---\n" + body)
    dest_spec, _ = E.export_spec(cfg, outbound_id, scheme="wt-native",
                                 to=str(tmp_path / "native-target"), task_id="9.9.9")
    assert dest_spec.exists()


def _find_outbound(cfg, outbound_id):
    from wt.export import _find_outbound_spec
    return _find_outbound_spec(cfg, outbound_id)


# ---- CLI (`wt spec export --scheme/--emit`, `wt spec schemes`) -----------------------

def test_cli_spec_schemes_lists_builtins():
    from click.testing import CliRunner
    from wt.cli import cli
    r = CliRunner().invoke(cli, ["spec", "schemes"])
    assert r.exit_code == 0, r.output
    assert "wt-native" in r.output
    assert "plain-md" in r.output



# ---- SPEC-0048 outcome signal ------------------------------------------------------

def test_has_outcome_signal_helper():
    from wt.export_schemes.canonical import CanonicalSpec, has_outcome_signal
    base = dict(id="X-1", title="t", kind="feature", context="c", goals=[], non_goals=[],
                decision="d", design="", acceptance=[], test_plan="tp", depends_on=[])
    assert not has_outcome_signal(CanonicalSpec(**base))
    assert has_outcome_signal(CanonicalSpec(**{**base, "outcome": "Users can triage"}))
    assert has_outcome_signal(CanonicalSpec(**{**base, "acceptance": ["I can filter sessions"]}))
    assert has_outcome_signal(CanonicalSpec(**{**base, "acceptance": ["User can export CSV"]}))


# ---- SPEC-0058 user-config-dir manifest loading ------------------------------------

_PLAIN_MD_MANIFEST = '''
name = "plain-md"
description = "user override"
required_fields = ["id", "title", "context", "acceptance"]
naming = "{id}-{slug}.md"
artifact = "spec"

[[sections]]
label = "Overview override"
source = "context"
'''

_DEMO_MANIFEST = '''
name = "demo-user-scheme"
description = "a user-dropped scheme"
required_fields = ["id", "title", "context"]
naming = "{id}-{slug}.md"
artifact = "spec"

[[sections]]
label = "Body"
source = "context"
'''


def test_load_manifests_from_missing_dir_is_noop(tmp_path):
    from wt.export_schemes.template_scheme import load_manifests_from
    assert load_manifests_from(tmp_path / "does-not-exist", origin="user-config") == []


def test_load_manifests_from_reads_toml_and_stamps_origin(tmp_path):
    from wt.export_schemes.template_scheme import load_manifests_from
    schemes_dir = tmp_path / "schemes"
    schemes_dir.mkdir()
    (schemes_dir / "demo.toml").write_text(_DEMO_MANIFEST)
    loaded = load_manifests_from(schemes_dir, origin="user-config")
    assert len(loaded) == 1
    assert loaded[0].name == "demo-user-scheme"
    assert loaded[0].origin == "user-config"


def test_user_config_scheme_registers_and_overrides_builtin(tmp_path, monkeypatch):
    """Integration: a WT_CONFIG_DIR/schemes/*.toml is picked up on (re)import and a same-named
    manifest overrides the shipped built-in (SPEC-0058)."""
    import importlib
    from wt import export_schemes as ES
    from wt.export_schemes import template_scheme as TS

    schemes_dir = tmp_path / "schemes"
    schemes_dir.mkdir()
    (schemes_dir / "demo.toml").write_text(_DEMO_MANIFEST)
    (schemes_dir / "plain-md.toml").write_text(_PLAIN_MD_MANIFEST)

    snapshot = dict(ES._REGISTRY)
    monkeypatch.setenv("WT_CONFIG_DIR", str(tmp_path))
    try:
        importlib.reload(TS)
        names = {s.name for s in ES.available()}
        assert "demo-user-scheme" in names
        demo = ES.get("demo-user-scheme")
        assert demo.origin == "user-config"

        overridden = ES.get("plain-md")
        assert overridden.origin == "user-config"
        assert overridden.description == "user override"
    finally:
        ES._REGISTRY.clear()
        ES._REGISTRY.update(snapshot)
        monkeypatch.delenv("WT_CONFIG_DIR", raising=False)
        importlib.reload(TS)


def test_cli_spec_schemes_shows_user_config_origin(tmp_path, monkeypatch):
    import importlib
    from click.testing import CliRunner
    from wt import export_schemes as ES
    from wt.export_schemes import template_scheme as TS
    from wt.cli import cli

    schemes_dir = tmp_path / "schemes"
    schemes_dir.mkdir()
    (schemes_dir / "demo.toml").write_text(_DEMO_MANIFEST)

    snapshot = dict(ES._REGISTRY)
    monkeypatch.setenv("WT_CONFIG_DIR", str(tmp_path))
    try:
        importlib.reload(TS)
        r = CliRunner().invoke(cli, ["spec", "schemes"])
        assert r.exit_code == 0, r.output
        assert "demo-user-scheme" in r.output
        assert "[user-config]" in r.output
        wt_native_line = next(ln for ln in r.output.splitlines() if "wt-native" in ln)
        assert "[user-config]" not in wt_native_line
    finally:
        ES._REGISTRY.clear()
        ES._REGISTRY.update(snapshot)
        monkeypatch.delenv("WT_CONFIG_DIR", raising=False)
        importlib.reload(TS)


# ---- SPEC-0059 static sections + {{field}} placeholder substitution ----------------

def _canon(**over):
    from wt.export_schemes.canonical import CanonicalSpec
    base = dict(id="X-1", title="Sample", kind="feature", context="ctx text", goals=["g1"],
                non_goals=[], decision="dec text", design="", acceptance=["a1", "a2"],
                test_plan="tp", depends_on=[])
    base.update(over)
    return CanonicalSpec(**base)


def _render_one(manifest, spec):
    from wt.export_schemes.template_scheme import TemplateScheme
    scheme = TemplateScheme(manifest)

    class Ctx:
        pass
    ctx = Ctx()
    ctx.canonical = spec
    return scheme.render(ctx, {})[0].content


def test_static_section_verbatim_no_placeholders():
    m = {"name": "s", "sections": [{"label": "Fixed", "static": "always the same text"}]}
    content = _render_one(m, _canon())
    assert "## Fixed\n\nalways the same text" in content


def test_static_section_substitutes_string_field():
    m = {"name": "s", "sections": [{"label": "Ctx", "static": "Symptom: {{context}}"}]}
    content = _render_one(m, _canon(context="the bug"))
    assert "Symptom: the bug" in content


def test_static_section_substitutes_list_field_as_bullets():
    m = {"name": "s", "sections": [{"label": "Goals", "static": "{{goals}}"}]}
    content = _render_one(m, _canon(goals=["do X", "do Y"]))
    assert "- do X\n- do Y" in content


def test_static_section_checkbox_placeholder():
    m = {"name": "s", "sections": [{"label": "AC", "static": "{{acceptance:checkbox}}"}]}
    content = _render_one(m, _canon(acceptance=["works"]))
    assert "- [ ] works" in content


def test_static_section_mixes_fixed_text_and_placeholder():
    m = {"name": "s", "sections": [
        {"label": "AC", "static": "{{acceptance:checkbox}}\n\n- [ ] CI green (fixed)"}]}
    content = _render_one(m, _canon(acceptance=["thing works"]))
    assert "- [ ] thing works" in content
    assert "- [ ] CI green (fixed)" in content


def test_static_section_missing_field_renders_fill():
    m = {"name": "s", "sections": [{"label": "X", "static": "{{scope_paths}}"}]}
    content = _render_one(m, _canon())
    assert "<!-- fill -->" in content


def test_static_boilerplate_example_manifest_renders_full_shape():
    """SPEC-0059: the vendored example manifest (docs/examples/schemes/static-boilerplate-example.toml)
    loads, interleaves fixed boilerplate with wt-sourced content, and lands under
    specs/tasks/ (naming stays wt-id-based; no X.Y.Z Task-ID allocation, per Non-goals)."""
    from wt.export_schemes.template_scheme import TemplateScheme, _load_manifest

    manifest_path = ROOT / "docs" / "examples" / "schemes" / "static-boilerplate-example.toml"
    scheme = TemplateScheme(_load_manifest(manifest_path))
    assert scheme.name == "static-boilerplate-example"

    spec = _canon(id="DEMO-0001", title="Fix idle timeout",
                 context="Connection drops after 30s idle.",
                 goals=["Connection stays alive past 30s idle"],
                 decision="Reset the keepalive timer.", design="Touches the session layer only.",
                 acceptance=["Idle connection survives 5min soak test"],
                 depends_on=["DEMO-0000 (branch setup)"],
                 verification_commands="pytest tests/test_idle.py -v")
    out = _render_one({**_load_manifest(manifest_path)}, spec)

    assert "Connection drops after 30s idle" in out      # wt-sourced Context
    assert "layer-dependency-flow" in out                  # fixed Architectural Contracts
    assert "- Connection stays alive past 30s idle" in out       # wt-sourced functional requirement
    assert "- [ ] Idle connection survives 5min soak test" in out  # wt-sourced AC (checkbox)
    assert "pytest tests/test_idle.py -v" in out  # substituted verification command
    assert "DEMO-0000 (branch setup)" in out                # wt-sourced dependency

    ctx_relpath_scheme = TemplateScheme(_load_manifest(manifest_path))

    class Ctx:
        pass
    ctx = Ctx()
    ctx.canonical = spec
    files = ctx_relpath_scheme.render(ctx, {})
    assert files[0].relpath == "specs/tasks/DEMO-0001-fix-idle-timeout.md"


def test_static_boilerplate_example_manifest_is_config_dir_installable(tmp_path, monkeypatch):
    """The example manifest, once copied into WT_CONFIG_DIR/schemes/, is picked up exactly
    like any other user-config scheme (SPEC-0058 mechanism) — no wt core change (SPEC-0059)."""
    import importlib
    import shutil
    from wt import export_schemes as ES
    from wt.export_schemes import template_scheme as TS

    manifest_path = ROOT / "docs" / "examples" / "schemes" / "static-boilerplate-example.toml"
    schemes_dir = tmp_path / "schemes"
    schemes_dir.mkdir()
    shutil.copy(manifest_path, schemes_dir / "static-boilerplate-example.toml")

    snapshot = dict(ES._REGISTRY)
    monkeypatch.setenv("WT_CONFIG_DIR", str(tmp_path))
    try:
        importlib.reload(TS)
        scheme = ES.get("static-boilerplate-example")
        assert scheme.origin == "user-config"
    finally:
        ES._REGISTRY.clear()
        ES._REGISTRY.update(snapshot)
        monkeypatch.delenv("WT_CONFIG_DIR", raising=False)
        importlib.reload(TS)



def test_export_module_has_no_runtime_jsonschema_dependency(monkeypatch):
    """`tools/spec_lint.py` requires jsonschema (a dev-only dependency); wt.export and
    wt.export_schemes.wt_native previously exec-loaded that file at runtime just to reuse
    split_frontmatter/sections (the same class of bug SPEC-0001/commit 6b0b4b9 fixed for
    cli.py's promote path — it recurred independently in export.py and scheme.py). Both
    must use wt.specmeta (the runtime-safe extraction) instead, so the installed `wt` binary
    (whose venv has no jsonschema) doesn't crash on `wt spec export`."""
    import builtins
    import importlib
    import sys

    real_import = builtins.__import__

    def blocked_import(name, *a, **k):
        if name == "jsonschema" or name.startswith("jsonschema."):
            raise ModuleNotFoundError("No module named 'jsonschema'")
        return real_import(name, *a, **k)

    for mod in ("wt.export", "wt.export_schemes.wt_native", "wt.export_schemes.canonical"):
        sys.modules.pop(mod, None)
    monkeypatch.setattr(builtins, "__import__", blocked_import)
    try:
        importlib.import_module("wt.export")
        importlib.import_module("wt.export_schemes.wt_native")
    finally:
        monkeypatch.undo()
        for mod in ("wt.export", "wt.export_schemes.wt_native", "wt.export_schemes.canonical"):
            sys.modules.pop(mod, None)
        importlib.import_module("wt.export")


# ---- IDEA-263: wrapped bullet continuation lines --------------------------------------

def test_canonical_bullets_joins_wrapped_continuation_lines():
    """Bug found in a real spec (EXAMPLE-0030's Goals section): a hand-wrapped bullet's second
    physical line, indented with no leading `-`, was silently dropped instead of joined back
    onto the bullet — truncating the exported Functional Requirement mid-sentence."""
    from wt.export_schemes.canonical import _bullets
    text = ("- Every batch of a multi-batch session appears in the step summary, the Slack "
            "message, and the\n"
            "  consumer PR fan-in row — not just one arbitrarily-picked batch.\n"
            "- Reuse the existing, already-correct manifest-driven iteration (the Check Runs "
            "path) as the\n"
            "  template, rather than inventing a new batch-discovery mechanism.\n")
    items = _bullets(text)
    assert items == [
        "Every batch of a multi-batch session appears in the step summary, the Slack message, "
        "and the consumer PR fan-in row — not just one arbitrarily-picked batch.",
        "Reuse the existing, already-correct manifest-driven iteration (the Check Runs path) "
        "as the template, rather than inventing a new batch-discovery mechanism.",
    ]


def test_specs_bullet_items_joins_wrapped_continuation_lines():
    """Same fix, internal wt.specs copy (used by spec_tasks for org task generation)."""
    from wt.specs import _bullet_items
    text = ("- A single-batch session (today's common case) renders identically to current "
            "behavior (no\n"
            "  regression for the non-multi-batch path).\n")
    assert _bullet_items(text) == [
        "A single-batch session (today's common case) renders identically to current "
        "behavior (no regression for the non-multi-batch path).",
    ]


def test_canonical_bullets_blank_line_resets_continuation():
    """A blank line marks a paragraph break (e.g. between **Goals** and **Non-goals**) and
    must not merge into the previous bullet."""
    from wt.export_schemes.canonical import _split_goals
    text = ("**Goals**\n"
            "- First goal.\n"
            "\n"
            "**Non-goals**\n"
            "- First non-goal.\n")
    goals, non_goals = _split_goals(text)
    assert goals == ["First goal."]
    assert non_goals == ["First non-goal."]


