"""SPEC-0203: `triage_reviews` (pure scorer) and `review_brief` — worked examples, dedup and
canonical ids, `waived` handling, AC cover matching, brief shape."""
import copy

import pytest

from wt.agentic import queue as Q
from wt.agentic.profiles import BUILTIN_PROFILES

A, B = "conformance_reviewer", "test_reviewer"
BAL = {"review": {"pass_score": 80, "weights": {"must_fix": 100, "major": 15, "minor": 4,
                                                 "nit": 1}}}


def f(sev, title=None, **kw):
    return dict({"severity": sev, "title": title or f"{sev} thing"}, **kw)


def v(verdict="pass", *findings, brief="", ac=()):
    return {"verdict": verdict, "score": 50, "fix_brief": brief, "findings": list(findings),
            "ac_assessment": [{"ac": a, "met": m} for a, m in ac]}


def tri(verdicts, profile=BAL, waived=()):
    return Q.triage_reviews(verdicts, profile, waived)


def test_minor_fix_does_not_block():
    out = tri({A: v("fix", f("minor"), f("minor", "other"), brief="x"),
               B: v("pass", f("nit"))})
    assert (out["outcome"], out["score"], out["score_only"]) == ("pass", 91, False)
    assert out["blocking"] == [] and len(out["advisory"]) == 3 and out["fixing"] == []


def test_fix_with_major_blocks_and_score_is_irrelevant():
    out = tri({A: v("fix", f("major"), f("minor"))})
    assert (out["outcome"], out["score"]) == ("fix", 81)
    assert [b["severity"] for b in out["blocking"]] == ["major"] and out["fixing"] == [A]
    assert [a["severity"] for a in out["advisory"]] == ["minor"] and not out["score_only"]


def test_fix_without_findings_adds_the_synthetic_major():
    out = tri({A: v("fix", brief="rewrite " + "x" * 100)})
    [b] = out["blocking"]
    assert (b["id"], b["severity"], b["title"]) == (f"{A}#fix", "major", ("rewrite " + "x" * 100)[:80])
    assert out["outcome"] == "fix" and out["synthetic"] == [f"{A}#fix"]
    [b] = tri({A: v("fix")})["blocking"]
    assert b["title"] == f"{A} returned fix without findings"


def test_score_only_and_waivable_weight():
    verdicts = {A: v("pass", *[f("minor", f"a{i}") for i in range(4)]),
                B: v("pass", *[f("minor", f"b{i}") for i in range(3)])}
    out = tri(verdicts)
    assert (out["outcome"], out["score"], out["score_only"], out["waivable_weight"]) == (
        "fix", 72, True, 28)
    assert len(out["blocking"]) == 7 and out["advisory"] == []
    waive = [f"{A}#0", f"{A}#1", f"{A}#2"]
    out = tri(verdicts, waived=waive)
    assert (out["outcome"], out["score"], out["waivable_weight"]) == ("pass", 84, 16)
    assert tri({A: v("pass", f("minor"))})["waivable_weight"] == 4


def test_must_fix_always_fixes_and_pass_with_must_fix_is_a_fix():
    out = tri({A: v("pass", f("must_fix"))})
    assert out["outcome"] == "fix" and out["fixing"] == [A] and not out["score_only"]
    assert out["score"] == 0 and out["blocking"][0]["severity"] == "must_fix"
    heavy = dict(BAL, review={"pass_score": 0, "weights": dict(BAL["review"]["weights"],
                                                                must_fix=0)})
    assert tri({A: v("pass", f("must_fix"))}, heavy)["outcome"] == "fix"


def test_pass_over_a_major_passes_and_it_is_advisory():
    out = tri({A: v("pass", f("major"))})
    assert (out["outcome"], out["score"]) == ("pass", 85)
    assert out["blocking"] == [] and out["advisory"][0]["severity"] == "major"


def test_profiles_weights_and_pass_score():
    verdicts = {A: v("pass", *[f("minor", f"m{i}") for i in range(6)])}      # score 76
    outs = {n: tri(verdicts, BUILTIN_PROFILES[n])["outcome"] for n in BUILTIN_PROFILES}
    assert outs == {"balanced": "fix", "thorough": "fix", "frugal": "pass"}
    assert tri(verdicts, {})["outcome"] == "fix"                      # no `review`: balanced
    cheap = {"review": {"pass_score": 80, "weights": {"minor": 1}}}  # partial weights: defaults
    assert tri(verdicts, cheap)["score"] == 94


def test_duplicates_across_reviewers_count_once():
    one = f("minor", "Missing  test", files=["b.py", "a.py"])
    two = f("major", "missing test", files=["a.py", "b.py"])
    out = tri({A: v("fix", one, brief="a"), B: v("pass", two)})
    assert out["score"] == 85
    [m] = out["blocking"]
    assert (m["id"], m["severity"], m["personas"]) == (f"{B}#0", "major", [A, B])
    assert out["outcome"] == "fix" and out["fixing"] == [A]           # A survives via the merge
    assert out["advisory"] == []


def test_duplicate_tie_keeps_the_earliest_persona_and_same_reviewer_merges():
    x = f("minor", "same", files=["a.py"])
    out = tri({A: v("pass", x), B: v("pass", dict(x))})
    [m] = out["advisory"]
    assert (m["id"], m["personas"], out["score"]) == (f"{A}#0", [A, B], 96)
    out = tri({A: v("pass", x, dict(x))})
    assert out["score"] == 96 and len(out["advisory"]) == 1
    # no files: never merged
    out = tri({A: v("pass", f("minor", "same"), f("minor", "same"))})
    assert out["score"] == 92


def test_unmet_ac_forces_fix_with_a_synthetic_major():
    out = tri({A: v("pass", f("minor", ac="AC one"), ac=[("AC one", False)])})
    assert (out["outcome"], out["score"], out["score_only"]) == ("fix", 81, False)
    [b] = out["blocking"]
    assert (b["id"], b["severity"], b["title"]) == (f"{A}#ac0", "major", "AC one")
    assert [a["severity"] for a in out["advisory"]] == ["minor"]


def test_unmet_ac_with_a_covering_major_adds_no_synthetic():
    out = tri({A: v("pass", f("major", "real", ac="AC one"), ac=[("AC one", False)])})
    assert out["outcome"] == "fix" and out["synthetic"] == []
    assert [b["title"] for b in out["blocking"]] == ["real"]


@pytest.mark.parametrize("finding_ac,covers", [
    ("AC one", True), ("  ac   ONE ", True),
    ("the cache is invalidated", True),            # containment >= 8
    ("cache is invalidated on write", True),
    ("AC", False), ("ac 1", False),                 # contained but too short / different
    ("something else", False), ("", False), (None, False),
])
def test_ac_cover_matching(finding_ac, covers):
    entry = "the cache is invalidated on write" if "cache" in str(finding_ac) else "AC one"
    kw = {"ac": finding_ac} if finding_ac is not None else {}
    out = tri({A: v("pass", f("major", "real", **kw), ac=[(entry, False)])})
    assert out["outcome"] == "fix"
    assert (out["synthetic"] == []) is covers
    assert len(out["blocking"]) == (1 if covers else 2)


def test_fix_without_findings_and_unmet_ac_gets_both_synthetics():
    out = tri({A: v("fix", brief="b", ac=[("AC one", False)])})
    assert out["synthetic"] == [f"{A}#fix", f"{A}#ac0"]
    assert out["score"] == 70 and len(out["blocking"]) == 2


def test_synthetic_ac_keeps_a_fix_verdict_alive():
    out = tri({A: v("fix", f("minor"), f("minor", "b"), brief="fix it",
                    ac=[("AC one", False)])})
    assert out["fixing"] == [A] and out["synthetic"] == [f"{A}#ac0"]
    brief = Q.review_brief({A: v("fix", brief="fix it")}, out, 80)
    assert brief["fix_brief"] == "fix it"


def test_waived_never_removes_major_must_fix_or_synthetic():
    verdicts = {A: v("fix", f("major"), f("minor")), B: v("fix", brief="b",
                                                       ac=[("AC", False)])}
    ids = [f"{A}#0", f"{A}#1", f"{B}#fix", f"{B}#ac0"]
    out = tri(verdicts, waived=ids)
    surviving = {x["id"] for x in out["blocking"] + out["advisory"]}
    assert surviving == {f"{A}#0", f"{B}#fix", f"{B}#ac0"}           # only the minor went
    assert tri({A: v("pass", f("must_fix"))}, waived=[f"{A}#0"])["outcome"] == "fix"


def test_triage_is_pure():
    verdicts = {A: v("fix", f("major", files=["a"]), f("minor")), B: v("pass", f("minor"))}
    before = copy.deepcopy(verdicts)
    assert tri(verdicts) == tri(verdicts)
    assert verdicts == before


def test_unknown_severity_counts_as_minor():
    assert tri({A: v("pass", f("weird"))})["score"] == 96


# ---- review_brief --------------------------------------------------------------------------------

def test_multi_brief_shape():
    verdicts = {A: v("fix", f("major", "boom", detail="why", files=["a.py", "b.py"]),
                     brief="do X"),
                B: v("pass", f("minor"))}
    out = tri(verdicts)
    brief = Q.review_brief(verdicts, out, 80)
    assert brief["personas"] == [A]
    assert brief["fix_brief"] == f"[{A}] do X\n\n- [major] boom (a.py, b.py): why"
    assert brief["must_fix"] == [{"ac": None, "title": "boom", "detail": "why",
                                  "files": ["a.py", "b.py"], "id": f"{A}#0",
                                  "severity": "major", "personas": [A]}]


def test_score_only_brief_has_the_score_line():
    verdicts = {A: v("pass", *[f("minor", f"a{i}") for i in range(4)]),
                B: v("pass", *[f("minor", f"b{i}") for i in range(3)])}
    brief = Q.review_brief(verdicts, tri(verdicts), 80)
    lines = brief["fix_brief"].splitlines()
    assert lines[0] == ("Review score 72 is below the pass score 80; address the "
                        "highest-weight findings first.")
    assert len(lines) == 8 and lines[1] == "- [minor] a0"
    assert len(brief["must_fix"]) == 7 and brief["personas"] == [A, B]


def test_single_reviewer_brief_keeps_the_0187_shape():
    verdicts = {A: v("fix", f("must_fix", ac="AC", detail="d", files=["x"]), brief="verbatim")}
    brief = Q.review_brief(verdicts, tri(verdicts), 80)
    assert brief == {"fix_brief": "verbatim", "must_fix": [
        {"ac": "AC", "title": "must_fix thing", "detail": "d", "files": ["x"]}]}
    # empty reviewer brief: a synthetic or score-only outcome gets bullets, others stay empty
    solo = {A: v("fix")}
    assert Q.review_brief(solo, tri(solo), 80)["fix_brief"].startswith("- [major] ")
    low = {A: v("pass", *[f("minor", f"m{i}") for i in range(6)])}
    assert Q.review_brief(low, tri(low), 80)["fix_brief"].startswith("Review score 76")
    empty = {A: v("fix", f("must_fix"))}
    assert Q.review_brief(empty, tri(empty), 80)["fix_brief"] == ""


# ---- SPEC-0206: needs_triager, waiver validation, triage_input ---------------------------------

import json

from wt.agentic import profiles as P

ON = {"review": {"pass_score": 80, "weights": BAL["review"]["weights"], "triager": True}}


def need(verdicts, profile=ON):
    return Q.needs_triager(tri(verdicts, profile), profile)


def minors(n, persona=A):
    return v("pass", *[f("minor", f"{persona} m{i}") for i in range(n)])


@pytest.mark.parametrize("label,verdicts,profile,want", [
    ("rescuable score-only fix", {A: minors(6)}, ON, True),
    ("a nit-only tail", {A: v("pass", *[f("nit", f"n{i}") for i in range(30)])}, ON, True),
    ("boundary: score + waivable == pass_score",
     {A: v("pass", f("major"), *[f("minor", f"m{i}") for i in range(6)])},
     {"review": {"pass_score": 85, "weights": BAL["review"]["weights"], "triager": True}}, True),
    ("just below the boundary",
     {A: v("pass", f("major"), *[f("minor", f"m{i}") for i in range(6)])},
     {"review": {"pass_score": 86, "weights": BAL["review"]["weights"], "triager": True}}, False),
    ("triager off", {A: minors(6)}, {"review": dict(ON["review"], triager=False)}, False),
    ("hand-built profile without the key", {A: minors(6)}, BAL, False),
    ("a pass", {A: minors(2)}, ON, False),
    ("a must_fix", {A: v("pass", f("must_fix"), *[f("minor", f"m{i}") for i in range(6)])},
     ON, False),
    ("a surviving fix verdict", {A: v("fix", f("major"), *[f("minor", f"m{i}") for i in range(6)])},
     ON, False),
    ("a synthetic major (fix without findings)", {A: v("fix"), B: minors(6, B)}, ON, False),
    ("an unmet AC", {A: v("pass", *[f("minor", f"m{i}") for i in range(6)], ac=[("AC one", False)])},
     ON, False),
    ("a hopeless score", {A: v("pass", *[f("major", f"j{i}") for i in range(5)],
                              *[f("minor", f"m{i}") for i in range(3)])}, ON, False),
])
def test_needs_triager_truth_table(label, verdicts, profile, want):
    assert need(verdicts, profile) is want, label


def _state(verdicts, profile, **lr):
    return {"id": "SPEC-0001-20261025-abcd", "counters": {"loops": 0, "runs": 3},
            "last_review": {"verdicts": {p: dict(x, loop=0, run=f"00{i}-{p}")
                                         for i, (p, x) in enumerate(verdicts.items(), 2)}, **lr}}


PROF2 = dict(ON, reviewers=[A, B])


def test_accept_waivers_validates_ids_reasons_and_severities():
    vs = {A: v("pass", f("minor", "taste"), f("nit", "typo"), f("major", "real"),
               f("must_fix", "broken")), B: v("fix", brief="x")}
    state = _state(vs, PROF2)
    got, ignored = Q._accept_waivers(state, PROF2, [
        {"id": f"{A}#0", "reason": " subjective "},       # minor: accepted, reason stripped
        {"id": f"{A}#1", "reason": "tiny"},               # nit: accepted
        {"id": f"{A}#1", "reason": "again"},              # duplicate
        {"id": f"{A}#2", "reason": "meh"},                # major
        {"id": f"{A}#3", "reason": "meh"},                # must_fix
        {"id": f"{B}#fix", "reason": "meh"},              # synthetic
        {"id": "nobody#9", "reason": "meh"},              # unknown
        {"id": f"{A}#0"},                                 # no reason
        {"reason": "no id"}, "junk", {"id": 3, "reason": "x"}])
    assert [(w["id"], w["severity"], w["title"], w["reason"]) for w in got] == [
        (f"{A}#0", "minor", "taste", "subjective"), (f"{A}#1", "nit", "typo", "tiny")]
    assert set(got[0]) == {"id", "personas", "severity", "title", "detail", "files", "ac",
                           "category", "reason"}
    assert ignored == [
        (f"{A}#1", "duplicate"), (f"{A}#2", "major findings cannot be waived"),
        (f"{A}#3", "must_fix findings cannot be waived"),
        (f"{B}#fix", "synthetic findings cannot be waived"),
        ("nobody#9", "not a finding of this review"), (f"{A}#0", "empty reason"),
        (None, "not a finding of this review"), (None, "not a finding of this review"),
        (3, "not a finding of this review")]


def test_accept_waivers_rejects_empty_reason_on_a_fresh_id():
    state = _state({A: minors(2)}, ON)
    got, ignored = Q._accept_waivers(state, ON, [{"id": f"{A}#0", "reason": "  "},
                                                 {"id": f"{A}#1", "reason": None}])
    assert got == [] and ignored == [(f"{A}#0", "empty reason"), (f"{A}#1", "empty reason")]


def test_waivers_rerun_triage_reviews():
    vs = {A: minors(8)}
    assert tri(vs, ON)["score"] == 68
    got, _ = Q._accept_waivers(_state(vs, ON), ON, [{"id": f"{A}#{i}", "reason": "r"}
                                                    for i in range(3)])
    out = tri(vs, ON, [w["id"] for w in got])
    assert (out["outcome"], out["score"]) == ("pass", 80)
    out = tri(vs, ON, [f"{A}#0", f"{A}#1"])                       # too few
    assert (out["outcome"], out["score"], out["score_only"]) == ("fix", 76, True)
    assert len(out["blocking"]) == 6


def test_triage_input_renders_score_verdicts_and_waivable_findings():
    vs = {A: v("pass", f("minor", "taste", detail="d1", files=["a.py"]), f("major", "real"),
               *[f("nit", f"n{i}") for i in range(2)], brief="ok"),
          B: v("pass", *[f("minor", f"m{i}") for i in range(4)])}
    state = _state(vs, PROF2)
    text = Q._triage_input(state, PROF2)
    data = json.loads(text)
    assert text == json.dumps(data, indent=2, sort_keys=True) == Q._triage_input(state, PROF2)
    assert (data["score"], data["pass_score"]) == (100 - 15 - 4 * 5 - 2, 80)
    assert data["reviewer_verdicts"] == {A: {"verdict": "pass", "fix_brief": "ok"},
                                         B: {"verdict": "pass", "fix_brief": ""}}
    ids = [w["id"] for w in data["waivable"]]
    assert f"{A}#1" not in ids and len(ids) == 7                 # the major is not waivable
    first = next(w for w in data["waivable"] if w["id"] == f"{A}#0")
    assert first == {"id": f"{A}#0", "personas": [A], "severity": "minor", "title": "taste",
                     "detail": "d1", "files": ["a.py"]}
    assert all(w["severity"] in ("minor", "nit") for w in data["waivable"])
    assert Q._triage_input(_state(vs, PROF2, waived=[{"id": f"{A}#0"}]), PROF2) == text


# ---- SPEC-0207: follow-up ideas for non-blocking findings ---------------------------------------

import subprocess
from zoneinfo import ZoneInfo

from wt import org as ORG
from wt import org_write as OW
from wt.agentic import followups as FU
from wt.agentic import store as S

SPEC = "SPEC-0207"
CID = "SPEC-0207-20261025-aaaa"


def fu_cfg(tmp_path):
    org = tmp_path / "org"
    org.mkdir(exist_ok=True)
    return {
        "org_files": [str(org)], "org_todo_keywords": ["TODO", "|", "DONE"],
        "org_idea_keywords": ["IDEA", "INCUBATE", "SPECCED", "|", "PROMOTED", "DROPPED"],
        "timezone": "America/New_York", "_tz": ZoneInfo("America/New_York"),
        "data_dir": str(tmp_path / "data"), "config_dir": str(tmp_path / "cfg"),
        "org_capture_file": str(org / "inbox.org"), "org_ideas_file": str(org / "ideas.org"),
        "specs_dir": str(tmp_path / "specs"), "agentic": {},
    }


def adv(sev, title=None, personas=(A,), **kw):
    return dict({"id": f"{personas[0]}#0", "personas": list(personas), "severity": sev,
                 "title": title or f"{sev} thing", "detail": "some detail", "files": ["a.py"],
                 "ac": None, "category": None}, **kw)


def fu_state(tmp_path, advisory=(), waived=(), carry=None, parent="SPEC-0169"):
    root = tmp_path / "root"
    (root / "docs").mkdir(parents=True, exist_ok=True)
    fm = f"parent: {parent}\n" if parent else ""
    (root / "docs" / "s.md").write_text(f"---\nid: {SPEC}\ntitle: t\n{fm}---\n\nbody\n")
    st = {"id": CID, "spec_id": SPEC, "project": "Meta-Tools",
          "spec": {"rel_path": "docs/s.md"}, "repo": {"root": str(root)},
          "last_review": {"advisory": list(advisory), "waived": list(waived)}}
    if carry is not None:
        st["review_carry"] = carry
    return st


def file_now(cfg, st, profile=None, *, record=True):
    """Run the filer; like `_apply`, append its events to the ledger. Returns the summary."""
    events, summary = FU.file_followups(cfg, st, profile or BAL)
    if record:
        for etype, payload in events:
            S.append_event(cfg, st["id"], etype, payload)
    return summary


def ideas(cfg):
    ORG.invalidate_tasks_cache()
    return [t for t in ORG.load_tasks(cfg) if t.is_idea]


def test_key_ignores_severity_and_persona_but_not_title_or_files():
    k = FU.finding_key(SPEC, "Naming  is Off", ["b.py", "a.py"])
    assert k == FU.finding_key(SPEC, "naming is off", ["a.py", "b.py"])
    assert len(k) == 40 and k != FU.finding_key(SPEC, "naming is off", ["a.py"])
    assert k != FU.finding_key(SPEC, "naming is of", ["a.py", "b.py"])
    assert k != FU.finding_key("SPEC-0001", "naming is off", ["a.py", "b.py"])
    a = FU.make_entry(SPEC, adv("minor", "x", personas=(A,)), "advisory")
    b = FU.make_entry(SPEC, adv("major", "x", personas=(B,)), "advisory")
    assert a["key"] == b["key"]


def test_idea_text_is_sanitized_and_bounded(tmp_path):
    cfg = fu_cfg(tmp_path)
    nasty = adv("minor", ("T:itle [x] — *bold*\n" + "y" * 300)[:300],
                detail="line1\nline2\t[z] :tag: " + "d" * 400,
                files=[f"src/dir{i}/file:{i}.py" for i in range(20)], ac="AC [1]: ok")
    st = fu_state(tmp_path, [nasty])
    summary = file_now(cfg, st)
    assert len(summary["filed"]) == 1 and summary["unfiled"] == []
    [t] = ideas(cfg)
    key12 = FU.finding_key(SPEC, nasty["title"], nasty["files"])[:12]
    assert t.heading.startswith(f"wtfu:{key12} [{SPEC}] {A}/AC 1 ok: ")
    assert len(t.heading) < 560 and "\n" not in t.heading
    assert t.tags == frozenset() and "+15 more" in t.heading
    for bad in ("[x]", "[z]", "*bold*", ":tag:"):
        assert bad not in t.heading.split(": ", 1)[1]
    assert t.heading.count("—") == 1                         # only the template's separator
    title_part = t.heading.split(": ", 1)[1].split(" — ")[0]
    assert len(title_part) <= 120


def test_filer_files_major_minor_with_project_epic_kind(tmp_path):
    cfg = fu_cfg(tmp_path)
    st = fu_state(tmp_path, [adv("major", "big"), adv("minor", "small", files=["b.py"]),
                             adv("nit", "tiny", files=["c.py"])],
                  waived=[adv("minor", "waived one", files=["d.py"], reason="not worth it"),
                          adv("nit", "waived nit", files=["e.py"], reason="style")])
    summary = file_now(cfg, st)
    assert [(f["severity"], f["title"], f["origin"]) for f in summary["filed"]] == [
        ("major", "big", "advisory"), ("minor", "small", "advisory"),
        ("minor", "waived one", "waived")]
    assert [(w["title"], w["waive_reason"]) for w in summary["waived"]] == [
        ("waived one", "not worth it"), ("waived nit", "style")]
    got = ideas(cfg)
    assert len(got) == 3
    for t in got:
        assert t.project == "Meta-Tools" and t.epic == "SPEC-0169"
        assert t.properties["KIND"] == "improvement" and t.properties["ID"].startswith("IDEA-")
    evs = S.read_ledger(cfg, CID, types=["followup"])
    assert [(e["severity"], e["origin"], e["persona"]) for e in evs] == [
        ("major", "advisory", A), ("minor", "advisory", A), ("minor", "waived", A)]
    assert [e["idea_id"] for e in evs] == [f["idea_id"] for f in summary["filed"]]


def test_nits_only_with_the_flag_and_nothing_when_disabled(tmp_path):
    cfg = fu_cfg(tmp_path)
    st = fu_state(tmp_path, [adv("nit", "tiny"), adv("minor", "small", files=["b.py"])])
    nits = {"review": {"followups": True, "followups_nits": True}}
    off = {"review": {"followups": False, "followups_nits": True}}
    assert file_now(cfg, st, off)["filed"] == [] and ideas(cfg) == []
    assert S.read_ledger(cfg, CID, types=["followup"]) == []
    assert [f["severity"] for f in file_now(cfg, st, nits)["filed"]] == ["nit", "minor"]
    assert len(ideas(cfg)) == 2
    assert FU.file_followups(cfg, dict(st, id="other"), {"review": {"followups": False}}) == (
        [], {"filed": [], "unfiled": [], "waived": []})


def test_no_parent_means_no_epic(tmp_path):
    cfg = fu_cfg(tmp_path)
    file_now(cfg, fu_state(tmp_path, [adv("minor")], parent=None))
    [t] = ideas(cfg)
    assert t.epic is None


def test_rerun_files_once_and_reports_skipped_in_filed(tmp_path):
    cfg = fu_cfg(tmp_path)
    st = fu_state(tmp_path, [adv("minor", "one"), adv("major", "two", files=["b.py"])])
    first = file_now(cfg, st)
    second = file_now(cfg, st)
    assert len(ideas(cfg)) == 2 and len(S.read_ledger(cfg, CID, types=["followup"])) == 2
    assert [(f["idea_id"], f["title"]) for f in second["filed"]] == [
        (f["idea_id"], f["title"]) for f in first["filed"]]
    assert all(f["idea_id"] for f in second["filed"])


def test_crash_between_add_idea_and_event_adopts(tmp_path):
    cfg = fu_cfg(tmp_path)
    st = fu_state(tmp_path, [adv("minor", "one")])
    first = file_now(cfg, st, record=False)               # the events never reached the ledger
    assert S.read_ledger(cfg, CID, types=["followup"]) == []
    second = file_now(cfg, st)
    assert len(ideas(cfg)) == 1
    assert second["filed"][0]["idea_id"] == first["filed"][0]["idea_id"]
    [ev] = S.read_ledger(cfg, CID, types=["followup"])
    assert ev["idea_id"] == first["filed"][0]["idea_id"]


def test_crash_adoption_finds_an_archived_idea(tmp_path):
    cfg = fu_cfg(tmp_path)
    st = fu_state(tmp_path, [adv("minor", "one")])
    first = file_now(cfg, st, record=False)
    [t] = ideas(cfg)
    OW.archive_idea(cfg, t)
    assert ideas(cfg) == [] or all(x.file != cfg["org_ideas_file"] for x in ideas(cfg))
    second = file_now(cfg, st)
    assert second["filed"][0]["idea_id"] == first["filed"][0]["idea_id"]
    assert not any("wtfu:" in x.heading and x.file == cfg["org_ideas_file"] for x in ideas(cfg))


def test_capture_error_is_unfiled_then_a_rerun_files_without_duplicates(tmp_path, monkeypatch):
    cfg = fu_cfg(tmp_path)
    st = fu_state(tmp_path, [adv("minor", "one"), adv("minor", "two", files=["b.py"])])
    real = OW.add_idea
    calls = []

    def flaky(cfg_, text, **kw):
        calls.append(text)
        if len(calls) == 1:
            raise OSError("disk full")
        return real(cfg_, text, **kw)

    monkeypatch.setattr(OW, "add_idea", flaky)
    first = file_now(cfg, st)
    assert [(u["title"], u["error"]) for u in first["unfiled"]] == [
        ("one", "OSError('disk full')")]
    assert [f["title"] for f in first["filed"]] == ["two"]
    assert [e["title"] for e in S.read_ledger(cfg, CID, types=["followup"])] == ["two"]
    assert any("follow-up not filed" in n["text"] for n in S.read_ledger(cfg, CID, types=["note"]))
    second = file_now(cfg, st)
    assert second["unfiled"] == [] and [f["title"] for f in second["filed"]] == ["one", "two"]
    assert len(ideas(cfg)) == 2


def test_non_oserror_from_the_org_layer_never_escapes(tmp_path, monkeypatch):
    cfg = fu_cfg(tmp_path)
    st = fu_state(tmp_path, [adv("minor", "one")])
    monkeypatch.setattr(FU, "find_marker", lambda *a: (_ for _ in ()).throw(KeyError("boom")))
    summary = file_now(cfg, st)
    assert summary["filed"] == [] and summary["unfiled"][0]["error"] == "KeyError('boom')"
    monkeypatch.undo()
    monkeypatch.setattr(S, "read_ledger", lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("no ledger")))
    events, summary = FU.file_followups(cfg, st, BAL)       # outer guard
    assert summary["filed"] == [] and events and events[0][0] == "note"
    assert "RuntimeError('no ledger')" in events[0][1]["text"]


def test_headline_without_the_marker_adds_a_note_but_still_records(tmp_path, monkeypatch):
    cfg = fu_cfg(tmp_path)
    real = OW.add_idea

    def lossy(cfg_, text, **kw):
        path, headline, idea_id = real(cfg_, text, **kw)
        return path, "* IDEA mangled", idea_id

    monkeypatch.setattr(OW, "add_idea", lossy)
    events, summary = FU.file_followups(cfg, fu_state(tmp_path, [adv("minor")]), BAL)
    assert [e[0] for e in events] == ["note", "followup"]
    assert "lost its wtfu:" in events[0][1]["text"]
    assert len(summary["filed"]) == 1 and summary["unfiled"] == []


def test_marker_round_trips_through_the_real_add_task_path(tmp_path):
    cfg = fu_cfg(tmp_path)
    entry = FU.make_entry(SPEC, adv("minor", "round: trip [it]", detail="x — y"), "advisory")
    marker = f"wtfu:{entry['key'][:12]}"
    assert FU.find_marker(cfg, marker) is None             # no ideas file yet
    _, headline, idea_id = OW.add_idea(cfg, FU.idea_text(SPEC, entry), kind="improvement")
    assert marker in headline and FU.find_marker(cfg, marker) == idea_id
    assert FU.find_marker(cfg, "wtfu:000000000000") is None


# carry-over ------------------------------------------------------------------------------------

def carry_state(**kw):
    return dict({"spec_id": SPEC}, **kw)


def test_merge_carry_dedups_by_key_with_highest_severity_and_waived_wins():
    st = carry_state()
    Q._merge_carry(st, {"blocking": [], "advisory": [adv("minor", "x"), adv("nit", "y", files=["y"])],
                        "waived": []})
    Q._merge_carry(st, {"blocking": [], "advisory": [adv("major", "x", personas=(B,))],
                        "waived": [adv("nit", "y", files=["y"], reason="meh")]})
    carry = st["review_carry"]
    assert [(e["title"], e["severity"], e["personas"], e["origin"]) for e in carry] == [
        ("x", "major", [A, B], "advisory"), ("y", "nit", [A], "waived")]
    assert carry[1]["waive_reason"] == "meh" and "waive_reason" not in carry[0]
    again = [dict(e) for e in carry]
    Q._merge_carry(st, {"blocking": [], "advisory": [adv("major", "x", personas=(B,))],
                        "waived": []})                      # the same review again adds nothing
    assert st["review_carry"] == again


def test_merge_carry_removes_a_blocking_key_whatever_severity_or_reporter():
    st = carry_state()
    Q._merge_carry(st, {"blocking": [], "advisory": [adv("minor", "x"), adv("minor", "z", files=["z"])],
                        "waived": []})
    blocking = [adv("must_fix", "x", personas=(B,))]       # same key, other severity and reporter
    Q._merge_carry(st, {"blocking": blocking, "advisory": [adv("minor", "x")], "waived": []})
    assert [e["title"] for e in st["review_carry"]] == ["z"]   # x removed, not re-added; z kept


def test_considered_set_unions_carry_and_the_final_review():
    carry = [FU.make_entry(SPEC, adv("minor", "old"), "advisory")]
    out = FU.considered(SPEC, carry, {"advisory": [adv("major", "old"), adv("minor", "new", files=["n"])],
                                      "waived": []})
    assert [(e["title"], e["severity"]) for e in out] == [("old", "major"), ("new", "minor")]
    assert carry[0]["severity"] == "minor"                   # inputs are not mutated
