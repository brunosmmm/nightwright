"""Close step (SPEC-0175): rebase → re-gate → ff-only merge, or push + PR hand-off.

This is where an autonomous loop can do real damage — the dogfood target is this repo and the
user's `nw` is an editable install of it — so the procedure never force-pushes, never
rewrites the base, never touches a dirty or off-base main checkout, re-runs every gate on the
exact tree it merges, and audits the spec's Definition of Done mechanically before any of it.
Pure Python over `gitops` / `gates` / `verify`; it returns the ledger events for the caller
(the queue, SPEC-0176) to append and the next state to record — it writes no campaign state
itself."""
from __future__ import annotations

import datetime as dt
import difflib
import json
import os
import posixpath
import re
import subprocess
from dataclasses import dataclass, field, replace
from pathlib import Path

import yaml

from ..specmeta import sections, split_frontmatter
from ..verify import verify_spec_file
from . import gates as G
from . import gitops as git
from . import project as P
from . import sentinel as SN
from . import store
from .gitops import GitError

MAX_CLOSE_ATTEMPTS = 2          # target captured → merged; base moved in between ⇒ retry
_FOOTER = "🤖 Generated with [Claude Code](https://claude.com/claude-code)"


@dataclass(frozen=True)
class CloseOutcome:
    next_state: str                       # MERGED | HANDED_OFF | TRIAGING | PARKED
    park: dict | None = None              # {reason, detail, resume_to}
    close: dict | None = None             # {mode, merged_sha, pr_url, gates_seq, target_sha}
    gate_run: G.GateRun | None = None     # the re-gate run (when it ran)
    synthetic_gate: dict | None = None    # the spec-dod failure that sent us to TRIAGING
    events: list = field(default_factory=list)   # [(type, payload)] for the caller to append
    attempts: int = 0                     # target-capture → merge attempts made
    retry: bool = False                   # CLOSING + redo the worktree phase (never persisted)
    gate_detail: str = ""                 # SPEC-0233: the red re-gate of a merge request, for its park
    held: str | None = None               # SPEC-0420: the close is held (red base); stays CLOSING


@dataclass(frozen=True)
class PreparedClose:
    """What the worktree phase (SPEC-0220) hands the finish phase. `outcome` is set when the
    phase already decided (TRIAGING / PARKED); otherwise `target`..`seq` describe the gated
    tree the finish phase merges or hands off."""
    outcome: CloseOutcome | None = None
    target: str = ""
    target_sha: str = ""
    head: str = ""                        # worktree HEAD after the rebase = the gated tree
    gate_run: G.GateRun | None = None
    seq: int | None = None
    policy: str = "merge"
    events: list = field(default_factory=list)
    pre_head: str = ""                    # SPEC-0221: worktree HEAD before the rebase started
    commits_since_request: int | None = None   # SPEC-0233: merge request only


def _commits_since_request(root, state: dict, tip: str) -> int | None:
    """Commits added to a merge request's branch after it was queued; None when the recorded
    head is no longer an ancestor of the pre-rebase tip."""
    recorded = (state.get("merge") or {}).get("head_at_request")
    if not recorded:
        return None
    anc = git.git(root, "merge-base", "--is-ancestor", recorded, tip, check=False)
    if anc.returncode != 0:
        return None
    return int(git.git(root, "rev-list", "--count", f"{recorded}..{tip}").stdout.strip() or 0)


def _gate_failure_detail(gate_run, target: str, target_sha: str) -> str:
    """The park detail of a red merge-request re-gate: the failing gates, their tails and the
    fact that the requester's branch now sits rebased onto the base."""
    failing = [r for r in gate_run.results if not r.ok]
    lines = ["gates failed: " + ", ".join(r.name for r in failing)]
    for r in failing:
        lines.append(f"--- {r.name}")
        lines.extend((r.tail or "").splitlines()[-20:])
    lines.append(f"branch rebased onto {target} {target_sha[:10]}")
    return "\n".join(lines)


def _park(reason, detail, resume_to, events, *, conflicts=None, target=None, target_sha=None,
          no_auto=False, **extra) -> CloseOutcome:
    payload = {"reason": reason, "detail": detail, "resume_to": resume_to}
    if no_auto:                           # the queue must not resume this park by itself (SPEC-0302)
        payload["no_auto"] = True
    if conflicts:
        payload.update(conflicts=conflicts, target=target, target_sha=target_sha)
    events.append(("park", payload))
    return CloseOutcome("PARKED", park=payload, events=events, **extra)


def _run_post_cmds(cmds, cwd, events) -> None:
    for cmd in cmds or []:
        r = subprocess.run(cmd, shell=True, cwd=str(cwd), capture_output=True, text=True)
        if r.returncode != 0:
            tail = "\n".join(((r.stdout or "") + (r.stderr or "")).splitlines()[-20:])
            events.append(("finding", {
                "persona": "close", "severity": "major", "gate": "post_cmds",
                "title": f"post command failed: {cmd}",
                "detail": f"exit {r.returncode}\n{tail}", "files": []}))


def spec_dod(worktree, spec_rel_path) -> tuple[bool, dict | None]:
    """(ok, synthetic_gate). `ok` means the spec in the worktree verifies and is `done`."""
    path = Path(worktree) / spec_rel_path
    try:
        audit = verify_spec_file(path, specs_dir=path.parent)
    except ValueError as e:
        audit = {"ok": False, "status": "", "checks": [{"name": "file", "status": "fail",
                                                        "detail": str(e)}]}
    failing = [f"{c['name']}: {c['detail']}" for c in audit["checks"] if c["status"] == "fail"]
    if audit.get("status") != "done":
        failing.insert(0, f"status: {audit.get('status') or 'missing'} (must be done)")
    if audit["ok"] and audit.get("status") == "done":
        return True, None
    return False, {"name": "spec-dod", "required": True, "ok": False, "exit_code": None,
                   "timed_out": False, "duration_s": 0.0, "log": "", "tail": "\n".join(failing)}


def split_check(worktree, state: dict) -> list[str]:
    """SPEC-0290: problems with a split campaign's result (empty = ok). Every part file exists
    and lints (its status is not judged), and the original, found at `state.split.original_rel`,
    is `superseded` by part 1."""
    sp = state.get("split") or {}
    worktree = Path(worktree)
    out = []
    for i, part in enumerate(sp.get("parts") or [], 1):
        rel = part.get("rel_path")
        if not part.get("spec_id") or not rel or not (worktree / rel).is_file():
            out.append(f"part {i} ({part.get('title')}): spec file missing")
            continue
        path = worktree / rel
        try:
            audit = verify_spec_file(path, specs_dir=path.parent)
            pfm, _ = split_frontmatter(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            out.append(f"part {i} {part['spec_id']}: {e}")
            continue
        if i > 1:                       # the chain: part k depends on part k-1
            want = [sp["parts"][i - 2].get("spec_id")]
            got = (pfm or {}).get("depends_on") or []
            if (got if isinstance(got, list) else [got]) != want:
                out.append(f"part {i} {part['spec_id']}: depends_on is {got!r}, not {want!r}")
        out += [f"part {i} {part['spec_id']}: {c['name']}: {c['detail']}"
                for c in audit["checks"] if c["status"] == "fail" and c["name"] != "status"]
    try:
        ofm, _ = split_frontmatter((worktree / sp["original_rel"]).read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError) as e:
        return out + [f"original {sp.get('original')}: cannot read {sp.get('original_rel')}: {e}"]
    ofm = ofm if isinstance(ofm, dict) else {}
    first = ((sp.get("parts") or [{}])[0]).get("spec_id")
    if ofm.get("status") != "superseded" or ofm.get("superseded_by") != first:
        out.append(f"original {sp.get('original')} is {ofm.get('status')!r} "
                   f"(superseded_by {ofm.get('superseded_by')!r}); it must be superseded by {first}")
    return out


# ---- spec-change guard (SPEC-0186) -----------------------------------------------------------

SPEC_AT_OPEN = "spec_at_open.md"
_ALLOWED_SUBSECTION_RE = re.compile(r"^###\s+(Test record|As shipped)\b", re.IGNORECASE)
_HEADING_RE = re.compile(r"^(#{1,6})\s")
_TICK_RE = re.compile(r"^(\s*[-*]\s+)\[[xX]\]")
_DRIFT_TAIL_LINES = 120


def _normalize_spec(text: str) -> str:
    """The spec with every change an implementer is allowed to make erased: frontmatter
    `status` / `updated`, checkbox ticks, `### Test record` / `### As shipped` subsections and
    the `## Clock Log` section. Two specs normalize equal ⇔ no governing text changed."""
    fm, body = split_frontmatter(text)
    if fm is None:
        fm, body = {}, text
    fm = {k: v for k, v in fm.items() if k not in ("status", "updated")}
    out = ["---", yaml.safe_dump(fm, sort_keys=True, default_flow_style=False).strip(), "---"]
    skip_level = None          # heading level whose section we are skipping
    for line in body.splitlines():
        m = _HEADING_RE.match(line)
        if m:
            level = len(m.group(1))
            if skip_level is not None and level <= skip_level:
                skip_level = None
            if skip_level is None:
                if level == 3 and _ALLOWED_SUBSECTION_RE.match(line):
                    skip_level = 3
                    continue
                if level == 2 and line.strip().lower() == "## clock log":
                    skip_level = 2
                    continue
        if skip_level is not None:
            continue
        out.append(_TICK_RE.sub(r"\1[ ]", line).rstrip())
    text = "\n".join(out)
    return re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"


def spec_drift(old: str, new: str) -> list[str]:
    """Unified-diff hunks of governing changes between the accepted spec (`old`) and the
    worktree spec (`new`); empty ⇔ only allowed edits."""
    a = _normalize_spec(old).splitlines()
    b = _normalize_spec(new).splitlines()
    if a == b:
        return []
    hunks, cur = [], []
    for line in difflib.unified_diff(a, b, "accepted", "worktree", n=1, lineterm=""):
        if line.startswith(("---", "+++")):
            continue
        if line.startswith("@@"):
            if cur:
                hunks.append("\n".join(cur))
            cur = [line]
        else:
            cur.append(line)
    if cur:
        hunks.append("\n".join(cur))
    return hunks


def spec_guard(cfg, state: dict) -> tuple[bool, dict | None, list]:
    """(ok, synthetic_gate, events). Compares the worktree spec with the open-time copy."""
    rel = (state.get("spec") or {}).get("rel_path") or ""
    worktree = (state.get("repo") or {}).get("worktree") or ""
    copy = store.campaign_dir(cfg, state["id"]) / SPEC_AT_OPEN
    if not copy.is_file():
        return True, None, [("finding", {
            "persona": "close", "severity": "info", "gate": "spec-changed",
            "title": "spec-changed guard skipped: no open-time copy of the spec",
            "detail": str(copy), "files": [rel]})]
    try:
        new = (Path(worktree) / rel).read_text(encoding="utf-8")
    except OSError as e:
        new = f"(spec unreadable: {e})"
    hunks = spec_drift(copy.read_text(encoding="utf-8"), new)
    if not hunks:
        return True, None, []
    diff = "\n".join(hunks).splitlines()
    tail = "\n".join(diff[:_DRIFT_TAIL_LINES]
                     + ([f"... ({len(diff) - _DRIFT_TAIL_LINES} more lines)"]
                        if len(diff) > _DRIFT_TAIL_LINES else []))
    gate = {"name": "spec-changed", "required": True, "ok": False, "exit_code": None,
            "timed_out": False, "duration_s": 0.0, "log": "", "tail": tail}
    return False, gate, [("finding", {
        "persona": "close", "severity": "must_fix", "gate": "spec-changed",
        "title": "spec text changed outside allowed sections",
        "detail": tail, "files": [rel]})]


def pr_body(state: dict, spec_fm: dict, spec_sections: dict, gate_run: G.GateRun | None) -> str:
    """Pure function of its inputs: the PR description for policy pr / draft-pr."""
    spec_id = state.get("spec_id") or spec_fm.get("id") or "?"
    title = spec_fm.get("title") or ""
    rel = (state.get("spec") or {}).get("rel_path") or ""
    counters = state.get("counters") or {}
    cost = state.get("cost") or {}
    models = sorted((cost.get("by_persona") or {}).keys())
    lines = [f"# {spec_id}: {title}", "",
             f"Spec: `{rel}` ({spec_id}, status {spec_fm.get('status') or '?'})", "",
             "## Summary", "", (spec_sections.get("Decision") or "").strip() or "(no Decision section)",
             "", "## Acceptance criteria", "",
             (spec_sections.get("Acceptance criteria") or "").strip() or "(none)", "",
             "## Gates", ""]
    if gate_run:
        lines += ["| gate | result | duration |", "|---|---|---|"]
        for r in gate_run.results:
            res = "timeout" if r.timed_out else ("ok" if r.ok else f"exit {r.exit_code}")
            lines.append(f"| {r.name} | {res} | {r.duration_s:.1f}s |")
    else:
        lines.append("(no gate run recorded)")
    lines += ["", "## Campaign", "",
              f"- id: `{state.get('id')}`",
              f"- loops: {counters.get('loops', 0)} · runs: {counters.get('runs', 0)}",
              f"- cost: ${float(cost.get('total_usd') or 0):.2f} (cap ${float(cost.get('cap_usd') or 0):.2f})",
              f"- personas: {', '.join(models) if models else '(none)'}",
              "", _FOOTER, ""]
    return "\n".join(lines)


def _is_merge_request(state: dict) -> bool:
    """SPEC-0233: a campaign that merges an existing branch (no spec, no personas)."""
    return state.get("kind") == "merge"


def _is_spec_less(state: dict) -> bool:
    """SPEC-0290: no spec guard and no spec DoD on the campaign's own spec: a merge request has
    none (SPEC-0233) and a split campaign's original is superseded, not delivered. SPEC-0420: a
    red-base fix (`fixforward`, `revert`) has no spec either."""
    return state.get("kind") in ("merge", "split", "fixforward", "revert")


def close_hold(cfg, state: dict, project_cfg: dict | None) -> str | None:
    """SPEC-0420: why this close is held while its project's base is red, or None (the
    campaign fixes the red base, is a docs-only merge, or nothing is red). Never raises."""
    from . import redmain as RM
    try:
        return RM.holding(cfg, state.get("project"), state, project_cfg=project_cfg)
    except (OSError, ValueError):
        return None


_TEST_FILE_RE = re.compile(r"(^|/)(tests?/.*\.py|test_[^/]*\.py|[^/]*_test\.py|conftest\.py)$")
_TEST_DEF_RE = re.compile(r"^\s*(async\s+)?def\s+test_\w*\s*\(")
_WEAKEN_RE = re.compile(r"pytest\.mark\.(skip|skipif|xfail)\b|importorskip\(|pytest\.skip\(|"
                        r"collect_ignore")


def fixforward_weakens(worktree, target_sha) -> list[str]:
    """SPEC-0420 §5: how a fix-forward diff (`target_sha...HEAD`) weakens the tests, else []:
    a deleted test file, fewer `def test_` definitions in the test files, an added skip / skipif
    / xfail mark, `importorskip(`, `pytest.skip(` or `collect_ignore` in a test file, or any
    removed line in a test module marked `tree_guard` (SPEC-0421)."""
    rng = f"{target_sha}...HEAD"
    out = []
    names = git.git(worktree, "diff", "--name-status", "--no-renames", rng).stdout.splitlines()
    tests = []
    for line in names:
        parts = line.split("\t")
        if len(parts) < 2 or not _TEST_FILE_RE.search(parts[-1]):
            continue
        status, rel = parts[0][:1], parts[-1]
        if status == "D":
            out.append(f"deletes test file {rel}")
        tests.append(rel)
    if not tests:
        return out
    diff = git.git(worktree, "diff", "-U0", "--no-renames", rng, "--", *tests).stdout
    added = removed = 0
    cur, guarded, guard_cache = None, set(), {}
    for line in diff.splitlines():
        if line.startswith("+++ ") or line.startswith("--- "):
            if line.startswith("+++ "):
                cur = line[6:] if line.startswith("+++ b/") else None
            elif line.startswith("--- a/"):
                cur = line[6:]
            continue
        if line.startswith("+"):
            body = line[1:]
            if _TEST_DEF_RE.match(body):
                added += 1
            m = _WEAKEN_RE.search(body)
            if m:
                out.append(f"adds {m.group(0).rstrip('(')} in {cur}: {body.strip()[:120]}")
        elif line.startswith("-"):
            body = line[1:]
            if _TEST_DEF_RE.match(body):
                removed += 1
            if cur and cur not in guard_cache:
                shown = git.git(worktree, "show", f"{target_sha}:{cur}", check=False)
                guard_cache[cur] = shown.returncode == 0 and "tree_guard" in shown.stdout
            if cur and guard_cache.get(cur):
                guarded.add(cur)
    if removed > added:
        out.append(f"removes {removed - added} test function(s) net ({removed} removed, "
                   f"{added} added)")
    out += [f"removes lines from tree_guard module {g}" for g in sorted(guarded)]
    return out


def _prune_job_refs(root, state: dict) -> None:
    """SPEC-0344: the saved refs of failed persona write-backs go when the campaign closes."""
    from . import remote_personas as RPS
    RPS.prune_refs(root, state["id"])


def _cleanup(root, worktree, branch, project_cfg, mode, events, *, merge_request=None) -> None:
    """`merge_request` is the campaign's `merge` block for a SPEC-0233 merge request: only a
    worktree nightwright created is removed, and the requester's branch is never deleted."""
    wcfg = project_cfg.get("worktrees") or {}
    mcfg = project_cfg.get("merge") or {}
    policy = wcfg.get("cleanup", "on_merge")
    if policy == "never" or (policy == "on_merge" and mode != "merge"):
        return
    if merge_request is not None and not merge_request.get("own_worktree"):
        return
    try:
        git.worktree_remove(root, worktree, force=True)
    except GitError as e:
        events.append(("finding", {"persona": "close", "severity": "minor", "gate": "cleanup",
                                   "title": "worktree removal failed", "detail": e.tail,
                                   "files": []}))
        return
    if mode == "merge" and mcfg.get("delete_branch", True) and merge_request is None:
        try:
            git.branch_delete(root, branch)
        except GitError as e:
            events.append(("finding", {"persona": "close", "severity": "minor",
                                       "gate": "cleanup", "title": "branch delete failed",
                                       "detail": e.tail, "files": []}))



def _close_cfg(state: dict, project_cfg: dict) -> dict:
    repo = state.get("repo") or {}
    mcfg = project_cfg.get("merge") or {}
    return {"root": repo["root"], "worktree": repo["worktree"], "branch": repo["branch"],
            "mcfg": mcfg, "policy": mcfg.get("policy", "merge"),
            "base": mcfg.get("base") or project_cfg.get("base") or repo.get("base") or "main",
            "remote": mcfg.get("remote") or "origin",
            "rel": (state.get("spec") or {}).get("rel_path") or ""}


def _merge_policy_park(policy, events) -> CloseOutcome:
    """SPEC-0233: a merge request only ever merges locally; the project's policy may have
    changed to a pr policy since it was queued, and nothing is pushed on its behalf."""
    return _park("human", f"merge requests need merge.policy: merge, but the project is "
                 f"on {policy!r}; restore the policy or abandon the request", "CLOSING", events)


def _rebase_in_progress(worktree) -> bool:
    for name in ("rebase-merge", "rebase-apply"):
        r = git.git(worktree, "rev-parse", "--git-path", name, check=False)
        path = Path((r.stdout or "").strip())
        if r.returncode == 0 and path.name and not path.is_absolute():
            path = Path(worktree) / path
        if r.returncode == 0 and path.exists():
            return True
    return False


def _abort_leftover_rebase(worktree) -> None:
    """A hard kill mid-rebase leaves one in progress; the redo must start clean."""
    if _rebase_in_progress(worktree):
        git.git(worktree, "rebase", "--abort", check=False)


# SPEC-0399: the ledger block is a pure function of the spec frontmatter, so a rebase never merges
# it textually: nw regenerates it whether or not the project declared it in `merge.regenerate`.
IMPLICIT_LEDGER_PATH = "docs/LEDGER.md"
IMPLICIT_LEDGER_CMD = "python3 tools/spec_lint.py --write-ledger"
# the generated block's region, exactly as `tools/spec_lint.py --write-ledger` requires it (its
# REGION_RE): the full opening line, then the closing marker. Anything looser (the bare
# `<!-- specs:begin -->`, the markers reversed) makes the tool exit 1 on every close.
_LEDGER_BEGIN = ("<!-- specs:begin (generated by tools/spec_lint.py --write-ledger — "
                 "do not edit by hand) -->")
_LEDGER_END = "<!-- specs:end -->"
_LEDGER_REGION_RE = re.compile(re.escape(_LEDGER_BEGIN) + r".*?" + re.escape(_LEDGER_END), re.S)


def effective_regenerate(merge_cfg, worktree) -> dict[str, str]:
    """The declared `merge.regenerate` mapping plus the implicit ledger entry (SPEC-0399): added
    only when the project did not declare the ledger path itself, the worktree has
    `tools/spec_lint.py` and `docs/LEDGER.md` carries the generated block's full begin line then
    its end marker (without them `--write-ledger` exits 1, which would turn a clean rebase into a
    `regenerate_failed` park)."""
    regen = {r["path"]: r["cmd"] for r in (merge_cfg.get("regenerate") or [])}
    if any(posixpath.normpath(p) == IMPLICIT_LEDGER_PATH for p in regen):
        return regen
    worktree = Path(worktree)
    ledger = worktree / IMPLICIT_LEDGER_PATH
    if not (worktree / "tools" / "spec_lint.py").is_file() or not ledger.is_file():
        return regen
    try:
        text = ledger.read_text(errors="replace")
    except OSError:
        return regen
    if _LEDGER_REGION_RE.search(text):
        regen[IMPLICIT_LEDGER_PATH] = IMPLICIT_LEDGER_CMD
    return regen


def _regenerate_after_rebase(worktree, regen, events, campaign_id, own=True) -> str | None:
    """SPEC-0255: run every declared `merge.regenerate` command in the rebased worktree and commit
    exactly the declared paths that changed, so the gated head is the merged head. Returns None,
    or the `regenerate_failed` park detail (a failing command or git step, or undeclared files it
    dirtied). A nightwright-owned worktree is then put back to HEAD; an adopted one (`own` false) is the
    requester's, so nothing is reset or deleted there (only our staging is undone) and the
    resume parks `worktree_dirty`, as after a red re-gate."""
    if not regen:
        return None
    declared = list(regen)
    before = {ln[3:] for ln in git.status_porcelain(worktree, untracked=True)}

    def put_back():
        if not own:
            git.git(worktree, "reset", "-q", "--", *declared, check=False)
        elif not before:
            git.git(worktree, "reset", "-q", "--hard", "HEAD", check=False)
            git.git(worktree, "clean", "-fdq", check=False)

    try:
        for path, cmd in regen.items():
            git.run_regenerate(worktree, cmd)
        git.git(worktree, "add", "--", *declared)
        # SPEC-0266: a command that staged files itself must not get them into the commit
        extra = {p for p in git.git(worktree, "diff", "--cached", "--name-only",
                                          "--no-renames", "-z")
                 .stdout.split("\0") if p} - set(declared)
        if extra:
            git.git(worktree, "reset", "-q", "--", *sorted(extra), check=False)
            put_back()
            return ("regenerate commands staged undeclared files: "
                    + ", ".join(sorted(extra)[:10]))
        staged = git.git(worktree, "diff", "--cached", "--quiet", check=False).returncode != 0
        if staged:
            git.git(worktree, "commit", "-q", "-m",
                    f"nw({campaign_id}): regenerate generated files after rebase")
    except GitError as e:
        put_back()
        return f"regenerate failed: {e.tail}"
    if staged:
        events.append(("commit", {"sha": git.rev_parse(worktree),
                                  "message": "regenerate generated files after rebase"}))
        changed = git.git(worktree, "diff", "--name-only", "HEAD~1", "HEAD").stdout.split()
        events.append(("finding", {
            "persona": "close", "severity": "info", "gate": "regenerate",
            "title": "generated files regenerated after rebase and committed",
            "detail": "committed: " + ", ".join(changed), "files": changed}))
    stray = [ln[3:] for ln in git.status_porcelain(worktree, untracked=True)
             if ln[3:] not in before]
    if stray:
        put_back()
        return ("regenerate commands left undeclared files changed: " + ", ".join(stray[:10]))
    return None


def _skip_unaffected_gates(gate_cfg, worktree, target_sha, events):
    """SPEC-0350: drop close-tier gates whose `paths` globs match none of the paths the rebased
    branch changes against `target_sha`, recording a `gate.skipped` event for each. Fails closed:
    when the changed paths cannot be computed (or are empty) every gate runs."""
    if not any(g.get("paths") for g in gate_cfg.get("gates") or []):
        return gate_cfg
    try:
        changed = git.changed_files(worktree, target_sha, renames=False)
    except Exception:
        return gate_cfg
    close_gates = P.gates_for(gate_cfg, "close")
    _, skipped = P.split_gates_by_paths(close_gates, changed)
    if not skipped:
        return gate_cfg
    drop = {g["name"] for g, _ in skipped}
    for g, pats in skipped:
        events.append(("gate.skipped", {"gate": g["name"], "patterns": pats,
                                        "changed_count": len(changed),
                                        "required": g.get("required", True)}))
    return {**gate_cfg, "gates": [g for g in gate_cfg["gates"] if g["name"] not in drop]}


# ---- close-tier reuse of a green loop gate (SPEC-0359) ----------------------------------------

AFFECTED_TOOL = "tools/affected_tests.py"     # SPEC-0357: `--json [--closure] --changed`
AFFECTED_TIMEOUT_S = 60
REUSE_RULES = ("R1", "R2", "R3", "R4", "R5", "R6", "R7")


class _Decline(Exception):
    def __init__(self, rule: str, detail: str):
        super().__init__(f"{rule}: {detail}")
        self.rule, self.detail = rule, detail


_TOOL_PATH = re.compile(r"(?<![\w./-])(tools/[\w./-]+\.py)\b")


def _reuse_machinery(*gates) -> set[str]:
    """The paths whose change would let a branch judge its own reuse: the selector
    (`AFFECTED_TOOL`) and every `tools/*.py` a gate's command runs."""
    out = {AFFECTED_TOOL}
    for g in gates:
        out |= set(_TOOL_PATH.findall(str((g or {}).get("cmd") or "")))
    return out


def _close_full_env() -> bool:
    return os.environ.get("NW_CLOSE_FULL", "").strip().lower() not in ("", "0", "false", "no")


def _affected(worktree, changed, *, closure=False) -> dict:
    """`tools/affected_tests.py --json [--closure]` for `changed` on the worktree's tree (H).
    No changed path selects nothing. RuntimeError on a missing tool, a non-zero exit, a
    timeout or unparseable output."""
    if not changed:
        return {"full": False, "tests": [], "reason": "no changed paths", "closure": []}
    tool = Path(worktree) / AFFECTED_TOOL
    if not tool.is_file():
        raise RuntimeError(f"{AFFECTED_TOOL} not found in the worktree")
    argv = ["python3", str(tool), "--repo", str(worktree), "--json"]
    argv += ["--closure"] if closure else []
    argv += [f"--changed={p}" for p in changed]
    env = {k: v for k, v in os.environ.items() if k != "NW_GATE_REPORT"}
    what = f"{AFFECTED_TOOL} --json{' --closure' if closure else ''}"
    try:
        r = subprocess.run(argv, cwd=str(worktree), capture_output=True, text=True, env=env,
                           timeout=AFFECTED_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"{what} timed out after {AFFECTED_TIMEOUT_S}s")
    except OSError as e:
        raise RuntimeError(f"{what} failed to start: {e}")
    if r.returncode != 0:
        tail = " | ".join((r.stderr or r.stdout or "").strip().splitlines()[-3:])
        raise RuntimeError(f"{what} exited {r.returncode}: {tail}"[:500])
    lines = [ln for ln in (r.stdout or "").splitlines() if ln.strip()]
    try:
        rec = json.loads(lines[-1])
    except (IndexError, ValueError):
        raise RuntimeError(f"{what} printed no JSON report")
    if not isinstance(rec, dict) or "full" not in rec:
        raise RuntimeError(f"{what} printed an unexpected report")
    return rec


def _gated_seconds_ago(started_at, now=None) -> float | None:
    try:
        at = dt.datetime.fromisoformat(str(started_at).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None
    return (dt.datetime.now().timestamp() if now is None else now) - at


def _names(paths, n=5) -> str:
    paths = sorted(paths)
    return ", ".join(paths[:n]) + (f" (+{len(paths) - n} more)" if len(paths) > n else "")


def _reuse_evidence(cfg, state, project_cfg, gate, *, policy, root, worktree, target_sha, head,
                    now=None) -> dict:
    """Rules R1-R7 of SPEC-0359 §3 for one close gate with `reuse_from`, in order: the
    `gate.reused` payload, or `_Decline(rule, detail)` naming the first rule that fails."""
    src_name = gate["reuse_from"]
    close_cfg, _ = SN.settings(project_cfg)
    # R1 enabled
    if _is_merge_request(state):
        raise _Decline("R1", "merge requests have no loop tier")
    if policy != "merge":
        raise _Decline("R1", f"merge.policy is {policy!r}; reuse needs merge")
    if close_cfg.get("force_full"):
        raise _Decline("R1", "close.force_full is set")
    if _close_full_env():
        raise _Decline("R1", "NW_CLOSE_FULL is set")
    blocker = SN.reuse_blocker(cfg, state.get("project"), root=root, project_cfg=project_cfg,
                               now=now)
    if blocker:
        raise _Decline("R1", blocker)
    # R2 green source: a full run of this gate that was red in this campaign is not stood in
    # for by a later loop green (its selection may miss what failed) until a full run is green
    red = (state.get("close_red") or {}).get(gate["name"]) if isinstance(
        state.get("close_red"), dict) else None
    if red:
        raise _Decline("R2", f"the full {gate['name']!r} close run was red at "
                             f"{str(red.get('head') or '?')[:10]}; reuse waits for a green "
                             f"full run")
    lg = state.get("last_green_loop")
    if not isinstance(lg, dict) or not lg.get("ok"):
        raise _Decline("R2", "no green loop run recorded")
    res = next((r for r in lg.get("results") or []
                if isinstance(r, dict) and r.get("name") == src_name), None)
    if res is None or not res.get("ok"):
        raise _Decline("R2", f"the last green loop run has no green {src_name!r} result")
    report = res.get("report")
    if not isinstance(report, dict):
        raise _Decline("R2", f"the {src_name!r} result has no selection report")
    g = lg.get("head")
    if not g or report.get("head_sha") != g:
        raise _Decline("R2", f"report head_sha {str(report.get('head_sha'))[:10]} is not the "
                             f"gated head {str(g)[:10]}")
    if git.git(worktree, "cat-file", "-e", f"{g}^{{commit}}", check=False).returncode != 0:
        raise _Decline("R2", f"gated head {g[:10]} is not a valid commit")
    # R3 branch unchanged
    try:
        delta = sorted(p for p in git.git(worktree, "diff", "--name-only", "-z", "--no-renames",
                                          g, head).stdout.split("\0") if p)
        branch = sorted(git.changed_files(worktree, target_sha, renames=False))
    except GitError as e:
        raise _Decline("R3", f"git diff failed: {e.tail}")
    both = set(delta) & set(branch)
    if both:
        raise _Decline("R3", f"branch files differ from the gated tree: {_names(both)}")
    # R4 main delta mappable
    nw = [p for p in delta if p.startswith(".nw/")]
    if nw:
        raise _Decline("R4", f"the main delta changes gate definitions: {_names(nw)}")
    # the selector that decides R4-R6 (and chose the loop gate's tests) must be the one that
    # gated the base: a branch or a main delta that changes it is not judged by it
    src_gate = next((x for x in project_cfg.get("gates") or []
                     if isinstance(x, dict) and x.get("name") == src_name), None)
    tooling = (set(delta) | set(branch)) & _reuse_machinery(gate, src_gate)
    if tooling:
        raise _Decline("R4", f"the change touches the reuse tooling: {_names(tooling)}")
    try:
        sel_d = _affected(worktree, delta)
    except RuntimeError as e:
        raise _Decline("R4", str(e))
    if sel_d.get("full"):
        raise _Decline("R4", f"the main delta selects the full suite: {sel_d.get('reason')}")
    # R5 selection did not grow
    try:
        sel_b = _affected(worktree, branch)
    except RuntimeError as e:
        raise _Decline("R5", str(e))
    if not report.get("full"):
        if sel_b.get("full"):
            raise _Decline("R5", f"the branch now selects the full suite: {sel_b.get('reason')}")
        grown = set(sel_b.get("tests") or []) - set(report.get("tests") or [])
        if grown:
            raise _Decline("R5", f"the branch selection grew: {_names(grown)}")
    # R6 no interaction
    if delta and branch:
        for changed, other, what in ((branch, delta, "the branch's tests read main's changes"),
                                     (delta, branch, "main's tests read the branch's changes")):
            try:
                cl = _affected(worktree, changed, closure=True).get("closure")
            except RuntimeError as e:
                raise _Decline("R6", str(e))
            if cl is None:
                raise _Decline("R6", f"{what}: the selection is full, no closure")
            hit = set(cl) & set(other)
            if hit:
                raise _Decline("R6", f"{what}: {_names(hit)}")
    # R7 bounded age
    age = _gated_seconds_ago(lg.get("started_at"), now)
    if age is None or age >= float(close_cfg["reuse_max_age_s"]):
        raise _Decline("R7", f"the green loop run is {'of unknown age' if age is None else f'{int(age)}s old'}"
                             f" (max {close_cfg['reuse_max_age_s']}s)")
    if len(delta) > int(close_cfg["reuse_max_delta"]):
        raise _Decline("R7", f"{len(delta)} paths differ (max {close_cfg['reuse_max_delta']})")
    ev = {"gate": gate["name"], "from_gate": src_name, "from_gate_seq": lg.get("gate_seq"),
          "gated_head": g, "close_head": head, "target_sha": target_sha,
          "delta_count": len(delta), "selection_count": len(sel_b.get("tests") or []),
          "rules": {r: True for r in REUSE_RULES},
          "full_source": bool(report.get("full"))}
    if sel_b.get("reason"):
        ev["selection_reason"] = sel_b["reason"]
    return ev


def note_close_gates(state: dict, gate_run) -> None:
    """SPEC-0359: record on the campaign state which close gates a full close run saw red
    (`close_red`: name -> {head, gate_seq}); a later green full run of a gate removes it. Rule R2
    declines reuse for a gate listed there, so a triage `retry_gate` whose loop gate goes green
    again cannot make the next close skip the full suite this campaign just saw fail."""
    if gate_run is None:
        return
    red = dict(state.get("close_red") or {}) if isinstance(state.get("close_red"), dict) else {}
    for r in gate_run.results:
        if r.interrupted:
            continue
        if r.ok:
            red.pop(r.name, None)
        else:
            red[r.name] = {"head": gate_run.head, "gate_seq": gate_run.seq}
    state["close_red"] = red or None


def _reuse_loop_green(cfg, state, project_cfg, gate_cfg, *, policy, root, worktree, target_sha,
                      head, events, now=None) -> tuple[dict, list]:
    """SPEC-0359: drop each close gate with `reuse_from` whose rebase is clean by R1-R7,
    recording `gate.reused`; record `gate.reuse_declined` (the first failing rule) for the
    others, which run. Evidence only: `gate_reuse.json` is not touched here (the counter moves
    when the close merges). Returns the gate config to run and the reused gate names."""
    reusers = [g for g in gate_cfg.get("gates") or []
               if g.get("reuse_from") and P.gate_when(g) == "close"]
    reused = []
    for g in reusers:
        try:
            ev = _reuse_evidence(cfg, state, project_cfg, g, policy=policy, root=root,
                                 worktree=worktree, target_sha=target_sha, head=head, now=now)
        except _Decline as d:
            events.append(("gate.reuse_declined", {"gate": g["name"], "rule": d.rule,
                                                   "detail": d.detail}))
            continue
        except Exception as e:                  # fails closed: the gate runs
            events.append(("gate.reuse_declined", {"gate": g["name"], "rule": "R1",
                                                   "detail": f"reuse check failed: {e!r}"}))
            continue
        events.append(("gate.reused", ev))
        reused.append(g["name"])
    if not reused:
        return gate_cfg, []
    return {**gate_cfg, "gates": [g for g in gate_cfg["gates"] if g["name"] not in reused]}, reused


def prepare_close(cfg, state: dict, project_cfg: dict, *, gate_seq=None,
                  stop=None) -> PreparedClose:
    """The worktree phase (SPEC-0220): spec guard, spec DoD, snapshot commit, target capture,
    rebase (with regenerate resolution) and the re-gate. Its only side effects are in the
    campaign worktree and branch (and `fetch` into shared refs), so it may run on a worker
    thread and be discarded and redone."""
    events: list = []
    c = _close_cfg(state, project_cfg)
    root, worktree, base, policy, remote = c["root"], c["worktree"], c["base"], c["policy"], c["remote"]
    mcfg, rel = c["mcfg"], c["rel"]

    pre_head = ""

    def decided(outcome: CloseOutcome) -> PreparedClose:
        return PreparedClose(outcome=outcome, policy=policy, events=events, pre_head=pre_head)

    held = close_hold(cfg, state, project_cfg)       # SPEC-0420: red base, nothing touched
    if held:
        return decided(CloseOutcome("CLOSING", held=held, events=events))
    merge_request = _is_merge_request(state)
    if merge_request and policy != "merge":
        return decided(_merge_policy_park(policy, events))
    if merge_request and not (state.get("merge") or {}).get("own_worktree") \
            and _rebase_in_progress(worktree):
        # SPEC-0233 G3: a rebase in the requester's checkout is theirs, not a nightwright crash leftover;
        # aborting it would reset their partly resolved conflicts
        return decided(_park("worktree_dirty", "rebase in progress in the requester's "
                             f"worktree {worktree}; finish or abort it, then resume",
                             "CLOSING", events))
    _abort_leftover_rebase(worktree)

    if merge_request:
        # SPEC-0233 G3: the requester may have switched their checkout while we waited; never
        # rebase or gate a branch other than the requested one
        cur = git.current_branch(worktree)
        if cur != state["repo"]["branch"]:
            return decided(_park("worktree_dirty", f"worktree {worktree} is on "
                                 f"{cur or 'a detached HEAD'}, not {state['repo']['branch']}",
                                 "CLOSING", events))

    if state.get("kind") == "split":
        problems = split_check(worktree, state)
        if problems:
            events.append(("finding", {"persona": "close", "severity": "must_fix",
                                       "gate": "split-check", "title": "split check failed",
                                       "detail": "\n".join(problems), "files": []}))
            return decided(_park("spec_write_failed", "split check failed: " + "; ".join(problems),
                                 "SPEC_WRITING", events))
    if not _is_spec_less(state):
        # 0. spec-change guard (SPEC-0186): the governing text must be what was accepted
        ok, synthetic, guard_events = spec_guard(cfg, state)
        events.extend(guard_events)
        if not ok:
            return decided(CloseOutcome("TRIAGING", synthetic_gate=synthetic, events=events))

        # 1. spec DoD in the worktree
        ok, synthetic = spec_dod(worktree, rel)
        if not ok:
            events.append(("finding", {"persona": "close", "severity": "must_fix",
                                       "gate": "spec-dod",
                                       "title": "spec Definition of Done not met",
                                       "detail": synthetic["tail"], "files": [rel]}))
            return decided(CloseOutcome("TRIAGING", synthetic_gate=synthetic, events=events))

    # 2. worktree clean
    if not git.is_clean(worktree, untracked=True):
        if merge_request:
            # SPEC-0233: the requester's unfinished work is never committed for them, and
            # untracked files never reach the rebase or the re-gate
            dirty = git.status_porcelain(worktree, untracked=True)
            return decided(_park("worktree_dirty", "worktree has uncommitted or untracked "
                                 "changes: " + ", ".join(dirty[:10]), "CLOSING", events))
        sha = git.commit_all(worktree, f"nw({state.get('id')}): close-time snapshot")
        events.append(("commit", {"sha": sha, "message": "close-time snapshot"}))
    pre_rebase = git.rev_parse(worktree) if merge_request else None

    # 3. target
    try:
        if policy == "merge":
            target = base
            target_sha = git.rev_parse(root, base)
        else:
            git.fetch(root, remote, base)
            target = f"{remote}/{base}"
            target_sha = git.rev_parse(root, target)
    except GitError as e:
        return decided(_park("open_failed", f"cannot resolve {policy} target: {e.tail}",
                             "CLOSING", events))

    # 4. rebase
    regen = effective_regenerate(mcfg, worktree)                     # SPEC-0399
    pre_head = git.rev_parse(worktree)
    rb = git.rebase(worktree, target_sha, regenerate=regen)
    if not rb.ok:
        if rb.head != pre_head or git.rev_parse(worktree) != pre_head:
            raise GitError(["git", "rebase", target_sha], 1,
                           f"worktree HEAD is not the pre-rebase {pre_head} after the abort")
        # SPEC-0221: the extra keys let the queue dispatch the resolver without re-parsing
        return decided(_park("rebase_conflict",
                             f"rebase onto {target} ({target_sha[:10]}) conflicts: "
                             f"{', '.join(rb.conflicts)}", "CLOSING", events,
                             conflicts=list(rb.conflicts), target=target, target_sha=target_sha))
    if rb.regenerated:
        events.append(("finding", {
            "persona": "close", "severity": "info", "gate": "rebase",
            "title": "generated files regenerated during rebase",
            "detail": "regenerated: " + ", ".join(rb.regenerated),
            "files": list(rb.regenerated)}))
    docs_only = merge_request and (state.get("merge") or {}).get("docs_only")
    if docs_only:
        # SPEC-0234: no gate runs; the rebased diff must still be inside the (current) allowlist.
        # Diffing against the target keeps commits that landed on base meanwhile out of it.
        # Checked before regenerating so a refused merge never runs branch-modified commands.
        bad = git.docs_only_violations(
            git.changed_files(worktree, target_sha, renames=False),
            list(mcfg.get("docs_only_paths") or []) + list(regen))      # SPEC-0255
        if bad:
            more = f" (+{len(bad) - 10} more)" if len(bad) > 10 else ""
            cid = state.get("id")
            return decided(_park(
                "not_docs_only",
                f"--docs-only merge changes {len(bad)} paths outside merge.docs_only_paths: "
                f"{', '.join(bad[:10])}{more}. Recovery: abandon this campaign "
                f"(nw queue abandon {cid} --yes), then nw queue merge {c['branch']} --yes "
                f"without --docs-only", "QUEUED", events))
    own = not merge_request or bool((state.get("merge") or {}).get("own_worktree"))
    problem = _regenerate_after_rebase(worktree, regen, events, state.get("id"), own)
    if problem:
        return decided(_park("regenerate_failed", problem, "CLOSING", events))
    head = git.rev_parse(worktree)
    moved_since = None
    if merge_request:
        moved_since = _commits_since_request(root, state, pre_rebase)

    if docs_only:
        return PreparedClose(target=target, target_sha=target_sha, head=head, policy=policy,
                             events=events, commits_since_request=moved_since,
                             pre_head=pre_head)
    if state.get("kind") == "fixforward":
        # SPEC-0420 §5: a "fix" that deletes, skips or weakens a test never reaches the gates
        weak = fixforward_weakens(worktree, target_sha)
        if weak:
            events.append(("finding", {"persona": "close", "severity": "must_fix",
                                       "gate": "fixforward-weakens",
                                       "title": "fix-forward weakens the tests",
                                       "detail": "\n".join(weak), "files": []}))
            return decided(_park("fixforward_weakens", "fix-forward weakens the tests: "
                                 + "; ".join(weak[:5]), "IMPLEMENTING", events))

    # 5. re-gate on the rebased tree
    seq = gate_seq if gate_seq is not None else G.next_gate_seq(cfg, state["id"])
    gate_cfg, raises = P.campaign_gate_config(cfg, project_cfg, worktree)    # SPEC-0316
    if raises:
        events.append(("finding", P.gate_limits_finding(raises)))
    gate_cfg = _skip_unaffected_gates(gate_cfg, worktree, target_sha, events)       # SPEC-0350
    gate_cfg, reused = _reuse_loop_green(cfg, state, project_cfg, gate_cfg, policy=policy,
                                         root=root, worktree=worktree, target_sha=target_sha,
                                         head=head, events=events)              # SPEC-0359
    # SPEC-0346: the merge follows a coordinator re-gate, so this run stays on the coordinator
    # (its `gate` event has no `worker` key). A direct `run_all(..., tier="close")` can still
    # go to a worker (SPEC-0338/0341); only the close that is about to merge is local.
    gate_run = G.run_all(gate_cfg, state, cfg, seq=seq, stop=stop, tier="close",
                         local=True)   # SPEC-0328
    if reused:
        gate_run = replace(gate_run, reused=tuple(reused))
    seq = gate_run.seq                          # run_all moves on when the seq's tmp dir is taken
    # SPEC-0341: a close re-gate's job is never adopted (its record's tier is `close`); its
    # record goes final here
    G.settle_job(cfg, state["id"], gate_run)
    events.append(("gate", G.job_payload(gate_run)))
    if not gate_run.ok:
        detail = ""
        if merge_request:
            detail = _gate_failure_detail(gate_run, target, target_sha)
            # In a worktree nightwright created, the tree held no untracked files before the gate, so what
            # is untracked now is the gate's own output (reports, coverage); leaving it would park
            # the resume as dirty. An adopted worktree is the requester's: anything untracked there
            # may be their new work, so nothing is deleted (the resume parks `worktree_dirty`).
            if (state.get("merge") or {}).get("own_worktree"):
                git.git(worktree, "clean", "-fdq", check=False)
        return decided(CloseOutcome("TRIAGING", gate_run=gate_run, events=events,
                                    gate_detail=detail))
    return PreparedClose(target=target, target_sha=target_sha, head=head, gate_run=gate_run,
                         seq=seq, policy=policy, events=events,
                         commits_since_request=moved_since, pre_head=pre_head)


def _record_reused_merge(cfg, state, project_cfg, root, merged, events) -> None:
    """SPEC-0359 §4: the MERGED transition of a close whose gate run reused a gate is the only
    place `reuses_since_green` grows and a suspect is appended (under the project lock)."""
    try:
        rec = SN.record_merge(cfg, state.get("project"), state.get("id"), merged, root=root,
                              project_cfg=project_cfg)
    except (OSError, ValueError) as e:
        rec, err = None, str(e)
    else:
        err = SN.UNREADABLE if rec is None else ""
    if err:
        events.append(("finding", {"persona": "close", "severity": "minor", "gate": "reuse",
                                   "title": "reused merge not recorded for the sentinel",
                                   "detail": err, "files": []}))


def finish_close(cfg, state: dict, project_cfg: dict, prepared: PreparedClose, *, attempts=1,
                 max_attempts=MAX_CLOSE_ATTEMPTS, _hooks=None) -> CloseOutcome:
    """The finish phase (SPEC-0220), on the main thread: the only part that writes the main
    checkout or the remote. A moved base, worktree HEAD or branch tip is a `retry` outcome
    (CLOSING, redo the worktree phase) until `max_attempts` is spent, then a `base_moving` park."""
    hooks = _hooks or {}
    events: list = []
    c = _close_cfg(state, project_cfg)
    root, worktree, branch, base, policy = c["root"], c["worktree"], c["branch"], c["base"], c["policy"]
    mcfg, remote, rel = c["mcfg"], c["remote"], c["rel"]
    gate_run, target_sha, seq = prepared.gate_run, prepared.target_sha, prepared.seq
    extra = {"gate_run": gate_run, "attempts": attempts}

    def moved(detail) -> CloseOutcome:
        if attempts >= max_attempts:
            return _park("base_moving", detail, "CLOSING", events, **extra)
        return CloseOutcome("CLOSING", events=events, retry=True, **extra)

    if _is_merge_request(state) and policy != "merge":
        return _merge_policy_park(policy, events)
    if policy == "merge":
        # 6. merge preconditions on the main checkout
        cur = git.current_branch(root)
        if cur != base:
            return _park("main_not_on_base", f"main checkout is on {cur!r}, not {base!r}",
                         "CLOSING", events, **extra)
        if not git.is_clean(root, untracked=False):
            dirty = git.status_porcelain(root, untracked=False)
            return _park("main_dirty", "main checkout has uncommitted tracked changes: "
                         + ", ".join(dirty[:10]), "CLOSING", events, **extra)
        if "before_merge" in hooks:
            hooks["before_merge"]()
        if git.rev_parse(root, base) != target_sha:
            return moved(f"{base} moved {attempts} times while closing")
        # the merged tree must be the gated tree
        if git.rev_parse(worktree) != prepared.head or git.rev_parse(root, branch) != prepared.head:
            return moved("branch moved after re-gate")
        try:
            merged = git.ff_merge(root, branch)
        except GitError as e:
            return moved(f"ff-only merge refused: {e.tail}")
        if merged != prepared.head:
            return _park("base_moving", f"merged {merged[:10]} != gated HEAD {prepared.head[:10]}",
                         "CLOSING", events, no_auto=True, **extra)
        _run_post_cmds(mcfg.get("post_cmds"), root, events)
        if mcfg.get("push_after_merge"):
            try:
                git.push_branch(root, remote, base, set_upstream=False)
            except GitError as e:
                events.append(("finding", {"persona": "close", "severity": "major",
                                           "gate": "push", "title": "push after merge failed",
                                           "detail": e.tail, "files": []}))
        close = {"mode": "merge", "merged_sha": merged, "pr_url": None, "gates_seq": seq,
                 "target_sha": target_sha}
        merge_request = _is_merge_request(state)
        if merge_request and (state.get("merge") or {}).get("docs_only"):
            close["docs_only"] = True                # SPEC-0234: merged without gates
        if merge_request:                           # SPEC-0233: make a moved branch visible
            close["head_at_request"] = (state.get("merge") or {}).get("head_at_request")
            close["commits_since_request"] = prepared.commits_since_request
        if gate_run is not None and gate_run.reused:
            close["reused"] = list(gate_run.reused)     # SPEC-0359: this merge rests on a reuse
            _record_reused_merge(cfg, state, project_cfg, root, merged, events)
        events.append(("close", close))
        _prune_job_refs(root, state)
        _cleanup(root, worktree, branch, project_cfg, "merge", events,
                 merge_request=(state.get("merge") or {}) if merge_request else None)
        return CloseOutcome("MERGED", close=close, gate_run=gate_run, events=events,
                            attempts=attempts)

    # 7. pr / draft-pr (push and PR creation only here: a discarded worktree phase has none)
    if git.rev_parse(worktree) != prepared.head:
        return moved("branch moved after re-gate")
    try:
        git.push_branch(worktree, remote, branch)
        text = Path(worktree, rel).read_text(encoding="utf-8")
        fm, body = split_frontmatter(text)
        fm = fm or {}
        runs = store.runs_dir(cfg, state["id"])
        runs.mkdir(parents=True, exist_ok=True)
        body_file = runs / "pr-body.md"
        body_file.write_text(pr_body(state, fm, sections(body or ""), gate_run), encoding="utf-8")
        prcfg = mcfg.get("pr") or {}
        title = f"{prcfg.get('title_prefix') or ''}{state.get('spec_id')}: {fm.get('title') or ''}"
        url = git.gh_pr_create(worktree, base=base, head=branch, title=title,
                               body_file=body_file, draft=(policy == "draft-pr"),
                               labels=tuple(prcfg.get("labels") or ()))
    except GitError as e:
        return _park("gh_failed", f"{' '.join(e.cmd[:3])}: {e.tail}", "CLOSING", events, **extra)
    close = {"mode": policy, "merged_sha": None, "pr_url": url, "gates_seq": seq,
             "target_sha": target_sha}
    events.append(("close", close))
    _prune_job_refs(root, state)
    _cleanup(root, worktree, branch, project_cfg, policy, events)
    return CloseOutcome("HANDED_OFF", close=close, gate_run=gate_run, events=events,
                        attempts=attempts)


def close_campaign(cfg, state: dict, project_cfg: dict, *, gate_seq=None,
                   max_attempts=MAX_CLOSE_ATTEMPTS, _hooks=None) -> CloseOutcome:
    """Run the close algorithm serially for `state` (a `nw.campaign.v1` dict): the worktree
    phase then the finish phase, redoing both while the base moves. Returns the next state
    plus the ledger events to append. `_hooks` is for tests (`before_merge` runs after the
    main-checkout checks and before the base re-check)."""
    events: list = []
    attempts = 0
    while True:
        attempts += 1
        prep = prepare_close(cfg, state, project_cfg, gate_seq=gate_seq)
        events.extend(prep.events)
        if prep.outcome is not None:
            return replace(prep.outcome, events=events, attempts=attempts)
        oc = finish_close(cfg, state, project_cfg, prep, attempts=attempts,
                          max_attempts=max_attempts, _hooks=_hooks)
        events.extend(oc.events)
        if not oc.retry:
            return replace(oc, events=events, attempts=attempts)


def revert_campaign_range(state: dict, worktree, pcfg: dict, events: list) -> str | None:
    """SPEC-0420 §6: revert the culprit campaign's `target_sha..merged_sha` range in the new
    worktree (one commit). Conflicts in `merge.regenerate` paths take the worktree's side and are
    regenerated at the end; any other conflict aborts the revert and returns the park detail."""
    rv = state.get("revert") or {}
    rng = f"{rv.get('target_sha')}..{rv.get('merged_sha')}"
    regen = effective_regenerate(pcfg.get("merge") or {}, worktree)
    env = {**os.environ, "GIT_EDITOR": "true"}
    r = git.git(worktree, "revert", "--no-commit", "--no-edit", rng, check=False, env=env)
    touched = set()
    for _ in range(200):                             # one stop per conflicting commit at most
        if r.returncode == 0:
            break
        conflicts = [p for p in git.git(worktree, "diff", "--name-only", "--diff-filter=U",
                                        check=False).stdout.splitlines() if p]
        bad = [p for p in conflicts if p not in regen]
        if not conflicts or bad:
            git.git(worktree, "revert", "--abort", check=False)
            git.git(worktree, "reset", "-q", "--hard", "HEAD", check=False)
            return (f"git revert {rng} conflicts: {', '.join(bad or conflicts)[:400]}"
                    if (bad or conflicts) else f"git revert {rng} failed: "
                    f"{((r.stderr or '') + (r.stdout or '')).strip()[-400:]}")
        for p in conflicts:
            git.git(worktree, "checkout", "--ours", "--", p)
            git.git(worktree, "add", "--", p)
            touched.add(p)
        r = git.git(worktree, "revert", "--continue", check=False, env=env)
    for p in sorted(touched):
        rr = subprocess.run(regen[p], shell=True, cwd=str(worktree), capture_output=True,
                            text=True)
        if rr.returncode != 0:
            git.git(worktree, "reset", "-q", "--hard", "HEAD", check=False)
            return f"regenerate {p} ({regen[p]}) exited {rr.returncode} during the revert"
        git.git(worktree, "add", "-A", "--", p)
    if git.is_clean(worktree):
        return f"git revert {rng} changed nothing (already reverted?)"
    what = rv.get("spec_id") or rv.get("campaign")
    sha = git.commit_all(worktree, f"Revert {what}: {rng} (red episode "
                                   f"{state.get('red_episode')}, campaign {rv.get('campaign')})")
    events.append(("commit", {"sha": sha, "message": f"revert {rv.get('campaign')} ({rng})"}))
    return None
