"""SPEC-0203: `triage_reviews` (pure scorer) and `review_brief` — worked examples, dedup and
canonical ids, `waived` handling, AC cover matching, brief shape."""
import os
import copy

import pytest

from nightwright.agentic import queue as Q
from nightwright.agentic.profiles import builtin_profiles

pytestmark = pytest.mark.fast

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
    outs = {n: tri(verdicts, builtin_profiles()[n])["outcome"] for n in builtin_profiles()}
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

from nightwright.agentic import profiles as P

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


def test_triage_input_leaves_out_human_waived_findings_and_counts_the_score_without_them():
    vs = {A: v("pass", f("minor", "taste", files=["a.py"]), f("nit", "typo", files=["b.py"]))}
    state = _state(vs, PROF2)
    before = json.loads(Q._triage_input(state, PROF2))
    assert [w["id"] for w in before["waivable"]] == [f"{A}#0", f"{A}#1"]
    state["human_overrides"] = {"waive": [{"id": f"{A}#0", "title": "taste", "files": ["a.py"],
                                           "severity": "minor", "synthetic": False}],
                                "attest": []}
    after = json.loads(Q._triage_input(state, PROF2))
    assert [w["title"] for w in after["waivable"]] == ["typo"]
    assert after["score"] > before["score"]


# ---- SPEC-0207: follow-up ideas for non-blocking findings ---------------------------------------

import subprocess
import threading
from zoneinfo import ZoneInfo

from nightwright import org as ORG
from nightwright import explore as EX
from nightwright import org_write as OW
from nightwright.agentic import followups as FU
from nightwright.agentic import store as S

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


def group_of(cfg):
    [t] = ideas(cfg)                                         # exactly one idea
    return t


def body(cfg, t):
    return EX.read_idea_enrichment(cfg, t)["summary"]


def k12(title, files=("a.py",)):
    return FU.finding_key(SPEC, title, list(files))[:12]


def capped_profile(n):
    return {"review": dict(BAL["review"], followups_max=n)}


def seed_group(cfg, token, keys, spec=SPEC, close=False):
    """An existing grouped idea (another campaign's, or `legacy-<SPEC>`) listing `keys`."""
    _, _, idea_id = OW.add_idea(cfg, FU.group_title(token, spec, len(keys)),
                                kind="improvement")
    EX.set_summary(cfg, idea_id, "\n".join(f"- [ ] nwfu:{k} minor x: y" for k in keys))
    if close:
        EX.close_idea(cfg, idea_id, "drop", reason="done")
    return idea_id


def test_key_ignores_severity_and_persona_but_not_title_or_files():
    k = FU.finding_key(SPEC, "Naming  is Off", ["b.py", "a.py"])
    assert k == FU.finding_key(SPEC, "naming is off", ["a.py", "b.py"])
    assert len(k) == 40 and k != FU.finding_key(SPEC, "naming is off", ["a.py"])
    assert k != FU.finding_key(SPEC, "naming is of", ["a.py", "b.py"])
    assert k != FU.finding_key("SPEC-0001", "naming is off", ["a.py", "b.py"])
    a = FU.make_entry(SPEC, adv("minor", "x", personas=(A,)), "advisory")
    b = FU.make_entry(SPEC, adv("major", "x", personas=(B,)), "advisory")
    assert a["key"] == b["key"]


def test_group_title_and_checklist_line_shapes_and_bounds():                        # AC2
    assert FU.group_title("c1", SPEC, 3) == f"nwfug:c1 [{SPEC}] review follow-ups (3 findings)"
    assert FU.group_title("c1", SPEC, 1).endswith("(1 finding)")
    assert FU.group_title("c1", SPEC, 0).endswith("(0 findings)")
    assert FU.MARKER not in FU.GROUP_MARKER                  # find_marker("nwfu:…") never hits one
    nasty = adv("minor", ("T:itle [x] — *bold*\n" + "y" * 300)[:300],
                detail="line1\nline2\t[z] :tag: " + "d" * 400,
                files=[f"src/dir{i}/file:{i}.py" for i in range(20)], ac="AC [1]: ok")
    entry = FU.make_entry(SPEC, nasty, "advisory")
    line = FU.checklist_line(SPEC, entry)
    assert line.startswith(f"- [ ] nwfu:{entry['key'][:12]} minor {A}/AC 1 ok: ")
    assert f"[{SPEC}]" not in line and len(line) < 560 and "\n" not in line
    assert "+15 more" in line and line.count("—") == 1
    for bad in ("[x]", "[z]", "*bold*", ":tag:"):
        assert bad not in line.split(": ", 1)[1]
    assert len(line.split(": ", 1)[1].split(" — ")[0]) <= 120
    assert FU.summary_keys(line) == [entry["key"][:12]]
    assert FU.summary_keys(f"- [X] nwfu:{entry['key'][:12]} ticked\n- +3 more findings\nfree") == [
        entry["key"][:12]]                                   # a ticked line still counts


def test_filer_files_one_group_with_project_epic_kind_and_ordered_lines(tmp_path):  # AC2
    cfg = fu_cfg(tmp_path)
    st = fu_state(tmp_path, [adv("minor", "small", files=["b.py"]), adv("major", "big"),
                             adv("nit", "tiny", files=["c.py"]),
                             adv("minor", "alpha", files=["e.py"])],
                  waived=[adv("minor", "aaa waived", files=["d.py"], reason="not worth it"),
                          adv("nit", "waived nit", files=["e.py"], reason="style")])
    summary = file_now(cfg, st)
    order = [("major", "big", "advisory"), ("minor", "alpha", "advisory"),
             ("minor", "small", "advisory"), ("minor", "aaa waived", "waived")]
    assert [(f["severity"], f["title"], f["origin"]) for f in summary["filed"]] == order
    assert [(w["title"], w["waive_reason"]) for w in summary["waived"]] == [
        ("aaa waived", "not worth it"), ("waived nit", "style")]
    t = group_of(cfg)
    assert t.heading == f"nwfug:{CID} [{SPEC}] review follow-ups (4 findings)"
    assert t.project == "Meta-Tools" and t.epic == "SPEC-0169"
    assert t.properties["KIND"] == "improvement" and t.properties["ID"].startswith("IDEA-")
    lines = body(cfg, t).splitlines()
    assert [ln.split()[4] for ln in lines] == ["major", "minor", "minor", "minor"]
    assert [FU.summary_keys(ln)[0] for ln in lines] == [
        k12("big"), k12("alpha", ["e.py"]), k12("small", ["b.py"]),
        k12("aaa waived", ["d.py"])]
    evs = S.read_ledger(cfg, CID, types=["followup"])
    assert [(e["severity"], e["origin"], e["persona"]) for e in evs] == [
        ("major", "advisory", A), ("minor", "advisory", A), ("minor", "advisory", A),
        ("minor", "waived", A)]
    assert {e["idea_id"] for e in evs} == {t.properties["ID"]} == {
        f["idea_id"] for f in summary["filed"]}
    assert summary["idea_id"] == t.properties["ID"] and summary["capped"] == []


def test_nits_only_with_the_flag_and_nothing_when_disabled(tmp_path):
    cfg = fu_cfg(tmp_path)
    st = fu_state(tmp_path, [adv("nit", "tiny"), adv("minor", "small", files=["b.py"])])
    nits = {"review": {"followups": True, "followups_nits": True}}
    off = {"review": {"followups": False, "followups_nits": True}}
    assert file_now(cfg, st, off)["filed"] == [] and ideas(cfg) == []
    assert S.read_ledger(cfg, CID, types=["followup"]) == []
    assert [f["severity"] for f in file_now(cfg, st, nits)["filed"]] == ["minor", "nit"]
    assert "(2 findings)" in group_of(cfg).heading
    assert FU.file_followups(cfg, dict(st, id="other"), {"review": {"followups": False}}) == (
        [], {"filed": [], "unfiled": [], "waived": [], "capped": [], "idea_id": None})


def test_no_parent_means_no_epic(tmp_path):
    cfg = fu_cfg(tmp_path)
    file_now(cfg, fu_state(tmp_path, [adv("minor")], parent=None))
    assert group_of(cfg).epic is None


def test_cap_lists_the_most_severe_and_writes_an_overflow_line(tmp_path):            # AC3
    cfg = fu_cfg(tmp_path)
    advs = [adv("minor", f"m{i}", files=[f"{i}.py"]) for i in range(4)] + [adv("major", "big")]
    st = fu_state(tmp_path, advs)
    summary = file_now(cfg, st, capped_profile(2))
    t = group_of(cfg)
    lines = body(cfg, t).splitlines()
    assert len(lines) == 3 and lines[2] == f"- +3 more findings not listed (nw campaign show {CID})"
    assert [f["title"] for f in summary["filed"]] == ["big", "m0"]
    assert [(c["severity"], c["title"]) for c in summary["capped"]] == [
        ("minor", "m1"), ("minor", "m2"), ("minor", "m3")]
    assert "(2 findings)" in t.heading                       # the overflow line is not counted
    assert [e["title"] for e in S.read_ledger(cfg, CID, types=["followup"])] == ["big", "m0"]
    # a raised cap on a re-run lists the rest and drops the overflow line
    again = file_now(cfg, st, capped_profile(0))
    assert again["capped"] == [] and len(again["filed"]) == 5
    assert len(ideas(cfg)) == 1 and "more findings" not in body(cfg, group_of(cfg))
    assert "(5 findings)" in group_of(cfg).heading
    assert len(S.read_ledger(cfg, CID, types=["followup"])) == 5


def test_cap_zero_means_no_cap(tmp_path):
    cfg = fu_cfg(tmp_path)
    advs = [adv("minor", f"m{i}", files=[f"{i}.py"]) for i in range(5)]
    summary = file_now(cfg, fu_state(tmp_path, advs), capped_profile(0))
    assert len(summary["filed"]) == 5 and summary["capped"] == []
    assert len(body(cfg, group_of(cfg)).splitlines()) == 5


def test_open_group_keys_reads_open_groups_of_the_spec_only(tmp_path):                # AC4
    cfg = fu_cfg(tmp_path)
    other = seed_group(cfg, "SPEC-0207-20261025-bbbb", ["aaaaaaaaaaaa", "bbbbbbbbbbbb"])
    legacy = seed_group(cfg, f"legacy-{SPEC}", ["cccccccccccc"])
    seed_group(cfg, "SPEC-0207-20261025-cccc", ["dddddddddddd"], close=True)
    seed_group(cfg, "SPEC-0001-20261025-dddd", ["eeeeeeeeeeee"], spec="SPEC-0001")
    OW.add_idea(cfg, f"nwfu:ffffffffffff [{SPEC}] a single, not a group", kind="improvement")
    got = FU.open_group_keys(cfg, SPEC)
    assert got == {"aaaaaaaaaaaa": other, "bbbbbbbbbbbb": other, "cccccccccccc": legacy}
    assert FU.open_group_keys(cfg, SPEC, exclude=other) == {"cccccccccccc": legacy}
    assert FU.open_group_keys(cfg, "SPEC-0999") == {}


def test_finding_listed_in_another_open_group_gets_no_line(tmp_path):                # AC4
    cfg = fu_cfg(tmp_path)
    other = seed_group(cfg, f"legacy-{SPEC}", [k12("one")])
    st = fu_state(tmp_path, [adv("minor", "one"), adv("minor", "two", files=["b.py"])])
    summary = file_now(cfg, st)
    assert {f["title"]: f["idea_id"] for f in summary["filed"]}["one"] == other
    mine = summary["idea_id"]
    assert mine and mine != other and "(1 finding)" in [t for t in ideas(cfg)
                                                        if t.properties["ID"] == mine][0].heading
    assert FU.summary_keys(body(cfg, [t for t in ideas(cfg) if t.properties["ID"] == mine][0])) == [
        k12("two", ["b.py"])]
    assert {e["title"]: e["idea_id"] for e in S.read_ledger(cfg, CID, types=["followup"])} == {
        "one": other, "two": mine}


def test_everything_listed_elsewhere_creates_no_idea_and_no_idea_id(tmp_path):        # AC4
    cfg = fu_cfg(tmp_path)
    other = seed_group(cfg, "SPEC-0207-20261025-bbbb", [k12("one")])
    summary = file_now(cfg, fu_state(tmp_path, [adv("minor", "one")]))
    assert summary["idea_id"] is None and [f["idea_id"] for f in summary["filed"]] == [other]
    assert len(ideas(cfg)) == 1


def test_a_closed_group_does_not_suppress_filing(tmp_path):                           # AC4
    cfg = fu_cfg(tmp_path)
    closed = seed_group(cfg, "SPEC-0207-20261025-bbbb", [k12("one")], close=True)
    summary = file_now(cfg, fu_state(tmp_path, [adv("minor", "one")]))
    assert summary["idea_id"] and summary["idea_id"] != closed
    assert [f["idea_id"] for f in summary["filed"]] == [summary["idea_id"]]


def test_rerun_files_once_and_reports_skipped_in_filed(tmp_path):                     # AC5
    cfg = fu_cfg(tmp_path)
    st = fu_state(tmp_path, [adv("minor", "one"), adv("major", "two", files=["b.py"])])
    first = file_now(cfg, st)
    before = open(cfg["org_ideas_file"]).read()
    second = file_now(cfg, st)
    assert len(ideas(cfg)) == 1 and len(S.read_ledger(cfg, CID, types=["followup"])) == 2
    assert [(f["idea_id"], f["title"]) for f in second["filed"]] == [
        (f["idea_id"], f["title"]) for f in first["filed"]]
    assert second["idea_id"] == first["idea_id"] and len(body(cfg, group_of(cfg)).splitlines()) == 2
    assert open(cfg["org_ideas_file"]).read() == before      # a no-op re-run writes nothing


def test_crash_between_add_idea_and_set_summary_adopts_the_group(tmp_path, monkeypatch):  # AC5
    cfg = fu_cfg(tmp_path)
    st = fu_state(tmp_path, [adv("minor", "one"), adv("minor", "two", files=["b.py"])])
    real = EX.set_summary
    calls = []

    def flaky(cfg_, sel, text):
        calls.append(sel)
        if len(calls) == 1:
            raise OSError("disk full")
        return real(cfg_, sel, text)

    monkeypatch.setattr(EX, "set_summary", flaky)
    first = file_now(cfg, st)
    assert first["filed"] == [] and [u["error"] for u in first["unfiled"]] == [
        "OSError('disk full')"] * 2                           # reported, merge still goes on
    assert "(0 findings)" in group_of(cfg).heading
    assert S.read_ledger(cfg, CID, types=["followup"]) == []
    assert any("follow-ups not filed" in n["text"] for n in S.read_ledger(cfg, CID, types=["note"]))
    second = file_now(cfg, st)
    assert second["unfiled"] == [] and len(second["filed"]) == 2
    t = group_of(cfg)
    assert "(2 findings)" in t.heading and second["idea_id"] == t.properties["ID"]
    assert len(body(cfg, t).splitlines()) == 2


def test_crash_between_set_summary_and_retitle_is_repaired_under_a_tight_cap(tmp_path,
                                                                             monkeypatch):  # AC5
    cfg = fu_cfg(tmp_path)
    st = fu_state(tmp_path, [adv("minor", "one"), adv("minor", "two", files=["b.py"])])
    real = EX.retitle_idea
    calls = []

    def flaky(cfg_, sel, title):
        calls.append(sel)
        if len(calls) == 1:
            raise OSError("boom")
        return real(cfg_, sel, title)

    monkeypatch.setattr(EX, "retitle_idea", flaky)
    first = file_now(cfg, st, capped_profile(2))
    assert len(first["unfiled"]) == 2 and "(0 findings)" in group_of(cfg).heading
    assert len(body(cfg, group_of(cfg)).splitlines()) == 2    # the lines were written
    second = file_now(cfg, st, capped_profile(2))
    t = group_of(cfg)
    assert second["capped"] == [] and second["unfiled"] == []  # the own lines are not "L"
    assert len(second["filed"]) == 2 and second["idea_id"] == t.properties["ID"]
    assert "(2 findings)" in t.heading and len(body(cfg, t).splitlines()) == 2
    assert {e["idea_id"] for e in S.read_ledger(cfg, CID, types=["followup"])} == {
        t.properties["ID"]}


def test_crash_before_the_events_recovers_from_the_own_group(tmp_path):               # AC5
    cfg = fu_cfg(tmp_path)
    st = fu_state(tmp_path, [adv("minor", "one"), adv("minor", "two", files=["b.py"])])
    first = file_now(cfg, st, capped_profile(2), record=False)  # the events never reached the ledger
    assert S.read_ledger(cfg, CID, types=["followup"]) == []
    second = file_now(cfg, st, capped_profile(2))
    assert len(ideas(cfg)) == 1 and second["capped"] == []
    assert second["idea_id"] == first["idea_id"]
    assert [e["idea_id"] for e in S.read_ledger(cfg, CID, types=["followup"])] == [
        first["idea_id"]] * 2
    assert len(body(cfg, group_of(cfg)).splitlines()) == 2


def test_recovered_lines_count_against_the_cap_of_the_remaining_findings(tmp_path):    # AC5
    cfg = fu_cfg(tmp_path)
    advs = [adv("major", "one"), adv("minor", "two", files=["b.py"]),
            adv("minor", "three", files=["c.py"])]
    file_now(cfg, fu_state(tmp_path, advs[:1]), capped_profile(2), record=False)
    summary = file_now(cfg, fu_state(tmp_path, advs), capped_profile(2))
    assert [f["title"] for f in summary["filed"]] == ["one", "three"]   # by title
    assert [c["title"] for c in summary["capped"]] == ["two"]
    assert "(2 findings)" in group_of(cfg).heading


def test_a_ticked_line_still_counts_and_dedups(tmp_path):                              # AC5
    cfg = fu_cfg(tmp_path)
    st = fu_state(tmp_path, [adv("minor", "one"), adv("minor", "two", files=["b.py"])])
    file_now(cfg, st, record=False)
    t = group_of(cfg)
    ticked = body(cfg, t).replace("- [ ]", "- [X]", 1)
    EX.set_summary(cfg, t.properties["ID"], ticked)
    second = file_now(cfg, st)
    assert len(second["filed"]) == 2 and len(ideas(cfg)) == 1
    t = group_of(cfg)
    assert body(cfg, t).count("nwfu:") == 2 and "- [X]" in body(cfg, t)
    assert "(2 findings)" in t.heading


def test_an_archived_own_group_is_not_adopted(tmp_path):                              # 0266 AC4
    """SPEC-0266: a closed own group counts as absent even after it was archived; lines filed
    there without a ledger event are filed again in a new open group."""
    cfg = fu_cfg(tmp_path)
    st = fu_state(tmp_path, [adv("minor", "one")])
    first = file_now(cfg, st, record=False)                  # the events never reached the ledger
    t = group_of(cfg)
    EX.close_idea(cfg, t.properties["ID"], "drop", reason="done")
    t = next(x for x in ideas(cfg) if x.properties["ID"] == first["idea_id"])
    OW.archive_idea(cfg, t)
    archived = open(OW.ideas_archive_path(cfg)).read()
    assert f"nwfug:{CID}" not in open(cfg["org_ideas_file"]).read()
    second = file_now(cfg, st)
    assert second["idea_id"] and second["idea_id"] != first["idea_id"]
    assert [f["idea_id"] for f in second["filed"]] == [second["idea_id"]]
    assert open(OW.ideas_archive_path(cfg)).read() == archived
    assert [x.properties["ID"] for x in ideas(cfg)
            if f"nwfug:{CID}" in x.heading and x.file == cfg["org_ideas_file"]] == [
        second["idea_id"]]


def test_filer_ignores_closed_own_group(tmp_path):                                  # 0266 AC4
    cfg = fu_cfg(tmp_path)
    closed = seed_group(cfg, CID, [], close=True)
    t = next(x for x in ideas(cfg) if x.properties["ID"] == closed)
    before = body(cfg, t)
    summary = file_now(cfg, fu_state(tmp_path, [adv("minor", "one")]))
    assert summary["idea_id"] and summary["idea_id"] != closed
    assert [f["idea_id"] for f in summary["filed"]] == [summary["idea_id"]]
    t = next(x for x in ideas(cfg) if x.properties["ID"] == closed)
    assert t.is_done and body(cfg, t) == before
    new = next(x for x in ideas(cfg) if x.properties["ID"] == summary["idea_id"])
    assert k12("one") in body(cfg, new) and not new.is_done


def test_filer_rereads_group_under_lock(tmp_path, monkeypatch):                     # 0266 AC3
    """A concurrent writer's Summary read-modify-write waits on `org_write_lock` while `_file`
    holds the group, so neither its line nor the new follow-up lines are lost."""
    cfg = fu_cfg(tmp_path)
    st = fu_state(tmp_path, [adv("minor", "one")])
    file_now(cfg, st, record=False)                          # an own group, no ledger events
    group = group_of(cfg).properties["ID"]
    st["last_review"]["advisory"].append(adv("minor", "two", files=["b.py"]))
    real = EX.read_idea_enrichment
    calls, done = [], threading.Event()

    def human():
        with OW.org_write_lock(cfg):
            ORG.invalidate_tasks_cache()
            t = OW.resolve_selector(cfg, group)
            EX.set_summary(cfg, group, real(cfg, t)["summary"] + "\n- a human note")
        done.set()

    def read(cfg_, task):
        out = real(cfg_, task)
        if not calls:
            calls.append(task)
            th = threading.Thread(target=human)
            th.start()
            calls.append(th)
            done.wait(1.0)                                   # the old code let it write here
        return out

    monkeypatch.setattr(EX, "read_idea_enrichment", read)
    summary = file_now(cfg, st)
    calls[1].join(10)
    assert not calls[1].is_alive() and summary["unfiled"] == []
    monkeypatch.undo()
    text = body(cfg, group_of(cfg))
    assert "a human note" in text and k12("one") in text and k12("two", ["b.py"]) in text


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
        ("one", "OSError('disk full')"), ("two", "OSError('disk full')")]
    assert first["filed"] == [] and first["idea_id"] is None
    assert S.read_ledger(cfg, CID, types=["followup"]) == []
    assert any("follow-ups not filed" in n["text"] for n in S.read_ledger(cfg, CID, types=["note"]))
    second = file_now(cfg, st)
    assert second["unfiled"] == [] and [f["title"] for f in second["filed"]] == ["one", "two"]
    assert len(ideas(cfg)) == 1


def test_non_oserror_from_the_org_layer_never_escapes(tmp_path, monkeypatch):
    cfg = fu_cfg(tmp_path)
    st = fu_state(tmp_path, [adv("minor", "one")])
    monkeypatch.setattr(FU, "find_marker",
                        lambda *a, **k: (_ for _ in ()).throw(KeyError("boom")))
    summary = file_now(cfg, st)
    assert summary["filed"] == [] and summary["unfiled"][0]["error"] == "KeyError('boom')"
    monkeypatch.undo()
    monkeypatch.setattr(S, "read_ledger", lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("no ledger")))
    events, summary = FU.file_followups(cfg, st, BAL)       # outer guard
    assert summary["filed"] == [] and events and events[0][0] == "note"
    assert "RuntimeError('no ledger')" in events[0][1]["text"]
    assert [(u["severity"], u["title"], u["error"]) for u in summary["unfiled"]] == [
        ("minor", "one", "RuntimeError('no ledger')")]            # reported, not dropped


def test_group_without_an_id_is_not_adopted_and_not_duplicated(tmp_path):
    cfg = fu_cfg(tmp_path)
    with open(cfg["org_ideas_file"], "w") as fh:
        fh.write(f"* IDEA nwfug:{CID} [{SPEC}] review follow-ups (0 findings)\n")
    events, summary = FU.file_followups(cfg, fu_state(tmp_path, [adv("minor", "one")]), BAL)
    assert summary["filed"] == [] and "has no :ID:" in summary["unfiled"][0]["error"]
    assert open(cfg["org_ideas_file"]).read().count(f"nwfug:{CID}") == 1


def test_headline_without_the_marker_adds_a_note_but_still_records(tmp_path, monkeypatch):
    cfg = fu_cfg(tmp_path)
    real = OW.add_idea

    def lossy(cfg_, text, **kw):
        path, headline, idea_id = real(cfg_, text, **kw)
        return path, "* IDEA mangled", idea_id

    monkeypatch.setattr(OW, "add_idea", lossy)
    events, summary = FU.file_followups(cfg, fu_state(tmp_path, [adv("minor")]), BAL)
    assert [e[0] for e in events] == ["note", "followup"]
    assert f"lost its {FU.GROUP_MARKER}" in events[0][1]["text"]
    assert len(summary["filed"]) == 1 and summary["unfiled"] == []


def test_group_marker_round_trips_through_the_real_add_task_path(tmp_path):
    cfg = fu_cfg(tmp_path)
    marker = f"nwfug:{CID}"
    assert FU.find_marker(cfg, marker) is None             # no ideas file yet
    _, headline, idea_id = OW.add_idea(cfg, FU.group_title(CID, SPEC, 0), kind="improvement")
    assert marker in headline and FU.find_marker(cfg, marker) == idea_id
    assert FU.find_marker(cfg, "nwfu:000000000000") is None
    assert FU.find_marker(cfg, f"nwfu:{CID}") is None       # a group is never a single


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


# ---- SPEC-0235: human overrides ----------------------------------------------------------

def hw(fid, title, files=(), **kw):
    return dict({"id": fid, "title": title, "files": list(files), "severity": "major",
                 "synthetic": False, "ac": None}, **kw)


def htri(verdicts, human_waived=(), attested=(), profile=BAL):
    return Q.triage_reviews(verdicts, profile, (), human_waived, attested)


def test_human_waiver_by_id_drops_a_must_fix_and_routes_pass():
    vs = {A: v("fix", f("must_fix", "broken", files=["a.py"]), f("minor", "taste"))}
    assert tri(vs)["outcome"] == "fix"
    out = htri(vs, [hw(f"{A}#0", "broken", ["a.py"], severity="must_fix")])
    assert out["outcome"] == "pass" and out["fixing"] == [] and out["human_waived"] == [f"{A}#0"]
    assert [x["id"] for x in out["advisory"]] == [f"{A}#1"]


def test_human_waiver_matches_a_fresh_review_by_title_and_files():
    old = {A: v("fix", f("major", "Sorted output", files=["b.py", "a.py"]))}
    waiver = hw(f"{A}#0", "Sorted output", ["b.py", "a.py"])
    fresh = {A: v("fix", f("minor", "other"), f("major", " sorted  OUTPUT ", files=["a.py", "b.py"]))}
    assert tri(fresh)["outcome"] == "fix"
    out = htri(fresh, [waiver])
    assert out["outcome"] == "pass" and out["human_waived"] == [f"{A}#1"]
    assert htri(old, [waiver])["outcome"] == "pass"


def test_a_reworded_title_on_re_review_is_not_matched():
    waiver = hw(f"{A}#0", "Sorted output", ["a.py"])
    fresh = {A: v("fix", f("minor", "other"), f("major", "Output must be sorted", files=["a.py"]))}
    assert htri(fresh, [waiver])["outcome"] == "fix"            # documented limit


def test_synthetic_waiver_matches_by_id_and_title_only():
    vs = {A: v("fix", brief="rewrite it")}
    waiver = hw(f"{A}#fix", "rewrite it", synthetic=True)
    assert htri(vs, [waiver])["outcome"] == "pass"
    assert htri({A: v("fix", brief="rewrite it differently")}, [waiver])["outcome"] == "fix"


def test_attested_ac_is_neither_unmet_nor_synthetic():
    vs = {A: v("pass", ac=[("AC 4 output is sorted", False), ("AC 5 other thing", True)])}
    assert tri(vs)["outcome"] == "fix" and tri(vs)["synthetic"] == [f"{A}#ac0"]
    out = htri(vs, attested=["AC 4 output is sorted"])
    assert out["outcome"] == "pass" and out["synthetic"] == [] and out["blocking"] == []
    assert out["attested"] == ["AC 4 output is sorted"]


def test_waiving_a_real_major_that_covers_an_unmet_ac_clears_the_ac():
    vs = {A: v("fix", f("major", "unsorted", ac="AC 4 output is sorted"),
               ac=[("AC 4 output is sorted", False)])}
    assert tri(vs)["outcome"] == "fix"
    out = htri(vs, [hw(f"{A}#0", "unsorted", ac="AC 4 output is sorted",
                       resolved_ac=["AC 4 output is sorted"])])
    assert out["outcome"] == "pass" and out["blocking"] == []
    assert out["attested"] == ["AC 4 output is sorted"]


def test_attestation_matches_exact_ac_only():
    longer = "uv run pytest passes with the new regression tests"
    vs = {A: v("pass", ac=[(longer, False)])}
    out = htri(vs, attested=["uv run pytest passes"])
    assert out["outcome"] == "fix" and out["attested"] == [] and out["synthetic"] == [f"{A}#ac0"]
    vs = {A: v("pass", ac=[("UV run pytest passes ", False)])}
    out = htri(vs, attested=["uv run pytest passes"])
    assert out["outcome"] == "pass" and out["attested"] == ["uv run pytest passes"]


def test_synthetic_waiver_does_not_key_real_findings():
    vs = {A: v("fix", f("major", "Fix the thing"))}
    assert htri(vs)["outcome"] == "fix"
    out = htri(vs, [hw("p#fix", "Fix the thing", synthetic=True)])
    assert out["outcome"] == "fix" and out["human_waived"] == []
    out = htri(vs, [hw("p#ac3", "Fix the thing", synthetic=True)])
    assert out["outcome"] == "fix"
    out = htri(vs, [hw("p#7", "Fix the thing")])          # a real waiver still keys by title+files
    assert out["outcome"] == "pass"


def test_waiver_resolved_ac_clears_substring_ac():
    full = "AC 4 output is sorted by name"
    vs = {A: v("pass", f("major", "unsorted", ac="output is sorted"), ac=[(full, False)])}
    w = hw(f"{A}#0", "unsorted", ac="output is sorted")
    assert htri(vs, [w])["outcome"] == "fix"               # the partial quote alone clears nothing
    out = htri(vs, [{**w, "resolved_ac": [full]}])
    assert out["outcome"] == "pass" and out["attested"] == [full]


def test_a_merged_finding_is_waived_by_its_merged_id():
    one = f("major", "shared", files=["a.py"])
    vs = {A: v("fix", one), B: v("fix", dict(one))}
    out = tri(vs)
    [m] = out["blocking"]
    assert m["personas"] == [A, B]
    out = htri(vs, [hw(m["id"], "shared", ["a.py"])])
    assert out["outcome"] == "pass" and out["human_waived"] == [m["id"]]


def test_no_overrides_leave_the_result_unchanged():
    vs = {A: v("fix", f("major"))}
    assert Q.triage_reviews(vs, BAL)["human_waived"] == []
    assert Q.triage_reviews(vs, BAL)["attested"] == []


def test_accept_waivers_does_not_offer_a_human_waived_finding():
    state = _state({A: minors(2)}, ON)
    state["human_overrides"] = {"waive": [hw(f"{A}#0", f"{A} m0", severity="minor")],
                                "attest": []}
    got, ignored = Q._accept_waivers(state, ON, [{"id": f"{A}#0", "reason": "r"},
                                                 {"id": f"{A}#1", "reason": "r"}])
    assert [w["id"] for w in got] == [f"{A}#1"]
    assert ignored == [(f"{A}#0", "not a finding of this review")]


def test_suggested_fix_reaches_the_brief_and_changes_nothing_else():          # SPEC-0295
    plain = {A: v("fix", f("major", "boom", detail="why", files=["a.py"]), brief="do X")}
    fixed = {A: v("fix", f("major", "boom", detail="why", files=["a.py"],
                           suggested_fix="rename x", acceptance="test_x passes"), brief="do X")}
    t0, t1 = tri(plain), tri(fixed)
    strip = lambda t: {k: [{x: y for x, y in b.items() if x != "detail"} for b in t[k]]
                       if k in ("blocking", "advisory") else t[k] for k in t}
    assert strip(t0) == strip(t1)
    brief = Q.review_brief(fixed, t1, 80)
    assert brief["must_fix"][0]["detail"] == "why\nFix: rename x\nDone when: test_x passes"
    assert Q.review_brief(plain, t0, 80)["must_fix"][0]["detail"] == "why"


# ---- SPEC-0554: delta-rule demotions (stored by `_store_verdicts` via `packet.enforce`) -------

def test_demoted_finding_is_advisory_and_never_blocks():
    out = tri({A: v("fix", f("must_fix", "old code", files=["x.py"], demoted=True))})
    assert out["outcome"] == "pass" and out["blocking"] == [] and out["fixing"] == []
    assert [a["title"] for a in out["advisory"]] == ["old code"]
    assert out["basis_demoted"] == [f"{A}#0"] and out["score"] == 100


def test_demoted_beside_real_finding_and_human_waiver():
    out = tri({A: v("fix", f("must_fix", "real", files=["a.py"]),
                    f("major", "unsupported", demoted=True))})
    assert out["outcome"] == "fix" and [b["title"] for b in out["blocking"]] == ["real"]
    assert "unsupported" in [a["title"] for a in out["advisory"]]
    hw = [{"id": f"{A}#1", "title": "unsupported", "files": [], "reason": "no"}]
    out = Q.triage_reviews({A: v("pass", f("major", "unsupported", demoted=True))}, BAL,
                           human_waived=hw)
    assert out["advisory"] == [] and "basis_demoted" not in out


def test_fixed_prior_not_counted_open_prior_counts():
    """`packet.enforce` keeps an open prior finding as a stored finding (`prior`) and drops a
    `fixed` one; the scorer then counts only what is stored."""
    from nightwright.agentic import packet as PK
    doc = {"findings": [
        {"id": "k1", "persona": A, "loop": 1, "severity": "must_fix", "title": "fixed one",
         "status": "open", "evidence": "", "files": []},
        {"id": "k2", "persona": A, "loop": 1, "severity": "must_fix", "title": "still open",
         "status": "open", "evidence": "", "files": []}]}
    enf = {"loop": 2, "delta_files": set(), "ac_ids": {"AC1"}, "doc": doc}
    for status, outcome in (("fixed", "fix"), ("both", "pass")):
        ps = [{"id": "k1", "status": "fixed"}] + (
            [{"id": "k2", "status": "fixed"}] if status == "both" else [])
        verdict = v("pass")
        verdict["prior_status"] = ps
        stored, demoted = PK.enforce("SPEC-1", A, verdict, enf)
        out = tri({A: dict(verdict, findings=stored)})
        assert out["outcome"] == outcome and demoted == 0
        assert "fixed one" not in [b["title"] for b in out["blocking"]]


def test_missed_ac_with_unknown_ac_is_demoted():
    from nightwright.agentic import packet as PK
    enf = {"loop": 2, "delta_files": {"a.py"}, "ac_ids": {"AC1"}, "doc": {"findings": []}}
    verdict = v("fix", f("minor", "missed", ac="AC5", basis="missed_ac"))
    stored, demoted = PK.enforce("SPEC-1", A, verdict, enf)
    assert demoted == 1
    out = tri({A: dict(verdict, findings=stored)})
    assert out["outcome"] == "pass" and out["basis_demoted"] == [f"{A}#0"]
    assert "basis_outside_delta" not in out


# ---- SPEC-0596: a major / must_fix with an invalid basis stays blocking --------------------

@pytest.mark.parametrize("sev", ["major", "must_fix"])
def test_blocking_severity_outside_delta_stays_blocking_and_marked(sev):
    from nightwright.agentic import packet as PK
    enf = {"loop": 2, "delta_files": {"a.py"}, "ac_ids": {"AC1"}, "doc": {"findings": []}}
    verdict = v("fix", f(sev, "MAC reflection", files=["old.py"], basis="initial"),
                f("minor", "naming", files=["old.py"]))
    stored, demoted = PK.enforce("SPEC-1", A, verdict, enf)
    assert demoted == 1
    out = tri({A: dict(verdict, findings=stored)})
    assert out["outcome"] == "fix" and out["fixing"] == [A]
    assert [(b["title"], b.get("basis_outside_delta")) for b in out["blocking"]] == [
        ("MAC reflection", True)]
    assert out["basis_outside_delta"] == [f"{A}#0"] and out["basis_demoted"] == [f"{A}#1"]
    assert [a["title"] for a in out["advisory"]] == ["naming"]
    assert "basis_outside_delta" not in out["advisory"][0]


def test_outside_delta_marker_survives_merge_into_unmarked_finding():
    """SPEC-0596 (review fix): an identical finding (same files and title) from two reviewers
    merges into the first one; the marker of a later, marked member is kept."""
    out = tri({A: v("fix", f("minor", "MAC reflection", files=["old.py"])),
               B: v("fix", f("major", "MAC reflection", files=["old.py"],
                             basis_outside_delta=True))})
    [b] = out["blocking"]
    assert b["severity"] == "major" and b["basis_outside_delta"] is True
    assert out["basis_outside_delta"] == [b["id"]]
    out = tri({A: v("fix", f("major", "MAC reflection", files=["old.py"])),
               B: v("fix", f("major", "MAC reflection", files=["old.py"],
                             basis_outside_delta=True))})
    assert out["blocking"][0]["basis_outside_delta"] is True


def test_outside_delta_major_merged_and_human_waived():
    """The marked finding is an ordinary finding to the scorer: a human waiver removes it, and
    then neither the marker list nor a block remains."""
    stored = [f("major", "MAC reflection", files=["old.py"], basis_outside_delta=True)]
    hw = [{"id": f"{A}#0", "title": "MAC reflection", "files": ["old.py"], "reason": "known"}]
    out = Q.triage_reviews({A: v("fix", *stored)}, BAL, human_waived=hw)
    assert out["outcome"] == "pass" and "basis_outside_delta" not in out


def test_flag_off_triage_result_shape_unchanged():
    out = tri({A: v("fix", f("must_fix", "x"))})
    assert list(out) == ["outcome", "score", "score_only", "blocking", "advisory",
                         "waivable_weight", "fixing", "synthetic", "human_waived", "attested"]


# ---- SPEC-0555: scoring on the in-scope set ------------------------------------------------

def test_out_of_scope_findings_are_never_scored():
    """A finding the collation scoped out is listed under `out_of_scope` (never blocking,
    advisory or scored); the fix verdict it backed does not survive and no synthetic is added."""
    out = tri({A: v("fix", f("major", "old code", scope="out", tie="none"),
                    f("minor", "naming", scope="in", tie="AC1"), brief="fix both")})
    assert out["outcome"] == "pass" and out["score"] == 96 and out["fixing"] == []
    assert [x["title"] for x in out["out_of_scope"]] == ["old code"]
    assert [x["title"] for x in out["advisory"]] == ["naming"]
    assert out["advisory"][0]["tie"] == "AC1" and out["synthetic"] == []


def test_in_scope_blocking_brief_carries_ties_and_no_out_of_scope_text():
    verdicts = {A: v("fix", f("major", "real bug", files=["a.py"], scope="in", tie="AC2"),
                     f("major", "old code", files=["b.py"], scope="out", tie="none"),
                     brief="fix the real bug and old code")}
    t = tri(verdicts)
    assert [b["title"] for b in t["blocking"]] == ["real bug"] and t["blocking"][0]["tie"] == "AC2"
    brief = Q.review_brief(verdicts, t, 80)
    assert "old code" not in brief["fix_brief"] and "[AC2] real bug" in brief["fix_brief"]
    assert brief["must_fix"][0]["tie"] == "AC2"
    multi = {**verdicts, B: v("fix", f("major", "b only", scope="in", tie="design"),
                              brief="see old code too")}
    mb = Q.review_brief(multi, tri(multi), 80)
    assert "old code" not in mb["fix_brief"] and "[design] b only" in mb["fix_brief"]


def test_without_scope_keys_the_result_has_no_collation_keys():
    out = tri({A: v("fix", f("major"))})
    assert "out_of_scope" not in out and all("tie" not in b for b in out["blocking"])
