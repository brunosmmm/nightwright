"""SPEC-0171: `nw campaign list|show|log|note` over a tmp data dir."""
import json
from zoneinfo import ZoneInfo

from click.testing import CliRunner

from nightwright.agentic import store as S
from nightwright.cli import cli
from nightwright.agentic import schemas as SC

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
                            "repo": {"base": "main", "worktree": "/nonexistent/nightwright", "branch": "nw/x"},
                            "counters": {"loops": 1, "runs": 2}, "cost": {"total_usd": 1.5, "cap_usd": 10.0},
                            "park": None})
    S.create_campaign(cfg, {"id": "SPEC-0171-20261025-bbbb", "spec_id": "SPEC-0171", "project": "Meta-Tools",
                            "profile": "balanced", "state": "MERGED", "created_at": "2026-10-25T11:00:00-04:00",
                            "repo": {}, "counters": {}, "cost": {}})


def _run(cfg, monkeypatch, args, env=None):
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    return CliRunner().invoke(cli, args, env=env or {})


def test_list_json_and_all(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _seed(cfg)
    r = _run(cfg, monkeypatch, ["campaign", "list", "--json"])
    assert r.exit_code == 0, r.output
    d = json.loads(r.output)
    assert d["schema"] == "nw.campaigns.v1"
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
    assert json.loads(r.output) == {"schema": "nw.campaigns.v1", "campaigns": []}
    assert _run(cfg, monkeypatch, ["campaign", "list"]).exit_code == 0


def test_show_json_and_human(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _seed(cfg)
    _ev(cfg, "SPEC-0170-20261025-aaaa", "gate", {"ok": False})
    r = _run(cfg, monkeypatch, ["campaign", "show", "SPEC-0170-20261025-aaaa", "--json"])
    assert r.exit_code == 0, r.output
    d = json.loads(r.output)
    assert d["schema"] == "nw.campaign.v1" and d["state"] == "GATING"
    assert [e["type"] for e in d["last_events"]] == ["open", "gate"]
    assert d["worktree_present"] is False
    r = _run(cfg, monkeypatch, ["campaign", "show", "SPEC-0170-20261025-aaaa"])
    assert r.exit_code == 0 and "GATING" in r.output and "(missing)" in r.output
    assert "review:" not in r.output
    st = S.read_state(cfg, "SPEC-0170-20261025-aaaa")
    st["last_review"] = {"verdict": "pass", "score": 85, "findings": 2, "must_fix": 0,
                         "advisory": [{"id": "a#0", "severity": "major"},
                                      {"id": "a#1", "severity": "minor"}]}
    S.write_state(cfg, "SPEC-0170-20261025-aaaa", st)
    r = _run(cfg, monkeypatch, ["campaign", "show", "SPEC-0170-20261025-aaaa"])
    assert "review: score 85  pass  advisory 2 (1 major)" in r.output
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
    assert d["schema"] == "nw.campaign.log.v1" and d["campaign"] == cid
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
             env={"NW_CAMPAIGN": cid})
    assert r.exit_code == 0, r.output
    events = S.read_ledger(cfg, cid, types=["note"])
    assert [(e["text"], e["by"]) for e in events] == [("checked the fixture", "human"),
                                                      ("from inside a run", "agent:implementer")]
    monkeypatch.delenv("NW_CAMPAIGN", raising=False)
    r = _run(cfg, monkeypatch, ["campaign", "note", "orphan text"])
    assert r.exit_code != 0 and "set NW_CAMPAIGN" in r.output
    r = _run(cfg, monkeypatch, ["campaign", "note", "ghost text"], env={"NW_CAMPAIGN": "nope"})
    assert r.exit_code != 0 and "no campaign 'nope'" in r.output


def test_show_prints_sizing_and_tolerates_pre_sizing(tmp_path, monkeypatch):     # SPEC-0219
    cfg = _cfg(tmp_path)
    _seed(cfg)
    S.create_campaign(cfg, {"id": "SPEC-0172-20261025-cccc", "spec_id": "SPEC-0172",
                            "project": "Meta-Tools", "profile": "thorough", "state": "QUEUED",
                            "repo": {}, "counters": {}, "cost": {},
                            "sizing": {"size": "l", "tone": "high-end", "source": "review",
                                       "reason": "size l, tone high-end → thorough",
                                       "rationale": "10 ACs, state machine"}})
    r = _run(cfg, monkeypatch, ["campaign", "show", "SPEC-0172-20261025-cccc"])
    assert r.exit_code == 0, r.output
    assert "size l  tone high-end  profile thorough (review)" in r.output
    assert "sizing: size l, tone high-end → thorough — 10 ACs, state machine" in r.output
    r = _run(cfg, monkeypatch, ["campaign", "show", "SPEC-0172-20261025-cccc", "--json"])
    assert json.loads(r.output)["sizing"]["size"] == "l"
    r = _run(cfg, monkeypatch, ["campaign", "show", "SPEC-0170-20261025-aaaa"])
    assert r.exit_code == 0 and "sizing:" not in r.output


def test_log_renders_rebase_resolved(tmp_path, monkeypatch):                       # SPEC-0221
    cfg = _cfg(tmp_path)
    _seed(cfg)
    cid = "SPEC-0170-20261025-aaaa"
    _ev(cfg, cid, "rebase_resolved", {"conflicts": ["src/app.py"], "summary": "s",
                                      "resolutions": [{"path": "src/app.py", "strategy": "both",
                                                       "how": "kept both"}]})
    r = _run(cfg, monkeypatch, ["campaign", "log", cid, "--tail", "1"])
    assert r.exit_code == 0, r.output
    assert "rebase resolved: 1 paths (both×1)" in r.output


def test_show_prints_profile_sources_and_tolerates_old_campaigns(tmp_path, monkeypatch):   # SPEC-0238
    cfg = _cfg(tmp_path)
    _seed(cfg)
    S.create_campaign(cfg, {"id": "SPEC-0173-20261025-dddd", "spec_id": "SPEC-0173",
                            "project": "Meta-Tools", "profile": "balanced", "state": "QUEUED",
                            "repo": {}, "counters": {}, "cost": {},
                            "profile_sources": ["builtin:balanced.yaml",
                                                "config.yaml:agentic.profiles.balanced"]})
    r = _run(cfg, monkeypatch, ["campaign", "show", "SPEC-0173-20261025-dddd"])
    assert r.exit_code == 0, r.output
    assert "profile balanced (builtin + config.yaml)" in r.output
    r = _run(cfg, monkeypatch, ["campaign", "show", "SPEC-0170-20261025-aaaa"])
    assert r.exit_code == 0 and "profile balanced" in r.output and "profile balanced (" not in r.output
    d = json.loads(_run(cfg, monkeypatch, ["campaign", "show", "SPEC-0173-20261025-dddd",
                                           "--json"]).output)
    assert d["profile_sources"][0] == "builtin:balanced.yaml"


def test_show_lists_split_parts(tmp_path, monkeypatch):               # SPEC-0290
    cfg = _cfg(tmp_path)
    parts = [{"title": "First half", "scope": "a", "number": 101, "rel_path": "x", "spec_id": "SPEC-0101"},
             {"title": "Second half", "scope": "b", "number": 102, "rel_path": "y", "spec_id": "SPEC-0102"},
             {"title": "Rest", "scope": "c", "number": None, "rel_path": None, "spec_id": None}]
    S.create_campaign(cfg, {"id": "SPEC-0005-20261030-cccc", "kind": "split", "spec_id": "SPEC-0005",
                            "project": "Meta-Tools", "profile": "balanced", "state": "SPEC_WRITING",
                            "created_at": "2026-10-30T10:00:00-04:00", "repo": {}, "counters": {},
                            "cost": {}, "split": {"original": "SPEC-0005", "original_rel": "a.md",
                                                  "review_sha": "x", "parts": parts}})
    r = _run(cfg, monkeypatch, ["campaign", "show", "SPEC-0005-20261030-cccc"])
    assert r.exit_code == 0, r.output
    assert "split of SPEC-0005 into 3 parts" in r.output
    assert "part 1: SPEC-0101 First half" in r.output
    assert "part 2: SPEC-0102 Second half" in r.output
    assert "part 3: (pending) Rest" in r.output


# ---- SPEC-0425: note attribution -------------------------------------------------------------

def _register(cfg, reader, sid):
    from nightwright.agentic import conductor_cost as CC
    CC.record_session(cfg, reader, {"CLAUDE_CODE_SESSION_ID": sid}, source="manual")


def test_note_actor_matrix(tmp_path, monkeypatch):
    from nightwright.agentic.cli_campaign import note_actor
    monkeypatch.setattr("nightwright.rules.projects_payload",
                        lambda cfg: {"projects": [{"name": "Meta-Tools"}]})
    cfg = _cfg(tmp_path)
    _register(cfg, "conductor", "sid-c")
    _register(cfg, "steward:Meta-Tools", "sid-s")
    _register(cfg, "steward", "sid-bad")              # not an address: ignored
    cases = [
        ({}, "x", ("agent:x", "x")),
        ({"NW_PERSONA": "implementer", "NW_PERSONA_RUN": "1", "CLAUDECODE": "1"}, None,
         ("agent:implementer", "implementer")),
        ({"NW_PERSONA_RUN": "1"}, None, ("agent:persona", "persona")),
        ({"NW_MAILBOX_ADDR": "conductor", "CLAUDECODE": "1"}, None, ("conductor", "conductor")),
        ({"NW_MAILBOX_ADDR": "nobody", "CLAUDECODE": "1"}, None, ("agent:session", "session")),
        ({"CLAUDE_CODE_SESSION_ID": "sid-s", "CLAUDECODE": "1"}, None,
         ("steward:Meta-Tools", "steward:Meta-Tools")),
        ({"CLAUDE_CODE_SESSION_ID": "sid-c", "CLAUDECODE": "1"}, None, ("conductor", "conductor")),
        ({"CLAUDE_CODE_SESSION_ID": "sid-bad", "CLAUDECODE": "1"}, None,
         ("agent:session", "session")),
        ({"CLAUDE_CODE_SESSION_ID": "unknown", "CLAUDECODE": "1"}, None,
         ("agent:session", "session")),
        ({}, None, ("human", "human")),
    ]
    for env, author, want in cases:
        assert note_actor(cfg, env, author) == want, (env, author)


def test_note_cli_attribution(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _seed(cfg)
    cid = "SPEC-0170-20261025-aaaa"
    for env in ({"NW_PERSONA": "implementer", "NW_PERSONA_RUN": "1"},
                {"NW_MAILBOX_ADDR": "conductor"}, {"CLAUDECODE": "1"}, {}):
        r = _run(cfg, monkeypatch, ["campaign", "note", cid, "x"], env=env)
        assert r.exit_code == 0, r.output
        for k in env:
            monkeypatch.delenv(k, raising=False)
    notes = S.read_ledger(cfg, cid, types=["note"])
    assert [(e["by"], e["author"]) for e in notes] == [
        ("agent:implementer", "implementer"), ("conductor", "conductor"),
        ("agent:session", "session"), ("human", "human")]


def test_show_prints_the_story_by_default_raw_on_request_and_both_in_json(tmp_path, monkeypatch):
    """SPEC-0433: `nw campaign show` tells the story; `--raw` is the old last-events view."""
    cfg = _cfg(tmp_path)
    _seed(cfg)
    cid = "SPEC-0170-20261025-aaaa"
    _ev(cfg, cid, "transition", {"from": "QUEUED", "to": "GATING", "reason": "x"})
    _ev(cfg, cid, "gate.reuse_declined", {"gate": "pytest", "rule": "R3", "detail": "differs"})
    _ev(cfg, cid, "gate", {"gate_seq": 3, "ok": False, "duration_s": 2431, "worker": "box",
                           "results": [{"name": "pytest", "ok": False, "required": True,
                                        "exit_code": 1, "duration_s": 2400}]})
    r = _run(cfg, monkeypatch, ["campaign", "show", cid], env={"COLUMNS": "200"})
    assert r.exit_code == 0, r.output
    assert "Loop 1 · " in r.output and "Gating · GATING · " in r.output
    assert "Gate #3 on box — FAILED after 40m 31s (pytest)  [+2]" in r.output
    assert "reuse_declined" not in r.output and "rule=R3" not in r.output
    x = _run(cfg, monkeypatch, ["campaign", "show", cid, "-x"], env={"COLUMNS": "200"}).output
    assert "pytest: reuse declined (R3: differs)" in x and "✗ pytest (40m, exit 1)" in x
    raw = _run(cfg, monkeypatch, ["campaign", "show", cid, "--raw"], env={"COLUMNS": "200"})
    assert "gate.reuse_declined" in raw.output and "rule=R3" in raw.output
    assert "Loop 1 · " not in raw.output
    d = json.loads(_run(cfg, monkeypatch, ["campaign", "show", cid, "--json"]).output)
    assert d["schema"] == "nw.campaign.v1" and d["last_events"][-1]["type"] == "gate"
    assert d["story"]["schema"] == "nw.campaign.story.v1" and d["story"]["events"] == 4
