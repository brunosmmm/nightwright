"""SPEC-0255: close regenerates declared generated files after the rebase and commits them
before gating, so the gated head is the merged head. Fixtures come from test_agentic_close."""
import json

import pytest

from test_agentic_close import (_docs_only_world, _events, _git, _git_env, _commit,  # noqa: F401
                                _merge_world, _pcfg, world)
from wt.agentic import close as CL
from wt.agentic import gitops as G
from wt.agentic import queue as Q

CMD = "cp src.txt gen.txt"


def _regen(entries):
    return json.dumps([{"path": p, "cmd": c} for p, c in entries])


def _pc(world, cmd=CMD, gates=("true",), path="gen.txt"):
    return _pcfg(world, gates=gates, regenerate=_regen([(path, cmd)]))


def _stale(world, gen="old\n"):
    """The branch changes the source but carries a stale generated file."""
    _commit(world["wt"], "src.txt", "new\n", "source")
    _commit(world["wt"], "gen.txt", gen, "stale generated")


def _commits(wt):
    return _git(wt, "log", "--format=%s", "main..HEAD").splitlines()


def test_stale_generated_file_is_committed_before_the_head_is_captured(world):
    _stale(world)
    pcfg = _pc(world)                              # commits the config to main: rebase moves HEAD
    _git(world["wt"], "rebase", "-q", "main")
    before = G.rev_parse(world["wt"])
    prep = CL.prepare_close(world["cfg"], world["state"], pcfg)
    assert prep.outcome is None and prep.head == G.rev_parse(world["wt"])
    assert _git(world["wt"], "rev-parse", "HEAD~1") == before
    assert _git(world["wt"], "show", "--name-only", "--format=%s", "HEAD").split() == [
        f"wt({world['state']['id']}):", "regenerate", "generated", "files", "after", "rebase",
        "gen.txt"]
    assert (world["wt"] / "gen.txt").read_text() == "new\n"
    commit = _events(prep, "commit")
    assert commit and commit[0]["sha"] == prep.head
    info = [p for p in _events(prep, "finding")
            if p["title"] == "generated files regenerated after rebase and committed"]
    assert info and info[0]["severity"] == "info" and info[0]["files"] == ["gen.txt"]


def test_merged_tree_is_the_gated_tree_and_has_the_regenerated_file(world):
    _stale(world)
    pcfg = _pc(world, gates=("grep -q new gen.txt",))            # fails without the helper
    out = CL.close_campaign(world["cfg"], world["state"], pcfg)
    assert out.next_state == "MERGED", out
    assert (world["root"] / "gen.txt").read_text() == "new\n"
    assert _git(world["root"], "show", "--name-only", "--format=", "-1") == "gen.txt"
    assert _git(world["root"], "log", "--format=%s", "-1").endswith(
        "regenerate generated files after rebase")


def test_fresh_generated_file_adds_no_commit(world):
    _stale(world, gen="new\n")
    pcfg = _pc(world)
    _git(world["wt"], "rebase", "-q", "main")
    before = G.rev_parse(world["wt"])
    prep = CL.prepare_close(world["cfg"], world["state"], pcfg)
    assert prep.outcome is None and prep.head == before
    assert _events(prep, "commit") == []


def test_only_declared_paths_are_committed_and_prior_dirt_is_left_alone(world):
    _stale(world)
    (world["wt"] / "junk.txt").write_text("untracked\n")
    events = []
    assert CL._regenerate_after_rebase(world["wt"], {"gen.txt": CMD}, events, "C1") is None
    assert _git(world["wt"], "show", "--name-only", "--format=", "HEAD") == "gen.txt"
    assert _git(world["wt"], "status", "--porcelain") == "?? junk.txt"


def test_failing_regenerate_command_parks_without_merging(world):
    _stale(world)
    out = CL.close_campaign(world["cfg"], world["state"], _pc(world, "echo boom >&2; exit 1"))
    assert out.next_state == "PARKED" and out.park["reason"] == "regenerate_failed"
    assert out.park["resume_to"] == "CLOSING" and "boom" in out.park["detail"]
    assert _git(world["root"], "log", "--format=%s", "-1") != "stale generated"
    assert not (world["root"] / "gen.txt").exists()
    assert "regenerate_failed" in Q.PARK_REASONS
    assert Q.park_owner("regenerate_failed") == "conductor"


def test_regenerate_command_dirtying_an_undeclared_file_parks(world):
    _stale(world)
    out = CL.close_campaign(world["cfg"], world["state"],
                            _pc(world, CMD + "; echo x > stray.txt"))
    assert out.next_state == "PARKED" and out.park["reason"] == "regenerate_failed"
    assert out.park["resume_to"] == "CLOSING" and "stray.txt" in out.park["detail"]
    assert _git(world["wt"], "status", "--porcelain") == ""           # put back, not gated dirty
    assert not (world["root"] / "gen.txt").exists()


def test_docs_only_close_regenerates_a_declared_path_without_parking(world):
    _commit(world["root"], "docs/LEDGER.md", "stale\n", "ledger")
    st, _ = _docs_only_world(world, {"docs/design/a.md": "x\n"})
    pcfg = _pcfg(world, gates=("false",), docs_only_paths=json.dumps(["docs/design/"]),
                 regenerate=_regen([("docs/LEDGER.md", "echo fresh > docs/LEDGER.md")]))
    out = CL.close_campaign(world["cfg"], st, pcfg)
    assert out.next_state == "MERGED", out
    assert (world["root"] / "docs/LEDGER.md").read_text() == "fresh\n"
    assert (world["root"] / "docs/design/a.md").exists() and _events(out, "gate") == []


def test_merge_request_keeps_the_requesters_commits_and_adds_ours(world):
    st = _merge_world(world)
    _stale(world)
    st["merge"]["head_at_request"] = G.rev_parse(world["wt"])
    out = CL.close_campaign(world["cfg"], st, _pc(world))
    assert out.next_state == "MERGED", out
    log = _git(world["root"], "log", "--format=%s", "-4").splitlines()
    assert log[0].endswith("regenerate generated files after rebase")
    assert log[1:] == ["stale generated", "source", "feature"]       # the requester's, untouched
    assert (world["root"] / "gen.txt").read_text() == "new\n"


def test_resolver_style_rebase_ends_with_the_regenerated_file_in_main(world):
    _commit(world["root"], "src.txt", "base\n", "src")
    _commit(world["root"], "gen.txt", "base\n", "gen")
    G.git(world["wt"], "rebase", "-q", "main")
    _commit(world["wt"], "src.txt", "branch\n", "branch src")
    _commit(world["wt"], "gen.txt", "branch\n", "branch gen")
    _commit(world["root"], "src.txt", "main\n", "main src")
    _commit(world["root"], "gen.txt", "main\n", "main gen")
    pcfg = _pc(world)
    first = CL.close_campaign(world["cfg"], world["state"], pcfg)
    assert first.next_state == "PARKED" and first.park["reason"] == "rebase_conflict"
    G.git(world["wt"], "rebase", "main", check=False)                 # the resolver: merge text
    (world["wt"] / "src.txt").write_text("merged\n")
    (world["wt"] / "gen.txt").write_text("stale merged text\n")
    G.git(world["wt"], "add", "-A")
    while G.git(world["wt"], "rebase", "--continue", check=False,
                env={"GIT_EDITOR": "true"}).returncode != 0:
        (world["wt"] / "src.txt").write_text("merged\n")
        (world["wt"] / "gen.txt").write_text("stale merged text\n")
        G.git(world["wt"], "add", "-A")
    out = CL.close_campaign(world["cfg"], world["state"], pcfg)
    assert out.next_state == "MERGED", out
    assert (world["root"] / "gen.txt").read_text() == "merged\n"


def test_adopted_worktree_keeps_files_written_while_a_failing_command_runs(world):
    st = _merge_world(world)
    _stale(world)
    out = CL.close_campaign(world["cfg"], st, _pc(world, "echo mine > mine.txt; exit 1"))
    assert out.next_state == "PARKED" and out.park["reason"] == "regenerate_failed"
    assert (world["wt"] / "mine.txt").read_text() == "mine\n"        # the requester's, not cleaned


def test_adopted_worktree_keeps_stray_files_and_edits_and_parks(world):
    st = _merge_world(world)
    _stale(world)
    cmd = CMD + "; echo mine > mine.txt; echo edited > feature.txt"
    pcfg = _pc(world, cmd)
    out = CL.close_campaign(world["cfg"], st, pcfg)
    assert out.next_state == "PARKED" and out.park["reason"] == "regenerate_failed"
    assert "mine.txt" in out.park["detail"]
    assert (world["wt"] / "mine.txt").exists()
    assert (world["wt"] / "feature.txt").read_text() == "edited\n"   # not reset
    again = CL.close_campaign(world["cfg"], st, pcfg)
    assert again.park["reason"] == "worktree_dirty"                    # the established resume


def test_declared_path_that_does_not_exist_parks_and_resume_snapshots_nothing(world):
    _stale(world)
    pcfg = _pc(world, "echo x > stray.txt", path="missing.txt")
    out = CL.close_campaign(world["cfg"], world["state"], pcfg)
    assert out.next_state == "PARKED" and out.park["reason"] == "regenerate_failed"
    assert out.park["resume_to"] == "CLOSING"
    head = G.rev_parse(world["wt"])
    assert _git(world["wt"], "status", "--porcelain") == ""           # put back
    again = CL.close_campaign(world["cfg"], world["state"], pcfg)     # the resume
    assert again.park["reason"] == "regenerate_failed"
    assert G.rev_parse(world["wt"]) == head
    assert "close-time snapshot" not in _git(world["wt"], "log", "--format=%s", "main..HEAD")
    assert not (world["root"] / "stray.txt").exists()


def test_refused_docs_only_merge_never_runs_the_regenerate_commands(world):
    st, _ = _docs_only_world(world, {"src/code.py": "x\n"})
    pcfg = _pcfg(world, gates=("false",), docs_only_paths=json.dumps(["docs/design/"]),
                 regenerate=_regen([("gen.txt", "touch ran.txt; echo f > gen.txt")]))
    out = CL.close_campaign(world["cfg"], st, pcfg)
    assert out.next_state == "QUEUED" or out.park["reason"] == "not_docs_only"
    assert not (world["wt"] / "ran.txt").exists()


def test_run_regenerate_raises_with_the_output_tail(world):
    G.run_regenerate(world["wt"], "true")
    with pytest.raises(G.GitError, match="nope"):
        G.run_regenerate(world["wt"], "echo nope; exit 3")
