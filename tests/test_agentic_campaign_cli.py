"""SPEC-0171: `wt campaign list|show|log|note` over a tmp data dir."""
import json
from zoneinfo import ZoneInfo

from click.testing import CliRunner

from wt.agentic import store as S
from wt.cli import cli
from wt.agentic import schemas as SC

def _ev(cfg, cid, etype, payload=None, **kw):
    """append_event with the schema's required keys defaulted (SPEC-0192 fixtures)."""
    return S.append_event(cfg, cid, etype, SC.with_defaults(etype, payload), **kw)


def _cfg(tmp_path):
    org = tmp_path / "org"
    org.mkdir()
    return {
        "org_files": [str(org)],
        "org_todo_keywords": ["TODO", "|", "DONE"],
        "org_idea_keywords": ["IDEA", "INCUBATE", "SPECCED", "|", "PROMOTED", "DROPPED"],
        "timezone": "America/New_York",
        "_tz": ZoneInfo("America/New_York"),
        "data_dir": str(tmp_path / "data"),
        "config_dir": str(tmp_path / "cfg"),
        "org_capture_file": str(org / "inbox.org"),
        "org_ideas_file": str(org / "ideas.org"),
        "specs_dir": str(tmp_path / "specs"),
        "agentic": {"profiles": {}, "lease_ttl_s": 120},
    }


def _seed(cfg):
    S.create_campaign(cfg, {"id": "SPEC-0170-20261025-aaaa", "spec_id": "SPEC-0170", "project": "Meta-Tools",
                            "profile": "balanced", "state": "GATING", "created_at": "2026-10-25T10:00:00-04:00",
                            "repo": {"base": "main", "worktree": "/nonexistent/wt", "branch": "wt/x"},
                            "counters": {"loops": 1, "runs": 2}, "cost": {"total_usd": 1.5, "cap_usd": 10.0},
                            "park": None})
    S.create_campaign(cfg, {"id": "SPEC-0171-20261025-bbbb", "spec_id": "SPEC-0171", "project": "Meta-Tools",
                            "profile": "balanced", "state": "MERGED", "created_at": "2026-10-25T11:00:00-04:00",
                            "repo": {}, "counters": {}, "cost": {}})


def _run(cfg, monkeypatch, args, env=None):
    monkeypatch.setattr("wt.cli.load_config", lambda: cfg)
    return CliRunner().invoke(cli, args, env=env or {})


def test_list_json_and_all(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _seed(cfg)
    r = _run(cfg, monkeypatch, ["campaign", "list", "--json"])
    assert r.exit_code == 0, r.output
    d = json.loads(r.output)
    assert d["schema"] == "wt.campaigns.v1"
    assert [c["id"] for c in d["campaigns"]] == ["SPEC-0170-20261025-aaaa"]
    assert d["campaigns"][0]["loops"] == 1 and d["campaigns"][0]["cost_usd"] == 1.5
    r = _run(cfg, monkeypatch, ["campaign", "list", "--all", "--json"])
    assert len(json.loads(r.output)["campaigns"]) == 2
    r = _run(cfg, monkeypatch, ["campaign", "list", "--all", "--spec", "SPEC-0171", "--json"])
    assert [c["id"] for c in json.loads(r.output)["campaigns"]] == ["SPEC-0171-20261025-bbbb"]
    r = _run(cfg, monkeypatch, ["campaign", "list"])
    assert r.exit_code == 0 and "GATING" in r.output


def test_list_empty_data_dir(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    r = _run(cfg, monkeypatch, ["campaign", "list", "--json"])
    assert json.loads(r.output) == {"schema": "wt.campaigns.v1", "campaigns": []}
    assert _run(cfg, monkeypatch, ["campaign", "list"]).exit_code == 0


def test_show_json_and_human(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _seed(cfg)
    _ev(cfg, "SPEC-0170-20261025-aaaa", "gate", {"ok": False})
    r = _run(cfg, monkeypatch, ["campaign", "show", "SPEC-0170-20261025-aaaa", "--json"])
    assert r.exit_code == 0, r.output
    d = json.loads(r.output)
    assert d["schema"] == "wt.campaign.v1" and d["state"] == "GATING"
    assert [e["type"] for e in d["last_events"]] == ["open", "gate"]
    assert d["worktree_present"] is False
    r = _run(cfg, monkeypatch, ["campaign", "show", "SPEC-0170-20261025-aaaa"])
    assert r.exit_code == 0 and "GATING" in r.output and "(missing)" in r.output
    r = _run(cfg, monkeypatch, ["campaign", "show", "nope"])
    assert r.exit_code != 0 and "no campaign 'nope'" in r.output


def test_log_filters_and_json(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _seed(cfg)
    cid = "SPEC-0170-20261025-aaaa"
    _ev(cfg, cid, "transition", {"from": "QUEUED", "to": "OPENING"})
    _ev(cfg, cid, "gate", {"ok": True})
    r = _run(cfg, monkeypatch, ["campaign", "log", cid, "--json"])
    d = json.loads(r.output)
    assert d["schema"] == "wt.campaign.log.v1" and d["campaign"] == cid
    assert [e["type"] for e in d["events"]] == ["open", "transition", "gate"]
    r = _run(cfg, monkeypatch, ["campaign", "log", cid, "--type", "gate", "--json"])
    assert [e["type"] for e in json.loads(r.output)["events"]] == ["gate"]
    r = _run(cfg, monkeypatch, ["campaign", "log", cid, "--tail", "1"])
    assert r.exit_code == 0 and "gate" in r.output and "transition" not in r.output


def test_note_with_argument_env_and_usage_error(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _seed(cfg)
    cid = "SPEC-0170-20261025-aaaa"
    r = _run(cfg, monkeypatch, ["campaign", "note", cid, "checked", "the", "fixture"])
    assert r.exit_code == 0, r.output
    r = _run(cfg, monkeypatch, ["campaign", "note", "from inside a run", "--author", "implementer"],
             env={"WT_CAMPAIGN": cid})
    assert r.exit_code == 0, r.output
    events = S.read_ledger(cfg, cid, types=["note"])
    assert [(e["text"], e["by"]) for e in events] == [("checked the fixture", "human"),
                                                      ("from inside a run", "agent:implementer")]
    monkeypatch.delenv("WT_CAMPAIGN", raising=False)
    r = _run(cfg, monkeypatch, ["campaign", "note", "orphan text"])
    assert r.exit_code != 0 and "set WT_CAMPAIGN" in r.output
    r = _run(cfg, monkeypatch, ["campaign", "note", "ghost text"], env={"WT_CAMPAIGN": "nope"})
    assert r.exit_code != 0 and "no campaign 'nope'" in r.output
