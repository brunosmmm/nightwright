"""SPEC-0325: stewards are mailed about their project's campaign transitions; a second-project
merge reconciles its idea from the project's own repo; pr-policy campaigns branch from the remote."""
import datetime as dt
import subprocess
from pathlib import Path

import pytest

from test_agentic_e2e import (_git, _git_env, added, impl_done, make_cfg, make_repo,  # noqa: F401
                              run_cli, script)
from wt.agentic import events as E
from wt.agentic import mailbox as M
from wt.agentic import queue as Q
from wt.agentic import store as S

PR = "https://gh/p/1"
READER = "mailbox:steward:Meta-Tools"


def _cfg(tmp_path):
    (tmp_path / "cfg").mkdir(exist_ok=True)
    (tmp_path / "cfg" / "mappings.yaml").write_text("{}\n")
    return {"data_dir": str(tmp_path / "data"), "config_dir": str(tmp_path / "cfg"),
            "agentic": {"lease_ttl_s": 120}, "outbox_targets": {}, "project_axis": "bucket",
            "specs_dir": str(tmp_path / "specs")}


def _campaign(cfg, cid="SPEC-0900-20261031-aaaa", state="CLOSING"):
    S.create_campaign(cfg, {"id": cid, "spec_id": cid[:9], "project": "Meta-Tools",
                            "profile": "balanced", "state": state, "repo": {"base": "main"},
                            "cost": {"total_usd": 1.25, "cap_usd": 10.0}})
    return cid


def _hand_off(cfg, cid):
    st = S.read_state(cfg, cid)
    st["close"] = {"mode": "pr", "merged_sha": None, "pr_url": PR}
    return Q._transition(cfg, st, "HANDED_OFF", "closed")


def _view(merge_sha="abc1234567890"):
    return lambda url: {"state": "MERGED", "merged_at": "2026-10-31T14:16:50Z",
                        "merge_sha": merge_sha}


def _inbox(cfg):
    return M.read_inbox(cfg, "steward:Meta-Tools")


def test_armed_steward_gets_one_info_per_transition(tmp_path):
    cfg = _cfg(tmp_path)
    cid = _campaign(cfg)
    with E.reader_lock(cfg, READER):
        _hand_off(cfg, cid)
        Q.sweep_handed_off(cfg, apply=True, view=_view())
        Q.sweep_handed_off(cfg, apply=True, view=_view())        # nothing left to sweep
        st = S.read_state(cfg, cid)
        (tr,) = [e for e in S.read_ledger(cfg, cid, types=["transition"]) if e["to"] == "MERGED"]
        Q._notify_steward(cfg, st, "MERGED", "pr_merged", tr["seq"], "sweep")   # a re-run
    msgs = _inbox(cfg)
    assert [m["kind"] for m in msgs] == ["info", "info"]
    assert all(m["needs_reply"] is False and m["campaign"] == cid for m in msgs)
    assert all(m["from"] == "conductor" for m in msgs)
    handed, merged = (m["text"] for m in msgs)
    assert "HANDED_OFF (closed)" in handed and PR in handed and "cost $1.25" in handed
    assert "MERGED (pr_merged)" in merged and "merged abc1234567" in merged
    assert "SPEC-0900" in merged and "pull" not in merged
    assert len(S.read_state(cfg, cid)["steward_notified"]) == 2
    # the conductor's stream is not woken by steward mail
    assert "message" not in [e["type"] for e in E.sync(cfg)]


def test_no_follower_no_message(tmp_path):
    cfg = _cfg(tmp_path)
    cid = _campaign(cfg)
    _hand_off(cfg, cid)
    Q.sweep_handed_off(cfg, apply=True, view=_view())
    st = S.read_state(cfg, cid)
    assert st["state"] == "MERGED" and _inbox(cfg) == [] and not st.get("steward_notified")


def test_park_and_abandon_notify_reparks_do_not(tmp_path):
    cfg = _cfg(tmp_path)
    cid = _campaign(cfg, state="IMPLEMENTING")
    with E.reader_lock(cfg, READER):
        st = Q._park(cfg, S.read_state(cfg, cid), "human", "needs a decision on X", "IMPLEMENTING")
        Q._park(cfg, st, "human", "still X", "IMPLEMENTING", count=False)   # PARKED -> PARKED
        Q._transition(cfg, S.read_state(cfg, cid), "ABANDONED", "abandoned", by="wt")
    texts = [m["text"] for m in _inbox(cfg)]
    assert len(texts) == 2
    assert "PARKED (human)" in texts[0] and "needs a decision on X" in texts[0]
    assert "ABANDONED (abandoned)" in texts[1]


def test_other_states_send_nothing(tmp_path):
    cfg = _cfg(tmp_path)
    cid = _campaign(cfg, state="IMPLEMENTING")
    with E.reader_lock(cfg, READER):
        Q._transition(cfg, S.read_state(cfg, cid), "GATING", "implemented")
    assert _inbox(cfg) == []


def test_send_failure_keeps_the_transition(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    cid = _campaign(cfg)

    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(M, "send", boom)
    with E.reader_lock(cfg, READER):
        _hand_off(cfg, cid)
        (row,) = Q.sweep_handed_off(cfg, apply=True, view=_view())
    st = S.read_state(cfg, cid)
    assert row["action"] == "merge" and st["state"] == "MERGED"
    assert not st.get("steward_notified")
    notes = [e["text"] for e in S.read_ledger(cfg, cid, types=["note"])]
    assert sum("steward notice to steward:Meta-Tools failed" in n for n in notes) == 2
    assert "disk full" in notes[-1]


# ---- Fix 2: a second-project merge reconciles its idea --------------------------------------

def _second_project(tmp_path, monkeypatch, idea_state):
    from wt import explore as EX
    from wt import org_write as OW
    from wt.org import load_tasks
    wt_repo = make_repo(tmp_path)                    # wt's own store: its SPEC-0001 is accepted
    cfg = make_cfg(tmp_path, wt_repo, script(tmp_path, []))
    cfg["org_idea_keywords"] = ["IDEA", "INCUBATE", "SPECCED", "|", "PROMOTED", "SHIPPED",
                                "DROPPED"]
    OW.add_idea(cfg, "second project idea")
    iid = [t for t in load_tasks(cfg) if t.is_idea][0].properties["ID"]
    if idea_state != "IDEA":
        EX.set_idea_state(cfg, iid, idea_state)
    proj = tmp_path / "proj"
    proj.mkdir()
    _git(proj, "init", "-q", "-b", "main")
    rel = "docs/specs/0001-proj.md"
    (proj / "docs" / "specs").mkdir(parents=True)
    tomorrow = dt.date.today() + dt.timedelta(days=1)
    (proj / rel).write_text(f"---\nid: SPEC-0001\ntitle: p\nstatus: done\ncreated: {tomorrow}\n"
                            f"source_idea: {iid}\n---\n# p\n")
    _git(proj, "add", "-A")
    _git(proj, "commit", "-q", "-m", "spec done")
    sha = _git(proj, "rev-parse", "HEAD")
    cid = "IDEA-900-20261031-bbbb"
    S.create_campaign(cfg, {"id": cid, "kind": "spec", "spec_id": "SPEC-0001",
                            "project": "comfort-assistant", "state": "HANDED_OFF",
                            "spec": {"id": "SPEC-0001", "rel_path": rel, "internal": True},
                            "repo": {"root": str(proj), "base": "main", "head": sha},
                            "close": {"mode": "pr", "merged_sha": None, "pr_url": PR},
                            "cost": {"total_usd": 0.0, "cap_usd": 10.0}})
    return cfg, cid, iid, sha, wt_repo, proj


@pytest.mark.parametrize("idea_state", ["SPECCED", "PROMOTED"])
def test_second_project_merge_ships_its_idea(tmp_path, monkeypatch, idea_state):
    from wt import org_write as OW
    cfg, cid, iid, sha, wt_repo, proj = _second_project(tmp_path, monkeypatch, idea_state)
    specs = Path(cfg["specs_dir"])
    before = {p.name: p.read_bytes() for p in specs.iterdir()}
    proj_before = (proj / "docs/specs/0001-proj.md").read_bytes()
    (row,) = Q.sweep_handed_off(cfg, apply=True, view=_view(sha))
    assert row["action"] == "merge" and S.read_state(cfg, cid)["state"] == "MERGED"
    (ev,) = S.read_ledger(cfg, cid, types=["idea_reconciled"])
    assert (ev["spec_id"], ev["idea_id"], ev["from"], ev["to"]) == ("SPEC-0001", iid, idea_state,
                                                                    "SHIPPED")
    assert OW.resolve_selector(cfg, iid).state == "SHIPPED"
    assert {p.name: p.read_bytes() for p in specs.iterdir()} == before      # wt's store untouched
    assert (proj / "docs/specs/0001-proj.md").read_bytes() == proj_before
    assert not [f for f in S.read_ledger(cfg, cid, types=["finding"]) if "not this repo" in f["title"]]


def test_second_project_merged_sha_not_local_falls_back_to_head(tmp_path, monkeypatch):
    from wt import org_write as OW
    cfg, cid, iid, sha, _w, _p = _second_project(tmp_path, monkeypatch, "SPECCED")
    Q.sweep_handed_off(cfg, apply=True, view=_view("f" * 40))     # merged on GitHub, not fetched
    assert OW.resolve_selector(cfg, iid).state == "SHIPPED"


def test_second_project_hand_off_waits(tmp_path, monkeypatch):
    from wt import org_write as OW
    cfg, cid, iid, _sha, _w, _p = _second_project(tmp_path, monkeypatch, "SPECCED")
    st = S.read_state(cfg, cid)
    events = []
    Q._reconcile_closed(cfg, st, events, "HANDED_OFF")
    assert [e[1]["title"] for e in events] == ["project idea reconcile waits for the merge"]
    assert OW.resolve_selector(cfg, iid).state == "SPECCED"


# ---- Fix 3: pr-policy campaigns branch from the remote --------------------------------------

def _advance_origin(tmp_path):
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", "-b", "main", str(tmp_path / "origin.git"), str(clone)], check=True)
    (clone / "remote.txt").write_text("ahead\n")
    _git(clone, "add", "-A")
    _git(clone, "commit", "-q", "-m", "remote ahead")
    _git(clone, "push", "-q", "origin", "main")
    return _git(clone, "rev-parse", "HEAD")


def _open(tmp_path, monkeypatch, policy):
    repo = make_repo(tmp_path, policy=policy, with_origin=True)
    tip = _advance_origin(tmp_path)
    local = _git(repo, "rev-parse", "main")
    cfg = make_cfg(tmp_path, repo, script(tmp_path, [impl_done()] * 2))
    cid = added(cfg, monkeypatch)
    run_cli(cfg, monkeypatch, ["queue", "tick"])
    return repo, cfg, cid, tip, local


@pytest.mark.parametrize("policy", ["pr", "draft-pr"])
def test_pr_policy_opens_from_the_remote_tip(tmp_path, monkeypatch, policy):
    repo, cfg, cid, tip, local = _open(tmp_path, monkeypatch, policy)
    st = S.read_state(cfg, cid)
    assert st["state"] != "PARKED", st.get("park")
    assert st["repo"]["base_sha_at_open"] == tip != local
    wt = st["repo"]["worktree"]
    subprocess.run(["git", "-C", wt, "merge-base", "--is-ancestor", tip, "HEAD"], check=True)
    assert _git(repo, "rev-parse", "main") == local            # the human's branch is untouched
    # started from the sha, so the campaign branch has no upstream
    assert subprocess.run(["git", "-C", wt, "rev-parse", "--abbrev-ref", "@{u}"],
                          capture_output=True).returncode != 0


def test_merge_policy_still_opens_from_local_base(tmp_path, monkeypatch):
    repo, cfg, cid, tip, local = _open(tmp_path, monkeypatch, "merge")
    st = S.read_state(cfg, cid)
    assert st["repo"]["base_sha_at_open"] == local != tip


def test_pr_policy_fetch_failure_parks_open_failed(tmp_path, monkeypatch):
    repo = make_repo(tmp_path, policy="pr", with_origin=True)
    _git(repo, "remote", "set-url", "origin", str(tmp_path / "missing.git"))
    cfg = make_cfg(tmp_path, repo, script(tmp_path, [impl_done()]))
    cid = added(cfg, monkeypatch)
    run_cli(cfg, monkeypatch, ["queue", "tick"])
    st = S.read_state(cfg, cid)
    assert st["state"] == "PARKED" and st["park"]["reason"] == "open_failed"
    assert "cannot fetch origin/main for a pr campaign" in st["park"]["detail"]
    assert not Path(st["repo"]["worktree"]).exists()
