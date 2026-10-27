"""SPEC-0242: `wt spec review --show` and `wt spec reviews` read stored reviews without running."""
import json
import subprocess

from test_agentic_e2e import make_cfg, make_repo, run_cli, script, write_review
from test_spec_review_store import _second_repo
from wt.agentic import spec_review as SR

FINDINGS = [{"severity": "must_fix", "section": "Design", "title": "gap", "detail": "d"},
            {"severity": "major", "section": "Context", "title": "vague", "detail": "e"}]
SIZED = {"size": "m", "tone": "high-end", "rationale": "r"}


def _setup(tmp_path):
    repo = make_repo(tmp_path, review=False)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, []))      # an empty script fails if a reviewer runs
    return repo, cfg, repo / "docs" / "specs" / "0001-trivial.md"


def _show(cfg, monkeypatch, *args):
    return run_cli(cfg, monkeypatch, ["spec", "review", "SPEC-0001", "--show", *args])


def test_show_revise_runs_nothing_and_needs_no_force(tmp_path, monkeypatch):
    repo, cfg, spec = _setup(tmp_path)
    write_review(cfg, spec, "SPEC-0001", "revise", findings=FINDINGS, **SIZED)
    ran = []
    monkeypatch.setattr(SR, "select_runner", lambda cfg: ran.append(1))
    r = _show(cfg, monkeypatch)
    assert r.exit_code == 0, r.output
    assert "revise" in r.output and "gap" in r.output and "vague" in r.output
    assert "reviewing " not in r.output
    sha12 = SR.spec_sha256(spec)[:12]
    assert f"reviewed {sha12} · current · admissible: no — " in r.output
    assert "not applied yet" in r.output
    assert ran == [] and SR.read_journal(cfg) == []
    adm = json.loads(_show(cfg, monkeypatch, "--json").stdout)["admission"]
    assert adm["ok"] is False and adm["kind"] is None
    assert isinstance(adm["message"], str) and adm["message"]


def test_show_after_edit_and_approve_profile(tmp_path, monkeypatch):
    repo, cfg, spec = _setup(tmp_path)
    write_review(cfg, spec, "SPEC-0001", "revise", findings=FINDINGS, **SIZED)
    spec.write_text(spec.read_text() + "\nedited\n")
    r = _show(cfg, monkeypatch)
    assert r.exit_code == 0 and "edited since · admissible: after_revision" in r.output
    write_review(cfg, spec, "SPEC-0001", "approve", **SIZED)
    r = _show(cfg, monkeypatch)
    assert "current · admissible: approved" in r.output and "→ profile" in r.output


def test_show_json_prefers_current_sha(tmp_path, monkeypatch):
    repo, cfg, spec = _setup(tmp_path)
    write_review(cfg, spec, "SPEC-0001", "revise", sha="a" * 64,
                 reviewed_at="2026-10-26T00:00:00+00:00")            # newer, but an older text
    write_review(cfg, spec, "SPEC-0001", "approve", **SIZED)
    r = _show(cfg, monkeypatch, "--json")
    assert r.exit_code == 0, r.output
    out = json.loads(r.stdout)
    sha = SR.spec_sha256(spec)
    assert out["schema"] == "wt.spec.review.show.v1" and out["current_sha"] == sha
    assert out["matches_current"] is True and out["review"]["verdict"] == "approve"
    assert out["admission"] == {"ok": True, "kind": "approved", "message": None}
    assert out["profile"]["profile"] and out["stored"] == 2
    spec.write_text(spec.read_text() + "\nedited\n")                 # the newest review is a revise
    out = json.loads(_show(cfg, monkeypatch, "--json").stdout)
    assert out["matches_current"] is False and out["profile"] is None
    assert out["admission"] == {"ok": True, "kind": "after_revision", "message": None}


def test_show_errors_and_path_scope(tmp_path, monkeypatch):
    repo, cfg, spec = _setup(tmp_path)
    r = _show(cfg, monkeypatch)
    assert r.exit_code == 1
    assert "SPEC-0001 has no stored review; run wt spec review SPEC-0001" in r.output
    r = _show(cfg, monkeypatch, "--force")
    assert r.exit_code == 2 and "--force" in r.output
    other, ospec = _second_repo(tmp_path)
    write_review(cfg, spec, "SPEC-0001", "revise")                     # an unrelated spec id
    show_ospec = ["spec", "review", "--path", str(ospec), "--show"]
    r = run_cli(cfg, monkeypatch, show_ospec)
    assert r.exit_code == 1 and "SPEC-0007 has no stored review" in r.output
    # a SPEC-0007 review of a different, existing repository, newer and with another verdict
    write_review(cfg, ospec, "SPEC-0007", "revise", repo_root=str(repo),
                 reviewed_at="2026-10-26T00:00:00+00:00", **SIZED)
    r = run_cli(cfg, monkeypatch, show_ospec)
    assert r.exit_code == 1 and "SPEC-0007 has no stored review" in r.output
    r = run_cli(cfg, monkeypatch, [*show_ospec, "--json"])
    assert r.exit_code == 1
    write_review(cfg, ospec, "SPEC-0007", "split", repo_root=str(other), **SIZED)
    r = run_cli(cfg, monkeypatch, [*show_ospec, "--json"])
    assert r.exit_code == 0, r.output
    out = json.loads(r.stdout)
    assert out["review"]["verdict"] == "split" and out["stored"] == 1


def test_reviews_list_filter_json_and_unresolvable(tmp_path, monkeypatch):
    repo, cfg, spec = _setup(tmp_path)
    r = run_cli(cfg, monkeypatch, ["spec", "reviews"])
    assert r.exit_code == 0 and "no stored spec reviews" in r.output
    write_review(cfg, spec, "SPEC-0001", "approve", sha="a" * 64, **SIZED)    # older
    write_review(cfg, spec, "SPEC-0001", "revise", findings=FINDINGS, **SIZED,
                 reviewed_at="2026-10-26T00:00:00+00:00")
    write_review(cfg, spec, "SPEC-0999", "approve", sha="c" * 64, **SIZED)   # no such spec
    r = run_cli(cfg, monkeypatch, ["spec", "reviews", "--json"])
    assert r.exit_code == 0, r.output
    out = json.loads(r.stdout)
    assert out["schema"] == "wt.spec.reviews.v1"
    rows = {x["spec_id"]: x for x in out["reviews"]}
    assert set(rows) == {"SPEC-0001", "SPEC-0999"}
    assert rows["SPEC-0001"]["matches_current"] is True
    assert rows["SPEC-0001"]["counts"] == {"must_fix": 1, "major": 1}
    assert rows["SPEC-0001"]["size"] == "m" and rows["SPEC-0001"]["tone"] == "high-end"
    assert rows["SPEC-0999"]["matches_current"] is None and rows["SPEC-0999"]["verdict"] == "approve"
    r = run_cli(cfg, monkeypatch, ["spec", "reviews"])
    assert r.exit_code == 0 and "must_fix" in r.output and "2026-10-26 00:00" in r.output
    assert "yes" in r.output and "?" in r.output and "edited" not in r.output
    spec.write_text(spec.read_text() + "\nedited\n")
    r = run_cli(cfg, monkeypatch, ["spec", "reviews"])
    assert "edited" in r.output and "yes" not in r.output
    r = run_cli(cfg, monkeypatch, ["spec", "reviews", "--verdict", "revise", "--json"])
    assert [x["spec_id"] for x in json.loads(r.stdout)["reviews"]] == ["SPEC-0001"]


def test_reviews_lists_other_repo_review_with_colliding_local_id(tmp_path, monkeypatch):
    repo, cfg, spec = _setup(tmp_path)
    other, ospec = _second_repo(tmp_path)
    write_review(cfg, spec, "SPEC-0001", "split", repo_root=str(other), **SIZED)   # no local review
    out = json.loads(run_cli(cfg, monkeypatch, ["spec", "reviews", "--json"]).stdout)
    rows = {x["spec_id"]: x for x in out["reviews"]}
    assert rows["SPEC-0001"]["verdict"] == "split" and rows["SPEC-0001"]["matches_current"] is None
    r = run_cli(cfg, monkeypatch, ["spec", "reviews"])
    assert r.exit_code == 0 and "SPEC-0001" in r.output and "?" in r.output
    write_review(cfg, spec, "SPEC-0001", "approve", repo_root=str(repo), **SIZED)  # local review wins
    rows = json.loads(run_cli(cfg, monkeypatch, ["spec", "reviews", "--json"]).stdout)["reviews"]
    assert rows[0]["verdict"] == "approve" and rows[0]["matches_current"] is True


def test_reviews_table_orders_counts_and_marks_none(tmp_path, monkeypatch):
    repo, cfg, spec = _setup(tmp_path)
    write_review(cfg, spec, "SPEC-0001", "revise", findings=[*FINDINGS, FINDINGS[1]], **SIZED)
    write_review(cfg, spec, "SPEC-0002", "approve", sha="b" * 64, **SIZED)
    r = run_cli(cfg, monkeypatch, ["spec", "reviews"])
    assert r.exit_code == 0, r.output
    lines = {ln.split()[0]: ln for ln in r.output.splitlines() if "SPEC-000" in ln}
    assert "1 must_fix · 2 major" in lines["SPEC-0001"]
    assert "2 major · 1 must_fix" not in lines["SPEC-0001"]
    assert " - " in lines["SPEC-0002"]            # no findings


def test_reviews_skips_ids_without_a_readable_review(tmp_path, monkeypatch):
    repo, cfg, spec = _setup(tmp_path)
    write_review(cfg, spec, "SPEC-0001", "approve", **SIZED)
    root = SR.store_path(cfg, "SPEC-0001", "x").parent.parent
    (root / "SPEC-0002").mkdir()                                            # empty directory
    (root / "SPEC-0003").mkdir()
    (root / "SPEC-0003" / ("d" * 64 + ".json")).write_text("{not json")     # corrupt only
    (root / "SPEC-0004").mkdir()
    (root / "SPEC-0004" / ("e" * 64 + ".json")).write_text("[1]")           # not an object
    r = run_cli(cfg, monkeypatch, ["spec", "reviews", "--json"])
    assert r.exit_code == 0, r.output
    assert [x["spec_id"] for x in json.loads(r.stdout)["reviews"]] == ["SPEC-0001"]
    r = run_cli(cfg, monkeypatch, ["spec", "reviews"])
    assert r.exit_code == 0 and "SPEC-0002" not in r.output and "SPEC-0003" not in r.output


def test_reviews_tolerates_malformed_stored_reviews(tmp_path, monkeypatch):
    repo, cfg, spec = _setup(tmp_path)
    write_review(cfg, spec, "SPEC-0001", "revise", findings="none", size=3, tone=None)
    write_review(cfg, spec, "SPEC-0002", "revise", sha="b" * 64, findings=["text"])
    write_review(cfg, spec, "SPEC-0003", "revise", sha="c" * 64,
                 findings=[{"severity": 2, "title": "t"}, {"severity": "major"}, "x"])
    r = run_cli(cfg, monkeypatch, ["spec", "reviews"])
    assert r.exit_code == 0, r.output
    rows = {x["spec_id"]: x for x in json.loads(
        run_cli(cfg, monkeypatch, ["spec", "reviews", "--json"]).stdout)["reviews"]}
    assert rows["SPEC-0001"]["counts"] == {} and rows["SPEC-0002"]["counts"] == {"unknown": 1}
    assert rows["SPEC-0003"]["counts"] == {"unknown": 2, "major": 1}
    r = _show(cfg, monkeypatch)                                             # the single view too
    assert r.exit_code == 0, r.output


def test_reviews_compares_against_the_cwd_checkout(tmp_path, monkeypatch):
    repo, cfg, spec = _setup(tmp_path)
    wt = tmp_path / "wt-copy"
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", "-b", "feat", str(wt)],
                   check=True)
    wspec = wt / "docs" / "specs" / "0001-trivial.md"
    write_review(cfg, spec, "SPEC-0001", "revise", findings=FINDINGS, **SIZED)
    wspec.write_text(wspec.read_text() + "\nedited in the worktree\n")
    monkeypatch.chdir(wt)
    out = json.loads(run_cli(cfg, monkeypatch, ["spec", "reviews", "--json"]).stdout)
    assert out["reviews"][0]["matches_current"] is False                    # as --show says
    assert json.loads(_show(cfg, monkeypatch, "--json").stdout)["matches_current"] is False
    monkeypatch.chdir(tmp_path)
    out = json.loads(run_cli(cfg, monkeypatch, ["spec", "reviews", "--json"]).stdout)
    assert out["reviews"][0]["matches_current"] is True


def test_reviews_empty_store_prints_empty_table_and_filter_message(tmp_path, monkeypatch):
    repo, cfg, spec = _setup(tmp_path)
    r = run_cli(cfg, monkeypatch, ["spec", "reviews"])
    assert r.exit_code == 0 and "verdict" in r.output and "no stored spec reviews" in r.output
    write_review(cfg, spec, "SPEC-0001", "approve", **SIZED)
    r = run_cli(cfg, monkeypatch, ["spec", "reviews", "--verdict", "split"])
    assert r.exit_code == 0 and "no split spec reviews" in r.output
    assert "no stored spec reviews" not in r.output


def test_show_renders_markup_like_text_verbatim(tmp_path, monkeypatch):
    repo, cfg, spec = _setup(tmp_path)
    findings = [{"severity": "major", "section": "[red]Design", "title": "[/oops] keep [x]",
                 "detail": "d [/]"}]
    write_review(cfg, spec, "SPEC-0001", "revise", findings=findings, size="m", tone="[bold]t",
                 rationale="why [/x]", model="[red]m",
                 suggested_splits=[{"title": "a [/b]", "scope": "c [/d]"}])
    r = _show(cfg, monkeypatch)
    assert r.exit_code == 0, r.output
    for text in ("[/oops] keep [x]", "[red]m", "[red]Design", "why [/x]", "a [/b]", "c [/d]"):
        assert text in r.output, r.output
