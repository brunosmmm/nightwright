"""SPEC-0176: `wt queue add|list|park|resume|abandon` — refusals, dry-run defaults, `--json`
envelopes. Repos come from the e2e harness; nothing runs a persona here."""
import json
import subprocess

from test_agentic_e2e import (SPEC_ACCEPTED, added, impl_done, make_cfg, make_repo, run_cli, script,
                              write_review)
from wt.agentic import store as S


def _cfg(tmp_path, **kw):
    repo = make_repo(tmp_path, **kw)
    return repo, make_cfg(tmp_path, repo, script(tmp_path, [impl_done()]))


def test_add_dry_run_writes_nothing_and_json_envelope(tmp_path, monkeypatch):
    repo, cfg = _cfg(tmp_path)
    r = run_cli(cfg, monkeypatch, ["queue", "add", "SPEC-0001"])
    assert r.exit_code == 0 and "not written" in r.output, r.output
    assert not S.list_campaigns(cfg)
    r = run_cli(cfg, monkeypatch, ["queue", "add", "SPEC-0001", "--json"])
    d = json.loads(r.output)
    assert d["schema"] == "wt.queue.add.v1" and d["written"] is False
    assert d["project"] == "Meta-Tools" and d["profile"] == "balanced" and d["base"] == "main"
    assert d["spec_rel_path"] == "docs/specs/0001-trivial.md" and d["branch"].startswith("wt/SPEC-0001-")
    assert d["worktree"].endswith(f"/.wt-worktrees/Meta-Tools/{d['campaign']}")
    assert d["merge_policy"] == "merge" and d["cap_usd"] == 10.0


def test_add_refusals(tmp_path, monkeypatch):
    repo, cfg = _cfg(tmp_path)
    r = run_cli(cfg, monkeypatch, ["queue", "add", "SPEC-0099", "--yes"])
    assert r.exit_code != 0 and "SPEC-0099" in r.output
    r = run_cli(cfg, monkeypatch, ["queue", "add", "SPEC-0001", "--project", "nope", "--yes"])
    assert r.exit_code != 0 and "nope" in r.output
    r = run_cli(cfg, monkeypatch, ["queue", "add", "SPEC-0001", "--profile", "bogus", "--yes"])
    assert r.exit_code != 0 and "bogus" in r.output
    cid = added(cfg, monkeypatch)
    r = run_cli(cfg, monkeypatch, ["queue", "add", "SPEC-0001", "--yes"])
    assert r.exit_code != 0 and cid in r.output and "abandon" in r.output
    assert len(S.list_campaigns(cfg)) == 1


def test_add_requires_accepted_unless_forced(tmp_path, monkeypatch):
    repo, cfg = _cfg(tmp_path)
    spec = repo / "docs" / "specs" / "0001-trivial.md"
    spec.write_text(spec.read_text().replace("status: accepted", "status: in-progress"))
    r = run_cli(cfg, monkeypatch, ["queue", "add", "SPEC-0001", "--yes"])
    assert r.exit_code != 0 and "in-progress" in r.output and "--force" in r.output
    r = run_cli(cfg, monkeypatch, ["queue", "add", "SPEC-0001", "--yes", "--force", "--json"])
    assert r.exit_code == 0 and json.loads(r.output)["spec_status"] == "in-progress"
    spec.write_text(spec.read_text().replace("status: in-progress", "status: draft"))
    r = run_cli(cfg, monkeypatch, ["queue", "add", "SPEC-0001", "--yes", "--force"])
    assert r.exit_code != 0


def test_add_spec_outside_repo_is_refused(tmp_path, monkeypatch):
    repo, cfg = _cfg(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "0001-trivial.md").write_text(SPEC_ACCEPTED.format(id="SPEC-0001", extra=""))
    cfg["specs_dir"] = str(elsewhere)
    r = run_cli(cfg, monkeypatch, ["queue", "add", "SPEC-0001", "--yes"])
    assert r.exit_code != 0 and "not inside the project repo" in r.output


def test_list_json_shape_and_filters(tmp_path, monkeypatch):
    repo, cfg = _cfg(tmp_path)
    cid = added(cfg, monkeypatch)
    r = run_cli(cfg, monkeypatch, ["queue", "list", "--json"])
    d = json.loads(r.output)
    assert d["schema"] == "wt.queue.v1"
    assert set(d["lease"]) >= {"held", "pid", "heartbeat_age_s", "ttl_s", "stale"}
    assert d["slots"] == {"total": 2, "busy": 0}
    item = d["items"][0]
    assert item["campaign"] == cid and item["state"] == "QUEUED" and item["loops"] == "0/3"
    assert item["cost_usd"] == 0.0 and item["cap_usd"] == 10.0 and item["park"] is None
    assert item["review_score"] is None
    st = S.read_state(cfg, cid)
    st["last_review"] = {"verdict": "fix", "score": 72}
    S.write_state(cfg, cid, st)
    assert json.loads(run_cli(cfg, monkeypatch, ["queue", "list", "--json"]).output)[
        "items"][0]["review_score"] == 72
    r = run_cli(cfg, monkeypatch, ["queue", "list", "--state", "MERGED", "--json"])
    assert json.loads(r.output)["items"] == []
    r = run_cli(cfg, monkeypatch, ["queue", "list", "--project", "Meta-Tools"])
    assert r.exit_code == 0 and cid in r.output and "no loop running" in r.output
    assert "Active (0/2)" in r.output and "Backlog (1)" in r.output and "ready" in r.output


def test_park_resume_abandon_dry_runs_and_errors(tmp_path, monkeypatch):
    repo, cfg = _cfg(tmp_path)
    cid = added(cfg, monkeypatch)
    # park a QUEUED item immediately
    r = run_cli(cfg, monkeypatch, ["queue", "park", cid, "--reason", "hold"])
    assert r.exit_code == 0 and "not written" in r.output and S.read_state(cfg, cid)["state"] == "QUEUED"
    r = run_cli(cfg, monkeypatch, ["queue", "park", cid, "--reason", "hold", "--yes"])
    assert r.exit_code == 0 and S.read_state(cfg, cid)["state"] == "PARKED"
    assert S.read_state(cfg, cid)["park"]["resume_to"] == "QUEUED"
    r = run_cli(cfg, monkeypatch, ["queue", "park", cid, "--reason", "again", "--yes"])
    assert r.exit_code != 0 and "nothing to park" in r.output
    # resume: dry-run, bad --to, note + reset
    r = run_cli(cfg, monkeypatch, ["queue", "resume", cid, "--json"])
    d = json.loads(r.output)
    assert d["schema"] == "wt.queue.resume.v1" and d["to"] == "QUEUED" and d["written"] is False
    assert S.read_state(cfg, cid)["state"] == "PARKED"
    r = run_cli(cfg, monkeypatch, ["queue", "resume", cid, "--to", "MERGED", "--yes"])
    assert r.exit_code != 0
    r = run_cli(cfg, monkeypatch, ["queue", "resume", cid, "--to", "IMPLEMENTING", "--note", "try x",
                                   "--reset-loops", "--yes"])
    assert r.exit_code == 0, r.output
    st = S.read_state(cfg, cid)
    assert st["state"] == "IMPLEMENTING" and st["pending_brief"]["human_notes"] == ["try x"]
    ev = S.read_ledger(cfg, cid, types=["resume"])[-1]
    assert ev["to"] == "IMPLEMENTING" and ev["note"] == "try x" and ev["loops_reset"] is True
    r = run_cli(cfg, monkeypatch, ["queue", "resume", cid, "--yes"])
    assert r.exit_code != 0 and "not PARKED" in r.output
    # abandon only PARKED / QUEUED
    r = run_cli(cfg, monkeypatch, ["queue", "abandon", cid, "--yes"])
    assert r.exit_code != 0 and "park it first" in r.output
    run_cli(cfg, monkeypatch, ["queue", "park", cid, "--reason", "drop", "--yes"])
    r = run_cli(cfg, monkeypatch, ["queue", "abandon", cid, "--keep-worktree", "--json"])
    d = json.loads(r.output)
    assert d["schema"] == "wt.queue.abandon.v1" and d["written"] is False and d["remove_worktree"] is False
    r = run_cli(cfg, monkeypatch, ["queue", "abandon", cid, "--yes"])
    assert r.exit_code == 0 and S.read_state(cfg, cid)["state"] == "ABANDONED"
    assert S.read_ledger(cfg, cid, types=["close"])[-1]["mode"] == "abandoned"


def test_queue_help_lists_every_command():
    r = run_cli({"agentic": {}, "data_dir": "/nonexistent", "config_dir": "/nonexistent"},
                __import__("pytest").MonkeyPatch(), ["queue", "--help"])
    for name in ("add", "list", "backlog", "prioritize", "watch", "tick", "run", "park", "resume",
                 "abandon", "gates"):
        assert f"  {name}" in r.output, name


def test_second_spec_commit_helper_sanity(tmp_path):
    """The harness repo is a real git repo on `main` with a clean tree."""
    repo = make_repo(tmp_path)
    out = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"], capture_output=True,
                         text=True).stdout
    assert out == ""


# ---- SPEC-0180: priority, prioritize, backlog ---------------------------------------------------

def test_add_priority_plan_state_and_validation(tmp_path, monkeypatch):
    repo, cfg = _cfg(tmp_path)
    r = run_cli(cfg, monkeypatch, ["queue", "add", "SPEC-0001", "--priority", "2", "--json"])
    d = json.loads(r.output)
    assert d["priority"] == 2 and d["written"] is False
    for bad in ("0", "6", "-1"):
        r = run_cli(cfg, monkeypatch, ["queue", "add", "SPEC-0001", "--priority", bad, "--yes"])
        assert r.exit_code != 0 and "priority must be" in r.output, bad
    assert not S.list_campaigns(cfg)
    cid = added(cfg, monkeypatch, "SPEC-0001", "--priority", "1")
    assert S.read_state(cfg, cid)["priority"] == 1
    r = run_cli(cfg, monkeypatch, ["queue", "add", "SPEC-0001", "--yes", "--priority", "x"])
    assert r.exit_code != 0


def test_prioritize_dry_run_apply_ledger_and_errors(tmp_path, monkeypatch):
    repo, cfg = _cfg(tmp_path)
    cid = added(cfg, monkeypatch)
    assert S.read_state(cfg, cid)["priority"] == 3
    r = run_cli(cfg, monkeypatch, ["queue", "prioritize", cid, "1", "--json"])
    d = json.loads(r.output)
    assert d["schema"] == "wt.queue.prioritize.v1" and d["written"] is False
    assert (d["from"], d["to"]) == (3, 1) and S.read_state(cfg, cid)["priority"] == 3
    r = run_cli(cfg, monkeypatch, ["queue", "prioritize", cid, "1", "--yes"])
    assert r.exit_code == 0, r.output
    assert S.read_state(cfg, cid)["priority"] == 1
    ev = S.read_ledger(cfg, cid, types=["priority"])
    assert [(e["from"], e["to"]) for e in ev] == [(3, 1)]
    for bad in ("0", "6"):
        r = run_cli(cfg, monkeypatch, ["queue", "prioritize", cid, bad, "--yes"])
        assert r.exit_code != 0 and "priority must be" in r.output
    run_cli(cfg, monkeypatch, ["queue", "park", cid, "--reason", "hold", "--yes"])
    r = run_cli(cfg, monkeypatch, ["queue", "prioritize", cid, "4", "--yes"])      # PARKED is ok
    assert r.exit_code == 0 and S.read_state(cfg, cid)["priority"] == 4
    st = S.read_state(cfg, cid)
    st["state"] = "IMPLEMENTING"
    S.write_state(cfg, cid, st)
    r = run_cli(cfg, monkeypatch, ["queue", "prioritize", cid, "2", "--yes"])
    assert r.exit_code != 0 and "only QUEUED, WAITING or PARKED" in r.output


def test_backlog_json_order_and_list_sections(tmp_path, monkeypatch):
    repo, cfg = _cfg(tmp_path)
    (repo / "docs" / "specs" / "0002-second.md").write_text(
        SPEC_ACCEPTED.format(id="SPEC-0002", extra="depends_on: [SPEC-0001, SPEC-0099]\n"))
    write_review(repo / "docs" / "specs" / "0002-second.md", "SPEC-0002")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "second"], check=True)
    first = added(cfg, monkeypatch, "SPEC-0001")
    second = added(cfg, monkeypatch, "SPEC-0002", "--priority", "1")
    r = run_cli(cfg, monkeypatch, ["queue", "backlog", "--json"])
    d = json.loads(r.output)
    assert d["schema"] == "wt.queue.backlog.v1"
    assert [i["campaign"] for i in d["items"]] == [second, first]
    top = d["items"][0]
    assert set(top) == {"campaign", "spec_id", "project", "priority", "position", "ready",
                        "blocked_by", "age_s"}
    assert top["position"] == 1 and top["priority"] == 1 and top["ready"] is False
    assert top["blocked_by"] == ["waiting on SPEC-0001 (accepted)", "SPEC-0099 not found"]
    assert d["items"][1]["ready"] is True and d["items"][1]["blocked_by"] == []
    r = run_cli(cfg, monkeypatch, ["queue", "backlog"])
    assert r.exit_code == 0 and "Backlog (2)" in r.output and "SPEC-0099" in r.output
    r = run_cli(cfg, monkeypatch, ["queue", "list", "--json"])
    items = {i["campaign"]: i for i in json.loads(r.output)["items"]}
    assert items[second]["backlog"] == {"position": 1, "priority": 1, "ready": False,
                                        "blocked_by": top["blocked_by"]}
    assert items[first]["backlog"]["position"] == 2 and items[first]["backlog"]["ready"] is True
