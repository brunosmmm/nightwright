"""Classify tracked dirt on a project's base checkout (SPEC-0546).

`plan_clear` is read-only: it runs git and reads bytes, never writes the checkout. No patch
text is parsed: each dirty path is classified from the `old` and `new` bytes of the pair the
spec names (combined, or staged and unstaged halves of a partially staged path), so diff
prefix, quoting and rename config cannot change what is read. Every rule errs toward
`branch`: a miss costs a branch, a false `trivial` lands unreviewed code on main."""
from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import os
import posixpath
import re
import shlex
import shutil
import signal
import subprocess
from pathlib import Path

from . import project as P

SCHEMA = "nw.main_dirty_plan.v1"
APPLY_SCHEMA = "nw.main_dirty_apply.v1"
_COMMIT_SUBJECT = "chore: land stray trivial main-checkout edits"
_COMMIT_TIMEOUT = 1800                 # seconds; `git commit` runs the repo's hooks
_WIP_SUBJECT = "WIP: stray main-checkout edits"
_SAVE_ACTIONS = ("save_branch", "branch_and_escalate", "hold")
_FC_RANK = {"code": 4, "other": 3, "comment": 2, "frontmatter": 1, "docs": 1}
_UNMERGED = frozenset({"UU", "AA", "DD", "AU", "UA", "DU", "UD"})
_MARKERS = (("MERGE_HEAD", "merge"), ("CHERRY_PICK_HEAD", "cherry-pick"),
            ("REVERT_HEAD", "revert"), ("rebase-merge", "rebase"), ("rebase-apply", "rebase"))

_CODE_SUFFIXES = frozenset({
    ".py", ".pyi", ".sh", ".bash", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".go", ".rs",
    ".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".rb", ".pl", ".java", ".sql", ".lua",
})
_PY_SUFFIXES = frozenset({".py", ".pyi", ".toml"})
_HASH_SUFFIXES = frozenset({".sh", ".bash", ".yml", ".yaml", ".rb", ".pl"})
_CLIKE_SUFFIXES = frozenset({".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".go", ".rs", ".c", ".h",
                             ".cc", ".cpp", ".cxx", ".hpp", ".java"})
_BACKTICK_SUFFIXES = frozenset({".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".go"})
_MD_SUFFIXES = frozenset({".md", ".html", ".xml"})
# characters that end a line for CPython's tokenizer or for JavaScript (plus Unicode separators)
_LINE_BREAKS = "\r\x0b\x0c\x1c\x1d\x1e\x85  "
_SCALAR_ENDS = ("|", "|-", "|+", ">", ">-", ">+")
# a YAML block scalar header: `|` or `>` with optional indent/chomp indicators and a comment
_SCALAR_RE = re.compile(r"[|>][0-9+-]*\s*(#.*)?$")
# files whose suffix has a kinds table; any other suffix has no comment lines
_KNOWN_SUFFIXES = (_PY_SUFFIXES | _HASH_SUFFIXES | _CLIKE_SUFFIXES | _MD_SUFFIXES | frozenset({".css"}))
# a trailing backslash splices the next line (preprocessor, shell, Python, Ruby, Perl)
_SPLICE_SUFFIXES = frozenset({".py", ".pyi", ".sh", ".bash", ".rb", ".pl"}) | _CLIKE_SUFFIXES


# suffixes whose quoted literals can span lines (a comment-looking line inside one is data)
_DQUOTE_SUFFIXES = frozenset({".sh", ".bash", ".rb", ".pl", ".yml", ".yaml", ".rs", ".jsx", ".tsx"})
_SQUOTE_SUFFIXES = frozenset({".sh", ".bash", ".rb", ".pl", ".yml", ".yaml"})
_JSX_RE = re.compile(r"<[A-Za-z/>]")       # JSX children: a `//` line there is text, not a comment
_RUST_RAW_RE = re.compile(r'\br#*"')
_ESCAPED_RE = re.compile(r"\\.")
# pragma-style comments change behaviour (build tags, cgo, encodings, compiler directives)
_PRAGMA_RES = {
    ".py": re.compile(r"#.*coding[:=]"), ".pyi": re.compile(r"#.*coding[:=]"),
    ".rb": re.compile(r"#\s*(frozen_string_literal|encoding|coding)\s*:"),
    ".go": re.compile(r"//\s*(go:|\+build|export\b|line\b)"),
    ".ts": re.compile(r"//\s*@ts-|///\s*<reference"), ".tsx": re.compile(r"//\s*@ts-|///\s*<reference"),
    ".js": re.compile(r"//\s*@(flow|jsx|ts-)|//#\s*source"),
    ".jsx": re.compile(r"//\s*@(flow|jsx|ts-)|//#\s*source"),
    ".mjs": re.compile(r"//\s*@(flow|jsx|ts-)|//#\s*source"),
    ".cjs": re.compile(r"//\s*@(flow|jsx|ts-)|//#\s*source"),
}


class NotAWorktree(Exception):
    """`--root` is not the top of a git worktree (CLI exit 2)."""


class PlanError(Exception):
    """A git failure or an invalid project config (CLI exit 3)."""


# ---- git -------------------------------------------------------------------------------

def _git(root, *args, check=True, timeout=120):
    env = {k: v for k, v in os.environ.items()
           if k not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")}
    env.update({"LC_ALL": "C", "GIT_OPTIONAL_LOCKS": "0"})
    try:
        r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, env=env,
                           timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise PlanError(f"git {' '.join(args)}: {e}")
    if check and r.returncode != 0:
        msg = (r.stderr or b"").decode("utf-8", "replace").strip()
        raise PlanError(f"git {' '.join(args)} exited {r.returncode}: {msg}")
    return r


def _dec(b: bytes) -> str:
    return b.decode("utf-8", "surrogateescape")


def _check_worktree(root: Path) -> None:
    if not root.is_dir():
        raise NotAWorktree(f"{root} is not a directory")
    r = _git(root, "rev-parse", "--show-toplevel", check=False)
    err = (r.stderr or b"").decode("utf-8", "replace")
    if r.returncode != 0:
        if "not a git repository" in err or "work tree" in err:
            raise NotAWorktree(f"{root} is not a git worktree")
        raise PlanError(f"git rev-parse --show-toplevel exited {r.returncode}: {err.strip()}")
    top = _dec(r.stdout).strip()
    if not top or os.path.realpath(top) != os.path.realpath(root):
        raise NotAWorktree(f"{root} is not the top of a git worktree (top is {top or 'unknown'})")


def _base(root: Path, base) -> str:
    """The base `finish_close` compares: through `load_project_config` when the project has a
    config (`--base` is then ignored), else `--base`, else `main`."""
    path = P.config_path(root)
    try:
        found = P.load_project_config(path)["merge"]["base"] if path.is_file() else (base or "main")
    except (ValueError, OSError) as e:
        raise PlanError(str(e))
    if found.startswith("-") or not found.strip():
        raise PlanError(f"invalid base branch {found!r}")   # `git switch <base>` would read an option
    return found


def _in_progress(root: Path, xys) -> str | None:
    for name, label in _MARKERS:
        r = _git(root, "rev-parse", "--git-path", name, check=False)
        out = _dec(r.stdout).strip()
        if r.returncode == 0 and out and (root / out).exists():
            return label
    if any(xy in _UNMERGED for xy in xys):
        return "conflict"
    return None


# ---- inputs ----------------------------------------------------------------------------

def _status(root: Path) -> list[dict]:
    out = _git(root, "status", "--porcelain=v1", "-z", "--renames", "--untracked-files=no").stdout
    parts = _dec(out).split("\0")
    rows, i = [], 0
    while i < len(parts):
        rec = parts[i]
        i += 1
        if not rec:
            continue
        xy, path, orig = rec[:2], rec[3:], None
        if "R" in xy or "C" in xy:
            orig = parts[i] if i < len(parts) else None
            i += 1
        rows.append({"xy": xy, "path": path,
                     "orig": orig if "R" in xy else None})
    return rows


def _raw(root: Path, *cmd) -> dict:
    """path -> (old_mode, new_mode, status) from `--raw --no-renames -z`. The plumbing
    commands (`diff-index`, `diff-files`) are used because `git diff` refreshes and rewrites a
    stale index even with `GIT_OPTIONAL_LOCKS=0`."""
    out = _git(root, *cmd, "--raw", "--no-renames", "-z", "--no-ext-diff", "--no-textconv",
               "--no-color").stdout
    parts = _dec(out).split("\0")
    res, i = {}, 0
    while i < len(parts):
        meta = parts[i]
        i += 1
        if not meta.startswith(":"):
            continue
        fields = meta[1:].split()
        if i < len(parts) and len(fields) >= 5:
            res[parts[i]] = (fields[0], fields[1], fields[4][:1])
            i += 1
    return res


def _blob(root: Path, spec: str) -> bytes | None:
    r = _git(root, "cat-file", "blob", spec, check=False)
    return r.stdout if r.returncode == 0 else None


def _wt(root: Path, path: str) -> bytes | None:
    full = root / path
    try:
        if full.is_symlink():
            return os.fsencode(os.readlink(full))
        if not full.is_file():
            return None
        return full.read_bytes()
    except OSError as e:
        raise PlanError(f"cannot read {path}: {e}")


# ---- line scan -------------------------------------------------------------------------

def _suffix(path: str) -> str:
    i = path.rfind(".")
    return path[i:].lower() if i > path.rfind("/") else ""


def _split(text: str) -> list[str]:
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def _triple(ln: str, tok):
    """(touched, open token) after scanning `ln` for Python triple-quoted strings."""
    touched, i = tok is not None, 0
    while True:
        if tok is None:
            hits = [(ln.find(t, i), t) for t in ('"""', "'''") if ln.find(t, i) >= 0]
            if not hits:
                return touched, None
            j, tok = min(hits)
            touched, i = True, j + 3
        else:
            j = ln.find(tok, i)
            if j < 0:
                return touched, tok
            i, tok = j + 3, None


def _scan(path: str, text: str):
    """(lines, kinds, hazard): each line is `blank`, `comment`, `text` or (`.md .html .xml`)
    `fenced`. State is carried top to bottom; `hazard` marks a side whose lines cannot be
    trusted to be split by `\\n` alone, or that holds a heredoc, block scalar or template string."""
    suf = _suffix(path)
    lines = _split(text)
    kinds, hazard = [], False
    tok = None                       # open Python triple-quote token
    in_block = in_fence = in_html = False
    clike = suf in _CLIKE_SUFFIXES or suf == ".css"
    for line in lines:
        ln = line[:-1] if line.endswith("\r") else line
        # ASCII blanks only: a line that is whitespace for a Unicode reason (NBSP, U+3000,
        # U+2000-U+200A) is not `blank`; it stays non-empty `text` and sets `hazard`
        s = ln.strip(" \t\x0b\x0c")
        if s and any(c in ln for c in _LINE_BREAKS):
            hazard = True
        if suf in (".sh", ".bash", ".rb", ".pl") and "<<" in ln:
            hazard = True
        if suf in (".rb", ".pl") and s in ("__END__", "__DATA__"):
            hazard = True               # everything after is data
        if suf in (".yml", ".yaml") and (s.endswith(_SCALAR_ENDS) or _SCALAR_RE.search(s)):
            hazard = True
        if suf == ".java" and '"""' in ln:
            hazard = True               # a text block
        if suf in _BACKTICK_SUFFIXES and "`" in ln:
            hazard = True
        if suf in _SPLICE_SUFFIXES and s.endswith("\\"):
            hazard = True
        if suf in _PY_SUFFIXES:
            touched, tok = _triple(ln, tok)
            if touched:
                kind = "text"
            elif not s:
                kind = "blank"
            else:
                kind = "comment" if s.startswith("#") and not s.startswith("#!") else "text"
        elif suf in _HASH_SUFFIXES:
            if not s:
                kind = "blank"
            else:
                kind = "comment" if s.startswith("#") and not s.startswith("#!") else "text"
        elif clike:
            if in_block:
                kind = "comment"
                if "*/" in ln:
                    in_block = False
                    if not s.endswith("*/") or ln.count("*/") != 1:
                        hazard = True
            elif not s:
                kind = "blank"
            elif s.startswith("//") and suf != ".css":
                kind = "comment"
            elif s.startswith("/*"):
                rest = s[2:]
                if "*/" in rest:
                    kind = "comment" if s.endswith("*/") and rest.count("*/") == 1 else "text"
                else:
                    kind, in_block = "comment", True
            else:
                kind = "text"
            if kind == "text" and ("/*" in s or "*/" in s):
                hazard = True
        elif suf in _MD_SUFFIXES:
            if s.startswith("```"):
                if in_html:
                    hazard = True
                in_fence = not in_fence
                kind = "fenced"
            elif in_fence:
                kind = "fenced"
            elif in_html:
                kind = "comment"
                if "-->" in ln:
                    in_html = False
                    if not s.endswith("-->") or ln.count("-->") != 1:
                        hazard = True
            elif not s:
                kind = "blank"
            elif s.startswith("<!--"):
                if "-->" in s:
                    kind = "comment" if s.endswith("-->") and s.count("-->") == 1 else "text"
                else:
                    kind, in_html = "comment", True
            else:
                kind = "text"
            if kind == "text" and ("<!--" in s or "-->" in s):
                hazard = True
        else:
            kind = "text"
        pragma = _PRAGMA_RES.get(suf)
        if kind == "comment" and pragma and pragma.match(s):
            kind = "text"
        if kind == "text":
            bare = _ESCAPED_RE.sub("", ln)
            if suf in _DQUOTE_SUFFIXES and bare.count('"') % 2:
                hazard = True          # a quoted literal that may span lines
            if suf in _SQUOTE_SUFFIXES and bare.count("'") % 2:
                hazard = True
            if suf == ".rs" and _RUST_RAW_RE.search(ln):
                hazard = True          # a raw string
            if suf in (".jsx", ".tsx") and _JSX_RE.search(ln):
                hazard = True          # JSX children
            if suf == ".go" and s == 'import "C"':
                hazard = True          # the block comment before it is cgo source
        kinds.append(kind)
    if tok is not None:
        hazard = True                   # an unterminated triple-quoted string
    return lines, kinds, hazard


def _line_kinds(path: str, text: str):
    _, kinds, hazard = _scan(path, text)
    return kinds, hazard


# ---- file rules ------------------------------------------------------------------------

def _is_code_path(path: str) -> bool:
    p = posixpath.normpath(path)
    return p.startswith(("src/", "tests/", "tools/")) or _suffix(p) in _CODE_SUFFIXES


def _is_docs_path(path: str) -> bool:
    p = posixpath.normpath(path)
    if p == "docs/LEDGER.md":
        return False
    return p in ("README.md", "CHANGELOG.md") or (p.startswith("docs/") and p.endswith(".md"))


def _seq(lines, kinds, *wanted):
    return [ln for ln, k in zip(lines, kinds) if k in wanted]


def _frontmatter_end(text: str):
    """None: no leading `---`; -1: unclosed (or a fence first); else the offset after the closing line."""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return None
    off = len(lines[0]) + 1
    for ln in lines[1:]:
        if ln.strip().startswith("```"):
            return -1
        if ln.strip() == "---":
            return off + len(ln) + 1
        off += len(ln) + 1
    return -1


def _half_class(path, orig, *, code_side, renamed, flags, old, new) -> str:
    """The file class of one (old, new) pair; `old`/`new` are bytes or None (absent)."""
    # rule 1: no text lines to read
    special = flags or any(b is not None and b"\0" in b for b in (old, new))
    if special:
        return "code" if code_side else "other"
    p = posixpath.normpath(path)
    deleted = new is None
    # rule 2
    if code_side:
        if deleted or renamed:
            return "code"
        return "comment" if _comment_only(path, old, new) else "code"
    # rule 3
    if p.startswith("skills/") or p == "docs/LEDGER.md":
        return "other"
    # rule 4
    if deleted or renamed:
        return "other"
    # rule 5
    suf = _suffix(p)
    docs = _is_docs_path(p)
    if old is None:
        return "other"
    ot, nt = _dec(old), _dec(new)
    if suf in _MD_SUFFIXES and (docs or suf != ".md"):
        ol, ok, _ = _scan(p, ot)
        nl, nk, _ = _scan(p, nt)
        if _seq(ol, ok, "fenced") != _seq(nl, nk, "fenced"):
            return "other"
    if (suf != ".md" or docs) and _comment_only(p, old, new):
        return "comment"
    if docs:
        oe, ne = _frontmatter_end(ot), _frontmatter_end(nt)
        if oe not in (None, -1) and ne not in (None, -1) and ot[oe:] == nt[ne:]:
            return "frontmatter"
        if ot != nt and oe != -1 and ne != -1:
            return "docs"
    return "other"


def _comment_only(path, old, new) -> bool:
    if old is None or new is None or old == new or b"\0" in old or b"\0" in new:
        return False
    if _suffix(path) not in _KNOWN_SUFFIXES:
        return False                    # no kinds table: every line is `text`, never a comment
    if old.endswith(b"\n") != new.endswith(b"\n"):
        return False                    # `_split` drops the final newline; it is still an edit
    ol, ok, oh = _scan(path, _dec(old))
    nl, nk, nh = _scan(path, _dec(new))
    if oh or nh:
        return False
    return _seq(ol, ok, "text", "fenced") == _seq(nl, nk, "text", "fenced")


# ---- plan ------------------------------------------------------------------------------

def _specs(xy, path, orig):
    """[(label, old spec, new spec, renamed)]; a spec is ('blob', rev:path) | ('wt', path)."""
    staged_x, wt_y = xy[0] not in " ?", xy[1] not in " ?"
    old_name = orig or path
    if staged_x and wt_y and xy not in _UNMERGED:
        return [("staged", ("blob", f"HEAD:{old_name}"), ("blob", f":{path}"), orig is not None),
                ("unstaged", ("blob", f":{path}"), ("wt", path), False)]
    new = None if xy[0] == "D" else ("wt", path)
    return [("combined", ("blob", f"HEAD:{old_name}"), new, orig is not None)]


def _flags(root, half, raw, path, orig) -> bool:
    """True when the half has no text lines: symlink, gitlink, typechange, mode change, directory."""
    ent_new = raw.get(path)
    ent_old = raw.get(orig) if orig else ent_new
    modes = {m for e in (ent_new, ent_old) if e for m in e[:2]}
    if "120000" in modes or "160000" in modes:
        return True
    if any(e and e[2] == "T" for e in (ent_new, ent_old)):
        return True
    if ent_new and ent_old:
        om, nm = ent_old[0], ent_new[1]
        if om != "000000" and nm != "000000" and om != nm:
            return True
    full = root / path
    return full.is_dir() and not full.is_symlink()


def _is_gitlink(raws, path, orig) -> bool:
    return any(r.get(n) and "160000" in r[n][:2] for r in raws for n in (path, orig) if n)


def _spec_bytes(root, spec):
    if spec is None:
        return None
    return _blob(root, spec[1]) if spec[0] == "blob" else _wt(root, spec[1])


def _classify(root, row, raws) -> str:
    path, orig, xy = row["path"], row["orig"], row["xy"]
    code_side = _is_code_path(path) or (orig is not None and _is_code_path(orig))
    best = None
    for label, old_spec, new_spec, renamed in _specs(xy, path, orig):
        raw = raws["unstaged" if label == "unstaged" else "staged" if label == "staged" else "head"]
        flags = _flags(root, label, raw, path, orig if renamed else None)
        old = new = None
        if not flags:
            old, new = _spec_bytes(root, old_spec), _spec_bytes(root, new_spec)
        fc = _half_class(path, orig, code_side=code_side, renamed=renamed, flags=flags,
                         old=old, new=new)
        if best is None or _FC_RANK[fc] > _FC_RANK[best]:
            best = fc
    return best


def _wip_name(root: Path) -> str:
    base = "wip/main-dirty-" + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    name, n = base, 1
    while _dec(_git(root, "branch", "--list", name).stdout).strip():
        n += 1
        name = f"{base}-{n}"
    return name


def _recommended(root, action, klass, base, rows) -> list[str]:
    R = str(root)

    def cmd(*args, literal=False):
        pre = ["git", "-C", R] + (["--literal-pathspecs"] if literal else [])
        return " ".join(shlex.quote(a) for a in pre + list(args))

    upaths = [r["path"] for r in rows if r["xy"][1] not in " ?"]
    add = [cmd("add", "--", *upaths, literal=True)] if upaths else []
    if action in ("resume", "escalate"):
        return []
    if action == "commit":
        return add + [cmd("commit", "-m", _COMMIT_SUBJECT)]
    # SPEC-0549: an invalid base (option-shaped, bad ref name, no such branch) is never named
    switch = [cmd("switch", base)] if _base_refusal(root, base) is None else []
    if not rows:                                           # hold, no tracked dirt
        return switch
    wip = _wip_name(root)
    return ([cmd("switch", "-c", wip)] + add
            + [cmd("commit", "-m", f"{_WIP_SUBJECT} ({klass})")] + switch)


def plan_clear(root, base=None) -> dict:
    root = Path(root)
    _check_worktree(root)
    absroot = os.path.abspath(root)
    root = Path(absroot if os.path.realpath(absroot) == os.path.realpath(root)
                else os.path.realpath(root))
    arg_base = base
    base = _base(root, base)
    rows = _status(root)
    xys = [r["xy"] for r in rows]
    partial = any(r["xy"][0] not in " ?" and r["xy"][1] not in " ?" and r["xy"] not in _UNMERGED
                  for r in rows)
    raws = {"head": _raw(root, "diff-index", "HEAD") if rows else {}}
    raws["staged"] = _raw(root, "diff-index", "--cached", "HEAD") if partial else {}
    raws["unstaged"] = _raw(root, "diff-files") if partial else {}
    for r in rows:
        r["file_class"] = _classify(root, r, raws)
        r["gitlink"] = _is_gitlink(raws.values(), r["path"], r["orig"]) and r["xy"][1] not in " ?"
    in_progress = _in_progress(root, xys)

    if any(r["file_class"] == "code" for r in rows):
        klass, action = "substantive", "branch_and_escalate"
    elif any(r["file_class"] == "other" for r in rows):
        klass, action = "branch", "save_branch"
    elif rows:
        klass, action = "trivial", "commit"
    else:
        klass, action = "clean", "resume"

    br = _git(root, "symbolic-ref", "--short", "HEAD", check=False)
    on_base = br.returncode == 0 and _dec(br.stdout).strip() == base
    dirty_gitlinks = [r["path"] for r in rows if r["gitlink"]]
    if in_progress:
        klass, action = "merge", "escalate"
    elif dirty_gitlinks:
        action = "escalate"
    elif not on_base:
        action = "hold"

    paths, subst = [], []
    for r in rows:
        names = [r["path"]] + ([r["orig"]] if r["orig"] else [])
        paths += names
        if r["file_class"] == "code":
            subst += names
    recommended = _recommended(root, action, klass, base, rows)
    try:
        refusal = (verb_refusal(root, {"action": action, "paths": paths, "base": base})
                   if action == "commit" or action in _SAVE_ACTIONS else "n/a")
    except (PlanError, OSError, ValueError):   # a guard fault keeps the raw list, not a crash
        refusal = "guard failed"
    if refusal is None:
        # SPEC-0548/0549: one audited command instead of raw git, only when the verb will not refuse
        recommended = ["nw queue main-dirty-apply --root " + shlex.quote(str(root))
                       + (f" --base {shlex.quote(arg_base)}" if arg_base is not None else "")]
    return {
        "schema": SCHEMA, "root": str(root), "base": base, "on_base": on_base, "class": klass,
        "action": action, "in_progress": in_progress, "paths": paths,
        "substantive_paths": subst,
        "files": [{"path": r["path"], "orig_path": r["orig"], "status": r["xy"],
                   "file_class": r["file_class"]} for r in rows],
        "recommended": recommended,
    }


# ---- apply (SPEC-0548) -----------------------------------------------------------------

def _lg(root, *args, check=True, timeout=120):
    """`_git` with `--literal-pathspecs`: a path is never a glob or a magic pathspec."""
    return _git(root, "--literal-pathspecs", *args, check=check, timeout=timeout)


_BATCH = 400                                  # paths per git call: bounded calls, bounded argv


def _batches(items):
    items = list(items)
    return [items[i:i + _BATCH] for i in range(0, len(items), _BATCH)]


def _index_map(root, paths) -> dict[str, list]:
    """`{path: [(mode, oid, stage), ...]}` for the index entries named exactly `paths`."""
    want, ents = set(paths), {}
    for ch in _batches(sorted(want)):
        out = _lg(root, "ls-files", "-s", "-z", "--", *ch).stdout
        for rec in _dec(out).split("\0"):
            if rec:
                meta, _, name = rec.partition("\t")
                mode, oid, stage = meta.split()
                if name in want:
                    ents.setdefault(name, []).append((mode, oid, int(stage)))
    return ents


def _head_map(root, names, rev="HEAD") -> dict[str, tuple]:
    """`{name: (mode, oid)}` for the `rev` tree entries named exactly `names` (none if no HEAD)."""
    want, found = set(names), {}
    for ch in _batches(sorted(want)):
        r = _lg(root, "ls-tree", "-z", rev, "--", *ch, check=False)
        if r.returncode != 0:
            if _head(root) is None:                          # an unborn HEAD: no entries, no symlinks
                return {}
            # a real git failure must not read as "no HEAD entries": that would skip guard 5's
            # HEAD-mode checks for the whole batch
            raise PlanError(f"git ls-tree exited {r.returncode}: {_dec(r.stderr).strip()}")
        for rec in _dec(r.stdout).split("\0"):
            if rec:
                meta, _, name = rec.partition("\t")
                mode, _typ, oid = meta.split()
                if name in want:
                    found[name] = (mode, oid)
    return found


def _hash_many(root, paths) -> dict[str, str | None]:
    """The blob id `git add` would record for each worktree file (nothing is written); None when
    the path does not exist."""
    paths = list(dict.fromkeys(paths))
    live = [p for p in paths if os.path.lexists(root / p)]
    oids = {}
    for ch in _batches(live):
        out = _dec(_lg(root, "hash-object", "--", *ch).stdout).split()
        oids.update(zip(ch, out))
    return {p: oids.get(p) for p in paths}


def _prefixes(path) -> list[str]:
    parts = posixpath.normpath(path).split("/") if path else []
    return ["/".join(parts[:i + 1]) for i in range(len(parts))]


def _structurally_bad(path) -> bool:
    return not path or path.startswith("/") or ".." in path.split("/") or not _prefixes(path)


def _bad_in(root, path, head, ents) -> bool:
    """`_bad_path` against precomputed HEAD entries and index entries (see `_lookups`)."""
    if _structurally_bad(path):
        return True
    prefixes = _prefixes(path)
    if any((root / pre).is_symlink() for pre in prefixes):
        return True
    for pre in prefixes:                                     # a symlink in HEAD, here or above
        h = head.get(pre)
        if h and (h[0] == "120000" or (pre == path and h[0] == "160000")):
            return True
    full = root / path
    if full.is_dir():                                        # a directory, which a gitlink is
        return True
    es = ents.get(path, [])
    if any(m in ("120000", "160000") for m, _o, _s in es):
        return True
    return not (head.get(path) or es or os.path.lexists(full))


def _lookups(root, paths):
    """HEAD entries (for every path and its parents) and index entries, in a bounded number of
    git calls however many paths there are."""
    ok = [p for p in paths if not _structurally_bad(p)]
    names = {pre for p in ok for pre in _prefixes(p)}
    return _head_map(root, names), _index_map(root, ok)


def _bad_path(root, path) -> bool:
    head, ents = _lookups(root, [path])
    return _bad_in(root, path, head, ents)


def verb_refusal(root, plan) -> str | None:
    """Guards 5 and 6 of `apply_clear`, read-only: the error text the verb would refuse with,
    or None. `recommended` calls it too, so a human is never told to run a command that refuses.
    A bounded number of git calls: one lookup each for HEAD, the index and the worktree hashes."""
    root = Path(root)
    paths = list(plan.get("paths") or [])
    head, ents = _lookups(root, paths)
    for p in paths:
        if _bad_in(root, p, head, ents):
            return f"bad path {p}"
    suspect = [p for p in paths if ents.get(p) and ents[p][0][1] != (head.get(p) or (0, None))[1]]
    wt = _hash_many(root, suspect)
    for p in suspect:
        if ents[p][0][1] != wt[p]:
            return f"partially staged {p}"
    return _guard7(root, plan, paths)


def _base_refusal(root, base) -> str | None:
    """Guard 7's base check: a valid branch name that is not option-shaped and exists."""
    if not isinstance(base, str) or not base or base.startswith("-"):
        return f"bad base {base}"
    if _git(root, "check-ref-format", "--branch", base, check=False).returncode != 0:
        return f"bad base {base}"
    if _git(root, "show-ref", "--verify", "--quiet", f"refs/heads/{base}", check=False).returncode:
        return f"bad base {base}"
    return None


def _guard7(root, plan, paths) -> str | None:
    """SPEC-0549 guard 7, for the actions that save or switch: the base, a detached HEAD that is
    on a branch and, only when there is tracked dirt to save, a place for the WIP worktree."""
    if plan.get("action") not in _SAVE_ACTIONS:
        return None
    base = plan["base"] if "base" in plan else _base(root, None)
    bad = _base_refusal(root, base)
    if bad:
        return bad
    if _git(root, "symbolic-ref", "-q", "HEAD", check=False).returncode != 0:      # detached
        out = _git(root, "for-each-ref", "--count=1", "--contains", "HEAD", "--format=%(refname)",
                   "refs/heads").stdout
        if not _dec(out).strip():
            return "detached HEAD not on any branch"
    if not paths:
        return None
    _doc, error = _wip_config(root)
    return error


def _wip_config(root):
    """(`.nw/project.yaml` doc, error text): the config that places the WIP worktree."""
    path = P.config_path(root)
    doc = None
    if path.is_file():
        try:
            doc = P.load_project_config(path)
        except (ValueError, TypeError, OSError) as e:
            return None, f"bad worktrees config: {e}"
    project = (doc or {}).get("project")
    if doc is None or project is None:
        return None, "no project config to place the WIP worktree"
    if (not isinstance(project, str) or not project or project in (".", "..")
            or any(c in project for c in "/\\\0") or project != project.strip()):
        return None, "bad project name"
    try:
        P.worktree_root(doc, root, project)
    except (AttributeError, TypeError, ValueError, OSError):
        return None, "bad worktrees config"
    return doc, None


@contextlib.contextmanager
def _clear_lock(root):
    """One clear per checkout: a non-blocking flock in the git dir, released with the process
    (a crashed clear leaves nothing stale)."""
    gdir = _dec(_git(root, "rev-parse", "--absolute-git-dir").stdout).strip()
    fd = os.open(os.path.join(gdir, "nw-main-dirty.lock"), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _tracked_clean(root) -> bool:
    return not _dec(_git(root, "status", "--porcelain", "--untracked-files=no").stdout).strip()


def _head(root):
    r = _git(root, "rev-parse", "HEAD", check=False)
    return _dec(r.stdout).strip() if r.returncode == 0 else None


def _stage(root, paths):
    """Stage exactly the planned paths (the hook point between the recorded hashes and the commit)."""
    return _lg(root, "add", "--", *paths, check=False)


def _restore_index(idx, data, mode) -> str | None:
    """Put the saved index bytes back through git's own protocol: create `<index>.lock` with
    O_EXCL, write, `os.replace` over the index. None on success, else the error text."""
    lock = idx + ".lock"
    try:
        fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return "index locked, not restored"
    except OSError as e:
        return f"index not restored: {e}"
    replaced = False
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(lock, mode)
        os.replace(lock, idx)
        replaced = True
    except OSError as e:
        return f"index not restored: {e}"
    finally:
        if not replaced:                   # also on a second interrupt: never leave a stale lock
            with contextlib.suppress(OSError):
                os.unlink(lock)
    return None


def _commit_trivial(root, paths) -> dict:
    """The `commit` action: stage the planned paths, check what is staged, commit. On any
    failure before a commit lands the index is put back byte-identical (no reset, no restore)."""
    idx = _dec(_git(root, "rev-parse", "--git-path", "index").stdout).strip()
    idx = idx if os.path.isabs(idx) else str(root / idx)
    data, mode = Path(idx).read_bytes(), os.stat(idx).st_mode & 0o777
    head0 = _head(root)
    want = _hash_many(root, paths)

    def fail(error, restore=True):
        try:
            head = _head(root)
        except (PlanError, OSError):       # cannot tell: nothing of ours has moved HEAD, still restore
            head = head0
        if head not in (None, head0):                        # a commit landed: leave the index
            return {"ok": False, "sha": head, "error": error}
        if not restore:
            return {"ok": False, "error": error}
        return {"ok": False, "error": _restore_index(idx, data, mode) or error}

    def on_signal(signum, _frame):         # SIGTERM / SIGHUP unwind like Ctrl-C, so the index is put back
        raise SystemExit(128 + signum)

    saved = {}
    for sig in (signal.SIGTERM, signal.SIGHUP):
        with contextlib.suppress(ValueError, OSError):       # not the main thread: no handler
            saved[sig] = signal.signal(sig, on_signal)
    try:
        return _stage_and_commit(root, paths, want, fail)
    except (PlanError, OSError) as e:
        return fail(str(e))
    except BaseException:                  # ValueError, Ctrl-C, SIGTERM: put the index back, re-raise
        with contextlib.suppress(Exception):
            fail("interrupted")
        raise
    finally:
        for sig, old in saved.items():
            with contextlib.suppress(ValueError, OSError):
                signal.signal(sig, old)


def _stage_and_commit(root, paths, want, fail) -> dict:
    r = _stage(root, paths)
    if r.returncode != 0:
        # `git add` updates the index atomically, so a failed add changed nothing: nothing to
        # restore, and restoring the bytes read before it could overwrite a concurrent write
        return fail(_dec(r.stderr).strip() or "git add failed", restore=False)
    names = set(_dec(_lg(root, "diff", "--cached", "--no-renames", "--name-only", "-z"
                         ).stdout).split("\0")) - {""}
    for p in sorted(names ^ set(paths)):
        return fail(f"changed since plan {p}")
    staged = _index_map(root, paths)
    for p in paths:
        ents = staged.get(p, [])
        if len(ents) != 1 or ents[0][1] != want[p]:
            return fail(f"changed since plan {p}")
    msgs = ["-m", _COMMIT_SUBJECT] + [x for p in paths for x in ("-m", p)]
    try:
        # hooks run inside `git commit`: a generous bound, and a kill (which leaves hook
        # children possibly running) never restores the index under them
        r = _lg(root, "commit", *msgs, check=False, timeout=_COMMIT_TIMEOUT)
    except PlanError as e:
        return fail(f"{e}; index not restored", restore=False)
    if r.returncode != 0:
        return fail(_dec(r.stderr or r.stdout).strip() or "git commit failed")
    sha = _head(root)
    if not _tracked_clean(root):
        return {"ok": False, "sha": sha, "error": "main not tracked-clean after commit"}
    return {"ok": True, "sha": sha}


# ---- save to a WIP branch (SPEC-0549) --------------------------------------------------

def _raw_oids(root, paths) -> dict[str, str | None]:
    """The blob id of each worktree file's raw bytes (`--no-filters`: no clean filter, no EOL
    conversion); None when the path does not exist. Nothing is written."""
    paths = list(dict.fromkeys(paths))
    live = [p for p in paths if os.path.lexists(root / p)]
    oids = {}
    for ch in _batches(live):
        out = _dec(_lg(root, "hash-object", "--no-filters", "--", *ch).stdout).split()
        oids.update(zip(ch, out))
    return {p: oids.get(p) for p in paths}


def _file_mode(root, path) -> str:
    return "100755" if os.stat(root / path).st_mode & 0o111 else "100644"


def _allocate_wip_slot(root, parent) -> tuple[str, Path]:
    """`wip/main-dirty-<ts>` and `<parent>/main-dirty-<ts>`, suffix -2, -3, ... when either exists."""
    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    n = 1
    while True:
        sfx = "" if n == 1 else f"-{n}"
        branch, path = f"wip/main-dirty-{ts}{sfx}", Path(parent) / f"main-dirty-{ts}{sfx}"
        taken = _dec(_git(root, "branch", "--list", branch).stdout).strip()
        if not taken and not os.path.lexists(path):
            return branch, path
        n += 1


def _worktree_registered(root, path) -> bool:
    out = _dec(_git(root, "worktree", "list", "--porcelain", check=False).stdout)
    want = os.path.realpath(path)
    return any(ln.startswith("worktree ") and os.path.realpath(ln[9:]) == want
               for ln in out.splitlines())


def _discard_wip(root, branch, path, error, created) -> dict:
    """Rows (a)-(c): remove the WIP worktree first, then delete the branch (no commit on it).
    A failing cleanup keeps what remains and names both failures."""
    problems = []
    if _worktree_registered(root, path):
        r = _git(root, "worktree", "remove", "--force", str(path), check=False)
        if r.returncode != 0:
            problems.append(_dec(r.stderr or r.stdout).strip() or "worktree remove failed")
    if not problems and created and os.path.lexists(path):
        shutil.rmtree(path, ignore_errors=True)           # a partial directory the verb created
    if not problems and _dec(_git(root, "branch", "--list", branch).stdout).strip():
        r = _git(root, "branch", "-d", branch, check=False)
        if r.returncode != 0:
            problems.append(_dec(r.stderr or r.stdout).strip() or "branch -d failed")
    if problems:
        error = f"{error}; cleanup failed: {'; '.join(problems)}"
    return {"ok": False, "error": error}


def _copy_to_wip(root, wip, copies) -> None:
    """Copy each non-deleted path's bytes and mode into the WIP tree."""
    for p, mode in copies.items():
        dst = Path(wip) / p
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / p, dst)
        os.chmod(dst, 0o755 if mode == "100755" else 0o644)


def _stage_in_wip(wip, copies, deletions) -> None:
    """Raw bytes into the index (`hash-object --no-filters`, `update-index --cacheinfo`: `git add`
    would apply clean filters and EOL conversion); a deletion goes through `git rm -f`."""
    for p, mode in copies.items():
        oid = _dec(_lg(wip, "hash-object", "-w", "--no-filters", "--", p).stdout).strip()
        _lg(wip, "update-index", "--add", "--cacheinfo", f"{mode},{oid},{p}")
    for p in deletions:
        _lg(wip, "rm", "-f", "--", p)


def _wip_commit(wip, klass, paths):
    msgs = ["-m", f"{_WIP_SUBJECT} ({klass})"] + [x for p in paths for x in ("-m", p)]
    return _lg(wip, "commit", "--allow-empty", *msgs, check=False, timeout=_COMMIT_TIMEOUT)


def _verify_wip(root, sha, paths, modes, head) -> str | None:
    """From the WIP commit: each path's blob and mode equals the raw main file (absent for a
    deletion), and the commit's changed paths are exactly the planned ones."""
    want = _raw_oids(root, [p for p in paths if modes[p]])
    got = _head_map(root, paths, rev=sha)
    for p in paths:
        if modes[p] is None:
            if p in got:
                return f"WIP commit still holds deleted {p}"
        elif got.get(p) != (modes[p], want[p]):
            return f"WIP blob or mode differs from main for {p}"
    names = set(_dec(_git(root, "diff-tree", "-r", "--name-only", "--no-renames", "-z", "HEAD",
                          sha).stdout).split("\0")) - {""}
    expected = {p for p in paths
                if (p in head if modes[p] is None else (modes[p], want[p]) != head.get(p))}
    if names != expected:
        return f"WIP commit paths differ from the plan: {sorted(names ^ expected)}"
    return None


def _reverify_path(root, p, mode, oid) -> str | None:
    """Just before restoring `p`: main still holds exactly what the WIP commit holds."""
    if mode is None:
        return "changed since the save" if os.path.lexists(root / p) else None
    if not os.path.isfile(root / p) or os.path.islink(root / p):
        return "changed since the save"
    if _file_mode(root, p) != mode or _raw_oids(root, [p])[p] != oid:
        return "changed since the save"
    return None


def _restore_path(root, p, in_head) -> None:
    if in_head:
        _lg(root, "checkout", "HEAD", "--", p)
    else:
        _lg(root, "rm", "-f", "--", p)


def _restore_main(root, paths, modes, oids, head):
    """Restore main path by path, re-verifying each just before. None, or (path, error)."""
    for p in paths:
        try:
            bad = _reverify_path(root, p, modes[p], oids.get(p))
            if bad:
                return p, bad
            _restore_path(root, p, p in head)
        except (PlanError, OSError) as e:
            return p, str(e)
    return None


def _save_to_wip(root, live, doc) -> dict:
    """Save the tracked dirt to a `wip/main-dirty-*` branch in a worktree, verify it from the WIP
    commit, and only then restore main path by path. Result keys by failure phase: see SPEC-0549."""
    paths = sorted(set(live["paths"]))
    project = doc["project"]
    parent = P.worktree_root(doc, root, project)
    branch, wip = _allocate_wip_slot(root, parent)
    head = _head_map(root, paths)
    modes = {p: _file_mode(root, p) if os.path.lexists(root / p) else None for p in paths}
    oids = _raw_oids(root, [p for p in paths if modes[p]])
    copies = {p: m for p, m in modes.items() if m}
    deletions = [p for p in paths if modes[p] is None and p in head]
    created = not os.path.lexists(wip)
    r = _git(root, "worktree", "add", "-b", branch, str(wip), "HEAD", check=False)         # (a)
    if r.returncode != 0:
        return _discard_wip(root, branch, wip, _dec(r.stderr or r.stdout).strip()
                            or "git worktree add failed", created)
    try:
        _copy_to_wip(root, wip, copies)                                                    # (b)
        _stage_in_wip(wip, copies, deletions)
    except (PlanError, OSError) as e:
        return _discard_wip(root, branch, wip, str(e), created)
    try:
        r = _wip_commit(wip, live["class"], paths)                                         # (c)
    except PlanError as e:
        return _discard_wip(root, branch, wip, str(e), created)
    if r.returncode != 0:
        return _discard_wip(root, branch, wip, _dec(r.stderr or r.stdout).strip()
                            or "git commit failed", created)
    sha = _head(wip)
    keep = {"wip_branch": branch, "wip_path": str(wip), "wip_sha": sha}
    try:
        bad = _verify_wip(root, sha, paths, modes, head)                                   # (d)
    except (PlanError, OSError) as e:
        bad = str(e)
    if bad:
        return {"ok": False, "error": bad, **keep}
    failed = _restore_main(root, paths, modes, oids, head)                                 # (e)
    if failed:
        return {"ok": False, "error": f"{failed[0]}: {failed[1]}", **keep}
    try:
        dirty = _dec(_git(root, "status", "--porcelain", "--untracked-files=no").stdout)   # (f)
        tip = _dec(_git(root, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}",
                        check=False).stdout).strip()
    except (PlanError, OSError) as e:
        return {"ok": False, "error": str(e), **keep}
    if dirty.strip() or tip != sha:
        left = [ln[3:] for ln in dirty.splitlines()] or ["the WIP commit is missing"]
        return {"ok": False, "error": f"main not restored: {', '.join(left)}", **keep}
    return {"ok": True, **keep}


def _hold_switch(out, root, arg_base) -> None:
    """`hold`: with main tracked-clean, switch to the base (read again after the restore: HEAD's
    config may differ from the stray dirty one), then report the real re-plan."""
    out["ok"] = False
    try:
        base = _base(root, arg_base)
        bad = _base_refusal(root, base)
        if bad:
            out["error"] = bad
            return
        if not _tracked_clean(root):
            out["error"] = "main not tracked-clean"
            return
        r = _git(root, "switch", "--no-guess", base, check=False)
        err = _dec(r.stderr or r.stdout).strip()
        live = plan_clear(root, base=arg_base)
    except (PlanError, OSError, ValueError) as e:
        out["error"] = str(e)
        return
    out["replanned"] = {"on_base": live["on_base"], "action": live["action"], "class": live["class"]}
    if r.returncode != 0:
        out["error"] = err or "git switch failed"
    elif not live["on_base"] or not _tracked_clean(root):
        out["error"] = "not on the base and tracked-clean after the switch"
    else:
        out["ok"] = True


def _save_clear(out, root, live, arg_base) -> None:
    """`save_branch`, `branch_and_escalate`, `hold` (SPEC-0549)."""
    if live["paths"]:
        doc, error = _wip_config(root)
        if error:
            out["error"] = error
            return
        out.update(_save_to_wip(root, live, doc))
        if not out["ok"]:
            return
    if live["action"] == "hold":
        _hold_switch(out, root, arg_base)
    else:
        out["ok"] = True


def apply_clear(root, plan=None, base=None) -> dict:
    """Clear a dirty base checkout (SPEC-0548): re-plan live, refuse an operation in progress,
    a stale `plan` file, unsafe paths and partial staging, and only then run the live action.
    `plan` (a plan file) is a check, never an input; `base` is the CLI's `--base`."""
    out = {"schema": APPLY_SCHEMA, "root": os.path.abspath(root), "action": "none", "ok": False,
           "escalate": False}
    try:
        with _clear_lock(Path(root)) as got:
            if not got:
                out["error"] = "clear already in progress"
                return out
            return _apply_locked(out, Path(root), plan, base)
    except (NotAWorktree, PlanError, OSError, ValueError, TypeError, AttributeError) as e:
        out.update({"ok": False, "error": str(e) or type(e).__name__})
        return out


def _apply_locked(out, root, plan, base) -> dict:
    live = plan_clear(root, base=base)
    out["root"], out["action"] = live["root"], live["action"]
    out["escalate"] = live["class"] in ("substantive", "merge")
    root = Path(live["root"])
    if live["class"] == "merge":
        out["error"] = f"{live['in_progress']} in progress"
        return out
    if plan is not None and (plan.get("action") != live["action"]
                             or sorted(plan.get("paths") or []) != sorted(live["paths"])):
        out["error"] = "plan is stale"
        return out
    refusal = verb_refusal(root, live)
    if refusal:
        out["error"] = refusal
        return out
    action = live["action"]
    if action == "resume":
        out["ok"] = _tracked_clean(root)
        if not out["ok"]:
            out["error"] = "main not tracked-clean"
    elif action == "commit":
        out.update(_commit_trivial(root, live["paths"]))
    elif action in _SAVE_ACTIONS:
        _save_clear(out, root, live, base)
    else:
        out["error"] = f"nothing to apply: {action}"
    return out
