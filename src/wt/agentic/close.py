"""Close step (SPEC-0175): rebase → re-gate → ff-only merge, or push + PR hand-off.

This is where an autonomous loop can do real damage — the dogfood target is this repo and the
user's `wt` is an editable install of it — so the procedure never force-pushes, never
rewrites the base, never touches a dirty or off-base main checkout, re-runs every gate on the
exact tree it merges, and audits the spec's Definition of Done mechanically before any of it.
Pure Python over `gitops` / `gates` / `verify`; it returns the ledger events for the caller
(the queue, SPEC-0176) to append and the next state to record — it writes no campaign state
itself."""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

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


def _park(reason, detail, resume_to, events, **extra) -> CloseOutcome:
    payload = {"reason": reason, "detail": detail, "resume_to": resume_to}
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


def _cleanup(root, worktree, branch, project_cfg, mode, events) -> None:
    wcfg = project_cfg.get("worktrees") or {}
    mcfg = project_cfg.get("merge") or {}
    policy = wcfg.get("cleanup", "on_merge")
    if policy == "never" or (policy == "on_merge" and mode != "merge"):
        return
    try:
        git.worktree_remove(root, worktree, force=True)
    except GitError as e:
        events.append(("finding", {"persona": "close", "severity": "minor", "gate": "cleanup",
                                   "title": "worktree removal failed", "detail": e.tail,
                                   "files": []}))
        return
    if mode == "merge" and mcfg.get("delete_branch", True):
        try:
            git.branch_delete(root, branch)
        except GitError as e:
            events.append(("finding", {"persona": "close", "severity": "minor",
                                       "gate": "cleanup", "title": "branch delete failed",
                                       "detail": e.tail, "files": []}))


def close_campaign(cfg, state: dict, project_cfg: dict, *, gate_seq=None,
                   max_attempts=MAX_CLOSE_ATTEMPTS, _hooks=None) -> CloseOutcome:
    """Run the close algorithm for `state` (a `wt.campaign.v1` dict). Returns the next state
    plus the ledger events to append. `_hooks` is for tests (`before_merge` runs after the
    target sha is captured and before the ff-merge)."""
    hooks = _hooks or {}
    events: list = []
    repo = state.get("repo") or {}
    root, worktree, branch = repo["root"], repo["worktree"], repo["branch"]
    mcfg = project_cfg.get("merge") or {}
    policy = mcfg.get("policy", "merge")
    base = mcfg.get("base") or project_cfg.get("base") or repo.get("base") or "main"
    remote = mcfg.get("remote") or "origin"
    rel = (state.get("spec") or {}).get("rel_path") or ""

    # 1. spec DoD in the worktree
    ok, synthetic = spec_dod(worktree, rel)
    if not ok:
        events.append(("finding", {"persona": "close", "severity": "must_fix", "gate": "spec-dod",
                                   "title": "spec Definition of Done not met",
                                   "detail": synthetic["tail"], "files": [rel]}))
        return CloseOutcome("TRIAGING", synthetic_gate=synthetic, events=events)

    # 2. worktree clean
    if not git.is_clean(worktree, untracked=True):
        sha = git.commit_all(worktree, f"wt({state.get('id')}): close-time snapshot")
        events.append(("commit", {"sha": sha, "message": "close-time snapshot"}))

    attempts = 0
    while True:
        attempts += 1
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
            return _park("open_failed", f"cannot resolve {policy} target: {e.tail}",
                         "CLOSING", events, attempts=attempts)

        # 4. rebase
        rb = git.rebase(worktree, target_sha)
        if not rb.ok:
            return _park("rebase_conflict",
                         f"rebase onto {target} ({target_sha[:10]}) conflicts: "
                         f"{', '.join(rb.conflicts)}", "CLOSING", events, attempts=attempts)

        # 5. re-gate on the rebased tree
        seq = gate_seq if gate_seq is not None else G.next_gate_seq(cfg, state["id"])
        gate_run = G.run_all(project_cfg, state, cfg, seq=seq)
        events.append(("gate", G.as_payload(gate_run)))
        if not gate_run.ok:
            return CloseOutcome("TRIAGING", gate_run=gate_run, events=events, attempts=attempts)

        if policy != "merge":
            break

        # 6. merge preconditions on the main checkout
        cur = git.current_branch(root)
        if cur != base:
            return _park("main_not_on_base", f"main checkout is on {cur!r}, not {base!r}",
                         "CLOSING", events, gate_run=gate_run, attempts=attempts)
        if not git.is_clean(root, untracked=False):
            dirty = git.status_porcelain(root, untracked=False)
            return _park("main_dirty", "main checkout has uncommitted tracked changes: "
                         + ", ".join(dirty[:10]), "CLOSING", events, gate_run=gate_run,
                         attempts=attempts)
        if "before_merge" in hooks:
            hooks["before_merge"]()
        if git.rev_parse(root, base) != target_sha:
            if attempts >= max_attempts:
                return _park("base_moving", f"{base} moved {attempts} times while closing",
                             "CLOSING", events, gate_run=gate_run, attempts=attempts)
            continue
        try:
            merged = git.ff_merge(root, branch)
        except GitError as e:
            if attempts >= max_attempts:
                return _park("base_moving", f"ff-only merge refused: {e.tail}", "CLOSING",
                             events, gate_run=gate_run, attempts=attempts)
            continue
        head = git.rev_parse(worktree)
        if merged != head:
            return _park("base_moving", f"merged {merged[:10]} != worktree HEAD {head[:10]}",
                         "CLOSING", events, gate_run=gate_run, attempts=attempts)
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
        events.append(("close", close))
        _cleanup(root, worktree, branch, project_cfg, "merge", events)
        return CloseOutcome("MERGED", close=close, gate_run=gate_run, events=events,
                            attempts=attempts)

    # 7. pr / draft-pr
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
        return _park("gh_failed", f"{' '.join(e.cmd[:3])}: {e.tail}", "CLOSING", events,
                     gate_run=gate_run, attempts=attempts)
    close = {"mode": policy, "merged_sha": None, "pr_url": url, "gates_seq": seq,
             "target_sha": target_sha}
    events.append(("close", close))
    _cleanup(root, worktree, branch, project_cfg, policy, events)
    return CloseOutcome("HANDED_OFF", close=close, gate_run=gate_run, events=events,
                        attempts=attempts)
