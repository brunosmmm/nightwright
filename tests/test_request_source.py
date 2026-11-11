"""SPEC-0541: the request-only writer path: `load_public_request`, `_request_source` and the
readiness check of a hand-built `request_only` campaign (no queue command creates one yet)."""
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from nightwright import idea_link as IL
from nightwright import org as ORG
from nightwright import org_write as OW
from nightwright.agentic import queue as Q
from nightwright.agentic import store as S
from nightwright.cli import cli

from test_agentic_e2e import make_cfg, make_repo, script

pytestmark = pytest.mark.fast

PROJECT = "Meta-Tools"
TITLE = "Export the thing"
BODY = "Public summary of the thing."


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True).stdout


@pytest.fixture
def env(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.com")
    cfg = make_cfg(tmp_path, repo, script(tmp_path, []))
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    return cfg, repo


def put_request(repo, req, *, title=TITLE, summary=BODY, status="open", spec=None,
                status_suffix=None):
    """(Re)write `{req}.md` on `nw-backlog` in the SPEC-0480 format `load_agreed_request` reads
    (`nw backlog add` renders a blank line before the summary, which that reader refuses)."""
    body = IL.render_request(req_id=req, title=title, summary=summary, status=status,
                             created="2026-11-01T00:00:00Z", updated="2026-11-02T00:00:00Z",
                             created_by="Test User <test@example.com>",
                             updated_by="Test User <test@example.com>", spec=spec,
                             status_suffix=status_suffix)
    IL._publish_commit(repo, rel_path=f"{req}.md", body=body, message=f"put {req}",
                       name="Test User", email="test@example.com")


def add_request(repo, **kw):
    req = IL.mint_req_id()
    put_request(repo, req, **kw)
    return req


def linked_idea(cfg, monkeypatch, *, summary=BODY, heading=TITLE, state="INCUBATE"):
    """An org idea published to the public backlog (`:REQ:` + `:REQ_BASE:`); returns (task, req)."""
    _, _, iid = OW.add_idea(cfg, heading, state=state, project=PROJECT)
    r = CliRunner().invoke(cli, ["idea", "summary", iid, "--set", summary])
    assert r.exit_code == 0, r.output
    ORG.invalidate_tasks_cache()
    task = OW.resolve_selector(cfg, iid)
    IL.publish_idea(cfg, task)
    task = OW.resolve_selector(cfg, iid)
    return task, task.properties["REQ"]


def request_only_state(cfg, req_id, *, project=PROJECT, title=TITLE, body=BODY):
    """A `QUEUED` `request_only` campaign by hand (the Decision's payload), no queue command."""
    cid = S.new_campaign_id(cfg, req_id)
    root = str(cfg["nw_checkout"])
    idea = {"schema": "nw.idea.v1", "id": req_id, "state": "", "heading": title, "tags": [],
            "kind": "idea", "summary": body, "questions": "", "question_items": [], "log": "",
            "project": project, "source_request": req_id, "request_only": True, "epic": None,
            "state_before": None}
    state = {
        "schema": "nw.campaign.v1", "id": cid, "kind": "idea", "spec_id": None, "idea": idea,
        "project": project, "profile": "balanced", "state": "QUEUED", "prev_state": None,
        "priority": 3, "force": False, "backlog": None,
        "repo": {"root": root, "base": "main", "base_sha_at_open": None,
                 "branch": f"nw/{cid}", "worktree": str(Path(cfg["data_dir"]) / "wt" / cid),
                 "head": None},
    }
    return state


def corrupt_org(cfg, monkeypatch):
    """A configured org file that cannot be read."""
    def boom(_cfg):
        raise ValueError("bad org syntax")
    monkeypatch.setattr(Q, "_ideas", boom)


def test_load_public_request_has_no_private_side(env):
    cfg, repo = env
    req = add_request(repo)
    got = IL.load_public_request(cfg, PROJECT, req)
    assert (got.req_id, got.title, got.summary, got.spec) == (req, TITLE, BODY, None)
    assert got.status_value == "open" and got.status == "open"
    with pytest.raises(ValueError, match="is not on refs/heads/nw-backlog"):
        IL.load_public_request(cfg, PROJECT, "REQ-0ABCDEFG")
    with pytest.raises(ValueError, match="invalid request id"):
        IL.load_public_request(cfg, PROJECT, "IDEA-001")


def test_request_source_without_an_org_file(env):
    cfg, repo = env
    req = add_request(repo)
    cfg["org_files"] = ["/nonexistent/ideas.org"]
    agreed, gate = Q._request_source(cfg, req, PROJECT)
    assert gate is None and agreed.summary == BODY and agreed.title == TITLE


def test_request_source_status_values(env):
    cfg, repo = env
    req = add_request(repo)
    put_request(repo, req, status="closed")
    with pytest.raises(ValueError, match=f"{req} is closed; only open requests are queued"):
        Q._request_source(cfg, req, PROJECT)
    put_request(repo, req)
    assert Q._request_source(cfg, req, PROJECT)[1] is None


def test_request_source_linked_idea_gates_the_request(env, monkeypatch):
    cfg, repo = env
    task, req = linked_idea(cfg, monkeypatch)
    agreed, gate = Q._request_source(cfg, req, PROJECT)
    assert agreed.summary == BODY and gate.properties["ID"] == task.properties["ID"]
    iid = task.properties["ID"]

    # a different project
    with pytest.raises(ValueError, match=f"{req} is linked to {iid} of project {PROJECT}, "
                                         "not Other"):
        Q._request_source(cfg, req, "Other")

    # a closed request
    put_request(repo, req, status="closed")
    with pytest.raises(ValueError, match="only open requests"):
        Q._request_source(cfg, req, PROJECT)
    put_request(repo, req)

    # drift on the public side
    put_request(repo, req, summary="Changed in public.")
    with pytest.raises(ValueError, match=f"{iid} and {req} do not match :REQ_BASE:"):
        Q._request_source(cfg, req, PROJECT)
    put_request(repo, req)
    assert Q._request_source(cfg, req, PROJECT)[0].summary == BODY

    # drift on the private side
    r = CliRunner().invoke(cli, ["idea", "summary", iid, "--set", "Changed in private."])
    assert r.exit_code == 0, r.output
    with pytest.raises(ValueError, match=f"{iid} and {req} do not match :REQ_BASE:"):
        Q._request_source(cfg, req, PROJECT)
    # drift on both sides at once
    put_request(repo, req, summary="Changed in public.")
    with pytest.raises(ValueError, match=f"{iid} and {req} do not match :REQ_BASE:"):
        Q._request_source(cfg, req, PROJECT)
    put_request(repo, req)
    r = CliRunner().invoke(cli, ["idea", "summary", iid, "--set", BODY])
    assert r.exit_code == 0, r.output
    assert Q._request_source(cfg, req, PROJECT)[0].summary == BODY

    # a cited request, and an idea with :SPEC:
    put_request(repo, req, spec="SPEC-0009")
    with pytest.raises(ValueError, match=f"{req} already cites SPEC-0009"):
        Q._request_source(cfg, req, PROJECT)
    put_request(repo, req)
    OW.set_property(cfg, OW.resolve_selector(cfg, iid), "SPEC", "SPEC-0007")
    with pytest.raises(ValueError, match=f"{iid} already has SPEC-0007; queue that spec "
                                         "instead: nw queue add SPEC-0007"):
        Q._request_source(cfg, req, PROJECT)


def test_request_source_closed_idea_and_two_claimants(env, monkeypatch):
    cfg, repo = env
    task, req = linked_idea(cfg, monkeypatch)
    iid = task.properties["ID"]
    OW.set_state(cfg, OW.resolve_selector(cfg, iid), "DROPPED")
    with pytest.raises(ValueError, match=f"{iid} is DROPPED; only open ideas"):
        Q._request_source(cfg, req, PROJECT)
    OW.set_state(cfg, OW.resolve_selector(cfg, iid), "IDEA")
    _, _, other = OW.add_idea(cfg, "Another", project=PROJECT)
    OW.set_property(cfg, OW.resolve_selector(cfg, other), "REQ", req)
    with pytest.raises(ValueError, match=f"{req} is claimed by {iid} and {other}|"
                                         f"{req} is claimed by {other} and {iid}"):
        Q._request_source(cfg, req, PROJECT)


def test_request_source_fails_closed_on_an_unreadable_org_file(env, monkeypatch):
    cfg, repo = env
    req = add_request(repo)
    state = request_only_state(cfg, req)
    assert Q.backlog_readiness(cfg, state)[0] is True
    with monkeypatch.context() as m:
        corrupt_org(cfg, m)
        with pytest.raises(ValueError, match=f"could not read the ideas file to check {req}: "
                                             "bad org syntax"):
            Q._request_source(cfg, req, PROJECT)
        ok, why = Q.backlog_readiness(cfg, state)
        assert not ok and why == [f"request {req}: could not read the ideas file to check "
                                  f"{req}: bad org syntax"]
        with pytest.raises(ValueError, match="could not read the ideas file"):
            Q._request_source_for(cfg, state)
    # no org file configured: it queues
    cfg["org_files"] = ["/nonexistent/ideas.org"]
    assert Q._request_source(cfg, req, PROJECT)[1] is None
    assert Q.backlog_readiness(cfg, state)[0] is True


def test_request_source_allow_spec(env):
    cfg, repo = env
    req = add_request(repo, spec="SPEC-0042")
    assert Q._request_source(cfg, req, PROJECT, allow_spec="SPEC-0042")[0].spec == "SPEC-0042"
    with pytest.raises(ValueError, match=f"{req} already cites SPEC-0042"):
        Q._request_source(cfg, req, PROJECT, allow_spec="SPEC-0043")
    with pytest.raises(ValueError, match=f"{req} already cites SPEC-0042"):
        Q._request_source(cfg, req, PROJECT)


def test_request_source_refuses_a_status_that_is_not_open_or_closed(env):
    cfg, repo = env
    req = add_request(repo)
    put_request(repo, req, status_suffix='"open"')
    assert Q._request_source(cfg, req, PROJECT)[0].status_value == "open"
    put_request(repo, req, status_suffix="maybe")
    with pytest.raises(ValueError, match=f"{req} file is not a request"):
        Q._request_source(cfg, req, PROJECT)


def test_request_campaign_is_ready_without_an_idea(env, monkeypatch):
    cfg, repo = env
    req = add_request(repo)
    state = request_only_state(cfg, req)

    def spy(*a, **kw):
        raise AssertionError("_idea_task must not be called")
    monkeypatch.setattr(Q, "_idea_task", spy)

    assert Q.backlog_readiness(cfg, state) == (True, [])
    put_request(repo, req, status="closed")
    ok, why = Q.backlog_readiness(cfg, state)
    assert not ok and why == [f"request {req}: {req} is closed; only open requests are queued"]
    put_request(repo, req)
    put_request(repo, req, spec="SPEC-0042")
    ok, why = Q.backlog_readiness(cfg, state)
    assert not ok and why == [f"request {req}: {req} already cites SPEC-0042"]
    # the campaign's own spec was cited: still ready
    state["spec"] = {"id": "SPEC-0042"}
    state["spec_id"] = "SPEC-0042"
    state["kind"] = "idea"
    assert Q._request_source_for(cfg, state)[0].spec == "SPEC-0042"
    # with `spec.id` set the campaign is no longer spec-less (the ordinary spec-file readiness
    # applies); with only `spec_id` recorded the spec-less branch must allow the own spec
    state["spec"] = {}
    assert Q._request_source_for(cfg, state)[0].spec == "SPEC-0042"
    assert Q.backlog_readiness(cfg, state)[0] is True
    state["spec_id"] = None
    ok, why = Q.backlog_readiness(cfg, state)
    assert not ok and why == [f"request {req}: {req} already cites SPEC-0042"]
    missing = request_only_state(cfg, "REQ-0ABCDEFG")
    ok, why = Q.backlog_readiness(cfg, missing)
    assert not ok and "is not on refs/heads/nw-backlog" in why[0]


def test_non_request_idea_campaign_readiness_is_unchanged(env, monkeypatch):
    cfg, _ = env
    state = request_only_state(cfg, "IDEA-001")
    state["idea"].pop("request_only")
    assert Q.backlog_readiness(cfg, state) == (False, ["idea IDEA-001 not found"])


def test_request_only_close_reconcile_does_not_look_for_an_idea(env, monkeypatch):
    """`_reconcile_project_spec` must not take the campaign's `REQ-…` id for an idea id: a spec with
    no `source_idea` of a `request_only` campaign is an info finding, not `idea not found`."""
    cfg, repo = env
    req = add_request(repo)
    monkeypatch.setattr(Q, "_project_spec_fm",
                        lambda st: ({"id": "SPEC-0002", "status": "done",
                                     "source_request": req}, "abc1234"))
    monkeypatch.setattr(Q, "_ideas", lambda c: (_ for _ in ()).throw(
        AssertionError("the org file must not be read")))

    def run(state):
        found = []
        Q._reconcile_project_spec(cfg, state, [], "SPEC-0002", "MERGED",
                                  lambda sev, title, detail: found.append((sev, title, detail)))
        return found

    state = request_only_state(cfg, req)
    assert [(s, t) for s, t, _ in run(state)] == [("info", "idea reconcile: spec has no "
                                                           "source_idea")]
    # a plain idea campaign keeps looking the idea up (the id is not found here)
    monkeypatch.setattr(Q, "_ideas", lambda c: [])
    state["idea"].pop("request_only")
    assert [(s, t, d) for s, t, d in run(state)] == [("minor", "project idea reconcile: idea "
                                                               "not found", req.upper())]


def test_unlinked_idea_still_does_not_resolve_the_repo(env, monkeypatch):
    cfg, _ = env
    _, _, iid = OW.add_idea(cfg, "Plain", project=PROJECT)
    monkeypatch.setattr(IL, "resolve_linked_repo",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("resolved")))
    assert IL.load_agreed_request(cfg, OW.resolve_selector(cfg, iid)) is None
