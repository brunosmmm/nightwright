"""SPEC-0423: `--after` ordering of idea campaigns, `after_met`, the spec writer's `depends_on`
must-include, Breakdown `(depends_on child N)` parsing and the batch order. The real-git, real-tick
walk through is `test_idea_ordering_e2e.py`."""
import json

import pytest

from nightwright import specs as SP
from nightwright.agentic import queue as Q
from nightwright.agentic import quiet as QT
from nightwright.agentic import spec_write as SW
from nightwright.agentic import store as S

from test_agentic_e2e import run_cli
from test_agentic_idea_campaign import add_idea_campaign, capture, env  # noqa: F401


def _state(cfg, cid):
    return S.read_state(cfg, cid)


def _merge(cfg, cid):
    st = _state(cfg, cid)
    Q._transition(cfg, st, "MERGED", "test")


# ---- AC1 / AC2: storage and readiness -------------------------------------------------------

def test_after_idea_waits_then_is_ready_when_prerequisite_merges(env, monkeypatch):
    cfg, repo = env
    a, b = capture(cfg, "First"), capture(cfg, "Second")
    ca = add_idea_campaign(cfg, monkeypatch, a)
    cb = add_idea_campaign(cfg, monkeypatch, b, "--after", a)
    st = _state(cfg, cb)
    assert st["after"] == [{"ref": a, "kind": "idea"}]
    assert "after" not in _state(cfg, ca)               # a campaign without --after is untouched
    assert any("after: " in e["text"] for e in S.read_ledger(cfg, cb, types=["note"]))
    ready, blocked = Q.backlog_readiness(cfg, st)
    assert not ready and blocked == [f"waiting on {a} (after: campaign {ca} is QUEUED)"]
    rows = {r["campaign"]: r for r in Q.backlog_view(cfg)}
    assert rows[cb]["waiting_on"] == [a] and rows[cb]["after"] == [a] and "after" not in rows[ca]
    _merge(cfg, ca)
    assert Q.backlog_readiness(cfg, _state(cfg, cb)) == (True, [])


def test_live_idea_after_does_not_reread_the_idea_file(env, monkeypatch):
    """SPEC-0454: a prerequisite that already has a campaign is answered from that
    campaign. The idea corpus is not loaded."""
    cfg, _repo = env
    a, b = capture(cfg, "First"), capture(cfg, "Second")
    add_idea_campaign(cfg, monkeypatch, a)
    cb = add_idea_campaign(cfg, monkeypatch, b, "--after", a)
    calls = []
    real = Q._idea_task

    def wrapped(cfg, idea_id):
        calls.append(idea_id)
        return real(cfg, idea_id)

    monkeypatch.setattr(Q, "_idea_task", wrapped)
    ready, blocked = Q.backlog_readiness(cfg, _state(cfg, cb))
    assert not ready and calls == [] and "campaign" in blocked[0]


def test_after_spec_and_campaign_refs(env, monkeypatch):
    cfg, repo = env
    a, b, c = capture(cfg, "A"), capture(cfg, "B"), capture(cfg, "C")
    ca = add_idea_campaign(cfg, monkeypatch, a)
    cb = add_idea_campaign(cfg, monkeypatch, b, "--after", "SPEC-0001")
    cc = add_idea_campaign(cfg, monkeypatch, c, "--after", ca)
    assert _state(cfg, cb)["after"] == [{"ref": "SPEC-0001", "kind": "spec"}]
    assert _state(cfg, cc)["after"] == [{"ref": ca, "kind": "campaign"}]
    ready, blocked = Q.backlog_readiness(cfg, _state(cfg, cb))
    assert not ready and blocked == ["waiting on SPEC-0001 (after: accepted)"]
    spec = repo / "docs/specs/0001-trivial.md"
    spec.write_text(spec.read_text().replace("status: accepted", "status: done"))
    assert Q.backlog_readiness(cfg, _state(cfg, cb)) == (True, [])
    assert Q.backlog_readiness(cfg, _state(cfg, cc))[1] == [
        f"waiting on {ca} (after: campaign {ca} is QUEUED)"]
    # an abandoned prerequisite is not an ordinary wait: the conductor must see it
    st = _state(cfg, ca)
    Q._transition(cfg, st, "ABANDONED", "test")
    ready, blocked = Q.backlog_readiness(cfg, _state(cfg, cc))
    assert not ready and blocked == [f"after {ca}: campaign {ca} abandoned"]
    assert not QT._benign(blocked[0])
    # ... and an idea prerequisite whose newest campaign is abandoned, the same
    cd = add_idea_campaign(cfg, monkeypatch, capture(cfg, "D"), "--after", a)
    assert Q.backlog_readiness(cfg, _state(cfg, cd))[1] == [
        f"after {a}: campaign {ca} abandoned"]


def test_unknown_after_is_refused_at_add(env, monkeypatch):
    cfg, repo = env
    iid = capture(cfg, "A")
    for ref in ("IDEA-999", "SPEC-9999", "nonsense-campaign"):
        r = run_cli(cfg, monkeypatch, ["queue", "add", iid, "--after", ref, "--yes"])
        assert r.exit_code != 0 and "--after" in r.output and "not found" in r.output, ref
    assert S.list_campaigns(cfg) == []
    r = run_cli(cfg, monkeypatch, ["queue", "add", iid, "--after", iid, "--yes"])
    assert r.exit_code != 0 and "itself" in r.output


def test_dry_run_shows_after_and_writes_nothing(env, monkeypatch):
    cfg, repo = env
    a, b = capture(cfg, "A"), capture(cfg, "B")
    ca = add_idea_campaign(cfg, monkeypatch, a)
    r = run_cli(cfg, monkeypatch, ["queue", "add", b, "--after", a])
    assert r.exit_code == 0 and f"{a} (idea, campaign {ca} is QUEUED)" in " ".join(r.output.split())
    assert len(S.list_campaigns(cfg)) == 1


def test_merge_campaigns_ignore_after(env):
    cfg, repo = env
    st = {"kind": "merge", "repo": {"root": str(repo), "branch": "main"},
          "after": [{"ref": "IDEA-1", "kind": "idea"}]}
    assert Q.backlog_readiness(cfg, st) == (True, [])


def test_spec_campaign_after_wins_over_spec_checks(env, monkeypatch):
    cfg, repo = env
    a = capture(cfg, "A")
    add_idea_campaign(cfg, monkeypatch, a)
    r = run_cli(cfg, monkeypatch, ["queue", "add", "SPEC-0001", "--after", a, "--yes", "--json"])
    assert r.exit_code == 0, r.output
    st = _state(cfg, json.loads(r.output)["campaign"])
    ready, blocked = Q.backlog_readiness(cfg, st)
    assert not ready and blocked[0].startswith(f"waiting on {a} (after:")


def test_queue_after_command(env, monkeypatch):
    cfg, repo = env
    a, b = capture(cfg, "A"), capture(cfg, "B")
    ca = add_idea_campaign(cfg, monkeypatch, a)
    cb = add_idea_campaign(cfg, monkeypatch, b)
    r = run_cli(cfg, monkeypatch, ["queue", "after", cb, "--add", a])
    assert r.exit_code == 0 and "after" not in _state(cfg, cb)          # dry-run
    r = run_cli(cfg, monkeypatch, ["queue", "after", cb, "--add", a, "--yes"])
    assert r.exit_code == 0 and _state(cfg, cb)["after"] == [{"ref": a, "kind": "idea"}]
    assert not Q.backlog_readiness(cfg, _state(cfg, cb))[0]
    r = run_cli(cfg, monkeypatch, ["queue", "after", cb, "--remove", "IDEA-777", "--yes"])
    assert r.exit_code != 0
    r = run_cli(cfg, monkeypatch, ["queue", "after", cb, "--remove", a, "--yes"])
    assert r.exit_code == 0 and "after" not in _state(cfg, cb)
    assert Q.backlog_readiness(cfg, _state(cfg, cb))[0]
    Q._transition(cfg, _state(cfg, ca), "ABANDONED", "t")
    r = run_cli(cfg, monkeypatch, ["queue", "after", ca, "--add", b, "--yes"])
    assert r.exit_code != 0 and "ABANDONED" in r.output


def test_campaign_without_after_is_unchanged(env, monkeypatch):
    cfg, repo = env
    ca = add_idea_campaign(cfg, monkeypatch, capture(cfg, "A"))
    assert "after" not in _state(cfg, ca)
    assert Q.backlog_readiness(cfg, _state(cfg, ca)) == (True, [])
    row = Q.backlog_view(cfg)[0]
    assert "after" not in row


# ---- SPEC-0463: dependency conflict resolution (AC1–AC3) -----------------------------------

def test_idea_after_holds_on_main_done_file_until_merged(env, monkeypatch):
    cfg, repo = env
    a = capture(cfg, "First")
    ca = add_idea_campaign(cfg, monkeypatch, a)
    st = _state(cfg, ca)
    st["state"] = "REVIEWING"
    st["spec_id"] = "SPEC-0001"
    S.write_state(cfg, ca, st)
    spec = repo / "docs/specs/0001-trivial.md"
    spec.write_text(spec.read_text().replace("status: accepted", "status: done"))
    b = capture(cfg, "Second")
    cb = add_idea_campaign(cfg, monkeypatch, b, "--after", a)
    wait = f"waiting on {ca} (after: campaign {ca} is REVIEWING)"
    blocked = Q.backlog_readiness(cfg, _state(cfg, cb))[1]
    assert blocked == [wait]
    ctx = QT.Ctx(cfg, "conductor")
    assert QT.dependency_wait(blocked, ctx.live_specs, _state(cfg, cb)["project"])
    Q._transition(cfg, _state(cfg, ca), "HANDED_OFF", "test")
    handed = f"waiting on {ca} (after: campaign {ca} is HANDED_OFF)"
    blocked = Q.backlog_readiness(cfg, _state(cfg, cb))[1]
    assert blocked == [handed]
    ctx2 = QT.Ctx(cfg, "conductor")
    live = {x[1] for x in ctx2.live_specs if x[0] == _state(cfg, cb)["project"]}
    assert ca not in live
    assert QT.dependency_wait(blocked, ctx2.live_specs, _state(cfg, cb)["project"])
    _merge(cfg, ca)
    assert Q.backlog_readiness(cfg, _state(cfg, cb)) == (True, [])


def test_abandoned_idea_ref_stays_abandoned_when_main_spec_is_done(env, monkeypatch):
    cfg, repo = env
    a = capture(cfg, "Abandoned prereq")
    ca = add_idea_campaign(cfg, monkeypatch, a)
    st = _state(cfg, ca)
    st["spec_id"] = "SPEC-0001"
    S.write_state(cfg, ca, st)
    spec = repo / "docs/specs/0001-trivial.md"
    spec.write_text(spec.read_text().replace("status: accepted", "status: done"))
    Q._transition(cfg, _state(cfg, ca), "ABANDONED", "test")
    cd = add_idea_campaign(cfg, monkeypatch, capture(cfg, "Waiter"), "--after", a)
    blocked = Q.backlog_readiness(cfg, _state(cfg, cd))[1]
    assert blocked == [f"after {a}: campaign {ca} abandoned"]
    assert not QT.dependency_wait(blocked, QT.Ctx(cfg, "conductor").live_specs,
                                  _state(cfg, cd)["project"])


def test_idea_after_holds_handed_off_when_merged_campaign_reused_spec_id(env, monkeypatch):
    """Wait on idea A while A is HANDED_OFF but a newer MERGED row reused the same spec_id."""
    cfg, repo = env
    a = capture(cfg, "Prereq")
    ca = add_idea_campaign(cfg, monkeypatch, a)
    shared = "SPEC-0446"
    st = _state(cfg, ca)
    st["spec_id"] = shared
    st["state"] = "HANDED_OFF"
    S.write_state(cfg, ca, st)
    cb = add_idea_campaign(cfg, monkeypatch, capture(cfg, "merged reuse"))
    st_b = _state(cfg, cb)
    st_b["state"] = "MERGED"
    st_b["created_at"] = "2026-11-08T00:00:01"
    S.write_state(cfg, cb, st_b)
    st_a = _state(cfg, ca)
    st_a["created_at"] = "2026-11-07T00:00:00"
    S.write_state(cfg, ca, st_a)
    done_path = repo / "docs/specs/0446-sdk-model-effort-catalog.md"
    done_path.parent.mkdir(parents=True, exist_ok=True)
    done_path.write_text("---\nid: SPEC-0446\nstatus: done\n---\n\n", encoding="utf-8")
    waiter = add_idea_campaign(cfg, monkeypatch, capture(cfg, "waiter"), "--after", a)
    handed = f"waiting on {ca} (after: campaign {ca} is HANDED_OFF)"
    blocked = Q.backlog_readiness(cfg, _state(cfg, waiter))[1]
    assert blocked == [handed]
    ctx = QT.Ctx(cfg, "conductor")
    assert ca not in {x[1] for x in ctx.live_specs if x[0] == _state(cfg, waiter)["project"]}
    assert QT.dependency_wait(blocked, ctx.live_specs, _state(cfg, waiter)["project"])


def test_spec_ref_not_found_when_newest_carrier_is_abandoned(env, monkeypatch):
    cfg, _repo = env
    dep = "SPEC-0407"
    older = add_idea_campaign(cfg, monkeypatch, capture(cfg, "older carrier"))
    st_old = _state(cfg, older)
    st_old["spec_id"] = dep
    st_old["state"] = "QUEUED"
    S.write_state(cfg, older, st_old)
    newer = add_idea_campaign(cfg, monkeypatch, capture(cfg, "merged carrier"))
    st_new = _state(cfg, newer)
    st_new["spec_id"] = dep
    st_new["state"] = "ABANDONED"
    S.write_state(cfg, newer, st_new)
    waiter = add_idea_campaign(cfg, monkeypatch, capture(cfg, "spec waiter"))
    st_w = _state(cfg, waiter)
    st_w["after"] = [{"ref": dep, "kind": "spec"}]
    S.write_state(cfg, waiter, st_w)
    blocked = Q.backlog_readiness(cfg, _state(cfg, waiter))[1]
    assert blocked == [f"after {dep} not found"]
    assert "waiting on" not in blocked[0]


def test_after_missing_spec_waits_on_carrying_campaign(env, monkeypatch):
    cfg, _repo = env
    dep = "SPEC-0407"
    a, b = capture(cfg, "Prereq A"), capture(cfg, "Prereq B")
    ca = add_idea_campaign(cfg, monkeypatch, a)
    cb = add_idea_campaign(cfg, monkeypatch, b)
    for cid in (ca, cb):
        st = _state(cfg, cid)
        st["spec_id"] = dep
        S.write_state(cfg, cid, st)
    waiter_idea = capture(cfg, "Waiter idea")
    cw = add_idea_campaign(cfg, monkeypatch, waiter_idea, "--after", a)
    waiter_spec = add_idea_campaign(cfg, monkeypatch, capture(cfg, "Waiter spec"))
    st_ws = _state(cfg, waiter_spec)
    st_ws["after"] = [{"ref": dep, "kind": "spec"}]
    S.write_state(cfg, waiter_spec, st_ws)
    st = _state(cfg, ca)
    want_idea = f"waiting on {ca} (after: campaign {ca} is QUEUED)"
    assert Q.backlog_readiness(cfg, _state(cfg, cw))[1] == [want_idea]
    assert "not found" not in Q.backlog_readiness(cfg, _state(cfg, cw))[1][0]
    assert QT.dependency_wait(Q.backlog_readiness(cfg, _state(cfg, cw))[1],
                            QT.Ctx(cfg, "conductor").live_specs, st["project"])
    # spec ref follows list_campaigns' first hit for the id (cb if newer)
    hits = S.list_campaigns(cfg, spec_id=dep)
    first = hits[0]["id"]
    want_spec = f"waiting on {first} (after: campaign {first} is QUEUED)"
    blocked_spec = Q.backlog_readiness(cfg, _state(cfg, waiter_spec))[1]
    assert blocked_spec == [want_spec]
    assert "not found" not in blocked_spec[0]
    Q._transition(cfg, _state(cfg, ca), "ABANDONED", "test")
    Q._transition(cfg, _state(cfg, cb), "ABANDONED", "test")
    abandoned = Q.backlog_readiness(cfg, _state(cfg, waiter_spec))[1]
    assert abandoned == [f"after {dep} not found"]
    assert not QT.dependency_wait(abandoned, QT.Ctx(cfg, "conductor").live_specs,
                                  _state(cfg, waiter_spec)["project"])


def test_idea_ref_holds_when_merged_campaign_reuses_spec_id_no_file(env, monkeypatch):
    cfg, _repo = env
    dep = "SPEC-0407"
    a = capture(cfg, "Prereq")
    ca = add_idea_campaign(cfg, monkeypatch, a)
    st = _state(cfg, ca)
    st["spec_id"] = dep
    st["state"] = "QUEUED"
    S.write_state(cfg, ca, st)
    newer = add_idea_campaign(cfg, monkeypatch, capture(cfg, "merged carrier"))
    st_new = _state(cfg, newer)
    st_new["spec_id"] = dep
    S.write_state(cfg, newer, st_new)
    _merge(cfg, newer)
    waiter = add_idea_campaign(cfg, monkeypatch, capture(cfg, "waiter"), "--after", a)
    want = f"waiting on {ca} (after: campaign {ca} is QUEUED)"
    blocked = Q.backlog_readiness(cfg, _state(cfg, waiter))[1]
    assert blocked == [want]
    assert "not found" not in blocked[0]
    hits = S.list_campaigns(cfg, spec_id=dep, include_terminal=True)
    assert hits[0]["id"] == newer
    assert QT.dependency_wait(blocked, QT.Ctx(cfg, "conductor").live_specs, st["project"])


def test_idea_ref_holds_when_file_not_done_and_merged_reuses_spec_id(env, monkeypatch):
    cfg, repo = env
    dep = "SPEC-0407"
    path = repo / "docs/specs/0407-on-main.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\nid: SPEC-0407\nstatus: accepted\n---\n\nx\n", encoding="utf-8")
    a = capture(cfg, "Prereq")
    ca = add_idea_campaign(cfg, monkeypatch, a)
    st = _state(cfg, ca)
    st["spec_id"] = dep
    st["state"] = "REVIEWING"
    S.write_state(cfg, ca, st)
    newer = add_idea_campaign(cfg, monkeypatch, capture(cfg, "merged carrier"))
    st_new = _state(cfg, newer)
    st_new["spec_id"] = dep
    S.write_state(cfg, newer, st_new)
    _merge(cfg, newer)
    waiter = add_idea_campaign(cfg, monkeypatch, capture(cfg, "waiter"), "--after", a)
    want = f"waiting on {ca} (after: campaign {ca} is REVIEWING)"
    blocked = Q.backlog_readiness(cfg, _state(cfg, waiter))[1]
    assert blocked == [want]
    assert QT.dependency_wait(blocked, QT.Ctx(cfg, "conductor").live_specs, st["project"])


def test_spec_ref_done_met_while_other_campaign_carries_id(env, monkeypatch):
    cfg, repo = env
    a = capture(cfg, "Carrier")
    ca = add_idea_campaign(cfg, monkeypatch, a)
    st = _state(cfg, ca)
    st["state"] = "IMPLEMENTING"
    st["spec_id"] = "SPEC-0001"
    S.write_state(cfg, ca, st)
    spec = repo / "docs/specs/0001-trivial.md"
    spec.write_text(spec.read_text().replace("status: accepted", "status: done"))
    cb = add_idea_campaign(cfg, monkeypatch, capture(cfg, "Waiter"), "--after", "SPEC-0001")
    assert Q.backlog_readiness(cfg, _state(cfg, cb)) == (True, [])


# ---- AC3: the spec writer --------------------------------------------------------------------

def test_write_rules_and_front_matter_must_depend_on():
    sw = {"path": "docs/specs/0005-x.md", "number": 5,
          "must_depend_on": [{"spec_id": "SPEC-0003", "ref": "IDEA-1"}]}
    rules = SW.write_rules("write", sw, {"id": "IDEA-2"}, None)
    assert "`depends_on` must include: SPEC-0003 (prerequisite IDEA-1)" in rules
    assert "must include" not in SW.write_rules(
        "write", {**sw, "must_depend_on": []}, {"id": "IDEA-2"}, None)
    fm = {"id": "SPEC-0005", "status": "proposed", "source_idea": "IDEA-2", "owner": "user"}
    idea = {"id": "IDEA-2"}
    want = "front matter: depends_on must include SPEC-0003 (prerequisite IDEA-1)"
    assert SW._front_matter_problems(fm, "SPEC-0005", idea, sw["must_depend_on"]) == [want]
    assert SW._front_matter_problems({**fm, "depends_on": ["SPEC-0003"]}, "SPEC-0005", idea,
                                     sw["must_depend_on"]) == []
    assert SW._front_matter_problems({**fm, "depends_on": "SPEC-0003"}, "SPEC-0005", idea,
                                     sw["must_depend_on"]) == []
    assert SW._front_matter_problems({**fm, "depends_on": ["SPEC-0003", "SPEC-0001"]},
                                     "SPEC-0005", idea, ["SPEC-0003"]) == []
    assert SW._front_matter_problems(fm, "SPEC-0005", idea) == []


def test_must_depend_on_resolves_each_ref_kind(env, monkeypatch):
    cfg, repo = env
    a = capture(cfg, "A")
    ca = add_idea_campaign(cfg, monkeypatch, a)
    st = _state(cfg, ca)
    st["spec"]["id"] = st["spec_id"] = "SPEC-0042"
    S.write_state(cfg, ca, st)
    _merge(cfg, ca)
    probe = {"after": [{"ref": a, "kind": "idea"}, {"ref": ca, "kind": "campaign"},
                       {"ref": "SPEC-0001", "kind": "spec"}]}
    got = Q._must_depend_on(cfg, probe)
    assert got == [{"spec_id": "SPEC-0042", "ref": a}, {"spec_id": "SPEC-0001", "ref": "SPEC-0001"}]


# ---- AC4: Breakdown annotations ---------------------------------------------------------------

@pytest.mark.parametrize("text,ords", [
    ("Do it (depends_on child 1)", [1]),
    ("Do it (depends_on child 1 and child 2)", [1, 2]),
    ("Do it (depends_on child 1, child 3)", [1, 3]),
    ("Do it (depends_on children 2, 3)", [2, 3]),
    ("Do it (DEPENDS_ON Child 2)", [2]),
])
def test_parse_breakdown_deps(text, ords):
    cleaned, got = SP.parse_breakdown_deps(text)
    assert got == ords and cleaned == "Do it"


def test_parse_breakdown_deps_no_annotation():
    assert SP.parse_breakdown_deps("Plain — text (project: X)") == ("Plain — text (project: X)", [])


# ---- AC5: batch order -------------------------------------------------------------------------

def test_batch_orders_ideas_by_after_and_refuses_cycles(env, monkeypatch):
    cfg, repo = env
    a, b, c = capture(cfg, "A"), capture(cfg, "B"), capture(cfg, "C")
    from nightwright import org_write as OW
    from test_agentic_idea_campaign import idea
    OW.set_property(cfg, idea(cfg, a), "AFTER", f"{b} {c}")
    OW.set_property(cfg, idea(cfg, b), "AFTER", c)
    batch = Q.plan_add_batch(cfg, [a, b, c])
    assert [it["id"] for it in batch["items"]] == [c, b, a]
    assert [e["ref"] for e in batch["items"][2]["plan"]["after"]] == [b, c]
    assert "after" not in batch["items"][0]["plan"]
    Q.apply_add_batch(cfg, batch)
    by_idea = {s["idea"]["id"]: s for s in S.list_campaigns(cfg)}
    assert by_idea[a]["after"] == [{"ref": b, "kind": "idea"}, {"ref": c, "kind": "idea"}]
    assert "after" not in by_idea[c]


def test_batch_cycle_and_cascade(env, monkeypatch):
    cfg, repo = env
    a, b, c = capture(cfg, "A"), capture(cfg, "B"), capture(cfg, "C")
    from nightwright import org_write as OW
    from test_agentic_idea_campaign import idea
    OW.set_property(cfg, idea(cfg, a), "AFTER", b)
    OW.set_property(cfg, idea(cfg, b), "AFTER", a)
    OW.set_property(cfg, idea(cfg, c), "AFTER", a)
    batch = Q.plan_add_batch(cfg, [a, b, c])
    items = {it["id"]: it for it in batch["items"]}
    assert "dependency cycle" in items[a]["error"] and "dependency cycle" in items[b]["error"]
    assert items[c]["error"] == f"depends on refused {a}" and "plan" not in items[c]


def test_batch_cli_after_applies_to_every_id(env, monkeypatch):
    cfg, repo = env
    a, b, c = capture(cfg, "A"), capture(cfg, "B"), capture(cfg, "C")
    ca = add_idea_campaign(cfg, monkeypatch, a)
    r = run_cli(cfg, monkeypatch, ["queue", "add", b, c, "--after", a, "--yes"])
    assert r.exit_code == 0, r.output
    afters = [s.get("after") for s in S.list_campaigns(cfg) if s["id"] != ca]
    assert afters == [[{"ref": a, "kind": "idea"}]] * 2


# ---- AC6: the quiet stream --------------------------------------------------------------------

def test_quiet_holds_live_after_wait_and_delivers_dead_one(env, monkeypatch):
    cfg, repo = env
    a, b = capture(cfg, "A"), capture(cfg, "B")
    ca = add_idea_campaign(cfg, monkeypatch, a)
    add_idea_campaign(cfg, monkeypatch, b, "--after", ca)
    ctx = QT.Ctx(cfg, "conductor", lease={"held": False})
    proj = _state(cfg, ca)["project"]
    assert (proj, a) in ctx.live_specs and (proj, ca) in ctx.live_specs
    wait_idea = [f"waiting on {a} (after: campaign {ca} is QUEUED)"]
    assert QT.dependency_wait(wait_idea, ctx.live_specs, proj)
    wait_cid = [f"waiting on {ca} (after: campaign {ca} is QUEUED)"]
    assert QT.waited_ids(wait_cid[0]) == [ca, ca]
    assert QT.dependency_wait(wait_cid, ctx.live_specs, proj)
    assert not QT.dependency_wait([f"waiting on {a} (after: no campaign queued for {a})"],
                                  frozenset(), proj)
    assert not QT.dependency_wait([f"after {a}: campaign {ca} abandoned"], ctx.live_specs, proj)
