"""SPEC-0176: `nw queue add|list|park|resume|abandon` — refusals, dry-run defaults, `--json`
envelopes. Repos come from the e2e harness; nothing runs a persona here."""
import json
import subprocess

import pytest

from test_agentic_e2e import (SPEC_ACCEPTED, added, impl_done, make_cfg, make_repo, run_cli, script,
                              write_review)
from nightwright import org_write as OW
from nightwright.agentic import queue as Q
from nightwright.agentic import store as S


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
    assert d["schema"] == "nw.queue.add.v1" and d["written"] is False
    assert d["project"] == "Meta-Tools" and d["profile"] == "balanced" and d["base"] == "main"
    assert d["spec_rel_path"] == "docs/specs/0001-trivial.md" and d["branch"].startswith("nw/SPEC-0001-")
    assert d["worktree"].endswith(f"/.nw-worktrees/Meta-Tools/{d['campaign']}")
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
    assert d["schema"] == "nw.queue.v1"
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
    assert d["schema"] == "nw.queue.resume.v1" and d["to"] == "QUEUED" and d["written"] is False
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
    assert d["schema"] == "nw.queue.abandon.v1" and d["written"] is False and d["remove_worktree"] is False
    r = run_cli(cfg, monkeypatch, ["queue", "abandon", cid, "--yes"])
    assert r.exit_code == 0 and S.read_state(cfg, cid)["state"] == "ABANDONED"
    assert S.read_ledger(cfg, cid, types=["close"])[-1]["mode"] == "abandoned"


def test_queue_help_lists_every_command(monkeypatch):
    r = run_cli({"agentic": {}, "data_dir": "/nonexistent", "config_dir": "/nonexistent"},
                monkeypatch, ["queue", "--help"])
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
    assert d["schema"] == "nw.queue.prioritize.v1" and d["written"] is False
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
    assert d["schema"] == "nw.queue.backlog.v1"
    assert [i["campaign"] for i in d["items"]] == [second, first]
    top = d["items"][0]
    assert set(top) >= {"campaign", "spec_id", "project", "priority", "position", "ready",
                        "blocked_by", "age_s"}             # SPEC-0356 adds row fields
    assert top["waiting_on"] == ["SPEC-0001", "SPEC-0099"]
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
    from nightwright.agentic import schemas as SC
    repo, cfg = _sized(tmp_path)
    cid = added(cfg, monkeypatch)
    st = S.read_state(cfg, cid)
    assert st["profile"] == "frugal"
    from nightwright.agentic import spec_review as SR
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
    from nightwright.agentic import cli_queue as CQ
    from nightwright.agentic import queue as Q
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
    # SPEC-0356: a narrow table drops size / tone / profile (rank 7) and never cuts the ids
    narrow = _render(CQ._queue_table(payload, 80), 80)
    assert "cost-effective" not in narrow and max(map(len, narrow.splitlines())) <= 80
    assert sized in narrow and "SPEC-0002-old" in narrow


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
    row = next(line for line in r.output.splitlines() if rebase in line)
    assert r.exit_code == 0 and "rebase_conflict" in row, r.output    # SPEC-0356: own column
    assert "PARKED:" not in r.output and "Needs you" not in r.output
    assert "PARKED !" in row, r.output                          # 600 s undecided: overdue
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
    from nightwright.agentic import cli_queue as CQ
    from nightwright.agentic import queue as Q
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
    assert "\x1b[2mPARKED" in rows["old"] and "rebase_conflict" in rows["old"]   # dim: conductor's
    assert "\x1b[2mPARKED" in rows["young"] and "base_moving" in rows["young"]
    assert "\x1b[33mPARKED" in rows["dirty"] and "main_dirty" in rows["dirty"]   # yellow: the human's


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


def write_log(monkeypatch):
    """Ids in the order the batch applied them. `created_at` has 1 s resolution, so the creation
    order is read from the appliers' calls, which are what `apply_add_batch` sequences."""
    log = []
    for name in ("apply_add", "apply_add_idea"):
        def rec(cfg, plan, *a, _orig=getattr(Q, name), **kw):
            log.append(plan.get("spec_id") or plan["idea_id"])
            return _orig(cfg, plan, *a, **kw)
        monkeypatch.setattr(Q, name, rec)
    return log


def campaign_order(cfg):
    return [c["spec_id"] for c in sorted(S.list_campaigns(cfg), key=lambda c: c["created_at"])]


def test_batch_topological_order(tmp_path, monkeypatch):
    repo, cfg = _cfg(tmp_path)
    b = add_spec(cfg, repo, 2, deps=["SPEC-0003"])
    add_spec(cfg, repo, 3)
    r, d = batch(cfg, monkeypatch, b, "SPEC-0003", "SPEC-0001")
    assert r.exit_code == 0 and d["schema"] == "nw.queue.add.batch.v1", r.output
    assert [i["id"] for i in d["items"]] == ["SPEC-0003", "SPEC-0002", "SPEC-0001"]
    assert [i["position"] for i in d["items"]] == [1, 2, 3] and d["ok"] == 3
    assert not S.list_campaigns(cfg)
    log = write_log(monkeypatch)
    r, d = batch(cfg, monkeypatch, b, "SPEC-0003", "SPEC-0001", "--yes")
    assert r.exit_code == 0 and all(i["written"] for i in d["items"]), r.output
    assert log == ["SPEC-0003", "SPEC-0002", "SPEC-0001"]
    assert sorted(campaign_order(cfg)) == sorted(log)


def test_batch_write_order_detects_a_wrong_sort(tmp_path, monkeypatch):
    """Sabotage guard: a reversed topological sort writes the dependent first, and the write
    log shows it."""
    repo, cfg = _cfg(tmp_path)
    add_spec(cfg, repo, 2, deps=["SPEC-0003"])
    add_spec(cfg, repo, 3)
    monkeypatch.setattr(Q, "_order_specs", lambda ids, deps: (list(reversed(ids)), {}))
    log = write_log(monkeypatch)
    r, d = batch(cfg, monkeypatch, "SPEC-0003", "SPEC-0002", "--yes")
    assert r.exit_code == 0, r.output
    assert log == ["SPEC-0002", "SPEC-0003"]                     # the wrong order, visible


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
    log = write_log(monkeypatch)
    r, d = batch(cfg, monkeypatch, idea, "SPEC-0002", "SPEC-0001", "--yes")
    assert r.exit_code == 0 and all(i["written"] for i in d["items"]), r.output
    assert log == ["SPEC-0002", "SPEC-0001", idea]
    created = S.list_campaigns(cfg)
    assert len(created) == 3
    assert sum(1 for c in created if c["kind"] == "idea" and c["idea"]["id"] == idea) == 1


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
    assert d["schema"] == "nw.queue.add.v1" and "items" not in d
    assert {"campaign", "spec_id", "project", "profile", "priority", "base", "worktree",
            "branch", "cap_usd", "written"} <= set(d)


def test_batch_cycle_message_on_member_with_planner_error(tmp_path, monkeypatch):
    repo, cfg = _cfg(tmp_path)
    add_spec(cfg, repo, 2, deps=["SPEC-0003"], review=False)
    add_spec(cfg, repo, 3, deps=["SPEC-0002"])
    r, d = batch(cfg, monkeypatch, "SPEC-0002", "SPEC-0003")
    errs = {i["id"]: i["error"] for i in d["items"]}
    assert "no spec review" in errs["SPEC-0002"]
    assert "dependency cycle: SPEC-0002 -> SPEC-0003 -> SPEC-0002" in errs["SPEC-0002"]
    assert errs["SPEC-0003"].startswith("dependency cycle")


def test_batch_ids_are_case_insensitive(tmp_path, monkeypatch):
    repo, cfg = _cfg(tmp_path)
    add_spec(cfg, repo, 2, deps=["spec-0003"])
    add_spec(cfg, repo, 3)
    log = write_log(monkeypatch)
    r, d = batch(cfg, monkeypatch, "spec-0003", "SPEC-0003", "spec-0002", "--yes")
    assert r.exit_code == 0, r.output
    assert [i["id"] for i in d["items"]] == ["SPEC-0003", "SPEC-0002"]
    assert d["items"][1]["would_block"] == []
    assert log == ["SPEC-0003", "SPEC-0002"]
    # a depends_on written in another case still orders the batch
    add_spec(cfg, repo, 4, deps=["spec-0005"])
    add_spec(cfg, repo, 5)
    r, d = batch(cfg, monkeypatch, "SPEC-0004", "SPEC-0005")
    assert [i["id"] for i in d["items"]] == ["SPEC-0005", "SPEC-0004"]


def test_batch_markup_in_ids_does_not_crash(tmp_path, monkeypatch):
    repo, cfg = _cfg(tmp_path)
    f = tmp_path / "ids.txt"
    f.write_text("SPEC-0001[/]\n[/x]\nSPEC-0001\n")
    r = run_cli(cfg, monkeypatch, ["queue", "add", "--from-file", str(f)])
    assert r.exit_code == 0 and r.exception is None, r.output
    assert "SPEC-0001[/]" in r.output and "[/x]" in r.output and "2 refused" in r.output


def test_batch_epic_source_order_and_explicit_ids(tmp_path, monkeypatch):
    repo, cfg = _cfg(tmp_path)
    add_spec(cfg, repo, 5)                                         # the epic
    add_spec(cfg, repo, 10, parent="SPEC-0005")
    add_spec(cfg, repo, 11, parent="SPEC-0005")                   # queued first
    add_spec(cfg, repo, 12, parent="SPEC-0005")
    add_spec(cfg, repo, 20)
    f = tmp_path / "ids.txt"
    f.write_text("SPEC-0001\nSPEC-0010\n")                        # SPEC-0010 is also an epic child
    added(cfg, monkeypatch, "SPEC-0011")
    r, d = batch(cfg, monkeypatch, "SPEC-0020", "--from-file", str(f), "--epic", "SPEC-0005",
                 "SPEC-0011")
    assert r.exit_code == 0, r.output
    ids = [i["id"] for i in d["items"]]
    assert ids == ["SPEC-0020", "SPEC-0011", "SPEC-0001", "SPEC-0010", "SPEC-0012"], ids
    byid = {i["id"]: i for i in d["items"]}
    assert "already has an active campaign" in byid["SPEC-0011"]["error"]
    assert "SPEC-0011" not in [s["id"] for s in d["skipped"]]       # explicit: refused, not skipped
    assert "SPEC-0010" not in [s["id"] for s in d["skipped"]]       # explicit and de-duplicated


def test_batch_unknown_epic_writes_nothing(tmp_path, monkeypatch):
    repo, cfg = _cfg(tmp_path)
    r = run_cli(cfg, monkeypatch, ["queue", "add", "SPEC-0001", "--epic", "SPEC-0999", "--yes"])
    assert r.exit_code != 0 and not S.list_campaigns(cfg), r.output


def test_batch_uncapped_in_cli_output(tmp_path, monkeypatch):
    repo, cfg = _cfg(tmp_path)
    add_spec(cfg, repo, 2)
    orig = Q.plan_add

    def uncapped(*a, **kw):
        plan = orig(*a, **kw)
        plan["cap_usd"] = None
        return plan

    monkeypatch.setattr(Q, "plan_add", uncapped)
    r, d = batch(cfg, monkeypatch, "SPEC-0001", "SPEC-0002")
    assert d["uncapped"] == 2 and d["cap_usd_total"] == 0, d
    r = run_cli(cfg, monkeypatch, ["queue", "add", "SPEC-0001", "SPEC-0002"])
    assert "$0 cap total + 2 uncapped" in r.output, r.output


def test_batch_epic_id_is_canonicalised(tmp_path, monkeypatch):
    repo, cfg = _cfg(tmp_path)
    add_spec(cfg, repo, 5)                                         # the epic
    add_spec(cfg, repo, 10, parent="SPEC-0005")
    free = OW.add_idea(cfg, "free idea", epic="SPEC-0005")[2]
    r, d = batch(cfg, monkeypatch, "--epic", "spec-0005", "--yes")
    assert r.exit_code == 0, r.output
    assert [i["id"] for i in d["items"]] == ["SPEC-0010", free] and d["ok"] == 2


def test_batch_epic_idea_without_usable_id_is_skipped(tmp_path, monkeypatch):
    repo, cfg = _cfg(tmp_path)
    add_spec(cfg, repo, 5)                                         # the epic
    add_spec(cfg, repo, 10, parent="SPEC-0005")
    noid = OW.add_idea(cfg, "no id idea", epic="SPEC-0005")[2]
    OW.set_property(cfg, Q._idea_task(cfg, noid), "ID", "weird")
    lower = OW.add_idea(cfg, "lowercase id idea", epic="SPEC-0005")[2]
    OW.set_property(cfg, Q._idea_task(cfg, lower), "ID", lower.lower())
    r, d = batch(cfg, monkeypatch, "--epic", "SPEC-0005", "--yes")
    assert r.exit_code == 0, r.output                              # nothing trips all-or-nothing
    assert [i["id"] for i in d["items"]] == ["SPEC-0010", lower]
    assert "weird" in {s["id"] for s in d["skipped"]}
    r, d = batch(cfg, monkeypatch, "--epic", "SPEC-0005", "--yes")      # top-up: nothing new
    assert r.exit_code == 0 and d["items"] == [], r.output


def test_batch_outbound_dependencies_resolve_in_the_target_repo(tmp_path, monkeypatch):
    repo, cfg = _cfg(tmp_path)
    add_spec(cfg, repo, 3, status="done")                          # nightwright's own SPEC-0003
    target = tmp_path / "target" / "docs" / "specs"
    target.mkdir(parents=True)
    (target / "0002-x.md").write_text(
        "---\nid: ACME-0002\nstatus: accepted\ndepends_on: [SPEC-0003]\n---\n")
    (target / "0003-y.md").write_text("---\nid: SPEC-0003\nstatus: draft\n---\n")
    outbox = tmp_path / "data" / "outbox" / "Acme"
    outbox.mkdir(parents=True)
    (outbox / "ACME-0002-x.md").write_text(
        f"---\nid: ACME-0002\nstatus: accepted\ntarget_spec_path: {target / '0002-x.md'}\n---\n")
    cfg["outbox_dir"] = str(tmp_path / "data" / "outbox")
    assert Q._depends_on(cfg, "ACME-0002") == ["SPEC-0003"]       # the target copy's, not the outbox's
    allv, edges = Q._batch_deps(cfg, ["ACME-0002", "SPEC-0003"])
    assert allv["ACME-0002"] == ["SPEC-0003"] and edges["ACME-0002"] == []   # no false edge
    assert Q._dangling(cfg, "ACME-0002", ["SPEC-0003"], {"ACME-0002", "SPEC-0003"}) == [
        "SPEC-0003 is draft and not queued"]                      # the target's status, not nightwright's


def test_batch_write_time_race_exits_1_through_the_cli(tmp_path, monkeypatch):
    repo, cfg = _cfg(tmp_path)
    add_spec(cfg, repo, 2)
    orig = Q.plan_add_batch

    def racy(*a, **kw):
        plan = orig(*a, **kw)
        added(cfg, monkeypatch, "SPEC-0002")                      # someone queues it meanwhile
        return plan

    monkeypatch.setattr(Q, "plan_add_batch", racy)
    r, d = batch(cfg, monkeypatch, "SPEC-0001", "SPEC-0002", "--yes")
    assert r.exit_code == 1, r.output
    byid = {i["id"]: i for i in d["items"]}
    assert byid["SPEC-0001"]["written"] is True and d["errors"] == 1
    assert "already has an active campaign" in byid["SPEC-0002"]["error"]


def test_batch_cycle_under_skip_invalid_writes_only_the_valid_items(tmp_path, monkeypatch):
    repo, cfg = _cfg(tmp_path)
    add_spec(cfg, repo, 2, deps=["SPEC-0003"])
    add_spec(cfg, repo, 3, deps=["SPEC-0002"])
    add_spec(cfg, repo, 4, deps=["SPEC-0002"])                   # downstream of the cycle
    log = write_log(monkeypatch)
    r, d = batch(cfg, monkeypatch, "SPEC-0002", "SPEC-0003", "SPEC-0004", "SPEC-0001", "--yes",
                 "--skip-invalid")
    assert r.exit_code == 1, r.output
    assert log == ["SPEC-0001"] and campaign_order(cfg) == ["SPEC-0001"]
    byid = {i["id"]: i for i in d["items"]}
    assert byid["SPEC-0001"]["written"] is True and d["written"] is True
    assert d["ok"] == 1 and d["errors"] == 3
    assert all("error" in byid[k] and "written" not in byid[k]
               for k in ("SPEC-0002", "SPEC-0003", "SPEC-0004"))


def test_batch_empty_from_file_and_empty_epic(tmp_path, monkeypatch):
    repo, cfg = _cfg(tmp_path)
    add_spec(cfg, repo, 5)                                         # an epic without children
    f = tmp_path / "empty.txt"
    f.write_text("# nothing here\n\n   \n")
    for args in (["--from-file", str(f)], ["--epic", "SPEC-0005"]):
        r, d = batch(cfg, monkeypatch, *args)
        assert r.exit_code == 0 and d["items"] == [], r.output
        assert (d["ok"], d["errors"], d["uncapped"], d["cap_usd_total"]) == (0, 0, 0, 0)
        r, d = batch(cfg, monkeypatch, *args, "--yes")
        assert r.exit_code == 0 and d["items"] == [] and d["written"] is False, r.output
        assert d["refused"] is None and not S.list_campaigns(cfg)
        r = run_cli(cfg, monkeypatch, ["queue", "add", *args])
        assert r.exit_code == 0 and "0 to queue · 0 refused · $0 cap total" in r.output, r.output


def test_batch_epic_child_without_id_is_skipped(tmp_path, monkeypatch):
    repo, cfg = _cfg(tmp_path)
    add_spec(cfg, repo, 5)
    add_spec(cfg, repo, 10, parent="SPEC-0005")
    path = repo / "docs" / "specs" / "0011-s11.md"
    path.write_text(SPEC_ACCEPTED.format(id="SPEC-0011", extra="parent: SPEC-0005\n").replace(
        "id: SPEC-0011\n", ""))
    assert "id:" not in path.read_text().split("---")[1]
    r, d = batch(cfg, monkeypatch, "--epic", "SPEC-0005", "--yes")
    assert r.exit_code == 0, r.output
    assert [i["id"] for i in d["items"]] == ["SPEC-0010"]
    assert {"id": "0011-s11", "reason": "no id"} in d["skipped"]
    r = run_cli(cfg, monkeypatch, ["queue", "add", "--epic", "SPEC-0005"])
    assert "skipped 0011-s11: no id" in r.output, r.output


def test_batch_unreadable_from_file_is_a_clean_error(tmp_path, monkeypatch):
    """click checks the file is readable up front; a read that still fails (the file vanished,
    an I/O error) must be a clean error, not a traceback."""
    repo, cfg = _cfg(tmp_path)
    f = tmp_path / "ids.txt"
    f.write_text("SPEC-0001\n")
    real_open = open

    def flaky(path, *a, **kw):
        if str(path) == str(f):
            raise PermissionError(13, "Permission denied", str(path))
        return real_open(path, *a, **kw)

    monkeypatch.setattr("builtins.open", flaky)
    r = run_cli(cfg, monkeypatch, ["queue", "add", "--from-file", str(f), "--yes"])
    monkeypatch.undo()
    assert r.exit_code == 1 and "Traceback" not in r.output, r.output
    assert "Permission denied" in r.output and not S.list_campaigns(cfg)


def test_batch_wide_flags_reach_every_written_campaign(tmp_path, monkeypatch):
    """--priority/--profile/--base/--project/--force apply to every item; `apply_add_batch`
    re-plans each item from `params`, so the written campaigns must carry them."""
    repo, cfg = _cfg(tmp_path)
    add_spec(cfg, repo, 2)
    idea = OW.add_idea(cfg, "an idea")[2]
    args = ("SPEC-0001", "SPEC-0002", idea, "--priority", "2", "--profile", "thorough",
            "--base", "main", "--project", "Meta-Tools")
    r, d = batch(cfg, monkeypatch, *args)
    assert r.exit_code == 0 and d["params"] == {"project": "Meta-Tools", "profile": "thorough",
                                                "base": "main", "force": False, "priority": 2}
    assert [i["plan"]["profile"] for i in d["items"]] == ["thorough"] * 3
    assert not S.list_campaigns(cfg)
    r, d = batch(cfg, monkeypatch, *args, "--yes")
    assert r.exit_code == 0 and all(i["written"] for i in d["items"]), r.output
    created = S.list_campaigns(cfg)
    assert len(created) == 3
    for c in created:
        st = S.read_state(cfg, c["id"])
        assert (st["priority"], st["profile"], st["project"]) == (2, "thorough", "Meta-Tools")
    assert sorted(i["plan"]["campaign"] for i in d["items"]) == sorted(c["id"] for c in created)
    assert {i["plan"]["base"] for i in d["items"]} == {"main"}


def test_batch_force_applies_to_every_item(tmp_path, monkeypatch):
    repo, cfg = _cfg(tmp_path)
    add_spec(cfg, repo, 2, review=False)
    r, d = batch(cfg, monkeypatch, "SPEC-0001", "SPEC-0002", "--yes")
    assert r.exit_code == 1 and not S.list_campaigns(cfg), r.output           # unreviewed: refused
    r, d = batch(cfg, monkeypatch, "SPEC-0001", "SPEC-0002", "--yes", "--force")
    assert r.exit_code == 0 and d["params"]["force"] is True, r.output
    created = S.list_campaigns(cfg)
    assert sorted(c["spec_id"] for c in created) == ["SPEC-0001", "SPEC-0002"]
    assert all(S.read_state(cfg, c["id"]).get("force") for c in created)


def test_batch_superseded_dependency_is_not_would_block(tmp_path, monkeypatch):
    repo, cfg = _cfg(tmp_path)
    add_spec(cfg, repo, 3, status="superseded")
    add_spec(cfg, repo, 2, deps=["SPEC-0003"])
    r, d = batch(cfg, monkeypatch, "SPEC-0002", "SPEC-0001")
    assert r.exit_code == 0 and d["items"][0]["would_block"] == [], r.output


def test_batch_refused_idea_blocks_yes_without_skip_invalid(tmp_path, monkeypatch):
    repo, cfg = _cfg(tmp_path)
    idea = OW.add_idea(cfg, "has a spec")[2]
    OW.set_property(cfg, Q._idea_task(cfg, idea), "SPEC", "SPEC-0001")
    r, d = batch(cfg, monkeypatch, "SPEC-0001", idea, "--yes")
    byid = {i["id"]: i for i in d["items"]}
    assert "already has SPEC-0001" in byid[idea]["error"]
    assert r.exit_code == 1 and not S.list_campaigns(cfg) and d["written"] is False
    r, d = batch(cfg, monkeypatch, "SPEC-0001", idea, "--yes", "--skip-invalid")
    assert r.exit_code == 1 and [c["spec_id"] for c in S.list_campaigns(cfg)] == ["SPEC-0001"]


def test_queue_run_detach_once_usage_error(tmp_path, monkeypatch):
    """SPEC-0291: short runs need no detach."""
    repo, cfg = _cfg(tmp_path)
    for extra in (["--once"], ["--max-ticks", "3"]):
        r = run_cli(cfg, monkeypatch, ["queue", "run", "--detach", *extra])
        assert r.exit_code == 2 and "--detach" in r.output, r.output
    assert not (S.agentic_dir(cfg) / "runner.log").exists()


def test_queue_stop_dry_run_no_signal(tmp_path, monkeypatch):
    from nightwright.agentic import lease as L
    repo, cfg = _cfg(tmp_path)
    real_stop = Q.stop_runner

    def no_stop(*a, **k):                   # stop_runner's own `kill` default is bound early
        raise AssertionError("a dry run must not call stop_runner")

    monkeypatch.setattr(Q, "stop_runner", no_stop)
    with L.acquire(cfg, ["nw", "queue", "run"]):
        r = run_cli(cfg, monkeypatch, ["queue", "stop"])
        assert r.exit_code == 0 and "not written" in r.output and "SIGTERM" in r.output, r.output
        assert r.exception is None and L.is_held(cfg)
    monkeypatch.setattr(Q, "stop_runner", real_stop)
    r = run_cli(cfg, monkeypatch, ["queue", "stop", "--yes"])
    assert r.exit_code == 0 and "no runner" in r.output, r.output


def test_queue_stop_json_schema(tmp_path, monkeypatch):
    import os

    from nightwright.agentic import lease as L
    repo, cfg = _cfg(tmp_path)
    monkeypatch.delenv("NW_MAILBOX_ADDR", raising=False)
    r = run_cli(cfg, monkeypatch, ["queue", "stop", "--yes", "--json"])
    d = json.loads(r.output)
    assert r.exit_code == 0 and d["schema"] == "nw.queue.stop.v1" and d["written"] is True
    assert d["held"] is False and d["result"] == {"stopped": False, "reason": "no runner"}
    with L.acquire(cfg, ["nw", "queue", "run"]) as lease:
        lease.update_info(busy={"kind": "close", "campaign": "C-1", "since": S.now_iso(),
                                "budget_s": 600})
        d = json.loads(run_cli(cfg, monkeypatch, ["queue", "stop", "--json", "--wait-guard"]).output)
        assert d["written"] is False and d["result"] is None and d["pid"] == os.getpid()
        assert d["guard"]["kind"] == "close" and d["wait_guard"] is True and d["timeout_s"] == 60
        assert d["by"] == "human"
        r = run_cli(cfg, monkeypatch, ["queue", "stop", "--yes"])
        assert r.exit_code == 1 and "close guard: close of C-1" in r.output, r.output
        r = run_cli(cfg, monkeypatch, ["queue", "stop", "--yes", "--json"])
        d = json.loads(r.output)                # a refusal is still nw.queue.stop.v1 JSON
        assert r.exit_code == 1 and d["schema"] == "nw.queue.stop.v1" and d["result"] is None
        assert d["error"].startswith("close guard: close of C-1") and L.is_held(cfg)


def test_queue_stop_dry_run_shows_the_same_refusals(tmp_path, monkeypatch):
    """The plan says "refuse" wherever `stop_runner` would: the close guard (without
    --wait-guard), a one-shot tick, another host."""
    import os

    from nightwright.agentic import lease as L
    repo, cfg = _cfg(tmp_path)
    monkeypatch.setattr(Q, "stop_runner", lambda *a, **k: pytest.fail("a dry run"))

    def plan(*extra):
        r = run_cli(cfg, monkeypatch, ["queue", "stop", "--json", *extra])
        assert r.exit_code == 0, r.output
        return json.loads(r.output)

    with L.acquire(cfg, ["nw", "queue", "run"]) as lease:
        assert plan()["refusal"] is None
        lease.update_info(busy={"kind": "open", "campaign": "C-1", "since": S.now_iso(),
                                "budget_s": 600})
        assert plan()["refusal"].startswith("close guard: open of C-1")
        assert plan("--wait-guard")["refusal"] is None
        r = run_cli(cfg, monkeypatch, ["queue", "stop"])
        assert "refuse: close guard" in r.output.replace("\n", ""), r.output
        lease.update_info(busy=None, host="elsewhere.example")
        assert "not this host" in plan()["refusal"]
    with L.acquire(cfg, ["nw", "queue", "tick"]):
        d = plan()
        assert d["pid"] == os.getpid() and "one-shot `nw queue tick`" in d["refusal"]


def test_queue_stop_reports_a_runner_that_started_after_the_plan(tmp_path, monkeypatch):
    """No holder at the plan read, then a runner takes the lease and `stop_runner` stops it:
    the output names that runner, never "no runner"; the JSON is coherent with the result."""
    import socket
    repo, cfg = _cfg(tmp_path)
    monkeypatch.setattr(Q, "stop_runner",
                        lambda *a, **k: {"stopped": True, "pid": 4242, "waited_s": 0.3})
    r = run_cli(cfg, monkeypatch, ["queue", "stop", "--yes"])
    assert r.exit_code == 0 and "runner pid 4242 stopped" in r.output, r.output
    assert "no runner" not in r.output
    d = json.loads(run_cli(cfg, monkeypatch, ["queue", "stop", "--yes", "--json"]).output)
    assert d["held"] is True and d["pid"] == 4242 and d["host"] == socket.gethostname()
    assert d["result"]["stopped"] is True


def test_queue_run_detach_cli_wiring(tmp_path, monkeypatch):
    """SPEC-0291: the command's argv, default log, `--log` and output; ValueError ⇒ exit 1."""
    import sys
    repo, cfg = _cfg(tmp_path)
    exe = tmp_path / "bin" / "nw"
    exe.parent.mkdir()
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    calls = []

    def detach(cfg_, argv, log, *, timeout_s):
        calls.append((argv, log, timeout_s))
        return {"pid": 4242, "log": str(log)}

    monkeypatch.setattr(Q, "detach_runner", detach)
    args = ["queue", "run", "--detach", "--poll-interval", "5"]
    monkeypatch.setattr(sys, "argv", [str(exe), *args])
    r = run_cli(cfg, monkeypatch, args)
    default_log = str(S.agentic_dir(cfg) / "runner.log")
    assert r.exit_code == 0, r.output
    assert calls == [([str(exe), *args], default_log, 15)]
    assert f"runner started: pid 4242, log {default_log}" in r.output.replace("\n", "")
    calls.clear()
    monkeypatch.setattr(sys, "argv", [str(exe), *args, "--log", "x.log"])
    r = run_cli(cfg, monkeypatch, [*args, "--log", "x.log"])
    assert r.exit_code == 0 and calls[0][1] == "x.log", r.output

    def refuse(*a, **k):
        raise ValueError("queue held by pid 77 (heartbeat 1s ago); stop it with nw queue stop")

    monkeypatch.setattr(Q, "detach_runner", refuse)
    r = run_cli(cfg, monkeypatch, args)
    assert r.exit_code == 1 and "queue held by pid 77" in r.output, r.output


def test_dangling_outbound_prefixed_file_and_handed_off_dep(tmp_path, monkeypatch):
    """SPEC-0347: `_dangling` reads `PROJ-NNNN-slug.md` and skips a handed-off dependency."""
    repo, cfg = _cfg(tmp_path)
    target = tmp_path / "target" / "docs" / "specs"
    target.mkdir(parents=True)
    (target / "ACME-0002-x.md").write_text(
        "---\nid: ACME-0002\nstatus: accepted\ndepends_on: [ACME-0003, ACME-0004]\n---\n")
    (target / "ACME-0003-y.md").write_text("---\nid: ACME-0003\nstatus: accepted\n---\n")
    (target / "ACME-0004-z.md").write_text("---\nid: ACME-0004\nstatus: draft\n---\n")
    outbox = tmp_path / "data" / "outbox" / "Acme"
    outbox.mkdir(parents=True)
    (outbox / "ACME-0002-x.md").write_text(
        f"---\nid: ACME-0002\nstatus: accepted\n"
        f"target_spec_path: {target / 'ACME-0002-x.md'}\n---\n")
    cfg["outbox_dir"] = str(tmp_path / "data" / "outbox")
    S.create_campaign(cfg, {"id": "ACME-0003-c1", "spec_id": "ACME-0003", "project": "Acme",
                            "state": "HANDED_OFF", "created_at": "2026-11-01T00:00:00"})
    assert Q._dangling(cfg, "ACME-0002", ["ACME-0003", "ACME-0004"], {"ACME-0002"}) == [
        "ACME-0004 is draft and not queued"]
