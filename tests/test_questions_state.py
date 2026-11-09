"""Open-questions state model: OPEN/RESOLVED headlines, resolve, normalize, no task leakage
(SPEC-0054)."""
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from click.testing import CliRunner

from nightwright import explore as EX
from nightwright import org_write as W
from nightwright.cli import cli
from nightwright.org import load_tasks, filter_tasks

pytestmark = pytest.mark.fast

ROOT = Path(__file__).resolve().parent.parent


def _cfg(tmp_path):
    org = tmp_path / "org"
    org.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    return {
        "org_files": [str(org)],
        "org_todo_keywords": ["TODO", "|", "DONE"],
        "org_idea_keywords": ["IDEA", "INCUBATE", "SPECCED", "|", "PROMOTED", "DROPPED"],
        "org_question_keywords": ["OPEN", "|", "RESOLVED"],
        "timezone": "America/New_York",
        "_tz": ZoneInfo("America/New_York"),
        "data_dir": str(data),
        "org_capture_file": str(org / "inbox.org"),
        "org_ideas_file": str(org / "ideas.org"),
        # SPEC-0079: state the specs dir so spec lookups can never reach the live repo.
        "specs_dir": str(tmp_path / "specs"),
    }


def _idea(cfg):
    return [t for t in load_tasks(cfg) if t.is_idea][0]


# --- classifier ------------------------------------------------------------------------

def test_classify_covers_all_legacy_forms():
    blob = (
        "- [X] cbx resolved\n"
        "- [ ] cbx open\n"
        "(resolved) paren resolved\n"
        "(open) paren open\n"
        "- bare open\n"
        "_Resolved:_\n"
        "- grouped one\n"
        "- grouped two\n"
    )
    items = EX._classify_question_lines(blob)
    assert items == [
        {"state": "resolved", "priority": None, "text": "cbx resolved"},
        {"state": "open", "priority": None, "text": "cbx open"},
        {"state": "resolved", "priority": None, "text": "paren resolved"},
        {"state": "open", "priority": None, "text": "paren open"},
        {"state": "open", "priority": None, "text": "bare open"},
        {"state": "resolved", "priority": None, "text": "grouped one"},
        {"state": "resolved", "priority": None, "text": "grouped two"},
    ]


def test_classify_leading_status_labels():
    """Freeform decision-log labels map to state (and priority) without doubling keywords."""
    blob = (
        "- RESOLVED — Q2: signal defined\n"
        "- RESOLVED (round 1): only nightly\n"
        "- OPEN (source-side): 30% gap\n"
        "- LOCKED: response = unattributed\n"
        "- OPTIONAL: validation harness\n"
        "- Remaining: apply to perf too\n"
        "_None blocking — deferred._\n"
    )
    assert EX._classify_question_lines(blob) == [
        {"state": "resolved", "priority": None, "text": "Q2: signal defined"},
        {"state": "resolved", "priority": None, "text": "(round 1): only nightly"},
        {"state": "open", "priority": None, "text": "(source-side): 30% gap"},
        {"state": "resolved", "priority": None, "text": "response = unattributed"},
        {"state": "open", "priority": "C", "text": "validation harness"},
        {"state": "open", "priority": None, "text": "apply to perf too"},
        {"state": "open", "priority": None, "text": "_None blocking — deferred._"},
    ]


def test_migration_preserves_text_verbatim(tmp_path):
    """Regression: migration must not run `to_org_body` — it would convert backticks and pair
    a headline's `***` with `**` inside the text (e.g. `=** Summary=` → `=* Summary=`)."""
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "verbatim")
    task = _idea(cfg)
    EX._replace_section_body(
        cfg, task, EX.SECTION_QUESTIONS,
        "- print org-style (=** Summary=) not `ATX`\n", apply_markup=False)
    EX.normalize_idea_questions(cfg)
    raw = open(cfg["org_ideas_file"]).read()
    assert "*** OPEN print org-style (=** Summary=) not `ATX`" in raw
    assert "=* Summary=" not in raw                       # stars intact
    assert "~ATX~" not in raw                             # backticks not rewritten


def test_classify_idempotent_on_headlines():
    body = EX._items_to_question_body([
        {"state": "open", "text": "q1"}, {"state": "resolved", "priority": "C", "text": "q2"}])
    assert EX._classify_question_lines(body) == [
        {"state": "open", "priority": None, "text": "q1"},
        {"state": "resolved", "priority": "C", "text": "q2"}]


# --- add / read / state ----------------------------------------------------------------

def test_add_question_writes_state_headline_and_header(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "stateful questions")
    EX.add_question(cfg, "IDEA-001", "first open one")
    EX.add_question(cfg, "IDEA-001", "already answered", state="resolved")
    raw = open(cfg["org_ideas_file"]).read()
    assert "#+TODO: OPEN | RESOLVED" in raw
    assert "*** OPEN first open one" in raw
    assert "*** RESOLVED already answered" in raw
    items = EX.read_idea_questions(cfg, _idea(cfg))
    assert items == [
        {"state": "open", "priority": None, "text": "first open one"},
        {"state": "resolved", "priority": None, "text": "already answered"},
    ]


def test_header_insert_is_idempotent(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "hdr")
    assert EX._ensure_question_header(cfg, cfg["org_ideas_file"]) is True
    assert EX._ensure_question_header(cfg, cfg["org_ideas_file"]) is False
    assert open(cfg["org_ideas_file"]).read().count("#+TODO: OPEN | RESOLVED") == 1


# --- resolve ---------------------------------------------------------------------------

def test_resolve_flips_nth_question_text_unchanged(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "resolve me")
    EX.set_questions(cfg, "IDEA-001", "keep asking\nanswer this one\nstill open")
    EX.resolve_question(cfg, "IDEA-001", 2)
    items = EX.read_idea_questions(cfg, _idea(cfg))
    assert items == [
        {"state": "open", "priority": None, "text": "keep asking"},
        {"state": "resolved", "priority": None, "text": "answer this one"},
        {"state": "open", "priority": None, "text": "still open"},
    ]


def test_resolve_out_of_range_errors_without_write(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "oob")
    EX.set_questions(cfg, "IDEA-001", "only one")
    before = open(cfg["org_ideas_file"]).read()
    with pytest.raises(ValueError):
        EX.resolve_question(cfg, "IDEA-001", 5)
    assert open(cfg["org_ideas_file"]).read() == before


# --- migration -------------------------------------------------------------------------

def test_normalize_questions_lossless_and_idempotent(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "legacy questions")
    # write a legacy body verbatim via the raw section replacer (bypass state model)
    task = _idea(cfg)
    EX._replace_section_body(
        cfg, task, EX.SECTION_QUESTIONS,
        "- [X] done check\n- [ ] open check\n(resolved) inline done\n- bare q\n")
    res = EX.normalize_idea_questions(cfg)
    assert res[0][1] == 1                                   # one idea rewritten
    raw = open(cfg["org_ideas_file"]).read()
    for text in ("done check", "open check", "inline done", "bare q"):
        assert text in raw                                  # nothing dropped
    assert "*** RESOLVED done check" in raw
    assert "*** OPEN open check" in raw
    assert "*** RESOLVED inline done" in raw
    assert "*** OPEN bare q" in raw
    assert "#+TODO: OPEN | RESOLVED" in raw
    # idempotent second run
    res2 = EX.normalize_idea_questions(cfg)
    assert res2[0][1] == 0


def test_normalize_questions_covers_every_idea_bearing_file(tmp_path):
    """SPEC-0074: no-selector mode must not silently miss ideas living outside the originally
    configured org_ideas_file (e.g. a rotated or archived file)."""
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "in active file")
    task1 = _idea(cfg)
    EX._replace_section_body(
        cfg, task1, EX.SECTION_QUESTIONS, "- bare q one\n")

    # simulate a second idea-bearing file (as rotation/archival would produce)
    second_path = Path(cfg["org_ideas_file"]).parent / "ideas-archive.org"
    second_path.write_text(
        "#+TODO: IDEA INCUBATE SPECCED | PROMOTED DROPPED\n\n"
        "* DROPPED in archive file\n  :PROPERTIES:\n  :ID: IDEA-002\n  :END:\n"
        "** Open questions\n- bare q two\n** Log\n"
    )

    res = EX.normalize_idea_questions(cfg)
    by_path = {str(Path(p)): n for p, n in res}
    assert len(res) == 2
    assert by_path[str(Path(cfg["org_ideas_file"]))] == 1
    assert by_path[str(second_path)] == 1
    assert "*** OPEN bare q two" in second_path.read_text()


def test_normalize_log_stamps_covers_every_idea_bearing_file(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "in active file")
    task1 = _idea(cfg)
    EX._replace_section_body(cfg, task1, "Log", "*** 2026-07-01\nan old-style stamp\n")

    second_path = Path(cfg["org_ideas_file"]).parent / "ideas-archive.org"
    second_path.write_text(
        "#+TODO: IDEA INCUBATE SPECCED | PROMOTED DROPPED\n\n"
        "* DROPPED in archive file\n  :PROPERTIES:\n  :ID: IDEA-002\n  :END:\n"
        "** Log\n*** 2026-07-02\nanother old-style stamp\n"
    )

    res = EX.normalize_idea_log_stamps(cfg)
    by_path = {str(Path(p)): n for p, n in res}
    assert len(res) == 2
    assert by_path[str(Path(cfg["org_ideas_file"]))] == 1
    assert by_path[str(second_path)] == 1
    assert "[2026-07-02" in second_path.read_text()


# --- leakage guard ---------------------------------------------------------------------

def test_question_headlines_do_not_leak_into_tasks(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    W.add_idea(cfg, "no leak")
    EX.add_question(cfg, "IDEA-001", "an open question")
    EX.add_question(cfg, "IDEA-001", "a resolved one", state="resolved")
    EX.append_log(cfg, "IDEA-001", "a log note")

    tasks = load_tasks(cfg)
    # the idea itself is still an idea; no enrichment node became a task
    assert filter_tasks(tasks, is_idea=True)
    assert all(t.state not in ("OPEN", "RESOLVED") for t in tasks)
    assert not any("question" in t.heading for t in tasks)

    for args in (["tasks"], ["tasks", "--all"]):
        out = CliRunner().invoke(cli, args).output
        assert "an open question" not in out and "a resolved one" not in out


# --- show / json -----------------------------------------------------------------------

def test_show_and_json_expose_question_state(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    W.add_idea(cfg, "render states")
    EX.add_question(cfg, "IDEA-001", "still curious")
    EX.add_question(cfg, "IDEA-001", "settled", state="resolved")

    show = CliRunner().invoke(cli, ["idea", "show", "IDEA-001", "--plain"])
    assert show.exit_code == 0, show.output
    assert "*** OPEN still curious" in show.output
    assert "*** RESOLVED settled" in show.output

    js = CliRunner().invoke(cli, ["idea", "show", "IDEA-001", "--json"])
    payload = json.loads(js.output)
    assert payload["question_items"] == [
        {"state": "open", "priority": None, "text": "still curious"},
        {"state": "resolved", "priority": None, "text": "settled"},
    ]


# ---- SPEC-0080: bulk resolve ------------------------------------------------------

def _three_questions(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "bulk resolve fixture")
    EX.set_questions(cfg, "IDEA-001", "alpha\nbeta\ngamma")
    return cfg


def _states(cfg, sel="IDEA-001"):
    return [q["state"] for q in EX.read_idea_questions(cfg, W.resolve_selector(cfg, sel))]


def test_resolve_multiple_indices(tmp_path):
    cfg = _three_questions(tmp_path)
    assert EX.resolve_questions(cfg, "IDEA-001", [1, 3]) == 2
    assert _states(cfg) == ["resolved", "open", "resolved"]


def test_resolve_all(tmp_path):
    cfg = _three_questions(tmp_path)
    assert EX.resolve_questions(cfg, "IDEA-001", None) == 3
    assert _states(cfg) == ["resolved"] * 3


def test_resolve_all_is_idempotent(tmp_path):
    cfg = _three_questions(tmp_path)
    EX.resolve_questions(cfg, "IDEA-001", None)
    body = open(cfg["org_ideas_file"]).read()
    assert EX.resolve_questions(cfg, "IDEA-001", None) == 0
    assert open(cfg["org_ideas_file"]).read() == body


def test_already_resolved_index_is_a_noop_not_an_error(tmp_path):
    cfg = _three_questions(tmp_path)
    EX.resolve_questions(cfg, "IDEA-001", [1])
    assert EX.resolve_questions(cfg, "IDEA-001", [1, 2]) == 1
    assert _states(cfg) == ["resolved", "resolved", "open"]


def test_bad_index_rejects_the_whole_call(tmp_path):
    """A half-applied bulk resolve would be worse than a refused one."""
    cfg = _three_questions(tmp_path)
    before = open(cfg["org_ideas_file"]).read()
    with pytest.raises(ValueError, match="#9"):
        EX.resolve_questions(cfg, "IDEA-001", [1, 9])
    assert open(cfg["org_ideas_file"]).read() == before
    assert _states(cfg) == ["open"] * 3


def test_bulk_resolve_is_one_write(tmp_path, monkeypatch):
    cfg = _three_questions(tmp_path)
    calls = []
    real = EX._atomic_backup_write
    monkeypatch.setattr(EX, "_atomic_backup_write",
                        lambda c, p, t: (calls.append(p), real(c, p, t))[1])
    EX.resolve_questions(cfg, "IDEA-001", None)
    assert len(calls) == 1, calls


def test_resolve_and_resolve_all_conflict(tmp_path, monkeypatch):
    from click.testing import CliRunner

    import nightwright.cli as cli_mod
    from nightwright.cli import cli

    cfg = _three_questions(tmp_path)
    monkeypatch.setattr(cli_mod, "load_config", lambda: cfg)
    r = CliRunner().invoke(cli, ["idea", "questions", "IDEA-001", "--resolve", "1",
                                 "--resolve-all"])
    assert r.exit_code != 0 and "not both" in r.output


def test_cli_resolve_multiple(tmp_path, monkeypatch):
    from click.testing import CliRunner

    import nightwright.cli as cli_mod
    from nightwright.cli import cli

    cfg = _three_questions(tmp_path)
    monkeypatch.setattr(cli_mod, "load_config", lambda: cfg)
    r = CliRunner().invoke(cli, ["idea", "questions", "IDEA-001", "--resolve", "1",
                                 "--resolve", "2"])
    assert r.exit_code == 0, r.output
    assert _states(cfg) == ["resolved", "resolved", "open"]

