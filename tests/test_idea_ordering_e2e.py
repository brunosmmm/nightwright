"""SPEC-0423 real walk-through: a real org ideas file, a real epic spec in a real git fixture repo,
real CLI invocations (`spec seed-children`, `queue add --epic`), real `queue.tick()`, and a
scripted spec writer whose output the real validation accepts or rejects. Only the persona's
output is scripted and the prerequisite's MERGED transition is applied through
`queue._transition` (the full implement/gate/merge pipeline has its own tests)."""
import json
import subprocess
import sys

from test_agentic_e2e import _git, _write, make_cfg, run_cli, script
from test_agentic_idea_campaign import idea
from test_agentic_spec_writer import _spec_md, make_project, spec_written
from nightwright import org as ORG
from nightwright.agentic import queue as Q
from nightwright.agentic import store as S
import pytest

pytestmark = pytest.mark.integration

EPIC_BREAKDOWN = ("## Breakdown / sub-specs\n\n"
                  "- First child — the base\n"
                  "- Second child — builds on it (depends_on child 1)\n"
                  "- Third child — needs both (depends_on child 1 and child 2)\n")


def _setup(tmp_path, monkeypatch):
    repo = make_project(tmp_path)
    _write(repo, "docs/specs/0100-the-epic.md", _spec_md(
        100, None, title="The big epic", status="accepted", kind="epic",
        extra_body=EPIC_BREAKDOWN))
    subprocess.run([sys.executable, "tools/spec_lint.py", "--write-ledger"], cwd=repo, check=True,
                   capture_output=True)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "epic")
    cfg = make_cfg(tmp_path, repo, script(tmp_path, []))
    return cfg, repo


def _by_title(cfg):
    ORG.invalidate_tasks_cache()
    return {t.heading: t for t in ORG.load_tasks(cfg) if t.is_idea}


def test_epic_after_graph_gates_real_ticks_and_writer_validation(tmp_path, monkeypatch):
    cfg, repo = _setup(tmp_path, monkeypatch)
    # 1. seed-children stamps :AFTER: in the real ideas file
    r = run_cli(cfg, monkeypatch, ["spec", "seed-children", "SPEC-0100"])
    assert r.exit_code == 0, r.output
    ideas = _by_title(cfg)
    ids = [ideas[h].properties["ID"] for h in ("First child", "Second child", "Third child")]
    i1, i2, i3 = ids
    assert (ideas["First child"].properties.get("AFTER") or "") == ""
    assert ideas["Second child"].properties["AFTER"] == i1
    assert ideas["Third child"].properties["AFTER"] == f"{i1} {i2}"
    text = open(cfg["org_ideas_file"]).read()
    assert f":AFTER: {i1} {i2}" in text

    # 2. queue add --epic: three QUEUED campaigns in order, `after` set from :AFTER:
    r = run_cli(cfg, monkeypatch, ["queue", "add", "--epic", "SPEC-0100", "--yes", "--json"])
    assert r.exit_code == 0, r.output
    out = json.loads(r.output)
    assert [it["id"] for it in out["items"]] == [i1, i2, i3]
    camps = {c["idea"]["id"]: c for c in S.list_campaigns(cfg)}
    assert "after" not in camps[i1]
    assert camps[i2]["after"] == [{"ref": i1, "kind": "idea"}]
    assert camps[i3]["after"] == [{"ref": i1, "kind": "idea"}, {"ref": i2, "kind": "idea"}]
    rows = {r["idea_id"]: r for r in Q.backlog_view(cfg)}
    assert rows[i1]["ready"] and not rows[i2]["ready"] and not rows[i3]["ready"]
    assert rows[i2]["waiting_on"] == [i1] and rows[i3]["waiting_on"] == [i1, i2]
    r = run_cli(cfg, monkeypatch, ["queue", "backlog", "--json"])
    assert r.exit_code == 0 and i1 in r.output

    # 3. a real tick admits only the first child
    script(tmp_path, [spec_written(101, i1, epic="SPEC-0100", slug="first-child")])
    Q.tick(cfg)
    states = {i: S.read_state(cfg, c["id"])["state"] for i, c in camps.items()}
    assert states[i1] != "QUEUED" and states[i2] == "QUEUED" and states[i3] == "QUEUED"
    for _ in range(3):                      # let child 1 write its spec
        Q.tick(cfg)
    st1 = S.read_state(cfg, camps[i1]["id"])
    assert st1["spec"].get("id") and S.read_state(cfg, camps[i2]["id"])["state"] == "QUEUED"
    spec1 = st1["spec"]["id"]

    # 4. child 1 merges; only then is child 2 admitted (child 3 still waits for child 2)
    Q._transition(cfg, st1, "MERGED", "fixture merge")
    ready2, _ = Q.backlog_readiness(cfg, S.read_state(cfg, camps[i2]["id"]))
    ready3, blocked3 = Q.backlog_readiness(cfg, S.read_state(cfg, camps[i3]["id"]))
    assert ready2 and not ready3 and blocked3[0].startswith(f"waiting on {i2} (after:")
    n2 = int(spec1[5:]) + 1
    bad = spec_written(n2, i2, epic="SPEC-0100", slug="second-child")
    good = spec_written(n2, i2, epic="SPEC-0100", slug="second-child",
                        fm={"depends_on": f"[{spec1}]"})
    script(tmp_path, [bad, good])
    Q.tick(cfg)                                                   # admits child 2
    st2 = S.read_state(cfg, camps[i2]["id"])
    assert st2["state"] == "SPEC_WRITING"
    Q.tick(cfg)                                                   # the bad write is rejected
    st2 = S.read_state(cfg, camps[i2]["id"])
    assert st2["spec_write"]["must_depend_on"] == [{"spec_id": spec1, "ref": i1}]
    problems = st2["pending_brief"]["write_problems"]
    want = f"front matter: depends_on must include {spec1} (prerequisite {i1})"
    assert want in problems and st2["counters"]["spec_write_retries"] == 1
    for _ in range(2):                                            # the retry cites it and passes
        Q.tick(cfg)
    st2 = S.read_state(cfg, camps[i2]["id"])
    assert st2["spec"]["id"]                      # written: the run went on to spec review (unscripted)
    assert len(S.read_ledger(cfg, camps[i2]["id"], types=["spec_written"])) == 1
    wt = st2["repo"]["worktree"]
    spec_text = open(f"{wt}/{st2['spec']['rel_path']}").read()
    assert f"depends_on: [{spec1}]" in spec_text
    prompt = (S.runs_dir(cfg, camps[i2]["id"]) / "001-spec_writer" / "prompt.md").read_text()
    assert f"`depends_on` must include: {spec1} (prerequisite {i1})" in prompt
