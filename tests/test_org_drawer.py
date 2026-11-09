"""SPEC-0196: property writes reuse an existing drawer wherever it sits before the next headline."""
from zoneinfo import ZoneInfo

from nightwright import org_write as W
from nightwright.org import load_tasks
import pytest

pytestmark = pytest.mark.fast

AFTER_BODY = """\
* TODO Misplaced drawer
Some body text first.
:PROPERTIES:
:ID: abc-1
:CREATED: [2026-01-01 Thu]
:END:
More body.
* TODO Next entry
"""


def _cfg(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    return {"org_files": [str(tmp_path)], "org_todo_keywords": ["TODO", "|", "DONE"],
            "timezone": "UTC", "_tz": ZoneInfo("UTC"), "data_dir": str(data_dir)}


def _lines(text):
    return text.splitlines(keepends=True)


def test_find_drawer_after_body_and_absent():
    lines = _lines(AFTER_BODY)
    assert W._find_drawer(lines, 0) == (2, 5)
    assert W._find_drawer(lines, 7) is None  # next entry has none; never crosses a headline


def test_inject_into_drawer_after_body_adds_key_in_place():
    lines = _lines(AFTER_BODY)
    W._inject_property(lines, 0, "*", "FOO", "bar")
    assert sum(l.strip() == ":PROPERTIES:" for l in lines) == 1
    assert lines[5] == "  :FOO: bar\n" and lines[6] == ":END:\n"
    W._inject_property(lines, 0, "*", "FOO", "baz")  # replaces, does not duplicate
    assert [l for l in lines if ":FOO:" in l] == ["  :FOO: baz\n"]
    assert ":ID: abc-1\n" in lines


def test_remove_from_drawer_after_body():
    lines = _lines(AFTER_BODY)
    assert W._remove_property(lines, 0, "CREATED") is True
    assert not any(":CREATED:" in l for l in lines)
    assert W._remove_property(lines, 0, "CREATED") is False


def test_no_drawer_creates_one_after_headline_and_planning():
    lines = _lines("* TODO Plain\nSCHEDULED: <2026-01-02 Fri>\nbody\n")
    W._inject_property(lines, 0, "*", "FOO", "bar")
    assert lines == ["* TODO Plain\n", "SCHEDULED: <2026-01-02 Fri>\n", "  :PROPERTIES:\n",
                     "  :FOO: bar\n", "  :END:\n", "body\n"]
    lines = _lines("* TODO Plain\nbody\n")
    W._inject_property(lines, 0, "*", "FOO", "bar")
    assert lines[1] == "  :PROPERTIES:\n"


def test_set_and_clear_property_on_misplaced_drawer(tmp_path):
    path = tmp_path / "a.org"
    path.write_text(AFTER_BODY)
    cfg = _cfg(tmp_path)
    task = next(t for t in load_tasks(cfg) if "Misplaced" in t.heading)
    W.set_property(cfg, task, "FOO", "bar", touch=False)
    text = path.read_text()
    assert text.count(":PROPERTIES:") == 1
    assert ":FOO: bar" in text and ":ID: abc-1" in text
    task = next(t for t in load_tasks(cfg) if "Misplaced" in t.heading)
    W.clear_property(cfg, task, "FOO")
    text = path.read_text()
    assert ":FOO:" not in text and text.count(":PROPERTIES:") == 1


