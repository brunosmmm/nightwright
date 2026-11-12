"""SPEC-0572: spec-phase sessions (`agentic.lean.spec_sessions`) — the flag and its pin shape,
the spec writer's resumed revise round, the spec reviewer's delta re-review, the resume
fallback, the effort ceiling on a resumed run, and flag off / pinned before the flag unchanged
(FakeRunner idea campaigns, as in `test_agentic_spec_review_loop`)."""
import json
from pathlib import Path

import pytest

from test_agentic_e2e import _git, make_cfg, script
from test_agentic_idea_campaign import add_idea_campaign, capture
from test_agentic_spec_review_loop import finding, reviewed, writes
from test_agentic_spec_writer import make_project, parked, resume, spec_written, wtree
from nightwright.agentic import gitops
from nightwright.agentic import runner as R
from nightwright.agentic import sessions as SS
from nightwright.agentic import store as S
from nightwright.agentic import queue as Q
from nightwright.agentic import tiers as T
from nightwright.agentic.gitops import GitError

pytestmark = pytest.mark.integration

SPEC = "docs/specs/0002-add-a-thing.md"
SPEC_ON = {"spec_sessions": True}
CODE_ON = {"packet": True, "collator": True, "sessions": True}     # pinned before SPEC-0572


# ---- the flag -----------------------------------------------------------------------------------

def test_flag_has_no_dependency_and_is_pinned_only_when_on():
    assert "spec_sessions" in T.LEAN_FLAGS and "spec_sessions" not in T.LEAN_REQUIRES
    off = {"tiers": False, "packet": False, "collator": False, "sessions": False}
    assert T.effective_lean({}, "P") == (off, [])
    assert T.effective_lean({"agentic": {"lean": {"spec_sessions": True}}}, "P") == (
        dict(off, spec_sessions=True), [])
    assert T.effective_lean({"agentic": {"project_lean": {"P": {"spec_sessions": True}}}},
                            "P")[0]["spec_sessions"] is True
    assert "spec_sessions" not in T.effective_lean(
        {"agentic": {"lean": {"spec_sessions": "yes"}}}, "P")[0]            # only literal true
    # SPEC-0558 `--lean` turns it on; `--lean-flags` may name it alone
    assert T.lean_request(True)[-1] == "spec_sessions"
    assert T.effective_lean({}, "P", request=T.lean_request(True))[0]["spec_sessions"] is True
    assert T.effective_lean({}, "P", request=["spec_sessions"]) == (
        dict(off, spec_sessions=True), [])
    assert "spec_sessions" not in T.effective_lean({}, "P", request=["tiers"])[0]


@pytest.mark.parametrize("persona", ["spec_writer", "spec_reviewer"])
def test_spec_personas_are_managed_only_with_the_flag(persona):
    st = {"id": "C1", "repo": {"worktree": "/wt"}}
    assert not SS.managed({**st, "lean": dict(CODE_ON, tiers=True)}, persona)
    assert not SS.managed(st, persona)
    assert SS.managed({**st, "lean": dict(SPEC_ON)}, persona)
    assert not SS.managed({**st, "lean": dict(SPEC_ON), "kind": "split"}, persona)
    # the code personas still need their own flags
    assert not SS.managed({**st, "lean": dict(SPEC_ON)}, "implementer")


def test_finding_status_by_section():
    old = SS._sections("## Context\n\nWhy.\n\n## Acceptance criteria\n\n- [ ] a\n")
    new = SS._sections("## Context\n\nWhy.\n\n## Acceptance criteria\n\n- [ ] b\n")
    assert SS.finding_status({"section": "Acceptance criteria"}, old, new) == "section changed"
    assert SS.finding_status({"section": "context"}, old, new) == "section unchanged"
    assert SS.finding_status({"section": "Rollout"}, old, new) == "section not found"
    assert SS.finding_status({}, old, new) == "section not found"


# ---- the queue ----------------------------------------------------------------------------------

def begin(tmp_path, monkeypatch, entries, *, lean=SPEC_ON, after_open=None, profile=None):
    """An idea campaign opened into SPEC_WRITING with `lean` in the config at creation (the pin);
    `after_open` replaces the live config flags afterwards."""
    repo = make_project(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, []), profile=profile)
    if lean is not None:
        cfg["agentic"]["lean"] = dict(lean)
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    iid = capture(cfg)
    cid = add_idea_campaign(cfg, monkeypatch, iid)
    if after_open is not None:
        cfg["agentic"]["lean"] = dict(after_open)
    Q.tick(cfg)
    assert S.read_state(cfg, cid)["state"] == "SPEC_WRITING"
    script(tmp_path, entries(iid))
    return cfg, repo, iid, cid


def tick_to(cfg, cid, states, n=8):
    for _ in range(n):
        if S.read_state(cfg, cid)["state"] in states:
            return S.read_state(cfg, cid)
        Q.tick(cfg)
    raise AssertionError(S.read_state(cfg, cid)["state"])


def run_dir(cfg, cid, name):
    return S.runs_dir(cfg, cid) / name


def doc(cfg, cid, name):
    return json.loads((run_dir(cfg, cid, name) / "request.json").read_text())


def text(cfg, cid, name, file="prompt.md"):
    return (run_dir(cfg, cid, name) / file).read_text()


def ends(cfg, cid):
    return {e["run_id"]: e for e in S.read_ledger(cfg, cid, types=["run.end"])}


def revise_round(i):
    return [spec_written(2, i), reviewed("revise", findings=[finding()]), writes(i)]


def test_the_revise_round_resumes_the_writer_with_only_the_findings(tmp_path, monkeypatch):
    cfg, repo, iid, cid = begin(tmp_path, monkeypatch, revise_round)
    st = tick_to(cfg, cid, {"IMPLEMENTING", "PARKED"})
    assert st["state"] == "IMPLEMENTING", st.get("park")
    w1, rv, w2 = "001-spec_writer", "002-spec_reviewer", "003-spec_writer"
    assert doc(cfg, cid, w1)["persist_session"] is True and "resume_session" not in doc(cfg, cid, w1)
    assert doc(cfg, cid, rv)["persist_session"] is True and "resume_session" not in doc(cfg, cid, rv)
    d2 = doc(cfg, cid, w2)
    assert d2["resume_session"] == f"fake-spec_writer-{w1}" and d2["persist_session"] is True
    cont, fresh = text(cfg, cid, w2), text(cfg, cid, w2, R.FRESH_PROMPT)
    assert cont.startswith("# Continue: spec writer")
    assert "AC 1 is untestable" in cont and "Fix: fix AC 1 is untestable" in cont
    assert "Revise mode: edit only" in cont and SPEC in cont
    for big in ("## Spec template", "## Spec schema and rules", "## Source idea"):
        assert big not in cont and big in fresh     # nothing it already read is sent again
    assert len(cont) < len(fresh) / 3
    e = ends(cfg, cid)
    assert e[w2]["resumed"] is True and e[w1]["resumed"] is False and e[rv]["resumed"] is False
    reg = st["sessions"]
    assert set(reg) == {"spec_writer", "spec_reviewer"}
    assert reg["spec_writer"]["session_id"] == f"fake-spec_writer-{w1}"
    assert reg["spec_writer"]["run_id"] == w2
    # the resumed run is charged its own cost, not the session's running total
    assert st["cost"]["by_persona"]["spec_writer"] == pytest.approx(0.8)
    assert not S.read_ledger(cfg, cid, types=["session_fallback"])
    # no second review (SPEC-0230 unchanged): the resumed revision is accepted
    assert [r["round"] for r in S.read_ledger(cfg, cid, types=["spec_review"])] == [1]
    assert "status: accepted" in (wtree(cfg, cid) / SPEC).read_text()


def test_a_failed_resume_falls_back_once_to_todays_fresh_writer(tmp_path, monkeypatch):
    cfg, repo, iid, cid = begin(tmp_path, monkeypatch, lambda i: [
        *revise_round(i)[:2], {**writes(i), "resume_error": True}, writes(i)])
    st = tick_to(cfg, cid, {"IMPLEMENTING", "PARKED"})
    assert st["state"] == "IMPLEMENTING", st.get("park")
    failed, retry = "003-spec_writer", "004-spec_writer"
    (fb,) = S.read_ledger(cfg, cid, types=["session_fallback"])
    assert fb["run_id"] == failed and fb["retry_run_id"] == retry
    assert fb["session_id"] == "fake-spec_writer-001-spec_writer"
    start = next(e for e in S.read_ledger(cfg, cid, types=["run.start"]) if e["run_id"] == retry)
    assert start["session_fallback"] is True and start["retry_of"] == failed
    d = doc(cfg, cid, retry)
    assert "resume_session" not in d and d["persist_session"] is True
    assert text(cfg, cid, retry) == text(cfg, cid, failed, R.FRESH_PROMPT)    # today's prompt
    e = ends(cfg, cid)
    assert e[failed]["is_error"] and e[failed]["resumed"] is True and e[retry]["resumed"] is False
    assert st["sessions"]["spec_writer"]["session_id"] == f"fake-spec_writer-{retry}"
    assert not S.read_ledger(cfg, cid, types=["park"]) and st["counters"].get("spec_write_retries", 0) == 0


def second_review(tmp_path, monkeypatch, *round_two):
    """The one in-campaign round 2 (SPEC-0230): a human edit of the approved spec after an
    accept failure, then a resume. Returns the campaign once it left SPEC_REVIEW."""
    cfg, repo, iid, cid = begin(tmp_path, monkeypatch, lambda i: [
        spec_written(2, i),
        reviewed("approve", findings=[finding("minor", "AC wording"),
                                      {**finding("nit", "Context is short"), "section": "Context"}]),
        *round_two])
    Q.tick(cfg)                                     # the write
    wt = wtree(cfg, cid)
    real = gitops.git

    def failing(repo_, *args, **kw):
        if args and args[0] == "commit":
            raise GitError(["git", "commit"], 1, "boom")
        return real(repo_, *args, **kw)

    monkeypatch.setattr(gitops, "git", failing)
    Q.tick(cfg)                                     # round 1 approves, the accept fails
    monkeypatch.setattr(gitops, "git", real)
    assert parked(cfg, cid)["resume_to"] == "SPEC_REVIEW"
    spec = (wt / SPEC).read_text()
    (wt / SPEC).write_text(spec.replace("- [ ] it works", "- [ ] it works on every input"))
    _git(wt, "commit", "-am", "human edit")
    resume(cfg, cid)
    st = tick_to(cfg, cid, {"IMPLEMENTING", "PARKED"})
    assert st["state"] == "IMPLEMENTING", st.get("park")
    assert [r["round"] for r in S.read_ledger(cfg, cid, types=["spec_review"])] == [1, 2]
    return cfg, cid, st


def test_a_second_review_resumes_with_the_spec_diff_and_prior_findings(tmp_path, monkeypatch):
    cfg, cid, st = second_review(tmp_path, monkeypatch, reviewed("approve"))
    r2 = "003-spec_reviewer"
    assert doc(cfg, cid, r2)["resume_session"] == "fake-spec_reviewer-002-spec_reviewer"
    cont, fresh = text(cfg, cid, r2), text(cfg, cid, r2, R.FRESH_PROMPT)
    assert cont.startswith("# Continue: spec reviewer — SPEC-0002, review round 2")
    assert "```diff" in cont and "+- [ ] it works on every input" in cont
    assert "-- [ ] it works\n" in cont
    assert "(Acceptance criteria) AC wording — section changed" in cont
    assert "(Context) Context is short — section unchanged" in cont
    assert "## Schema and rules for specs" not in cont and "## Schema and rules for specs" in fresh
    assert ends(cfg, cid)[r2]["resumed"] is True


def test_a_failed_review_resume_falls_back_to_todays_fresh_review(tmp_path, monkeypatch):
    cfg, cid, st = second_review(tmp_path, monkeypatch,
                                 {**reviewed("approve"), "resume_error": True}, reviewed("approve"))
    (fb,) = S.read_ledger(cfg, cid, types=["session_fallback"])
    assert (fb["run_id"], fb["retry_run_id"]) == ("003-spec_reviewer", "004-spec_reviewer")
    assert text(cfg, cid, "004-spec_reviewer") == \
        text(cfg, cid, "003-spec_reviewer", R.FRESH_PROMPT)
    assert "resume_session" not in doc(cfg, cid, "004-spec_reviewer")
    (ev,) = [e for e in S.read_ledger(cfg, cid, types=["spec_review"]) if e["round"] == 2]
    assert ev["run_id"] == "004-spec_reviewer"
    assert st["counters"].get("spec_review_retries", 0) == 0


def test_a_resumed_run_keeps_the_effort_ceiling(tmp_path, monkeypatch):
    prof = {"personas": {"spec_writer": {"model": "sonnet", "effort": "max"}}}
    cfg, repo, iid, cid = begin(tmp_path, monkeypatch, revise_round, profile=prof)
    st = tick_to(cfg, cid, {"IMPLEMENTING", "PARKED"})
    assert st["state"] == "IMPLEMENTING", st.get("park")
    d = doc(cfg, cid, "003-spec_writer")
    assert d["resume_session"] and d["effort"] == "high"
    start = next(e for e in S.read_ledger(cfg, cid, types=["run.start"])
                 if e["run_id"] == "003-spec_writer")
    assert start["effort"] == "high" and start["effort_clamped_from"] == "max"


def test_a_non_resumable_backend_runs_todays_spec_phase(tmp_path, monkeypatch):
    monkeypatch.setitem(R.SESSION_RESUME, "fake", False)
    cfg, repo, iid, cid = begin(tmp_path, monkeypatch, revise_round)
    st = tick_to(cfg, cid, {"IMPLEMENTING", "PARKED"})
    assert st["state"] == "IMPLEMENTING", st.get("park")
    for name in ("001-spec_writer", "002-spec_reviewer", "003-spec_writer"):
        assert not set(R.SESSION_KEYS) & set(doc(cfg, cid, name))
    assert "## Spec template" in text(cfg, cid, "003-spec_writer")
    assert "sessions" not in st


def _spec_phase(cfg, cid):
    """The spec runs' request docs and prompts, without the per-campaign paths."""
    out = []
    for name in ("001-spec_writer", "002-spec_reviewer", "003-spec_writer"):
        root = str(Path(cfg["data_dir"]).parent)
        d = {k: v for k, v in doc(cfg, cid, name).items()
             if k not in ("run_dir", "env", "cwd", "add_dirs", "started_at")}
        p = text(cfg, cid, name)
        out.append((json.dumps(d).replace(root, "<root>").replace(cid, "<cid>"),
                    p.replace(root, "<root>").replace(cid, "<cid>")))
    return out


@pytest.mark.parametrize("pinned", [None, CODE_ON, dict(CODE_ON, tiers=False)])
def test_flag_off_or_pinned_before_the_flag_is_unchanged(tmp_path, monkeypatch, pinned):
    """No session field, `state.sessions`, `resumed` mark or continuation in the spec phase,
    whatever the live config says after the open; the spec runs' requests and prompts equal a
    flag-off campaign's."""
    def boom(*a, **k):
        raise AssertionError("session shaping reached with spec_sessions off")
    base_dir = tmp_path / "base"
    base_dir.mkdir()
    cfg0, _r, _i, cid0 = begin(base_dir, monkeypatch, revise_round, lean=None)
    assert tick_to(cfg0, cid0, {"IMPLEMENTING", "PARKED"})["state"] == "IMPLEMENTING"
    baseline = _spec_phase(cfg0, cid0)
    if pinned is None:
        monkeypatch.setattr(SS, "shape", boom)
    pin_dir = tmp_path / "pin"
    pin_dir.mkdir()
    cfg, repo, iid, cid = begin(pin_dir, monkeypatch, revise_round, lean=pinned,
                                after_open=dict(CODE_ON, tiers=False, spec_sessions=True))
    st = tick_to(cfg, cid, {"IMPLEMENTING", "PARKED"})
    assert st["state"] == "IMPLEMENTING", st.get("park")
    assert "spec_sessions" not in (st.get("lean") or {})
    assert "sessions" not in st and not S.read_ledger(cfg, cid, types=["session_fallback"])
    for name in ("001-spec_writer", "002-spec_reviewer", "003-spec_writer"):
        d = doc(cfg, cid, name)
        assert not set(R.SESSION_KEYS) & set(d)
        assert not (run_dir(cfg, cid, name) / R.FRESH_PROMPT).exists()
        assert "resumed" not in ends(cfg, cid)[name]
    assert _spec_phase(cfg, cid) == baseline
