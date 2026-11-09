"""SPEC-0294: a merged split campaign queues its child specs as one SPEC-0276 batch, admitted as
`split_child` (no review of their own). Uses the SPEC-0290 flow of test_agentic_split."""
import pytest

from test_agentic_e2e import _write
from test_agentic_split import ORIG, part, run_ticks, written  # noqa: F401 — `written` is a fixture helper
from test_agentic_spec_writer import ledger, parked  # noqa: F401
from nightwright.agentic import queue as Q
from nightwright.agentic import schemas as SC
from nightwright.agentic import store as S

pytestmark = pytest.mark.fast

REAL_HOOK = Q._queue_split_children
PARTS = ["SPEC-0101", "SPEC-0102", "SPEC-0103"]
PR = {"state": "MERGED", "merged_at": "2026-10-27T10:00:00Z", "merge_sha": "abc123"}


def part_files(repo):
    return [repo / f"docs/specs/{n[5:]}-part-{i}.md" for i, n in enumerate(PARTS, 1)]


def merged(tmp_path, monkeypatch, *, hook=False):
    """A split campaign taken to MERGED (finalize, close). Without `hook` the SPEC-0294 hook is
    stubbed out, so nothing is queued yet."""
    cfg, repo, cid = written(tmp_path, monkeypatch)
    if not hook:
        monkeypatch.setattr(Q, "_queue_split_children", lambda *a, **k: None)
    st = run_ticks(cfg, cid, 2)
    assert st["state"] == "MERGED", st.get("park")
    return cfg, repo, cid


def children(cfg, ids=PARTS):
    return {sid: S.list_campaigns(cfg, spec_id=sid, include_terminal=True) for sid in ids}


def queued_ids(cfg):
    return sorted(c["spec_id"] for c in S.list_campaigns(cfg) if c["kind"] == "implement")


def handed_off(cfg, cid):
    st = S.read_state(cfg, cid)
    S.write_state(cfg, cid, dict(st, state="HANDED_OFF", close={
        "mode": "pr", "merged_sha": None, "pr_url": "https://gh/p/1"}))


def sweep(cfg):
    (row,) = Q.sweep_handed_off(cfg, apply=True, view=lambda url: dict(PR))
    assert row["action"] == "merge"


def test_split_merge_queues_children_in_order(tmp_path, monkeypatch):
    cfg, repo, cid = written(tmp_path, monkeypatch)
    run_ticks(cfg, cid, 1)                               # finalize -> CLOSING
    seen = []
    real = Q._queue_split_children

    def spy(cfg_, state, events):
        seen.append(S.read_state(cfg_, state["id"])["state"])
        return real(cfg_, state, events)

    monkeypatch.setattr(Q, "_queue_split_children", spy)
    st = run_ticks(cfg, cid, 1)
    assert st["state"] == "MERGED" and seen == ["CLOSING"]      # not MERGED on disk yet
    made = children(cfg)
    assert all(len(v) == 1 and v[0]["state"] == "QUEUED" for v in made.values())
    (ev,) = ledger(cfg, cid, "split_queued")
    assert SC.ledger_problems(ev) == [] and ev["refused"] == []
    assert [q["spec_id"] for q in ev["queued"]] == PARTS         # the batch order
    assert [q["campaign"] for q in ev["queued"]] == [made[s][0]["id"] for s in PARTS]
    assert all(made[s][0]["admission"] == "split_child" and made[s][0]["split_origin"] == cid
               for s in PARTS)
    ready, why = Q.backlog_readiness(cfg, made["SPEC-0102"][0])
    assert not ready and why                                     # part 1 is only `accepted`
    assert not [f for f in ledger(cfg, cid, "finding") if f["gate"] == "bookkeeping"]


def test_split_origin_inflight_vs_durable(tmp_path, monkeypatch):
    cfg, repo, cid = written(tmp_path, monkeypatch)
    closing = run_ticks(cfg, cid, 1)
    assert closing["state"] == "CLOSING"
    assert Q._split_origin(cfg, "SPEC-0102", closing) is closing
    assert Q._split_origin(cfg, "SPEC-0999", closing) is None
    assert Q._split_origin(cfg, "SPEC-0102", dict(closing, kind="implement")) is None
    assert Q._split_origin(cfg, "SPEC-0102") is None             # CLOSING on disk
    monkeypatch.setattr(Q, "_queue_split_children", lambda *a, **k: None)
    assert run_ticks(cfg, cid, 1)["state"] == "MERGED"
    assert Q._split_origin(cfg, "SPEC-0102")["id"] == cid
    assert Q._split_origin(cfg, "SPEC-0999") is None
    # an explicit origin that does not list the id is not rescued by the durable lookup
    other = dict(closing, split={"parts": [{"spec_id": "SPEC-0500"}]})
    assert Q._split_origin(cfg, "SPEC-0102", other) is None
    with pytest.raises(ValueError, match="no spec review"):
        Q.plan_add(cfg, "SPEC-0102", split_origin=other)


def test_split_sweep_queues_children(tmp_path, monkeypatch):
    cfg, repo, cid = merged(tmp_path, monkeypatch)
    handed_off(cfg, cid)
    monkeypatch.setattr(Q, "_queue_split_children", REAL_HOOK)
    sweep(cfg)
    assert S.read_state(cfg, cid)["state"] == "MERGED"
    assert queued_ids(cfg) == PARTS
    (ev,) = ledger(cfg, cid, "split_queued")
    assert [q["spec_id"] for q in ev["queued"]] == PARTS and ev["refused"] == []


def test_split_sweep_missing_files_refused(tmp_path, monkeypatch):
    cfg, repo, cid = merged(tmp_path, monkeypatch)
    handed_off(cfg, cid)
    saved = {p: p.read_text() for p in part_files(repo)}
    for p in saved:
        p.unlink()                                       # the checkout has not pulled them yet
    monkeypatch.setattr(Q, "_queue_split_children", REAL_HOOK)
    sweep(cfg)
    assert S.read_state(cfg, cid)["state"] == "MERGED"
    assert queued_ids(cfg) == []
    (ev,) = ledger(cfg, cid, "split_queued")
    assert ev["queued"] == [] and [r["spec_id"] for r in ev["refused"]] == PARTS
    finds = [f for f in ledger(cfg, cid, "finding") if f["gate"] == "bookkeeping"]
    assert len(finds) == 3 and {f["severity"] for f in finds} == {"major"}
    for p, text in saved.items():                        # the files arrive; queue by hand
        _write(repo, str(p.relative_to(repo)), text)
    plan = Q.plan_add(cfg, "SPEC-0101")
    assert plan["review"] == "split_child" and plan["split_origin"] == cid


def test_split_child_text_not_bound(tmp_path, monkeypatch):
    cfg, repo, cid = merged(tmp_path, monkeypatch)
    f = part_files(repo)[1]
    f.write_text(f.read_text() + "\nEdited after the merge.\n")
    plan = Q.plan_add(cfg, "SPEC-0102")
    assert plan["review"] == "split_child" and plan["split_origin"] == cid
    assert plan["review_sha256"] is None


def test_split_child_admission_requires_origin(tmp_path, monkeypatch):
    cfg, repo, cid = merged(tmp_path, monkeypatch)
    # an accepted spec that no split campaign lists
    _write(repo, "docs/specs/0150-other.md", part_files(repo)[0].read_text()
           .replace("SPEC-0101", "SPEC-0150").replace("part-1", "other"))
    with pytest.raises(ValueError, match="no spec review"):
        Q.plan_add(cfg, "SPEC-0150")
    # the origin must be MERGED and a split campaign
    S.write_state(cfg, cid, dict(S.read_state(cfg, cid), state="CLOSING"))
    with pytest.raises(ValueError, match="no spec review"):
        Q.plan_add(cfg, "SPEC-0101")


def test_split_child_own_review_wins(tmp_path, monkeypatch):
    from test_agentic_e2e import write_review
    cfg, repo, cid = merged(tmp_path, monkeypatch)
    f = part_files(repo)[0]
    write_review(cfg, f, "SPEC-0101", "approve")
    plan = Q.plan_add(cfg, "SPEC-0101")
    assert plan["review"] == "approved" and plan["split_origin"] is None


@pytest.mark.parametrize("verdict", ["reject", "approve_stale"])
def test_split_child_inadmissible_own_review_is_not_rescued(tmp_path, monkeypatch, verdict):
    """A review of the child keeps the admission in its hands: a `reject` (or a stale approve)
    is refused, never quietly replaced by `split_child`."""
    from test_agentic_e2e import write_review
    cfg, repo, cid = merged(tmp_path, monkeypatch)
    f = part_files(repo)[0]
    if verdict == "reject":
        write_review(cfg, f, "SPEC-0101", "reject")
    else:
        write_review(cfg, f, "SPEC-0101", "approve", sha="0" * 64)
    with pytest.raises(ValueError) as e:
        Q.plan_add(cfg, "SPEC-0101")
    assert "no spec review" not in str(e.value)


def test_split_child_origin_is_scoped_to_the_project(tmp_path, monkeypatch):
    """SPEC-0327: spec ids are per project. A MERGED split in Meta-Tools that lists SPEC-0101
    does not admit another project's unreviewed SPEC-0101; its own child is still admitted."""
    import subprocess
    cfg, repo, cid = merged(tmp_path, monkeypatch)
    other = tmp_path / "other"
    other.mkdir()
    src = part_files(repo)[0]
    _write(other, "docs/specs/0101-part-1.md", src.read_text())
    _write(other, ".nw/project.yaml", (repo / ".nw" / "project.yaml").read_text())
    for args in (["init", "-q", "-b", "main"], ["add", "-A"],
                 ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x"]):
        subprocess.run(["git", "-C", str(other), *args], check=True)
    cfg["outbox_targets"] = {**(cfg.get("outbox_targets") or {}), "B": {"repo_path": str(other)}}
    with pytest.raises(ValueError, match="no spec review"):
        Q.plan_add(cfg, "SPEC-0101", project="B")
    plan = Q.plan_add(cfg, "SPEC-0101", project="Meta-Tools")
    assert plan["review"] == "split_child" and plan["split_origin"] == cid
    # the in-flight origin is project-scoped too
    st = S.read_state(cfg, cid)
    assert Q._split_origin(cfg, "SPEC-0101", st, "Meta-Tools") is st
    assert Q._split_origin(cfg, "SPEC-0101", st, "B") is None
    assert Q._split_origin(cfg, "SPEC-0101", None, "B") is None


def test_split_children_of_a_non_internal_project_are_queued(tmp_path, monkeypatch):
    """The batch keeps a minimal copy of the in-flight origin for apply's re-plan; it must carry
    the project, or every child of a split in a project other than the internal one is refused
    at apply time with "no spec review"."""
    import subprocess
    cfg, repo, cid = merged(tmp_path, monkeypatch)
    other = tmp_path / "other"
    other.mkdir()
    for src in part_files(repo):
        _write(other, f"docs/specs/{src.name}", src.read_text())
    _write(other, ".nw/project.yaml", (repo / ".nw" / "project.yaml").read_text())
    for args in (["init", "-q", "-b", "main"], ["add", "-A"],
                 ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x"]):
        subprocess.run(["git", "-C", str(other), *args], check=True)
    cfg["outbox_targets"] = {**(cfg.get("outbox_targets") or {}), "B": {"repo_path": str(other)}}
    state = dict(S.read_state(cfg, cid), project="B")
    batch = Q.plan_add_batch(cfg, PARTS, project="B", split_origin=state)
    assert batch["params"]["split_origin"]["project"] == "B"
    Q.apply_add_batch(cfg, batch, skip_invalid=True, by="queue")
    assert [it["id"] for it in batch["items"] if it.get("written")] == PARTS, \
        [it.get("error") for it in batch["items"]]
    assert all(it["plan"]["review"] == "split_child" and it["plan"]["project"] == "B"
               for it in batch["items"])


def test_split_close_handed_off_does_not_run_the_hook(tmp_path, monkeypatch):
    """merge.policy: pr ends the close at HANDED_OFF: the PR is not merged, so no child is queued
    and no `split_queued` is recorded until the sweep sees the merge."""
    import subprocess
    from test_agentic_e2e import fake_gh
    cfg, repo, cid = written(tmp_path, monkeypatch)
    fake_gh(tmp_path, monkeypatch)
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    yaml = repo / ".nw" / "project.yaml"
    yaml.write_text(yaml.read_text().replace("policy: merge", "policy: pr"))
    for args in (["remote", "add", "origin", str(origin)], ["add", "-A"],
                 ["commit", "-qm", "pr policy"], ["push", "-q", "-u", "origin", "main"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True)
    calls = []
    monkeypatch.setattr(Q, "_queue_split_children",
                        lambda *a, **k: calls.append(1) or REAL_HOOK(*a, **k))
    st = run_ticks(cfg, cid, 2)
    assert st["state"] == "HANDED_OFF", st.get("park")
    assert calls == [] and ledger(cfg, cid, "split_queued") == []
    assert queued_ids(cfg) == []


def test_split_queue_refusal_is_finding(tmp_path, monkeypatch):
    cfg, repo, cid = written(tmp_path, monkeypatch)
    run_ticks(cfg, cid, 1)
    # part 2 already has an active campaign
    blocker = {"schema": "nw.campaign.v1", "id": "SPEC-0102-20261001-aaaa", "kind": "implement",
               "spec_id": "SPEC-0102", "project": "Meta-Tools", "state": "QUEUED",
               "created_at": "2026-10-01T00:00:00+00:00"}
    S.create_campaign(cfg, blocker)
    st = run_ticks(cfg, cid, 1)
    assert st["state"] == "MERGED"
    (ev,) = ledger(cfg, cid, "split_queued")
    assert [q["spec_id"] for q in ev["queued"]] == ["SPEC-0101"]
    assert [r["spec_id"] for r in ev["refused"]] == ["SPEC-0102", "SPEC-0103"]
    finds = [f for f in ledger(cfg, cid, "finding") if f["gate"] == "bookkeeping"]
    assert len(finds) == 2 and {f["severity"] for f in finds} == {"major"}
    assert "SPEC-0102" in finds[0]["title"] and "SPEC-0103" in finds[1]["title"]
    assert children(cfg, ["SPEC-0101"])["SPEC-0101"][0]["state"] == "QUEUED"
    assert children(cfg, ["SPEC-0103"])["SPEC-0103"] == []


def test_split_queue_exception_keeps_merged(tmp_path, monkeypatch):
    cfg, repo, cid = written(tmp_path, monkeypatch)
    run_ticks(cfg, cid, 1)
    monkeypatch.setattr(Q, "plan_add_batch", lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("boom")))
    st = run_ticks(cfg, cid, 1)
    assert st["state"] == "MERGED"
    (ev,) = ledger(cfg, cid, "split_queued")
    assert ev["queued"] == [] and {r["error"] for r in ev["refused"]} == {"boom"}
    finds = [f for f in ledger(cfg, cid, "finding") if f["gate"] == "bookkeeping"]
    assert len(finds) == 1 and finds[0]["severity"] == "major" and "boom" in finds[0]["detail"]
    assert queued_ids(cfg) == []


def test_split_queue_exception_midway_lists_created_children(tmp_path, monkeypatch):
    cfg, repo, cid = written(tmp_path, monkeypatch)
    run_ticks(cfg, cid, 1)
    real, n = Q.apply_add, []

    def flaky(cfg_, plan, **kw):
        n.append(plan["spec_id"])
        if len(n) == 2:
            raise RuntimeError("disk full")
        return real(cfg_, plan, **kw)

    monkeypatch.setattr(Q, "apply_add", flaky)
    st = run_ticks(cfg, cid, 1)
    assert st["state"] == "MERGED"
    made = children(cfg)
    assert len(made["SPEC-0101"]) == 1 and made["SPEC-0102"] == [] and made["SPEC-0103"] == []
    (ev,) = ledger(cfg, cid, "split_queued")
    assert ev["queued"] == [{"spec_id": "SPEC-0101", "campaign": made["SPEC-0101"][0]["id"]}]
    assert [r["spec_id"] for r in ev["refused"]] == ["SPEC-0102", "SPEC-0103"]
    finds = [f for f in ledger(cfg, cid, "finding") if f["gate"] == "bookkeeping"]
    assert len(finds) == 1 and made["SPEC-0101"][0]["id"] in finds[0]["detail"]
    assert "disk full" in finds[0]["detail"]


def test_manual_requeue_uses_split_child(tmp_path, monkeypatch):
    cfg, repo, cid = written(tmp_path, monkeypatch)
    run_ticks(cfg, cid, 1)
    real = Q.plan_add_batch
    monkeypatch.setattr(Q, "plan_add_batch", lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("boom")))
    assert run_ticks(cfg, cid, 1)["state"] == "MERGED"
    monkeypatch.setattr(Q, "plan_add_batch", real)
    plan = Q.plan_add(cfg, "SPEC-0101")
    assert plan["review"] == "split_child" and plan["split_origin"] == cid
    assert plan["review_sha256"] is None
    state = Q.apply_add(cfg, plan)
    assert state["split_origin"] == cid and state["admission"] == "split_child"
    assert S.read_state(cfg, plan["campaign"])["split_origin"] == cid
    (ev,) = ledger(cfg, plan["campaign"], "admitted_split_child")
    assert ev["origin"] == cid and SC.ledger_problems(ev) == []


def test_split_events_validate():
    rec = {"schema": SC.LEDGER_SCHEMA, "ts": "t", "seq": 1, "campaign": "c", "by": "queue"}
    assert SC.ledger_problems({**rec, "type": "admitted_split_child", "origin": "x"}) == []
    assert SC.ledger_problems({**rec, "type": "admitted_split_child"}) != []
    assert SC.ledger_problems({**rec, "type": "split_queued", "queued": [], "refused": []}) == []
    assert SC.ledger_problems({**rec, "type": "split_queued", "queued": []}) != []


def test_non_split_close_queues_nothing(tmp_path):
    events = []
    Q._queue_split_children(None, {"kind": "implement"}, events)
    assert events == []



def idea_parts(first=101):
    fm = {"source_idea": "IDEA-001"}               # a split child inherits the original's idea
    return [part(first, 1, fm=fm), part(first + 1, 2, prev=first, fm=fm),
            part(first + 2, 3, prev=first + 1, fm=fm)]


@pytest.mark.parametrize("route", ["close", "sweep"])
def test_split_children_link_the_idea_the_split_released(tmp_path, monkeypatch, route):
    """The hook runs after `_release_idea_link`: a child sees no live holder, so the idea ends up
    linked to the first child campaign (not to nothing, not skipped as 'held by <split>')."""
    from nightwright import org as ORG
    from nightwright import org_write as OW
    from test_agentic_split import split_fx

    def link(iid="IDEA-001"):
        ORG.invalidate_tasks_cache()
        return Q._idea_task(cfg, iid).properties.get("CAMPAIGN")

    cfg, repo, cid = split_fx(tmp_path, monkeypatch, idea_parts(), idea=True)
    assert link() == cid                                          # linked at add
    run_ticks(cfg, cid, 3)                                        # write the three parts
    if route == "sweep":
        monkeypatch.setattr(Q, "_queue_split_children", lambda *a, **k: None)
    st = run_ticks(cfg, cid, 2)
    if route == "sweep":
        assert st["state"] == "MERGED" and link() is None
        handed_off(cfg, cid)
        OW.set_property(cfg, Q._idea_task(cfg, "IDEA-001"), "CAMPAIGN", cid)   # still held at HANDED_OFF
        monkeypatch.setattr(Q, "_queue_split_children", REAL_HOOK)
        sweep(cfg)
    assert S.read_state(cfg, cid)["state"] == "MERGED"
    made = children(cfg)
    first = made["SPEC-0101"][0]
    assert first["state"] == "QUEUED" and len(ledger(cfg, cid, "idea_unlinked")) >= 1
    assert link() == first["id"]
    assert first.get("idea_link") == {"id": "IDEA-001"}
    for sid in PARTS:
        assert not [e for e in ledger(cfg, made[sid][0]["id"], "idea_link_skipped")
                    if f"held by {cid}" in e["reason"]]

