"""Mode-aware parsing of the Open questions section (SPEC-0111).

Both reproductions from IDEA-143 are regression tests here: they must fail on the pre-fix
parser. The addressing case is the serious one — before this, prose became a phantom question
and a *correct* `--resolve 2` mutated the wrong item.
"""
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from click.testing import CliRunner

from nightwright import explore as EX
from nightwright.cli import cli
from nightwright.org import load_tasks

pytestmark = pytest.mark.fast

ROOT = Path(__file__).resolve().parent.parent


def _cfg(tmp_path):
    org = tmp_path / "org"
    org.mkdir()
    return {
        "org_files": [str(org)],
        "org_todo_keywords": ["TODO", "|", "DONE"],
        "org_idea_keywords": ["IDEA", "INCUBATE", "SPECCED", "|", "PROMOTED", "DROPPED"],
        "org_question_keywords": ["OPEN", "|", "RESOLVED"],
        "timezone": "America/New_York",
        "_tz": ZoneInfo("America/New_York"),
        "data_dir": str(tmp_path),
        "org_capture_file": str(org / "inbox.org"),
        "org_ideas_file": str(org / "ideas.org"),
        "specs_dir": str(tmp_path / "specs"),
    }


def _seed(tmp_path, monkeypatch, *, body=None, indent=""):
    """Two normalized questions, optionally with hand-written prose under the first."""
    cfg = _cfg(tmp_path)
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    run = CliRunner().invoke
    assert run(cli, ["idea", "body probe"]).exit_code == 0
    assert run(cli, ["idea", "questions", "IDEA-001", "--add", "first question?"]).exit_code == 0
    assert run(cli, ["idea", "questions", "IDEA-001", "--add", "second question?"]).exit_code == 0
    if body:
        p = Path(cfg["org_ideas_file"])
        lines = p.read_text().splitlines(keepends=True)
        out = []
        for ln in lines:
            out.append(ln)
            if ln.startswith("*** OPEN first question?"):
                out.append(f"{indent}{body}\n")
        p.write_text("".join(out))
    return cfg


def _idea(cfg):
    return [t for t in load_tasks(cfg) if t.is_idea][0]


def _questions(cfg):
    return [(i["state"], i["text"]) for i in EX.read_idea_questions(cfg, _idea(cfg))]


def _section(cfg):
    return EX.read_idea_enrichment(cfg, _idea(cfg))["questions"]


# --- the two reproductions from IDEA-143 -------------------------------------------------

@pytest.mark.parametrize("indent", ["", "  "])
def test_prose_under_a_question_is_not_a_question(tmp_path, monkeypatch, indent):
    """Unindented prose became a phantom question; indented prose was merged into the text."""
    cfg = _seed(tmp_path, monkeypatch, body="We decided X because Y.", indent=indent)
    assert _questions(cfg) == [("open", "first question?"), ("open", "second question?")]


@pytest.mark.parametrize("indent", ["", "  "])
def test_addressing_targets_the_nth_question_not_the_prose(tmp_path, monkeypatch, indent):
    """The serious half: a correct `--resolve 2` used to mutate the prose."""
    cfg = _seed(tmp_path, monkeypatch, body="We decided X because Y.", indent=indent)
    assert CliRunner().invoke(cli, ["idea", "questions", "IDEA-001",
                                    "--resolve", "2"]).exit_code == 0
    assert _questions(cfg) == [("open", "first question?"), ("resolved", "second question?")]
    assert "We decided X because Y." in _section(cfg)


# --- round trip through every mutator -----------------------------------------------------

@pytest.mark.parametrize("argv", [
    ["--resolve", "2"],
    ["--unresolve", "1"],
    ["--edit", "2", "--text", "reworded?"],
    ["--priority", "1", "--pri", "A"],
    ["--delete", "2"],
    ["--add", "third question?"],
])
def test_every_mutator_preserves_the_body(tmp_path, monkeypatch, argv):
    cfg = _seed(tmp_path, monkeypatch, body="We decided X because Y.")
    assert CliRunner().invoke(cli, ["idea", "questions", "IDEA-001"] + argv).exit_code == 0
    assert "We decided X because Y." in _section(cfg)
    assert "*** OPEN We decided" not in _section(cfg)


def test_body_survives_a_second_round_trip(tmp_path, monkeypatch):
    cfg = _seed(tmp_path, monkeypatch, body="We decided X because Y.")
    run = CliRunner().invoke
    for argv in (["--resolve", "1"], ["--unresolve", "1"], ["--edit", "1", "--text", "again?"]):
        assert run(cli, ["idea", "questions", "IDEA-001"] + argv).exit_code == 0
    assert _section(cfg).count("We decided X because Y.") == 1


def test_multi_line_body_is_kept(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    run = CliRunner().invoke
    assert run(cli, ["idea", "multiline"]).exit_code == 0
    assert run(cli, ["idea", "questions", "IDEA-001", "--add", "q?"]).exit_code == 0
    p = Path(cfg["org_ideas_file"])
    p.write_text(p.read_text().replace("*** OPEN q?\n",
                                       "*** OPEN q?\nline one\nline two\n"))
    assert run(cli, ["idea", "questions", "IDEA-001", "--add", "q2?"]).exit_code == 0
    section = _section(cfg)
    assert "line one" in section and "line two" in section
    assert len(_questions(cfg)) == 2


# --- legacy migration must keep working -----------------------------------------------------

def test_legacy_bare_lines_still_become_questions(tmp_path, monkeypatch):
    """SPEC-0054's migration is deliberate; SPEC-0111 must not break it."""
    cfg = _cfg(tmp_path)
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    run = CliRunner().invoke
    assert run(cli, ["idea", "legacy probe"]).exit_code == 0
    assert run(cli, ["idea", "questions", "IDEA-001", "--set", "bare one\nbare two"]).exit_code == 0
    assert _questions(cfg) == [("open", "bare one"), ("open", "bare two")]


def test_legacy_forms_still_classify(tmp_path, monkeypatch):
    body = "- [X] done one\n- [ ] open one\n(resolved) paren\n- bare bullet"
    items = EX._classify_question_lines(body)
    assert [i["state"] for i in items] == ["resolved", "open", "resolved", "open"]


def test_a_half_migrated_section_stays_legacy():
    """Detection is conservative: content *before* the first headline means still migrating,
    so half-converted questions keep migrating rather than being reclassified as prose."""
    half = "still a bare question\n*** OPEN a real one?"
    assert EX._section_is_normalized(half) is False
    assert len(EX._classify_question_lines(half)) == 2


def test_normalized_detection():
    assert EX._section_is_normalized("*** OPEN a?\n*** RESOLVED b?") is True
    assert EX._section_is_normalized("*** OPEN a?\nprose under it") is True
    assert EX._section_is_normalized("") is False
    assert EX._section_is_normalized("just prose") is False


# --- normalize-questions ------------------------------------------------------------------------

def test_normalize_does_not_promote_a_body(tmp_path, monkeypatch):
    """`normalize-questions` with no selector walks every idea in every file — this is the
    path that could have corrupted the whole corpus in one pass."""
    cfg = _seed(tmp_path, monkeypatch, body="We decided X because Y.")
    before = Path(cfg["org_ideas_file"]).read_text()
    assert CliRunner().invoke(cli, ["idea", "normalize-questions"]).exit_code == 0
    assert Path(cfg["org_ideas_file"]).read_text() == before
    assert len(_questions(cfg)) == 2


def test_normalize_is_still_idempotent_on_legacy(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    run = CliRunner().invoke
    assert run(cli, ["idea", "legacy"]).exit_code == 0
    assert run(cli, ["idea", "questions", "IDEA-001", "--set", "one\ntwo"]).exit_code == 0
    assert run(cli, ["idea", "normalize-questions"]).exit_code == 0
    once = Path(cfg["org_ideas_file"]).read_text()
    assert run(cli, ["idea", "normalize-questions"]).exit_code == 0
    assert Path(cfg["org_ideas_file"]).read_text() == once


# --- counts stay honest ----------------------------------------------------------------------------

def test_open_question_count_ignores_bodies(tmp_path, monkeypatch):
    """`q` column / `open_questions` JSON must not count prose (SPEC-0103 + SPEC-0110)."""
    import json

    cfg = _seed(tmp_path, monkeypatch, body="We decided X because Y.")
    payload = json.loads(CliRunner().invoke(
        cli, ["idea", "show", "IDEA-001", "--json"]).output)
    assert payload["open_questions"] == 2
    assert len(payload["question_items"]) == 2

