"""SPEC-0554: the review packet (`agentic/packet.py`) — AC parsing, the packet built from a
fixture repo across two loops (diff, delta, gates, prior findings and their statuses), the
marshal map (cleaned, failure -> `ac_map: null`), the builder failure, schema validation and the
loop-2 basis enforcement."""
import json
import os
import subprocess

import pytest

from nightwright.agentic import packet as PK
from nightwright.agentic import store as S

pytestmark = pytest.mark.fast

SPEC = """---
id: SPEC-0001
status: accepted
---

## Acceptance criteria

- [ ] `feature.txt` exists, and
      says hello.
- [x] The CLI prints `ok`.

* [ ] third one

## Test plan

- [ ] not an AC
"""


def _git(repo, *args):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True,
                          env=env).stdout.strip()


def _commit(repo, rel, text, msg):
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def world(tmp_path):
    repo = tmp_path / "wt"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _commit(repo, "docs/specs/0001-x.md", SPEC, "init")
    _git(repo, "checkout", "-q", "-b", "nw/x")
    _commit(repo, "feature.txt", "hello\n", "impl")
    _commit(repo, "tests/test_feature.py", "def test_exists():\n    assert True\n", "test")
    cfg = {"data_dir": str(tmp_path / "data")}
    st = {"id": "SPEC-0001-20261111-ab12", "spec_id": "SPEC-0001", "project": "P",
          "lean": {"packet": True}, "counters": {"loops": 0},
          "repo": {"worktree": str(repo), "base": "main"},
          "spec": {"rel_path": "docs/specs/0001-x.md"},
          "last_gate": {"results": [
              {"name": "pytest", "ok": True, "required": True, "exit_code": 0,
               "log": "gates/001-pytest.log", "flaky": ["tests/t.py::test_f"],
               "summary": {"passed": 3, "failed": 0, "failing": []}}]}}
    S.campaign_dir(cfg, st["id"]).mkdir(parents=True)
    return cfg, st, repo


def test_parse_acs_verbatim_in_order():
    assert PK.parse_acs(SPEC) == [
        {"id": "AC1", "text": "`feature.txt` exists, and says hello."},
        {"id": "AC2", "text": "The CLI prints `ok`."},
        {"id": "AC3", "text": "third one"}]
    assert PK.parse_acs("## Context\n\n- [ ] no\n") == []


def test_build_two_loops(world):
    cfg, st, repo = world
    meta = PK.build(cfg, st)
    assert meta == {"loop": 1, "ok": True, "mode": "full", "path": "packet-loop-1.json",
                    "marshal": "pending", "bytes": meta["bytes"]}
    cdir = S.campaign_dir(cfg, st["id"])
    p1 = json.loads((cdir / "packet-loop-1.json").read_text())
    assert PK.validate(p1) == []
    assert p1["head"] == _git(repo, "rev-parse", "HEAD") and p1["prev_head"] is None
    assert [s["path"] for s in p1["diff"]["stat"]] == ["feature.txt", "tests/test_feature.py"]
    assert "+hello" in (cdir / "packet-loop-1.diff").read_text()
    assert p1["delta"] is None and p1["ac_map"] is None and p1["findings"] == []
    assert p1["candidates"] == {"hunks": ["feature.txt:1", "tests/test_feature.py:1-2"],
                                "tests": ["tests/test_feature.py::test_exists"]}
    assert p1["gates"]["flaky"] == ["tests/t.py::test_f"]
    assert p1["gates"]["results"][0]["summary"] == {"passed": 3, "failed": 0}
    assert p1["pointers"]["gate_logs"] == ["gates/001-pytest.log"]
    assert st["review_heads"] == {"1": p1["head"]}
    # loop 1's verdicts, as `_begin_review` keeps them when loop 2's review starts
    st["packet"] = meta
    st["last_review"] = {"verdicts": {
        "conformance_reviewer": {"loop": 0, "findings": [
            {"severity": "must_fix", "title": "wrong text", "files": ["feature.txt"]},
            {"severity": "minor", "title": "style", "files": []}]},
        "adversarial_reviewer": {"loop": 0, "findings": [
            {"severity": "major", "title": "waived one", "files": ["x.py"]}]}},
        "waived": [{"title": "style", "files": [], "reason": "taste"}]}
    PK.remember_verdicts(st)
    st["human_overrides"] = {"waive": [{"title": "waived one", "files": ["x.py"],
                                        "reason": "not in scope"}]}
    st["counters"]["loops"] = 1
    _commit(repo, "feature.txt", "better\n", "fix")
    meta2 = PK.build(cfg, st)
    p2 = json.loads((cdir / "packet-loop-2.json").read_text())
    assert PK.validate(p2) == [] and meta2["mode"] == "delta"
    assert p2["prev_head"] == p1["head"] and [s["path"] for s in p2["delta"]["stat"]] == [
        "feature.txt"]
    assert "+better" in (cdir / p2["delta"]["path"]).read_text()
    status = {f["title"]: (f["persona"], f["status"], f["loop"]) for f in p2["findings"]}
    assert status == {"wrong text": ("conformance_reviewer", "open", 1),
                      "style": ("conformance_reviewer", "waived", 1),
                      "waived one": ("adversarial_reviewer", "waived", 1)}
    assert all(len(f["id"]) == 40 for f in p2["findings"])
    assert st["review_heads"]["2"] == p2["head"]


def test_prior_findings_statuses():
    prev = [{"id": "a", "persona": "c", "loop": 1, "severity": "major", "title": "A",
             "status": "open", "evidence": ""},
            {"id": "b", "persona": "c", "loop": 1, "severity": "minor", "title": "B",
             "status": "open", "evidence": ""},
            {"id": "z", "persona": "t", "loop": 1, "severity": "major", "title": "Z",
             "status": "open", "evidence": ""}]
    verdicts = {"c": {"prior_status": [{"id": "a", "status": "fixed", "evidence": "a.py:3"},
                                       {"id": "z", "status": "fixed", "evidence": "not mine"}],
                      "findings": [{"severity": "major", "title": "B", "prior": "b"},
                                   {"severity": "minor", "title": "new", "files": ["n.py"],
                                    "basis": "initial", "demoted": True}]}}
    out = PK.prior_findings("SPEC-1", prev, verdicts, 2)
    by = {f["title"]: f for f in out}
    assert (by["A"]["status"], by["A"]["evidence"]) == ("fixed", "a.py:3")
    assert by["B"]["status"] == "open"                      # omitted: stays open
    assert by["Z"]["status"] == "open"                      # another reviewer's: untouched
    assert by["new"]["advisory"] is True and by["new"]["loop"] == 2
    # raised again after being fixed: open again
    again = PK.prior_findings("SPEC-1", out, {"c": {"findings": [
        {"severity": "major", "title": "A", "files": []}]}}, 3)
    from nightwright.agentic.followups import finding_key
    assert finding_key("SPEC-1", "A", []) != "a"            # different key: a new finding
    assert any(f["title"] == "A" and f["status"] == "open" and f["loop"] == 3 for f in again)


def test_marshal_map_cleaned_and_failure(world):
    cfg, st, _repo = world
    st["packet"] = PK.build(cfg, st)
    ev = PK.apply_marshal(cfg, st, {"ac_map": [
        {"ac": "AC1", "hunks": ["feature.txt:1", "made/up.py:9"], "tests": [],
         "coverage": "partial"},
        {"ac": "AC9", "hunks": [], "tests": [], "coverage": "covered"},
        {"ac": "AC2", "hunks": [], "tests": [], "coverage": "covered"}]})
    doc = PK.load(cfg, st)
    assert doc["ac_map"] == [
        {"ac": "AC1", "hunks": ["feature.txt:1"], "tests": [], "coverage": "partial"},
        {"ac": "AC2", "hunks": [], "tests": [], "coverage": "none"},
        {"ac": "AC3", "hunks": [], "tests": [], "coverage": "none"}]
    assert "ac_map_error" not in doc and PK.validate(doc) == []
    assert ev["ac_map"] is True and ev["none"] == 2 and st["packet"]["marshal"] == "done"
    ev = PK.apply_marshal(cfg, st, None, error="marshal timed out")
    doc = PK.load(cfg, st)
    assert doc["ac_map"] is None and doc["ac_map_error"] == "marshal timed out"
    assert ev["ac_map"] is False and st["packet"]["marshal"] == "failed"
    assert PK.validate(doc) == []


def test_builder_failure_raises(world):
    cfg, st, _repo = world
    st["repo"] = {"worktree": "/nonexistent", "base": "main"}
    with pytest.raises(ValueError):
        PK.build(cfg, st)
    meta = PK.failed(st, "ValueError: x")
    assert meta["ok"] is False and not list(S.campaign_dir(cfg, st["id"]).glob("packet-*"))


def test_validate_reports_problems():
    assert any(p.startswith("packet.schema:") for p in PK.validate({"schema": "nope"}))
    bad = {"schema": PK.SCHEMA_ID, "campaign": "c", "spec_id": "s", "loop": 1, "head": "h",
           "base": "b", "prev_head": None, "acs": [], "diff": {"stat": [], "path": "d"},
           "delta": {"stat": []}, "ac_map": None,
           "gates": {"results": [], "failing": [], "flaky": []},
           "findings": [{"id": "x", "persona": "p", "loop": 1, "severity": "major",
                         "title": "t", "status": "maybe", "evidence": ""}],
           "pointers": {"spec": None, "diff": "d", "runs": "runs/", "gate_logs": []}}
    probs = PK.validate(bad)
    assert any("status" in p for p in probs) and any("delta.path" in p for p in probs)


def test_enforce_basis_rule():
    doc = {"findings": [
        {"id": "p1", "persona": "c", "loop": 1, "severity": "major", "title": "P1",
         "status": "open", "evidence": "", "files": ["a.py"]},
        {"id": "p2", "persona": "c", "loop": 1, "severity": "minor", "title": "P2",
         "status": "open", "evidence": "", "files": []},
        {"id": "p3", "persona": "c", "loop": 1, "severity": "major", "title": "P3",
         "status": "waived", "evidence": ""},
        {"id": "p4", "persona": "c", "loop": 1, "severity": "major", "title": "P4",
         "status": "open", "evidence": "", "advisory": True}]}
    enf = {"loop": 2, "delta_files": {"a.py"}, "ac_ids": {"AC1", "AC2"}, "doc": doc}
    verdict = {"prior_status": [{"id": "p1", "status": "fixed"}], "findings": [
        {"severity": "must_fix", "title": "reg", "files": ["a.py"], "basis": "delta_regression"},
        {"severity": "must_fix", "title": "reg elsewhere", "files": ["b.py"],
         "basis": "delta_regression"},
        {"severity": "major", "title": "missed", "ac": "AC2", "basis": "missed_ac"},
        {"severity": "major", "title": "missed unknown", "ac": "AC7", "basis": "missed_ac"},
        {"severity": "major", "title": "no basis"}]}
    out, demoted = PK.enforce("SPEC-1", "c", verdict, enf)
    assert [(f["title"], f.get("prior"), bool(f.get("demoted"))) for f in out] == [
        ("P2", "p2", False), ("reg", None, False), ("reg elsewhere", None, True),
        ("missed", None, False), ("missed unknown", None, True), ("no basis", None, True)]
    assert demoted == 3


def test_extend_schema_and_flag_helpers():
    from nightwright.agentic import personas as PS
    s = PK.extend_schema(PS.TEST_REVIEWER_SCHEMA)
    assert "basis" in s["properties"]["findings"]["items"]["properties"]
    assert "prior_status" not in s["required"]
    assert "basis" not in PS.TEST_REVIEWER_SCHEMA["properties"]["findings"]["items"]["properties"]
    off = {"counters": {"loops": 2}}
    assert not PK.on(off) and not PK.marshal_due(off) and PK.run_marks(off, "x", "review") == {}
    on = {"lean": {"packet": True}, "counters": {"loops": 2}}
    assert PK.loop_of(on) == 3 and PK.marshal_due(on)
    on["packet"] = {"loop": 3, "ok": True, "marshal": "done"}
    assert not PK.marshal_due(on)
    assert PK.run_marks(on, "test_reviewer", "review") == {"loop": 3, "packet": True}
    assert PK.run_marks(on, "implementer", "implement") == {"loop": 3, "packet": False}
    PK.remember_verdicts(off)
    assert "packet_prev" not in off
