"""SPEC-0246: the campaign detail view model (no Textual)."""
import json

import pytest

from test_agentic_dashboard import _campaign, _ev, _iso
from test_agentic_e2e import impl_done, make_cfg, make_repo, script
from nightwright.agentic import schemas as SC
from nightwright.agentic import spec_review as SR
from nightwright.agentic import store as S
from nightwright.tui import agentic_model as AM


@pytest.fixture
def env(tmp_path):
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, [impl_done()]))
    return repo, cfg


def _state(cfg, cid):
    return S.read_state(cfg, cid)


def _patch(cfg, cid, **kw):
    st = _state(cfg, cid)
    st.update(kw)
    S.write_state(cfg, cid, st)


def _plain(lines):
    return [t.plain for t in lines]


# --- header ---------------------------------------------------------------------------------

def test_fresh_campaign_header_has_lines_one_to_three(env):
    repo, cfg = env
    cid = _campaign(cfg, repo, "SPEC-0001-a", "IMPLEMENTING")
    lines = _plain(AM.load_campaign(cfg, cid).header)
    assert len(lines) == 3
    assert lines[0].startswith("SPEC-0001-a  IMPLEMENTING  spec SPEC-0001 ")
    assert "project Meta-Tools" in lines[0]
    assert lines[1].startswith("loops 1/") and "runs 1" in lines[1] and "cost $1.25" in lines[1]
    assert lines[1].endswith("/ $10.00")
    assert lines[2] == "branch nw/SPEC-0001-a  worktree /tmp/nw/SPEC-0001-a (missing)"


def test_sized_parked_header_shows_every_line(env, tmp_path):
    repo, cfg = env
    wt = tmp_path / "present"
    wt.mkdir()
    cid = _campaign(cfg, repo, "SPEC-0001-b", "PARKED",
                    park={"reason": "rate_limited", "detail": "429", "resume_to": "GATING"},
                    sizing={"size": "m", "tone": "careful", "source": "spec_review"},
                    cost={"total_usd": 2.0, "estimated_usd": 0.5, "cap_usd": 8.0},
                    close={"followups": {"filed": [{"idea_id": "IDEA-001"}],
                                         "waived": [{}], "capped": [{}, {}]}})
    st = _state(cfg, cid)
    st["repo"]["worktree"] = str(wt)
    S.write_state(cfg, cid, st)
    lines = _plain(AM.load_campaign(cfg, cid).header)
    assert len(lines) == 5
    assert "(~$0.50 est.) / $8.00" in lines[1]
    assert "profile balanced (spec_review)  size m  tone careful" in lines[1]
    assert lines[2] == f"branch nw/SPEC-0001-b  worktree {wt}"
    assert lines[3] == "parked rate_limited: 429 → resume to GATING"
    assert lines[4] == "follow-ups IDEA-001 · 1 waived · 2 capped"
    # a missing worktree
    cid2 = _campaign(cfg, repo, "SPEC-0001-c", "QUEUED",
                     park_requested={"reason": "human", "detail": "hold"})
    lines2 = _plain(AM.load_campaign(cfg, cid2).header)
    assert lines2[2].endswith("(missing)") and lines2[3] == "park requested: hold"


def test_followups_line_distinct_ids_and_counts_only(env):
    repo, cfg = env
    cid = _campaign(cfg, repo, "SPEC-0001-d", "MERGED", close={"followups": {"filed": [
        {"idea_id": "IDEA-007"}, {"idea_id": "IDEA-008"}, {"idea_id": "IDEA-007"}]}})
    assert _plain(AM.load_campaign(cfg, cid).header)[-1] == "follow-ups IDEA-007 IDEA-008"
    _patch(cfg, cid, close={"followups": {"unfiled": [{}], "waived": [{}, {}]}})
    assert _plain(AM.load_campaign(cfg, cid).header)[-1] == "1 unfiled · 2 waived"
    _patch(cfg, cid, close={"followups": {}})
    assert len(AM.load_campaign(cfg, cid).header) == 3


# --- timeline -------------------------------------------------------------------------------

def test_group_membership_covers_every_event_type():
    expect = {"transitions": {"open", "transition", "park", "resume", "priority", "close",
                              "admitted_after_revision", "admitted_revise_pending"},
              "runs": {"run.start", "run.end", "cost"}, "gates": {"gate"},
              "findings": {"finding", "followup"}}
    for group, types in expect.items():
        for t in types:
            assert AM.event_group(t) == group, t
    for t in set(SC.EVENT_KEYS) - set().union(*expect.values()):
        assert AM.event_group(t) == "notes", t
    assert {"note", "commit", "backlog"} <= {t for t in SC.EVENT_KEYS if AM.event_group(t) == "notes"}
    assert AM.event_group("something.new") == "notes"


def test_entries_follow_the_ledger_with_refs(env):
    repo, cfg = env
    cid = _campaign(cfg, repo, "SPEC-0001-e", "IMPLEMENTING")
    _ev(cfg, cid, "note", {"text": "hello"})
    _ev(cfg, cid, "run.start", {"persona": "implementer", "run_id": "001-implementer"})
    _ev(cfg, cid, "gate", {"gate_seq": 3, "ok": True, "results": []})
    _ev(cfg, cid, "commit", {"sha": "abc", "message": "m"})
    d = AM.load_campaign(cfg, cid)
    assert [e.seq for e in d.entries] == sorted(e.seq for e in d.entries)
    assert [e.type for e in d.entries][-4:] == ["note", "run.start", "gate", "commit"]
    assert [e.ref for e in d.entries][-4:] == [None, "001-implementer", 3, None]
    assert [e.group for e in d.entries][-4:] == ["notes", "runs", "gates", "notes"]
    note = d.entries[-4]
    assert "text=hello" in note.text and "note" not in note.text.split()[0]


# --- entry_detail ---------------------------------------------------------------------------

def _run_dir(cfg, cid, rid):
    d = S.runs_dir(cfg, cid) / rid
    d.mkdir(parents=True)
    return d


def _entry(cfg, cid, etype):
    return [e for e in AM.load_campaign(cfg, cid).entries if e.type == etype][-1]


def test_run_detail_with_a_run_dir(env):
    repo, cfg = env
    cid = _campaign(cfg, repo, "SPEC-0001-f", "IMPLEMENTING")
    d = _run_dir(cfg, cid, "001-implementer")
    (d / "prompt.md").write_text("p")
    (d / "result.json").write_text(json.dumps({"permission_denials": [{"tool": "Bash"}]}))
    (d / "stderr.log").write_text("\n".join(f"err {i}" for i in range(60)))
    _ev(cfg, cid, "run.start", {"persona": "implementer", "run_id": "001-implementer",
                                "model": "sonnet", "effort": "high"})
    _ev(cfg, cid, "run.end", {"persona": "implementer", "run_id": "001-implementer",
                              "duration_s": 12.5, "cost_usd": 0.5, "verdict_ok": True,
                              "verdict": {"status": "blocked", "summary": "stuck on X",
                                          "must_fix": ["fix A"], "blockers": ["B"]}})
    out = AM.entry_detail(cfg, cid, _entry(cfg, cid, "run.end")).plain
    for needle in ("persona: implementer", "model: sonnet", "effort: high", "verdict_status: blocked",
                   "duration_s: 12.5", "cost_usd: 0.5", "stuck on X", "- fix A", "- B",
                   "permission_denials: 1", "err 59", "prompt:", "stderr:", "dir:"):
        assert needle in out, needle
    assert "err 19" not in out         # only the stderr tail
    assert str(d / "prompt.md") in out


def test_run_detail_without_a_run_dir_and_unresolvable_run(env):
    repo, cfg = env
    cid = _campaign(cfg, repo, "SPEC-0001-g", "IMPLEMENTING")
    _ev(cfg, cid, "run.start", {"persona": "implementer", "run_id": "002-implementer"})
    out = AM.entry_detail(cfg, cid, _entry(cfg, cid, "run.start")).plain
    assert "persona: implementer" in out and "(missing:" in out
    # an entry whose run id campaign_run cannot resolve
    ghost = AM.TimelineEntry(99, "", "run.end", "runs", "", "zzz-nope",
                             {"type": "run.end", "run_id": "zzz-nope", "persona": "x"})
    out = AM.entry_detail(cfg, cid, ghost).plain
    assert "(run unavailable: no run 'zzz-nope'" in out and "persona: x" in out


def test_cost_and_finding_detail_are_unclipped(env):
    repo, cfg = env
    cid = _campaign(cfg, repo, "SPEC-0001-h", "IMPLEMENTING")
    long = "x" * 300
    _ev(cfg, cid, "cost", {"persona": "implementer", "delta_usd": 0.1, "total_usd": 1.0})
    _ev(cfg, cid, "finding", {"persona": "reviewer", "severity": "major", "title": "T",
                              "detail": long})
    cost = AM.entry_detail(cfg, cid, _entry(cfg, cid, "cost")).plain
    assert "delta_usd: 0.1" in cost and "total_usd: 1.0" in cost
    assert long in AM.entry_detail(cfg, cid, _entry(cfg, cid, "finding")).plain


def test_gate_detail_tails_failed_logs_and_reports_missing_ones(env):
    repo, cfg = env
    cid = _campaign(cfg, repo, "SPEC-0001-i", "GATING")
    gates = S.gates_dir(cfg, cid)
    gates.mkdir(parents=True, exist_ok=True)
    (gates / "0001-test.log").write_text("\n".join(f"line {i}" for i in range(100)))
    rel = (gates / "0001-test.log").relative_to(S.campaign_dir(cfg, cid)).as_posix()
    _ev(cfg, cid, "gate", {"gate_seq": 1, "ok": False, "duration_s": 3.0, "results": [
        {"name": "pytest", "ok": False, "required": True, "exit_code": 1, "duration_s": 2.0,
         "log": rel},
        {"name": "lint", "ok": False, "required": False, "exit_code": 2, "duration_s": 1.0,
         "log": "gates/0001-lint.log"},
        {"name": "fmt", "ok": True, "required": True, "exit_code": 0, "duration_s": 0.1,
         "log": "gates/0001-fmt.log"}]})
    out = AM.entry_detail(cfg, cid, _entry(cfg, cid, "gate")).plain
    assert "pytest: FAIL" in out and "fmt: ok" in out and "exit=2" in out
    assert "line 99" in out and "line 60" in out and "line 59" not in out
    assert "(missing:" in out and "0001-lint.log" in out
    assert "0001-fmt.log" not in out        # passing gates show no log


# --- review_text ----------------------------------------------------------------------------

def _verdicts(findings):
    return {"conformance_reviewer": {"verdict": "fix", "score": None, "fix_brief": None,
                                     "findings": findings, "run": "003-r", "loop": 1}}


def test_review_text_derives_pass_score_and_blocking(env):
    repo, cfg = env
    cid = _campaign(cfg, repo, "SPEC-0001-j", "IMPLEMENTING", last_review={
        "verdict": "fix", "score": 55, "loop": 1,
        "verdicts": _verdicts([{"severity": "must_fix", "title": "Boom", "files": ["a.py"]},
                               {"severity": "nit", "title": "Tiny"}]),
        "advisory": [{"id": "conformance_reviewer#3", "severity": "major",
                      "personas": ["conformance_reviewer"], "title": "Adv", "files": []}],
        "waived": [{"id": "conformance_reviewer#1", "severity": "nit",
                    "personas": ["conformance_reviewer"], "title": "Tiny",
                    "files": [], "reason": "style only"}]})
    out = AM.review_text(cfg, _state(cfg, cid)).plain
    assert "score 55 / pass " in out and "pass ?" not in out
    assert "verdict fix  loop 1" in out
    block = out.split("blocking:")[1].split("advisory:")[0]
    assert "conformance_reviewer#0 must_fix conformance_reviewer Boom (a.py)" in block
    assert "Tiny" not in block                      # waived, so not blocking
    assert "conformance_reviewer#3 major" in out.split("advisory:")[1]
    assert "reason: style only" in out.split("waived:")[1]


def test_review_text_falls_back_when_the_profile_does_not_resolve(env):
    repo, cfg = env
    cid = _campaign(cfg, repo, "SPEC-0001-k", "IMPLEMENTING", profile="vanished", last_review={
        "verdict": "fix", "score": 40, "loop": 2, "verdicts": _verdicts([]), "advisory": [],
        "waived": []})
    out = AM.review_text(cfg, _state(cfg, cid)).plain
    assert "score 40 / pass ?  verdict fix  loop 2" in out
    assert "unavailable (" in out


def test_review_text_placeholders_and_stored_spec_review(env):
    repo, cfg = env
    bare = _campaign(cfg, repo, "SPEC-0077-l", "IMPLEMENTING", spec_id="SPEC-0077",
                     rel="docs/specs/0077-none.md")
    out = AM.review_text(cfg, _state(cfg, bare)).plain
    assert "no code review yet" in out and "no spec review" in out
    cid = _campaign(cfg, repo, "SPEC-0001-l", "IMPLEMENTING")
    spec_path = repo / "docs/specs/0001-trivial.md"
    SR.save_review(cfg, {"schema": SR.SCHEMA, "spec_id": "SPEC-0001", "verdict": "revise",
                         "spec_sha256": SR.spec_sha256(spec_path), "reviewed_at": _iso(5),
                         "cost_usd": 0.4, "size": "s", "tone": "light", "rationale": "small",
                         "findings": [{"severity": "minor", "title": "Wording"}],
                         "repo_root": str(repo)})
    out = AM.review_text(cfg, _state(cfg, cid)).plain
    assert "verdict revise  size s  tone light  cost $0.40" in out
    assert "rationale: small" in out and "minor Wording" in out
    # an idea-only campaign never reads a spec review
    idea = _campaign(cfg, repo, "IDEA-001-x", "QUEUED", spec_id=None, kind="idea",
                     idea={"id": "IDEA-001", "heading": "An idea"})
    assert "no spec review" in AM.review_text(cfg, _state(cfg, idea)).plain


# --- signature ------------------------------------------------------------------------------

def test_signature_changes_on_a_ledger_append(env):
    repo, cfg = env
    cid = _campaign(cfg, repo, "SPEC-0001-m", "IMPLEMENTING")
    before = AM.campaign_signature(cfg, cid)
    assert AM.campaign_signature(cfg, cid) == before
    _ev(cfg, cid, "note", {"text": "x"})
    assert AM.campaign_signature(cfg, cid) != before


def test_unknown_campaign_raises_value_error(env):
    repo, cfg = env
    with pytest.raises(ValueError):
        AM.load_campaign(cfg, "SPEC-9999-nope")
