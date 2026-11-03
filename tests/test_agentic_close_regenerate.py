"""SPEC-0255: close regenerates declared generated files after the rebase and commits them
before gating, so the gated head is the merged head. Fixtures come from test_agentic_close."""
import json

import pytest

from test_agentic_close import (_docs_only_world, _events, _git, _git_env, _commit,  # noqa: F401
                                _merge_world, _pcfg, world)
from nightwright.agentic import close as CL
from nightwright.agentic import gitops as G
from nightwright.agentic import queue as Q

CMD = "cp src.txt gen.txt"


def _regen(entries):
    return json.dumps([{"path": p, "cmd": c} for p, c in entries])


def _pc(world, cmd=CMD, gates=("true",), path="gen.txt"):
    return _pcfg(world, gates=gates, regenerate=_regen([(path, cmd)]))


def _stale(world, gen="old\n"):
    """The branch changes the source but carries a stale generated file."""
    _commit(world["nw"], "src.txt", "new\n", "source")
    _commit(world["nw"], "gen.txt", gen, "stale generated")


def _commits(wt):
    return _git(wt, "log", "--format=%s", "main..HEAD").splitlines()


def test_stale_generated_file_is_committed_before_the_head_is_captured(world):
    _stale(world)
    pcfg = _pc(world)                              # commits the config to main: rebase moves HEAD
    _git(world["nw"], "rebase", "-q", "main")
    before = G.rev_parse(world["nw"])
    prep = CL.prepare_close(world["cfg"], world["state"], pcfg)
    assert prep.outcome is None and prep.head == G.rev_parse(world["nw"])
    assert _git(world["nw"], "rev-parse", "HEAD~1") == before
    assert _git(world["nw"], "show", "--name-only", "--format=%s", "HEAD").split() == [
        f"nw({world['state']['id']}):", "regenerate", "generated", "files", "after", "rebase",
        "gen.txt"]
    assert (world["nw"] / "gen.txt").read_text() == "new\n"
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
    _git(world["nw"], "rebase", "-q", "main")
    before = G.rev_parse(world["nw"])
    prep = CL.prepare_close(world["cfg"], world["state"], pcfg)
    assert prep.outcome is None and prep.head == before
    assert _events(prep, "commit") == []


def test_only_declared_paths_are_committed_and_prior_dirt_is_left_alone(world):
    _stale(world)
    (world["nw"] / "junk.txt").write_text("untracked\n")
    events = []
    assert CL._regenerate_after_rebase(world["nw"], {"gen.txt": CMD}, events, "C1") is None
    assert _git(world["nw"], "show", "--name-only", "--format=", "HEAD") == "gen.txt"
    assert _git(world["nw"], "status", "--porcelain") == "?? junk.txt"


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
    assert _git(world["nw"], "status", "--porcelain") == ""           # put back, not gated dirty
    assert not (world["root"] / "gen.txt").exists()


@pytest.mark.parametrize("adopted", [False, True])
def test_regenerate_that_stages_undeclared_file_parks(world, adopted):
    """SPEC-0266: undeclared paths a command staged itself never reach the regenerate commit,
    and are unstaged again (own and adopted worktree)."""
    st = _merge_world(world) if adopted else world["state"]
    _stale(world)
    out = CL.close_campaign(world["cfg"], st, _pc(world, CMD + " && touch x && git add x"))
    assert out.next_state == "PARKED" and out.park["reason"] == "regenerate_failed"
    assert "staged undeclared files: x" in out.park["detail"]
    assert "regenerate generated files after rebase" not in "\n".join(_commits(world["nw"]))
    assert _git(world["nw"], "diff", "--cached", "--name-only") == ""
    assert not (world["root"] / "gen.txt").exists()


def test_regenerate_rename_of_undeclared_path_refuses(world):
    """SPEC-0287: a staged rename lists its source too, so `git mv undeclared declared` cannot
    smuggle the undeclared deletion into the regenerate commit."""
    _commit(world["nw"], "undeclared.txt", "keep me\n", "undeclared")
    head = G.rev_parse(world["nw"])
    detail = CL._regenerate_after_rebase(world["nw"], {"gen.txt": "git mv undeclared.txt gen.txt"},
                                         [], "C1")
    assert detail and "staged undeclared files" in detail and "undeclared.txt" in detail
    assert G.rev_parse(world["nw"]) == head
    assert _git(world["nw"], "ls-tree", "--name-only", "HEAD", "undeclared.txt") == "undeclared.txt"


def test_docs_only_close_regenerates_a_declared_path_without_parking(world):
    _commit(world["root"], "docs/LEDGER.md", "stale\n", "ledger")
    st, _ = _docs_only_world(world, {"docs/design/a.md": "x\n"})
    pcfg = _pcfg(world, gates=("false",), docs_only_paths=json.dumps(["docs/design/"]),
                 regenerate=_regen([("docs/LEDGER.md", "echo fresh > docs/LEDGER.md")]))
    out = CL.close_campaign(world["cfg"], st, pcfg)
    assert out.next_state == "MERGED", out
    assert (world["root"] / "docs/LEDGER.md").read_text() == "fresh\n"
    assert (world["root"] / "docs/design/a.md").exists() and _events(out, "gate") == []


def test_docs_only_close_accepts_a_branch_that_itself_carries_a_stale_declared_path(world):
    st, _ = _docs_only_world(world, {"docs/design/a.md": "x\n", "docs/LEDGER.md": "stale\n"})
    pcfg = _pcfg(world, gates=("false",), docs_only_paths=json.dumps(["docs/design/"]),
                 regenerate=_regen([("docs/LEDGER.md", "echo fresh > docs/LEDGER.md")]))
    out = CL.close_campaign(world["cfg"], st, pcfg)
    assert out.next_state == "MERGED", out                       # not PARKED / not_docs_only
    assert (world["root"] / "docs/LEDGER.md").read_text() == "fresh\n"
    assert (world["root"] / "docs/design/a.md").exists() and _events(out, "gate") == []


def test_merge_request_keeps_the_requesters_commits_and_adds_ours(world):
    st = _merge_world(world)
    _stale(world)
    st["merge"]["head_at_request"] = G.rev_parse(world["nw"])
    out = CL.close_campaign(world["cfg"], st, _pc(world))
    assert out.next_state == "MERGED", out
    log = _git(world["root"], "log", "--format=%s", "-4").splitlines()
    assert log[0].endswith("regenerate generated files after rebase")
    assert log[1:] == ["stale generated", "source", "feature"]       # the requester's, untouched
    assert (world["root"] / "gen.txt").read_text() == "new\n"


def test_resolver_style_rebase_ends_with_the_regenerated_file_in_main(world):
    _commit(world["root"], "src.txt", "base\n", "src")
    _commit(world["root"], "gen.txt", "base\n", "gen")
    G.git(world["nw"], "rebase", "-q", "main")
    _commit(world["nw"], "src.txt", "branch\n", "branch src")
    _commit(world["nw"], "gen.txt", "branch\n", "branch gen")
    _commit(world["root"], "src.txt", "main\n", "main src")
    _commit(world["root"], "gen.txt", "main\n", "main gen")
    pcfg = _pc(world)
    first = CL.close_campaign(world["cfg"], world["state"], pcfg)
    assert first.next_state == "PARKED" and first.park["reason"] == "rebase_conflict"
    G.git(world["nw"], "rebase", "main", check=False)                 # the resolver: merge text
    (world["nw"] / "src.txt").write_text("merged\n")
    (world["nw"] / "gen.txt").write_text("stale merged text\n")
    G.git(world["nw"], "add", "-A")
    while G.git(world["nw"], "rebase", "--continue", check=False,
                env={"GIT_EDITOR": "true"}).returncode != 0:
        (world["nw"] / "src.txt").write_text("merged\n")
        (world["nw"] / "gen.txt").write_text("stale merged text\n")
        G.git(world["nw"], "add", "-A")
    out = CL.close_campaign(world["cfg"], world["state"], pcfg)
    assert out.next_state == "MERGED", out
    assert (world["root"] / "gen.txt").read_text() == "merged\n"


def test_adopted_worktree_keeps_files_written_while_a_failing_command_runs(world):
    st = _merge_world(world)
    _stale(world)
    out = CL.close_campaign(world["cfg"], st, _pc(world, "echo mine > mine.txt; exit 1"))
    assert out.next_state == "PARKED" and out.park["reason"] == "regenerate_failed"
    assert (world["nw"] / "mine.txt").read_text() == "mine\n"        # the requester's, not cleaned


def test_adopted_worktree_keeps_stray_files_and_edits_and_parks(world):
    st = _merge_world(world)
    _stale(world)
    cmd = CMD + "; echo mine > mine.txt; echo edited > feature.txt"
    pcfg = _pc(world, cmd)
    out = CL.close_campaign(world["cfg"], st, pcfg)
    assert out.next_state == "PARKED" and out.park["reason"] == "regenerate_failed"
    assert "mine.txt" in out.park["detail"]
    assert (world["nw"] / "mine.txt").exists()
    assert (world["nw"] / "feature.txt").read_text() == "edited\n"   # not reset
    again = CL.close_campaign(world["cfg"], st, pcfg)
    assert again.park["reason"] == "worktree_dirty"                    # the established resume


def test_declared_path_that_does_not_exist_parks_and_resume_snapshots_nothing(world):
    _stale(world)
    pcfg = _pc(world, "echo x > stray.txt", path="missing.txt")
    out = CL.close_campaign(world["cfg"], world["state"], pcfg)
    assert out.next_state == "PARKED" and out.park["reason"] == "regenerate_failed"
    assert out.park["resume_to"] == "CLOSING"
    head = G.rev_parse(world["nw"])
    assert _git(world["nw"], "status", "--porcelain") == ""           # put back
    again = CL.close_campaign(world["cfg"], world["state"], pcfg)     # the resume
    assert again.park["reason"] == "regenerate_failed"
    assert G.rev_parse(world["nw"]) == head
    assert "close-time snapshot" not in _git(world["nw"], "log", "--format=%s", "main..HEAD")
    assert not (world["root"] / "stray.txt").exists()


def test_refused_docs_only_merge_never_runs_the_regenerate_commands(world):
    st, _ = _docs_only_world(world, {"src/code.py": "x\n"})
    pcfg = _pcfg(world, gates=("false",), docs_only_paths=json.dumps(["docs/design/"]),
                 regenerate=_regen([("gen.txt", "touch ran.txt; echo f > gen.txt")]))
    out = CL.close_campaign(world["cfg"], st, pcfg)
    assert out.next_state == "QUEUED" or out.park["reason"] == "not_docs_only"
    assert not (world["nw"] / "ran.txt").exists()


def test_run_regenerate_raises_with_the_output_tail(world):
    G.run_regenerate(world["nw"], "true")
    with pytest.raises(G.GitError, match="nope"):
        G.run_regenerate(world["nw"], "echo nope; exit 3")


# ---- SPEC-0399: the ledger is regenerated on every rebase, declared or not --------------------

import shutil  # noqa: E402
import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402

LEDGER = "docs/LEDGER.md"
LEDGER_CMD = "python3 tools/spec_lint.py --write-ledger"
HEAD = "# Ledger\n\n<!-- specs:begin (generated by tools/spec_lint.py --write-ledger) -->\n"
TAIL = "<!-- specs:end -->\n"
# deterministic stand-in for spec_lint: rewrite the marker block from docs/specs/*.md
STUB = '''import pathlib, re, sys
root = pathlib.Path(__file__).resolve().parent.parent
led = root / "docs" / "LEDGER.md"
text = led.read_text()
if "<!-- specs:begin" not in text:
    sys.exit(1)
rows = "".join(f"- {p.name}\\n" for p in sorted((root / "docs" / "specs").glob("*.md")))
led.write_text(re.sub(r"(<!-- specs:begin[^\\n]*-->\\n).*?(<!-- specs:end -->)",
                      lambda m: m.group(1) + rows + m.group(2), text, flags=re.S))
'''
FAILING_STUB = "import sys\nsys.exit(1)\n"


def _spec(repo, name):
    (Path(repo) / "docs/specs").mkdir(parents=True, exist_ok=True)
    (Path(repo) / "docs/specs" / name).write_text(f"spec {name}\n")


def _ledger_rows(repo, *names):
    (Path(repo) / LEDGER).parent.mkdir(parents=True, exist_ok=True)
    (Path(repo) / LEDGER).write_text(HEAD + "".join(f"- {n}\n" for n in names) + TAIL)


def _stub(repo, text=STUB):
    p = Path(repo) / "tools" / "spec_lint.py"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def _commit_all(repo, msg):
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg)


def _ledger_world(world, *, stub=STUB, conflict=True, code_conflict=False, fresh=True):
    """main and the branch each add a spec and the ledger row for it, so the ledger conflicts
    (`conflict`); with `conflict` false the branch adds a spec without touching the ledger
    (stale unless `fresh`). `stub` is the tools/spec_lint.py both sides carry (None = absent)."""
    root, nw = world["root"], world["nw"]
    _ledger_rows(root)
    if stub is not None:
        _stub(root, stub)
    _commit_all(root, "ledger base")
    _git(nw, "rebase", "-q", "main")
    _spec(nw, "0002-a.md")
    if conflict:
        _ledger_rows(nw, "0001-t.md", "0002-a.md")
        _spec(root, "0003-b.md")
        _ledger_rows(root, "0003-b.md")
        if code_conflict:
            (root / "shared.txt").write_text("main\n")
            (nw / "shared.txt").write_text("branch\n")   # same commit as the ledger
        _commit_all(root, "main spec")
    elif fresh:
        _ledger_rows(nw, "0001-t.md", "0002-a.md")
    _commit_all(nw, "branch spec")
    _git(root, "push", "-q", "origin", "main")
    return root, nw


def _prep(world, policy="merge", monkeypatch=None, **kw):
    if policy != "merge":
        _fake_gh_path(world, monkeypatch)
    pcfg = _pcfg(world, policy=policy, **kw) if policy != "merge" else _pcfg(world)
    _git(world["root"], "push", "-q", "origin", "main")
    return CL.prepare_close(world["cfg"], world["state"], pcfg)


def _fake_gh_path(world, monkeypatch):
    from test_agentic_close import _fake_gh
    _fake_gh(world["tmp"], monkeypatch)


def _findings(prep):
    return [p["title"] for p in _events(prep, "finding")]


def test_effective_regenerate_adds_the_implicit_ledger_entry(tmp_path):
    _ledger_rows(tmp_path)
    _stub(tmp_path)
    assert CL.effective_regenerate({}, tmp_path) == {LEDGER: LEDGER_CMD}
    other = {"regenerate": [{"path": "gen.txt", "cmd": CMD}]}
    assert CL.effective_regenerate(other, tmp_path) == {"gen.txt": CMD, LEDGER: LEDGER_CMD}


def test_effective_regenerate_needs_the_generator_and_the_markers(tmp_path):
    _ledger_rows(tmp_path)
    assert CL.effective_regenerate({}, tmp_path) == {}                  # no tools/spec_lint.py
    _stub(tmp_path)
    (tmp_path / LEDGER).write_text("# Ledger\nno markers\n")
    assert CL.effective_regenerate({}, tmp_path) == {}                  # no markers
    (tmp_path / LEDGER).write_text("# Ledger\n<!-- specs:begin -->\n")
    assert CL.effective_regenerate({}, tmp_path) == {}                  # no end marker
    (tmp_path / LEDGER).unlink()
    assert CL.effective_regenerate({}, tmp_path) == {}                  # no ledger
    declared = {"regenerate": [{"path": "gen.txt", "cmd": CMD}]}
    assert CL.effective_regenerate(declared, tmp_path) == {"gen.txt": CMD}


@pytest.mark.parametrize("spelling", [LEDGER, "./" + LEDGER])
def test_a_declared_ledger_entry_wins_and_is_not_duplicated(tmp_path, spelling):
    _ledger_rows(tmp_path)
    _stub(tmp_path)
    declared = {"regenerate": [{"path": spelling, "cmd": "make ledger"}]}
    assert CL.effective_regenerate(declared, tmp_path) == {spelling: "make ledger"}


def test_ledger_only_conflict_resolves_under_merge_with_an_empty_regenerate(world):
    _ledger_world(world)
    out = CL.close_campaign(world["cfg"], world["state"], _pcfg(world))
    assert out.next_state == "MERGED", out
    assert "generated files regenerated during rebase" in _findings(out)
    assert (world["root"] / LEDGER).read_text() == (
        HEAD + "- 0001-t.md\n- 0002-a.md\n- 0003-b.md\n" + TAIL)


@pytest.mark.parametrize("policy", ["merge", "pr", "draft-pr"])
def test_ledger_only_conflict_resolves_in_prepare_close_for_every_policy(world, monkeypatch,
                                                                         policy):
    _ledger_world(world)
    prep = _prep(world, policy, monkeypatch)
    assert prep.outcome is None, prep.outcome
    assert prep.target == ("main" if policy == "merge" else "origin/main")
    assert "generated files regenerated during rebase" in _findings(prep)
    assert (world["nw"] / LEDGER).read_text() == (
        HEAD + "- 0001-t.md\n- 0002-a.md\n- 0003-b.md\n" + TAIL)


def test_ledger_conflict_still_parks_without_spec_lint(world):
    _ledger_world(world, stub=None)
    prep = _prep(world)
    assert prep.outcome.park["reason"] == "rebase_conflict"
    assert LEDGER in prep.outcome.park["conflicts"]


def test_ledger_conflict_still_parks_without_markers(world):
    root, nw = world["root"], world["nw"]
    _stub(root)
    (root / "docs").mkdir()
    (root / LEDGER).write_text("# Ledger\nbase\n")
    _commit_all(root, "ledger base")
    _git(nw, "rebase", "-q", "main")
    (nw / LEDGER).write_text("# Ledger\nbranch\n")
    _commit_all(nw, "branch ledger")
    (root / LEDGER).write_text("# Ledger\nmain\n")
    _commit_all(root, "main ledger")
    prep = _prep(world)
    assert prep.outcome.park["reason"] == "rebase_conflict"
    assert LEDGER in prep.outcome.park["conflicts"]


def test_ledger_plus_code_conflict_parks_with_both_paths_and_restores_head(world):
    _ledger_world(world, code_conflict=True)
    head = G.rev_parse(world["nw"])
    prep = _prep(world)
    assert prep.outcome.park["reason"] == "rebase_conflict"
    assert set(prep.outcome.park["conflicts"]) == {LEDGER, "shared.txt"}
    assert G.rev_parse(world["nw"]) == head
    assert _git(world["nw"], "status", "--porcelain") == ""


def test_stale_ledger_gets_one_regenerate_commit_and_a_fresh_one_none(world):
    _ledger_world(world, conflict=False, fresh=False)
    prep = _prep(world)
    assert prep.outcome is None
    assert _commits(world["nw"]).count(
        f"nw({world['state']['id']}): regenerate generated files after rebase") == 1
    assert (world["nw"] / LEDGER).read_text() == HEAD + "- 0001-t.md\n- 0002-a.md\n" + TAIL


def test_fresh_ledger_adds_no_commit(world):
    _ledger_world(world, conflict=False, fresh=True)
    prep = _prep(world)
    assert prep.outcome is None and _events(prep, "commit") == []
    assert not any("regenerate generated files" in s for s in _commits(world["nw"]))


def test_failing_ledger_command_parks_regenerate_failed_and_restores_the_worktree(world):
    _ledger_world(world, stub=FAILING_STUB, conflict=False, fresh=False)
    prep = _prep(world)
    assert prep.outcome.park["reason"] == "regenerate_failed"
    assert _git(world["nw"], "status", "--porcelain") == ""
    assert not any("regenerate generated files" in s for s in _commits(world["nw"]))


def test_docs_only_allowlist_includes_the_ledger_only_when_the_entry_applies(world):
    root = world["root"]
    _ledger_rows(root)
    _stub(root)
    _commit_all(root, "ledger base")
    stale = HEAD + "- stale\n" + TAIL
    st, pcfg = _docs_only_world(world, {"docs/design/a.md": "x\n", LEDGER: stale})
    out = CL.close_campaign(world["cfg"], st, pcfg)
    assert out.next_state == "MERGED", out
    assert (root / LEDGER).read_text() == HEAD + TAIL                  # regenerated, not stale


def test_docs_only_refuses_the_ledger_when_there_is_no_generator(world):
    root = world["root"]
    _ledger_rows(root)
    _commit_all(root, "ledger base")
    st, pcfg = _docs_only_world(world, {"docs/design/a.md": "x\n",
                                        LEDGER: HEAD + "- stale\n" + TAIL})
    out = CL.close_campaign(world["cfg"], st, pcfg)
    assert out.next_state == "PARKED" and out.park["reason"] == "not_docs_only"
    assert LEDGER in out.park["detail"]


def test_spec_writer_regenerate_calls_still_read_only_the_declared_mapping():
    src = (Path(Q.__file__)).read_text()
    assert "effective_regenerate" not in src
    assert "IMPLICIT_LEDGER" not in src


REPO = Path(__file__).resolve().parent.parent


def test_real_spec_lint_passes_on_the_regenerated_ledger_of_a_rebased_conflict(world):
    """Two branches each add a spec; the rebase conflicts on the ledger; the result lints."""
    root, nw = world["root"], world["nw"]
    for rel in ("tools/spec_lint.py", "docs/specs/spec.schema.json"):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO / rel, root / rel)
    _ledger_rows(root)
    (root / LEDGER).write_text(                          # the real markers, for the real tool
        "# Ledger\n\n<!-- specs:begin (generated by tools/spec_lint.py --write-ledger — do not "
        "edit by hand) -->\n<!-- specs:end -->\n")

    def spec(repo, n):
        (Path(repo) / f"docs/specs/{n:04d}-s{n}.md").write_text(
            f"---\nid: SPEC-{n:04d}\ntitle: s{n}\nstatus: draft\nowner: user\n"
            f"created: 2026-11-02\nkind: policy\n---\n\n## Context\n\nc\n\n## Decision\n\nd\n\n"
            f"## Acceptance criteria\n\n- [ ] a\n\n## Test plan\n\nN/A\n\n"
            f"## Definition of done\n\n- [ ] x\n")

    def lint(repo, *args):
        return subprocess.run(["python3", "tools/spec_lint.py", *args], cwd=repo,
                              capture_output=True, text=True)

    (nw / "docs/specs/0001-t.md").unlink()                # world's SPEC_DONE is not lint-clean here
    _commit_all(nw, "drop world spec")
    _commit_all(root, "lint tooling")
    _git(nw, "rebase", "-q", "main")
    spec(nw, 2)
    assert lint(nw, "--write-ledger").returncode == 0
    _commit_all(nw, "branch spec")
    spec(root, 3)
    assert lint(root, "--write-ledger").returncode == 0
    _commit_all(root, "main spec")
    _git(root, "push", "-q", "origin", "main")
    state = dict(world["state"], spec_id=None, spec=None, kind="merge",
                 merge={"branch": "nw/c1", "head_at_request": G.rev_parse(nw),
                        "own_worktree": False})
    (world["tmp"] / "data" / "agentic" / "campaigns" / state["id"] / "spec_at_open.md").unlink()
    prep = CL.prepare_close(world["cfg"], state, _pcfg(world))
    assert prep.outcome is None, prep.outcome
    assert "generated files regenerated during rebase" in _findings(prep)
    assert lint(nw).returncode == 0, lint(nw).stdout
