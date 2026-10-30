"""SPEC-0188: `wt spec review` (fake runner), the sidecar, the `wt queue add` review gate, the
spec_reviewer persona/profile defaults, and the linter's placeholder rule."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from test_agentic_e2e import make_cfg, make_repo, run_cli, script, write_review
from wt.agentic import personas as PS
from wt.agentic import profiles as PR
from wt.agentic import spec_review as SR
from wt.agentic.runner import build_argv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import spec_lint as L  # noqa: E402

SIZED = {"size": "m", "tone": "high-end", "rationale": "7 ACs across three modules."}
APPROVE = {"verdict": "approve", **SIZED,
           "findings": [{"severity": "nit", "section": "Context", "title": "wording",
                         "suggested_fix": "reword the first sentence"}],
           "suggested_splits": []}


def _review_entry(verdict=None, **kw):
    return {"persona": "spec_reviewer", "verdict": verdict or APPROVE, "cost_usd": 0.42, **kw}


def _setup(tmp_path, entries):
    repo = make_repo(tmp_path, review=False)
    return repo, make_cfg(tmp_path, repo, script(tmp_path, entries))


def _spec(repo):
    return repo / "docs" / "specs" / "0001-trivial.md"


def test_review_writes_valid_sidecar(tmp_path, monkeypatch):
    repo, cfg = _setup(tmp_path, [_review_entry()])
    r = run_cli(cfg, monkeypatch, ["spec", "review", "SPEC-0001", "--json"])
    assert r.exit_code == 0, r.output
    out = json.loads(r.stdout)
    side = SR.load_review(cfg, "SPEC-0001", SR.spec_sha256(_spec(repo)))
    assert not (repo / "docs" / "specs" / "reviews").exists()
    assert out.pop("profile") == "balanced" and out == side        # SPEC-0219: not stored
    assert side["schema"] == "wt.spec.review.v1" and side["spec_id"] == "SPEC-0001"
    assert side["verdict"] == "approve" and side["cost_usd"] == 0.42
    assert side["spec_sha256"] == SR.spec_sha256(_spec(repo)) and side["model"] == "sonnet"
    assert side["findings"][0]["title"] == "wording" and side["suggested_splits"] == []
    assert side["reviewed_at"]


def test_review_uses_the_project_profile_layer(tmp_path, monkeypatch):           # SPEC-0237
    repo, cfg = _setup(tmp_path, [_review_entry()])
    prof = repo / ".wt" / "profiles"
    prof.mkdir(parents=True, exist_ok=True)
    (prof / "balanced.yaml").write_text(
        "schema: wt.profile.v1\npersonas: {spec_reviewer: {model: opus}}\n")
    r = run_cli(cfg, monkeypatch, ["spec", "review", "SPEC-0001", "--json"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.stdout)["model"] == "opus"
    (prof / "balanced.yaml").write_text("schema: wt.profile.v1\nbogus: 1\n")
    r = run_cli(cfg, monkeypatch, ["spec", "review", "SPEC-0001", "--json",
                                   "--force"])                         # SPEC-0230: 2nd round
    assert r.exit_code != 0 and "balanced.yaml: bogus: unknown key" in r.output


def test_review_pretty_output(tmp_path, monkeypatch):
    split = {"verdict": "split", **SIZED, "findings": [{"severity": "major", "section": "Design",
                                               "title": "too big", "detail": "x",
                                               "suggested_fix": "split it"}],
             "suggested_splits": [{"title": "Part A", "scope": "first half"}]}
    repo, cfg = _setup(tmp_path, [_review_entry(split)])
    r = run_cli(cfg, monkeypatch, ["spec", "review", "SPEC-0001"])
    assert r.exit_code == 0, r.output
    assert "split" in r.output and "too big" in r.output and "Part A" in r.output
    assert "$0.42" in r.output


@pytest.mark.parametrize("entry", [
    _review_entry(malformed=True),
    _review_entry({"verdict": "maybe", **SIZED, "findings": []}),
    _review_entry({"verdict": "approve", **SIZED, "findings": [{"severity": "must_fix",
                                                                 "title": "t",
                                                                 "suggested_fix": "f"}]}),
    _review_entry({"verdict": "approve", "findings": []}),                 # SPEC-0219: unsized
    _review_entry({"verdict": "approve", "size": "xl", "tone": "critical", "rationale": "r",
                   "findings": []}),
    _review_entry({"verdict": "revise", "size": "xl", "tone": "critical", "rationale": "r",
                   "findings": [{"severity": "major", "title": "t",
                                 "suggested_fix": "f"}]}),
    _review_entry(exit_code=1),
    _review_entry(timed_out=True),
])
def test_bad_review_errors_and_writes_nothing(tmp_path, monkeypatch, entry):
    repo, cfg = _setup(tmp_path, [entry])
    r = run_cli(cfg, monkeypatch, ["spec", "review", "SPEC-0001"])
    assert r.exit_code != 0 and "SPEC-0001" in r.output
    assert not (Path(cfg["data_dir"]) / "agentic" / "reviews").exists()


def test_review_request_is_read_only_and_persona_has_defaults():
    p = PS.get_persona("spec_reviewer")
    assert p.read_only and p.schema["required"] == ["verdict", "findings", "size", "tone",
                                                  "rationale"]
    assert p.schema["properties"]["verdict"]["enum"] == ["approve", "revise", "split"]
    for ph in ("{{spec_id}}", "{{spec_text}}", "{{idea_text}}", "{{schema_text}}"):
        assert ph in p.template
    budgets = {}
    for name, prof in PR.builtin_profiles().items():
        s = prof["personas"]["spec_reviewer"]
        assert s["read_only"] is True and s["permission_mode"] == "dontAsk"
        budgets[name] = (s["model"], s["max_budget_usd"])
    assert budgets == {"balanced": ("sonnet", 0.8), "thorough": ("opus", 1.5),
                       "frugal": ("sonnet", 0.4)}


def test_review_run_is_restricted_and_prompt_carries_inputs(tmp_path, monkeypatch):
    repo, cfg = _setup(tmp_path, [_review_entry()])
    seen = []

    class Spy:
        def run(self, req):
            seen.append(req)
            raise ValueError("stop")

    monkeypatch.setattr(SR, "select_runner", lambda cfg: Spy())
    with pytest.raises(ValueError, match="stop"):
        SR.review_spec(cfg, "SPEC-0001")
    req = seen[0]
    assert req.read_only and req.allowed_tools == () and req.cwd == repo
    assert "--restricted" in build_argv(req)
    assert "WT_CAMPAIGN" not in req.env and req.env["WT_SPEC_ID"] == "SPEC-0001"
    assert req.env["WT_PERSONA_RUN"] == "1"                           # SPEC-0272
    assert "Ship `feature.txt`." in req.prompt and "{{" not in req.prompt
    assert "queue" not in req.system_append
    assert req.run_dir.parent.name == "spec-reviews"


# ---- the queue gate ------------------------------------------------------------------------

def _add(cfg, monkeypatch, *extra):
    return run_cli(cfg, monkeypatch, ["queue", "add", "SPEC-0001", "--json", *extra])


def test_queue_add_refuses_without_approving_review(tmp_path, monkeypatch):
    repo, cfg = _setup(tmp_path, [])
    want = "SPEC-0001 has no spec review; run wt spec review SPEC-0001"
    r = _add(cfg, monkeypatch)                                        # no review at all
    assert r.exit_code != 0 and want in r.output
    write_review(cfg, _spec(repo), "SPEC-0001", verdict="revise")     # SPEC-0279: amended first
    r = _add(cfg, monkeypatch)
    assert r.exit_code == 0 and json.loads(r.stdout)["review"] == "revise_pending"
    write_review(cfg, _spec(repo), "SPEC-0001", verdict="split")
    assert _add(cfg, monkeypatch).exit_code != 0


def test_queue_add_accepts_matching_approve_and_refuses_stale_hash(tmp_path, monkeypatch):
    repo, cfg = _setup(tmp_path, [])
    write_review(cfg, _spec(repo), "SPEC-0001")
    r = _add(cfg, monkeypatch)
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["review"] == "approved"
    _spec(repo).write_text(_spec(repo).read_text() + "\nedited after review\n")
    r = _add(cfg, monkeypatch)
    assert r.exit_code != 0 and "edited after its approving review" in r.output


def test_queue_add_force_admits_and_records_forced(tmp_path, monkeypatch):
    repo, cfg = _setup(tmp_path, [])
    r = _add(cfg, monkeypatch, "--force")
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["review"] == "forced"


def test_review_then_queue_add_round_trip(tmp_path, monkeypatch):
    repo, cfg = _setup(tmp_path, [_review_entry()])
    assert run_cli(cfg, monkeypatch, ["spec", "review", "SPEC-0001"]).exit_code == 0
    r = _add(cfg, monkeypatch)
    assert r.exit_code == 0, r.output


# ---- linter placeholder rule -----------------------------------------------------------------

_BASE = (ROOT / "docs" / "specs" / "TEMPLATE.md").read_text()


def test_first_placeholder_forms():
    assert L.first_placeholder("a\n<The chosen approach in a few sentences.>\n") == (
        2, "a line starting '<The chosen approach'")
    assert L.first_placeholder("x\ny\n- …\n")[0] == 3
    assert L.first_placeholder("  - …  \n")[0] == 1
    assert L.first_placeholder("- **<option>** — why\n")[0] == 1
    assert L.first_placeholder('---\n# milestone: "M?: <phase>"\n---\n')[0] == 2


def test_first_placeholder_ignores_code_and_prose():
    text = ("Mention `<option>` and `- …` inline, or a line starting `<The chosen approach`.\n"
            "```\n- …\n<option>\n```\n- a real bullet … with text\n")
    assert L.first_placeholder(text) is None


def test_template_itself_has_placeholders():
    assert L.first_placeholder(_BASE) is not None


@pytest.mark.parametrize("line", ["<The chosen approach, stated.>", "- …", "- **<option>** — x",
                                  '# milestone: "M?: <phase>"'])
@pytest.mark.parametrize("status,flagged", [("accepted", True), ("in-progress", True),
                                            ("done", True), ("draft", False),
                                            ("proposed", False)])
def test_lint_flags_placeholders_by_status(tmp_path, monkeypatch, line, status, flagged):
    specs = L.load_specs()
    s = next(x for x in specs if x["id"] == "SPEC-0179")
    s = {**s, "status": status, "text": s["text"] + "\n" + line + "\n",
         "fm": {**s["fm"], "status": status}}
    others = [x for x in specs if x["id"] != "SPEC-0179"]
    problems = [p for p in L.validate(others + [s])
                if p.startswith(s["file"]) and "placeholder" in p]
    assert bool(problems) is flagged, problems
    if flagged:
        assert f"line {len(s['text'].splitlines())}" in problems[0]


def test_current_corpus_has_no_placeholders():
    bad = [(s["file"], L.first_placeholder(s["text"])) for s in L.load_specs()
           if s["status"] in L.PLACEHOLDER_STATUSES and L.first_placeholder(s["text"])]
    assert bad == []


# ---- sizing and tone (SPEC-0219) -------------------------------------------------------------

def test_review_stores_size_tone_rationale(tmp_path, monkeypatch):
    repo, cfg = _setup(tmp_path, [_review_entry()])
    r = run_cli(cfg, monkeypatch, ["spec", "review", "SPEC-0001", "--json"])
    assert r.exit_code == 0, r.output
    out = json.loads(r.stdout)
    side = SR.load_review(cfg, "SPEC-0001", SR.spec_sha256(_spec(repo)))
    assert (side["size"], side["tone"], side["rationale"]) == ("m", "high-end", SIZED["rationale"])
    assert side["schema"] == "wt.spec.review.v1"
    assert out.pop("profile") == "balanced" and out == side      # would-pick, not stored
    assert "profile" not in side


def test_review_text_shows_size_tone_and_would_pick(tmp_path, monkeypatch):
    repo, cfg = _setup(tmp_path, [_review_entry({**APPROVE, "size": "xs", "tone": "critical"})])
    r = run_cli(cfg, monkeypatch, ["spec", "review", "SPEC-0001"])
    assert r.exit_code == 0, r.output
    assert "size xs" in r.output and "tone critical" in r.output
    assert "rationale: 7 ACs" in r.output and "→ profile thorough" in r.output


def test_xl_split_is_accepted_and_has_no_profile(tmp_path, monkeypatch):
    xl = {"verdict": "split", "size": "xl", "tone": "high-end", "rationale": "12 ACs",
          "findings": [], "suggested_splits": [{"title": "A"}]}
    repo, cfg = _setup(tmp_path, [_review_entry(xl), _review_entry(xl)])
    r = run_cli(cfg, monkeypatch, ["spec", "review", "SPEC-0001"])
    assert r.exit_code == 0, r.output
    assert "xl: split recommended, no profile" in r.output
    r = run_cli(cfg, monkeypatch, ["spec", "review", "SPEC-0001", "--json", "--force"])   # SPEC-0230: 2nd round
    out = json.loads(r.stdout)
    assert out["size"] == "xl" and out["profile"] is None


@pytest.mark.parametrize("verdict", ["approve", "revise"])
def test_xl_without_split_is_rejected_and_writes_nothing(tmp_path, verdict):
    repo, cfg = _setup(tmp_path, [_review_entry({"verdict": verdict, "size": "xl",
                                                 "tone": "high-end", "rationale": "r",
                                                 "findings": []})])
    with pytest.raises(ValueError):
        SR.review_spec(cfg, "SPEC-0001")
    assert not (repo / "docs" / "specs" / "reviews").exists()
    with pytest.raises(ValueError, match="xl"):
        SR._check_verdict("SPEC-0001", {"verdict": verdict, "size": "xl", "findings": []})


def test_template_carries_the_rubric():
    t = PS.get_persona("spec_reviewer").template
    for word in ("`xs`", "`s`", "`m`", "`l`", "`xl`", "`cost-effective`", "`high-end`",
                 "`critical`", "`rationale`"):
        assert word in t


# ---- SPEC-0230: one review round ---------------------------------------------------------------

FINDINGS = [{"severity": "major", "section": "Design", "title": "gap in step 2",
             "detail": "say what happens on timeout"},
            {"severity": "nit", "title": "wording", "detail": ""}]


def test_revision_brief_shape_and_render():
    review = {"spec_sha256": "ab" * 32, "verdict": "revise", "findings": FINDINGS}
    brief = SR.revision_brief(review)
    assert brief["source"] == "spec_review" and brief["human_notes"] == []
    assert "review abababababab," in brief["fix_brief"] and "no second review" in brief["fix_brief"]
    assert brief["must_fix"] == [
        {"title": "[major] gap in step 2 (Design)", "detail": "say what happens on timeout"},
        {"title": "[nit] wording", "detail": None}]
    text = PS.render_pending_brief(brief)
    assert "MUST FIX: [major] gap in step 2 (Design) — say what happens on timeout" in text
    assert "MUST FIX: [nit] wording" in text and text.startswith("From: spec_review")


@pytest.mark.parametrize("by_path", [False, True])
def test_spec_review_second_round_needs_force(tmp_path, monkeypatch, by_path):
    from wt.agentic.runner import FakeRunner
    repo, cfg = _setup(tmp_path, [_review_entry(), _review_entry()])
    ran = []
    inner = FakeRunner(script(tmp_path, [_review_entry(), _review_entry()], "spy.json"))

    class Spy:
        def run(self, req):
            ran.append(req)
            return inner.run(req)

    monkeypatch.setattr(SR, "select_runner", lambda cfg: Spy())
    target = ["--path", str(_spec(repo))] if by_path else ["SPEC-0001"]
    r = run_cli(cfg, monkeypatch, ["spec", "review", *target])          # no prior review: runs
    assert r.exit_code == 0, r.output
    assert len(ran) == 1 and "warning" not in r.output
    sha12 = SR.spec_sha256(_spec(repo))[:12]
    r = run_cli(cfg, monkeypatch, ["spec", "review", *target])
    assert r.exit_code != 0 and len(ran) == 1                            # no reviewer ran
    assert (f"SPEC-0001 was already reviewed (approve, {sha12}); a revised spec is queued "
            "without a second review. Pass --force for an extra round") in r.output
    r = run_cli(cfg, monkeypatch, ["spec", "review", *target, "--force"])
    assert r.exit_code == 0, r.output
    assert len(ran) == 2
    assert f"warning: extra review round for SPEC-0001 (previous: approve, {sha12})" in r.output
    assert f"warning: extra review round for SPEC-0001 (previous: approve, {sha12})" in r.stderr


def _spy(monkeypatch, tmp_path):
    from wt.agentic.runner import FakeRunner
    ran = []
    inner = FakeRunner(script(tmp_path, [_review_entry(), _review_entry()], "spy.json"))

    class Spy:
        def run(self, req):
            ran.append(req)
            return inner.run(req)

    monkeypatch.setattr(SR, "select_runner", lambda cfg: Spy())
    return ran


def test_spec_review_ignores_foreign_repo_review(tmp_path, monkeypatch):
    repo, cfg = _setup(tmp_path, [])
    other = tmp_path / "other"
    other.mkdir()
    subprocess.run(["git", "-C", str(other), "init", "-q", "-b", "main"], check=True)
    p = write_review(cfg, _spec(repo), "SPEC-0001", "revise", sha="a" * 64)
    data = json.loads(p.read_text())
    data["repo_root"] = str(other)                                      # another repo's SPEC-0001
    p.write_text(json.dumps(data))
    ran = _spy(monkeypatch, tmp_path)
    r = run_cli(cfg, monkeypatch, ["spec", "review", "SPEC-0001"])
    assert r.exit_code == 0, r.output
    assert len(ran) == 1 and "warning" not in r.output and "already reviewed" not in r.output


def test_spec_review_still_blocks_after_review_from_removed_worktree(tmp_path, monkeypatch):
    repo, cfg = _setup(tmp_path, [])
    wt = tmp_path / "linked"
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", "--detach", str(wt)], check=True)
    p = write_review(cfg, _spec(repo), "SPEC-0001", "revise", sha="a" * 64)
    data = json.loads(p.read_text())
    data["repo_root"] = str(wt)
    p.write_text(json.dumps(data))
    subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", str(wt)], check=True)
    ran = _spy(monkeypatch, tmp_path)
    r = run_cli(cfg, monkeypatch, ["spec", "review", "SPEC-0001"])
    assert r.exit_code != 0 and not ran and "already reviewed (revise, aaaaaaaaaaaa)" in r.output


def test_review_accepts_a_matrix_cell_naming_a_project_only_profile(tmp_path, monkeypatch):
    repo, cfg = _setup(tmp_path, [_review_entry()])                  # SPEC-0237
    cfg.setdefault("agentic", {})["profile_matrix"] = {"high-end": {"m": "careful"}}
    prof = repo / ".wt" / "profiles"
    prof.mkdir(parents=True, exist_ok=True)
    (prof / "careful.yaml").write_text("schema: wt.profile.v1\nextends: balanced\n")
    r = run_cli(cfg, monkeypatch, ["spec", "review", "SPEC-0001", "--json"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.stdout)["profile"] == "careful"


def test_spec_review_run_json_records_profile(tmp_path, monkeypatch):            # SPEC-0238
    repo, cfg = _setup(tmp_path, [_review_entry()])
    prof = repo / ".wt" / "profiles"
    prof.mkdir(parents=True, exist_ok=True)
    (prof / "balanced.yaml").write_text(
        "schema: wt.profile.v1\npersonas: {spec_reviewer: {model: opus}}\n")
    r = run_cli(cfg, monkeypatch, ["spec", "review", "SPEC-0001", "--json"])
    assert r.exit_code == 0, r.output
    (run_json,) = (Path(cfg["data_dir"])).rglob("spec-reviews/*/run.json")
    rec = json.loads(run_json.read_text())
    assert rec["profile"] == "balanced"
    assert rec["profile_sources"] == ["builtin:balanced.yaml",
                                      "config.yaml:agentic.profiles.balanced",
                                      str(prof / "balanced.yaml")]


# ---- SPEC-0278: --profile, agentic.spec_review_profile, the profile recorded ---------------

def _reviewer(name):
    return PR.persona_settings(PR.resolve_profile({}, name), "spec_reviewer")


def test_review_profile_name_precedence():
    ag = {"default_profile": "frugal", "spec_review_profile": "careful"}
    assert SR.review_profile_name({"agentic": ag}, "thorough") == "thorough"
    assert SR.review_profile_name({"agentic": ag}) == "careful"
    assert SR.review_profile_name({"agentic": {**ag, "spec_review_profile": None}}) == "frugal"
    assert SR.review_profile_name({"agentic": {"default_profile": None}}) == "balanced"
    assert SR.review_profile_name({}) == "balanced"


def test_cli_review_profile_flag_sets_model(tmp_path, monkeypatch):
    repo, cfg = _setup(tmp_path, [_review_entry()])
    ran = _spy(monkeypatch, tmp_path)
    r = run_cli(cfg, monkeypatch, ["spec", "review", "SPEC-0001", "--profile", "thorough"])
    assert r.exit_code == 0, r.output
    (req,) = ran
    want = _reviewer("thorough")
    assert want["model"] != _reviewer("balanced")["model"]
    assert (req.model, req.effort, req.max_budget_usd) == (
        want["model"], want["effort"], float(want["max_budget_usd"]))
    assert req.profile == "thorough"
    r = run_cli(cfg, monkeypatch, ["spec", "review", "--path", str(_spec(repo)), "--force",
                                   "--profile", "frugal"])
    assert r.exit_code == 0, r.output
    assert ran[1].profile == "frugal" and ran[1].model == _reviewer("frugal")["model"]


def test_cli_review_config_default_profile(tmp_path, monkeypatch):
    repo, cfg = _setup(tmp_path, [_review_entry()])
    cfg["agentic"]["spec_review_profile"] = "thorough"
    ran = _spy(monkeypatch, tmp_path)
    r = run_cli(cfg, monkeypatch, ["spec", "review", "SPEC-0001"])
    assert r.exit_code == 0, r.output
    assert ran[0].profile == "thorough" and ran[0].model == _reviewer("thorough")["model"]
    assert SR.latest_review(cfg, "SPEC-0001")["reviewer_profile"] == "thorough"


def test_cli_review_unknown_profile_no_journal(tmp_path, monkeypatch):
    repo, cfg = _setup(tmp_path, [_review_entry()])
    ran = _spy(monkeypatch, tmp_path)
    r = run_cli(cfg, monkeypatch, ["spec", "review", "SPEC-0001", "--profile", "nope"])
    assert r.exit_code != 0 and not ran
    assert "unknown profile 'nope'" in r.output and "thorough" in r.output
    assert not SR.journal_path(cfg).exists() and SR.latest_review(cfg, "SPEC-0001") is None
    assert "reviewing " not in r.output


def test_review_payload_and_journal_record_profile(tmp_path, monkeypatch):
    repo, cfg = _setup(tmp_path, [_review_entry()])
    r = run_cli(cfg, monkeypatch, ["spec", "review", "SPEC-0001", "--json",
                                   "--profile", "thorough"])
    assert r.exit_code == 0, r.output
    out = json.loads(r.stdout)
    assert out["reviewer_profile"] == "thorough" and out["profile"] == "balanced"   # the queue pick
    side = SR.load_review(cfg, "SPEC-0001", SR.spec_sha256(_spec(repo)))
    assert side["reviewer_profile"] == "thorough" and "profile" not in side
    start = next(j for j in SR.read_journal(cfg) if j["kind"] == "start")
    assert start["reviewer_profile"] == "thorough" and start["model"] == _reviewer("thorough")["model"]


def test_reviewing_line_names_profile_and_model(tmp_path, monkeypatch):
    repo, cfg = _setup(tmp_path, [_review_entry()])
    r = run_cli(cfg, monkeypatch, ["spec", "review", "SPEC-0001", "--profile", "thorough"])
    assert r.exit_code == 0, r.output
    line = next(ln for ln in r.output.splitlines() if ln.startswith("reviewing "))
    assert line.endswith(f"· profile thorough ({_reviewer('thorough')['model']})")


def test_cli_review_profile_with_show_usage_error(tmp_path, monkeypatch):
    repo, cfg = _setup(tmp_path, [])
    r = run_cli(cfg, monkeypatch, ["spec", "review", "SPEC-0001", "--show",
                                   "--profile", "thorough"])
    assert r.exit_code == 2 and "--show and --profile cannot be combined" in r.output


# ---- SPEC-0295: findings carry a required suggested_fix ----------------------------------------

def _fixed(**kw):
    return {"verdict": "revise", **SIZED,
            "findings": [{"severity": "nit", "section": "Context", "title": "wording",
                          "detail": "d", "suggested_fix": "reword it", **kw}],
            "suggested_splits": []}


@pytest.mark.parametrize("fix", ["", "   "])
def test_blank_suggested_fix_is_rejected_and_stores_nothing(tmp_path, monkeypatch, fix):
    repo, cfg = _setup(tmp_path, [_review_entry(_fixed(suggested_fix=fix))])
    r = run_cli(cfg, monkeypatch, ["spec", "review", "SPEC-0001"])
    assert r.exit_code != 0 and "wording" in r.output and "suggested_fix" in r.output
    assert not (Path(cfg["data_dir"]) / "agentic" / "reviews").exists()


@pytest.mark.parametrize("fix", ["", "   ", None, 3])
def test_check_verdict_names_the_finding_without_a_usable_fix(fix):
    with pytest.raises(ValueError, match="wording"):
        SR._check_verdict("SPEC-0001", _fixed(suggested_fix=fix))


def test_a_missing_suggested_fix_is_a_failed_run(tmp_path, monkeypatch):
    v = _fixed()
    del v["findings"][0]["suggested_fix"]
    repo, cfg = _setup(tmp_path, [_review_entry(v)])
    r = run_cli(cfg, monkeypatch, ["spec", "review", "SPEC-0001"])
    assert r.exit_code != 0
    assert not (Path(cfg["data_dir"]) / "agentic" / "reviews").exists()


def test_review_stores_and_prints_fix_and_acceptance(tmp_path, monkeypatch):
    repo, cfg = _setup(tmp_path, [_review_entry(_fixed(acceptance="no 'wordy' left")),
                                  _review_entry(_fixed(acceptance="no 'wordy' left"))])
    r = run_cli(cfg, monkeypatch, ["spec", "review", "SPEC-0001"])
    assert r.exit_code == 0, r.output
    assert "Fix: reword it" in r.output and "Done when: no 'wordy' left" in r.output
    r = run_cli(cfg, monkeypatch, ["spec", "review", "SPEC-0001", "--show"])
    assert "Fix: reword it" in r.output and "Done when:" in r.output
    r = run_cli(cfg, monkeypatch, ["spec", "review", "SPEC-0001", "--show", "--json"])
    f = json.loads(r.stdout)["review"]["findings"][0]
    assert (f["suggested_fix"], f["acceptance"]) == ("reword it", "no 'wordy' left")


def test_stored_review_without_the_new_keys_still_renders(tmp_path, monkeypatch):
    repo, cfg = _setup(tmp_path, [])
    spec = repo / "docs" / "specs" / "0001-trivial.md"
    old = [{"severity": "major", "section": "Design", "title": "old gap", "detail": "d"}]
    write_review(cfg, spec, "SPEC-0001", "revise", findings=old, **SIZED)
    r = run_cli(cfg, monkeypatch, ["spec", "review", "SPEC-0001", "--show"])
    assert r.exit_code == 0 and "old gap" in r.output and "Fix:" not in r.output
    r = run_cli(cfg, monkeypatch, ["spec", "reviews"])
    assert r.exit_code == 0, r.output
    brief = SR.revision_brief({"spec_sha256": "ab" * 32, "findings": old})
    assert brief["must_fix"][0]["detail"] == "d"


def test_revision_brief_carries_the_fix_for_every_finding_nits_included():
    fs = [{"severity": "major", "title": "gap", "detail": "d", "suggested_fix": "add X",
           "acceptance": "X is stated"},
          {"severity": "nit", "title": "wording", "suggested_fix": "say Y"}]
    brief = SR.revision_brief({"spec_sha256": "ab" * 32, "findings": fs})
    assert [m["detail"] for m in brief["must_fix"]] == ["d\nFix: add X\nDone when: X is stated",
                                                        "Fix: say Y"]
    assert "Fix: say Y" in PS.render_pending_brief(brief)
