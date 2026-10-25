"""SPEC-0188: `wt spec review` (fake runner), the sidecar, the `wt queue add` review gate, the
spec_reviewer persona/profile defaults, and the linter's placeholder rule."""
import json
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
           "findings": [{"severity": "nit", "section": "Context", "title": "wording"}],
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


def test_review_pretty_output(tmp_path, monkeypatch):
    split = {"verdict": "split", **SIZED, "findings": [{"severity": "major", "section": "Design",
                                               "title": "too big", "detail": "x"}],
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
                                                                 "title": "t"}]}),
    _review_entry({"verdict": "approve", "findings": []}),                 # SPEC-0219: unsized
    _review_entry({"verdict": "approve", "size": "xl", "tone": "critical", "rationale": "r",
                   "findings": []}),
    _review_entry({"verdict": "revise", "size": "xl", "tone": "critical", "rationale": "r",
                   "findings": [{"severity": "major", "title": "t"}]}),
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
    for name, prof in PR.BUILTIN_PROFILES.items():
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
    assert "Ship `feature.txt`." in req.prompt and "{{" not in req.prompt
    assert "queue" not in req.system_append
    assert req.run_dir.parent.name == "spec-reviews"


# ---- the queue gate ------------------------------------------------------------------------

def _add(cfg, monkeypatch, *extra):
    return run_cli(cfg, monkeypatch, ["queue", "add", "SPEC-0001", "--json", *extra])


def test_queue_add_refuses_without_approving_review(tmp_path, monkeypatch):
    repo, cfg = _setup(tmp_path, [])
    want = "SPEC-0001 has no approving review for its current text; run wt spec review SPEC-0001"
    r = _add(cfg, monkeypatch)                                        # no review at all
    assert r.exit_code != 0 and want in r.output and "--force" in r.output
    write_review(cfg, _spec(repo), "SPEC-0001", verdict="revise")           # not approving
    r = _add(cfg, monkeypatch)
    assert r.exit_code != 0 and want in r.output
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
    assert r.exit_code != 0 and "no approving review" in r.output


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
    out = json.loads(r.output)
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
    r = run_cli(cfg, monkeypatch, ["spec", "review", "SPEC-0001", "--json"])
    out = json.loads(r.output)
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
