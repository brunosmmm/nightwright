"""SPEC-0221: the close rebase hands a non-generated conflict to a `conflict_resolver` persona
run (a step within CLOSING) instead of parking. Offline: FakeRunner entries drive a real git
conflict in a tmp repo (`pre_shell` / `shell` run git in the campaign worktree)."""
import json
import threading
from pathlib import Path

import pytest

from test_agentic_e2e import (_git, _write, added, fake_gh, impl_done, make_cfg, make_repo,
                              review, run_cli, script, triage)
from wt.agentic import personas as PS
from wt.agentic import profiles as PR
from wt.agentic import project as P
from wt.agentic import queue as Q
from wt.agentic import store as S
from wt.agentic.runner import FakeRunner

APP_BASE = "a\nb\nc\nd\ne\n"
APP_BRANCH = "a\nb\nc-branch\nd\ne\n"
APP_MAIN = "a\nb\nc-main\nd\ne\n"
APP_MERGED = "a\nb\nc-branch c-main\nd\ne\n"
OTHER_SPEC = """---
id: SPEC-9001
title: "Other spec"
status: done
owner: t
created: 2026-10-25
---

## Decision

Line three of the app is now c-main.

## Acceptance criteria

- [x] app line 3 says c-main
"""


def _verdict(status="resolved", summary="kept both sides", questions=(), strategy="both"):
    v = {"status": status, "summary": summary,
         "resolutions": [{"path": "src/app.py", "strategy": strategy, "how": "joined"}]
         if status == "resolved" else [],
         "questions": list(questions)}
    return v


CONTINUE = "git add -A && GIT_EDITOR=true git rebase --continue"


def resolver(shell=(CONTINUE,), edits=True, pre=("git rebase $WT_REBASE_TARGET || true",),
             verdict=None, extra_edits=(), **more):
    """A resolver FakeRunner entry: rebase, write the merged file, continue."""
    entry = {"persona": "conflict_resolver", "pre_shell": list(pre),
             "edits": ([{"path": "src/app.py", "write": APP_MERGED}] if edits else [])
             + list(extra_edits),
             "shell": list(shell), "verdict": verdict or _verdict(), "cost_usd": 0.4}
    entry.update(more)
    return entry


def gave_up(summary="intents contradict", questions=("which wins?",)):
    return resolver(shell=(), edits=False, verdict=_verdict("gave_up", summary, questions))


def impl_with_app(**kw):
    entry = impl_done(**kw)
    entry["edits"].append({"path": "src/app.py", "write": APP_BRANCH})
    return entry


class World:
    def __init__(self, tmp_path, monkeypatch, entries, *, profile=None, resolver_cfg=None,
                 regenerate=False, test_gate="test -f feature.txt"):
        self.tmp_path = tmp_path
        self.repo = make_repo(tmp_path, test_gate=test_gate)
        yml = self.repo / ".wt" / "project.yaml"
        text = yml.read_text()
        if resolver_cfg is not None:
            text = text.replace("  remote: origin\n", f"  remote: origin\n  resolver: {resolver_cfg}\n")
        if regenerate:
            text = text.replace("  remote: origin\n", "  remote: origin\n  regenerate:\n"
                                "    - {path: gen.txt, cmd: 'echo regenerated > gen.txt'}\n")
        yml.write_text(text)
        _write(self.repo, "src/app.py", APP_BASE)
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "app")
        self.script = script(tmp_path, entries)
        self.cfg = make_cfg(tmp_path, self.repo, self.script, profile=profile)
        self.runner = FakeRunner(self.script)
        self.monkeypatch = monkeypatch
        self.cid = added(self.cfg, monkeypatch)

    def tick(self, **kw):
        return Q.tick(self.cfg, runner=self.runner, **kw)

    def st(self):
        return S.read_state(self.cfg, self.cid)

    def drive(self, pred, limit=40):
        for _ in range(limit):
            if pred(self.st()):
                return self.st()
            self.tick()
        raise AssertionError(f"never reached: state {self.st()['state']}, "
                             f"closing {self.st().get('closing')}")

    def open_and_conflict(self, main_edit=True):
        self.tick()                                               # admit + open
        assert self.st()["state"] == "IMPLEMENTING"
        if main_edit:
            self.main_conflict()

    def main_conflict(self, content=APP_MAIN):
        _write(self.repo, "src/app.py", content)
        _write(self.repo, "docs/specs/9001-other.md", OTHER_SPEC)
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "SPEC-9001: change app line 3")

    def dispatched(self):
        return self.drive(lambda s: (s.get("closing") or {}).get("resolve") is not None)

    def attempted(self, n):
        return self.drive(lambda s: s["state"] != "CLOSING"
                          or int((s.get("closing") or {}).get("resolve_attempts") or 0) >= n)

    def ledger(self, *types):
        return S.read_ledger(self.cfg, self.cid, types=list(types) or None)

    def findings(self, title):
        return [f for f in self.ledger("finding") if f["title"] == title]

    def head(self):
        return _git(self.st()["repo"]["worktree"], "rev-parse", "HEAD")

    def run_ids(self):
        return [e["run_id"] for e in self.ledger("run.start") if e["persona"] == "conflict_resolver"]


def basic(tmp_path, monkeypatch, *resolvers, **kw):
    return World(tmp_path, monkeypatch, [impl_with_app(), review(), *resolvers], **kw)


# ---- dispatch and merge -------------------------------------------------------------------

def test_conflict_dispatches_resolver_then_merges(tmp_path, monkeypatch):
    w = basic(tmp_path, monkeypatch, resolver())
    w.open_and_conflict()
    st = w.dispatched()
    assert st["state"] == "CLOSING" and st["park"] is None
    rs = st["closing"]["resolve"]
    assert rs["conflicts"] == ["src/app.py"] and rs["branch"] == st["repo"]["branch"]
    assert rs["pre_head"] == w.head() == _git(st["repo"]["worktree"], "rev-parse", st["repo"]["branch"])
    assert rs["target_sha"] == _git(w.repo, "rev-parse", "main")
    f = w.findings("rebase conflict; resolver dispatched")
    assert len(f) == 1 and f[0]["severity"] == "info" and f[0]["gate"] == "rebase"
    assert f[0]["files"] == ["src/app.py"] and f[0]["persona"] == "close"
    assert [e["type"] for e in w.ledger("park")] == []
    assert w.run_ids() == []                                       # the run is the next tick's

    w.drive(lambda s: s["state"] in ("MERGED", "PARKED", "TRIAGING"))
    st = w.st()
    assert st["state"] == "MERGED", (st["state"], st.get("park"))
    assert w.run_ids() == ["003-conflict_resolver"]
    assert [e["run_id"] for e in w.ledger("run.end") if e["persona"] == "conflict_resolver"] \
        == ["003-conflict_resolver"]
    ev, = w.ledger("rebase_resolved")
    assert ev["run_id"] == "003-conflict_resolver" and ev["conflicts"] == ["src/app.py"]
    assert ev["resolutions"] == [{"path": "src/app.py", "strategy": "both", "how": "joined"}]
    assert ev["target_sha"] == rs["target_sha"] and ev["summary"] == "kept both sides"
    assert (w.repo / "src" / "app.py").read_text() == APP_MERGED
    assert st["cost"]["by_persona"]["conflict_resolver"] == 0.4
    reasons = [t["reason"] for t in w.ledger("transition") if t["from"] == "CLOSING"]
    assert reasons[-1] == "closed"
    from wt.agentic import schemas as SC
    assert all(SC.ledger_problems(e) == [] for e in w.ledger())


def test_resolver_prompt_context(tmp_path, monkeypatch):
    w = basic(tmp_path, monkeypatch, resolver())
    w.open_and_conflict()
    st = w.dispatched()
    cfg = w.cfg
    from wt.agentic import project as PJ
    pcfg = PJ.project_config(cfg, st["project"])
    req = PS.build_request(cfg, PR.resolve_profile(cfg, st.get("profile")), pcfg, st,
                           "conflict_resolver", run_dir=tmp_path / "runs" / "x")
    target = _git(w.repo, "rev-parse", "main")
    assert req.env["WT_REBASE_TARGET"] == target
    p = req.prompt
    assert "src/app.py" in p
    assert "+c-branch" in p and "-c" in p                          # campaign hunk
    assert "+c-main" in p                                          # base hunk
    assert "SPEC-9001: change app line 3" in p
    assert "Line three of the app is now c-main." in p             # the landed spec's text
    assert target in p and "git rebase " + target in p
    assert "regenerate" in p.lower() and "{{" not in p
    assert req.permission_mode == "bypassPermissions" and not req.read_only
    # the hidden `campaign prompt` renders the same values
    r = run_cli(cfg, monkeypatch, ["campaign", "prompt", w.cid, "conflict_resolver"])
    assert r.exit_code == 0, r.output
    assert "+c-main" in r.output and "SPEC-9001: change app line 3" in r.output


def test_resolver_context_uses_current_fork_point(tmp_path, monkeypatch):
    w = basic(tmp_path, monkeypatch, resolver())
    w.tick()                                                       # open
    base_open = w.st()["repo"]["base_sha_at_open"]
    _write(w.repo, "src/app.py", "a-main-A\nb\nc\nd\ne\n")
    _git(w.repo, "add", "-A")
    _git(w.repo, "commit", "-q", "-m", "absorbed base commit A")
    a_sha = _git(w.repo, "rev-parse", "HEAD")
    w.drive(lambda s: s["state"] == "REVIEWING")
    wt = w.st()["repo"]["worktree"]
    _git(wt, "rebase", "main")                                     # an earlier close rebase
    w.main_conflict("a-main-A\nb\nc-main\nd\ne\n")
    st = w.dispatched()
    rs = st["closing"]["resolve"]
    assert rs["fork"] == _git(wt, "merge-base", rs["pre_head"], rs["target_sha"]) == a_sha
    assert rs["fork"] != base_open
    ctx = PS.resolve_context(st, rs, P.project_config(w.cfg, st["project"]))
    assert "+c-branch" in ctx["campaign_diff"]
    for d in (ctx["campaign_diff"], ctx["base_diff"]):       # context lines may show it; changes not
        assert not [ln for ln in d.splitlines() if ln.startswith(("+a-main-A", "-a-main-A", "-a"))
                    and not ln.startswith("---")]
    assert "+c-main" in ctx["base_diff"]
    assert "absorbed base commit A" not in (ctx["landed"] or "")


# ---- what the close does with the result ----------------------------------------------------

def test_resolved_but_regate_red_goes_triaging(tmp_path, monkeypatch):
    fixed = {"persona": "implementer", "edits": [{"path": "src/app.py", "write": APP_MERGED}],
             "verdict": {"status": "done", "summary": "fixed", "changed_files": [],
                         "tests_run": [], "ac_status": [], "blockers": [], "followup_ideas": []},
             "cost_usd": 0.3}
    bad = APP_MERGED.replace("c-branch c-main", "c-branch BAD c-main")
    w = World(tmp_path, monkeypatch,
              [impl_with_app(), review(),
               resolver(edits=False, extra_edits=[{"path": "src/app.py", "write": bad}]),
               triage("fix"), fixed, review()],
              test_gate="test -f feature.txt && ! grep -q BAD src/app.py")
    w.open_and_conflict()
    w.drive(lambda s: s["state"] == "TRIAGING")
    assert [t["reason"] for t in w.ledger("transition")][-1] == "regate_failed"
    assert w.ledger("rebase_resolved")
    w.drive(lambda s: s["state"] in ("MERGED", "PARKED"))
    assert w.st()["state"] == "MERGED", w.st().get("park")
    assert "BAD" not in (w.repo / "src" / "app.py").read_text()
    assert len(w.ledger("rebase_resolved")) == 1                    # the fix loop needed no second one


def test_resolver_edits_spec_triages_spec_changed(tmp_path, monkeypatch):
    w = basic(tmp_path, monkeypatch, resolver(
        extra_edits=[{"path": "docs/specs/0001-trivial.md", "append": "\nA new governing line.\n"}]))
    w.open_and_conflict()
    w.drive(lambda s: s["state"] in ("TRIAGING", "MERGED", "PARKED"))
    st = w.st()
    assert st["state"] == "TRIAGING"
    assert st["last_gate"]["results"][0]["name"] == "spec-changed"
    assert w.ledger("rebase_resolved")
    assert (w.repo / "src" / "app.py").read_text() == APP_MAIN         # nothing merged


# ---- bounded attempts, verification and restore ----------------------------------------------

def test_gave_up_twice_parks_rebase_conflict(tmp_path, monkeypatch):
    w = basic(tmp_path, monkeypatch, gave_up("first"), gave_up("cannot decide c"), resolver())
    w.open_and_conflict()
    pre = w.dispatched()["closing"]["resolve"]["pre_head"]
    st = w.attempted(1)
    assert st["state"] == "CLOSING" and st["closing"]["resolve"]["pre_head"] == pre
    assert st["closing"]["resolve_attempts"] == 1
    assert w.head() == pre
    f = w.findings("conflict resolver did not resolve")
    assert len(f) == 1 and f[0]["severity"] == "major" and "which wins?" in f[0]["detail"]
    st = w.drive(lambda s: s["state"] == "PARKED")
    assert st["park"]["reason"] == "rebase_conflict" and st["park"]["resume_to"] == "CLOSING"
    assert "src/app.py" in st["park"]["detail"] and "cannot decide c" in st["park"]["detail"]
    assert "2 attempts" in st["park"]["detail"]
    wt = st["repo"]["worktree"]
    assert w.head() == pre and _git(wt, "symbolic-ref", "HEAD") == f"refs/heads/{st['repo']['branch']}"
    assert _git(wt, "status", "--porcelain") == ""
    assert (w.repo / "src" / "app.py").read_text() == APP_MAIN
    # a resume resets the budget; the third entry resolves
    r = run_cli(w.cfg, monkeypatch, ["queue", "resume", w.cid, "--yes"])
    assert r.exit_code == 0, r.output
    assert w.st()["closing"] == {"attempts": 0}
    w.drive(lambda s: s["state"] in ("MERGED", "PARKED"))
    assert w.st()["state"] == "MERGED"
    assert len(w.ledger("rebase_resolved")) == 1


FAILS = {
    "rebase_left_running": (dict(shell=(), edits=False), "HEAD is not attached"),
    "dirty_tree": (dict(shell=(CONTINUE + " && echo junk >> feature.txt",)), "dirty"),
    "rebase_aborted": (dict(pre=(), shell=(), edits=False), "not an ancestor"),
    "detached_head": (dict(shell=(CONTINUE + " && git checkout -q --detach",)), "not attached"),
    "other_branch": (dict(shell=(CONTINUE + " && git checkout -q -b other",)), "not attached"),
    "dropped_commit": (dict(shell=("git rebase --skip",), edits=False), "commit count"),
    "swapped_commit": (dict(shell=("git rebase --skip && git commit -q --allow-empty -m swapped",),
                            edits=False), "dropped or added"),
    "extra_commit": (dict(shell=(CONTINUE + " && git commit -q --allow-empty -m extra",)),
                     "commit count"),
}


@pytest.mark.parametrize("name", sorted(FAILS))
def test_unverified_resolved_is_restored(tmp_path, monkeypatch, name):
    kw, check = FAILS[name]
    w = basic(tmp_path, monkeypatch, resolver(**kw), gave_up())
    w.open_and_conflict()
    pre = w.dispatched()["closing"]["resolve"]["pre_head"]
    st = w.attempted(1)
    wt, branch = st["repo"]["worktree"], st["repo"]["branch"]
    assert st["state"] == "CLOSING" and st["closing"]["resolve"]["pre_head"] == pre
    assert not w.ledger("rebase_resolved")
    f, = w.findings("conflict resolver did not resolve")
    assert f["severity"] == "major" and check in f["detail"], f["detail"]
    assert _git(wt, "symbolic-ref", "HEAD") == f"refs/heads/{branch}"
    assert _git(wt, "rev-parse", "HEAD") == _git(wt, "rev-parse", branch) == pre
    assert _git(wt, "status", "--porcelain") == ""
    for d in ("rebase-merge", "rebase-apply"):
        assert not Path(wt, _git(wt, "rev-parse", "--git-path", d)).exists()
    w.tick()                                                          # the second attempt (gave_up)
    st = w.st()
    assert st["state"] == "PARKED" and st["park"]["reason"] == "rebase_conflict"
    assert w.head() == pre


def test_interrupted_resolver_not_counted(tmp_path, monkeypatch):
    w = basic(tmp_path, monkeypatch, resolver(sleep_s=0.4), resolver())
    w.open_and_conflict()
    pre = w.dispatched()["closing"]["resolve"]["pre_head"]
    stop = threading.Event()
    timer = threading.Timer(0.15, stop.set)
    timer.start()
    try:
        w.tick(stop=stop)
    finally:
        timer.cancel()
    st = w.st()
    assert st["state"] == "CLOSING" and st["closing"]["resolve"]["pre_head"] == pre
    assert not st["closing"].get("resolve_attempts")
    assert w.head() == pre and not w.ledger("rebase_resolved")
    assert _git(st["repo"]["worktree"], "status", "--porcelain") == ""
    assert [e["interrupted"] for e in w.ledger("run.end")
            if e["persona"] == "conflict_resolver"] == [True]
    w.drive(lambda s: s["state"] in ("MERGED", "PARKED"))
    assert w.st()["state"] == "MERGED"
    assert len(w.run_ids()) == 2


def test_resolver_opt_out_parks(tmp_path, monkeypatch):
    w = basic(tmp_path, monkeypatch, resolver(), resolver_cfg="false")
    w.open_and_conflict()
    st = w.drive(lambda s: s["state"] == "PARKED")
    assert st["park"]["reason"] == "rebase_conflict" and st["park"]["resume_to"] == "CLOSING"
    assert "src/app.py" in st["park"]["detail"] and "resolver" not in st["park"]["detail"]
    assert w.run_ids() == [] and not w.findings("rebase conflict; resolver dispatched")


def test_resolver_cost_cap_parks(tmp_path, monkeypatch):
    prof = {"campaign_cap_usd": 2.0, "personas": {
        "implementer": {"max_budget_usd": 0.5}, "conformance_reviewer": {"max_budget_usd": 0.3},
        "conflict_resolver": {"max_budget_usd": 2.0}}}
    w = basic(tmp_path, monkeypatch, resolver(), profile=prof)
    w.open_and_conflict()
    st = w.drive(lambda s: s["state"] == "PARKED")
    assert st["park"]["reason"] == "cost_cap" and st["park"]["resume_to"] == "CLOSING"
    assert w.run_ids() == []
    assert "closing" not in st or not st["closing"].get("resolve")


def test_generated_only_conflict_no_resolver(tmp_path, monkeypatch):
    entry = impl_with_app()
    entry["edits"].append({"path": "gen.txt", "write": "branch\n"})
    w = World(tmp_path, monkeypatch, [entry, review()], regenerate=True)
    w.tick()
    _write(w.repo, "gen.txt", "main\n")
    _git(w.repo, "add", "-A")
    _git(w.repo, "commit", "-q", "-m", "gen on main")
    w.drive(lambda s: s["state"] in ("MERGED", "PARKED"))
    assert w.st()["state"] == "MERGED"
    assert w.run_ids() == [] and not w.ledger("rebase_resolved")
    assert (w.repo / "gen.txt").read_text() == "regenerated\n"
    assert any(f["title"] == "generated files regenerated during rebase"
               for f in w.ledger("finding"))


def test_attempt_budget_spans_resolved_runs_in_one_closing_entry(tmp_path, monkeypatch):
    """Two verified resolutions (the base moved and conflicted again in between) use the
    whole budget: the third conflict parks without a third run."""
    second = resolver(edits=False, extra_edits=[
        {"path": "src/app.py", "write": "a\nb\nc-branch c-main c-main2\nd\ne\n"}])
    w = basic(tmp_path, monkeypatch, resolver(), second, resolver())
    w.open_and_conflict()
    w.drive(lambda s: len(w.ledger("rebase_resolved")) == 1)
    assert w.st()["state"] == "CLOSING" and not w.st()["closing"].get("resolve")
    w.main_conflict("a\nb\nc-main2\nd\ne\n")
    w.drive(lambda s: len(w.ledger("rebase_resolved")) == 2)
    assert w.st()["closing"]["resolve_attempts"] == 2
    w.main_conflict("a\nb\nc-main3\nd\ne\n")
    st = w.drive(lambda s: s["state"] == "PARKED")
    assert st["park"]["reason"] == "rebase_conflict" and "2 attempts" in st["park"]["detail"]
    assert "kept both sides" in st["park"]["detail"]
    assert len(w.run_ids()) == 2
