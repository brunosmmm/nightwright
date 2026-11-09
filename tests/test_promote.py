"""Idea -> spec promotion (SPEC-0013): next_spec_number/slugify, scaffold_from_idea (Context
prefill, source_idea, reciprocal :SPEC: property + SPECCED state), idempotence, CLI.

All scaffolding happens in a tmp specs dir (never docs/specs/) — see cfg["specs_dir"]."""
import importlib.util
import pathlib
import re
import shutil

from click.testing import CliRunner
from zoneinfo import ZoneInfo

from nightwright import org_write as W
from nightwright import specs as S
from nightwright.cli import cli
from nightwright.org import load_tasks
import pytest

pytestmark = pytest.mark.fast

ROOT = pathlib.Path(__file__).resolve().parent.parent
REAL_SPECS = ROOT / "docs" / "specs"
LINT = ROOT / "tools" / "spec_lint.py"


def _linter():
    spec = importlib.util.spec_from_file_location("spec_lint", LINT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _cfg(tmp_path):
    org = tmp_path / "org"
    org.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    specs_dir = tmp_path / "specs"
    specs_dir.mkdir()
    for name in ("TEMPLATE.md", "TEMPLATE-epic.md"):
        shutil.copy(REAL_SPECS / name, specs_dir / name)
    return {"org_files": [str(org)], "org_todo_keywords": ["TODO", "|", "DONE"],
            "org_idea_keywords": ["IDEA", "INCUBATE", "SPECCED", "|", "PROMOTED", "DROPPED"],
            "timezone": "America/New_York", "_tz": ZoneInfo("America/New_York"),
            "data_dir": str(data), "org_capture_file": str(org / "inbox.org"),
            "org_ideas_file": str(org / "ideas.org"), "specs_dir": str(specs_dir)}


def _seed_idea(cfg, heading="instant review agent", notes=None, tags=()):
    W.add_idea(cfg, heading, tags=tags)
    path = cfg["org_ideas_file"]
    if notes:
        text = open(path).read()
        text = text.replace(f"* IDEA {heading}\n", f"* IDEA {heading}\n{notes}\n")
        open(path, "w").write(text)
    return _find(cfg, heading)


def _find(cfg, sub):
    return next(t for t in load_tasks(cfg) if sub in t.heading)


# ---- next_spec_number / slugify ---------------------------------------------

def test_next_spec_number_empty_dir(tmp_path):
    d = tmp_path / "specs"; d.mkdir()
    assert S.next_spec_number(d) == "0001"


def test_next_spec_number_max_plus_one(tmp_path):
    d = tmp_path / "specs"; d.mkdir()
    (d / "0001-foo.md").write_text("x")
    (d / "0013-bar.md").write_text("x")
    assert S.next_spec_number(d) == "0014"


def test_slugify():
    assert S.slugify("Idea -> Spec Promotion!") == "idea-spec-promotion"
    assert S.slugify("  weird   spacing  ") == "weird-spacing"


# ---- scaffold_from_idea ------------------------------------------------------

def test_scaffold_from_idea_creates_lint_valid_spec(tmp_path):
    cfg = _cfg(tmp_path)
    _seed_idea(cfg, "instant review agent",
              notes="  Some incubation notes about instant review.\n  More detail here.")
    path, spec_id = S.scaffold_from_idea(cfg, "instant review agent")

    assert path.exists()
    assert spec_id == "SPEC-0001"
    text = path.read_text()
    assert "id: SPEC-0001" in text
    assert "title: \"instant review agent\"" in text
    assert "source_idea:" in text
    assert "Some incubation notes about instant review." in text
    assert "status: draft" in text

    mod = _linter()
    fm, body = mod.split_frontmatter(text)
    specs = [{"path": path, "file": path.name, "num": "0001", "fm": fm, "raw_fm_ok": True,
             "sections": mod.sections(body), "id": fm.get("id", ""),
             "kind": fm.get("kind", "feature"), "status": fm.get("status", "")}]
    problems = [p for p in mod.validate(specs) if not p.startswith("LEDGER.md")]
    assert problems == [], problems


def test_scaffold_from_idea_links_back_and_advances_state(tmp_path):
    cfg = _cfg(tmp_path)
    _seed_idea(cfg, "instant review agent")
    path, spec_id = S.scaffold_from_idea(cfg, "instant review agent")

    idea = _find(cfg, "instant review agent")
    assert idea.properties.get("SPEC") == spec_id
    assert idea.state == "SPECCED"


def test_scaffold_from_idea_epic_uses_epic_template(tmp_path):
    cfg = _cfg(tmp_path)
    _seed_idea(cfg, "review platform")
    path, spec_id = S.scaffold_from_idea(cfg, "review platform", epic=True)
    text = path.read_text()
    assert "kind: epic" in text
    assert "Architecture / cross-cutting design" in text


def test_scaffold_from_idea_title_override(tmp_path):
    cfg = _cfg(tmp_path)
    _seed_idea(cfg, "review platform")
    path, _ = S.scaffold_from_idea(cfg, "review platform", title="Review platform v2")
    assert "review-platform-v2" in path.name
    assert "title: \"Review platform v2\"" in path.read_text()


def test_scaffold_from_idea_rejects_non_idea(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_task(cfg, "a regular task")
    try:
        S.scaffold_from_idea(cfg, "a regular task")
        assert False, "expected ValueError"
    except ValueError as e:
        assert "not an idea" in str(e)


def test_scaffold_from_idea_idempotence_guard(tmp_path):
    cfg = _cfg(tmp_path)
    _seed_idea(cfg, "instant review agent")
    S.scaffold_from_idea(cfg, "instant review agent")
    try:
        S.scaffold_from_idea(cfg, "instant review agent")
        assert False, "expected ValueError"
    except ValueError as e:
        assert "already promoted" in str(e)

    # --force allows re-promotion, numbering a new spec
    path2, spec_id2 = S.scaffold_from_idea(cfg, "instant review agent", force=True)
    assert spec_id2 == "SPEC-0002"
    idea = _find(cfg, "instant review agent")
    assert idea.properties.get("SPEC") == spec_id2


# ---- CLI ----------------------------------------------------------------------

def test_cli_spec_new(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _seed_idea(cfg, "instant review agent")
    import nightwright.cli as cli_mod
    monkeypatch.setattr(cli_mod, "load_config", lambda: cfg)
    r = CliRunner().invoke(cli, ["spec", "new", "--from-idea", "instant review agent"])
    assert r.exit_code == 0, r.output
    assert "SPEC-0001" in r.output
    idea = _find(cfg, "instant review agent")
    assert idea.properties.get("SPEC") == "SPEC-0001"
    assert idea.state == "SPECCED"


def test_cli_spec_new_rejects_non_idea(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    W.add_task(cfg, "a regular task")
    import nightwright.cli as cli_mod
    monkeypatch.setattr(cli_mod, "load_config", lambda: cfg)
    r = CliRunner().invoke(cli, ["spec", "new", "--from-idea", "a regular task"])
    assert r.exit_code != 0
    assert "not an idea" in r.output


def test_scaffold_from_long_idea_heading_caps_filename_and_title(tmp_path):
    """A paragraph-length idea heading must not overrun the OS filename limit (SPEC-0013 fix)."""
    cfg = _cfg(tmp_path)
    long_heading = ("I want to create an interactive web interface that is capable of using "
                    "the Example investigative tools in the backend to perform real-time queries "
                    "and show the archaeological, historical, and investigative digests and "
                    "analysis for test pass rates, flakiness, device health, node health")
    _seed_idea(cfg, heading=long_heading)
    spec_path, spec_id = S.scaffold_from_idea(cfg, "interactive web interface")
    name = pathlib.Path(spec_path).name
    assert len(name) <= 64                      # comfortably under the 255-byte limit
    text = pathlib.Path(spec_path).read_text()
    # frontmatter title is truncated, but the full heading survives in Context
    assert long_heading in text
    assert "title: \"I want to create an interactive web interface that is capable of\"" in text


def test_slugify_caps_length():
    assert len(S.slugify("word " * 100)) <= 50
    assert S.slugify("Adaptive Log Levels") == "adaptive-log-levels"


# ---- Clock Log section (SPEC-0062) -----------------------------------------------------

def test_scaffold_from_idea_includes_clock_log_section(tmp_path):
    cfg = _cfg(tmp_path)
    _seed_idea(cfg)
    path, _ = S.scaffold_from_idea(cfg, "instant review agent")
    text = pathlib.Path(path).read_text()
    assert "## Clock Log" in text
    assert "CLOCK-IN:" in text and "CLOCK-OUT:" in text


def test_scaffold_outbound_includes_clock_log_section(tmp_path):
    cfg = _cfg(tmp_path)
    path, _ = S.scaffold_outbound(cfg, "some-project", title="Outbound clock test")
    text = pathlib.Path(path).read_text()
    assert "## Clock Log" in text


def test_scaffold_epic_has_no_clock_log_section(tmp_path):
    cfg = _cfg(tmp_path)
    _seed_idea(cfg, heading="review platform epic")
    path, _ = S.scaffold_from_idea(cfg, "review platform epic", epic=True)
    text = pathlib.Path(path).read_text()
    assert "## Clock Log" not in text


def test_scaffolded_spec_with_empty_clock_log_still_lints_clean(tmp_path):
    """The Clock Log section is optional content — its presence (empty) must not trip any
    required-section check."""
    cfg = _cfg(tmp_path)
    _seed_idea(cfg, heading="lint check idea")
    path, spec_id = S.scaffold_from_idea(cfg, "lint check idea")
    text = pathlib.Path(path).read_text()
    text = text.replace(
        "## Acceptance criteria\n\n<What must be TRUE for this to be correct. Each item must "
        "be objectively checkable.>\n\n- [ ] …\n- [ ] …",
        "## Acceptance criteria\n\n- [ ] Works")
    text = text.replace(
        "## Test plan\n\n<HOW we prove the acceptance criteria. Required for feature/behavior "
        "specs; for a pure\npolicy/doc spec write \"N/A — <reason>\". Cover:>\n\n"
        "- **Automated tests:** which tests, where (`tests/…`), what they assert; fixtures needed.\n"
        "- **Manual verification:** exact commands/steps to run and the expected result.\n"
        "- **Regression guard:** how we confirm existing behavior is unchanged.",
        "## Test plan\n\n- **Automated tests:** pytest\n- **Manual verification:** look\n"
        "- **Regression guard:** green")
    text = text.replace("status: draft", "status: accepted", 1)
    # SPEC-0188: an accepted spec may not keep template placeholders
    text = re.sub(r'^# milestone: "M\?.*\n', "", text, flags=re.M)
    text = re.sub(r"^<The chosen approach.*$", "Do the thing.", text, flags=re.M)
    text = re.sub(r"^- …$", "- ok", text, flags=re.M)
    text = text.replace("<option>", "none")
    pathlib.Path(path).write_text(text)

    lint = _linter()
    lint.SPECS = pathlib.Path(cfg["specs_dir"])   # point the linter at this tmp specs dir
    problems = [p for p in lint.validate(lint.load_specs())
               if not p.startswith("LEDGER.md")]
    assert problems == [], problems
