"""Idea explore enrichment (SPEC-0024) + log rename/datetime (SPEC-0031/0032)."""
from pathlib import Path
import datetime as dt
import re
from zoneinfo import ZoneInfo

import pytest
from click.testing import CliRunner

from nightwright import explore as EX
from nightwright import org_write as W
from nightwright import specs as S
from nightwright.cli import cli
from nightwright.org import load_tasks
from nightwright.workflow import next_step_for_idea

pytestmark = pytest.mark.fast

ROOT = Path(__file__).resolve().parent.parent


def _cfg(tmp_path):
    org = tmp_path / "org"
    org.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    specs = tmp_path / "specs"
    specs.mkdir()
    for name in ("TEMPLATE.md", "TEMPLATE-epic.md"):
        (specs / name).write_text((ROOT / "docs" / "specs" / name).read_text())
    return {
        "org_files": [str(org)],
        "org_todo_keywords": ["TODO", "|", "DONE"],
        "org_idea_keywords": ["IDEA", "INCUBATE", "SPECCED", "|", "PROMOTED", "DROPPED"],
        "timezone": "America/New_York",
        "_tz": ZoneInfo("America/New_York"),
        "data_dir": str(data),
        "org_capture_file": str(org / "inbox.org"),
        "org_ideas_file": str(org / "ideas.org"),
        "specs_dir": str(specs),
        "config_dir": str(tmp_path / "config"),
        "outbox_dir": str(tmp_path / "outbox"),
    }


def test_explore_summary_log_show_and_next(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    r = CliRunner().invoke(cli, ["idea", "needs", "exploration"])
    assert r.exit_code == 0
    idea = load_tasks(cfg)[0]
    assert next_step_for_idea(cfg, idea) == "nw idea log IDEA-001"

    assert CliRunner().invoke(cli, ["idea", "log", "IDEA-001",
                                    "--note", "looked at code"]).exit_code == 0
    assert CliRunner().invoke(cli, ["idea", "summary", "IDEA-001",
                                    "--set", "We need an explore stage"]).exit_code == 0
    idea = [t for t in load_tasks(cfg) if t.properties.get("ID") == "IDEA-001"][0]
    assert idea.state == "INCUBATE"
    assert "We need an explore stage" in EX.idea_summary_text(cfg, idea)
    assert next_step_for_idea(cfg, idea) == "nw spec new --from-idea IDEA-001"

    show = CliRunner().invoke(cli, ["idea", "show", "IDEA-001"])
    assert show.exit_code == 0
    assert "We need an explore stage" in show.output
    assert "looked at code" in show.output
    assert "** Summary" in show.output
    assert "## Summary" not in show.output


def test_summary_normalizes_markdown_to_org(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "md body")
    EX.set_summary(cfg, "IDEA-001", "Try `nw idea` — this is **important**")
    t = load_tasks(cfg)[0]
    raw = open(t.file).read()
    assert "~nw idea~" in raw
    assert "*important*" in raw
    assert "`nw idea`" not in raw
    assert "**important**" not in raw
    body = EX.idea_summary_text(cfg, t)
    assert "~nw idea~" in body


def test_promote_context_converts_org_to_markdown(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "enriched idea")
    EX.set_summary(cfg, "IDEA-001", "Durable finding about `X`")
    EX.append_log(cfg, "IDEA-001", "checked module Y")
    path, _ = S.scaffold_from_idea(cfg, "IDEA-001", force=True)
    text = path.read_text()
    assert "Durable finding about `X`" in text
    assert "~X~" not in text.split("## Context")[1].split("## Goals")[0]
    assert "checked module Y" in text


def test_summary_preserves_code_and_fences_on_disk(tmp_path, monkeypatch):
    """SPEC-0164: identifiers in verbatim/code survive the write path byte-for-byte, and a
    fenced block lands as a comma-escaped org src block that does not break the idea tree."""
    cfg = _cfg(tmp_path)
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    W.add_idea(cfg, "code idea")
    body = ("Resolves =Path(__file__)= here.\n"
            "```python\ndef __init__(self):\n* not a headline\n```\n")
    r = CliRunner().invoke(cli, ["idea", "summary", "IDEA-001", "--set", body])
    assert r.exit_code == 0, r.output
    r = CliRunner().invoke(cli, ["idea", "log", "IDEA-001",
                                 "--note", "see `snake__case__name`"])
    assert r.exit_code == 0, r.output
    t = load_tasks(cfg)[0]
    raw = open(t.file).read()
    assert "=Path(__file__)=" in raw
    assert "#+begin_src python\ndef __init__(self):\n,* not a headline\n#+end_src" in raw
    assert "~snake__case__name~" in raw
    # the escaped line is not a new headline: still exactly one idea, Summary intact
    assert [x.properties.get("ID") for x in load_tasks(cfg)] == ["IDEA-001"]
    assert "def __init__(self):" in EX.idea_summary_text(cfg, t)
    # a second write still finds the sections (tree not broken)
    assert CliRunner().invoke(cli, ["idea", "log", "IDEA-001", "--note", "again"]).exit_code == 0


def test_promote_context_fences_and_no_spurious_spans(tmp_path):
    """SPEC-0164: org → md prefill turns src blocks back into fences and leaves `a = b = c`
    and `2*3*4` alone."""
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "fence idea")
    EX.set_summary(cfg, "IDEA-001", "Check a = b = c and 2*3*4.\n```sh\necho hi\n```\n")
    path, _ = S.scaffold_from_idea(cfg, "IDEA-001", force=True)
    ctx = path.read_text().split("## Context")[1].split("## Goals")[0]
    assert "Check a = b = c and 2*3*4." in ctx
    assert "```sh\necho hi\n```" in ctx


def test_log_stamp_inactive_org_with_time(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "stamp me")
    EX.append_log(cfg, "IDEA-001", "timed note")
    t = load_tasks(cfg)[0]
    raw = open(t.file).read()
    assert re.search(r"\*\*\* \[\d{4}-\d{2}-\d{2} \w{3} \d{2}:\d{2}\]\n", raw)
    assert "timed note" in raw
    # must not write active agenda stamps
    assert not re.search(r"\*\*\* <", raw)


def test_normalize_plain_log_headlines():
    src = (
        "** Log\n"
        "*** 2026-07-22\n"
        "old date-only\n"
        "*** 2026-07-23 07:50\n"
        "with time\n"
        "*** [2026-07-23 Thu 07:54]\n"
        "already good\n"
    )
    out, n = EX.normalize_plain_log_headlines(src)
    assert n == 2
    assert "*** [2026-07-22 Wed]\n" in out
    assert "*** [2026-07-23 Thu 07:50]\n" in out
    assert "*** [2026-07-23 Thu 07:54]\n" in out
    # idempotent
    out2, n2 = EX.normalize_plain_log_headlines(out)
    assert n2 == 0 and out2 == out


def test_inactive_stamp_helper(tmp_path):
    cfg = _cfg(tmp_path)
    when = dt.datetime(2026, 7, 23, 7, 42, tzinfo=cfg["_tz"])
    assert W._inactive_stamp(cfg, when=when, with_time=True) == "[2026-07-23 Thu 07:42]"
    assert W._inactive_stamp(cfg, when=when, with_time=False) == "[2026-07-23 Thu]"
    assert W._closed_stamp(cfg)  # still inactive brackets, no time required in shape
    assert W._closed_stamp(cfg).startswith("[") and W._closed_stamp(cfg).endswith("]")


def test_explore_alias_still_logs(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    W.add_idea(cfg, "alias")
    r = CliRunner().invoke(cli, ["idea", "explore", "IDEA-001", "--note", "via alias"])
    assert r.exit_code == 0, r.output
    assert "via alias" in open(load_tasks(cfg)[0].file).read()


def test_explore_subcommand_does_not_capture_idea_named_explore(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    W.add_idea(cfg, "seed")
    from nightwright.org import filter_tasks
    before = len(filter_tasks(load_tasks(cfg), is_idea=True))
    r = CliRunner().invoke(cli, ["idea", "explore", "IDEA-001", "--note", "x"])
    assert r.exit_code == 0
    ideas = filter_tasks(load_tasks(cfg), is_idea=True)
    assert len(ideas) == before
    assert not any(t.heading.startswith("explore") for t in ideas)


def test_retitle_and_questions(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "old title")
    EX.set_questions(cfg, "IDEA-001", "- q1\n")
    EX.add_question(cfg, "IDEA-001", "q2")
    EX.retitle_idea(cfg, "IDEA-001", "new title")
    t = load_tasks(cfg)[0]
    assert t.heading == "new title"
    q = EX.read_idea_enrichment(cfg, t)["questions"]
    assert "q1" in q and "q2" in q


def test_promote_warns_without_summary(tmp_path, capsys):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "blank enrich")
    S.scaffold_from_idea(cfg, "IDEA-001")
    err = capsys.readouterr().err
    assert "empty Summary" in err


def test_promote_context_includes_summary(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "enriched idea")
    EX.set_summary(cfg, "IDEA-001", "Durable finding about X")
    EX.append_log(cfg, "IDEA-001", "checked module Y")
    path, _ = S.scaffold_from_idea(cfg, "IDEA-001", force=True)
    text = path.read_text()
    assert "Durable finding about X" in text
    assert "checked module Y" in text


def test_promote_context_drops_drawers(tmp_path):
    """SPEC-0195: the idea's PROPERTIES / LOGBOOK drawers never reach the spec's Context."""
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "clocked idea")
    EX.set_summary(cfg, "IDEA-001", "Durable finding about X")
    W.clock_in(cfg, "IDEA-001")
    W.clock_out(cfg, "IDEA-001")
    assert ":LOGBOOK:" in open(load_tasks(cfg)[0].file).read()
    path, _ = S.scaffold_from_idea(cfg, "IDEA-001", force=True)
    ctx = path.read_text().split("## Context")[1].split("## Goals")[0]
    for junk in (":PROPERTIES:", ":LOGBOOK:", ":END:", ":ID:", "CLOCK:"):
        assert junk not in ctx
    assert "Durable finding about X" in ctx
    assert "** Summary" in ctx


def test_idea_help_lists_subcommands():
    r = CliRunner().invoke(cli, ["idea", "--help"])
    assert r.exit_code == 0
    assert "log" in r.output
    assert "show" in r.output
    assert "capture" in r.output or "Bare TEXT" in r.output

