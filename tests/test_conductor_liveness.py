"""SPEC-0485: conductor liveness when an unacked wake stays past conductor_unacked_after_s."""
import datetime as dt
import json
import os
from pathlib import Path

import pytest

from nightwright.agentic import conductor_cost as CC
from nightwright.agentic import events as E
from nightwright.agentic import gate_slots as GS
from nightwright.agentic import store
from nightwright.agentic import mailbox as M
from nightwright.agentic import quiet as QT
from nightwright.agentic import herdr_watch as HW
from test_agentic_events import _cfg

T0 = dt.datetime(2026, 11, 9, 12, 0, 0).astimezone()


def at(s):
    return T0 + dt.timedelta(seconds=s)


def iso(s):
    return at(s).isoformat(timespec="seconds")


def _human_mail(cfg):
    p = Path(cfg["data_dir"]) / "agentic"
    return [json.loads(x) for f in p.rglob("*.jsonl") if "human" in f.name
            for x in f.read_text().splitlines() if x.strip()]


def _conductor_cursor(cfg, after, *, pending=None, ts=None):
    doc = {"reader": "conductor", "after": after,
           "ts": ts if ts is not None else iso(0)}
    if pending is not None:
        doc["pending"] = pending
    p = E.cursors_dir(cfg) / "conductor.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(doc) + "\n")


def _cu_events(cfg):
    return [e for e in E.read_since(cfg, 0) if e["type"] == "conductor_unresponsive"]


def _cu_from_sync(sync_out):
    return [e for e in sync_out if e["type"] == "conductor_unresponsive"]


def test_unacked_ping_escalates_once(tmp_path):
    cfg = _cfg(tmp_path)
    wake = E.append(cfg, "runner_dead", data={"active": 0, "queued": 0})
    _conductor_cursor(cfg, 0, pending=wake["id"])
    assert _cu_from_sync(E.sync(cfg, now=at(0), ping=True)) == []
    out = _cu_from_sync(E.sync(cfg, now=at(1800), ping=True))
    assert len(out) == 1
    d = out[0]["data"]
    for k in ("ping_id", "cursor_after", "unacked_s", "follower_pid", "relaunch"):
        assert k in d
    assert d["cursor_after"] == 0 and d["unacked_s"] >= 1800
    proj = json.loads((store.agentic_dir(cfg) / "events.projector.json").read_text())
    assert d["ping_id"] == proj["last_ping_id"]
    assert d["follower_pid"] is None
    assert _cu_from_sync(E.sync(cfg, now=at(1900), ping=True)) == []
    proj_mid = json.loads((store.agentic_dir(cfg) / "events.projector.json").read_text())
    ping_before = proj_mid["last_ping_id"]
    assert _cu_from_sync(E.sync(cfg, now=at(2400), ping=True)) == []
    proj_late = json.loads((store.agentic_dir(cfg) / "events.projector.json").read_text())
    assert proj_late["last_ping_id"] > ping_before
    mail = _human_mail(cfg)
    assert len(mail) == 1 and mail[0]["from"] == "conductor" and mail[0]["kind"] == "info"
    text = mail[0]["text"]
    assert str(d["unacked_s"]) in text
    assert "In the conductor's terminal" in text and "stop that session" in text
    assert text.index("stop that session") < text.index(d["relaunch"])
    assert len(_cu_events(cfg)) == 1


def test_mail_failure_is_retried_without_a_second_event(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    wake = E.append(cfg, "runner_dead", data={"active": 0, "queued": 0})
    _conductor_cursor(cfg, 0, pending=wake["id"])
    calls = {"n": 0}
    real = M.send

    def flaky_send(cfg, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("down")
        return real(cfg, **kw)

    monkeypatch.setattr(M, "send", flaky_send)
    E.sync(cfg, now=at(0), ping=True)
    E.sync(cfg, now=at(1800), ping=True)
    assert len(_cu_events(cfg)) == 1 and len(_human_mail(cfg)) == 0
    E.sync(cfg, now=at(1900), ping=True)
    assert len(_cu_events(cfg)) == 1 and len(_human_mail(cfg)) == 1


def _reset_cu_episode(cfg, *, since_s=0, now_s=0):
    wake = E.append(cfg, "runner_dead", data={"active": 0, "queued": 0})
    _conductor_cursor(cfg, 0, pending=wake["id"])
    E.sync(cfg, now=at(now_s), ping=True)
    proj_path = store.agentic_dir(cfg) / "events.projector.json"
    proj = json.loads(proj_path.read_text())
    for k in ("conductor_unresponsive_ping", "conductor_unresponsive_mailed",
              "conductor_unacked_pending_id"):
        proj.pop(k, None)
    proj["conductor_unacked_since"] = iso(since_s)
    proj_path.write_text(json.dumps(proj) + "\n")
    return wake


def _fire_cu(cfg, at_s):
    E.sync(cfg, now=at(at_s - 1), ping=True)
    out = _cu_from_sync(E.sync(cfg, now=at(at_s), ping=True))
    assert len(out) == 1
    return out[0]["data"]


def test_relaunch_command_follows_the_launch_record(tmp_path):
    cfg = _cfg(tmp_path)
    _reset_cu_episode(cfg, since_s=0, now_s=-1)
    d = _cu_from_sync(E.sync(cfg, now=at(1800), ping=True))[0]["data"]
    assert d["relaunch"] == "nw queue conductor-launch"
    for at_s, sid in ((3600, "f-1"), (5400, "m-1")):
        src = "follower" if sid == "f-1" else "manual"
        CC.record_session(cfg, "conductor", {}, source=src, session_id=sid)
        _reset_cu_episode(cfg, since_s=at_s - 1800)
        d_src = _fire_cu(cfg, at_s)
        assert d_src["relaunch"] == "nw queue conductor-launch"
    CC.record_session(cfg, "conductor", {}, source="launch", session_id="s-1")
    at_s = 7200
    _reset_cu_episode(cfg, since_s=at_s - 1800)
    d2 = _fire_cu(cfg, at_s)
    assert d2["relaunch"] == "nw queue conductor-launch --resume"
    mail = _human_mail(cfg)[-1]
    text = mail["text"]
    assert "nw queue conductor-launch --resume" in text
    assert text.index("stop that session") < text.index("nw queue conductor-launch --resume")


def test_follower_pid_and_the_reader_lock_stays_held(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    wake = E.append(cfg, "runner_dead", data={"active": 0, "queued": 0})
    _conductor_cursor(cfg, 0, pending=wake["id"])
    live = os.getpid()
    (E.cursors_dir(cfg) / "conductor.pid").write_text(str(live))
    monkeypatch.setattr(GS, "_pid_alive", lambda p: p == live)
    E.sync(cfg, now=at(0), ping=True)
    d = _cu_from_sync(E.sync(cfg, now=at(1800), ping=True))[0]["data"]
    assert d["follower_pid"] == live
    (E.cursors_dir(cfg) / "conductor.pid").write_text("999999")
    _conductor_cursor(cfg, 0, pending=wake["id"])
    proj_path = store.agentic_dir(cfg) / "events.projector.json"
    proj = json.loads(proj_path.read_text())
    for k in ("conductor_unresponsive_ping", "conductor_unresponsive_mailed"):
        proj.pop(k, None)
    proj["conductor_unacked_since"] = iso(0)
    proj_path.write_text(json.dumps(proj) + "\n")
    d2 = _cu_from_sync(E.sync(cfg, now=at(3600), ping=True))[0]["data"]
    assert d2["follower_pid"] is None
    with E.reader_lock(cfg, "conductor"):
        E.sync(cfg, now=at(3700), ping=True)
        assert E.reader_holder(cfg, "conductor") is not None


def test_healthy_young_missing_disabled_and_non_ping_sync_stay_quiet(tmp_path):
    cfg = _cfg(tmp_path)
    wake = E.append(cfg, "runner_dead", data={"active": 0, "queued": 0})
    E.cursor_set(cfg, "conductor", 0)
    E.cursor_note_pending(cfg, "conductor", wake["id"])
    E.commit_ack(cfg, "conductor", wake["id"])
    _conductor_cursor(cfg, wake["id"], ts=iso(-2000))
    E.sync(cfg, now=at(0), ping=True)
    E.sync(cfg, now=at(600), ping=True)
    E.sync(cfg, now=at(2500), ping=True)
    assert _cu_events(cfg) == [] and _human_mail(cfg) == []
    young = wake["id"] + 1
    _conductor_cursor(cfg, 0, pending=young)
    E.sync(cfg, now=at(2600), ping=True)
    assert _cu_events(cfg) == []
    (E.cursors_dir(cfg) / "conductor.json").unlink()
    E.sync(cfg, now=at(5000), ping=True)
    assert _cu_events(cfg) == [] and _human_mail(cfg) == []
    cfg["agentic"]["events"] = {"conductor_unacked_after_s": 0}
    proj_path = store.agentic_dir(cfg) / "events.projector.json"
    proj = json.loads(proj_path.read_text())
    proj["conductor_unacked_since"] = iso(0)
    proj_path.write_text(json.dumps(proj) + "\n")
    _conductor_cursor(cfg, 0, pending=wake["id"])
    E.sync(cfg, now=at(5500), ping=True)
    assert _cu_events(cfg) == [] and _human_mail(cfg) == []
    # disabled threshold must not seed conductor_unacked_since on a fresh unacked wake
    (E.cursors_dir(cfg) / "conductor.json").unlink(missing_ok=True)
    for k in ("conductor_unacked_since", "conductor_unacked_pending_id"):
        proj.pop(k, None)
    proj_path.write_text(json.dumps(proj) + "\n")
    _conductor_cursor(cfg, 0, pending=wake["id"])
    E.sync(cfg, now=at(6100), ping=True)
    proj = json.loads(proj_path.read_text())
    assert "conductor_unacked_since" not in proj
    cfg["agentic"]["events"] = {}
    n_ev = len(_cu_events(cfg))
    n_mail = len(_human_mail(cfg))
    for k in ("conductor_unresponsive_ping", "conductor_unresponsive_mailed"):
        proj.pop(k, None)
    proj["conductor_unacked_since"] = iso(0)
    proj_path.write_text(json.dumps(proj) + "\n")
    _conductor_cursor(cfg, 0, pending=wake["id"])
    assert _cu_from_sync(E.sync(cfg, now=at(8000), ping=False)) == []
    assert len(_cu_events(cfg)) == n_ev and len(_human_mail(cfg)) == n_mail


def test_unparseable_conductor_unacked_since_heals_then_fires(tmp_path):
    cfg = _cfg(tmp_path)
    wake = E.append(cfg, "runner_dead", data={"active": 0, "queued": 0})
    _conductor_cursor(cfg, 0, pending=wake["id"])
    E.sync(cfg, now=at(-60), ping=True)
    proj_path = store.agentic_dir(cfg) / "events.projector.json"
    proj = json.loads(proj_path.read_text())
    proj["conductor_unacked_since"] = "not-a-timestamp"
    proj_path.write_text(json.dumps(proj) + "\n")
    assert _cu_from_sync(E.sync(cfg, now=at(0), ping=True)) == []
    proj = json.loads(proj_path.read_text())
    since = proj["conductor_unacked_since"]
    assert since != "not-a-timestamp"
    dt.datetime.fromisoformat(since)
    assert _cu_from_sync(E.sync(cfg, now=at(1799), ping=True)) == []
    out = _cu_from_sync(E.sync(cfg, now=at(1800), ping=True))
    assert len(out) == 1
    proj = json.loads(proj_path.read_text())
    assert out[0]["data"]["ping_id"] == proj["last_ping_id"]


def test_ack_of_timed_wake_resets_clock_for_new_pending(tmp_path):
    cfg = _cfg(tmp_path)
    wake = E.append(cfg, "runner_dead", data={"active": 0, "queued": 0})
    _conductor_cursor(cfg, 0, pending=wake["id"])
    E.sync(cfg, now=at(0), ping=True)
    proj_path = store.agentic_dir(cfg) / "events.projector.json"
    proj = json.loads(proj_path.read_text())
    proj["conductor_unacked_since"] = iso(-1700)
    proj["conductor_unacked_pending_id"] = wake["id"]
    proj_path.write_text(json.dumps(proj) + "\n")
    E.commit_ack(cfg, "conductor", wake["id"])
    wake2 = E.append(cfg, "runner_dead", data={"active": 0, "queued": 0})
    E.cursor_note_pending(cfg, "conductor", wake2["id"])
    assert _cu_from_sync(E.sync(cfg, now=at(200), ping=True)) == []
    assert _cu_from_sync(E.sync(cfg, now=at(1999), ping=True)) == []
    assert len(_cu_from_sync(E.sync(cfg, now=at(2000), ping=True))) == 1


def test_threshold_300_fires_at_300_not_299(tmp_path):
    cfg = _cfg(tmp_path)
    cfg["agentic"]["events"] = {"conductor_unacked_after_s": 300, "ping_interval_s": 600}
    wake = E.append(cfg, "runner_dead", data={"active": 0, "queued": 0})
    _conductor_cursor(cfg, 0, pending=wake["id"])
    E.sync(cfg, now=at(0), ping=True)
    wake2 = E.append(cfg, "runner_dead", data={"active": 0, "queued": 0})
    _conductor_cursor(cfg, 0, pending=wake2["id"], ts=iso(150))
    E.sync(cfg, now=at(150), ping=True)
    assert _cu_from_sync(E.sync(cfg, now=at(299), ping=True)) == []
    out = _cu_from_sync(E.sync(cfg, now=at(300), ping=True))
    assert len(out) == 1
    proj = json.loads((store.agentic_dir(cfg) / "events.projector.json").read_text())
    assert proj.get("last_ping_id") is None
    assert out[0]["data"]["ping_id"] == wake2["id"]


def test_newer_pending_before_ack_does_not_retarget_clock(tmp_path):
    cfg = _cfg(tmp_path)
    wake_a = E.append(cfg, "runner_dead", data={"active": 0, "queued": 0})
    _conductor_cursor(cfg, 0, pending=wake_a["id"])
    E.sync(cfg, now=at(0), ping=True)
    wake_b = E.append(cfg, "runner_dead", data={"active": 0, "queued": 0})
    _conductor_cursor(cfg, 0, pending=wake_b["id"])
    E.sync(cfg, now=at(1790), ping=True)
    proj = json.loads((store.agentic_dir(cfg) / "events.projector.json").read_text())
    assert proj.get("conductor_unacked_pending_id") == wake_a["id"]
    E.commit_ack(cfg, "conductor", wake_a["id"])
    assert _cu_from_sync(E.sync(cfg, now=at(1800), ping=True)) == []
    assert len(_cu_from_sync(E.sync(cfg, now=at(3600), ping=True))) == 1


def test_restart_after_mailed_episode_fires_again_for_new_pending(tmp_path):
    cfg = _cfg(tmp_path)
    wake_a = E.append(cfg, "runner_dead", data={"active": 0, "queued": 0})
    _conductor_cursor(cfg, 0, pending=wake_a["id"])
    E.sync(cfg, now=at(0), ping=True)
    E.sync(cfg, now=at(1800), ping=True)
    assert len(_cu_events(cfg)) == 1 and len(_human_mail(cfg)) == 1
    proj_path = store.agentic_dir(cfg) / "events.projector.json"
    proj = json.loads(proj_path.read_text())
    assert proj.get("conductor_unacked_pending_id") == wake_a["id"]
    wake_b = E.append(cfg, "runner_dead", data={"active": 0, "queued": 0})
    E.commit_ack(cfg, "conductor", wake_a["id"])
    E.cursor_note_pending(cfg, "conductor", wake_b["id"])
    assert _cu_from_sync(E.sync(cfg, now=at(1900), ping=True)) == []
    proj = json.loads(proj_path.read_text())
    assert "conductor_unresponsive_ping" not in proj
    assert "conductor_unresponsive_mailed" not in proj
    assert proj.get("conductor_unacked_pending_id") == wake_b["id"]
    assert _cu_from_sync(E.sync(cfg, now=at(2000), ping=True)) == []
    assert len(_human_mail(cfg)) == 1
    E.sync(cfg, now=at(3700), ping=True)
    assert len(_cu_events(cfg)) == 2 and len(_human_mail(cfg)) == 2
    assert _cu_events(cfg)[1]["data"]["unacked_s"] >= 1800


def test_ack_clears_the_episode(tmp_path):
    cfg = _cfg(tmp_path)
    wake = E.append(cfg, "runner_dead", data={"active": 0, "queued": 0})
    _conductor_cursor(cfg, 0, pending=wake["id"])
    E.sync(cfg, now=at(0), ping=True)
    E.sync(cfg, now=at(1800), ping=True)
    assert len(_cu_events(cfg)) == 1
    E.commit_ack(cfg, "conductor", wake["id"])
    E.sync(cfg, now=at(1900), ping=True)
    proj = json.loads((store.agentic_dir(cfg) / "events.projector.json").read_text())
    assert "conductor_unacked_since" not in proj
    assert "conductor_unresponsive_ping" not in proj
    n_before = len(_cu_events(cfg))
    E.sync(cfg, now=at(2500), ping=True)
    E.sync(cfg, now=at(4000), ping=True)
    assert len(_cu_events(cfg)) == n_before and len(_human_mail(cfg)) == 1
    wake2 = E.append(cfg, "runner_dead", data={"active": 0, "queued": 0})
    _conductor_cursor(cfg, wake["id"], pending=wake2["id"])
    E.sync(cfg, now=at(4100), ping=True)
    E.sync(cfg, now=at(6000), ping=True)
    assert len(_cu_events(cfg)) == 2 and len(_human_mail(cfg)) == 2


def test_summary_line_and_validate(tmp_path):
    cfg = _cfg(tmp_path)
    full = {"ping_id": 5, "cursor_after": 3, "unacked_s": 1800,
            "follower_pid": None, "relaunch": "nw queue conductor-launch"}
    with pytest.raises(ValueError, match="missing data keys"):
        E.append(cfg, "conductor_unresponsive", data={})
    for omit in full:
        partial = {k: v for k, v in full.items() if k != omit}
        with pytest.raises(ValueError, match="missing data keys"):
            E.validate("conductor_unresponsive", partial)
    ev_down = E.append(cfg, "conductor_unresponsive", by="watchdog", data=full)
    assert E.summary_line(ev_down) == (
        f"#{ev_down['id']} conductor_unresponsive unacked_s=1800 "
        "after=3 ping=5 follower=down")
    live = {**full, "follower_pid": 424242}
    ev_live = E.append(cfg, "conductor_unresponsive", by="watchdog", data=live)
    assert E.summary_line(ev_live) == (
        f"#{ev_live['id']} conductor_unresponsive unacked_s=1800 "
        "after=3 ping=5 follower=424242")
    E.validate("conductor_ping", {"blocked": [], "parked": [], "stuck": [], "waiting": [],
                                  "ready_not_admitted": [], "counts": {}, "runner": None})


def test_conductor_unresponsive_is_wake():
    assert QT.RULES["conductor_unresponsive"] == "wake"


def test_herdr_notification_for_conductor_unresponsive():
    ev = {"type": "conductor_unresponsive",
          "data": {"unacked_s": 1800, "relaunch": "nw queue conductor-launch --resume"}}
    title, body, sound = HW.notification_for(ev)
    assert title == "nw: conductor silent"
    assert "Stop the conductor session" in body
    assert "nw queue conductor-launch --resume" in body
    assert sound == "none"


def test_conduct_skill_row():
    from test_skills import _conduct_row
    row = _conduct_row("conductor_unresponsive")
    assert "data.relaunch" in row and "--rule conductor_unresponsive" in row
    assert "Do not spawn, stop or kill a session" in row


def test_agent_surface_lists_conductor_unresponsive():
    text = (Path(__file__).resolve().parents[1]
            / "docs/product/guides/agent-surface.md").read_text()
    assert "conductor_unresponsive" in text.split("nw.event.v1")[1].split("\n")[0]
