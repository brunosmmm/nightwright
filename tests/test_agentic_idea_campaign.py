"""SPEC-0209: `wt queue add IDEA-NNN` — idea campaigns that open into SPEC_WRITING, the
no-spec-yet tolerance of the queue views, and the unlink on abandon. Uses the SPEC-0176
end-to-end harness (temp repo as project, fake runner, temp org)."""
import json
import threading

import pytest

from wt import org as ORG
from wt import org_write as OW
from wt import render as R
from wt import spec_numbers as SN
from wt.agentic import events as E
from wt.agentic import queue as Q
from wt.agentic import store as S

from test_agentic_e2e import (_git_env, make_cfg, make_repo, run_cli,  # noqa: F401
                             script)


@pytest.fixture
def env(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, []))
    monkeypatch.setattr("wt.cli.load_config", lambda: cfg)
    return cfg, repo


def capture(cfg, text="Add a thing", **kw):
    _, _, iid = OW.add_idea(cfg, text, **kw)
    return iid


def idea(cfg, iid):
    ORG.invalidate_tasks_cache()
    return Q._idea_task(cfg, iid)


def add_idea_campaign(cfg, monkeypatch, iid, *extra):
    r = run_cli(cfg, monkeypatch, ["queue", "add", iid, "--yes", "--json", *extra])
    assert r.exit_code == 0, r.output
    d = json.loads(r.output)
    return d["campaign"]


def ticks(cfg, n=2):
    for _ in range(n):
        Q.tick(cfg)


@pytest.fixture
def parked(env, monkeypatch):
    """An idea campaign opened into SPEC_WRITING and parked `spec_write_failed` (SPEC-0215: its
    `:EPIC:` names no epic spec in the fixture repo, so the writer never runs)."""
    cfg, repo = env
    iid = capture(cfg, epic="SPEC-0169")
    cid = add_idea_campaign(cfg, monkeypatch, iid)
    ticks(cfg)
    return cfg, repo, iid, cid


# ---- AC1: add ------------------------------------------------------------------------------

def test_add_idea_dry_run_then_write(env, monkeypatch):
    cfg, _ = env
    iid = capture(cfg, epic="SPEC-0169")
    r = run_cli(cfg, monkeypatch, ["queue", "add", iid, "--priority", "2", "--profile",
                                   "balanced", "--json"])
    assert r.exit_code == 0, r.output
    plan = json.loads(r.output)
    assert plan["schema"] == "wt.queue.add.v1" and plan["written"] is False
    for k in ("kind", "idea_id", "project", "profile", "priority", "campaign", "branch",
              "worktree"):
        assert k in plan
    assert plan["kind"] == "idea" and plan["idea_id"] == iid and plan["priority"] == 2
    assert S.list_campaigns(cfg) == []                                # dry run wrote nothing
    assert "CAMPAIGN" not in idea(cfg, iid).properties

    cid = add_idea_campaign(cfg, monkeypatch, iid, "--priority", "2", "--profile", "balanced")
    st = S.read_state(cfg, cid)
    assert st["state"] == "QUEUED" and st["kind"] == "idea" and st["spec_id"] is None
    assert st["priority"] == 2 and st["profile"] == "balanced"
    assert st["spec"] == {"id": None, "rel_path": None, "internal": True}
    assert st["idea"]["id"] == iid and st["idea"]["heading"] == "Add a thing"
    assert st["idea"]["epic"] == "SPEC-0169" and st["idea"]["state_before"] == "IDEA"
    assert st["repo"]["branch"] == f"wt/{cid}"
    assert idea(cfg, iid).properties["CAMPAIGN"] == cid


# ---- AC2: refusals -------------------------------------------------------------------------

def test_add_idea_refusals(env, monkeypatch):
    cfg, _ = env

    def refused(iid):
        r = run_cli(cfg, monkeypatch, ["queue", "add", iid, "--yes"])
        assert r.exit_code != 0
        return r.output

    assert "unknown idea IDEA-999" in refused("IDEA-999")

    dropped = capture(cfg, "dropped one")
    OW.set_state(cfg, idea(cfg, dropped), "DROPPED")
    assert f"{dropped} is DROPPED" in refused(dropped)

    specced = capture(cfg, "has a spec")
    OW.set_property(cfg, idea(cfg, specced), "SPEC", "SPEC-0100")
    OW.set_state(cfg, idea(cfg, specced), "SPECCED")
    out = refused(specced)
    assert "queue that spec instead: wt queue add SPEC-0100" in out
    assert "only open ideas" not in out

    badproj = capture(cfg, "no such project", project="NoSuchProject")
    assert "cannot resolve a repo for project 'NoSuchProject'" in refused(badproj)
    assert S.list_campaigns(cfg) == []

    dup = capture(cfg, "dup")
    cid = add_idea_campaign(cfg, monkeypatch, dup)
    assert f"already has an active campaign {cid}" in refused(dup)
    # an abandoned campaign does not block
    Q.apply_abandon(cfg, Q.plan_abandon(cfg, cid))
    add_idea_campaign(cfg, monkeypatch, dup)


def test_closed_idea_message(env):
    cfg, _ = env
    iid = capture(cfg, "closed")
    OW.set_state(cfg, idea(cfg, iid), "DROPPED")
    with pytest.raises(ValueError) as e:
        Q.plan_add_idea(cfg, iid)
    assert f"{iid} is DROPPED" in str(e.value)


# ---- AC3: concurrency ----------------------------------------------------------------------

def test_concurrent_apply_add_idea_creates_one_campaign(env):
    cfg, _ = env
    iid = capture(cfg)
    plans = [Q.plan_add_idea(cfg, iid), Q.plan_add_idea(cfg, iid)]
    barrier = threading.Barrier(2)
    outcomes = []

    def go(plan):
        barrier.wait()
        try:
            outcomes.append(Q.apply_add_idea(cfg, plan)["id"])
        except ValueError as e:
            outcomes.append(e)

    threads = [threading.Thread(target=go, args=(p,)) for p in plans]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    errs = [o for o in outcomes if isinstance(o, ValueError)]
    assert len(errs) == 1 and "already has an active campaign" in str(errs[0])
    assert len(S.list_campaigns(cfg)) == 1


def test_apply_add_holds_add_lock(env, monkeypatch):
    cfg, _ = env
    held = []
    real = S.create_campaign

    def spy(c, state, **kw):
        # the add lock is held (reentrantly) here: its process lock is owned by this thread
        rlock, meta = S._process_lock(S.agentic_dir(c) / "add.lock")
        held.append(meta["depth"])
        return real(c, state, **kw)

    monkeypatch.setattr(S, "create_campaign", spy)
    Q.apply_add(cfg, Q.plan_add(cfg, "SPEC-0001"))
    assert held == [1]


# ---- AC4: legacy spec campaigns are not "no spec yet" -----------------------------------------

def test_legacy_spec_campaign_state_is_not_specless(env, monkeypatch):
    cfg, repo = env
    legacy = {"kind": "implement", "spec_id": "SPEC-0001", "spec": {"rel_path": "docs/specs/0001-trivial.md"},
              "repo": {"root": str(repo)}}
    assert not Q.has_no_spec(legacy)
    assert Q.backlog_readiness(cfg, legacy) == (True, [])
    legacy["spec"]["rel_path"] = "docs/specs/missing.md"
    assert Q.backlog_readiness(cfg, legacy) == (False, ["spec file missing"])
    # open still hashes and copies the spec
    cid = Q.apply_add(cfg, Q.plan_add(cfg, "SPEC-0001"))["id"]
    st = S.read_state(cfg, cid)
    st.pop("kind")
    Q._transition(cfg, st, "OPENING", "admitted")
    res = Q._do_open(Q._Ctx(cfg), st)
    assert res.error is None
    assert st["spec"]["sha256_at_open"]
    assert (S.campaign_dir(cfg, cid) / "spec_at_open.md").is_file()


# ---- AC5: readiness ------------------------------------------------------------------------

def test_backlog_ready_until_idea_closed(env, monkeypatch):
    cfg, _ = env
    iid = capture(cfg)
    cid = add_idea_campaign(cfg, monkeypatch, iid)
    row = Q.backlog_view(cfg)[0]
    assert row["campaign"] == cid and row["ready"] is True and row["idea_id"] == iid
    OW.set_state(cfg, idea(cfg, iid), "INCUBATE")
    assert Q.backlog_view(cfg)[0]["ready"] is True
    OW.set_state(cfg, idea(cfg, iid), "DROPPED")
    ready, blocked = Q.backlog_readiness(cfg, S.read_state(cfg, cid))
    assert not ready and blocked and iid in blocked[0]
    st = S.read_state(cfg, cid)
    st["idea"]["id"] = "IDEA-998"
    assert Q.backlog_readiness(cfg, st) == (False, ["idea IDEA-998 not found"])


# ---- AC6: open + first tick ---------------------------------------------------------------------

def test_opens_into_spec_writing_then_parks(env, monkeypatch):
    cfg, _ = env
    iid = capture(cfg)
    cid = add_idea_campaign(cfg, monkeypatch, iid)
    Q.tick(cfg)
    st = S.read_state(cfg, cid)
    assert st["state"] == "SPEC_WRITING" and not st["spec"].get("sha256_at_open")
    assert not (S.campaign_dir(cfg, cid) / "spec_at_open.md").exists()
    assert st["state"] in Q.ACTIVE_STATES and "SPEC_WRITING" in Q.RESUMABLE_STATES
    Q.tick(cfg)
    st = S.read_state(cfg, cid)
    assert st["state"] == "PARKED"
    assert st["park"]["reason"] == "spec_write_failed"      # SPEC-0215 replaced the old park
    assert "fake runner script exhausted" in st["park"]["detail"]   # the writer ran, no script
    assert st["park"]["resume_to"] == "SPEC_WRITING"


def test_render_style(env):
    assert R.STATE_STYLES["SPEC_WRITING"]


# ---- AC7: specless tolerance -----------------------------------------------------------------------

@pytest.mark.parametrize("state", ["SPEC_WRITING", "PARKED"])
def test_specless_tolerance(env, monkeypatch, state):
    cfg, _ = env
    iid = capture(cfg)
    cid = add_idea_campaign(cfg, monkeypatch, iid)
    Q.tick(cfg)
    if state == "PARKED":
        Q.tick(cfg)
    assert S.read_state(cfg, cid)["state"] == state
    for args in (["queue", "list", "--json"], ["queue", "list"], ["queue", "backlog"],
                 ["queue", "watch", "--once"], ["queue", "watch", "--once", "--json"],
                 ["campaign", "show", cid], ["campaign", "show", cid, "--json"],
                 ["campaign", "log", cid], ["campaign", "list"],
                 ["queue", "conduct-status"], ["queue", "conduct-status", "--json"]):
        r = run_cli(cfg, monkeypatch, args)
        assert r.exit_code == 0, (args, r.output, r.exception)
    r = run_cli(cfg, monkeypatch, ["queue", "list"])
    assert iid in r.output
    payload = Q.queue_payload(cfg)
    assert payload["items"][0]["idea_id"] == iid
    Q.dashboard_payload(cfg)
    E.sync(cfg)
    E.watchdog_checks(cfg)
    E.ping_digest(cfg)
    from wt.specs import reconcile_ideas
    reconcile_ideas(cfg)


# ---- AC8: abandon ----------------------------------------------------------------------------------

def _abandon(cfg, cid):
    return Q.apply_abandon(cfg, Q.plan_abandon(cfg, cid))


def test_abandon_restores_idea_and_unlinks(parked, monkeypatch):
    cfg, repo, iid, cid = parked
    st = S.read_state(cfg, cid)
    st["spec"]["id"] = "SPEC-0300"
    S.write_state(cfg, cid, st)
    OW.set_property(cfg, idea(cfg, iid), "SPEC", "SPEC-0300")
    OW.set_state(cfg, idea(cfg, iid), "SPECCED")
    specs = repo / "docs" / "specs"
    mine = SN.allocate(cfg, by="t", campaign=cid, specs_dir=specs)
    other = SN.allocate(cfg, by="t", campaign="other-campaign", specs_dir=specs)
    assert {r["number"] for r in SN.list_reservations(cfg, specs_dir=specs)} == {
        mine["number"], other["number"]}

    assert _abandon(cfg, cid)["state"] == "ABANDONED"
    t = idea(cfg, iid)
    assert t.state == "IDEA"                                           # restored
    assert "CAMPAIGN" not in t.properties                              # unlinked
    assert "SPEC" not in t.properties                                  # own :SPEC: cleared
    left = {r["number"] for r in SN.list_reservations(cfg, specs_dir=specs)}
    assert left == {other["number"]}                                   # mine released
    # the idea can be queued again
    add_idea_campaign(cfg, monkeypatch, iid)


def test_abandon_keeps_hand_set_spec(parked):
    cfg, _, iid, cid = parked
    st = S.read_state(cfg, cid)
    st["spec"]["id"] = "SPEC-0300"
    S.write_state(cfg, cid, st)
    OW.set_property(cfg, idea(cfg, iid), "SPEC", "SPEC-0777")
    OW.set_state(cfg, idea(cfg, iid), "SPECCED")
    _abandon(cfg, cid)
    t = idea(cfg, iid)
    assert t.properties.get("SPEC") == "SPEC-0777" and "CAMPAIGN" not in t.properties
    assert t.state == "SPECCED"                                        # not restored: hand-set


def test_abandon_keeps_hand_set_state(parked):
    cfg, _, iid, cid = parked
    OW.set_state(cfg, idea(cfg, iid), "INCUBATE")                      # hand-moved, not DROPPED
    OW.set_state(cfg, idea(cfg, iid), "DROPPED")
    _abandon(cfg, cid)
    t = idea(cfg, iid)
    assert t is None or (t.state == "DROPPED" and "CAMPAIGN" not in t.properties)
    notes = [e["text"] for e in S.read_ledger(cfg, cid, types=["note"])]
    assert "idea state kept: DROPPED" in notes


def test_abandon_waiting_campaign(env, monkeypatch):
    cfg, _ = env
    iid = capture(cfg)
    cid = add_idea_campaign(cfg, monkeypatch, iid)
    Q.tick(cfg)                                    # opened into SPEC_WRITING
    st = S.read_state(cfg, cid)
    Q._enter_slot_or_wait(cfg, st, "SPEC_WRITING", "slot_test", busy=1, slots=1)
    st = S.read_state(cfg, cid)
    assert st["state"] == "WAITING" and st["waiting"]["resume_to"] == "SPEC_WRITING"
    out = _abandon(cfg, cid)
    assert out["state"] == "ABANDONED" and "waiting" not in out
    assert idea(cfg, iid).state == "IDEA" and "CAMPAIGN" not in idea(cfg, iid).properties


# ---- SPEC-0237: idea campaigns see the project's profile layer -----------------------------

def _project_profile(repo, name, text):
    d = repo / ".wt" / "profiles"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.yaml").write_text("schema: wt.profile.v1\n" + text, encoding="utf-8")


def test_idea_campaign_records_the_project_profile_cap(env, monkeypatch):
    cfg, repo = env
    _project_profile(repo, "balanced", "campaign_cap_usd: 25.0\nloop_max: 5\n")
    plan = Q.plan_add_idea(cfg, capture(cfg))
    assert plan["cap_usd"] == 25.0 and plan["loop_max"] == 5
    cid = add_idea_campaign(cfg, monkeypatch, capture(cfg, "second"))
    assert S.read_state(cfg, cid)["cost"]["cap_usd"] == 25.0


def test_idea_campaign_accepts_a_project_only_profile(env, monkeypatch):
    cfg, repo = env
    iid = capture(cfg)
    with pytest.raises(ValueError, match="unknown profile 'careful'"):
        Q.plan_add_idea(cfg, iid, profile="careful")
    _project_profile(repo, "careful", "extends: balanced\ncampaign_cap_usd: 4.0\n")
    assert Q.plan_add_idea(cfg, iid, profile="careful")["cap_usd"] == 4.0
    cid = add_idea_campaign(cfg, monkeypatch, iid, "--profile", "careful")
    assert S.read_state(cfg, cid)["profile"] == "careful"
