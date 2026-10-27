"""SPEC-0176: `wt queue add|list|park|resume|abandon` — refusals, dry-run defaults, `--json`
envelopes. Repos come from the e2e harness; nothing runs a persona here."""
import json
import subprocess

from test_agentic_e2e import (SPEC_ACCEPTED, added, impl_done, make_cfg, make_repo, run_cli, script,
                              write_review)
from wt import org_write as OW
from wt.agentic import queue as Q
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
    write_review(cfg, repo / "docs" / "specs" / "0002-second.md", "SPEC-0002")
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


# ---- sizing and tone pick the profile (SPEC-0219) ----------------------------------------------

SIZED = {"size": "s", "tone": "cost-effective", "rationale": "2 ACs, one module."}


def _sized(tmp_path, **extra):
    repo, cfg = _cfg(tmp_path, review=False)
    write_review(cfg, repo / "docs" / "specs" / "0001-trivial.md", "SPEC-0001", **(extra or SIZED))
    return repo, cfg


def _plan(cfg, monkeypatch, *args):
    r = run_cli(cfg, monkeypatch, ["queue", "add", "SPEC-0001", "--json", *args])
    assert r.exit_code == 0, r.output
    return json.loads(r.output)


def test_add_picks_profile_from_sized_review(tmp_path, monkeypatch):
    repo, cfg = _sized(tmp_path)
    d = _plan(cfg, monkeypatch)
    assert d["profile"] == "frugal" and d["profile_source"] == "review"
    assert d["profile_reason"] == "size s, tone cost-effective → frugal"
    assert (d["size"], d["tone"], d["sizing_rationale"]) == ("s", "cost-effective",
                                                              SIZED["rationale"])
    assert d["cap_usd"] == 3.0
    r = run_cli(cfg, monkeypatch, ["queue", "add", "SPEC-0001"])
    assert "profile_source: review" in r.output and "size: s" in r.output


def test_add_records_sizing_on_state_and_open_event(tmp_path, monkeypatch):
    from wt.agentic import schemas as SC
    repo, cfg = _sized(tmp_path)
    cid = added(cfg, monkeypatch)
    st = S.read_state(cfg, cid)
    assert st["profile"] == "frugal"
    from wt.agentic import spec_review as SR
    sha = SR.spec_sha256(repo / "docs/specs/0001-trivial.md")
    assert st["sizing"] == {"size": "s", "tone": "cost-effective", "rationale": SIZED["rationale"],
                            "source": "review", "reason": "size s, tone cost-effective → frugal",
                            "review_sha256": sha}
    ev = S.read_ledger(cfg, cid)[0]
    assert ev["type"] == "open" and SC.ledger_problems(ev) == []
    assert (ev["size"], ev["tone"], ev["profile_source"]) == ("s", "cost-effective", "review")
    assert ev["profile_reason"] == "size s, tone cost-effective → frugal"


def test_add_flag_wins_but_still_records_sizing(tmp_path, monkeypatch):
    repo, cfg = _sized(tmp_path)
    cid = added(cfg, monkeypatch, "SPEC-0001", "--profile", "thorough")
    st = S.read_state(cfg, cid)
    assert st["profile"] == "thorough"
    assert st["sizing"]["source"] == "flag" and st["sizing"]["reason"] == "--profile thorough"
    assert (st["sizing"]["size"], st["sizing"]["tone"]) == ("s", "cost-effective")


def test_add_old_sidecar_and_force_use_default_profile(tmp_path, monkeypatch):
    repo, cfg = _cfg(tmp_path)                       # write_review without size/tone
    d = _plan(cfg, monkeypatch)
    assert (d["profile"], d["profile_source"]) == ("balanced", "default")
    assert d["profile_reason"] == "review predates sizing (no size/tone)"
    assert d["size"] is None and d["tone"] is None
    write_review(cfg, repo / "docs" / "specs" / "0001-trivial.md", "SPEC-0001", **SIZED)
    d = _plan(cfg, monkeypatch, "--force")           # a forced add never trusts the review
    assert (d["profile"], d["profile_source"]) == ("balanced", "default")
    assert d["profile_reason"] == "no approving review (--force)" and d["size"] is None


def test_matrix_config_override_reaches_add(tmp_path, monkeypatch):
    repo, cfg = _sized(tmp_path)
    cfg["agentic"]["profile_matrix"] = {"cost-effective": {"s": "thorough"}}
    assert _plan(cfg, monkeypatch)["profile"] == "thorough"


def test_queue_list_json_and_table_show_sizing(tmp_path, monkeypatch):
    from wt.agentic import cli_queue as CQ
    from wt.agentic import queue as Q
    repo, cfg = _sized(tmp_path)
    sized = added(cfg, monkeypatch)
    S.create_campaign(cfg, {"id": "SPEC-0002-old", "spec_id": "SPEC-0002", "project": "Meta-Tools",
                            "profile": "balanced", "state": "IMPLEMENTING"})   # pre-sizing
    r = run_cli(cfg, monkeypatch, ["queue", "list", "--json"])
    items = {it["campaign"]: it for it in json.loads(r.output)["items"]}
    assert (items[sized]["size"], items[sized]["tone"], items[sized]["profile"],
            items[sized]["profile_source"]) == ("s", "cost-effective", "frugal", "review")
    old = items["SPEC-0002-old"]
    assert (old["size"], old["tone"], old["profile_source"]) == (None, None, None)
    assert old["profile"] == "balanced"
    # wide table: columns; the source marker shows only when the review did not pick
    payload = Q.queue_payload(cfg)
    for it in payload["items"]:
        it["state"] = "IMPLEMENTING"
    next(it for it in payload["items"] if it["campaign"] == sized)["profile_source"] = "flag"
    out = _render(CQ._queue_table(payload, 140), 140)
    for col in ("size", "tone", "profile"):
        assert col in out
    assert "cost-effective" in out and "(flag)" in out
    old_row = next(line for line in out.splitlines() if "SPEC-0002-old" in line)
    assert " -  " in old_row and "(" not in old_row
    # narrow table keeps the columns it had; the sizing rides under the campaign id
    narrow = _render(CQ._queue_table(payload, 80), 80)
    assert "s/cost-effective → frugal (flag)" in narrow and max(map(len, narrow.splitlines())) <= 80


def _render(renderable, width):
    from rich.console import Console
    c = Console(width=width, record=True, force_terminal=False)
    c.print(renderable)
    return c.export_text()


def test_add_after_revision_dry_run_text_and_json(tmp_path, monkeypatch):      # SPEC-0230
    repo = make_repo(tmp_path, review=False)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, [impl_done()]))
    spec = repo / "docs" / "specs" / "0001-trivial.md"
    findings = [{"severity": "major", "title": "gap"}, {"severity": "nit", "title": "word"}]
    write_review(cfg, spec, "SPEC-0001", "revise", sha="f" * 64, findings=findings)
    r = run_cli(cfg, monkeypatch, ["queue", "add", "SPEC-0001"])
    assert r.exit_code == 0, r.output
    assert "revision:" in r.output and "ffffffffffff" in r.output and "2 findings" in r.output
    assert "after_revision" in r.output and "not written" in r.output
    r = run_cli(cfg, monkeypatch, ["queue", "add", "SPEC-0001", "--json"])
    d = json.loads(r.output)
    assert d["review"] == "after_revision" and d["written"] is False
    assert d["revision"]["review_sha"] == "f" * 64 and d["revision"]["findings"] == findings
    assert d["revision"]["current_sha"] != "f" * 64
    assert not S.list_campaigns(cfg)
    r = run_cli(cfg, monkeypatch, ["queue", "add", "SPEC-0001", "--yes", "--json"])
    assert r.exit_code == 0 and len(S.list_campaigns(cfg)) == 1


def test_list_needs_you_table_and_park_keys(tmp_path, monkeypatch):
    """SPEC-0222: PARKED rows are coloured by owner and a `Needs you` table appears only when a
    human-owned park exists."""
    from test_agentic_dashboard import _campaign, ev_at
    repo, cfg = _cfg(tmp_path)
    rebase = _campaign(cfg, repo, "SPEC-0001-reb", "PARKED",
                       park={"reason": "rebase_conflict", "detail": "conflict in x",
                             "resume_to": "GATING"})
    ev_at(cfg, rebase, "park", {"reason": "rebase_conflict"}, ago_s=600)
    r = run_cli(cfg, monkeypatch, ["queue", "list"])
    assert r.exit_code == 0 and "PARKED:rebase_conflict" in r.output, r.output
    assert "Needs you" not in r.output
    assert "PARKED:rebase_conflict !" in r.output, r.output     # 600 s undecided: overdue
    dirty = _campaign(cfg, repo, "SPEC-0001-dir", "PARKED",
                      park={"reason": "main_dirty", "detail": "uncommitted files",
                            "resume_to": "CLOSING"})
    ev_at(cfg, dirty, "park", {"reason": "main_dirty"}, ago_s=60)
    r = run_cli(cfg, monkeypatch, ["queue", "list"])
    assert "Needs you (1)" in r.output and "uncommitted files" in r.output, r.output
    r = run_cli(cfg, monkeypatch, ["queue", "list", "--json"])
    items = {i["campaign"]: i for i in json.loads(r.output)["items"]}
    reb = items[rebase]["park"]
    assert reb["owner"] == "conductor" and reb["overdue"] is True and reb["decision"] is None
    assert reb["decided"] is False and reb["parked_at"]
    assert items[dirty]["park"]["owner"] == "human"


def test_list_marks_overdue_and_colours_parks_by_owner(tmp_path, monkeypatch):
    from io import StringIO
    from rich.console import Console
    from test_agentic_dashboard import _campaign, ev_at
    from wt.agentic import cli_queue as CQ
    from wt.agentic import queue as Q
    repo, cfg = _cfg(tmp_path)
    parks = (("old", "rebase_conflict", 600, False), ("young", "base_moving", 60, False),
             ("decided", "rebase_conflict", 600, True), ("dirty", "main_dirty", 600, False))
    ids = {}
    for key, reason, ago, noted in parks:
        ids[key] = _campaign(cfg, repo, f"SPEC-0001-{key}", "PARKED",
                             park={"reason": reason, "detail": "d", "resume_to": "GATING"})
        ev_at(cfg, ids[key], "park", {"reason": reason}, ago_s=ago)
        if noted:
            ev_at(cfg, ids[key], "note", {"text": "ok", "author": "conductor"}, ago_s=10)
    buf = StringIO()
    Console(file=buf, width=200, force_terminal=True, color_system="standard").print(
        CQ._queue_table(Q.queue_payload(cfg)))
    rows = {k: next(l for l in buf.getvalue().splitlines() if ids[k] in l) for k in ids}
    assert "!" in rows["old"] and "\x1b[31m!" in rows["old"]            # red overdue marker
    for k in ("young", "decided", "dirty"):
        assert "!" not in rows[k], rows[k]
    assert "\x1b[2mPARKED:rebase_conflict" in rows["old"]               # dim: conductor's
    assert "\x1b[2mPARKED:base_moving" in rows["young"]
    assert "\x1b[33mPARKED:main_dirty" in rows["dirty"]                 # yellow: the human's


# ---- SPEC-0276: batch add ---------------------------------------------------------------------

def add_spec(cfg, repo, n, *, deps=(), status="accepted", parent=None, review=True):
    """SPEC-000n in the fixture repo, reviewed unless `review` is false."""
    extra = ""
    if deps:
        extra += f"depends_on: [{', '.join(deps)}]\n"
    if parent:
        extra += f"parent: {parent}\n"
    sid = f"SPEC-{n:04d}"
    path = repo / "docs" / "specs" / f"{n:04d}-s{n}.md"
    path.write_text(SPEC_ACCEPTED.format(id=sid, extra=extra).replace(
        "status: accepted", f"status: {status}"))
    if review:
        write_review(cfg, path, sid)
    return sid


def batch(cfg, monkeypatch, *args):
    r = run_cli(cfg, monkeypatch, ["queue", "add", *args, "--json"])
    return r, (json.loads(r.output) if r.output.startswith("{") else None)


def campaign_order(cfg):
    return [c["spec_id"] for c in sorted(S.list_campaigns(cfg), key=lambda c: c["created_at"])]


def test_batch_topological_order(tmp_path, monkeypatch):
    repo, cfg = _cfg(tmp_path)
    b = add_spec(cfg, repo, 2, deps=["SPEC-0003"])
    add_spec(cfg, repo, 3)
    r, d = batch(cfg, monkeypatch, b, "SPEC-0003", "SPEC-0001")
    assert r.exit_code == 0 and d["schema"] == "wt.queue.add.batch.v1", r.output
    assert [i["id"] for i in d["items"]] == ["SPEC-0003", "SPEC-0002", "SPEC-0001"]
    assert [i["position"] for i in d["items"]] == [1, 2, 3] and d["ok"] == 3
    assert not S.list_campaigns(cfg)
    r, d = batch(cfg, monkeypatch, b, "SPEC-0003", "SPEC-0001", "--yes")
    assert r.exit_code == 0 and all(i["written"] for i in d["items"]), r.output
    order = [c["spec_id"] for c in sorted(S.list_campaigns(cfg), key=lambda c: c["created_at"])]
    assert order == ["SPEC-0003", "SPEC-0002", "SPEC-0001"]


def test_batch_cycle_refused(tmp_path, monkeypatch):
    repo, cfg = _cfg(tmp_path)
    add_spec(cfg, repo, 2, deps=["SPEC-0003"])
    add_spec(cfg, repo, 3, deps=["SPEC-0002"])
    add_spec(cfg, repo, 4, deps=["SPEC-0002"])
    r, d = batch(cfg, monkeypatch, "SPEC-0002", "SPEC-0003", "SPEC-0004", "--yes")
    assert r.exit_code == 1, r.output
    errs = {i["id"]: i["error"] for i in d["items"]}
    assert "dependency cycle: SPEC-0002 -> SPEC-0003 -> SPEC-0002" == errs["SPEC-0002"]
    assert errs["SPEC-0003"].startswith("dependency cycle")
    assert errs["SPEC-0004"] == "depends on refused SPEC-0002"      # downstream of the cycle
    assert not S.list_campaigns(cfg) and d["written"] is False and "cannot be queued" in d["refused"]


def test_batch_would_block_dangling_dep(tmp_path, monkeypatch):
    repo, cfg = _cfg(tmp_path)
    add_spec(cfg, repo, 2, deps=["SPEC-0003"])
    add_spec(cfg, repo, 3)
    r, d = batch(cfg, monkeypatch, "SPEC-0002", "SPEC-0001")
    item = next(i for i in d["items"] if i["id"] == "SPEC-0002")
    assert item["would_block"] == ["SPEC-0003 is accepted and not queued"]
    assert r.exit_code == 0                                      # dry run: the report is the result
    added(cfg, monkeypatch, "SPEC-0003")
    r, d = batch(cfg, monkeypatch, "SPEC-0002", "SPEC-0001")
    assert [i["would_block"] for i in d["items"]] == [[], []]


def test_batch_would_block_status_text(tmp_path, monkeypatch):
    repo, cfg = _cfg(tmp_path)
    add_spec(cfg, repo, 3, status="draft")
    add_spec(cfg, repo, 4, status="done")
    add_spec(cfg, repo, 2, deps=["SPEC-0003", "SPEC-0004", "SPEC-0077"])
    r, d = batch(cfg, monkeypatch, "SPEC-0002", "SPEC-0001")
    item = next(i for i in d["items"] if i["id"] == "SPEC-0002")
    assert item["would_block"] == ["SPEC-0003 is draft and not queued", "SPEC-0077 not found"]


def test_batch_all_or_nothing_and_skip_invalid(tmp_path, monkeypatch):
    repo, cfg = _cfg(tmp_path)
    add_spec(cfg, repo, 2)
    add_spec(cfg, repo, 3, review=False)
    r, d = batch(cfg, monkeypatch, "SPEC-0001", "SPEC-0002", "SPEC-0003", "--yes")
    assert r.exit_code == 1 and not S.list_campaigns(cfg), r.output
    assert d["errors"] == 1 and d["written"] is False and "1 of 3 items" in d["refused"]
    r, d = batch(cfg, monkeypatch, "SPEC-0001", "SPEC-0002", "SPEC-0003", "--yes",
                 "--skip-invalid")
    assert r.exit_code == 1, r.output                  # one item reported, not written
    assert sorted(campaign_order(cfg)) == ["SPEC-0001", "SPEC-0002"]
    assert d["written"] is True and "no spec review" in d["items"][2]["error"]


def test_batch_skip_invalid_refused_dependency(tmp_path, monkeypatch):
    repo, cfg = _cfg(tmp_path)
    add_spec(cfg, repo, 2, review=False)
    add_spec(cfg, repo, 3, deps=["SPEC-0002"])
    add_spec(cfg, repo, 4, deps=["SPEC-0003"])
    r, d = batch(cfg, monkeypatch, "SPEC-0003", "SPEC-0002", "SPEC-0004")      # the plan says so
    assert r.exit_code == 0 and d["errors"] == 3 and d["ok"] == 0
    r, d = batch(cfg, monkeypatch, "SPEC-0003", "SPEC-0002", "SPEC-0004", "--yes",
                 "--skip-invalid")
    assert r.exit_code == 1, r.output
    assert not S.list_campaigns(cfg)
    errs = {i["id"]: i["error"] for i in d["items"]}
    assert errs["SPEC-0003"] == "depends on refused SPEC-0002"
    assert errs["SPEC-0004"] == "depends on refused SPEC-0003"


def test_batch_race_cascades_at_write_time(tmp_path, monkeypatch):
    repo, cfg = _cfg(tmp_path)
    add_spec(cfg, repo, 2)
    add_spec(cfg, repo, 3, deps=["SPEC-0002"])
    plan = Q.plan_add_batch(cfg, ["SPEC-0003", "SPEC-0002", "SPEC-0001"])
    added(cfg, monkeypatch, "SPEC-0002")                          # someone queues it meanwhile
    out = Q.apply_add_batch(cfg, plan)
    byid = {i["id"]: i for i in out["items"]}
    assert "already has an active campaign" in byid["SPEC-0002"]["error"]
    assert byid["SPEC-0003"]["error"] == "depends on refused SPEC-0002"
    assert byid["SPEC-0001"]["written"] is True and out["errors"] == 2


def test_batch_cap_total_uncapped(tmp_path, monkeypatch):
    repo, cfg = _cfg(tmp_path)
    add_spec(cfg, repo, 2)
    plan = Q.plan_add_batch(cfg, ["SPEC-0001", "SPEC-0002"])
    assert plan["cap_usd_total"] == 20.0 and plan["uncapped"] == 0
    plan["items"][0]["plan"]["cap_usd"] = None
    assert Q._batch_totals(plan["items"]) == {"ok": 2, "errors": 0, "cap_usd_total": 10.0,
                                              "uncapped": 1}


def test_batch_epic_expansion(tmp_path, monkeypatch):
    repo, cfg = _cfg(tmp_path)
    add_spec(cfg, repo, 5)                                         # the epic
    add_spec(cfg, repo, 10, parent="SPEC-0005")
    add_spec(cfg, repo, 11, parent="SPEC-0005")                   # queued first
    add_spec(cfg, repo, 12, parent="SPEC-0005", status="draft")
    add_spec(cfg, repo, 13, parent="SPEC-0005", status="done")
    free = OW.add_idea(cfg, "free idea", epic="SPEC-0005")[2]
    covered = OW.add_idea(cfg, "covered idea", epic="SPEC-0005")[2]
    OW.set_property(cfg, Q._idea_task(cfg, covered), "SPEC", "SPEC-0013")
    closed = OW.add_idea(cfg, "closed idea", epic="SPEC-0005")[2]
    OW.set_state(cfg, Q._idea_task(cfg, closed), "DROPPED")
    added(cfg, monkeypatch, "SPEC-0011")
    r, d = batch(cfg, monkeypatch, "--epic", "SPEC-0005", "--yes")
    assert r.exit_code == 0, r.output
    assert [i["id"] for i in d["items"]] == ["SPEC-0010", free]
    skipped = {s["id"]: s["reason"] for s in d["skipped"]}
    assert skipped["SPEC-0012"] == "status draft" and skipped["SPEC-0013"] == "status done"
    assert skipped[covered] == "has SPEC-0013; covered by the spec"
    assert skipped[closed] == "idea is DROPPED"                   # never queued
    assert closed not in [i["id"] for i in d["items"]]
    assert skipped["SPEC-0011"].startswith("already queued (SPEC-0011-")
    assert len(S.list_campaigns(cfg)) == 3
    r, d = batch(cfg, monkeypatch, "--epic", "SPEC-0005", "--yes")      # top-up: nothing new
    assert r.exit_code == 0 and d["items"] == [] and len(S.list_campaigns(cfg)) == 3
    assert {s["id"] for s in d["skipped"]} >= {"SPEC-0010", free, "SPEC-0011", closed}


def test_batch_ideas_come_after_specs(tmp_path, monkeypatch):
    repo, cfg = _cfg(tmp_path)
    add_spec(cfg, repo, 2)
    idea = OW.add_idea(cfg, "an idea")[2]
    r, d = batch(cfg, monkeypatch, idea, "SPEC-0002", "SPEC-0001")
    assert r.exit_code == 0, r.output
    assert [i["id"] for i in d["items"]] == ["SPEC-0002", "SPEC-0001", idea]
    assert [i["position"] for i in d["items"]] == [1, 2, 3] and d["items"][2]["kind"] == "idea"
    r, d = batch(cfg, monkeypatch, idea, "SPEC-0002", "SPEC-0001", "--yes")
    assert r.exit_code == 0 and all(i["written"] for i in d["items"]), r.output
    created = [c for c in sorted(S.list_campaigns(cfg), key=lambda c: c["created_at"])]
    assert [c["spec_id"] for c in created[:2]] == ["SPEC-0002", "SPEC-0001"]
    assert len(created) == 3 and created[2]["kind"] == "idea" and created[2]["idea"]["id"] == idea


def test_batch_from_file_comments_and_dedup(tmp_path, monkeypatch):
    repo, cfg = _cfg(tmp_path)
    add_spec(cfg, repo, 2)
    f = tmp_path / "ids.txt"
    f.write_text("# header\nSPEC-0002  # trailing\n\n  SPEC-0001\nSPEC-0002\n")
    r, d = batch(cfg, monkeypatch, "SPEC-0001", "--from-file", str(f))
    assert r.exit_code == 0, r.output
    assert [i["id"] for i in d["items"]] == ["SPEC-0001", "SPEC-0002"]
    r = run_cli(cfg, monkeypatch, ["queue", "add"])
    assert r.exit_code == 2 and "at least one" in r.output


def test_batch_human_output(tmp_path, monkeypatch):
    repo, cfg = _cfg(tmp_path)
    add_spec(cfg, repo, 3, review=False)
    r = run_cli(cfg, monkeypatch, ["queue", "add", "SPEC-0001", "SPEC-0003"])
    assert r.exit_code == 0 and "1 to queue · 1 refused · $10 cap total" in r.output, r.output
    assert "not written" in r.output
    r = run_cli(cfg, monkeypatch, ["queue", "add", "SPEC-0001", "SPEC-0003", "--yes"])
    assert r.exit_code == 1 and "cannot be queued" in r.output and not S.list_campaigns(cfg)


def test_single_id_output_unchanged(tmp_path, monkeypatch):
    repo, cfg = _cfg(tmp_path)
    r = run_cli(cfg, monkeypatch, ["queue", "add", "SPEC-0001", "--json"])
    d = json.loads(r.output)
    assert d["schema"] == "wt.queue.add.v1" and "items" not in d
    assert {"campaign", "spec_id", "project", "profile", "priority", "base", "worktree",
            "branch", "cap_usd", "written"} <= set(d)
