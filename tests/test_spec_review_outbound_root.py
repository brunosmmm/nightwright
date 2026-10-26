"""SPEC-0243: an outbound spec is reviewed with its target project's checkout as root, reviews
recorded under the Meta-Tools root still count, and the admission kind lands on the campaign."""
import json
from pathlib import Path

from test_agentic_e2e import SPEC_ACCEPTED, make_cfg, make_repo, run_cli, script, write_review
from wt.agentic import queue as Q
from wt.agentic import spec_review as SR
from wt import org_write as OW
from wt import specs as SP
from wt.agentic import store

SIZED = {"size": "m", "tone": "high-end", "rationale": "r"}
APPROVE = {"verdict": "approve", **SIZED, "findings": [], "suggested_splits": []}


class Spy:
    def __init__(self):
        self.seen = []

    def run(self, req):
        self.seen.append(req)
        raise ValueError("stop")


def _world(tmp_path, *, outbox_dir="acme", target_project="acme", registered=True,
           target_schema=None):
    """wt's checkout (`repo`), a scratch target checkout and an outbox spec ACME-0001."""
    wt = make_repo(tmp_path, review=False)
    (wt / "docs" / "specs" / "SCHEMA.md").write_text("META-SCHEMA-MARKER\n")
    (tmp_path / "t").mkdir()
    target = make_repo(tmp_path / "t", spec_id="ACME-0001", spec_rel="docs/specs/ACME-0001-x.md",
                       review=False)
    if target_schema:
        (target / "docs" / "specs" / "SCHEMA.md").write_text(target_schema)
    fm = (f"target_project: {target_project}\ntarget_repo: {target}\n"
          f"target_spec_path: {target}/docs/specs/ACME-0001-x.md\n") if target_project else ""
    box = tmp_path / "outbox" / outbox_dir
    box.mkdir(parents=True)
    spec = box / "ACME-0001-thing.md"
    spec.write_text(SPEC_ACCEPTED.format(id="ACME-0001", extra=fm))
    cfg = make_cfg(tmp_path, wt, script(tmp_path, []),
                   outbox={"acme": {"repo_path": str(target)}} if registered else {})
    return wt, target, spec, cfg


def _review(cfg, monkeypatch, spy):
    monkeypatch.setattr(SR, "select_runner", lambda c: spy)
    try:
        SR.review_spec(cfg, "ACME-0001")
    except ValueError:
        pass
    return spy.seen[0]


def test_review_runs_in_the_target_checkout(tmp_path, monkeypatch):
    wt, target, spec, cfg = _world(tmp_path)
    req = _review(cfg, monkeypatch, Spy())
    assert req.cwd == target and tuple(req.add_dirs) == (target,)
    entries = [{"persona": "spec_reviewer", "verdict": APPROVE, "cost_usd": 0.1}]
    cfg["agentic"]["fake_script"] = str(script(tmp_path, entries, name="s2.json"))
    monkeypatch.undo()
    r = run_cli(cfg, monkeypatch, ["spec", "review", "ACME-0001", "--json"])
    assert r.exit_code == 0, r.output
    assert f"  {target.resolve()}" in r.stderr
    stored = SR.latest_review(cfg, "ACME-0001")
    assert stored["repo_root"] == str(target)


def test_project_from_directory_and_unregistered_fallback(tmp_path, monkeypatch):
    wt, target, spec, cfg = _world(tmp_path, target_project=None)      # no target_project key
    assert SR.spec_root(cfg, spec) == target
    wt2 = tmp_path / "again"
    wt2.mkdir()
    wt, target, spec, cfg = _world(wt2, outbox_dir="ghost", registered=False,
                                   target_project="ghost")
    assert SR.spec_root(cfg, spec) == wt                               # the Meta-Tools root
    assert _review(cfg, monkeypatch, Spy()).cwd == wt


def test_prompt_carries_target_schema_else_wts(tmp_path, monkeypatch):
    *_, cfg = _world(tmp_path, target_schema="TARGET-SCHEMA-MARKER\n")
    prompt = _review(cfg, monkeypatch, Spy()).prompt
    assert "TARGET-SCHEMA-MARKER" in prompt and "META-SCHEMA-MARKER" not in prompt
    sub = tmp_path / "plain"
    sub.mkdir()
    *_, cfg = _world(sub)
    prompt = _review(cfg, monkeypatch, Spy()).prompt
    assert "META-SCHEMA-MARKER" in prompt


def test_legacy_meta_tools_root_reviews_still_admit(tmp_path):
    wt, target, spec, cfg = _world(tmp_path)
    write_review(cfg, spec, "ACME-0001", verdict="revise", sha="a" * 64, repo_root=str(wt))
    spec.write_text(spec.read_text() + "\nedited\n")
    assert SR.check_admissible(cfg, spec, "ACME-0001")["admission"] == "after_revision"
    write_review(cfg, spec, "ACME-0001", verdict="approve", reviewed_at="2026-10-26T00:00:00+00:00",
                 repo_root=str(wt))
    assert SR.check_admissible(cfg, spec, "ACME-0001")["admission"] == "approved"


def test_plan_add_default_project_matches_outbound_project(tmp_path):
    wt, target, spec, cfg = _world(tmp_path)
    write_review(cfg, spec, "ACME-0001")
    assert Q.plan_add(cfg, "ACME-0001")["project"] == "acme"
    spec.write_text(spec.read_text().replace("target_project: acme\n", ""))
    write_review(cfg, spec, "ACME-0001")
    assert Q.plan_add(cfg, "ACME-0001")["project"] == "acme"          # the outbox dir name


def _open_event(cfg, cid):
    return store.read_ledger(cfg, cid, types=["open"])[0]


def test_admission_lands_on_state_and_open_event(tmp_path):
    wt, target, spec, cfg = _world(tmp_path)
    write_review(cfg, spec, "ACME-0001")
    plan = Q.plan_add(cfg, "ACME-0001")
    state = Q.apply_add(cfg, plan)
    assert state["admission"] == "approved"
    cid = state["id"]
    assert json.loads((store.campaign_dir(cfg, cid) / "state.json").read_text())["admission"] \
        == "approved"
    assert _open_event(cfg, cid)["admission"] == "approved"


def test_forced_admission_is_recorded(tmp_path):
    wt, target, spec, cfg = _world(tmp_path)
    spec.write_text(spec.read_text().replace("status: accepted", "status: in-progress"))
    plan = Q.plan_add(cfg, "ACME-0001", force=True)
    state = Q.apply_add(cfg, plan)
    assert state["admission"] == "forced" and _open_event(cfg, state["id"])["admission"] == "forced"


def test_front_matter_beats_the_directory_name(tmp_path):
    wt, target, spec, cfg = _world(tmp_path, outbox_dir="misc")      # dir `misc`, target_project acme
    assert SR.spec_root(cfg, spec) == target
    write_review(cfg, spec, "ACME-0001")
    assert Q.plan_add(cfg, "ACME-0001")["project"] == "acme"


def test_odd_front_matter_never_crashes_spec_root(tmp_path):
    wt, target, spec, cfg = _world(tmp_path)
    body = spec.read_text()
    for bad in (body.replace("target_project: acme", "target_project: 123"),
                body.replace("target_project: acme", "target_project: [a: b"),
                "---\n- a\n- b\n---\nbody\n"):
        spec.write_text(bad)
        assert SR.spec_root(cfg, spec) in (target, wt)
    assert SP.outbound_project(spec, ["x"]) == "acme"


def test_a_review_of_the_exported_copy_does_not_govern_the_outbox_file(tmp_path):
    wt, target, spec, cfg = _world(tmp_path)
    exported = target / "docs" / "specs" / "ACME-0001-x.md"

    def stored(sha, path, at):
        SR.save_review(cfg, {"schema": SR.SCHEMA, "spec_id": "ACME-0001", "verdict": "revise",
                             "spec_sha256": sha, "reviewed_at": at, "model": "sonnet",
                             "cost_usd": 0.0, "findings": [], "suggested_splits": [],
                             "repo_root": str(target), "spec_path": str(path)})
    stored("b" * 64, exported, "2026-10-25T00:00:00+00:00")
    assert SR.latest_review(cfg, "ACME-0001", spec) is None
    stored("a" * 64, spec, "2026-10-24T00:00:00+00:00")
    assert SR.latest_review(cfg, "ACME-0001", spec)["spec_sha256"] == "a" * 64


def test_idea_campaigns_and_older_states_have_no_admission(tmp_path):
    wt, target, spec, cfg = _world(tmp_path)
    _, _, iid = OW.add_idea(cfg, "Add a thing")
    state = Q.apply_add_idea(cfg, Q.plan_add_idea(cfg, iid))
    assert state.get("admission") is None and _open_event(cfg, state["id"])["admission"] is None
    store.create_campaign(cfg, {"id": "OLD-1", "spec_id": "ACME-0001", "project": "acme"})
    assert _open_event(cfg, "OLD-1")["admission"] is None
