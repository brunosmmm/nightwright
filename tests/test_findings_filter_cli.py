"""SPEC-0412: `query_records` against the oracle `nw campaign records --json` on the child 2
fixture. CLI-expressible filters must equal the CLI's id list (order included); extended filters
must equal `matches` applied to the CLI's unfiltered output. The data dir stays byte-identical and
the only writes are the history layer's cache under `cache_dir`."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
from pathlib import Path

import pytest
from click.testing import CliRunner

import history_scale as HS
from nightwright.agentic import history as H
from nightwright.cli import cli
from nightwright.tui import findings_filter as F

pytestmark = pytest.mark.fast

REAL = os.environ.get("NW_FINDINGS_REAL_DATA") == "1"
UTC = dt.timezone.utc


def tree_hash(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    if REAL:
        from nightwright.config import load_config
        cfg = load_config()
        yield cfg, None, None
        return
    base = tmp_path_factory.mktemp("findings-cli")
    data = HS.copy_fixture(base / "data")
    cache = base / "cache"
    cache.mkdir()
    cfg = {"data_dir": str(data), "cache_dir": str(cache), "agentic": {
        "default_profile": "balanced", "history": {"cache": True}}}
    before = tree_hash(data)                    # fixture writes are done before the first hash
    yield cfg, data, cache
    assert tree_hash(data) == before, "a data-dir file changed"
    outside = [p for p in base.rglob("*") if p.is_file() and cache not in p.parents
               and data not in p.parents]
    assert outside == []


@pytest.fixture
def cli_ids(env, monkeypatch):
    cfg = env[0]
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)

    def run(*flags):
        res = CliRunner().invoke(cli, ["campaign", "records", *flags, "--json"])
        assert res.exit_code == 0, res.output
        return [r["id"] for r in json.loads(res.output)["records"]]
    return run


@pytest.fixture
def all_records(env, monkeypatch):
    cfg = env[0]
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    res = CliRunner().invoke(cli, ["campaign", "records", "--json"])
    assert res.exit_code == 0, res.output
    return json.loads(res.output)["records"]


def ids(cfg, text, now=None):
    res = F.query_records(cfg, F.parse_record_filter(text, now=now), now)
    assert res.error is None and not res.problems, (res.error, res.problems)
    return [r["id"] for r in res.records]


def words(records):
    """A few (word, other_word_in_same_title, phrase) triples that really occur."""
    out = []
    for r in records:
        toks = re.findall(r"[A-Za-z]{5,}", r.get("title") or "")
        if len(toks) >= 2 and toks[0].lower() != toks[1].lower():
            out.append((toks[0].lower(), toks[1].lower(), f"{toks[0]} {toks[1]}"))
    return out


# --- CLI-expressible: AC4 -------------------------------------------------------------------

def test_cli_expressible_filters_equal_the_cli(env, cli_ids, all_records):
    cfg = env[0]
    assert all_records, "fixture has no records"
    word = words(all_records)[0][0]
    cases = [
        ("agent:conductor", ["--agent", "conductor"]),
        ("agent:steward:Meta-Tools", ["--agent", "steward:Meta-Tools"]),
        ("kind:decision", ["--kind", "decision"]),
        ("kind:finding", ["--kind", "finding"]),
        ("severity:major", ["--severity", "major"]),
        ("status:open", ["--status", "open"]),
        (word, ["--query", word]),
        ("kind:finding severity:major status:open",
         ["--kind", "finding", "--severity", "major", "--status", "open"]),
        (f"kind:finding {word}", ["--kind", "finding", "--query", word]),
        ("agent:conductor kind:decision", ["--agent", "conductor", "--kind", "decision"]),
    ]
    nonempty = 0
    for text, flags in cases:
        want = cli_ids(*flags)
        assert ids(cfg, text) == want, text
        nonempty += bool(want)
    assert nonempty >= 8


# --- extended: AC5 --------------------------------------------------------------------------

def test_extended_filters_equal_matches_over_the_unfiltered_cli(env, all_records):
    cfg = env[0]
    tri = words(all_records)
    assert tri
    one, two, phrase = tri[0]
    stamps = sorted(H._parse_ts(r["ts"]) for r in all_records if r.get("ts"))
    now = stamps[-1] + dt.timedelta(days=2)
    cid = next(r["campaign"] for r in all_records if r.get("campaign"))
    fragment = cid.split("-")[1] + "-" + cid.split("-")[2][:6]
    cases = {
        "agent:steward": True,
        "project:meta-tools": True,
        "severity:none": True,
        "status:none": True,
        "campaign:none": True,
        f"campaign:{fragment}": True,
        f"{one} {two}": True,
        f'"{phrase.lower()}"': None,           # a phrase need not occur; only the equality counts
        "since:7d": True,
        "agent:steward severity:none since:7d": None,
        # date-only bounds: the CLI compares the day as written in the record's offset, the grammar
        # the local day, so these are held to the grammar (`matches`), not to the CLI flags
        "since:2026-01-01": True,
        "until:2026-06-30": None,
        "since:2026-01-01 until:2026-06-30": None,
    }
    for text, must_be_nonempty in cases.items():
        flt = F.parse_record_filter(text, now=now)
        assert not flt.problems, text
        want = [r["id"] for r in all_records if F.matches(r, flt, now)]
        assert ids(cfg, text, now) == want, text
        if must_be_nonempty:
            assert want, f"{text} matched nothing: the case is vacuous"
    assert 0 < len(ids(cfg, "since:7d", now)) < len(all_records)
    assert ids(cfg, "project:META-TOOLS") == ids(cfg, "project:meta-tools")


def test_total_counts_and_campaignless_records(env, all_records):
    cfg = env[0]
    res = F.query_records(cfg, F.parse_record_filter("kind:finding"))
    assert res.total_records == len(all_records)
    assert res.total_matching == len(res.records) == sum(r["kind"] == "finding" for r in all_records)
    none = F.query_records(cfg, F.parse_record_filter("campaign:none"))
    assert none.records and all(not r["campaign"] for r in none.records)
    assert [r["id"] for r in F.query_records(cfg, F.parse_record_filter("")).records] == \
        [r["id"] for r in all_records]
    assert {r["severity"] for r in all_records} - {None} <= F.SEVERITIES
    assert {r["status"] for r in all_records} - {None} <= F.STATUSES


def test_since_relative_resolves_against_the_current_time(env, all_records):
    cfg = env[0]
    flt = F.parse_record_filter("since:7d")
    res = F.query_records(cfg, flt)                 # no `now`: the clock is read here
    assert res.error is None
    assert [r["id"] for r in res.records] == [
        r["id"] for r in all_records if F.matches(r, flt)]
    cutoff = dt.datetime.now(UTC) - dt.timedelta(days=7)
    assert all(H._parse_ts(r["ts"]) >= cutoff - dt.timedelta(minutes=5) for r in res.records)


def test_unknown_kind_and_source_pass_through_the_real_history_layer(env, monkeypatch):
    """History never writes an unknown kind today; a future version might, so one is injected
    after the real layer has built its records and must come through query_records untouched."""
    cfg = env[0]
    odd = {"schema": "nw.history.record.v1", "id": "telemetry:zzz:1", "ts": "2026-11-09T00:00:00+00:00",
           "source": "telemetry", "kind": "brand_new", "agent": None, "campaign": None,
           "spec_id": None, "project": None, "severity": None, "status": None,
           "title": "odd record", "text": "", "links": {}, "linked_by": "none"}
    real = H.history_records
    monkeypatch.setattr(H, "history_records", lambda c, **kw: real(c, **kw) + [odd])
    res = F.query_records(cfg, F.parse_record_filter("odd record"))
    assert res.error is None and [r["id"] for r in res.records] == ["telemetry:zzz:1"]
    everything = F.query_records(cfg, F.parse_record_filter(""))
    assert odd in everything.records and everything.total_records == len(everything.records)


# --- the same answer on the real data dir (manual, SPEC-0412 Test plan) ---------------------

@pytest.mark.skipif(not REAL, reason="set NW_FINDINGS_REAL_DATA=1 to run against the real data dir")
def test_real_data_dir_counts_agree(env, cli_ids):
    cfg = env[0]
    assert len(ids(cfg, "severity:major since:2026-10-01")) == len(
        cli_ids("--severity", "major", "--since", "2026-10-01"))
