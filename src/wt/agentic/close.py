"""Close step (SPEC-0175): rebase → re-gate → ff-only merge, or push + PR hand-off.

This is where an autonomous loop can do real damage — the dogfood target is this repo and the
user's `wt` is an editable install of it — so the procedure never force-pushes, never
rewrites the base, never touches a dirty or off-base main checkout, re-runs every gate on the
exact tree it merges, and audits the spec's Definition of Done mechanically before any of it.
Pure Python over `gitops` / `gates` / `verify`; it returns the ledger events for the caller
(the queue, SPEC-0176) to append and the next state to record — it writes no campaign state
itself."""
from __future__ import annotations

import difflib
import re
import subprocess
from dataclasses import dataclass, field, replace
from pathlib import Path

import yaml

from ..specmeta import sections, split_frontmatter
from ..verify import verify_spec_file
from . import gates as G
from . import gitops as git
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
          **extra) -> CloseOutcome:
    payload = {"reason": reason, "detail": detail, "resume_to": resume_to}
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


def _cleanup(root, worktree, branch, project_cfg, mode, events, *, merge_request=None) -> None:
    """`merge_request` is the campaign's `merge` block for a SPEC-0233 merge request: only a
    worktree wt created is removed, and the requester's branch is never deleted."""
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


def _regenerate_after_rebase(worktree, regen, events, campaign_id, own=True) -> str | None:
    """SPEC-0255: run every declared `merge.regenerate` command in the rebased worktree and commit
    exactly the declared paths that changed, so the gated head is the merged head. Returns None,
    or the `regenerate_failed` park detail (a failing command or git step, or undeclared files it
    dirtied). A wt-owned worktree is then put back to HEAD; an adopted one (`own` false) is the
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
        staged = git.git(worktree, "diff", "--cached", "--quiet", check=False).returncode != 0
        if staged:
            git.git(worktree, "commit", "-q", "-m",
                    f"wt({campaign_id}): regenerate generated files after rebase")
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

    merge_request = _is_merge_request(state)
    if merge_request and policy != "merge":
        return decided(_merge_policy_park(policy, events))
    if merge_request and not (state.get("merge") or {}).get("own_worktree") \
            and _rebase_in_progress(worktree):
        # SPEC-0233 G3: a rebase in the requester's checkout is theirs, not a wt crash leftover;
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

    if not merge_request:
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
        sha = git.commit_all(worktree, f"wt({state.get('id')}): close-time snapshot")
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
    regen = {r["path"]: r["cmd"] for r in (mcfg.get("regenerate") or [])}
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
                f"(wt queue abandon {cid} --yes), then wt queue merge {c['branch']} --yes "
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

    # 5. re-gate on the rebased tree
    seq = gate_seq if gate_seq is not None else G.next_gate_seq(cfg, state["id"])
    gate_run = G.run_all(project_cfg, state, cfg, seq=seq, stop=stop)
    seq = gate_run.seq                          # run_all moves on when the seq's tmp dir is taken
    events.append(("gate", G.as_payload(gate_run)))
    if not gate_run.ok:
        detail = ""
        if merge_request:
            detail = _gate_failure_detail(gate_run, target, target_sha)
            # In a worktree wt created, the tree held no untracked files before the gate, so what
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
                         "CLOSING", events, **extra)
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
        events.append(("close", close))
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
    _cleanup(root, worktree, branch, project_cfg, policy, events)
    return CloseOutcome("HANDED_OFF", close=close, gate_run=gate_run, events=events,
                        attempts=attempts)


def close_campaign(cfg, state: dict, project_cfg: dict, *, gate_seq=None,
                   max_attempts=MAX_CLOSE_ATTEMPTS, _hooks=None) -> CloseOutcome:
    """Run the close algorithm serially for `state` (a `wt.campaign.v1` dict): the worktree
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
