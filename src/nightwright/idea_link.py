"""Link a private idea to a public REQ file on refs/heads/nw-backlog (SPEC-0480 publish slice)."""
from __future__ import annotations

import hashlib
import os
import re
import secrets
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from .explore import idea_summary_text
from .org import Task
from .rules import INTERNAL_PROJECT, nw_checkout

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_BACKLOG_REF = "refs/heads/nw-backlog"
_NULL_SHA = "0000000000000000000000000000000000000000"


def canonical_pair(title: str, summary: str) -> tuple[str, str]:
    """Strip title; normalize summary newlines to LF and strip."""
    ct = (title or "").strip()
    s = (summary or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    return ct, s


def req_base(title: str, summary: str) -> str:
    ct, cs = canonical_pair(title, summary)
    payload = f"{ct}\n{cs}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _yaml_double_quoted(title: str) -> str:
    out = ['"']
    for ch in title:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch in "\n\r":
            raise ValueError("title must be one line")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def render_request(
    *,
    req_id: str,
    title: str,
    summary: str,
    status: str,
    created: str,
    updated: str,
    created_by: str,
    updated_by: str,
    spec: str | None = None,
) -> bytes:
    """UTF-8 request file body for refs/heads/nw-backlog (no path suffix on req_id)."""
    ct, cs = canonical_pair(title, summary)
    lines = [
        "---\n",
        f"id: {req_id}\n",
        f"title: {_yaml_double_quoted(ct)}\n",
        f"status: {status}\n",
        f"created: {created}\n",
        f"updated: {updated}\n",
        f"created_by: {created_by}\n",
        f"updated_by: {updated_by}\n",
    ]
    if spec is not None:
        lines.append(f"spec: {spec}\n")
    lines.append("---\n")
    return "".join(lines).encode("utf-8") + cs.encode("utf-8")


def resolve_linked_repo(cfg, project: str) -> Path:
    """Repo root for a project's nw-backlog branch (SPEC-0480)."""
    project = (project or "").strip()
    if not project:
        raise ValueError("project is required")
    if project == INTERNAL_PROJECT:
        root = nw_checkout(cfg)
        if not root:
            raise ValueError("cannot resolve a repo for project 'Meta-Tools': "
                             "could not discover nightwright checkout")
        return Path(root).resolve()
    from .agentic.project import project_root

    return project_root(cfg, project)


def mint_req_id() -> str:
    """REQ- plus eight Crockford characters."""
    tail = "".join(secrets.choice(_CROCKFORD) for _ in range(8))
    return f"REQ-{tail}"


def _utc_now_z() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _git(repo: Path, *args: str, check: bool = True, input: bytes | None = None,
         env: dict | None = None) -> subprocess.CompletedProcess:
    cmd = ["git", "-C", str(repo), *args]
    return subprocess.run(cmd, check=check, capture_output=True, input=input, env=env)


def _git_config(repo: Path, key: str) -> str:
    r = _git(repo, "config", "--local", key, check=False)
    return (r.stdout.decode().strip() if r.returncode == 0 else "")


def _git_identity(repo: Path) -> tuple[str, str]:
    name = _git_config(repo, "user.name")
    email = _git_config(repo, "user.email")
    if not name:
        raise ValueError("user.name is not set in the linked repo")
    if not email:
        raise ValueError("user.email is not set in the linked repo")
    return name, email


def _symbolic_head(repo: Path) -> str | None:
    r = _git(repo, "symbolic-ref", "-q", "HEAD", check=False)
    if r.returncode != 0:
        return None
    return r.stdout.decode().strip()


def _ref_tip(repo: Path, ref: str) -> str | None:
    r = _git(repo, "rev-parse", "--verify", "-q", ref, check=False)
    if r.returncode != 0:
        return None
    return r.stdout.decode().strip()


def _mktree(repo: Path, parent_sha: str | None, path: str, blob_sha: str) -> str:
    entries: dict[str, tuple[str, str]] = {}
    if parent_sha:
        r = _git(repo, "ls-tree", parent_sha)
        for line in r.stdout.decode().splitlines():
            parts = line.split("\t", 1)
            if len(parts) != 2:
                continue
            mode_typ_sha = parts[0].split()
            if len(mode_typ_sha) != 3:
                continue
            mode, _typ, sha = mode_typ_sha
            name = parts[1]
            if name == path:
                continue
            entries[name] = (mode, sha)
    entries[path] = ("100644", blob_sha)
    tree_lines = "".join(
        f"{mode} blob {sha}\t{name}\n" for name, (mode, sha) in sorted(entries.items())
    )
    r = _git(repo, "mktree", input=tree_lines.encode())
    return r.stdout.decode().strip()


def _publish_commit(
    repo: Path,
    *,
    rel_path: str,
    body: bytes,
    message: str,
    name: str,
    email: str,
) -> tuple[str, str | None, str]:
    """Write blob, tree, commit; compare-and-swap nw-backlog. Returns (new_sha, old_tip, req_id)."""
    if _symbolic_head(repo) == _BACKLOG_REF:
        raise ValueError("refusing to update nw-backlog while it is checked out")

    old_tip = _ref_tip(repo, _BACKLOG_REF)
    expected = old_tip or _NULL_SHA

    blob = _git(repo, "hash-object", "-w", "--stdin", input=body).stdout.decode().strip()
    tree = _mktree(repo, old_tip, rel_path, blob)
    parent_args = [tree]
    if old_tip:
        parent_args.extend(["-p", old_tip])
    env = os.environ.copy()
    env["GIT_AUTHOR_NAME"] = name
    env["GIT_AUTHOR_EMAIL"] = email
    env["GIT_COMMITTER_NAME"] = name
    env["GIT_COMMITTER_EMAIL"] = email
    commit = _git(
        repo,
        "commit-tree",
        *parent_args,
        "-m",
        message,
        env=env,
    ).stdout.decode().strip()

    r = _git(repo, "update-ref", _BACKLOG_REF, commit, expected, check=False)
    if r.returncode != 0:
        raise ValueError("update-ref failed")
    return commit, old_tip, blob


def _restore_backlog_tip(repo: Path, *, new_sha: str, old_tip: str | None) -> None:
    if old_tip is None:
        _git(repo, "update-ref", "-d", _BACKLOG_REF, new_sha, check=False)
    else:
        _git(repo, "update-ref", _BACKLOG_REF, old_tip, new_sha, check=False)


def _restore_backlog_tip_or_bail(
    repo: Path, *, req_id: str, new_sha: str, old_tip: str | None
) -> None:
    cur = _ref_tip(repo, _BACKLOG_REF)
    if cur != new_sha:
        raise ValueError(
            f"{req_id} was not linked; refs/heads/nw-backlog no longer points at {new_sha}; "
            "left unchanged"
        )
    _restore_backlog_tip(repo, new_sha=new_sha, old_tip=old_tip)
    raise ValueError(f"{req_id} was not linked; restored refs/heads/nw-backlog")


def publish_idea(cfg, task: Task, *, selector: str | None = None) -> str:
    """Publish one idea to nw-backlog and set :REQ: / :REQ_BASE:. Stdout value is :ID:."""
    who = selector if selector is not None else (task.properties or {}).get("ID") or task.id
    if not task.is_idea:
        raise ValueError(f"{who!r} is not an idea")

    existing = (task.properties or {}).get("REQ", "").strip()
    if existing:
        raise ValueError(f"{task.properties.get('ID', task.id)} is already linked to {existing}")

    title = (task.heading or "").strip()
    if not title:
        raise ValueError("title is empty")
    if re.search(r"[\n\r]", task.heading or ""):
        raise ValueError("title must be one line")

    summary = idea_summary_text(cfg, task)
    canon_title, canon_summary = canonical_pair(title, summary)
    if re.search(r"[\n\r]", canon_title):
        raise ValueError("title must be one line")

    project = (task.properties or {}).get("PROJECT", "").strip()
    if not project:
        raise ValueError("project is required")

    repo = resolve_linked_repo(cfg, project)
    name, email = _git_identity(repo)
    author = f"{name} <{email}>"

    req_id = mint_req_id()
    rel_path = f"{req_id}.md"
    now = _utc_now_z()
    body = render_request(
        req_id=req_id,
        title=title,
        summary=summary,
        status="open",
        created=now,
        updated=now,
        created_by=author,
        updated_by=author,
    )
    digest = req_base(title, summary)

    try:
        new_sha, old_tip, _blob = _publish_commit(
            repo,
            rel_path=rel_path,
            body=body,
            message=f"publish {req_id}",
            name=name,
            email=email,
        )
    except ValueError as e:
        if str(e) == "update-ref failed":
            raise ValueError("publish failed") from e
        raise

    from . import org_write as W

    try:
        W.link_idea_request(cfg, task, req_id, digest)
    except Exception:
        _restore_backlog_tip_or_bail(repo, req_id=req_id, new_sha=new_sha, old_tip=old_tip)
        raise

    return (task.properties or {}).get("ID", "").strip() or task.id
