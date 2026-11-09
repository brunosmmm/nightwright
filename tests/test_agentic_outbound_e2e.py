"""SPEC-0297: an outbound campaign that merges pulls the portable spec's status back into the
outbox copy and ships its `EXPORTED` idea; the HANDED_OFF sweep does the same on the PR merge."""
import json
from pathlib import Path

from test_agentic_e2e import (SPEC_ACCEPTED, added, fake_gh, impl_done, make_cfg, make_repo, review,
                              run_cli, script, write_review)
from test_agentic_queue import _apply, _merge_with_idea, opened  # noqa: F401 — fixture

from nightwright import org_write as OW
from nightwright.agentic import queue as Q
from nightwright.agentic import schemas as SC
from nightwright.agentic import store as S
from nightwright.org import load_tasks
from nightwright.specmeta import split_frontmatter
import pytest

pytestmark = pytest.mark.integration

REL = "docs/specs/ACME-0001-trivial.md"
EXTRA = "target_project: acme\n"


def _outbound(tmp_path, monkeypatch, *, policy="merge", rel=REL, frozen=True, with_origin=False):
    """A target repo holding an accepted `ACME-0001`, its outbox copy (linked to an `EXPORTED`
    idea) and a cfg whose runner implements the spec then passes review."""
    repo = make_repo(tmp_path, spec_id="ACME-0001", policy=policy, with_origin=with_origin,
                     spec_rel=rel, extra_fm=EXTRA)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, [impl_done("ACME-0001", spec_rel=rel, extra=EXTRA),
                                                      review()]),
                   outbox={"acme": {"repo_path": str(repo)}})
    cfg["org_idea_keywords"] = ["IDEA", "INCUBATE", "SPECCED", "EXPORTED", "|", "PROMOTED",
                                "SHIPPED", "DROPPED"]
    OW.add_idea(cfg, "outbound idea")
    idea = [t for t in load_tasks(cfg) if t.is_idea][0]
    iid = idea.properties["ID"]
    OW.set_state(cfg, idea, "EXPORTED")
    froze = f"target_spec_path: {repo / rel}\n" if frozen else ""
    outbox = Path(cfg["outbox_dir"]) / "acme" / "ACME-0001-trivial.md"
    outbox.parent.mkdir(parents=True)
    outbox.write_text(SPEC_ACCEPTED.format(
        id="ACME-0001", extra=f"target_project: acme\ntarget_repo: {repo}\n{froze}source_idea: {iid}\n"))
    write_review(cfg, outbox, "ACME-0001")
    return repo, cfg, outbox, iid


def _idea_state(cfg, iid):
    return OW.resolve_selector(cfg, iid).state


def _status(path):
    return split_frontmatter(Path(path).read_text())[0]["status"]


def _findings(cfg, cid):
    return [(e["severity"], e["title"]) for e in S.read_ledger(cfg, cid, types=["finding"])]


def _run(cfg, monkeypatch):
    r = run_cli(cfg, monkeypatch, ["queue", "run", "--until-idle"])
    assert r.exit_code == 0, r.output


def test_outbound_merge_pulls_status_and_ships_idea(tmp_path, monkeypatch):      # AC 1-3, 7
    repo, cfg, outbox, iid = _outbound(tmp_path, monkeypatch)
    cid = added(cfg, monkeypatch, "ACME-0001")
    _run(cfg, monkeypatch)
    assert S.read_state(cfg, cid)["state"] == "MERGED"
    assert (repo / "feature.txt").exists()
    assert _status(outbox) == "done"
    assert _idea_state(cfg, iid) == "SHIPPED"
    (ev,) = S.read_ledger(cfg, cid, types=["outbound_pulled"])
    assert (ev["spec_id"], ev["status"], ev["outbox_path"]) == ("ACME-0001", "done", str(outbox))
    assert (ev["idea_id"], ev["from"], ev["to"]) == (iid, "EXPORTED", "SHIPPED")
    assert SC.ledger_problems(ev) == []
    assert not [t for _s, t in _findings(cfg, cid) if "reconcile" in t]
    assert not list(S.read_ledger(cfg, cid, types=["idea_reconciled"]))
    r = run_cli(cfg, monkeypatch, ["campaign", "log", cid])
    assert r.exit_code == 0 and f"idea {iid}: EXPORTED -> SHIPPED" in r.output


def test_outbound_pr_handoff_does_not_pull(tmp_path, monkeypatch):               # AC 4
    repo, cfg, outbox, iid = _outbound(tmp_path, monkeypatch, policy="pr", with_origin=True)
    fake_gh(tmp_path, monkeypatch)
    cid = added(cfg, monkeypatch, "ACME-0001")
    _run(cfg, monkeypatch)
    assert S.read_state(cfg, cid)["state"] == "HANDED_OFF"
    assert _status(outbox) == "accepted" and _idea_state(cfg, iid) == "EXPORTED"
    assert ("info", "outbound pull-status waits for the merge") in _findings(cfg, cid)
    assert not list(S.read_ledger(cfg, cid, types=["outbound_pulled"]))


def _handed_off(tmp_path, monkeypatch, portable_status):
    repo, cfg, outbox, iid = _outbound(tmp_path, monkeypatch, policy="pr", with_origin=True)
    fake_gh(tmp_path, monkeypatch)
    cid = added(cfg, monkeypatch, "ACME-0001")
    _run(cfg, monkeypatch)
    assert S.read_state(cfg, cid)["state"] == "HANDED_OFF"
    # the user's local target checkout after (or before) pulling the merged PR
    path = repo / REL
    path.write_text(path.read_text().replace("status: accepted", f"status: {portable_status}"))
    merged = {"state": "MERGED", "merged_at": "2026-10-30T10:00:00Z", "merge_sha": "abc123"}
    (row,) = Q.sweep_handed_off(cfg, apply=True, view=lambda url: merged)
    assert row["action"] == "merge"
    assert S.read_state(cfg, cid)["state"] == "MERGED"
    return cfg, cid, outbox, iid


def test_outbound_sweep_merge_pulls(tmp_path, monkeypatch):                       # AC 5
    cfg, cid, outbox, iid = _handed_off(tmp_path, monkeypatch, "done")
    assert _status(outbox) == "done" and _idea_state(cfg, iid) == "SHIPPED"
    (ev,) = S.read_ledger(cfg, cid, types=["outbound_pulled"])
    assert (ev["status"], ev["idea_id"], ev["from"], ev["to"]) == ("done", iid, "EXPORTED", "SHIPPED")


def test_outbound_sweep_merge_with_stale_target_checkout(tmp_path, monkeypatch):  # AC 5
    cfg, cid, outbox, iid = _handed_off(tmp_path, monkeypatch, "in-progress")
    assert _status(outbox) == "in-progress" and _idea_state(cfg, iid) == "EXPORTED"
    (ev,) = S.read_ledger(cfg, cid, types=["outbound_pulled"])
    assert (ev["status"], ev["idea_id"], ev["to"]) == ("in-progress", None, None)
    assert ("minor", "outbound pulled status is in-progress, not done") in _findings(cfg, cid)


def test_outbound_pull_failure_is_a_finding(tmp_path, monkeypatch):               # AC 6
    repo, cfg, outbox, iid = _outbound(tmp_path, monkeypatch)
    cid = added(cfg, monkeypatch, "ACME-0001")
    while S.read_state(cfg, cid)["state"] != "CLOSING":
        Q.tick(cfg)
    outbox.unlink()
    while not Q.tick(cfg).idle:
        pass
    assert S.read_state(cfg, cid)["state"] == "MERGED"
    assert ("minor", "outbound pull-status failed") in _findings(cfg, cid)
    assert not list(S.read_ledger(cfg, cid, types=["outbound_pulled"]))
    assert _idea_state(cfg, iid) == "EXPORTED"


def test_outbound_done_but_idea_not_advanced_is_a_finding(tmp_path, monkeypatch):  # AC 6
    repo, cfg, outbox, iid = _outbound(tmp_path, monkeypatch)
    cid = added(cfg, monkeypatch, "ACME-0001")

    def boom(*a, **kw):
        raise ValueError("cannot move the idea")
    monkeypatch.setattr("nightwright.export.OW.set_state", boom)
    _run(cfg, monkeypatch)
    assert S.read_state(cfg, cid)["state"] == "MERGED" and _status(outbox) == "done"
    assert _idea_state(cfg, iid) == "EXPORTED"
    (ev,) = S.read_ledger(cfg, cid, types=["outbound_pulled"])
    assert (ev["status"], ev["idea_id"], ev["from"], ev["to"]) == ("done", None, None, None)
    (f,) = [e for e in S.read_ledger(cfg, cid, types=["finding"])
            if e["title"].startswith("outbound pull did not ship idea")]
    assert f["severity"] == "minor" and iid in f["title"]
    assert f["detail"] == "run `nw spec pull-status ACME-0001`"


def test_outbound_pull_uses_frozen_target_path(tmp_path, monkeypatch):            # AC 8
    # the frozen path is outside `spec_dir` (docs/specs), so a live-config derivation misses it
    rel = "specs/ACME-0001-trivial.md"
    repo, cfg, outbox, iid = _outbound(tmp_path, monkeypatch, rel=rel)
    cid = added(cfg, monkeypatch, "ACME-0001")
    _run(cfg, monkeypatch)
    assert S.read_state(cfg, cid)["state"] == "MERGED"
    assert _status(outbox) == "done" and _idea_state(cfg, iid) == "SHIPPED"
    assert not [t for _s, t in _findings(cfg, cid) if "failed" in t]


def test_outbound_pull_without_frozen_path_falls_back_to_repo_root(tmp_path, monkeypatch):
    repo, cfg, outbox, iid = _outbound(tmp_path, monkeypatch, frozen=False)
    path = repo / REL
    path.write_text(path.read_text().replace("status: accepted", "status: done"))
    from nightwright import export as EX
    seen, events = [], []
    real = EX.pull_status
    monkeypatch.setattr(EX, "pull_status", lambda c, i, **kw: (seen.append(kw), real(c, i, **kw))[1])
    Q._pull_outbound(cfg, {"repo": {"root": str(repo)}}, events, "ACME-0001", "MERGED")
    assert seen == [{"to": str(repo)}]
    (ev,) = [p for t, p in events if t == "outbound_pulled"]
    assert ev["status"] == "done" and _status(outbox) == "done"


def test_outbound_pull_with_frozen_path_passes_no_to(tmp_path, monkeypatch):
    repo, cfg, outbox, iid = _outbound(tmp_path, monkeypatch)
    from nightwright import export as EX
    seen, events = [], []
    real = EX.pull_status
    monkeypatch.setattr(EX, "pull_status", lambda c, i, **kw: (seen.append(kw), real(c, i, **kw))[1])
    Q._pull_outbound(cfg, {"repo": {"root": str(tmp_path / "elsewhere")}}, events, "ACME-0001", "MERGED")
    assert seen == [{}]


def test_internal_campaign_unchanged(opened):                                     # AC 7
    cfg, cid, ctx = opened
    iid, oc = _merge_with_idea(opened)
    _apply(ctx, cfg, cid, "close", close_outcome=oc)
    (ev,) = S.read_ledger(cfg, cid, types=["idea_reconciled"])
    assert ev["idea_id"] == iid
    assert not list(S.read_ledger(cfg, cid, types=["outbound_pulled"]))


