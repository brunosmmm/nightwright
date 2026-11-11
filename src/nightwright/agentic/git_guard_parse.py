"""Tokenizer, directory tracking and policy of the conductor git guard (SPEC-0550); the light
entry point is `git_guard`. `decide(command, cwd, environ)` returns a deny reason, or None when
the hook stays silent. See the spec's Design for the grammar this models; anything outside it
fails closed (the caller has already established that the session is a conductor's)."""
from __future__ import annotations

import os
import re
import shlex
import subprocess

PUNCT = "();<>|&\n"
_OP_SPLIT = re.compile(r"&&|\|\||>>|>&|<<|[;|\n()<>&]")
_SEG_OPS = (";", "&&", "||", "|", "\n")
_REDIRECTS = ("<", ">", ">>", ">&")
_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
_GIT_ENV_RE = re.compile(r"(GIT_DIR|GIT_WORK_TREE|GIT_INDEX_FILE|GIT_COMMON_DIR)=")
_HEREDOC_RE = re.compile(r"\$\(cat <<['\"]?(\w+)['\"]?\n(.*)\n\1\n\)\Z", re.S)
_MENTION_RE = re.compile(r"\b(git|cp)\b|main-dirty-apply")
_RESERVED = frozenset({"command", "exec", "if", "then", "else", "elif", "do", "while", "until",
                       "!", "time"})
_UNSUPPORTED_WORDS = frozenset({"{", "}", "pushd", "popd", "case", "select", "function", "[["})
_UV_VALUE_OPTS = frozenset({"--project", "--directory", "--with", "--python", "-p", "--package",
                            "--env-file", "--extra", "--group"})
_UNKNOWN_CHARS = frozenset("$~*?[{`")
_GLOBAL_OK = frozenset({"--literal-pathspecs", "--no-pager", "-P", "--no-replace-objects",
                        "--no-optional-locks"})
_READ_ONLY = frozenset({"status", "log", "diff", "show", "rev-parse", "rev-list", "merge-base",
                        "ls-files", "ls-tree", "cat-file", "describe", "blame", "shortlog",
                        "show-ref", "for-each-ref", "grep", "version", "help",
                        "fetch", "push"})
_BRANCH_OK = frozenset({"--list", "-a", "-r", "-v", "-vv", "--show-current"})
_CP_FLAGS = frozenset("aflnPpRrsuvx")


class Unsupported(Exception):
    """A command outside the modelled grammar (`str(e)` says which construct)."""


class Deny(Exception):
    """A fail-closed verdict; `str(e)` is the deny reason."""


def _cannot(what):
    return Deny(f"conductor-git-guard: cannot verify {what} (SPEC-0550); "
                "use a plain single git command in the worktree")


class Segment:
    def __init__(self):
        self.words = []             # arguments, redirects removed
        self.redirects = []         # (op, target)
        self.term = None            # the operator that ended it (None at the end)
        self.prev = None            # the operator that ended the previous segment


def tokenize(command: str) -> list[Segment]:
    """Quote-aware split of `command` into segments; raises `Unsupported`."""
    if "\\\n" in command:
        raise Unsupported("a backslash-newline continuation")
    lex = shlex.shlex(command, posix=True, punctuation_chars=PUNCT)
    lex.whitespace = " \t\r"
    lex.whitespace_split = True
    lex.commenters = ""
    try:
        tokens = list(lex)
    except ValueError as e:
        raise Unsupported(f"the quoting ({e})")
    segs = [Segment()]
    pending = None                  # a redirect operator waiting for its target word
    for tok in tokens:
        if tok and all(c in PUNCT for c in tok):
            ops = _OP_SPLIT.findall(tok)
            if "".join(ops) != tok or ";;" in tok:
                raise Unsupported(f"the operator {tok!r}")
        else:
            ops = None
        for op in ops if ops is not None else [None]:
            seg = segs[-1]
            if op is None:
                if pending is not None:
                    seg.redirects.append((pending, tok))
                    pending = None
                    continue
                if tok in _UNSUPPORTED_WORDS:
                    raise Unsupported(f"the word {tok!r}")
                if "`" in tok or "$(" in tok:
                    m = _HEREDOC_RE.match(tok)
                    if not m or "`" in m.group(2) or "$(" in m.group(2):
                        raise Unsupported("a command substitution")
                    tok = "<heredoc>"
                elif "\n" in tok:
                    raise Unsupported("a newline inside a word")
                seg.words.append(tok)
            elif op in ("(", ")", "<<", "&"):
                raise Unsupported(f"the operator {op!r}")
            elif op in _REDIRECTS:
                if pending is not None:
                    raise Unsupported("a redirect without a target")
                if seg.words and seg.words[-1].isdigit():
                    seg.words.pop()
                pending = op
            else:
                if pending is not None:
                    raise Unsupported("a redirect without a target")
                seg.term = op
                nxt = Segment()
                nxt.prev = op
                segs.append(nxt)
    if pending is not None:
        raise Unsupported("a redirect without a target")
    return [s for s in segs if s.words or s.redirects or s.term]


def split_prefix(words):
    """(assignments, index of the program word or None): skips NAME=value, `env`, `command`,
    `exec` and the shell reserved words that precede a command."""
    assigns, i = [], 0
    while i < len(words):
        w = words[i]
        if _NAME_RE.match(w):
            assigns.append(w)
        elif w == "env":
            if i + 1 < len(words) and words[i + 1].startswith("-"):
                raise Unsupported("an `env` option")
        elif w == "time":
            if i + 1 < len(words) and words[i + 1].startswith("-"):
                raise Unsupported("a `time` option")
        elif w not in _RESERVED:
            return assigns, i
        i += 1
    return assigns, None


def _unknown_path(p) -> bool:
    return any(c in _UNKNOWN_CHARS for c in p)


class Guard:
    def __init__(self, cwd, environ):
        self.cwd = cwd
        self.env = {k: v for k, v in (environ or {}).items() if not k.startswith("GIT_")}
        self.env["GIT_OPTIONAL_LOCKS"] = "0"
        self._cache = {}

    # ---- directories -------------------------------------------------------------------
    def classify(self, d):
        """'primary', 'linked' or 'norepo' (git exits 128: not a repository / cannot change
        to); `Deny` for any other failure."""
        if d in self._cache:
            return self._cache[d]
        try:
            r = subprocess.run(
                ["git", "-C", d, "rev-parse", "--path-format=absolute", "--git-dir",
                 "--git-common-dir"], capture_output=True, text=True, timeout=2, env=self.env)
        except subprocess.TimeoutExpired:
            raise _cannot("the repository (git timed out)")
        if r.returncode == 128 and ("not a git repository" in r.stderr
                                    or "cannot change to" in r.stderr):
            res = ("norepo", None)
        elif r.returncode != 0:
            raise _cannot("the repository (git rev-parse failed)")
        else:
            lines = r.stdout.splitlines()
            if len(lines) != 2 or not all(os.path.isabs(x) for x in lines):
                raise _cannot("the repository (unexpected git rev-parse output)")
            gd, cd = (os.path.realpath(x) for x in lines)
            if gd == cd:
                top = os.path.dirname(gd) if os.path.basename(gd) == ".git" else gd
                res = ("primary", top)
            else:
                res = ("linked", None)
        self._cache[d] = res
        return res

    def resolve(self, cur, p):
        """Absolute real path of `p` against `cur`; None when unknown."""
        if _unknown_path(p):
            return None
        if os.path.isabs(p):
            return os.path.realpath(p)
        if cur is None:
            return None
        return os.path.realpath(os.path.join(cur, p))

    def _ancestor(self, path):
        while path and not os.path.isdir(path):
            parent = os.path.dirname(path)
            if parent == path:
                break
            path = parent
        return path

    def check_dest(self, cur, p, what):
        """Deny when a file written at `p` lands in a primary worktree."""
        target = self.resolve(cur, p)
        if target is None:
            raise _cannot(f"the {what} {p!r}")
        kind, top = self.classify(self._ancestor(target))
        if kind == "primary":
            raise Deny(_primary_reason(top))

    # ---- segments ----------------------------------------------------------------------
    def run(self, segs):
        cur = self.cwd if isinstance(self.cwd, str) and os.path.isabs(self.cwd) else None
        skipping = False
        has_git = False
        for seg in segs:
            if skipping:        # chained by && (or a pipe) after a failed `cd X`: never runs
                if seg.term not in ("&&", "|"):
                    skipping, cur = False, None     # what follows runs in an unknown dir
                continue
            assigns, pi = split_prefix(seg.words)
            if pi is None:
                continue
            prog, args = seg.words[pi], seg.words[pi + 1:]
            base = os.path.basename(prog)
            if prog == "cd":
                cur, skipping = self.do_cd(cur, args, seg)
            elif base == "git":
                has_git = True
                self.do_git(cur, assigns, args, seg)
            elif base == "cp":
                self.do_cp(cur, args, seg)
            elif self.is_nw(seg.words, pi):
                self.do_verb(args)
        if has_git:
            for seg in segs:
                if any(_GIT_ENV_RE.match(w) for w in seg.words):
                    raise _cannot("a git environment override")

    def do_cd(self, cur, args, seg):
        """(new directory, skipping): `cd` followed by `;`, `&&` or newline changes it."""
        args = [a for a in args if a not in ("-L", "-P", "-e", "-@", "--")] \
            if args and args[0] in ("-L", "-P", "-e", "-@", "--") else args
        if (len(args) != 1 or args[0] == "-" or seg.term in ("|", "||")
                or seg.prev in ("|", "||")):
            return None, False
        target = self.resolve(cur, args[0])
        if target is None:
            return None, False
        if not os.path.isdir(target):
            # `cd X &&` skips what it chains; after `;` or a newline the shell, and a later
            # git, stay in the old directory (unknown from here on)
            return None, seg.term == "&&"
        return target, False

    def do_git(self, cur, assigns, args, seg):
        if any(_GIT_ENV_RE.match(a) for a in assigns):
            raise _cannot("a git environment override")
        self.check_redirects(cur, seg)      # relative to the shell's directory, not -C
        i, literal_c = 0, False
        while i < len(args) and args[i].startswith("-"):
            a = args[i]
            if a == "-C":
                if i + 1 >= len(args):
                    raise _cannot("a missing -C value")
                p = args[i + 1]
                cur = self.resolve(cur, p)
                literal_c = literal_c or cur is not None
                if cur is None:
                    raise _cannot(f"the -C value {p!r}")
                i += 2
            elif a in _GLOBAL_OK:
                i += 1
            else:       # --git-d*, --wo*, -c, --bare, --exec-path, anything else
                raise _cannot(f"the git option {a!r}")
        if cur is None:
            raise _cannot("the working directory")
        if literal_c and not os.path.isdir(cur):
            return
        kind, top = self.classify(cur)
        if kind == "norepo":
            if literal_c:
                return
            raise _cannot("the working directory (not a git repository)")
        if kind == "linked":
            return
        sub, rest = (args[i], args[i + 1:]) if i < len(args) else (None, [])
        if not self.primary_allowed(cur, sub, rest):
            raise Deny(_primary_reason(top))

    def primary_allowed(self, cur, sub, rest) -> bool:
        if sub in _READ_ONLY:
            return True
        if sub == "branch":
            if all(a in _BRANCH_OK for a in rest):
                return True
            return "--list" in rest and all(a in _BRANCH_OK or not a.startswith("-")
                                            for a in rest)
        if sub == "worktree" and rest:
            if rest[0] == "list":
                return True
            if rest[0] == "add":
                return self.worktree_add_ok(cur, rest[1:])
        return False

    def worktree_add_ok(self, cur, rest) -> bool:
        i = 0
        while i < len(rest) and rest[i].startswith("-"):
            if rest[i] == "-b" and i + 1 < len(rest):
                i += 2
            elif rest[i] in ("--detach", "--lock", "--no-checkout"):
                i += 1
            else:
                return False
        if i >= len(rest):
            return False
        target = self.resolve(cur, rest[i])
        return target is not None and ".nw-worktrees" in target.split(os.sep)

    def do_cp(self, cur, args, seg):
        if cur is None:
            raise _cannot("the working directory")
        positional, target_dir, only_pos, i = [], None, False, 0
        while i < len(args):
            a = args[i]
            if only_pos or not a.startswith("-") or a == "-":
                positional.append(a)
            elif a == "--":
                only_pos = True
            elif a == "-T":
                pass
            elif a.startswith("--target-directory="):
                target_dir = a.split("=", 1)[1]
            elif a == "-t" or (re.fullmatch(r"-[aflnPpRrsuvx]*t", a) is not None):
                if i + 1 >= len(args):
                    raise _cannot("a missing cp target directory")
                target_dir = args[i + 1]
                i += 1
            elif not (re.fullmatch(r"-[a-zA-Z]+", a) and set(a[1:]) <= _CP_FLAGS):
                raise _cannot(f"the cp option {a!r}")
            i += 1
        dest = target_dir if target_dir is not None else (positional[-1] if positional else None)
        if dest is None:
            raise _cannot("the cp destination")
        self.check_dest(cur, dest, "cp destination")
        self.check_redirects(cur, seg)

    def check_redirects(self, cur, seg):
        for op, target in seg.redirects:
            if op == "<" or (op == ">&" and (target.isdigit() or target == "-")):
                continue
            self.check_dest(cur, target, "redirect target")

    # ---- the verb ----------------------------------------------------------------------
    def is_nw(self, words, pi) -> bool:
        return self.nw_args(words, pi) is not None

    @staticmethod
    def nw_args(words, pi):
        """Index after the `nw` word for `nw ...` or `uv run [opts] nw ...`, else None."""
        base = os.path.basename(words[pi])
        if base == "nw":
            return pi + 1
        if base == "uv" and pi + 1 < len(words) and words[pi + 1] == "run":
            j = pi + 2
            while j < len(words) and words[j].startswith("-"):
                j += 2 if words[j] in _UV_VALUE_OPTS else 1
            if j < len(words) and os.path.basename(words[j]) == "nw":
                return j + 1
        return None

    def do_verb(self, args):
        if "main-dirty-apply" in args:
            nxt = args.index("main-dirty-apply") + 1
            if nxt >= len(args) or args[nxt] != "--trivial-only":
                raise Deny("conductor-git-guard: `main-dirty-apply` is allowed only with "
                           "`--trivial-only` for a conductor (SPEC-0550); escalate it")


def _primary_reason(top):
    return (f"conductor: no raw git writes in the main checkout {top or '(unknown)'}; "
            "escalate it (SPEC-0550)")


_WORD_SPLIT = re.compile(r"[\s;&|(){}<>$`\"'\\]+")
_COARSE_WORDS = frozenset({"cp", "cd", "pushd", "-C", "-c"})
_COARSE_PREFIXES = ("--chdir", "--git-d", "--wo", "--exec-path")
_COARSE_TEXT = ("core.worktree", "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR")


def _unsupported_reason(guard, command, cwd, why):
    """Step 2: an unsupported command is denied when it mentions git, cp or the verb; the one
    exception keeps SPEC-0496 worktree commits working (see the spec)."""
    if not _MENTION_RE.search(command):
        return None
    if "main-dirty-apply" not in command and isinstance(cwd, str) and os.path.isabs(cwd):
        words = _WORD_SPLIT.split(command)
        if (not any(w in _COARSE_WORDS or w.startswith(_COARSE_PREFIXES) for w in words)
                and not any(t in command for t in _COARSE_TEXT)):
            if guard.classify(cwd)[0] == "linked":
                return None
    return _cannot(f"{why} in a command it does not model").args[0]


def decide(command, cwd, environ=None):
    """The deny reason for `command` run in `cwd`, or None to stay silent."""
    guard = Guard(cwd, environ)
    try:
        segs = tokenize(command)
    except Unsupported as e:
        try:
            return _unsupported_reason(guard, command, cwd, str(e))
        except Deny as d:
            return str(d)
    try:
        guard.run(segs)
    except Unsupported as e:
        try:
            return _unsupported_reason(guard, command, cwd, str(e))
        except Deny as d:
            return str(d)
    except Deny as d:
        return str(d)
    return None
