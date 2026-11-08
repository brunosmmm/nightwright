"""SPEC-0186: the spec-change guard. `spec_drift` allows only the edits an implementer is
supposed to make (ticks, status/updated, Test record / As shipped subsections, Clock Log) and
reports everything else; the close step routes drift to TRIAGING; end to end a campaign whose
implementer deletes an AC never merges."""
import hashlib
from pathlib import Path

import pytest

from nightwright.agentic import close as CL
from nightwright.agentic import store as S

from test_agentic_e2e import (SPEC_ACCEPTED, _git, added, make_cfg, make_repo, run_cli,
                              script, review, triage)

BASE = """---
id: SPEC-0900
title: "Guarded"
status: accepted
owner: t
created: 2026-10-25
depends_on: [SPEC-0176]
---

## Context

Why.

## Decision

Do the thing.

## Design

- module x

## Acceptance criteria

- [ ] first criterion
- [ ] second criterion

## Test plan

- **Automated tests:** test_x.

## Clock Log

Internal work.

## Definition of done

- [ ] Acceptance criteria all met.
"""


def _allowed_edit(text):
    text = text.replace("status: accepted", "status: done")
    text = text.replace("created: 2026-10-25\n", "created: 2026-10-25\nupdated: 2026-10-26\n")
    text = text.replace("- [ ]", "- [x]")
    text = text.replace("## Clock Log\n\nInternal work.", "## Clock Log\n\nCLOCK-IN: [2026-10-26 10:00]")
    return text.replace("## Clock Log", "### Test record (2026-10-26)\n\n- ran test_x: 3 passed\n\n"
                        "### As shipped\n\n- used helper y\n\n## Clock Log", 1)


@pytest.mark.parametrize("edit", [
    lambda t: t.replace("status: accepted", "status: in-progress"),
    lambda t: t.replace("- [ ] first", "- [x] first"),
    lambda t: t.replace("created: 2026-10-25\n", "created: 2026-10-25\nupdated: 2026-10-26\n"),
    lambda t: t.replace("## Clock Log", "### Test record (2026-10-26)\n\n- ok\n\n## Clock Log"),
    lambda t: t.replace("## Clock Log\n\nInternal work.", "## Clock Log\n\nCLOCK-OUT: x"),
    _allowed_edit,
], ids=["status", "tick", "updated", "test-record", "clock-log", "all-allowed"])
def test_allowed_edits_are_not_drift(edit):
    assert CL.spec_drift(BASE, edit(BASE)) == []


@pytest.mark.parametrize("edit,needle", [
    (lambda t: t.replace("- [ ] second criterion\n", ""), "second criterion"),
    (lambda t: t.replace("second criterion", "second criterion (best effort)"), "best effort"),
    (lambda t: t.replace("- [ ] second criterion\n", "- [ ] second criterion\n- [ ] third\n"),
     "third"),
    (lambda t: t.replace("Do the thing.", "Do a smaller thing."), "smaller"),
    (lambda t: t.replace("- module x", "- module z"), "module z"),
    (lambda t: t.replace("test_x.", "none."), "none."),
    (lambda t: t.replace("depends_on: [SPEC-0176]\n", ""), "SPEC-0176"),
    (lambda t: _allowed_edit(t).replace("- [x] second criterion\n", ""), "second criterion"),
], ids=["delete-ac", "reword-ac", "add-ac", "decision", "design", "test-plan", "frontmatter",
        "allowed-plus-deleted-ac"])
def test_governing_edits_are_drift(edit, needle):
    hunks = CL.spec_drift(BASE, edit(BASE))
    assert hunks and any(needle in h for h in hunks)
    assert all(h.startswith("@@") for h in hunks)


def _state(tmp_path, cfg, text):
    wt = tmp_path / "nw"
    (wt / "docs/specs").mkdir(parents=True)
    (wt / "docs/specs/0900-g.md").write_text(text)
    st = {"id": "SPEC-0900-20261026-aaaa", "spec": {"rel_path": "docs/specs/0900-g.md"},
          "repo": {"worktree": str(wt)}}
    S.campaign_dir(cfg, st["id"]).mkdir(parents=True)
    return st


def test_guard_without_copy_is_skipped_with_info(tmp_path):
    cfg = {"data_dir": str(tmp_path / "data")}
    st = _state(tmp_path, cfg, BASE)
    ok, gate, events = CL.spec_guard(cfg, st)
    assert ok and gate is None
    assert events[0][1]["severity"] == "info" and events[0][1]["gate"] == "spec-changed"


def test_guard_fails_when_hash_set_but_snapshot_missing(tmp_path):
    cfg = {"data_dir": str(tmp_path / "data")}
    st = _state(tmp_path, cfg, BASE)
    st["spec"]["sha256_at_open"] = hashlib.sha256(BASE.encode("utf-8")).hexdigest()
    ok, gate, events = CL.spec_guard(cfg, st)
    assert not ok and gate["name"] == "spec-changed"
    assert events[0][1]["severity"] == "must_fix"


def test_guard_reports_drift_as_synthetic_gate(tmp_path):
    cfg = {"data_dir": str(tmp_path / "data")}
    st = _state(tmp_path, cfg, BASE.replace("- [ ] second criterion\n", ""))
    copy = S.campaign_dir(cfg, st["id"]) / CL.SPEC_AT_OPEN
    copy.write_text(BASE)
    st.setdefault("spec", {})["sha256_at_open"] = __import__("hashlib").sha256(
        BASE.encode("utf-8")).hexdigest()
    ok, gate, events = CL.spec_guard(cfg, st)
    assert not ok
    assert gate["name"] == "spec-changed" and gate["required"] and not gate["ok"]
    assert "second criterion" in gate["tail"]
    assert events[0][1]["severity"] == "must_fix"
    # allowed edits only: passes with no events
    (Path(st["repo"]["worktree"]) / "docs/specs/0900-g.md").write_text(_allowed_edit(BASE))
    st["spec"]["sha256_at_open"] = __import__("hashlib").sha256(
        _allowed_edit(BASE).encode("utf-8")).hexdigest()
    copy.write_text(_allowed_edit(BASE))
    assert CL.spec_guard(cfg, st) == (True, None, [])


# ---- end to end -----------------------------------------------------------------------------

def _impl_deleting_ac(spec_rel="docs/specs/0001-trivial.md"):
    done_without_ac = (SPEC_ACCEPTED.format(id="SPEC-0001", extra="")
                       .replace("status: accepted", "status: done")
                       .replace("- [ ] feature.txt exists\n", "")
                       .replace("- [ ]", "- [x]"))
    return {"persona": "implementer", "edits": [
        {"path": "feature.txt", "write": "hello\n"},
        {"path": spec_rel, "write": done_without_ac}],
        "verdict": {"status": "done", "summary": "wrote feature.txt", "changed_files": [],
                    "tests_run": [], "ac_status": [], "blockers": [], "followup_ideas": []},
        "cost_usd": 0.5}


def test_e2e_implementer_deleting_an_ac_never_merges(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    head_before = _git(repo, "rev-parse", "HEAD")
    entries = [_impl_deleting_ac(), review(), triage("escalate"), triage("escalate")]
    cfg = make_cfg(tmp_path, repo, script(tmp_path, entries))
    cid = added(cfg, monkeypatch)
    r = run_cli(cfg, monkeypatch, ["queue", "run", "--until-idle"])
    assert r.exit_code == 0, r.output
    st = S.read_state(cfg, cid)
    assert st["state"] == "PARKED", st
    trans = [(e["from"], e["to"]) for e in S.read_ledger(cfg, cid, types=["transition"])]
    assert ("REVIEWING", "CLOSING") in trans and ("CLOSING", "TRIAGING") in trans
    assert all(to != "MERGED" for _f, to in trans)
    findings = S.read_ledger(cfg, cid, types=["finding"])
    drift = [f for f in findings if f.get("gate") == "spec-changed"]
    assert drift and drift[0]["severity"] == "must_fix" and "feature.txt exists" in drift[0]["detail"]
    assert _git(repo, "rev-parse", "HEAD") == head_before
    assert (S.campaign_dir(cfg, cid) / CL.SPEC_AT_OPEN).read_text() == \
        SPEC_ACCEPTED.format(id="SPEC-0001", extra="")
    # the triager saw the synthetic gate
    assert any(r_["name"] == "spec-changed" for r_ in (st.get("last_gate") or {}).get("results", []))
