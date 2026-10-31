"""SPEC-0290: a split review becomes a split campaign that writes one child spec per suggested
part, supersedes the original and merges. Uses the SPEC-0215 spec-writer harness (temp repo with
the real linter and schema, FakeRunner)."""
import subprocess
import sys

import pytest

from test_agentic_e2e import _git, _write, added, make_cfg, script, write_review
from test_agentic_spec_writer import (ledger, make_project, parked, resume, spec_written,
                                      _spec_md, wtree)
from nightwright.agentic import close as CL
from nightwright.agentic import queue as Q
from nightwright.agentic import schemas as SC
from nightwright.agentic import spec_write as SW
from nightwright.agentic import store as S

ORIG = "docs/specs/0005-big-thing.md"
TITLES = ["Part 1", "Part 2", "Part 3"]


def _lint_ledger(repo):
    subprocess.run([sys.executable, "tools/spec_lint.py", "--write-ledger"], cwd=repo, check=True,
                   capture_output=True)


def part(number, k, *, prev=None, deps="[SPEC-0099]", **kw):
    """FakeRunner entry for part `k`: wired like the writer must (`prev` = the previous part's
    number); `kw` overrides front matter keys."""
    fm = {"supersedes": "[SPEC-0005]", "depends_on": f"[SPEC-{prev:04d}]" if prev else deps,
          **kw.pop("fm", {})}
    for k_ in [k_ for k_, v in fm.items() if v is None]:
        fm.pop(k_)
    kw.setdefault("epic", "SPEC-0100")
    return spec_written(number, None, mode="split", slug=f"part-{k}", title=f"Part {k}",
                        status=kw.pop("status", "accepted"), fm=fm, **kw)


def good_parts(first=101):
    return [part(first, 1), part(first + 1, 2, prev=first), part(first + 2, 3, prev=first + 1)]


def split_fx(tmp_path, monkeypatch, entries=(), n=3, *, close=False):
    """An accepted SPEC-0005 (parent SPEC-0100, depends on the not-done SPEC-0099) whose review is
    `split` with `n` parts, queued; one tick admits and opens it into SPEC_WRITING."""
    repo = make_project(tmp_path, with_epic=True)
    _write(repo, ORIG, _spec_md(5, None, title="Big thing", status="accepted", epic="SPEC-0100",
                                fm={"depends_on": "[SPEC-0099]"}))
    _write(repo, "feature.txt", "x\n")
    _lint_ledger(repo)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "an accepted spec")
    cfg = make_cfg(tmp_path, repo, script(tmp_path, []))
    write_review(cfg, repo / ORIG, "SPEC-0005", "split", suggested_splits=[
        {"title": t, "scope": f"scope of {t}"} for t in TITLES[:n]] if n <= 3 else
        [{"title": f"Part {i}", "scope": "s"} for i in range(1, n + 1)])
    cid = added(cfg, monkeypatch, "SPEC-0005")
    script(tmp_path, list(entries))
    Q.tick(cfg)
    return cfg, repo, cid


def run_ticks(cfg, cid, n):
    for _ in range(n):
        Q.tick(cfg)
    return S.read_state(cfg, cid)


def written(tmp_path, monkeypatch, **kw):
    """A split campaign with all three parts written (finalize not yet run)."""
    cfg, repo, cid = split_fx(tmp_path, monkeypatch, good_parts(), **kw)
    st = run_ticks(cfg, cid, 3)
    assert [p["spec_id"] for p in st["split"]["parts"]] == ["SPEC-0101", "SPEC-0102", "SPEC-0103"]
    return cfg, repo, cid


# ---- admission ---------------------------------------------------------------------------------

def test_add_split_creates_split_campaign(tmp_path, monkeypatch):
    repo = make_project(tmp_path, with_epic=True)
    _write(repo, ORIG, _spec_md(5, None, title="Big thing", status="accepted"))
    _lint_ledger(repo)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "x")
    cfg = make_cfg(tmp_path, repo, script(tmp_path, []))
    write_review(cfg, repo / ORIG, "SPEC-0005", "split", size="xl", suggested_splits=[
        {"title": "A", "scope": "sa"}, {"title": "B", "scope": "sb"}])
    cid = added(cfg, monkeypatch, "SPEC-0005")
    st = S.read_state(cfg, cid)
    assert st["kind"] == "split" and st["state"] == "QUEUED" and st["spec_id"] == "SPEC-0005"
    assert st["admission"] == "split_pending" and st["spec"]["id"] == "SPEC-0005"
    assert st["sizing"]["size"] is None and st["profile"] == "balanced"     # the default profile
    sp = st["split"]
    assert sp["original"] == "SPEC-0005" and sp["original_rel"] == ORIG
    assert sp["parts"] == [{"title": "A", "scope": "sa", "number": None, "rel_path": None,
                            "spec_id": None},
                           {"title": "B", "scope": "sb", "number": None, "rel_path": None,
                            "spec_id": None}]
    (ev,) = ledger(cfg, cid, "admitted_split")
    assert SC.ledger_problems(ev) == [] and len(ev["parts"]) == 2
    Q.tick(cfg)
    assert S.read_state(cfg, cid)["state"] == "SPEC_WRITING"


def test_split_original_rel_recorded(tmp_path, monkeypatch):
    cfg, repo, cid = written(tmp_path, monkeypatch)
    st = S.read_state(cfg, cid)
    assert st["split"]["original_rel"] == ORIG
    with S.item_lock(cfg, cid):                          # something rewrites state.spec
        st["spec"]["rel_path"] = "docs/specs/0101-part-1.md"
        S.write_state(cfg, cid, st)
    Q.tick(cfg)
    wt = wtree(cfg, cid)
    assert "status: superseded" in (wt / ORIG).read_text()
    assert "status: accepted" in (wt / "docs/specs/0101-part-1.md").read_text()


# ---- writing the parts -------------------------------------------------------------------------

def test_split_campaign_writes_each_part(tmp_path, monkeypatch):
    cfg, repo, cid = split_fx(tmp_path, monkeypatch, good_parts())
    wt = wtree(cfg, cid)
    for i, (num, deps) in enumerate([(101, "[SPEC-0099]"), (102, "[SPEC-0101]"),
                                     (103, "[SPEC-0102]")], 1):
        st = run_ticks(cfg, cid, 1)
        text = (wt / f"docs/specs/{num:04d}-part-{i}.md").read_text()
        assert f"depends_on: {deps}" in text and "supersedes: [SPEC-0005]" in text
        assert "parent: SPEC-0100" in text and "status: accepted" in text
        assert st["split"]["parts"][i - 1]["spec_id"] == f"SPEC-{num:04d}"
        assert st["split"]["parts"][i - 1]["rel_path"] == f"docs/specs/{num:04d}-part-{i}.md"
    runs = [e for e in ledger(cfg, cid, "run.start") if e["persona"] == "spec_writer"]
    assert len(runs) == 3
    assert st["spec_id"] == "SPEC-0005" and st["spec"]["id"] == "SPEC-0005"   # the original
    assert st["state"] == "SPEC_WRITING" and st["counters"].get("spec_write_retries", 0) == 0
    evs = ledger(cfg, cid, "spec_written")
    assert [e["spec_id"] for e in evs] == ["SPEC-0101", "SPEC-0102", "SPEC-0103"]
    assert {e["mode"] for e in evs} == {"split"} and all(SC.ledger_problems(e) == [] for e in evs)
    prompt = (S.runs_dir(cfg, cid) / "002-spec_writer" / "prompt.md").read_text()
    assert "part 2 of 3" in prompt and "scope of Part 2" in prompt
    assert "part 1: Part 1" in prompt and "part 3: Part 3" in prompt
    assert "`depends_on: [\"SPEC-0101\"]`" in prompt and "`supersedes: [\"SPEC-0005\"]`" in prompt


@pytest.mark.parametrize("wrong", ["supersedes missing", "depends_on wrong", "parent wrong",
                                   "status not accepted"])
def test_split_part_wrong_wiring_retried_then_parked(tmp_path, monkeypatch, wrong):
    bad = {"supersedes missing": dict(fm={"supersedes": None}),
           "depends_on wrong": dict(fm={"depends_on": "[SPEC-0001]"}),
           "parent wrong": dict(epic="SPEC-0099"),
           "status not accepted": dict(status="proposed")}[wrong]
    cfg, repo, cid = split_fx(tmp_path, monkeypatch, [part(101, 1, **bad)] * 2)
    st = run_ticks(cfg, cid, 1)
    assert st["state"] == "SPEC_WRITING" and st["counters"]["spec_write_retries"] == 1
    assert st["split"]["parts"][0]["spec_id"] is None
    assert not (wtree(cfg, cid) / "docs/specs/0101-part-1.md").exists()    # rolled back
    run_ticks(cfg, cid, 1)
    park = parked(cfg, cid)
    key = wrong.split()[0]
    assert park["reason"] == "spec_write_failed" and park["resume_to"] == "SPEC_WRITING"
    assert f"front matter: {key} must be" in park["detail"]
    others = {"status", "supersedes", "depends_on", "parent"} - {key}
    assert not any(f"front matter: {o} must be" in park["detail"] for o in others)


def test_split_step_never_parks_or_revises(tmp_path, monkeypatch):
    cfg, repo, cid = split_fx(tmp_path, monkeypatch, good_parts())
    st = S.read_state(cfg, cid)
    assert st["spec"]["id"] and not st.get("pending_brief")
    ctx = Q._Ctx(cfg)
    profile = ctx.profile(st)
    assert Q.plan_step(st, profile).kind == "write_spec"
    for admission in ("split_pending", "revise_pending"):
        st["admission"] = admission
        assert Q._prepare_spec_write(ctx, st)["write_rules"].startswith("Split mode")
        assert st["spec_write"]["mode"] == "split"
    st = run_ticks(cfg, cid, 3)
    assert Q.plan_step(st, profile).kind == "finalize_split"
    st["split"]["parts"][2]["spec_id"] = None
    assert Q.plan_step(st, profile).kind == "write_spec"


def test_split_step_parks_on_the_cost_cap(tmp_path, monkeypatch):
    cfg, repo, cid = split_fx(tmp_path, monkeypatch, good_parts())
    st = S.read_state(cfg, cid)
    profile = Q._Ctx(cfg).profile(st)
    st["cost"] = {"cap_usd": 0.0001, "total_usd": 0.0}
    step = Q.plan_step(st, profile)                      # a part is unwritten: the run is capped
    assert step.kind == "park" and step.reason == "cost_cap" and step.persona == "spec_writer"
    assert step.resume_to == "SPEC_WRITING"
    for p in st["split"]["parts"]:                       # finalize runs no persona: never capped
        p["spec_id"] = "SPEC-0101"
    assert Q.plan_step(st, profile).kind == "finalize_split"


# ---- readiness ---------------------------------------------------------------------------------

def test_split_readiness_ignores_depends_on(tmp_path, monkeypatch):
    repo = make_project(tmp_path, with_epic=True)
    _write(repo, ORIG, _spec_md(5, None, title="Big thing", status="accepted",
                                fm={"depends_on": "[SPEC-0099]"}))          # SPEC-0099 not done
    _lint_ledger(repo)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "x")
    cfg = make_cfg(tmp_path, repo, script(tmp_path, []))
    write_review(cfg, repo / ORIG, "SPEC-0005", "split", suggested_splits=[
        {"title": "A", "scope": "sa"}, {"title": "B", "scope": "sb"}])
    cid = added(cfg, monkeypatch, "SPEC-0005")
    st = S.read_state(cfg, cid)
    assert Q.backlog_readiness(cfg, st) == (True, [])
    spec = repo / ORIG
    orig = spec.read_text()
    spec.write_text(orig + "\nedited\n")
    assert Q.backlog_readiness(cfg, st) == (False, ["spec changed since the split review"])
    spec.write_text(orig.replace("status: accepted", "status: in-progress"))
    ready, why = Q.backlog_readiness(cfg, st)
    assert not ready and "spec status is in-progress" in why
    # the same dependency does block an ordinary campaign
    plain = {"kind": "implement", "repo": {"root": str(repo)}, "spec": {"rel_path": ORIG}}
    spec.write_text(orig)
    assert Q.backlog_readiness(cfg, plain) == (False, ["waiting on SPEC-0099 (accepted)"])


def _dependent_fx(tmp_path, part_statuses):
    repo = make_project(tmp_path, with_epic=True)
    _write(repo, ORIG, _spec_md(5, None, status="accepted"))
    _write(repo, "docs/specs/0006-dependent.md", _spec_md(
        6, None, title="Dependent", status="accepted", fm={"depends_on": "[SPEC-0005]"}))
    for i, status in enumerate(part_statuses, 1):
        _write(repo, f"docs/specs/{100 + i:04d}-part-{i}.md", _spec_md(
            100 + i, None, title=f"Part {i}", status=status, fm={"supersedes": "[SPEC-0005]"}))
    SW.supersede(repo, ORIG, "SPEC-0101")
    dep = {"kind": "implement", "repo": {"root": str(repo)},
           "spec": {"rel_path": "docs/specs/0006-dependent.md"}}
    return {}, dep


def test_dependent_of_split_original_waits_for_parts(tmp_path):
    cfg, dep = _dependent_fx(tmp_path, ["accepted", "accepted"])
    ready, why = Q.backlog_readiness(cfg, dep)
    assert not ready
    assert why == ["waiting on SPEC-0005 (superseded; waiting on SPEC-0101, SPEC-0102)"]


def test_dependent_of_split_original_one_part_pending(tmp_path):
    cfg, dep = _dependent_fx(tmp_path, ["done", "accepted"])
    assert Q.backlog_readiness(cfg, dep) == (
        False, ["waiting on SPEC-0005 (superseded; waiting on SPEC-0102)"])


def test_dependent_of_split_original_ready_when_parts_done(tmp_path):
    cfg, dep = _dependent_fx(tmp_path, ["done", "done"])
    assert Q.backlog_readiness(cfg, dep) == (True, [])
    (tmp_path / "b").mkdir()
    cfg, dep = _dependent_fx(tmp_path / "b", ["done", "superseded"])
    assert Q.backlog_readiness(cfg, dep) == (True, [])


def test_superseded_dependency_without_replacement_is_met(tmp_path):
    cfg, dep = _dependent_fx(tmp_path, [])
    text = (tmp_path / "repo" / ORIG).read_text().replace("superseded_by: SPEC-0101\n", "")
    (tmp_path / "repo" / ORIG).write_text(text.replace("superseded_by: SPEC-0101", ""))
    assert Q.backlog_readiness(cfg, dep) == (True, [])


def _chain_fx(tmp_path, specs):
    """Specs 0005 (the dependency X) and `specs` {number: (status, fm)}, plus the accepted
    dependent SPEC-0006 on X; returns the dependent's campaign dict."""
    repo = make_project(tmp_path, with_epic=True)
    _write(repo, "docs/specs/0006-dependent.md", _spec_md(
        6, None, title="Dependent", status="accepted", fm={"depends_on": "[SPEC-0005]"}))
    for number, (status, fm) in specs.items():
        _write(repo, f"docs/specs/{number:04d}-s{number}.md", _spec_md(
            number, None, title=f"S{number}", status=status, fm=fm))
    return {"kind": "implement", "repo": {"root": str(repo)},
            "spec": {"rel_path": "docs/specs/0006-dependent.md"}}


def test_superseded_by_only_replacement_is_waited_for(tmp_path):
    dep = _chain_fx(tmp_path, {5: ("superseded", {"superseded_by": "SPEC-0101"}),
                               101: ("accepted", {})})         # no `supersedes` back-link
    assert Q.backlog_readiness({}, dep) == (
        False, ["waiting on SPEC-0005 (superseded; waiting on SPEC-0101)"])
    p = tmp_path / "repo" / "docs/specs/0101-s101.md"
    p.write_text(p.read_text().replace("status: accepted", "status: done"))
    assert Q.backlog_readiness({}, dep) == (True, [])


def test_superseded_replacement_that_is_itself_superseded_recurses(tmp_path):
    dep = _chain_fx(tmp_path, {
        5: ("superseded", {"superseded_by": "SPEC-0101"}),
        101: ("superseded", {"supersedes": "[SPEC-0005]", "superseded_by": "SPEC-0102"}),
        102: ("accepted", {"supersedes": "[SPEC-0101]"})})
    assert Q.backlog_readiness({}, dep) == (
        False, ["waiting on SPEC-0005 (superseded; waiting on SPEC-0101)"])
    p = tmp_path / "repo" / "docs/specs/0102-s102.md"
    p.write_text(p.read_text().replace("status: accepted", "status: done"))
    assert Q.backlog_readiness({}, dep) == (True, [])


def test_supersession_cycle_terminates(tmp_path):
    dep = _chain_fx(tmp_path, {
        5: ("superseded", {"superseded_by": "SPEC-0101"}),
        101: ("superseded", {"supersedes": "[SPEC-0005, SPEC-0102]", "superseded_by": "SPEC-0102"}),
        102: ("superseded", {"supersedes": "[SPEC-0101]", "superseded_by": "SPEC-0101"})})
    assert Q.backlog_readiness({}, dep) == (True, [])


def test_spec_that_supersedes_and_depends_on_the_same_spec_is_not_blocked_by_itself(tmp_path):
    repo = make_project(tmp_path, with_epic=True)
    _write(repo, ORIG, _spec_md(5, None, status="superseded", fm={"superseded_by": "SPEC-0006"}))
    _write(repo, "docs/specs/0006-dependent.md", _spec_md(
        6, None, title="Dependent", status="accepted",
        fm={"depends_on": "[SPEC-0005]", "supersedes": "[SPEC-0005]"}))
    state = {"kind": "implement", "repo": {"root": str(repo)},
             "spec": {"rel_path": "docs/specs/0006-dependent.md"}}
    assert Q.backlog_readiness({}, state) == (True, [])


# ---- finalize and close ------------------------------------------------------------------------

def test_finalize_split_supersedes_original(tmp_path, monkeypatch):
    cfg, repo, cid = written(tmp_path, monkeypatch)
    wt = wtree(cfg, cid)
    head = _git(wt, "rev-parse", "HEAD")
    st = run_ticks(cfg, cid, 1)
    assert st["state"] == "CLOSING", st.get("park")
    text = (wt / ORIG).read_text()
    assert "status: superseded" in text and "superseded_by: SPEC-0101" in text
    assert "status: accepted" not in text.split("---")[1]
    assert "SPEC-0005" in (wt / "docs/LEDGER.md").read_text()
    assert _git(wt, "rev-list", "--count", f"{head}..HEAD") == "1"
    assert _git(wt, "show", "-s", "--format=%s", "HEAD").startswith(
        f"nw({cid}): split SPEC-0005 into SPEC-0101, SPEC-0102, SPEC-0103")
    (ev,) = ledger(cfg, cid, "split_finalized")
    assert ev["original"] == "SPEC-0005" and ev["parts"] == ["SPEC-0101", "SPEC-0102", "SPEC-0103"]
    assert SC.ledger_problems(ev) == []
    assert subprocess.run([sys.executable, "tools/spec_lint.py"], cwd=wt,
                          capture_output=True, text=True).returncode == 0


def test_finalize_split_idempotent(tmp_path, monkeypatch):
    cfg, repo, cid = written(tmp_path, monkeypatch)
    run_ticks(cfg, cid, 1)
    wt = wtree(cfg, cid)
    head = _git(wt, "rev-parse", "HEAD")
    res = Q._do_finalize_split(Q._Ctx(cfg), S.read_state(cfg, cid))
    assert res.error is None and res.write["sha"] is None
    assert _git(wt, "rev-parse", "HEAD") == head
    assert SW.supersede(wt, ORIG, "SPEC-0101") is False
    assert (wt / ORIG).read_text().count("superseded_by:") == 1


def test_supersede_rewrites_only_front_matter_status_lines(tmp_path):
    f = tmp_path / "s.md"
    f.write_text("---\nid: SPEC-0005\nstatus: accepted\nupdated: 2020-01-01\nowner: u\n---\n\n"
                 "## Context\n\nstatus: accepted in the body\n")
    assert SW.supersede(tmp_path, "s.md", "SPEC-0101") is True
    text = f.read_text()
    assert text.startswith("---\nid: SPEC-0005\nstatus: superseded\nsuperseded_by: SPEC-0101\n"
                           "updated: ")
    assert text.endswith("owner: u\n---\n\n## Context\n\nstatus: accepted in the body\n")


def test_split_close_skips_review_and_followups(tmp_path, monkeypatch):
    cfg, repo, cid = written(tmp_path, monkeypatch)
    filed = []
    monkeypatch.setattr(Q.FU, "file_followups", lambda *a, **k: filed.append(a) or ([], {}))
    st = run_ticks(cfg, cid, 3)                          # finalize, then the close
    assert st["state"] == "MERGED", st.get("park")
    assert st["close"]["mode"] == "merge"
    personas = {e["persona"] for e in ledger(cfg, cid, "run.start")}
    assert personas == {"spec_writer"}
    assert not ledger(cfg, cid, "followup")
    assert "followups" not in st["close"] and filed == []        # the filer is never even called
    assert "superseded_by: SPEC-0101" in (repo / ORIG).read_text()
    assert (repo / "docs/specs/0103-part-3.md").is_file()
    assert subprocess.run([sys.executable, "tools/spec_lint.py"], cwd=repo,
                          capture_output=True, text=True).returncode == 0


def test_split_check_failure_resumes_to_spec_writing(tmp_path, monkeypatch):
    cfg, repo, cid = written(tmp_path, monkeypatch)
    run_ticks(cfg, cid, 1)                               # finalize -> CLOSING
    wt = wtree(cfg, cid)
    (wt / "docs/specs/0102-part-2.md").unlink()          # a part goes missing
    _git(wt, "commit", "-qam", "lose part 2")
    Q.tick(cfg)
    park = parked(cfg, cid)
    assert park["reason"] == "spec_write_failed" and park["resume_to"] == "SPEC_WRITING"
    assert "part 2 (Part 2): spec file missing" in park["detail"]
    with S.item_lock(cfg, cid):          # the human clears the part and the ones chained to it
        st = S.read_state(cfg, cid)
        for p in st["split"]["parts"][1:]:
            p.update(number=None, rel_path=None, spec_id=None)
        S.write_state(cfg, cid, st)
    (wt / "docs/specs/0103-part-3.md").unlink()
    _git(wt, "commit", "-qam", "lose part 3")
    # part 3 still holds its reservation (103), so part 2 gets the next free number
    script(tmp_path, good_parts() + [part(104, 2, prev=101), part(103, 3, prev=104)])
    resume(cfg, cid)
    st = run_ticks(cfg, cid, 2)
    assert st["state"] == "SPEC_WRITING", st["park"]
    assert [p["spec_id"] for p in st["split"]["parts"]] == ["SPEC-0101", "SPEC-0104", "SPEC-0103"]
    st = run_ticks(cfg, cid, 1)                          # finalize again, idempotent
    assert st["state"] == "CLOSING"
    assert len(ledger(cfg, cid, "split_finalized")) == 2
    assert run_ticks(cfg, cid, 1)["state"] == "MERGED"


def test_split_red_gate_parks_gates_failed_then_resumes_to_merged(tmp_path, monkeypatch):
    cfg, repo, cid = written(tmp_path, monkeypatch)
    run_ticks(cfg, cid, 1)                               # finalize -> CLOSING
    wt = wtree(cfg, cid)
    (wt / "feature.txt").unlink()                        # the project's test gate wants it
    _git(wt, "commit", "-qam", "break the gate")
    for _ in range(4):
        if S.read_state(cfg, cid)["state"] == "PARKED":
            break
        Q.tick(cfg)
    park = parked(cfg, cid)                              # no triager for a split campaign
    assert park["reason"] == "gates_failed" and park["resume_to"] == "CLOSING"
    assert "triager" not in {e["persona"] for e in ledger(cfg, cid, "run.start")}
    _write(wt, "feature.txt", "x\n")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-qm", "fix the gate")
    resume(cfg, cid)
    assert run_ticks(cfg, cid, 3)["state"] == "MERGED"


def test_finalize_failure_parks_rolls_back_and_a_resume_succeeds(tmp_path, monkeypatch):
    cfg, repo, cid = written(tmp_path, monkeypatch)
    wt = wtree(cfg, cid)
    head = _git(wt, "rev-parse", "HEAD")
    original = (wt / ORIG).read_text()
    (wt / "feature.txt").write_text("edited\n")          # tracked and dirty: finalize refuses
    st = run_ticks(cfg, cid, 1)
    assert st["state"] == "PARKED"
    park = st["park"]
    assert park["reason"] == "spec_write_failed" and park["resume_to"] == "SPEC_WRITING"
    assert park["detail"].startswith("finalize_split step failed:")
    assert (wt / ORIG).read_text() == original and _git(wt, "rev-parse", "HEAD") == head
    assert not ledger(cfg, cid, "split_finalized")
    _git(wt, "checkout", "--", "feature.txt")
    resume(cfg, cid)
    st = run_ticks(cfg, cid, 1)                          # every part is written: finalize again
    assert st["state"] == "CLOSING", st.get("park")
    assert "superseded_by: SPEC-0101" in (wt / ORIG).read_text()
    assert len(ledger(cfg, cid, "split_finalized")) == 1


def test_finalize_rolls_back_when_the_original_has_no_front_matter(tmp_path, monkeypatch):
    cfg, repo, cid = written(tmp_path, monkeypatch)
    wt = wtree(cfg, cid)
    (wt / ORIG).write_text("no front matter here\n")
    _git(wt, "commit", "-qam", "break the original")
    head = _git(wt, "rev-parse", "HEAD")
    run_ticks(cfg, cid, 1)
    park = parked(cfg, cid)
    assert park["reason"] == "spec_write_failed" and park["resume_to"] == "SPEC_WRITING"
    assert "no front matter" in park["detail"]
    assert _git(wt, "rev-parse", "HEAD") == head and _git(wt, "status", "--porcelain", "-uno") == ""


def test_supersede_survives_crlf_and_block_list_superseded_by(tmp_path):
    f = tmp_path / "s.md"
    f.write_bytes(b"---\r\nid: SPEC-0005\r\nstatus: accepted\r\nupdated: 2020-01-01\r\n---\r\n\r\nbody\r\n")
    assert SW.supersede(tmp_path, "s.md", "SPEC-0101") is True
    fm, body = SW.split_frontmatter(f.read_text())       # read_text folds CRLF; the file is LF now
    assert fm["status"] == "superseded" and fm["superseded_by"] == "SPEC-0101"
    assert body == "\nbody\n"
    g = tmp_path / "t.md"
    g.write_text("---\nid: SPEC-0005\nstatus: accepted\nsuperseded_by:\n  - SPEC-0050\n"
                 "  - SPEC-0051\nowner: u\n---\n\nbody\n")
    assert SW.supersede(tmp_path, "t.md", "SPEC-0101") is True
    text = g.read_text()
    assert "SPEC-0050" not in text and "SPEC-0051" not in text
    assert text.count("superseded_by:") == 1 and "owner: u" in text
    fm, _ = SW.split_frontmatter(text)
    assert fm["superseded_by"] == "SPEC-0101" and fm["owner"] == "u"


def test_split_with_an_empty_part_title_gets_a_default_title(tmp_path, monkeypatch):
    repo = make_project(tmp_path, with_epic=True)
    _write(repo, ORIG, _spec_md(5, None, title="Big thing", status="accepted"))
    _lint_ledger(repo)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "x")
    cfg = make_cfg(tmp_path, repo, script(tmp_path, []))
    write_review(cfg, repo / ORIG, "SPEC-0005", "split", suggested_splits=[
        {"title": "  ", "scope": "sa"}, {"scope": "sb"}])
    cid = added(cfg, monkeypatch, "SPEC-0005")
    assert [p["title"] for p in S.read_state(cfg, cid)["split"]["parts"]] == ["Part 1", "Part 2"]
