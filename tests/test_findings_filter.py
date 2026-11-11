"""SPEC-0412: the Findings record filter (`parse_record_filter`, `matches`, `query_records`),
pure and Textual-free. The CLI-differential half lives in `test_findings_filter_cli.py`."""
from __future__ import annotations

import datetime as dt
import random
import re
import subprocess
import sys
from pathlib import Path

import pytest

from nightwright.agentic import schemas as SC
from nightwright.tui import findings_filter as F

pytestmark = pytest.mark.fast

ROOT = Path(__file__).resolve().parents[1]
SPEC = next((ROOT / "docs" / "specs").glob("0412-*.md"))
UTC = dt.timezone.utc
NOW = dt.datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
parse = F.parse_record_filter


def rec(**kw):
    base = {"schema": SC.HISTORY_RECORD_SCHEMA, "id": "ledger:X:1", "ts": "2026-11-05T10:00:00+00:00",
            "source": "ledger", "kind": "finding", "agent": "conformance_reviewer",
            "campaign": "SPEC-0179-20261025-158c", "spec_id": "SPEC-0179", "project": "Meta-Tools",
            "severity": "major", "title": "Missing guard", "text": "the Guard is absent",
            "status": "open", "links": {}, "linked_by": "field"}
    base.update(kw)
    return base


# --- parse (AC2, AC3) -----------------------------------------------------------------------

@pytest.mark.parametrize("text, field, value", [
    ("agent:steward", "agent", "steward"),
    ("agent:steward:Meta-Tools", "agent", "steward:Meta-Tools"),
    ("kind:Finding", "kind", "finding"),
    ("project:Meta-Tools", "project", "Meta-Tools"),
    ("severity:MAJOR", "severity", "major"),
    ("severity:none", "severity", "none"),
    ("status:none", "status", "none"),
    ("campaign:0179", "campaign", "0179"),
    ("campaign:none", "campaign", "none"),
    ("kind:finding kind:decision", "kind", "decision"),            # last wins
    ("kind:finding kind:", "kind", "finding"),                     # empty value is ignored
])
def test_parse_keys(text, field, value):
    flt = parse(text)
    assert getattr(flt, field) == value
    assert not flt.impossible and not flt.terms


def test_parse_terms_quotes_and_unknown_keys():
    assert parse("foo bar").terms == ("foo", "bar")
    assert parse('"Two Words" x').terms == ("two words", "x")
    assert parse("foo:bar").terms == ("foo:bar",)                  # unknown key is text
    assert parse('"kind:finding"').terms == ("kind:finding",) and parse('"kind:finding"').kind is None
    assert parse('"unbalanced words').terms == ("unbalanced words",)
    assert parse(":").terms == (":",)
    flt = parse("kind:")
    assert flt.kind is None and flt.problems == ("empty value for kind:",) and not flt.impossible
    assert parse("").is_empty and parse("   ").is_empty and not parse("x").is_empty


def test_parse_dates_relative_and_absolute():
    flt = parse("since:7d until:12h", now=NOW)
    assert flt.since == NOW - dt.timedelta(days=7)
    assert flt.until == NOW - dt.timedelta(hours=12)
    assert flt.bounds(NOW + dt.timedelta(days=1)) == (
        NOW - dt.timedelta(days=6), NOW + dt.timedelta(hours=12))
    flt = parse("since:2026-01-01 until:2026-06-30")
    since, until = flt.bounds()
    local = lambda d: dt.datetime.combine(d, dt.time.min).astimezone(UTC)  # noqa: E731
    assert since == local(dt.date(2026, 1, 1)) and until == local(dt.date(2026, 7, 1))
    assert flt.since == since and flt.until == until


@pytest.mark.parametrize("text, key, problem", [
    ("kind:nonsense", "kind", "unknown kind 'nonsense'"),
    ("kind:none", "kind", "unknown kind 'none'"),
    ("severity:huge", "severity", "unknown severity 'huge'"),
    ("status:maybe", "status", "unknown status 'maybe'"),
    ("since:yesterday", "since", "bad date 'yesterday' for since"),
    ("until:2026-02-31", "until", "bad date '2026-02-31' for until"),
    ("since:99999999999d", "since", "bad date '99999999999d' for since"),
])
def test_parse_invalid_values_are_impossible(text, key, problem):
    flt = parse(text, now=NOW)
    assert flt.problems == (problem,)
    assert flt.impossible == {key}
    assert not flt.is_empty
    assert not F.matches(rec(), flt, NOW)


def test_later_valid_value_clears_impossible():
    assert not parse("kind:nope kind:finding").impossible


def test_describe_quotes_colon_terms_so_they_reparse_as_free_text():
    flt = parse('"kind:x" foo:bar', now=NOW)
    assert flt.kind is None and flt.terms == ("kind:x", "foo:bar")
    assert parse(flt.describe(), now=NOW) == flt


def test_history_cache_private_reader_still_exists():
    # `_total_records` reads this private helper and silently falls back if it moves; fail loudly
    from nightwright.agentic import history_cache as HC
    assert callable(HC._get_records_mem) and callable(HC._v1_root)


def test_describe_is_canonical():
    flt = parse('severity:Major agent:steward "two words" kind:finding since:7d x', now=NOW)
    assert flt.describe() == 'agent:steward kind:finding severity:major since:7d "two words" x'
    assert parse(flt.describe(), now=NOW) == flt


def test_closed_sets_follow_the_history_layer():
    assert F.KINDS == SC.RECORD_KINDS
    assert F.STATUSES == SC.RECORD_STATUSES


# --- never raises (AC1) ---------------------------------------------------------------------

def test_parse_never_raises_fuzz():
    rng = random.Random(412)
    alphabet = list('abcXYZ019 :"\'\t\n-_.\x00ßé☃😀/\\') + ["kind:", "since:", "agent:", "7d", "::"]
    nasty = ["", ":", '"', "\x00", "kind:\x00", "since:" + "9" * 400, "x" * 10_000, "a:" * 5000,
             '"' * 101, "\ud800", "since:١٢d", None, 5]
    inputs = list(nasty) + [
        "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 60))) for _ in range(2500)]
    for text in inputs:
        flt = parse(text, now=NOW)
        assert isinstance(flt, F.RecordFilter)
        assert "text" not in flt.impossible, repr(text)   # the catch-all fallback hides a crash


def test_severity_closed_set_is_pinned():
    # `must_fix` is what the ledger writes for the top severity; keep it with the spec's set
    assert F.SEVERITIES == {"blocker", "must_fix", "major", "minor", "nit", "info"}


# --- matches (AC2, AC7) ---------------------------------------------------------------------

def m(text, record, now=NOW):
    return F.matches(record, parse(text, now=now), now)


def test_matches_each_row():
    r = rec()
    assert m("", r)
    assert m("agent:conformance_reviewer", r) and m("agent:conformance", rec(agent="conformance:x"))
    assert not m("agent:conformance", r)                            # prefix needs the colon
    assert m("agent:steward", rec(agent="steward:Meta-Tools"))
    assert not m("agent:steward", rec(agent="stewardship")) and not m("agent:steward", rec(agent=None))
    assert m("kind:finding", r) and not m("kind:decision", r)
    assert m("project:META-tools", r) and not m("project:other", r)
    assert not m("project:meta-tools", rec(project=None))
    assert m("severity:major", r) and not m("severity:minor", r)
    assert m("severity:none", rec(severity=None)) and not m("severity:none", r)
    assert m("status:open", r) and m("status:none", rec(status=None)) and not m("status:none", r)
    assert m("campaign:0179", r) and m("campaign:spec-0179-2026", r) and not m("campaign:0180", r)
    assert m("campaign:none", rec(campaign=None)) and not m("campaign:none", r)
    assert not m("campaign:0179", rec(campaign=None))
    assert m("guard", r) and m("GUARD absent", r) and not m("guard missingword", r)
    assert m('"missing guard"', r) and not m('"guard missing"', r)
    assert m("kind:finding severity:major status:open guard", r)
    assert not m("kind:finding severity:minor guard", r)


def test_matches_dates_are_start_of_day_and_until_is_inclusive_of_the_day():
    day_start = dt.datetime.combine(dt.date(2026, 11, 5), dt.time.min).astimezone(UTC)
    at = lambda d: rec(ts=d.isoformat())  # noqa: E731
    assert m("since:2026-11-05", at(day_start))
    assert not m("since:2026-11-05", at(day_start - dt.timedelta(seconds=1)))
    next_day = day_start + dt.timedelta(days=1)
    assert m("until:2026-11-05", at(next_day - dt.timedelta(seconds=1)))
    assert not m("until:2026-11-05", at(next_day))
    assert not m("since:2026-11-05", rec(ts=None)) and not m("until:2026-11-05", rec(ts="junk"))
    assert m("since:7d", rec(ts=(NOW - dt.timedelta(days=6)).isoformat()))
    assert not m("since:7d", rec(ts=(NOW - dt.timedelta(days=8)).isoformat()))
    assert m("since:12h", rec(ts=(NOW - dt.timedelta(hours=11)).isoformat()))
    assert not m("since:12h", rec(ts=(NOW - dt.timedelta(hours=13)).isoformat()))


def test_matches_keeps_unknown_kind_and_source():
    odd = rec(kind="brand_new", source="telemetry", severity=None, status=None)
    assert m("", odd) and m("brand_new", rec(title="x brand_new")) is True
    assert not m("kind:finding", odd)
    assert F.matches({}, parse(""), NOW)                            # even a record with no fields


# --- GRAMMAR_ROWS vs the spec (AC6) ---------------------------------------------------------

def spec_grammar_rows():
    lines = SPEC.read_text().split("### Filter grammar", 1)[1].split("\nRules:", 1)[0].splitlines()
    rows = []
    for line in lines:
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if line.startswith("|") and len(cells) == 3 and cells[0] not in ("Token", "---"):
            rows.append((cells[0].replace("`", ""), tuple(re.findall(r"\b(history|tab)\b", cells[2]))))
    return rows


def test_grammar_rows_match_the_spec_table():
    assert spec_grammar_rows() == list(F.GRAMMAR_ROWS)


# --- query_records with a fake history (AC3, AC6, AC7, AC8) ---------------------------------

class FakeHistory:
    def __init__(self, rows=(), exc=None, garbage=None):
        self.rows, self.exc, self.garbage, self.calls = list(rows), exc, garbage, []

    def __call__(self, cfg, **kwargs):
        self.calls.append(kwargs)
        if self.exc:
            raise self.exc
        return self.garbage if self.garbage is not None else list(self.rows)


@pytest.fixture
def fake(monkeypatch):
    def install(**kw):
        fh = FakeHistory(**kw)
        monkeypatch.setattr(F.H, "history_records", fh)
        return fh
    return install


# filter text -> the kwargs `history_records` must receive (AC6: `history` rows reach it, `tab` rows never)
PASSED_DOWN = [
    ("", {}),
    ("kind:finding", {"kind": "finding"}),
    ("severity:major", {"severity": "major"}),
    ("status:open", {"status": "open"}),
    ("agent:steward:Meta-Tools", {"agent": "steward:Meta-Tools"}),
    ("alpha beta", {"query": "alpha"}),
    ("since:7d", {"since": NOW - dt.timedelta(days=7)}),
    ("since:7d until:12h",
     {"since": NOW - dt.timedelta(days=7), "until": NOW - dt.timedelta(hours=12)}),
    # date-only bounds stay on `matches` (CLI and grammar disagree on the day near midnight)
    ("since:2026-05-02 until:2026-05-04", {}),
    ("kind:finding severity:major status:open agent:conductor:x alpha",
     {"kind": "finding", "severity": "major", "status": "open", "agent": "conductor:x",
      "query": "alpha"}),
    # `tab` rows: nothing reaches history
    ("agent:steward", {}),
    ("project:Meta-Tools", {}),
    ("campaign:0179", {}),
    ("campaign:none", {}),
    ("severity:none", {}),
    ("status:none", {}),
    ("project:x campaign:0179 agent:steward severity:none status:none", {}),
]


@pytest.mark.parametrize("text,want", PASSED_DOWN)
def test_history_rows_reach_history_and_tab_rows_never_do(fake, text, want):
    fh = fake(rows=[rec()])
    F.query_records({}, parse(text, now=NOW), NOW)
    assert fh.calls[0] == want
    tab_only = {"project", "campaign"}
    assert not tab_only & set(fh.calls[0])


def test_every_tab_row_is_classified_tab_and_never_passed_down():
    assert {t for t, w in F.GRAMMAR_ROWS if "history" not in w} == {"project:X", "campaign:X"}
    mixed = {t for t, w in F.GRAMMAR_ROWS if "history" in w and "tab" in w}
    assert mixed == {"agent:X", "severity:X", "status:X", "anything else"}


def test_total_records_falls_back_to_one_unfiltered_recount_without_a_cache(fake):
    fh = fake(rows=[rec(id="a:1"), rec(id="a:2", kind="note")])
    res = F.query_records({}, parse("kind:finding"), NOW)
    assert (res.total_matching, res.total_records) == (1, 2)
    assert fh.calls == [{"kind": "finding"}, {}]
    fh.calls.clear()
    F.query_records({}, parse("project:x"), NOW)              # nothing narrowed: one call only
    assert fh.calls == [{}]


def test_impossible_filter_never_calls_history(fake):
    fh = fake(rows=[rec()])
    for text in ("kind:nope", "severity:x", "status:x", "since:bad", "until:bad", "kind:nope alpha"):
        res = F.query_records({}, parse(text, now=NOW), NOW)
        assert res.records == [] and res.error is None and res.problems
        assert res.total_matching == 0
    assert fh.calls == []


def test_query_orders_ts_desc_then_id_desc_and_counts(fake):
    rows = [rec(id="a:1", ts="2026-11-01T00:00:00+00:00"), rec(id="a:3", ts="2026-11-02T00:00:00+00:00"),
            rec(id="a:2", ts="2026-11-02T00:00:00+00:00"), rec(id="z:9", ts=None, campaign=None),
            rec(id="a:4", ts="2026-11-02T00:00:00+00:00", kind="brand_new", source="telemetry",
                campaign=None, severity=None, status=None)]
    fake(rows=rows)
    res = F.query_records({}, parse(""), NOW)
    assert [r["id"] for r in res.records] == ["a:4", "a:3", "a:2", "a:1", "z:9"]
    assert res.total_matching == res.total_records == 5 and res.error is None
    fh = fake(rows=rows)
    res = F.query_records({}, parse("campaign:none"), NOW)
    assert [r["id"] for r in res.records] == ["a:4", "z:9"]        # campaign-less + unknown kind
    assert (res.total_matching, res.total_records) == (2, 5)
    res = F.query_records({}, parse("kind:finding"), NOW)
    assert res.total_records == 5 and fh.calls[1:] == [{"kind": "finding"}, {}]   # narrowed + recount


def test_history_is_rechecked_by_matches(fake):
    fake(rows=[rec(id="a:1", severity="minor"), rec(id="a:2", severity="major")])
    res = F.query_records({}, parse("severity:major"), NOW)         # a looser history layer
    assert [r["id"] for r in res.records] == ["a:2"]


def test_history_failures_are_returned_not_raised(fake):
    for kw in ({"exc": OSError("unreadable log")}, {"exc": ValueError("corrupt cache")},
               {"exc": RuntimeError("boom")}, {"garbage": "not a list"},
               {"garbage": [1, 2, 3]}, {"garbage": [None]}):
        fake(**kw)
        res = F.query_records({}, parse("kind:finding"), NOW)
        assert res.records == [] and res.error and res.total_matching == 0


def test_corrupt_real_cache_is_an_error_or_a_rebuild_never_a_raise(tmp_path):
    import history_scale as HS
    data, cache = HS.copy_fixture(tmp_path / "data"), tmp_path / "cache"
    cfg = {"data_dir": str(data), "cache_dir": str(cache),
           "agentic": {"history": {"cache": True}}}
    good = F.query_records(cfg, parse("kind:finding"))
    assert good.error is None and good.records
    for path in (cache / "history" / "v1").rglob("*"):
        if path.is_file():
            path.write_bytes(b"\x00{not json\n")
    res = F.query_records(cfg, parse("kind:finding"))
    assert res.error is not None or [r["id"] for r in res.records] == [r["id"] for r in good.records]
    assert res.error is None or res.records == []


# --- Textual-free and read-only (AC10) ------------------------------------------------------

def test_module_source_does_not_import_textual():
    src = (ROOT / "src" / "nightwright" / "tui" / "findings_filter.py").read_text()
    assert not re.search(r"^\s*(?:from|import)\s+(?:textual|rich)\b", src, re.M)
    assert "textual" not in re.findall(r"^\s*(?:from|import)\s+([\w.]+)", src, re.M)


def test_imports_with_textual_blocked():
    code = (
        "import sys, importlib.abc\n"
        "class Block(importlib.abc.MetaPathFinder):\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name.split('.')[0] == 'textual':\n"
        "            raise ImportError('textual blocked')\n"
        "sys.meta_path.insert(0, Block())\n"
        "import nightwright.tui.findings_filter as F\n"
        "assert F.parse_record_filter('kind:finding').kind == 'finding'\n"
        "assert not [m for m in sys.modules if m.split('.')[0] == 'textual']\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr


def test_total_records_comes_from_the_loaded_cache_not_a_second_history_call(tmp_path, monkeypatch):
    """With a cache, a narrowed query makes one history call and still reports the exact total."""
    import history_scale as HS
    data, cache = HS.copy_fixture(tmp_path / "data"), tmp_path / "cache"
    cfg = {"data_dir": str(data), "cache_dir": str(cache),
           "agentic": {"history": {"cache": True}}}
    everything = F.query_records(cfg, parse(""))
    calls = []
    real = F.H.history_records
    monkeypatch.setattr(F.H, "history_records", lambda c, **kw: (calls.append(kw), real(c, **kw))[1])
    res = F.query_records(cfg, parse("kind:finding"))
    assert res.error is None and res.total_records == everything.total_records
    assert calls == [{"kind": "finding"}]
