"""SPEC-0221: the close rebase hands a non-generated conflict to a `conflict_resolver` persona
run (a step within CLOSING) instead of parking. Offline: FakeRunner entries drive a real git
conflict in a tmp repo (`pre_shell` / `shell` run git in the campaign worktree)."""
import ast
import json
import subprocess
import threading
from pathlib import Path

import pytest

import waits
from test_agentic_e2e import (_git, _write, added, fake_gh, impl_done, make_cfg, make_repo,
                              review, run_cli, script, triage)
from nightwright.agentic import personas as PS
from nightwright.agentic import gitops as G
from nightwright.agentic import profiles as PR
from nightwright.agentic import project as P
from nightwright.agentic import queue as Q
from nightwright.agentic import store as S
from nightwright.agentic.runner import FakeRunner


def _no_entry(closing: dict) -> dict:
    """`closing` without its SPEC-0260 `entered_at` stamp, which must be present."""
    assert closing.get("entered_at"), closing
    return {k: v for k, v in closing.items() if k != "entered_at"}


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


def resolver(shell=(CONTINUE,), edits=True, pre=("git rebase $NW_REBASE_TARGET || true",),
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
                 regenerate=False, test_gate="test -f feature.txt", allowance=None):
        self.tmp_path = tmp_path
        self.repo = make_repo(tmp_path, test_gate=test_gate)
        yml = self.repo / ".nw" / "project.yaml"
        text = yml.read_text()
        if resolver_cfg is not None:
            text = text.replace("  remote: origin\n", f"  remote: origin\n  resolver: {resolver_cfg}\n")
        if allowance is not None:
            text = text.replace("  remote: origin\n",
                                f"  remote: origin\n  resolver_allowance_usd: {allowance}\n")
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


class _HoldingRunner(FakeRunner):
    """SPEC-0322: the first `conflict_resolver` run announces it started, then blocks until
    `stop` is set, so a stop is always delivered while that run is in flight (no wall-clock race).
    Later resolver runs and every other persona go straight through."""

    def __init__(self, script_path, stop):
        super().__init__(script_path)
        self.stop = stop
        self.started = threading.Event()
        self._held = False

    def run(self, req, *, hooks=None):
        if req.persona == "conflict_resolver" and not self._held:
            self._held = True
            self.started.set()
            # `_do_resolve` catches this as a crashed attempt; the test's asserts surface it.
            assert waits.until(self.stop.is_set), "stop never set (watcher did not run)"
        return super().run(req, hooks=hooks)


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
    from nightwright.agentic import schemas as SC
    assert all(SC.ledger_problems(e) == [] for e in w.ledger())


def test_resolver_prompt_context(tmp_path, monkeypatch):
    w = basic(tmp_path, monkeypatch, resolver())
    w.open_and_conflict()
    st = w.dispatched()
    cfg = w.cfg
    from nightwright.agentic import project as PJ
    pcfg = PJ.project_config(cfg, st["project"])
    req = PS.build_request(cfg, PR.resolve_profile(cfg, st.get("profile")), pcfg, st,
                           "conflict_resolver", run_dir=tmp_path / "runs" / "x")
    target = _git(w.repo, "rev-parse", "main")
    assert req.env["NW_REBASE_TARGET"] == target
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

def test_two_failures_park_rebase_conflict(tmp_path, monkeypatch):
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
    assert _no_entry(w.st()["closing"]) == {"attempts": 0}
    w.drive(lambda s: s["state"] in ("MERGED", "PARKED"))
    assert w.st()["state"] == "MERGED"
    assert len(w.ledger("rebase_resolved")) == 1


def _hook(body):
    return ("H=$(git rev-parse --git-path hooks/pre-commit) && mkdir -p $(dirname $H) && "
            f"printf '#!/bin/sh\\n{body}\\n' > $H && chmod +x $H")


_UNMERGED = ("B=$(git rev-parse HEAD:src/app.py) && printf "
             "'100644 %s 1\\tsrc/app.py\\n100644 %s 2\\tsrc/app.py\\n100644 %s 3\\tsrc/app.py\\n' "
             "$B $B $B | git update-index --index-info")
_MERGE_NO_COMMIT = (
    "I=$(mktemp -u) && GIT_INDEX_FILE=$I git read-tree HEAD~1 && "
    "GIT_INDEX_FILE=$I git update-index --add --cacheinfo "
    "100644,$(echo s | git hash-object -w --stdin),side.txt && "
    "S=$(git commit-tree $(GIT_INDEX_FILE=$I git write-tree) -p HEAD~1 -m side) && "
    "rm -f $I && git merge -q --no-commit --no-ff $S")

FAILS = {
    "rebase_left_running": (dict(shell=(), edits=False), "HEAD is not attached"),
    "rebase_aborted": (dict(pre=(), shell=(), edits=False), "not an ancestor"),
    "detached_head": (dict(shell=(CONTINUE + " && git checkout -q --detach",)), "not attached"),
    "other_branch": (dict(shell=(CONTINUE + " && git checkout -q -b other",)), "not attached"),
    "dropped_commit": (dict(shell=("git rebase --skip",), edits=False),
                       "is missing after the resolution"),
    "superseded_undeclared": (dict(shell=("git rebase --skip",), edits=False),
                              "is missing after the resolution"),
    "superseded_unknown_sha_declared": (
        dict(shell=("git rebase --skip",), edits=False,
             verdict={**_verdict(), "dropped_commits": [{"sha": "deadbeef", "reason": "r"}]}),
        "is missing after the resolution"),
    "swapped_commit": (dict(shell=("git rebase --skip && git commit -q --allow-empty -m swapped",),
                            edits=False), "is missing after the resolution"),
    "reverting_extra_commit": (dict(shell=(CONTINUE + " && git revert --no-edit HEAD",)),
                               "reverts campaign commit"),
    "unmerged_no_rebase": (dict(shell=(CONTINUE + "; git cherry-pick main || true",)),
                           "unmerged paths remain"),
    # unmerged index entries with no sequencer state (e.g. a conflicted `git apply --3way`)
    "unmerged_no_sequencer": (dict(shell=(CONTINUE + " && " + _UNMERGED,)),
                              "unmerged paths remain"),
    # a clean `git merge --no-commit`: MERGE_HEAD and a staged change, no unmerged paths
    "sequencer_only": (dict(shell=(CONTINUE + " && " + _MERGE_NO_COMMIT,)), "dirty"),
    "hook_rejects_commit": (dict(shell=(CONTINUE + " && " + _hook("exit 1")
                                        + " && echo junk >> feature.txt",)), "dirty"),
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


def _precommit_world(tmp_path, monkeypatch, resolver_entry, wt_script, main_script):
    """A campaign with one commit made before the implementer runs, so the close sees two:
    `wt_script` runs in the worktree, `main_script` in the main repo (before the conflict)."""
    w = basic(tmp_path, monkeypatch, resolver_entry)
    w.tick()
    assert w.st()["state"] == "IMPLEMENTING"
    subprocess.run(["sh", "-ce", wt_script], cwd=w.st()["repo"]["worktree"], check=True,
                   capture_output=True)
    subprocess.run(["sh", "-ce", main_script], cwd=w.repo, check=True, capture_output=True)
    w.main_conflict()
    return w


def _merged(w):
    w.drive(lambda s: s["state"] in ("MERGED", "PARKED"))
    assert w.st()["state"] == "MERGED", (w.st()["state"], w.st().get("park"))
    return w.ledger("rebase_resolved")


def test_extra_commit_verified(tmp_path, monkeypatch):
    w = basic(tmp_path, monkeypatch, resolver(
        shell=(CONTINUE + " && echo port > port.txt && git add port.txt "
               "&& git commit -q -m 'port commit'",)))
    w.open_and_conflict()
    ev, = _merged(w)
    assert [c["subject"] for c in ev["extra_commits"]] == ["port commit"]
    assert len(ev["extra_commits"][0]["sha"]) == 40
    assert ev["absorbed_commits"] == [] and ev["resolver_commit"] is None
    assert (w.repo / "port.txt").read_text() == "port\n"


ADD_PRE = "echo x > pre.txt && git add pre.txt && git commit -q -m 'pre commit'"
ADD_PRE_MAIN = ADD_PRE.replace("'pre commit'", "'landed on main'")   # else the same commit object


@pytest.mark.parametrize("variant", ["patch_id", "context", "empty"])
def test_emptied_commit_verified(tmp_path, monkeypatch, variant):
    """A campaign commit the target already holds (or one empty from the start) is dropped by
    the rebase: absorbed into HEAD, not a failure."""
    pre = ("git rebase $NW_REBASE_TARGET || true",)
    wt_script, main_script = ADD_PRE, ADD_PRE_MAIN
    if variant == "context":                # a different patch-id: the target commit adds more
        main_script = "echo y > other.txt && git add other.txt && " + ADD_PRE_MAIN
    elif variant == "empty":
        wt_script, main_script = "git commit -q --allow-empty -m 'pre commit'", "true"
        pre = ("git rebase --no-keep-empty $NW_REBASE_TARGET || true",)
    w = _precommit_world(tmp_path, monkeypatch, resolver(pre=pre), wt_script, main_script)
    ev, = _merged(w)
    assert [c["subject"] for c in ev["absorbed_commits"]] == ["pre commit"]
    assert ev["extra_commits"] == []
    assert "absorbed" in _render_resolved(ev)


def test_rebase_resolved_text_extra_and_absorbed_and_old_events():
    base = {"conflicts": ["a", "b"], "resolutions": [{"strategy": "both"}]}
    assert _render_resolved(base) == "rebase resolved: 2 paths (both\u00d71)"
    assert _render_resolved({**base, "extra_commits": [], "absorbed_commits": []}) \
        == "rebase resolved: 2 paths (both\u00d71)"
    assert _render_resolved({**base, "extra_commits": [{}], "absorbed_commits": [{}, {}]}) \
        == "rebase resolved: 2 paths (both\u00d71); +1 extra, 2 absorbed"


def _render_resolved(ev):
    from nightwright import render
    return render.rebase_resolved_text(ev)


@pytest.mark.parametrize("summary", ["kept both sides", ""])
def test_staged_changes_committed_by_runner(tmp_path, monkeypatch, summary):
    w = basic(tmp_path, monkeypatch, resolver(
        shell=(CONTINUE + " && echo staged > new.txt && git add new.txt && echo edit >> feature.txt"
               " && echo o > app.py.orig",), verdict=_verdict(summary=summary)))
    w.open_and_conflict()
    w.drive(lambda s: len(w.ledger("rebase_resolved")) == 1)
    wt = w.st()["repo"]["worktree"]
    ev, = w.ledger("rebase_resolved")
    head = _git(wt, "rev-parse", "HEAD")
    assert ev["resolver_commit"] == head and ev["head"] == head
    msg = _git(wt, "log", "-1", "--format=%B")
    first = msg.splitlines()[0]
    assert first == (f"nw({w.cid}): conflict resolver run 003"
                     + (f" \u2014 {summary}" if summary else ""))
    assert msg.rstrip().endswith("Co-Authored-By: nightwright test <nightwright@test>")
    assert set(_git(wt, "show", "--name-only", "--format=", "HEAD").split()) \
        == {"new.txt", "feature.txt"}
    assert not (Path(wt) / "app.py.orig").exists()
    assert "app.py.orig" not in _git(wt, "ls-tree", "-r", "--name-only", "HEAD")
    assert ev["extra_commits"] and ev["extra_commits"][0]["sha"] == head
    assert _merged(w)


@pytest.mark.parametrize("name", ["unmerged_no_rebase", "unmerged_no_sequencer", "sequencer_only"])
def test_unmerged_no_rebase_commits_nothing(tmp_path, monkeypatch, name):
    """Each runner-commit guard alone: unmerged paths (with and without a sequencer) and a
    sequencer operation with no unmerged paths."""
    calls = []
    real = G.commit_tracked
    monkeypatch.setattr(G, "commit_tracked", lambda *a, **kw: calls.append(a) or real(*a, **kw))
    kw, check = FAILS[name]
    w = basic(tmp_path, monkeypatch, resolver(**kw), gave_up())
    w.open_and_conflict()
    w.dispatched()
    w.attempted(1)
    assert calls == []
    f, = w.findings("conflict resolver did not resolve")
    assert check in f["detail"]


def test_interrupted_resolver_not_counted(tmp_path, monkeypatch):
    w = basic(tmp_path, monkeypatch, resolver(), resolver())
    w.open_and_conflict()
    pre = w.dispatched()["closing"]["resolve"]["pre_head"]
    stop = threading.Event()
    w.runner = _HoldingRunner(w.script, stop)
    # SPEC-0322: stop only once the resolver run is in flight; the run holds until `stop` is set.
    t = threading.Thread(target=lambda: waits.until(
        lambda: w.runner.started.is_set() or stop.is_set()) and stop.set())
    t.start()
    try:
        w.tick(stop=stop)
    finally:
        stop.set()
        waits.join(t)
    assert w.runner.started.is_set()
    st = w.st()
    assert st["state"] == "CLOSING" and st["closing"]["resolve"]["pre_head"] == pre
    assert not st["closing"].get("resolve_attempts")
    assert not w.ledger("rebase_resolved")
    _assert_restored(w, st, pre)
    assert [e["interrupted"] for e in w.ledger("run.end")
            if e["persona"] == "conflict_resolver"] == [True]
    w.drive(lambda s: s["state"] in ("MERGED", "PARKED"))
    assert w.st()["state"] == "MERGED"
    assert len(w.run_ids()) == 2


def test_interrupted_resolver_test_has_no_fixed_timer():
    """SPEC-0322: the fixed-delay race (Timer + sleep_s) must not return to the interrupted test."""
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    fn, = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
           and n.name == "test_interrupted_resolver_not_counted"]
    for n in ast.walk(fn):
        if isinstance(n, ast.Call):
            callee = n.func
            name = callee.attr if isinstance(callee, ast.Attribute) else getattr(callee, "id", "")
            assert name != "Timer", "fixed threading.Timer in the interrupted-resolver test"
        assert not (isinstance(n, ast.keyword) and n.arg == "sleep_s")
        assert not (isinstance(n, ast.Constant) and n.value == "sleep_s")


def test_resolver_opt_out_still_parks(tmp_path, monkeypatch):
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
    w = basic(tmp_path, monkeypatch, resolver(), profile=prof, allowance=0)   # SPEC-0303: none
    w.open_and_conflict()
    st = w.drive(lambda s: s["state"] == "PARKED")
    assert st["park"]["reason"] == "cost_cap" and st["park"]["resume_to"] == "CLOSING"
    assert "conflict_resolver" in st["park"]["detail"]          # parked at the resolve step
    assert len(w.findings("rebase conflict; resolver dispatched")) == 1
    assert w.run_ids() == []
    # the resume resets `closing`; the next close re-detects the conflict and parks again
    r = run_cli(w.cfg, monkeypatch, ["queue", "resume", w.cid, "--yes"])
    assert r.exit_code == 0, r.output
    assert _no_entry(w.st()["closing"]) == {"attempts": 0}
    w.dispatched()
    st = w.drive(lambda s: s["state"] == "PARKED")
    assert st["park"]["reason"] == "cost_cap" and "conflict_resolver" in st["park"]["detail"]
    assert len(w.findings("rebase conflict; resolver dispatched")) == 2 and w.run_ids() == []


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


def _chain(k):
    """The resolver entry for the k-th conflict of a base that moves after every resolution."""
    if k == 1:
        return resolver()
    merged = "a\nb\nc-branch c-main " + " ".join(f"c-main{i}" for i in range(2, k + 1)) + "\nd\ne\n"
    return resolver(edits=False, extra_edits=[{"path": "src/app.py", "write": merged}])


def _move_base_after(w, k):
    """Wait for the k-th verified resolution, then move main so that the branch conflicts again."""
    w.drive(lambda s: len(w.ledger("rebase_resolved")) == k)
    assert w.st()["state"] == "CLOSING" and not w.st()["closing"].get("resolve")
    w.main_conflict(f"a\nb\nc-main{k + 1}\nd\ne\n")


def test_verified_runs_then_base_moving_park(tmp_path, monkeypatch):
    """Verified runs do not use the failure budget: the base is busy, not the resolver failing.
    `MAX_RESOLVE_RUNS` verified runs then park `base_moving`, which resumes to CLOSING."""
    w = basic(tmp_path, monkeypatch, *[_chain(k) for k in range(1, 6)])
    w.open_and_conflict()
    for k in range(1, Q.MAX_RESOLVE_RUNS + 1):
        _move_base_after(w, k)
    st = w.drive(lambda s: s["state"] == "PARKED")
    assert len(w.ledger("rebase_resolved")) == 4 == len(w.run_ids())
    assert st["park"]["reason"] == "base_moving" and st["park"]["resume_to"] == "CLOSING"
    assert "4 verified resolutions" in st["park"]["detail"]
    assert "earlier attempt failed" not in st["park"]["detail"]
    assert not w.findings("conflict resolver did not resolve")


def test_verified_then_failed_gets_retry(tmp_path, monkeypatch):
    w = basic(tmp_path, monkeypatch, _chain(1), gave_up("cannot decide"), _chain(2))
    w.open_and_conflict()
    _move_base_after(w, 1)
    st = w.drive(lambda s: (s.get("closing") or {}).get("resolve_failed"))
    assert st["state"] == "CLOSING" and st["closing"]["resolve"]
    w.drive(lambda s: s["state"] in ("MERGED", "PARKED"))
    assert w.st()["state"] == "MERGED"
    assert len(w.run_ids()) == 3 and len(w.ledger("rebase_resolved")) == 2


def test_run_cap_applies_to_retry(tmp_path, monkeypatch):
    """Verified x3, then one failure: the failed-run retry path does not pass through
    `_dispatch_resolver`, so `_apply_resolve` itself holds `MAX_RESOLVE_RUNS`."""
    w = basic(tmp_path, monkeypatch, _chain(1), _chain(2), _chain(3), gave_up("cannot decide"),
              _chain(4))
    w.open_and_conflict()
    for k in range(1, 4):
        _move_base_after(w, k)
    st = w.drive(lambda s: s["state"] == "PARKED")
    assert st["park"]["reason"] == "rebase_conflict" and st["park"]["resume_to"] == "CLOSING"
    assert "last: cannot decide" in st["park"]["detail"] and "4 attempts" in st["park"]["detail"]
    assert len(w.run_ids()) == 4                                  # no 5th run


def test_earlier_failure_noted_in_base_moving_detail(tmp_path, monkeypatch):
    w = basic(tmp_path, monkeypatch, _chain(1), gave_up("cannot decide"), _chain(2), _chain(3),
              _chain(4))
    w.open_and_conflict()
    _move_base_after(w, 1)
    _move_base_after(w, 2)
    _move_base_after(w, 3)
    st = w.drive(lambda s: s["state"] == "PARKED")
    assert len(w.run_ids()) == 4
    assert st["park"]["reason"] == "base_moving"
    assert "after 3 verified resolutions; an earlier attempt failed: cannot decide" \
        in st["park"]["detail"]


def test_failure_budget_ignores_verified_runs(tmp_path, monkeypatch):
    """verified, failed, failed: two failures park rebase_conflict whatever ran before."""
    w = basic(tmp_path, monkeypatch, _chain(1), gave_up("one"), gave_up("two"), _chain(2))
    w.open_and_conflict()
    _move_base_after(w, 1)
    st = w.drive(lambda s: s["state"] == "PARKED")
    assert st["park"]["reason"] == "rebase_conflict" and "last: two" in st["park"]["detail"]
    assert len(w.run_ids()) == 3


def _cap_profile(cap):
    return {"campaign_cap_usd": cap, "personas": {
        "implementer": {"max_budget_usd": 0.5}, "conformance_reviewer": {"max_budget_usd": 0.3},
        "conflict_resolver": {"max_budget_usd": 2.0}}}


def test_resolve_on_allowance_records_finding(tmp_path, monkeypatch):
    """cap 2.0: spent < 2.0 but spent + 2.0 > 2.0, so only the close allowance lets the run start."""
    title = "resolver ran on the close allowance"
    w = basic(tmp_path, monkeypatch, resolver(), profile=_cap_profile(2.0))
    w.open_and_conflict()
    cap = w.st()["cost"]["cap_usd"]
    assert cap == 2.0
    w.drive(lambda s: s["state"] in ("MERGED", "PARKED") or w.findings(title))
    w.drive(lambda s: s["state"] in ("MERGED", "PARKED"))
    assert w.st()["state"] == "MERGED" and len(w.run_ids()) == 1
    (f,) = w.findings(title)
    assert f["severity"] == "info" and f["gate"] == "rebase" and f["files"] == ["src/app.py"]
    assert w.st()["cost"]["cap_usd"] == cap                       # the cap itself is unchanged
    assert not [e for e in w.ledger("park") if e["reason"] == "cost_cap"]
    # companion: total + budget within the plain cap, so no finding
    (tmp_path / "plain").mkdir()
    w2 = basic(tmp_path / "plain", monkeypatch, resolver(), profile=_cap_profile(20.0))
    w2.open_and_conflict()
    w2.drive(lambda s: s["state"] in ("MERGED", "PARKED"))
    assert w2.st()["state"] == "MERGED" and not w2.findings(title)


def test_resolve_beyond_the_allowance_parks_cost_cap(tmp_path, monkeypatch):
    w = basic(tmp_path, monkeypatch, resolver(), profile=_cap_profile(2.0), allowance=0.5)
    w.open_and_conflict()
    st = w.drive(lambda s: s["state"] == "PARKED")
    assert st["park"]["reason"] == "cost_cap" and w.run_ids() == []
    assert not w.findings("resolver ran on the close allowance")


# ---- a failed run (non-zero exit, malformed verdict, crash) and a failed restore ---------------

class CrashingRunner(FakeRunner):
    """Starts the rebase like a resolver would, then raises on its first conflict_resolver run."""
    crashed = False

    def run(self, req):
        if req.persona == "conflict_resolver" and not self.crashed:
            type(self).crashed = self.crashed = True
            self._shell(req, ["git rebase $NW_REBASE_TARGET || true"])
            raise RuntimeError("resolver blew up")
        return super().run(req)


def _assert_restored(w, st, pre):
    wt, branch = st["repo"]["worktree"], st["repo"]["branch"]
    assert st["closing"]["resolve"]["pre_head"] == pre
    assert _git(wt, "symbolic-ref", "HEAD") == f"refs/heads/{branch}"
    assert _git(wt, "rev-parse", "HEAD") == _git(wt, "rev-parse", branch) == pre
    assert _git(wt, "status", "--porcelain") == ""
    for d in ("rebase-merge", "rebase-apply"):
        assert not Path(wt, _git(wt, "rev-parse", "--git-path", d)).exists()


@pytest.mark.parametrize("how", ["exit_code", "malformed", "crash"])
def test_failed_resolver_run_is_restored_and_counted(tmp_path, monkeypatch, how):
    failing = resolver(shell=(), edits=False, exit_code=1) if how == "exit_code" \
        else resolver(shell=(), edits=False, malformed=True)
    w = basic(tmp_path, monkeypatch, *([] if how == "crash" else [failing]), gave_up())
    if how == "crash":
        CrashingRunner.crashed = False
        w.runner = CrashingRunner(w.script)
    w.open_and_conflict()
    pre = w.dispatched()["closing"]["resolve"]["pre_head"]
    st = w.attempted(1)
    assert st["state"] == "CLOSING" and st["closing"]["resolve_attempts"] == 1
    assert not w.ledger("rebase_resolved")
    _assert_restored(w, st, pre)
    f, = w.findings("conflict resolver did not resolve")
    assert f["severity"] == "major"
    if how == "crash":
        assert "resolver blew up" in f["detail"]
    end, = [e for e in w.ledger("run.end") if e["persona"] == "conflict_resolver"]
    if how == "exit_code":
        assert end["exit_code"] == 1 and end["is_error"] is True
    else:
        assert end["verdict_ok"] is False
    if how == "crash":
        assert end["exit_code"] == -1 and end["is_error"] is True
    w.tick()                                                          # the second attempt (gave_up)
    st = w.st()
    assert st["state"] == "PARKED" and st["park"]["reason"] == "rebase_conflict"
    assert st["park"]["resume_to"] == "CLOSING"
    assert len(w.run_ids()) == 2 and len(w.findings("conflict resolver did not resolve")) == 2
    assert w.head() == pre


def test_restore_failure_parks_open_failed_keeping_the_run(tmp_path, monkeypatch):
    w = basic(tmp_path, monkeypatch, gave_up(), gave_up())
    w.open_and_conflict()
    w.dispatched()
    real, calls = G.restore_branch, []

    def flaky(*a, **kw):
        calls.append(a)
        if len(calls) > 1:                  # the restore before the run works; the one after fails
            raise G.GitError(["git", "restore"], 1, "cannot restore")
        return real(*a, **kw)

    monkeypatch.setattr(G, "restore_branch", flaky)
    w.tick()
    st = w.st()
    assert st["state"] == "PARKED" and st["park"]["reason"] == "open_failed"
    assert st["park"]["resume_to"] == "CLOSING" and "cannot restore" in st["park"]["detail"]
    assert w.run_ids() == ["003-conflict_resolver"]
    assert [e["run_id"] for e in w.ledger("run.end") if e["persona"] == "conflict_resolver"] \
        == ["003-conflict_resolver"]


def test_pre_run_restore_failure_still_ends_the_run(tmp_path, monkeypatch):
    """The first restore (before the persona) raising must not leave a run.start without a
    run.end, and must not charge a run that never happened."""
    w = basic(tmp_path, monkeypatch, gave_up())
    w.open_and_conflict()
    w.dispatched()
    before = w.st()["cost"]["total_usd"]

    def broken(*a, **kw):
        raise G.GitError(["git", "restore"], 1, "index.lock exists")

    monkeypatch.setattr(G, "restore_branch", broken)
    w.tick()
    st = w.st()
    assert st["state"] == "PARKED" and st["park"]["reason"] == "open_failed"
    assert st["park"]["resume_to"] == "CLOSING" and "index.lock" in st["park"]["detail"]
    ends = [e for e in w.ledger("run.end") if e["persona"] == "conflict_resolver"]
    assert [e["run_id"] for e in ends] == w.run_ids() == ["003-conflict_resolver"]
    assert ends[0]["is_error"] is True and ends[0]["exit_code"] == -1
    assert st["cost"]["total_usd"] == before


def test_verified_resolver_untracked_leftovers_are_not_merged(tmp_path, monkeypatch):
    w = basic(tmp_path, monkeypatch, resolver(
        shell=(CONTINUE + " && echo x > app.py.orig && mkdir -p scratch && echo y > scratch/m.txt",)))
    w.open_and_conflict()
    w.drive(lambda s: len(w.ledger("rebase_resolved")) == 1)
    assert not (Path(w.st()["repo"]["worktree"]) / "app.py.orig").exists()
    w.drive(lambda s: s["state"] in ("MERGED", "PARKED"))
    assert w.st()["state"] == "MERGED"
    assert (w.repo / "src" / "app.py").read_text() == APP_MERGED
    assert not (w.repo / "app.py.orig").exists() and not (w.repo / "scratch").exists()
    assert "close-time snapshot" not in _git(w.repo, "log", "--format=%s")


def test_attempts_survive_a_base_moved_redo(tmp_path, monkeypatch):
    """The re-gate of the resolved tree moves `main` once: the close redoes (`base_moved`),
    and the finish phase's `closing` write keeps `resolve_attempts` and the resolver budget."""
    flag = tmp_path / "moved.flag"
    gate = ("test -f feature.txt && { if grep -q c-main src/app.py && ! test -f %s; then "
            "touch %s; git -C %s commit -q --allow-empty -m moved; fi; }"
            % (flag, flag, tmp_path / "repo"))
    w = World(tmp_path, monkeypatch, [impl_with_app(), review(), resolver()], test_gate=gate)
    w.open_and_conflict()
    seen, real = [], Q.CL.finish_close

    def spy(cfg, state, pcfg, prepared, **kw):
        seen.append(dict(state.get("closing") or {}))          # what the finish phase wrote
        return real(cfg, state, pcfg, prepared, **kw)

    monkeypatch.setattr(Q.CL, "finish_close", spy)
    w.drive(lambda s: s["state"] in ("MERGED", "PARKED"))
    assert flag.exists(), "the re-gate never ran on the resolved tree"
    assert w.st()["state"] == "MERGED"
    assert w.findings("base moved during re-gate; redoing close")
    assert len(seen) == 2 and [c["attempts"] for c in seen] == [1, 2], seen
    assert all(c["resolve_attempts"] == 1 for c in seen), seen
    assert len(w.run_ids()) == 1


# ---- dispatch fallbacks and the spent-budget detail ---------------------------------------------

def test_missing_merge_base_parks_rebase_conflict(tmp_path, monkeypatch):
    w = basic(tmp_path, monkeypatch, resolver())

    def broken(*a, **kw):
        raise G.GitError(["git", "merge-base"], 1, "no merge base")

    w.open_and_conflict()
    monkeypatch.setattr(G, "merge_base", broken)
    st = w.drive(lambda s: s["state"] == "PARKED")
    assert st["park"]["reason"] == "rebase_conflict" and st["park"]["resume_to"] == "CLOSING"
    assert "src/app.py" in st["park"]["detail"] and "resolver" not in st["park"]["detail"]
    assert w.run_ids() == [] and not w.findings("rebase conflict; resolver dispatched")
    assert not (st.get("closing") or {}).get("resolve")


@pytest.mark.parametrize("pre_head,target_sha", [("", "abc"), ("abc", ""), (None, None)])
def test_dispatch_needs_pre_head_and_target_sha(tmp_path, pre_head, target_sha):
    class Prepared:
        pass

    prepared = Prepared()
    if pre_head is not None:
        prepared.pre_head = pre_head
    res = Q.StepResult("c", Q.Step("close"), "CLOSING", prepared=prepared)
    state = {"id": "c", "repo": {"worktree": str(tmp_path), "branch": "nw/c"}, "closing": {"attempts": 0}}
    park = {"reason": "rebase_conflict", "detail": "conflicts: a.py", "conflicts": ["a.py"],
            "target": "main", "target_sha": target_sha}
    events = []
    ctx = type("Ctx", (), {"project": lambda self, st: {}})()
    assert Q._dispatch_resolver(ctx, state, res, park, events) == "no"
    assert events == [] and "resolve" not in state["closing"]


def test_spent_resolver_park_detail_cases():
    park = {"reason": "rebase_conflict", "detail": "conflicts: a.py", "resume_to": "CLOSING"}
    # no resolver run in this CLOSING entry (opt-out): unchanged
    assert Q._spent_resolver_park({"closing": {"attempts": 0}}, park) == park
    assert Q._spent_resolver_park({}, park) == park
    # the last verdict is some other persona's, but the resolver failed: its own failure is named
    st = {"closing": {"resolve_attempts": 2, "resolve_failed": {"n": 1, "last": "cannot decide"}},
          "last_verdict": {"persona": "implementer", "verdict": {"summary": "other"}}}
    d = Q._spent_resolver_park(st, park)["detail"]
    assert d.endswith("(resolver: 2 attempts, last: cannot decide)") and "other" not in d
    # all verified: no failure to report (SPEC-0303: that is `base_moving`, not this park)
    st = {"closing": {"resolve_attempts": 2}, "last_verdict": {
        "persona": "conflict_resolver", "verdict": {"summary": "kept both sides"}}}
    assert Q._spent_resolver_park(st, park) == park
    # attempt 1 failed, attempt 2 verified, then the base moved: the failure is not "the last"
    st = {"closing": {"resolve_attempts": 2, "resolve_last_verified": True,
                      "resolve_failed": {"n": 1, "last": "cannot decide"}}}
    d = Q._spent_resolver_park(st, park)["detail"]
    assert "the last was verified" in d and "an earlier attempt failed: cannot decide" in d
    assert "last: cannot decide" not in d


# ---- gitops: the verification and restore helpers on their own --------------------------------

@pytest.fixture
def grepo(tmp_path):
    r = tmp_path / "g"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@t")
    _git(r, "config", "user.name", "t")
    _write(r, "f.txt", "1\n2\n3\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "base")
    fork = _git(r, "rev-parse", "HEAD")
    _git(r, "checkout", "-q", "-b", "nw/x")
    _write(r, "f.txt", "1\n2\nbranch\n")
    _git(r, "commit", "-qam", "branch edit")
    pre = _git(r, "rev-parse", "HEAD")
    _git(r, "checkout", "-q", "main")
    _write(r, "f.txt", "1\n2\nmain\n")
    _git(r, "commit", "-qam", "main edit")
    target = _git(r, "rev-parse", "HEAD")
    _git(r, "checkout", "-q", "nw/x")
    return r, fork, pre, target


def test_verify_resolution_rebase_in_progress_fires_alone(grepo):
    r, fork, pre, target = grepo
    # a conflicted rebase stops with the branch detached: attached check passes only after
    # re-attaching, so exercise the in-progress check by faking the rebase dir on an attached HEAD
    gitdir = Path(r, _git(r, "rev-parse", "--git-path", "rebase-merge"))
    gitdir.mkdir(parents=True)
    try:
        assert G.rebase_in_progress(r)
        assert G.verify_resolution(r, "nw/x", target, fork, pre).failed == "a rebase is still in progress"
    finally:
        gitdir.rmdir()


def test_verify_resolution_unmerged_paths_fire_alone(grepo):
    r, fork, pre, target = grepo
    subprocess.run(["git", "merge", "main"], cwd=r, capture_output=True)       # conflicts, no rebase
    assert _git(r, "ls-files", "-u")
    assert G.verify_resolution(r, "nw/x", target, fork, pre).failed == "unmerged paths remain"


def test_restore_branch_cleans_untracked_leftovers(grepo):
    r, fork, pre, target = grepo
    _write(r, "stray.orig", "x\n")
    _write(r, "d/inner.txt", "y\n")
    G.restore_branch(r, "nw/x", pre)
    assert not (r / "stray.orig").exists() and not (r / "d").exists()
    assert _git(r, "status", "--porcelain") == ""


# ---- verify_resolution accounting (SPEC-0252) ------------------------------------------------

def _sh(r, script):
    subprocess.run(["sh", "-ce", script], cwd=r, check=True, capture_output=True, text=True)


def vrepo(tmp_path, branch, target, resolve):
    """A repo with `main` (base + `target` commits) and `nw/x` (base + `branch` commits), then
    `resolve` run on nw/x. Returns (repo, fork, pre_head, target_sha)."""
    r = tmp_path / "v"
    r.mkdir()
    _sh(r, "git init -q -b main && git config user.email t@t && git config user.name t "
           "&& echo 1 > f.txt && git add -A && git commit -q -m base")
    fork = _git(r, "rev-parse", "HEAD")
    _sh(r, "git checkout -q -b nw/x\n" + branch)
    pre = _git(r, "rev-parse", "HEAD")
    _sh(r, "git checkout -q main\n" + target)
    tgt = _git(r, "rev-parse", "HEAD")
    _sh(r, "git checkout -q nw/x\n" + resolve)
    return r, fork, pre, tgt


def _write_cmd(name, data="x", binary=False):
    return f"printf '\\000\\001{data}' > {name}" if binary else f"echo {data} > {name}"


def _commit_file(name, data="x", msg=None, binary=False):
    return (f"{_write_cmd(name, data, binary)} && git add {name} "
            f"&& git commit -q -m '{msg or name}'\n")


def _check(r, fork, pre, tgt):
    return G.verify_resolution(r, "nw/x", tgt, fork, pre)


def test_verify_resolution_paired_commit_passes_with_nothing_extra(tmp_path):
    r, fork, pre, tgt = vrepo(tmp_path, _commit_file("a.txt"), _commit_file("m.txt"),
                              "git rebase main -q")
    c = _check(r, fork, pre, tgt)
    assert c.failed is None and c.extra == [] and c.absorbed == []


def test_verify_resolution_cherry_marked_commit_is_absorbed(tmp_path):
    r, fork, pre, tgt = vrepo(tmp_path, _commit_file("a.txt", msg="mine"),
                              _commit_file("a.txt", msg="landed"), "git rebase main -q")
    c = _check(r, fork, pre, tgt)
    assert c.failed is None and [a["subject"] for a in c.absorbed] == ["mine"]
    assert c.absorbed[0]["sha"] == _git(r, "rev-parse", pre)


def test_verify_resolution_empty_commit_is_absorbed(tmp_path):
    r, fork, pre, tgt = vrepo(tmp_path, "git commit -q --allow-empty -m nothing\n",
                              _commit_file("m.txt"), "git rebase --no-keep-empty main -q")
    c = _check(r, fork, pre, tgt)
    assert c.failed is None and [a["subject"] for a in c.absorbed] == ["nothing"]


@pytest.mark.parametrize("binary", [False, True])
def test_verify_resolution_reverse_apply_absorbs_a_commit_with_another_patch_id(tmp_path, binary):
    """The target lands the same file plus another: no patch-id match, but HEAD holds the change.
    `--binary` is what lets a binary file reverse-apply."""
    name = "a.dat" if binary else "a.txt"
    write = _write_cmd(name, binary=binary)
    r, fork, pre, tgt = vrepo(
        tmp_path, _commit_file(name, msg="mine", binary=binary),
        f"echo o > o.txt && {write} && git add -A && git commit -q -m landed\n",
        "git rebase main -q")
    c = _check(r, fork, pre, tgt)
    assert c.failed is None and [a["subject"] for a in c.absorbed] == ["mine"]
    assert _git(r, "cherry", tgt, pre).startswith("+")          # patch-ids differ


def test_verify_resolution_missing_commit_fails_and_names_it(tmp_path):
    r, fork, pre, tgt = vrepo(tmp_path, _commit_file("a.txt", msg="mine"), _commit_file("m.txt"),
                              "git reset -q --hard main")
    c = _check(r, fork, pre, tgt)
    assert c.failed == f"campaign commit {pre[:10]} (mine) is missing after the resolution"


def test_verify_resolution_unpaired_commit_is_extra_but_a_revert_fails(tmp_path):
    r, fork, pre, tgt = vrepo(tmp_path, _commit_file("a.txt"), _commit_file("m.txt"),
                              "git rebase main -q\n" + _commit_file("port.txt", msg="port"))
    c = _check(r, fork, pre, tgt)
    assert c.failed is None and [e["subject"] for e in c.extra] == ["port"]
    _sh(r, "git revert --no-edit HEAD~1")
    bad = _check(r, fork, pre, tgt)
    assert bad.failed and "reverts campaign commit" in bad.failed and "Revert" in bad.failed
    assert _git(r, "rev-parse", pre)[:10] in bad.failed


def test_verify_resolution_cherry_absorbs_what_the_target_changed_again(tmp_path):
    """The target holds the commit's exact patch, then changes the same lines: `git cherry`
    marks it `-`, but it no longer reverse-applies to HEAD."""
    r, fork, pre, tgt = vrepo(
        tmp_path, _commit_file("a.txt", "x", msg="mine"),
        _commit_file("a.txt", "x", msg="landed") + _commit_file("a.txt", "y", msg="later"),
        "git rebase main -q")
    assert _git(r, "cherry", tgt, pre).startswith("-")
    assert not G._patch_applies(r, pre, "HEAD", reverse=True)
    c = _check(r, fork, pre, tgt)
    assert c.failed is None and [a["subject"] for a in c.absorbed] == ["mine"]


@pytest.mark.parametrize("rebase", ["git rebase main -q", "git rebase --no-keep-empty main -q"])
def test_verify_resolution_empty_commit_does_not_trip_the_revert_guard(tmp_path, rebase):
    """An empty campaign commit (kept or dropped) is no revert target for a port commit."""
    r, fork, pre, tgt = vrepo(tmp_path, "git commit -q --allow-empty -m nothing\n",
                              _commit_file("m.txt"),
                              rebase + "\n" + _commit_file("port.txt", msg="port"))
    c = _check(r, fork, pre, tgt)
    assert c.failed is None and [e["subject"] for e in c.extra] == ["port"]


def test_verify_resolution_ignores_user_diff_and_apply_config(tmp_path):
    """The patch-equivalence checks do not depend on diff/apply UI config: absorption still
    holds and the revert guard still fires."""
    r, fork, pre, tgt = vrepo(
        tmp_path, _commit_file("a.txt", msg="mine") + _commit_file("b.txt", msg="two"),
        f"echo o > o.txt && {_write_cmd('a.txt')} && git add -A && git commit -q -m landed\n",
        "git rebase main -q")
    _sh(r, "git config diff.noprefix true && git config color.diff always "
           "&& git config diff.external false && git config apply.whitespace error")
    c = _check(r, fork, pre, tgt)
    assert c.failed is None and [e["subject"] for e in c.extra] == []
    _sh(r, "git revert --no-edit HEAD")
    bad = _check(r, fork, pre, tgt)
    assert bad.failed and "reverts campaign commit" in bad.failed


def test_verify_resolution_revert_of_an_absorbed_commit_fails(tmp_path):
    """The revert guard covers unpaired (absorbed) campaign commits too: the target holds the
    change (cherry `-`, dropped by the rebase), and an extra commit removes it from HEAD."""
    r, fork, pre, tgt = vrepo(tmp_path, _commit_file("a.txt", msg="mine"),
                              _commit_file("a.txt", msg="landed"),
                              "git rebase main -q\ngit revert --no-edit HEAD")
    assert _git(r, "cherry", tgt, pre).startswith("-")
    bad = _check(r, fork, pre, tgt)
    assert bad.failed and bad.failed.endswith(f"reverts campaign commit {pre[:10]}")


_CRLF = "printf 'a\\r\\nb\\r\\n' > w.txt && git add w.txt && git commit -q -m w0\n" \
        "printf 'a\\r\\nb\\r\\nc\\r\\n' > w.txt && git commit -qam w1\n"
_LATIN1 = "printf 'caf\\351\\nb\\n' > w.txt && git add w.txt && git commit -q -m w0\n" \
          "printf 'caf\\351\\nb\\nc\\n' > w.txt && git commit -qam w1\n"


@pytest.mark.parametrize("content", [_CRLF, _LATIN1], ids=["crlf", "latin1"])
def test_verify_resolution_patches_survive_crlf_and_non_utf8_bytes(tmp_path, content):
    """Patches go through `git apply` as bytes: CRLF line endings and non-UTF-8 text neither
    break the revert guard on a port commit nor hide a real revert."""
    r, fork, pre, tgt = vrepo(
        tmp_path, "git config core.autocrlf false\n" + content, _commit_file("m.txt"),
        "git rebase main -q\n" + _commit_file("port.txt", msg="port"))
    c = _check(r, fork, pre, tgt)
    assert c.failed is None and [e["subject"] for e in c.extra] == ["port"]
    _sh(r, "git revert --no-edit HEAD~1")
    bad = _check(r, fork, pre, tgt)
    assert bad.failed and "reverts campaign commit" in bad.failed


def test_verify_resolution_conflicted_latin1_commit_does_not_raise(tmp_path):
    """The campaign commit and the target both edit a Latin-1 line of `w.txt` and the resolver
    rewrites the commit, so range-diff pairs it `!` and would print a diff-of-diffs holding the
    raw 0xE9 byte. Verification reads only the pair headers and tolerates a non-UTF-8 subject."""
    r = tmp_path / "l"
    r.mkdir()
    _sh(r, "git init -q -b main && git config user.email t@t && git config user.name t "
           "&& git config core.autocrlf false && printf 'l1\\nl2\\ncaf\\351\\nl4\\nl5\\nl6\\n' > w.txt "
           "&& git add -A && git commit -q -m base")
    fork = _git(r, "rev-parse", "HEAD")
    _sh(r, "git checkout -q -b nw/x\n"
           "printf 'l1\\nl2\\ncaf\\351 branch\\nl4\\nl5\\nl6\\n' > w.txt && git commit -qam \"$(printf 'caf\\351')\"")
    pre = _git(r, "rev-parse", "HEAD")
    _sh(r, "git checkout -q main\n"
           "printf 'l1\\nl2\\ncaf\\351 main\\nl4\\nl5\\nl6\\n' > w.txt && git commit -qam landed")
    tgt = _git(r, "rev-parse", "HEAD")
    _sh(r, "git checkout -q nw/x\n"
           "git rebase main -q || true\n"
           "printf 'l1\\nl2\\ncaf\\351 merged\\nl4\\nl5\\nl6\\n' > w.txt && git add w.txt && GIT_EDITOR=true git rebase --continue")
    c = _check(r, fork, pre, tgt)
    assert c.failed is None and c.extra == [] and c.absorbed == []


def test_patch_applies_unusable_patch_raises_instead_of_answering_no(grepo, monkeypatch):
    r, fork, pre, target = grepo
    monkeypatch.setattr(G, "_commit_patch", lambda *a: b"not a patch\n")
    with pytest.raises(G.GitError):
        G._patch_applies(r, pre, target, reverse=False)


def test_patch_applies_leaves_index_and_worktree_untouched(grepo):
    r, fork, pre, target = grepo
    _write(r, "dirty.txt", "x\n")
    _git(r, "add", "dirty.txt")
    before = (_git(r, "ls-files", "-s"), _git(r, "status", "--porcelain"),
              (r / "f.txt").read_text())
    assert G._patch_applies(r, pre, f"{pre}^", reverse=False)
    assert G._patch_applies(r, pre, "HEAD", reverse=True)
    assert not G._patch_applies(r, pre, target, reverse=False)
    assert (_git(r, "ls-files", "-s"), _git(r, "status", "--porcelain"),
            (r / "f.txt").read_text()) == before


def test_commit_tracked_returns_none_when_nothing_is_staged(grepo):
    r, *_ = grepo
    _write(r, "stray.orig", "x\n")
    head = _git(r, "rev-parse", "HEAD")
    assert G.commit_tracked(r, "msg") is None and _git(r, "rev-parse", "HEAD") == head
    _write(r, "f.txt", "changed\n")
    sha = G.commit_tracked(r, "msg")
    assert sha == _git(r, "rev-parse", "HEAD") != head
    assert "stray.orig" not in _git(r, "ls-tree", "-r", "--name-only", "HEAD")


class _Ctx:
    def __init__(self, trailer=None):
        self._project = {"agents": {"commit_trailer": trailer}} if trailer else {}

    def project(self, state):
        return self._project


def test_resolver_commit_message_keeps_first_summary_line_cut_to_72(grepo):
    r, *_ = grepo
    _write(r, "f.txt", "changed\n")
    summary = ("x" * 100) + "\nsecond line"
    sha = Q._commit_resolver_changes(_Ctx("Co-Authored-By: t <t@t>"), {"id": "C1"}, r, "nw/x",
                                     "abcdef", {"summary": summary})
    assert sha == _git(r, "rev-parse", "HEAD")
    msg = _git(r, "log", "-1", "--format=%B")
    assert msg.splitlines()[0] == "nw(C1): conflict resolver run abc \u2014 " + "x" * 72
    assert "second line" not in msg and msg.endswith("Co-Authored-By: t <t@t>")


def test_resolver_commit_message_without_a_trailer_has_no_trailer_block(grepo):
    r, *_ = grepo
    _write(r, "f.txt", "changed\n")
    Q._commit_resolver_changes(_Ctx(), {"id": "C1"}, r, "nw/x", "abcdef", {"summary": "ok"})
    assert _git(r, "log", "-1", "--format=%B") == "nw(C1): conflict resolver run abc \u2014 ok"


# ---- review loop 3 --------------------------------------------------------------------------

def test_pre_head_mismatch_after_abort_is_an_error_not_a_dispatch(tmp_path, monkeypatch):
    """`prepare_close` refuses to dispatch from a worktree whose HEAD is not the pre-rebase head
    after the abort (the restore target would be wrong)."""
    w = basic(tmp_path, monkeypatch, resolver())
    w.open_and_conflict()
    real = G.rebase

    def lying(worktree, onto, **kw):
        rb = real(worktree, onto, **kw)
        from dataclasses import replace
        return replace(rb, head="0" * 40)

    monkeypatch.setattr(G, "rebase", lying)
    st = w.drive(lambda s: s["state"] == "PARKED")
    assert st["park"]["reason"] == "open_failed" and st["park"]["resume_to"] == "CLOSING"
    assert "not the pre-rebase" in st["park"]["detail"]
    assert not w.run_ids() and not w.ledger("rebase_resolved")


def test_head_moved_after_abort_is_an_error_not_a_dispatch(tmp_path, monkeypatch):
    w = basic(tmp_path, monkeypatch, resolver())
    w.open_and_conflict()
    real = G.rebase

    def moving(worktree, onto, **kw):
        rb = real(worktree, onto, **kw)
        _git(worktree, "commit", "-q", "--allow-empty", "-m", "drift")
        return rb

    monkeypatch.setattr(G, "rebase", moving)
    st = w.drive(lambda s: s["state"] == "PARKED")
    assert st["park"]["reason"] == "open_failed" and "not the pre-rebase" in st["park"]["detail"]
    assert not w.run_ids()


def test_verified_then_git_error_is_restored_not_merged_unrecorded(tmp_path, monkeypatch):
    """A git error after verification (the post-verify clean) must not leave the resolver's
    rewritten head on the branch without a `rebase_resolved` record."""
    w = basic(tmp_path, monkeypatch, resolver(), gave_up())
    w.open_and_conflict()
    pre = w.dispatched()["closing"]["resolve"]["pre_head"]
    real, calls = G.clean_untracked, []

    def flaky(worktree):
        calls.append(1)
        if len(calls) == 2:             # 1: the restore before the run; 2: after verification
            raise G.GitError(["git", "clean"], 1, "cannot remove scratch/")
        return real(worktree)

    monkeypatch.setattr(G, "clean_untracked", flaky)
    w.tick()
    st = w.st()
    assert st["state"] == "PARKED" and st["park"]["reason"] == "open_failed"
    assert st["park"]["resume_to"] == "CLOSING" and "cannot remove scratch/" in st["park"]["detail"]
    assert not w.ledger("rebase_resolved")
    assert w.head() == pre
    wt = st["repo"]["worktree"]
    assert _git(wt, "symbolic-ref", "HEAD") == f"refs/heads/{st['repo']['branch']}"
    assert _git(wt, "status", "--porcelain") == ""
    assert [e["run_id"] for e in w.ledger("run.end") if e["persona"] == "conflict_resolver"] \
        == w.run_ids()


def test_resolve_prepare_failure_before_run_start_writes_no_run_end(tmp_path, monkeypatch):
    w = basic(tmp_path, monkeypatch, resolver())
    w.open_and_conflict()
    w.dispatched()
    real = PS.build_request

    def boom(cfg, profile, pcfg, state, persona, **kw):
        if persona == "conflict_resolver":
            raise ValueError("prompt template broke")
        return real(cfg, profile, pcfg, state, persona, **kw)

    monkeypatch.setattr(PS, "build_request", boom)
    w.tick()
    st = w.st()
    assert st["state"] == "PARKED" and st["park"]["reason"] == "open_failed"
    assert "prompt template broke" in st["park"]["detail"]
    assert not w.run_ids()
    assert all(e.get("run_id") for e in w.ledger("run.end"))


def test_resolve_prepare_failure_after_run_start_closes_that_run(tmp_path, monkeypatch):
    w = basic(tmp_path, monkeypatch, resolver())
    w.open_and_conflict()
    w.dispatched()
    real_build, real_write, armed = PS.build_request, S.write_state, []

    def build(cfg, profile, pcfg, state, persona, **kw):
        if persona == "conflict_resolver":
            armed.append(1)
        return real_build(cfg, profile, pcfg, state, persona, **kw)

    def write(cfg, cid, state):
        if armed:                       # the write right after the resolver's run.start
            armed.clear()
            raise OSError("disk full")
        return real_write(cfg, cid, state)

    monkeypatch.setattr(PS, "build_request", build)
    monkeypatch.setattr(S, "write_state", write)
    w.tick()
    st = w.st()
    assert st["state"] == "PARKED" and st["park"]["reason"] == "open_failed"
    starts = w.run_ids()
    assert len(starts) == 1
    ends = [e for e in w.ledger("run.end") if e["persona"] == "conflict_resolver"]
    assert [e["run_id"] for e in ends] == starts and ends[0]["is_error"] is True
    assert all(e.get("run_id") for e in w.ledger("run.end"))


# ---- resolve_context caps ---------------------------------------------------------------------

OWN_SPEC = "docs/specs/0500-own.md"


def _spec_text(n, lines=450):
    return f"---\nid: SPEC-{n}\n---\n" + "".join(f"line {i}\n" for i in range(lines))


def _ctx_repo(tmp_path, *, commits, unnamed=False, base_specs=(2001, 2002, 2003, 2004)):
    """A repo with a campaign branch rewriting a big f.txt and `commits` commits on main that
    rewrite it too, each with a `SPEC-NNNN:` subject. Returns (repo, state, resolve)."""
    r = tmp_path / "ctx"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@t")
    _git(r, "config", "user.name", "t")
    _write(r, "f.txt", "".join(f"L{i}\n" for i in range(800)))
    for n in base_specs:
        _write(r, f"docs/specs/{n}-s.md", _spec_text(n))
    _write(r, OWN_SPEC, _spec_text(500, 5))
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "base")
    fork = _git(r, "rev-parse", "HEAD")
    _git(r, "checkout", "-q", "-b", "nw/x")
    _write(r, "f.txt", "".join(f"B{i}\n" for i in range(800)))
    _git(r, "commit", "-qam", "branch edit")
    pre = _git(r, "rev-parse", "HEAD")
    _git(r, "checkout", "-q", "main")
    ids = [2001, 2002, 2003, 2004]
    for i in range(commits):
        _write(r, "f.txt", "".join(f"M{i}-{j}\n" for j in range(800)))
        _git(r, "commit", "-qam", f"SPEC-{ids[i % 4]}: tweak {i}")
    if unnamed:
        _write(r, "docs/specs/3001-unnamed.md", _spec_text(3001, 3))
    _write(r, OWN_SPEC, _spec_text(500, 6))          # the campaign's own spec: must be skipped
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "docs only")
    target = _git(r, "rev-parse", "HEAD")
    _git(r, "checkout", "-q", "nw/x")
    state = {"repo": {"worktree": str(r)}, "spec": {"rel_path": OWN_SPEC}}
    resolve = {"fork": fork, "pre_head": pre, "target_sha": target, "conflicts": ["f.txt"]}
    return r, state, resolve


def test_resolve_context_caps(tmp_path, monkeypatch):
    r, state, resolve = _ctx_repo(tmp_path, commits=55)
    calls = []
    real = PS._git_text

    def counting(worktree, *args):
        calls.append(args)
        return real(worktree, *args)

    monkeypatch.setattr(PS, "_git_text", counting)
    ctx = PS.resolve_context(state, resolve, {})
    for key in ("campaign_diff", "base_diff"):               # 600 lines + the truncation note
        lines = ctx[key].splitlines()
        assert len(lines) == PS._RESOLVE_DIFF_MAX_LINES + 1 and "truncated" in lines[-1]
    landed = ctx["landed"].splitlines()
    commit_lines = [ln for ln in landed if ln.startswith("- ")]
    assert len(commit_lines) == PS._RESOLVE_LANDED_MAX == 50
    assert "\u2026 5 more commit(s)" in landed
    heads = [ln for ln in landed if ln.startswith("### Governing spec")]
    assert len(heads) == PS._RESOLVE_SPECS_MAX == 3
    assert not any("0500" in h for h in heads)                # the campaign's own spec is skipped
    assert sum("truncated: 53 more line(s)" in ln for ln in landed) == 3   # 3+450 lines, cut at 400
    block = "\n".join(landed[landed.index(heads[0]):])
    assert block.count("line 396") == 3 and "line 397" not in block
    # one listing of the target's specs, however many subjects name an unchanged spec
    assert sum(1 for a in calls if a[0] == "ls-tree") <= 1


def test_resolve_context_ls_tree_runs_once_and_stops_at_three_specs(tmp_path, monkeypatch):
    r, state, resolve = _ctx_repo(tmp_path, commits=40)
    calls = []
    real = PS._git_text
    monkeypatch.setattr(PS, "_git_text", lambda w, *a: (calls.append(a), real(w, *a))[1])
    ctx = PS.resolve_context(state, resolve, {})
    assert sum(1 for a in calls if a[0] == "ls-tree") == 1
    assert ctx["landed"].count("### Governing spec") == 3


def test_resolve_context_picks_up_an_unnamed_changed_spec(tmp_path):
    r, state, resolve = _ctx_repo(tmp_path, commits=1, unnamed=True)
    landed = PS.resolve_context(state, resolve, {})["landed"]
    assert "docs/specs/2001-s.md" in landed                   # named by the subject
    assert "docs/specs/3001-unnamed.md" in landed             # named by no subject, changed in range
    assert "0500-own" not in landed                           # the campaign's own spec


def test_resolve_context_skips_the_campaign_spec_named_in_a_subject(tmp_path):
    r, state, resolve = _ctx_repo(tmp_path, commits=0)
    _git(r, "checkout", "-q", "main")
    _write(r, "f.txt", "".join(f"X{j}\n" for j in range(800)))
    _git(r, "commit", "-qam", "SPEC-0500: touches the campaign's own spec id")
    resolve["target_sha"] = _git(r, "rev-parse", "HEAD")
    _git(r, "checkout", "-q", "nw/x")
    landed = PS.resolve_context(state, resolve, {})["landed"]
    assert "SPEC-0500: touches" in landed and "### Governing spec" not in landed


# ---- SPEC-0259: a resolver may declare a campaign commit superseded ---------------------------

def _sr(r, sha, reason="superseded upstream"):
    return {"sha": sha, "reason": reason}


def _supersede_vrepo(tmp_path):
    return vrepo(tmp_path, _commit_file("f.txt", "mine", msg="mine"),
                 _commit_file("f.txt", "theirs", msg="theirs"), "git reset -q --hard main")


def test_verify_resolution_declared_missing_commit_is_superseded(tmp_path):
    r, fork, pre, tgt = _supersede_vrepo(tmp_path)
    assert _git(r, "cherry", tgt, pre).startswith("+")
    c = G.verify_resolution(r, "nw/x", tgt, fork, pre, [(pre[:8], "fixed another way")])
    assert c.failed is None and c.absorbed == [] and c.extra == []
    assert [(e["sha"], e["subject"], e["reason"]) for e in c.superseded] \
        == [(pre, "mine", "fixed another way")]
    assert G.commit_files(r, [pre]) == ["f.txt"]


def test_verify_resolution_undeclared_missing_commit_still_fails(tmp_path):
    r, fork, pre, tgt = _supersede_vrepo(tmp_path)
    assert _check(r, fork, pre, tgt).failed.endswith("(mine) is missing after the resolution")
    assert G.verify_resolution(r, "nw/x", tgt, fork, pre, ()).failed


def test_declared_but_present_commit_ignored(tmp_path):
    r, fork, pre, tgt = vrepo(tmp_path, _commit_file("a.txt"), _commit_file("m.txt"),
                              "git rebase main -q")
    c = G.verify_resolution(r, "nw/x", tgt, fork, pre, [(pre, "wrongly declared")])
    assert c.failed is None and c.superseded == [] and c.extra == []


def test_declared_unknown_sha_ignored(tmp_path):
    r, fork, pre, tgt = _supersede_vrepo(tmp_path)
    for sha in ("deadbeef", tgt, fork, "not a sha"):    # unknown, target commit, pre-fork commit
        c = G.verify_resolution(r, "nw/x", tgt, fork, pre, [(sha, "r")])
        assert c.failed and "is missing after the resolution" in c.failed, sha
        assert c.superseded == []


def test_declared_drop_does_not_relax_other_checks(tmp_path):
    r, fork, pre, tgt = _supersede_vrepo(tmp_path)
    _sh(r, "git checkout -q --detach")
    c = G.verify_resolution(r, "nw/x", tgt, fork, pre, [(pre, "r")])
    assert c.failed == "HEAD is not attached to the campaign branch"


SUPERSEDE_PRE = (f"printf '{APP_BRANCH}'".replace("\n", "\\n")
                 + " > src/app.py && git add src/app.py && git commit -q -m 'edit line c'")


def _supersede_world(tmp_path, monkeypatch, *, dropped=True, **kw):
    """Commit 1 (made in the worktree) edits app.py line 3 one way, main edits it another way;
    the resolver `git rebase --skip`s commit 1 and, if `dropped`, declares it."""
    w = World(tmp_path, monkeypatch,
              [impl_done(), review(), resolver(shell=("git rebase --skip",), edits=False)], **kw)
    w.tick()
    wt = w.st()["repo"]["worktree"]
    subprocess.run(["sh", "-ce", SUPERSEDE_PRE], cwd=wt, check=True, capture_output=True)
    sha = _git(wt, "rev-parse", "HEAD")
    if dropped:
        w.runner.script[2]["verdict"]["dropped_commits"] = [
            {"sha": sha[:12], "reason": "main fixed line 3 another way"}]
    w.main_conflict()
    return w, sha


def test_superseded_commit_declared_verified(tmp_path, monkeypatch):
    w, sha = _supersede_world(tmp_path, monkeypatch)
    ev, = _merged(w)
    assert [(c["sha"], c["subject"], c["reason"]) for c in ev["superseded_commits"]] \
        == [(sha, "edit line c", "main fixed line 3 another way")]
    f, = w.findings("resolver dropped campaign commits")
    assert f["severity"] == "major" and f["gate"] == "rebase" and f["persona"] == "conflict_resolver"
    assert sha[:10] in f["detail"] and "edit line c" in f["detail"] \
        and "main fixed line 3 another way" in f["detail"]
    assert f["files"] == ["src/app.py"]
    assert "1 superseded" in _render_resolved(ev)
    assert (w.repo / "src" / "app.py").read_text() == APP_MAIN


def test_superseded_commit_regates_a_failing_gate_does_not_merge(tmp_path, monkeypatch):
    w, _ = _supersede_world(tmp_path, monkeypatch, test_gate="grep -q c-branch src/app.py")
    w.drive(lambda s: s["state"] == "TRIAGING" or s["state"] in ("MERGED", "PARKED"))
    assert w.ledger("rebase_resolved")[0]["superseded_commits"]
    st = w.st()
    assert st["state"] != "MERGED"
    assert [g for g in w.ledger("gate") if not g.get("ok")], "the re-gate did not run"
    assert _git(w.repo, "log", "-1", "--format=%s") == "SPEC-9001: change app line 3"


def test_superseded_undeclared_through_the_queue_parks(tmp_path, monkeypatch):
    w, _ = _supersede_world(tmp_path, monkeypatch, dropped=False)
    w.runner.script.append(gave_up())
    w.dispatched()
    w.attempted(1)
    assert not w.ledger("rebase_resolved")
    f, = w.findings("conflict resolver did not resolve")
    assert "is missing after the resolution" in f["detail"]


GATED = {"gates": [{"name": "t", "cmd": "true"}]}      # `required` defaults to true


def test_close_regates_only_without_docs_only_merge_request():
    assert Q._close_regates({"kind": "spec"}, GATED)
    assert Q._close_regates({"kind": "merge", "merge": {}}, GATED)
    assert not Q._close_regates({"kind": "merge", "merge": {"docs_only": True}}, GATED)
    verdict = {"dropped_commits": [{"sha": "abc", "reason": "r"}, {"reason": "no sha"}, "junk"]}
    assert Q._declared_drops({"kind": "spec"}, verdict, GATED) == [("abc", "r")]
    assert Q._declared_drops({"kind": "merge", "merge": {"docs_only": True}}, verdict, GATED) == []


def test_declared_drop_ignored_without_required_gates(tmp_path, monkeypatch):
    verdict = {"dropped_commits": [{"sha": "abc", "reason": "r"}]}
    optional = {"gates": [{"name": "a", "cmd": "true", "required": False},
                          {"name": "b", "cmd": "true", "required": False}]}
    for pcfg in ({"gates": []}, {}, optional):
        assert not Q._close_regates({"kind": "spec"}, pcfg)
        assert Q._declared_drops({"kind": "spec"}, verdict, pcfg) == []
    mixed = {"gates": [*optional["gates"], {"name": "c", "cmd": "true", "required": True}]}
    assert Q._declared_drops({"kind": "spec"}, verdict, mixed) == [("abc", "r")]
    # through the queue: a resolver that drops a campaign commit and declares it is not believed
    w, _ = _supersede_world(tmp_path, monkeypatch)
    real = P.project_config
    monkeypatch.setattr(P, "project_config", lambda cfg, name: {**real(cfg, name), "gates": []})
    w.runner.script.append(gave_up())
    w.dispatched()
    w.attempted(1)
    assert not w.ledger("rebase_resolved")
    f, = w.findings("conflict resolver did not resolve")
    assert "is missing after the resolution" in f["detail"]


def test_docs_only_merge_request_ignores_declaration(tmp_path, monkeypatch):
    w, sha = _supersede_world(tmp_path, monkeypatch)
    w.runner.script.append(gave_up())
    st = w.dispatched()
    pre = st["closing"]["resolve"]["pre_head"]
    on_disk = w.st()
    on_disk["kind"], on_disk["merge"] = "merge", {"docs_only": True}
    S.write_state(w.cfg, w.cid, on_disk)
    st = w.attempted(1)
    wt = st["repo"]["worktree"]
    assert not w.ledger("rebase_resolved") and not w.findings("resolver dropped campaign commits")
    f, = w.findings("conflict resolver did not resolve")
    assert "is missing after the resolution" in f["detail"]
    assert _git(wt, "rev-parse", "HEAD") == pre


def test_rebase_resolved_text_superseded():
    base = {"conflicts": ["a"], "resolutions": []}
    assert _render_resolved({**base, "superseded_commits": []}) == "rebase resolved: 1 paths"
    assert _render_resolved({**base, "superseded_commits": [{}, {}]}) \
        == "rebase resolved: 1 paths; 2 superseded"
