"""SPEC-0299: conductor session registry, transcript usage, `nw queue conductor-cost`."""
import datetime as dt
import io
import json
from zoneinfo import ZoneInfo

import pytest
from click.testing import CliRunner

from nightwright.agentic import conductor_cost as CC
from nightwright.agentic import events as E
from nightwright.agentic import schemas as SC
from nightwright.agentic import store as S
from nightwright.cli import cli

TZ = ZoneInfo("America/New_York")
PRICE = {"input": 10.0, "output": 0.0, "cache_read": 1.0, "cache_write_5m": 0.0,
         "cache_write_1h": 0.0}


@pytest.fixture
def cfg(tmp_path):
    (tmp_path / "data").mkdir()
    return {"data_dir": str(tmp_path / "data"), "config_dir": str(tmp_path / "config"),
            "_root": str(tmp_path / "projects"), "_tz": TZ, "timezone": "America/New_York",
            "agentic": {"conductor": {"prices": {"claude-x": PRICE, "claude": {"input": 99.0}}}}}


def line(msg_id, ts, *, model="claude-x", typ="assistant", usage=None, **extra):
    usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0,
             "cache_creation_input_tokens": 0, **(usage or {})}
    return {"type": typ, "timestamp": ts, "message": {"id": msg_id, "model": model,
                                                      "usage": usage, **extra}}


def write_transcript(cfg, sid, lines, *, sub=None):
    from pathlib import Path
    d = Path(cfg["_root"]) / "-slug"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{sid}.jsonl"
    path.write_text("".join((l if isinstance(l, str) else json.dumps(l)) + "\n" for l in lines))
    if sub:
        sd = d / sid / "subagents"
        sd.mkdir(parents=True)
        (sd / "agent-1.jsonl").write_text("".join(json.dumps(l) + "\n" for l in sub))
    return path


def utc(day, hour, minute=0):
    return f"2026-10-{day:02d}T{hour:02d}:{minute:02d}:00.000Z"


def turn(msg_id, ts, tokens=100_000, **kw):
    """A turn that costs tokens/1e5 dollars at PRICE (input only)."""
    return line(msg_id, ts, usage={"input_tokens": tokens}, **kw)


def campaign(cfg, cid, start, end, outcome="MERGED", total=2.0):
    st = {"id": cid, "spec_id": f"SPEC-{cid}", "project": "P", "kind": "spec", "profile": "x",
          "state": outcome or "IMPLEMENTING", "created_at": start,
          "cost": {"total_usd": total}, "counters": {}}
    S.create_campaign(cfg, st)
    recs = [{"schema": SC.LEDGER_SCHEMA, "ts": start, "seq": 1, "campaign": cid, "type": "open",
             "by": "t", **SC.with_defaults("open", {"spec_id": st["spec_id"], "project": "P"})}]
    if end:
        recs.append({"schema": SC.LEDGER_SCHEMA, "ts": end, "seq": 2, "campaign": cid,
                     "type": "transition", "by": "t",
                     **SC.with_defaults("transition", {"from": "CLOSING", "to": outcome,
                                                       "reason": "x"})})
    (S.campaign_dir(cfg, cid) / "ledger.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in recs))


# ---- registry and follower -----------------------------------------------------------------

def _follow_cfg(cfg):
    E.append(cfg, "message", by="t", data={"msg_id": "1", "from": "x", "kind": "info",
                                             "text": "t"})
    return cfg


def test_follow_records_session_once(cfg, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "abc")
    monkeypatch.setenv("CLAUDE_PID", "4242")
    _follow_cfg(cfg)
    out = io.StringIO()
    n = E.follow(cfg, "conductor", from_start=True, max_events=1, code_root=None, out=out)
    assert n == 1
    # the cursor is past the event now: the second arm stops at once
    E.follow(cfg, "conductor", code_root=None, out=io.StringIO(), stop=lambda: True)
    recs = CC.sessions(cfg)
    assert len(recs) == 1
    assert (recs[0]["reader"], recs[0]["session_id"], recs[0]["source"]) == \
        ("conductor", "abc", "follower")
    assert recs[0]["claude_pid"] == 4242 and recs[0]["cwd"]


def test_follow_without_session_env_records_nothing(cfg, monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    _follow_cfg(cfg)
    E.follow(cfg, "conductor", from_start=True, max_events=1, code_root=None, out=io.StringIO())
    assert CC.sessions(cfg) == []
    assert not (CC.conductor_dir(cfg) / "sessions.jsonl").exists()


def test_record_failure_never_breaks_follow(cfg, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "abc")

    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(CC, "record_session", boom)
    _follow_cfg(cfg)
    out = io.StringIO()
    assert E.follow(cfg, "conductor", from_start=True, max_events=1, code_root=None,
                    out=out) == 1
    assert '"message"' in out.getvalue()


def test_record_session_is_per_reader(cfg):
    assert CC.record_session(cfg, "conductor", {"CLAUDE_CODE_SESSION_ID": "s1"})
    assert CC.record_session(cfg, "conductor", {"CLAUDE_CODE_SESSION_ID": "s1"}) is None
    assert CC.record_session(cfg, "steward", {"CLAUDE_CODE_SESSION_ID": "s1"})
    assert CC.record_session(cfg, "conductor", {}) is None
    assert [r["session_id"] for r in CC.sessions(cfg, "conductor")] == ["s1"]


# ---- transcripts ---------------------------------------------------------------------------

def test_read_turns_dedupes_by_message_id(cfg):
    p = write_transcript(cfg, "s", [
        turn("m1", utc(20, 14), 1), turn("m1", utc(20, 14), 2), turn("m1", utc(20, 14), 3),
        turn("m2", utc(20, 15), 7)])
    turns = CC.read_turns(p)
    assert [t["input"] for t in turns] == [3, 7]
    assert [t["msg_id"] for t in turns] == ["m1", "m2"]


def test_read_turns_kinds_and_context(cfg):
    p = write_transcript(cfg, "s", [
        "not json {", json.dumps([1, 2]),
        line("u", utc(20, 14), typ="user", usage={"input_tokens": 5}),
        line("syn", utc(20, 14), model="<synthetic>", usage={"input_tokens": 5}),
        {"type": "assistant", "timestamp": utc(20, 14), "message": {"id": "nousage"}},
        line("a", utc(20, 14), usage={
            "input_tokens": 10, "output_tokens": 20, "cache_read_input_tokens": 300,
            "cache_creation_input_tokens": 70,
            "cache_creation": {"ephemeral_5m_input_tokens": 40,
                               "ephemeral_1h_input_tokens": 30}}),
        line("b", utc(20, 15), usage={"input_tokens": 1, "cache_creation_input_tokens": 9}),
    ])
    a, b = CC.read_turns(p)
    assert (a["input"], a["output"], a["cache_read"], a["cache_write_5m"], a["cache_write_1h"]) \
        == (10, 20, 300, 40, 30)
    assert a["context"] == 10 + 300 + 40 + 30
    assert (b["cache_write_5m"], b["cache_write_1h"], b["context"]) == (9, 0, 10)


def test_price_longest_prefix(cfg):
    assert CC.price_for(cfg, "claude-x-1")["input"] == 10.0
    assert CC.price_for(cfg, "claude-y")["input"] == 99.0
    assert CC.price_for(cfg, "gpt") is None
    t = {k: 0 for k in CC.KINDS} | {"input": 1_000_000, "cache_read": 2_000_000}
    assert CC.turn_usd(t, PRICE) == 12.0
    assert CC.turn_usd(t, None) is None


def test_unpriced_model_reported(cfg):
    write_transcript(cfg, "s", [
        turn("m1", utc(20, 14)), turn("m2", utc(20, 15)),
        turn("m3", utc(20, 16), 50, model="other-y")])
    rep = CC.cost_report(cfg, session_ids=["s"])
    t = rep["totals"]
    assert (t["turns"], t["input"], t["usd"], t["usd_complete"]) == (3, 200_050, 2.0, False)
    assert rep["unpriced_models"] == ["other-y"]
    assert t["usd_by_kind"]["input"] == 2.0
    assert rep["by_day"][0]["usd"] == 2.0
    assert rep["merges"] == 0 and rep["usd_per_merge"] is None
    assert rep["idle_usd"] == 2.0
    assert "(partial: 1 unpriced turns)" in __import__("nightwright.agentic.cli_conductor",
                                                       fromlist=["x"]).summary_line(rep)


def test_cost_report_split_and_per_merge(cfg):
    campaign(cfg, "A", "2026-10-20T14:00:00+00:00", "2026-10-20T16:00:00+00:00", total=2.0)
    campaign(cfg, "B", "2026-10-20T15:00:00+00:00", "2026-10-20T18:00:00+00:00", total=3.0)
    campaign(cfg, "L", "2026-10-22T00:00:00+00:00", None, total=9.0)       # still live, later
    write_transcript(cfg, "s", [
        turn("t0", utc(19, 15)),            # before --since
        turn("t1", utc(20, 13)),            # idle (before both)
        turn("t2", utc(20, 14, 30)),        # A only
        turn("t3", utc(20, 15, 30)),        # A and B
        turn("t4", utc(20, 16, 30)),        # B only
        turn("t5", utc(20, 19)),            # idle
        turn("t6", utc(21, 3)),             # 23:00 local on the 20th: on the --until date
        turn("t7", utc(21, 15)),            # local 21st: after --until
    ])
    import datetime as dt
    rep = CC.cost_report(cfg, since=dt.date(2026, 10, 20), until=dt.date(2026, 10, 20),
                         session_ids=["s"])
    assert rep["schema"] == "nw.conductor.cost.v1"
    assert rep["totals"]["turns"] == 6 and rep["totals"]["usd"] == 6.0
    assert rep["totals"]["usd_complete"] is True
    assert rep["idle_usd"] == 3.0
    shares = {c["campaign"]: c["conductor_usd"] for c in rep["campaigns"]}
    assert shares == {"A": 1.5, "B": 1.5}
    assert sum(shares.values()) == rep["totals"]["usd"] - rep["idle_usd"]
    assert rep["merges"] == 2 and rep["usd_per_merge"] == 3.0
    assert rep["persona_usd"] == 5.0
    assert rep["by_day"] == [{"date": "2026-10-20", "turns": 6, "usd": 6.0,
                              "cache_read": 0, "merges": 2}]
    assert rep["context"]["median"] == 100_000 and rep["context"]["max"] == 100_000
    # open bounds: the 19th and 21st count too, and L (live) takes the late turn
    rep = CC.cost_report(cfg, session_ids=["s"])
    assert rep["totals"]["turns"] == 8
    assert [d["date"] for d in rep["by_day"]] == ["2026-10-19", "2026-10-20", "2026-10-21"]
    # B ended 18:00Z, so with the until bound removed the numbers for A/B are unchanged
    assert {c["campaign"]: c["conductor_usd"] for c in rep["campaigns"]}["A"] == 1.5


def test_campaign_ends_outside_window_are_not_counted(cfg):
    # local dates in New York (UTC-4): X ended on the 19th (before --since), Y at 03:00Z on the
    # 21st (local 20th, on the --until date), Z at 15:00Z on the 21st (local 21st, after --until)
    campaign(cfg, "X", "2026-10-18T00:00:00+00:00", "2026-10-19T16:00:00+00:00", total=2.0)
    campaign(cfg, "Y", "2026-10-18T00:00:00+00:00", "2026-10-21T03:00:00+00:00", total=4.0)
    campaign(cfg, "Z", "2026-10-21T04:00:00+00:00", "2026-10-21T15:00:00+00:00", total=8.0)
    write_transcript(cfg, "s", [
        turn("a", utc(20, 14)),                      # Y only
        turn("u", utc(20, 15), 50, model="other-y"),  # unpriced, inside Y's window
        turn("i", utc(21, 3, 30)),                   # local 20th, nobody live: idle
    ])
    rep = CC.cost_report(cfg, since=dt.date(2026, 10, 20), until=dt.date(2026, 10, 20),
                         session_ids=["s"])
    assert rep["merges"] == 1
    assert rep["totals"]["usd"] == 2.0 and rep["usd_per_merge"] == rep["totals"]["usd"] / 1
    assert rep["persona_usd"] == 4.0
    assert [(d["date"], d["merges"]) for d in rep["by_day"]] == [("2026-10-20", 1)]
    assert [c["campaign"] for c in rep["campaigns"]] == ["Y"]
    y = rep["campaigns"][0]
    # the unpriced turn adds nothing to the campaign share or to idle
    assert y["conductor_usd"] == 1.0 and rep["idle_usd"] == 1.0
    assert rep["unpriced_models"] == ["other-y"]
    # open bounds: all three ended, so all three count
    rep = CC.cost_report(cfg, session_ids=["s"])
    assert rep["merges"] == 3 and rep["persona_usd"] == 14.0


def test_live_window_falls_back_to_created_at_without_open_event(cfg):
    campaign(cfg, "N", "2026-10-20T13:00:00+00:00", None)
    (S.campaign_dir(cfg, "N") / "ledger.jsonl").write_text("")
    (w,) = CC.live_windows(cfg)
    assert w["start"] == dt.datetime(2026, 10, 20, 13, tzinfo=dt.timezone.utc)
    assert w["end"] is None


def test_subagents_only_with_flag(cfg):
    write_transcript(cfg, "s", [turn("m1", utc(20, 14))], sub=[turn("x1", utc(20, 14), 500_000),
                                                                turn("x2", utc(20, 15), 500_000)])
    rep = CC.cost_report(cfg, session_ids=["s"])
    assert rep["subagents"] is None and rep["totals"]["turns"] == 1
    rep = CC.cost_report(cfg, session_ids=["s"], include_subagents=True)
    assert rep["totals"]["turns"] == 1 and rep["totals"]["usd"] == 1.0
    assert rep["subagents"]["turns"] == 2 and rep["subagents"]["usd"] == 10.0
    assert rep["subagents"]["transcripts"] == 1


# ---- CLI -----------------------------------------------------------------------------------

def test_cli_conductor_cost_json_and_text(cfg, monkeypatch):
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    campaign(cfg, "A", "2026-10-20T14:00:00+00:00", "2026-10-20T16:00:00+00:00")
    write_transcript(cfg, "s", [turn("t1", utc(20, 14, 30)), turn("t2", utc(20, 17))])
    CC.record_session(cfg, "conductor", {}, source="manual", session_id="s")
    r = CliRunner().invoke(cli, ["queue", "conductor-cost", "--json"])
    assert r.exit_code == 0, r.output
    rep = json.loads(r.output)
    assert rep["totals"]["turns"] == 2 and rep["merges"] == 1 and rep["usd_per_merge"] == 2.0
    r = CliRunner().invoke(cli, ["queue", "conductor-cost", "--since", "2026-10-20",
                                 "--until", "2026-10-20", "--session", "s"])
    assert r.exit_code == 0, r.output
    assert "2 turns" in r.output and "per merge (1 merges)" in r.output
    assert "By day" in r.output and "A" in r.output
    r = CliRunner().invoke(cli, ["queue", "conductor-cost", "--since", "nope"])
    assert r.exit_code != 0


def test_conductor_session_add_list(cfg, monkeypatch):
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    r = CliRunner().invoke(cli, ["queue", "conductor-session", "list"])
    assert r.exit_code == 0 and "no sessions" in r.output
    r = CliRunner().invoke(cli, ["queue", "conductor-session", "add", "ghost"])
    assert r.exit_code == 0 and "warning" in r.output and "registered" in r.output
    write_transcript(cfg, "real", [turn("m", utc(20, 14))])
    r = CliRunner().invoke(cli, ["queue", "conductor-session", "add", "real"])
    assert r.exit_code == 0 and "warning" not in r.output
    r = CliRunner().invoke(cli, ["queue", "conductor-session", "add", "real"])
    assert "already registered" in r.output
    r = CliRunner().invoke(cli, ["queue", "conductor-session", "list", "--json"])
    rows = json.loads(r.output)["sessions"]
    assert [(x["session_id"], x["source"]) for x in rows] == [("ghost", "manual"),
                                                             ("real", "manual")]


# ---- review round: merge states, context percentiles, --session, dedupe, bad prices ---------

def test_merges_count_merged_and_handed_off_only(cfg):
    campaign(cfg, "H", "2026-10-20T14:00:00+00:00", "2026-10-20T16:00:00+00:00",
             outcome="HANDED_OFF", total=1.0)
    campaign(cfg, "X", "2026-10-20T14:00:00+00:00", "2026-10-20T17:00:00+00:00",
             outcome="ABANDONED", total=4.0)
    campaign(cfg, "M", "2026-10-21T13:00:00+00:00", "2026-10-21T15:00:00+00:00", total=2.0)
    write_transcript(cfg, "s", [turn("t1", utc(20, 14, 30)),        # H and X live
                                turn("t2", utc(21, 14))])           # M live
    rep = CC.cost_report(cfg, session_ids=["s"])
    assert rep["merges"] == 2                       # H counts, ABANDONED X does not
    assert rep["usd_per_merge"] == 1.0              # $2 / 2
    assert [(d["date"], d["merges"]) for d in rep["by_day"]] == [("2026-10-20", 1),
                                                                 ("2026-10-21", 1)]
    by = {c["campaign"]: c for c in rep["campaigns"]}
    assert set(by) == {"H", "X", "M"}
    assert by["X"]["outcome"] == "ABANDONED" and by["X"]["conductor_usd"] == 0.5
    assert by["H"]["outcome"] == "HANDED_OFF"
    assert rep["persona_usd"] == 7.0                # X's persona spend is still counted


def test_context_percentiles(cfg):
    # written newest first, so `last` must come from the time order, not the file order
    write_transcript(cfg, "s", [turn(f"m{i}", utc(20, 10 + i), i * 1000) for i in range(10, 0, -1)])
    ctx = CC.cost_report(cfg, session_ids=["s"])["context"]
    assert ctx == {"median": 5500, "p90": 9000, "max": 10_000, "last": 10_000}
    write_transcript(cfg, "s", [turn(f"m{i}", utc(20, 20 - i), i * 1000) for i in range(1, 11)])
    assert CC.cost_report(cfg, session_ids=["s"])["context"]["last"] == 1000


def test_window_edges_are_start_inclusive_end_exclusive(cfg):
    campaign(cfg, "A", "2026-10-20T14:00:00+00:00", "2026-10-20T16:00:00+00:00")
    write_transcript(cfg, "s", [turn("at-open", utc(20, 14)), turn("at-end", utc(20, 16))])
    rep = CC.cost_report(cfg, session_ids=["s"])
    assert rep["campaigns"][0]["conductor_usd"] == 1.0 and rep["idle_usd"] == 1.0


def test_session_option_overrides_registry(cfg, monkeypatch):
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    write_transcript(cfg, "A", [turn("a1", utc(20, 14))])
    write_transcript(cfg, "B", [turn("b1", utc(20, 14)), turn("b2", utc(20, 15)),
                                turn("b3", utc(20, 16), 300_000)])
    CC.record_session(cfg, "conductor", {}, source="manual", session_id="A")
    assert CC.cost_report(cfg)["totals"]["turns"] == 1               # the registry alone: A
    rep = CC.cost_report(cfg, session_ids=["B"])
    assert rep["totals"]["turns"] == 3 and [s["session_id"] for s in rep["sessions"]] == ["B"]
    r = CliRunner().invoke(cli, ["queue", "conductor-cost", "--json", "--session", "B"])
    assert r.exit_code == 0, r.output
    rep = json.loads(r.output)
    assert rep["totals"]["turns"] == 3 and rep["totals"]["usd"] == 5.0
    assert [s["session_id"] for s in rep["sessions"]] == ["B"]


def test_turns_deduped_across_sessions(cfg):
    campaign(cfg, "A", "2026-10-20T13:00:00+00:00", "2026-10-20T18:00:00+00:00")
    # a resumed session replays the earlier history under a new id
    write_transcript(cfg, "old", [turn("m1", utc(20, 14)), turn("m2", utc(20, 15))])
    write_transcript(cfg, "new", [turn("m1", utc(20, 14)), turn("m2", utc(20, 15)),
                                  turn("m3", utc(20, 16))])
    for sid in ("old", "new"):
        CC.record_session(cfg, "conductor", {}, source="manual", session_id=sid)
    rep = CC.cost_report(cfg)
    assert rep["totals"]["turns"] == 3 and rep["totals"]["usd"] == 3.0
    assert [s["turns"] for s in rep["sessions"]] == [2, 1]
    assert CC.attribute(cfg) == {"A": 3.0}
    rep = CC.cost_report(cfg, session_ids=["old", "old"])
    assert rep["totals"]["turns"] == 2 and len(rep["sessions"]) == 1


def test_malformed_price_entry_means_unpriced(cfg):
    write_transcript(cfg, "s", [turn("m1", utc(20, 14))])
    CC.record_session(cfg, "conductor", {}, source="manual", session_id="s")
    for bad in ({"input": "$15"}, {"input": [1]}, {"input": True}):
        cfg["agentic"]["conductor"]["prices"] = {"claude-x": bad}
        assert CC.price_for(cfg, "claude-x") is None
        rep = CC.cost_report(cfg)
        assert rep["totals"]["usd"] == 0.0 and rep["unpriced_models"] == ["claude-x"]
        assert CC.attribute(cfg) is None
    cfg["agentic"]["conductor"]["prices"] = {"claude-x": {"input": 3}}
    assert CC.price_for(cfg, "claude-x") == {"input": 3}


def test_session_ids_with_path_or_glob_chars_never_match(cfg):
    write_transcript(cfg, "s", [turn("m1", utc(20, 14))])
    for sid in ("*", "../s", "s*", "[s]", ""):
        assert CC.find_transcript(cfg, sid) is None
        assert CC.subagent_transcripts(cfg, sid) == []


def test_conductor_session_list_text_rows(cfg, monkeypatch):
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    CC.record_session(cfg, "conductor", {"CLAUDE_PID": "77"}, session_id="sess-one")
    r = CliRunner().invoke(cli, ["queue", "conductor-session", "list"])
    assert r.exit_code == 0, r.output
    assert "sess-one" in r.output and "follower" in r.output


# ---- SPEC-0321: hardening --------------------------------------------------------------------

def set_state(cfg, cid, **changes):
    """Rewrite state.json directly: `store.write_state` restamps `updated_at`. A value of None
    removes the key."""
    path = S._state_path(cfg, cid)
    st = json.loads(path.read_text())
    for k, v in changes.items():
        if v is None:
            st.pop(k, None)
        else:
            st[k] = v
    path.write_text(json.dumps(st))


def add_transition(cfg, cid, ts, frm="IMPLEMENTING", to="REVIEWING"):
    rec = {"schema": SC.LEDGER_SCHEMA, "ts": ts, "seq": 9, "campaign": cid, "type": "transition",
           "by": "t", **SC.with_defaults("transition", {"from": frm, "to": to, "reason": "x"})}
    with open(S.campaign_dir(cfg, cid) / "ledger.jsonl", "a") as f:
        f.write(json.dumps(rec) + "\n")


def windows_by_id(cfg):
    return {w["campaign"]: w for w in CC.live_windows(cfg)}


def utcdt(day, hour, minute=0):
    return dt.datetime(2026, 10, day, hour, minute, tzinfo=dt.timezone.utc)


def test_terminal_state_without_transition_stops_absorbing(cfg):
    campaign(cfg, "T", "2026-10-20T13:00:00+00:00", None, outcome="MERGED")
    campaign(cfg, "L", "2026-10-20T12:00:00+00:00", "2026-10-20T17:00:00+00:00")
    set_state(cfg, "T", updated_at="2026-10-20T16:00:00+00:00")
    w = windows_by_id(cfg)["T"]
    assert w["live_end"] == utcdt(20, 16) and w["end"] is None and w["outcome"] is None
    write_transcript(cfg, "s", [turn("a", utc(20, 15)), turn("b", utc(20, 18))])
    rep = CC.cost_report(cfg, session_ids=["s"])
    by = {c["campaign"]: c["conductor_usd"] for c in rep["campaigns"]}
    assert by == {"T": 0.5, "L": 0.5}               # the 15:00 turn is split with L
    assert rep["idle_usd"] == 1.0                   # the 18:00 turn: T had ended
    assert rep["merges"] == 1                       # only L (T has no terminal transition)
    assert [d["merges"] for d in rep["by_day"]] == [1]
    assert rep["persona_usd"] == 2.0                # L only
    CC.record_session(cfg, "conductor", {}, source="manual", session_id="s")
    assert CC.attribute(cfg) == {"T": 0.5, "L": 0.5}


def test_terminal_state_without_updated_at_gets_zero_length_window(cfg):
    campaign(cfg, "T", "2026-10-20T13:00:00+00:00", None, outcome="MERGED")
    set_state(cfg, "T", updated_at=None)
    w = windows_by_id(cfg)["T"]
    assert w["live_end"] == w["start"] == utcdt(20, 13) and w["end"] is None
    write_transcript(cfg, "s", [turn("a", utc(20, 13)), turn("b", utc(20, 15))])
    rep = CC.cost_report(cfg, session_ids=["s"])
    assert rep["campaigns"] == [] and rep["idle_usd"] == 2.0
    CC.record_session(cfg, "conductor", {}, source="manual", session_id="s")
    assert CC.attribute(cfg).get("T", 0.0) == 0.0


def test_terminal_state_without_transition_uses_last_transition_when_updated_at_is_late(cfg):
    campaign(cfg, "T", "2026-10-20T13:00:00+00:00", None, outcome="MERGED")
    add_transition(cfg, "T", "2026-10-20T14:00:00+00:00")
    set_state(cfg, "T", updated_at="2026-10-27T09:00:00+00:00")
    w = windows_by_id(cfg)["T"]
    assert w["live_end"] == utcdt(20, 14) and w["end"] is None
    write_transcript(cfg, "s", [turn("a", utc(20, 15))])
    rep = CC.cost_report(cfg, session_ids=["s"])
    assert rep["campaigns"] == [] and rep["idle_usd"] == 1.0
    set_state(cfg, "T", updated_at="not a date")        # only the transition parses
    assert windows_by_id(cfg)["T"]["live_end"] == utcdt(20, 14)
    set_state(cfg, "T", updated_at="2026-10-20T13:30:00+00:00")     # updated_at is earlier
    assert windows_by_id(cfg)["T"]["live_end"] == utcdt(20, 13, 30)


def test_non_terminal_without_transition_stays_live(cfg):
    campaign(cfg, "R", "2026-10-20T13:00:00+00:00", None, outcome=None)
    set_state(cfg, "R", updated_at="2026-10-20T14:00:00+00:00")
    assert windows_by_id(cfg)["R"]["live_end"] is None
    write_transcript(cfg, "s", [turn("a", utc(20, 15)), turn("b", utc(22, 15))])
    rep = CC.cost_report(cfg, session_ids=["s"])
    assert [(c["campaign"], c["conductor_usd"]) for c in rep["campaigns"]] == [("R", 2.0)]


def test_terminal_transition_sets_live_end_to_end(cfg):
    campaign(cfg, "A", "2026-10-20T13:00:00+00:00", "2026-10-20T16:00:00+00:00")
    w = windows_by_id(cfg)["A"]
    assert w["live_end"] == w["end"] == utcdt(20, 16) and w["outcome"] == "MERGED"


def test_price_for_tolerates_malformed_config_shapes(cfg):
    for agentic in ([1], "x", {"conductor": [1]}, {"conductor": "x"},
                    {"conductor": {"prices": "x"}}, {"conductor": {"prices": [1]}}):
        assert CC.price_for({"agentic": agentic}, "claude-x") is None
    assert CC.price_for({}, "claude-x") is None


def test_non_finite_and_negative_prices_mean_unpriced(cfg):
    write_transcript(cfg, "s", [turn("m1", utc(20, 14))])
    campaign(cfg, "A", "2026-10-20T13:00:00+00:00", "2026-10-20T16:00:00+00:00")
    CC.record_session(cfg, "conductor", {}, source="manual", session_id="s")
    for bad in (float("nan"), float("inf"), -1, -0.5):
        cfg["agentic"]["conductor"]["prices"] = {"claude-x": {"input": bad}}
        assert CC.price_for(cfg, "claude-x") is None
        rep = CC.cost_report(cfg)
        json.dumps(rep, allow_nan=False)
        assert rep["totals"]["usd"] == 0.0 and rep["unpriced_models"] == ["claude-x"]
        assert CC.attribute(cfg) is None


def test_sessions_survive_non_utf8_bytes(cfg):
    d = CC.conductor_dir(cfg)
    d.mkdir(parents=True)
    ok = lambda sid: json.dumps({"ts": "t", "reader": "conductor", "session_id": sid}).encode()
    (d / "sessions.jsonl").write_bytes(ok("one") + b"\n\xff\xfe\n" + ok("two") + b"\n")
    assert [r["session_id"] for r in CC.sessions(cfg)] == ["one", "two"]


def test_find_transcript_does_not_glob(cfg, monkeypatch):
    path = write_transcript(cfg, "s", [turn("m1", utc(20, 14))])

    def boom(self, *a, **k):
        raise AssertionError("glob")
    monkeypatch.setattr("pathlib.Path.glob", boom)
    assert CC.find_transcript(cfg, "s") == path
    assert CC.find_transcript(cfg, "nope") is None
    for sid in (".", ".."):
        assert CC.find_transcript(cfg, sid) is None


def test_subagent_transcripts_exact_lookup(cfg, monkeypatch):
    from pathlib import Path
    write_transcript(cfg, "s", [turn("m1", utc(20, 14))], sub=[turn("x", utc(20, 14))])
    root = Path(cfg["_root"])
    (root / "subagents").mkdir()
    (root / "subagents" / "x.jsonl").write_text("{}\n")      # what `*/../subagents/**` hits
    (root / "-slug" / "ab").mkdir()
    (root / "-slug" / "ab" / "subagents").mkdir()
    (root / "-slug" / "ab" / "subagents" / "y.jsonl").write_text("{}\n")
    assert [p.name for p in CC.subagent_transcripts(cfg, "s")] == ["agent-1.jsonl"]
    for sid in (".", "..", "a/b", "a*"):
        assert CC.subagent_transcripts(cfg, sid) == []
    receivers = []
    real = Path.glob

    def spy(self, pattern, *a, **k):
        receivers.append(self)
        return real(self, pattern, *a, **k)
    monkeypatch.setattr("pathlib.Path.glob", spy)
    CC.subagent_transcripts(cfg, "s")
    assert receivers == [root / "-slug" / "s" / "subagents"]


def test_read_turns_streams(cfg, monkeypatch):
    big = {"type": "user", "timestamp": utc(20, 14), "message": {"content": "x" * 50_000}}
    result = {"type": "user", "timestamp": utc(20, 14), "toolUseResult": "y" * 50_000,
              "message": {"content": [{"type": "tool_result", "content": "z" * 1000}]}}
    mention = {"type": "user", "timestamp": utc(20, 14),
               "message": {"content": 'the word "assistant" and compact_boundary'}}
    brief = line("m2", utc(20, 15), content=[{"type": "tool_use", "name": "Bash",
                                              "input": {"command": "wt queue conductor-brief"}}])
    boundary = {"type": "system", "subtype": "compact_boundary", "timestamp": utc(20, 16)}
    # U+2028 raw inside a text block: str.splitlines used to cut the line in two and drop the turn
    sep = json.dumps(line("m3", utc(20, 17), content=[{"type": "text", "text": "a b"}]),
                     ensure_ascii=False)
    assert " " in sep
    path = write_transcript(cfg, "s", [big, turn("m1", utc(20, 14), 5), mention, result, brief,
                                       boundary])
    with open(path, "a", encoding="utf-8") as f:
        f.write(sep + "\n")

    def boom(self, *a, **k):
        raise AssertionError("read_text")
    monkeypatch.setattr("pathlib.Path.read_text", boom)
    turns = CC.read_turns(path)
    assert [t["msg_id"] for t in turns] == ["m1", "m2", "m3"]
    assert turns[0]["input"] == 5
    marks = CC.transcript_marks(path)
    assert [(k, i) for _, k, i in marks] == [("brief", 1), ("compact_boundary", 2)]
    assert CC.read_turns(path.with_name("missing.jsonl")) == []
    assert CC.transcript_marks(path.with_name("missing.jsonl")) == []


def _mixed(cfg):
    campaign(cfg, "L", "2026-10-20T13:00:00+00:00", None, outcome=None)
    write_transcript(cfg, "s", [
        turn("a", utc(20, 14)),                       # priced, inside L
        turn("u", utc(20, 14, 30), 50, model="other-y"),   # unpriced, inside L
        turn("b", utc(20, 15)),                       # priced, inside L
        turn("o", utc(20, 12, 30)),                   # priced, before any window
        turn("v", utc(20, 12, 0), 50, model="other-y"),    # unpriced, outside any window
    ])
    CC.record_session(cfg, "conductor", {}, source="manual", session_id="s")


def test_mixed_priced_unpriced_with_live_campaign(cfg):
    _mixed(cfg)
    rep = CC.cost_report(cfg)
    assert [(c["campaign"], c["conductor_usd"]) for c in rep["campaigns"]] == [("L", 2.0)]
    assert rep["idle_usd"] == 1.0
    assert rep["totals"]["unpriced_turns"] == 2 and rep["totals"]["usd"] == 3.0
    assert rep["unpriced_models"] == ["other-y"]
    assert CC.attribute(cfg) == {"L": 2.0}


def test_conductor_session_reader_option_through_cli(cfg, monkeypatch):
    monkeypatch.setattr("wt.cli.load_config", lambda: cfg)
    run = lambda *a: CliRunner().invoke(cli, ["queue", "conductor-session", *a])
    assert run("add", "--reader", "x", "sess-x").exit_code == 0
    r = run("list", "--reader", "x")
    assert r.exit_code == 0 and "sess-x" in r.output
    r = run("list")
    assert r.exit_code == 0 and "sess-x" not in r.output and "no sessions" in r.output


def test_conductor_session_list_json_marks_missing_transcript(cfg, monkeypatch):
    monkeypatch.setattr("wt.cli.load_config", lambda: cfg)
    CC.record_session(cfg, "conductor", {}, source="manual", session_id="ghost")
    r = CliRunner().invoke(cli, ["queue", "conductor-session", "list", "--json"])
    assert [x["session_id"] for x in json.loads(r.output)["sessions"]] == ["ghost"]
    r = CliRunner().invoke(cli, ["queue", "conductor-cost", "--json"])
    (row,) = json.loads(r.output)["sessions"]
    assert row["session_id"] == "ghost" and row["found"] is False and row["path"] is None


def test_conductor_cost_text_without_session_or_transcript(cfg, monkeypatch):
    monkeypatch.setattr("wt.cli.load_config", lambda: cfg)
    r = CliRunner().invoke(cli, ["queue", "conductor-cost"])
    assert r.exit_code == 0 and "no conductor session registered" in r.output
    CC.record_session(cfg, "conductor", {}, source="manual", session_id="ghost")
    r = CliRunner().invoke(cli, ["queue", "conductor-cost"])
    assert r.exit_code == 0 and "no transcript for session ghost" in r.output


def test_conductor_cost_text_shows_partial_suffix(cfg, monkeypatch):
    monkeypatch.setattr("wt.cli.load_config", lambda: cfg)
    campaign(cfg, "L", "2026-10-20T13:00:00+00:00", None, outcome=None)
    write_transcript(cfg, "s", [turn("a", utc(20, 14)),
                                turn("u", utc(20, 14, 30), 50, model="other-y")])
    CC.record_session(cfg, "conductor", {}, source="manual", session_id="s")
    r = CliRunner().invoke(cli, ["queue", "conductor-cost"])
    assert r.exit_code == 0, r.output
    assert "(partial: 1 unpriced turns)" in r.output and "no price for: other-y" in r.output
