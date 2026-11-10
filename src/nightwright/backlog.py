"""Public backlog on the orphan branch ``refs/heads/nw-backlog`` (SPEC-0449).

``add``, ``edit``, ``close``, and ``reopen`` read blobs by a captured sha and write them
through a temporary index. The product checkout, its index, and its current branch stay
as they were. Reads, fetch, and merge are later specs.
"""
from __future__ import annotations

import os
import re
import secrets
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import click
import yaml

REF = "refs/heads/nw-backlog"
ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_ID_RE = re.compile(r"^REQ-[0-9A-HJKMNP-TV-Z]{8}$")
_ID_ARG_RE = re.compile(r"^(REQ-[0-9A-HJKMNP-TV-Z]{8})(?:\.md)?$")
_SPEC_RE = re.compile(r"^SPEC-[0-9]{4}$")
_INSTANT_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
_PATH_OK = re.compile(r"^(?:README\.md|REQ-[0-9A-HJKMNP-TV-Z]{8}\.md)$")
_FRONT = re.compile(r"\A---\n(.*?)\n---\n(.*)\Z", re.DOTALL)
_REQUIRED = ("id", "title", "status", "created", "updated", "created_by", "updated_by")
_ZERO = "0" * 40
_ATTEMPTS = 3
_GIT_UNSET = (
    "GIT_INDEX_FILE",
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_NAMESPACE",
    "GIT_COMMON_DIR",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
)

_README_LINES = (
    "# nw-backlog",
    "",
    "This branch is the public backlog for this repository. It is not a product checkout.",
    "Do not build or run anything from it.",
    "",
    "Each `REQ-*.md` file at the root is one backlog item. There is no index file.",
)
README_TEXT = "\n".join(_README_LINES) + "\n"


class BacklogError(Exception):
    """A backlog command failed. ``exit_code`` is what the CLI exits with."""

    def __init__(self, message: str, exit_code: int = 1):
        super().__init__(message)
        self.exit_code = exit_code


class MintError(Exception):
    """``mint_id`` used its eight draws and every candidate collided."""


class _DuplicateKey(Exception):
    def __init__(self, key):
        self.key = key
        super().__init__(key)


class _ItemLoader(yaml.SafeLoader):
    """Safe loader that rejects a repeated key.

    The timestamp implicit resolver is removed on this subclass only. An unquoted
    ``YYYY-MM-DDTHH:MM:SSZ`` has to stay a string: the validation table accepts it with
    ``strptime``, and ``2026-02-31`` must be the ``created`` token rather than a loader
    error. ``yes`` / ``no`` / ``on`` / ``off`` stay YAML 1.1 booleans.
    """


def _construct_mapping(loader, node):
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        if not isinstance(key, str):
            raise yaml.constructor.ConstructorError(
                None, None, "expected a string key", key_node.start_mark)
        try:
            seen = key in mapping
        except TypeError as e:
            raise yaml.constructor.ConstructorError(
                None, None, "unhashable key", key_node.start_mark) from e
        if seen:
            raise _DuplicateKey(key)
        mapping[key] = loader.construct_object(value_node, deep=True)
    return mapping


_ItemLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)
_ItemLoader.yaml_implicit_resolvers = {
    key: [(tag, regexp) for tag, regexp in pairs if tag != "tag:yaml.org,2002:timestamp"]
    for key, pairs in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def current_time() -> datetime:
    return datetime.now(timezone.utc)


def _freeze(value: datetime) -> tuple[str, int]:
    """UTC instant and the floored unix second shared by frontmatter and both git dates."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    unix = int(value.timestamp())
    floored = datetime.fromtimestamp(unix, timezone.utc)
    return floored.strftime("%Y-%m-%dT%H:%M:%SZ"), unix


def _dq(value: str) -> str:
    """Double-quoted YAML scalar. Only ``\\`` and ``"`` are escaped."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _logical_summary(body: str) -> str:
    if body == "" or all(c == "\n" for c in body):
        return ""
    if body.startswith("\n"):
        body = body[1:]
    if body == "" or all(c == "\n" for c in body):
        return ""
    return body.rstrip("\n") + "\n"


def _normalize_summary(text: str) -> str:
    """Argument normalization. A leading newline is kept; parsing drops the separator."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if text == "" or all(c == "\n" for c in text):
        return ""
    return text.rstrip("\n") + "\n"


def _instant_ok(value) -> bool:
    if not isinstance(value, str) or _INSTANT_RE.fullmatch(value) is None:
        return False
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return False
    return True


def _person_ok(value) -> bool:
    return isinstance(value, str) and value.strip() != ""


def parse_item(filename: str, blob: bytes | str):
    """Parsed item dict, or the reason token of the first failing validation row."""
    data = blob.encode("utf-8") if isinstance(blob, str) else bytes(blob)
    if data.startswith(b"\xef\xbb\xbf"):
        return "bom"
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return "not utf-8"
    if "\r" in text:
        return "cr"
    matched = _FRONT.fullmatch(text)
    if matched is None:
        return "no frontmatter"
    try:
        doc = yaml.load(matched.group(1) + "\n", Loader=_ItemLoader)
    except _DuplicateKey as e:
        key = e.key if isinstance(e.key, str) else str(e.key)
        return f"duplicate key {_safe_path(key)}"
    except (yaml.YAMLError, ValueError, TypeError, RecursionError):
        return "invalid yaml"
    if isinstance(doc, dict) and any(not isinstance(key, str) for key in doc):
        return "invalid yaml"
    if not isinstance(doc, dict):
        return "not a mapping"
    for key in _REQUIRED:
        if key not in doc:
            return f"missing {key}"
    ident = doc["id"]
    if not isinstance(ident, str) or _ID_RE.fullmatch(ident) is None:
        return "bad id"
    stem = Path(filename).name
    if stem.endswith(".md"):
        stem = stem[:-3]
    if ident != stem:
        return "id mismatch"
    title = doc["title"]
    if not isinstance(title, str) or title.strip() == "" or "\n" in title:
        return "title"
    status = doc["status"]
    if not isinstance(status, str) or status not in ("open", "closed"):
        return "status"
    if not _instant_ok(doc["created"]):
        return "created"
    if not _instant_ok(doc["updated"]):
        return "updated"
    if not _person_ok(doc["created_by"]):
        return "created_by"
    if not _person_ok(doc["updated_by"]):
        return "updated_by"
    spec = None
    if "spec" in doc:
        spec = doc["spec"]
        if not isinstance(spec, str) or _SPEC_RE.fullmatch(spec) is None:
            return "spec"
    return {
        "id": ident,
        "title": title,
        "status": status,
        "created": doc["created"],
        "updated": doc["updated"],
        "created_by": doc["created_by"],
        "updated_by": doc["updated_by"],
        "spec": spec,
        "summary": _logical_summary(matched.group(2)),
    }


def render_item(item: dict) -> bytes:
    """Canonical item bytes. Unknown keys are not written. An empty summary has no body."""
    lines = [
        "---",
        f"id: {item['id']}",
        f"title: {_dq(item['title'])}",
        f"status: {item['status']}",
        f"created: {item['created']}",
        f"updated: {item['updated']}",
        f"created_by: {_dq(item['created_by'])}",
        f"updated_by: {_dq(item['updated_by'])}",
    ]
    if item.get("spec") is not None:
        lines.append(f"spec: {item['spec']}")
    lines.append("---")
    text = "\n".join(lines) + "\n"
    summary = item["summary"]
    if summary:
        text += "\n" + summary
    return text.encode("utf-8")


def mint_id(existing, draw=None) -> str:
    """Eight Crockford characters. ``existing`` is filenames; comparison is case-insensitive.

    ``draw`` is called with ``5`` and returns five bytes, high bits first. Eight colliding
    draws raise ``MintError`` and do not draw a ninth time.
    """
    if draw is None:
        draw = secrets.token_bytes
    taken = {name.casefold() for name in existing}
    for _ in range(8):
        raw = draw(5)
        n = int.from_bytes(raw, "big")
        chars = "".join(ALPHABET[(n >> ((7 - i) * 5)) & 31] for i in range(8))
        filename = f"REQ-{chars}.md"
        if filename.casefold() not in taken:
            return f"REQ-{chars}"
    raise MintError("could not mint an id")


def _safe_path(path: str) -> str:
    """Path text for stderr: no raw newlines, CR, or ESC; ``backslashreplace`` on encode."""
    out: list[str] = []
    for ch in path:
        o = ord(ch)
        if ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif o == 0x1B:
            out.append("\\x1b")
        elif o == 0x85:
            out.append("\\u0085")
        elif o == 0x2028:
            out.append("\\u2028")
        elif o == 0x2029:
            out.append("\\u2029")
        else:
            out.append(ch)
    return "".join(out).encode("utf-8", "backslashreplace").decode("utf-8")


_NON_PRINTABLE = yaml.reader.Reader.NON_PRINTABLE
_GIT_IDENT_CRUD = set(".,:;<>\"'\\")


def _is_git_ident_crud(ch: str) -> bool:
    return ord(ch) <= 32 or ch in _GIT_IDENT_CRUD


def _strip_git_ident_crud(name: str) -> str:
    start = 0
    end = len(name)
    while start < end and _is_git_ident_crud(name[start]):
        start += 1
    while end > start and _is_git_ident_crud(name[end - 1]):
        end -= 1
    return name[start:end]


def _yaml_unsafe_char(value: str) -> str | None:
    """Chars that break double-quoted scalars or PyYAML's reader (NEL, LS, PS, C0/DEL)."""
    for ch in value:
        o = ord(ch)
        if o in (0x85, 0x2028, 0x2029):
            return ch
        if _NON_PRINTABLE.match(ch):
            return ch
    return None


def _strict_utf8(field: str, value: str) -> None:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise BacklogError(f"{field} is not valid utf-8") from None


def _git(repo, args, *, env_extra=None, index=None, input=None, check=True):
    """Run one allowlisted git subcommand. ``GIT_INDEX_FILE`` is set only when ``index`` is."""
    env = os.environ.copy()
    for key in _GIT_UNSET:
        env.pop(key, None)
    env["GIT_NO_REPLACE_OBJECTS"] = "1"
    if index is not None:
        env["GIT_INDEX_FILE"] = str(index)
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, input=input, env=env)
    if check and proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip()
        raise BacklogError(err or f"git {' '.join(args)} failed")
    return proc


def _resolve_repo(repo) -> Path:
    raw = Path.cwd() if repo is None else Path(repo)
    inside = _git(raw, ["rev-parse", "--is-inside-work-tree"], check=False)
    bare = _git(raw, ["rev-parse", "--is-bare-repository"], check=False)
    if inside.returncode != 0 or bare.returncode != 0:
        raise BacklogError("not a git work tree")
    if inside.stdout.decode().strip() != "true" or bare.stdout.decode().strip() != "false":
        raise BacklogError("not a git work tree")
    top = _git(raw, ["rev-parse", "--show-toplevel"])
    raw_out = top.stdout
    if raw_out.endswith(b"\n"):
        raw_out = raw_out[:-1]
    return Path(raw_out.decode("utf-8", "surrogateescape"))


def _config_value(repo, key: str):
    """``(True, raw)`` or ``(False, "")`` when ``git config --get`` fails.

    Git appends one trailing newline to a successful value. That terminator is not part
    of the raw value; a newline that remains is a line break inside the value.
    """
    proc = _git(repo, ["config", "--get", key], check=False)
    if proc.returncode != 0:
        return False, ""
    raw = proc.stdout
    if raw.endswith(b"\n"):
        raw = raw[:-1]
    return True, raw.decode("utf-8", "surrogateescape")


def _identity_error(key: str, value: str, kind: str) -> str | None:
    if "\r" in value or "\n" in value:
        return f"{key} contains a line break"
    if value.strip() == "":
        return f"{key} is unset"
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return f"{key} is not valid utf-8"
    bad = _yaml_unsafe_char(value)
    if bad is not None:
        esc = _safe_path(repr(bad)[1:-1])
        return f"{key} contains control character {esc}"
    if kind == "name":
        if "<" in value:
            return "user.name contains '<'"
        if ">" in value:
            return "user.name contains '>'"
        stripped = _strip_git_ident_crud(value)
        if stripped == "":
            return "user.name consists only of disallowed characters"
        if stripped != value:
            start = 0
            while start < len(value) and _is_git_ident_crud(value[start]):
                start += 1
            end = len(value)
            while end > start and _is_git_ident_crud(value[end - 1]):
                end -= 1
            removed = value[:start] + value[end:]
            if any(c != '"' for c in removed):
                return "user.name contains a leading or trailing disallowed character"
        return None
    if any(c.isspace() for c in value):
        return "user.email contains whitespace"
    for ch in "<>\"\\":
        if ch in value:
            return f"user.email contains '{ch}'"
    return None


def _read_identity(repo) -> tuple[str, str]:
    errors = []
    values = []
    for key, kind in (("user.name", "name"), ("user.email", "email")):
        ok, value = _config_value(repo, key)
        if not ok:
            errors.append(f"{key} is unset")
            values.append("")
            continue
        err = _identity_error(key, value, kind)
        if err:
            errors.append(err)
            values.append("")
            continue
        values.append(value)
    if errors:
        raise BacklogError("\n".join(errors))
    return values[0], values[1]


def _checked_out(repo) -> list[str]:
    proc = _git(repo, ["worktree", "list", "--porcelain", "-z"])
    found = []
    path = None
    branch = None
    for part in proc.stdout.split(b"\0"):
        if not part:
            continue
        line = part.decode("utf-8", "surrogateescape")
        if line.startswith("worktree "):
            path = line[len("worktree "):]
            branch = None
        elif line.startswith("branch "):
            branch = line[len("branch "):]
            if path is not None and branch == REF:
                found.append(path)
    return found


def _refuse_checked_out(repo) -> None:
    paths = _checked_out(repo)
    if paths:
        raise BacklogError("\n".join(
            f"nw-backlog is checked out at {_safe_path(path)}" for path in paths))


def _ref_sha(repo) -> str | None:
    proc = _git(repo, ["rev-parse", "--verify", "--quiet", REF], check=False)
    if proc.returncode != 0:
        return None
    return proc.stdout.decode().strip()


def _ls_tree(repo, sha: str) -> list[tuple[str, str]]:
    proc = _git(repo, ["ls-tree", "-r", "-z", sha])
    entries = []
    for rec in proc.stdout.split(b"\0"):
        if not rec:
            continue
        meta, path = rec.split(b"\t", 1)
        mode = meta.split(b" ", 1)[0].decode()
        entries.append((mode, path.decode("utf-8", "surrogateescape")))
    return entries


def _problem_lines(entries) -> list[str]:
    found = []
    for mode, path in entries:
        if mode != "100644":
            found.append((path, "mode"))
        if _PATH_OK.fullmatch(path) is None:
            found.append((path, "stray"))
    found.sort(key=lambda item: (item[0], 0 if item[1] == "mode" else 1))
    return [f"{kind}: {_safe_path(path)}" for path, kind in found]


def _orphan_error(repo, tip: str, tip_entries) -> str | None:
    lines = _problem_lines(tip_entries)
    roots = _git(repo, ["rev-list", "--max-parents=0", tip]).stdout.decode().split()
    root_lines = []
    for root in sorted(roots):
        entries = tip_entries if root == tip else _ls_tree(repo, root)
        for problem in _problem_lines(entries):
            root_lines.append(f"root {root} {problem}")
    if not lines and not root_lines:
        return None
    return "refs/heads/nw-backlog is not an orphan backlog\n" + "\n".join(lines + root_lines)


def _show(repo, sha: str, path: str) -> bytes | None:
    proc = _git(repo, ["show", f"{sha}:{path}"], check=False)
    if proc.returncode != 0:
        return None
    return proc.stdout


def _author_env(name: str, email: str, unix: int) -> dict[str, str]:
    date = f"{unix} +0000"
    return {
        "GIT_AUTHOR_NAME": name,
        "GIT_AUTHOR_EMAIL": email,
        "GIT_COMMITTER_NAME": name,
        "GIT_COMMITTER_EMAIL": email,
        "GIT_AUTHOR_DATE": date,
        "GIT_COMMITTER_DATE": date,
    }


def _write_commit(repo, tip: str | None, path: str, blob: bytes, message: str,
                  name: str, email: str, unix: int, have_readme: bool) -> str:
    """Temporary index, then ``commit-tree``. The message has no stored trailing newline.

    ``git log --format=%B`` is tformat and appends one newline. The bytes given to
    ``commit-tree`` are the single line with no terminator, so ``%B`` is exactly
    ``backlog: <verb> <id>\\n`` and there is no blank body line.
    """
    with tempfile.TemporaryDirectory() as td:
        index = Path(td) / "index"
        if tip is None:
            _git(repo, ["read-tree", "--empty"], index=index)
        else:
            tree = _git(repo, ["rev-parse", f"{tip}^{{tree}}"]).stdout.decode().strip()
            _git(repo, ["read-tree", tree], index=index)
        item_blob = _git(
            repo, ["hash-object", "-w", "--stdin"], input=blob).stdout.decode().strip()
        _git(repo, ["update-index", "--add", "--cacheinfo", f"100644,{item_blob},{path}"],
             index=index)
        if not have_readme:
            readme_blob = _git(
                repo, ["hash-object", "-w", "--stdin"],
                input=README_TEXT.encode("utf-8")).stdout.decode().strip()
            _git(repo, ["update-index", "--add", "--cacheinfo",
                        f"100644,{readme_blob},README.md"], index=index)
        tree = _git(repo, ["write-tree"], index=index).stdout.decode().strip()
        args = ["commit-tree", tree]
        if tip is not None:
            args += ["-p", tip]
        args += ["-F", "-"]
        proc = _git(repo, args, input=message.encode("utf-8"),
                    env_extra=_author_env(name, email, unix))
        return proc.stdout.decode().strip()


def _check_title(title: str) -> None:
    if title.strip() == "":
        raise BacklogError("title is empty")
    if "\r" in title or "\n" in title:
        raise BacklogError("title must be one line")
    _strict_utf8("title", title)
    bad = _yaml_unsafe_char(title)
    if bad is not None:
        esc = _safe_path(repr(bad)[1:-1])
        raise BacklogError(f"title contains control character {esc}")


def _check_summary(summary: str) -> None:
    _strict_utf8("summary", summary)


def _check_spec(spec: str) -> None:
    if _SPEC_RE.fullmatch(spec) is None:
        raise BacklogError("invalid spec id")


def _ensure_renderable(ident: str, item: dict) -> None:
    """Refuse parsed values that ``parse_item`` may accept but ``render_item`` cannot round-trip."""
    for field in ("title", "created_by"):
        value = item[field]
        _strict_utf8(field, value)
        if "\r" in value or "\n" in value:
            raise BacklogError(f"invalid backlog item {ident}: {field}")
        if _yaml_unsafe_char(value) is not None:
            raise BacklogError(f"invalid backlog item {ident}: {field}")


def _parse_id_arg(item_id: str) -> str:
    matched = _ID_ARG_RE.fullmatch(item_id)
    if matched is None:
        raise BacklogError("invalid id")
    return matched.group(1)


def _check_add_args(title, summary, spec) -> None:
    if title is None:
        raise BacklogError("title is required")
    if summary is None:
        raise BacklogError("summary is required")
    _check_title(title)
    _check_summary(summary)
    if spec is not None:
        _check_spec(spec)


def _check_edit_args(item_id, title, summary, spec, clear_spec) -> str:
    ident = _parse_id_arg(item_id)
    if spec is not None and clear_spec:
        raise BacklogError("spec and clear-spec are mutually exclusive")
    if title is None and summary is None and spec is None and not clear_spec:
        raise BacklogError("nothing to change")
    if title is not None:
        _check_title(title)
    if summary is not None:
        _check_summary(summary)
    if spec is not None:
        _check_spec(spec)
    return ident


def _edit_noop(item, title, summary, spec, clear_spec) -> bool:
    if title is not None and title != item["title"]:
        return False
    if summary is not None and summary != item["summary"]:
        return False
    if spec is not None and spec != item["spec"]:
        return False
    if clear_spec and item["spec"] is not None:
        return False
    return True


def _same_ref(current: str | None, expected: str) -> bool:
    if expected == _ZERO:
        return current is None
    return current == expected


def _prepare(repo, now, draw):
    """Argument checks have already run. These four checks are not repeated on a retry."""
    root = _resolve_repo(repo)
    name, email = _read_identity(root)
    _refuse_checked_out(root)
    if now is None:
        now = current_time
    if draw is None:
        draw = secrets.token_bytes
    stamp, unix = _freeze(now())
    return root, name, email, stamp, unix, draw


def _commit(repo, *, op: str, title=None, summary=None, spec=None, clear_spec=False,
            ident=None, now=None, draw=None):
    root, name, email, stamp, unix, draw = _prepare(repo, now, draw)
    identity = f"{name} <{email}>"
    normalized = _normalize_summary(summary) if summary is not None else None
    minted = None
    for _attempt in range(_ATTEMPTS):
        tip = _ref_sha(root)
        entries = [] if tip is None else _ls_tree(root, tip)
        if tip is not None:
            err = _orphan_error(root, tip, entries)
            if err:
                raise BacklogError(err)
        paths = [path for _mode, path in entries]
        if op == "add":
            if minted is None:
                try:
                    minted = mint_id(paths, draw=draw)
                except MintError:
                    raise BacklogError("could not mint an id") from None
            elif f"{minted}.md".casefold() in {path.casefold() for path in paths}:
                raise BacklogError(f"{minted} is already on nw-backlog")
            item = {
                "id": minted,
                "title": title,
                "status": "open",
                "created": stamp,
                "updated": stamp,
                "created_by": identity,
                "updated_by": identity,
                "spec": spec,
                "summary": normalized,
            }
            path = f"{minted}.md"
            message = f"backlog: add {minted}"
        else:
            path = f"{ident}.md"
            blob = None if tip is None else _show(root, tip, path)
            if blob is None:
                raise BacklogError(f"{ident} is not on nw-backlog")
            parsed = parse_item(path, blob)
            if isinstance(parsed, str):
                raise BacklogError(f"invalid backlog item {ident}: {parsed}")
            if op == "close" and parsed["status"] == "closed":
                raise BacklogError(f"{ident} is already closed")
            if op == "reopen" and parsed["status"] == "open":
                raise BacklogError(f"{ident} is already open")
            if op == "edit" and _edit_noop(parsed, title, normalized, spec, clear_spec):
                return None
            item = dict(parsed)
            if op == "edit":
                if title is not None:
                    item["title"] = title
                if normalized is not None:
                    item["summary"] = normalized
                if spec is not None:
                    item["spec"] = spec
                if clear_spec:
                    item["spec"] = None
            elif op == "close":
                item["status"] = "closed"
            else:
                item["status"] = "open"
            item["updated"] = stamp
            item["updated_by"] = identity
            _ensure_renderable(ident, item)
            message = f"backlog: {op} {ident}"
            minted = ident
        new = _write_commit(
            root, tip, path, render_item(item), message, name, email, unix,
            have_readme="README.md" in paths)
        expected = _ZERO if tip is None else tip
        proc = _git(root, ["update-ref", REF, new, expected], check=False)
        if proc.returncode == 0:
            return minted
        if _same_ref(_ref_sha(root), expected):
            raise BacklogError("update-ref failed")
    raise BacklogError("nw-backlog changed")


def add(title, summary, spec=None, repo=None, *, now=None, draw=None) -> str:
    _check_add_args(title, summary, spec)
    return _commit(repo, op="add", title=title, summary=summary, spec=spec, now=now, draw=draw)


def edit(item_id, title=None, summary=None, spec=None, clear_spec=False, repo=None, *,
         now=None):
    ident = _check_edit_args(item_id, title, summary, spec, clear_spec)
    _commit(repo, op="edit", title=title, summary=summary, spec=spec, clear_spec=clear_spec,
            ident=ident, now=now)


def close(item_id, repo=None, *, now=None):
    ident = _parse_id_arg(item_id)
    _commit(repo, op="close", ident=ident, now=now)


def reopen(item_id, repo=None, *, now=None):
    ident = _parse_id_arg(item_id)
    _commit(repo, op="reopen", ident=ident, now=now)


def _run(fn) -> None:
    try:
        fn()
    except BacklogError as e:
        click.echo(str(e), err=True)
        raise SystemExit(e.exit_code) from None


@click.group("backlog")
def backlog():
    """Public backlog on the orphan nw-backlog branch."""


@backlog.command("add")
@click.option("--title", default=None, help="Item title.")
@click.option("--summary", default=None, help="Item summary.")
@click.option("--spec", "spec_id", default=None, help="SPEC-NNNN to record on the item.")
@click.option("--repo", default=None, help="Git work tree. Defaults to the current directory.")
def add_cmd(title, summary, spec_id, repo):
    """Commit one open item onto nw-backlog."""
    ident = None

    def go():
        nonlocal ident
        ident = add(title, summary, spec=spec_id, repo=repo)

    _run(go)
    click.echo(ident)


@backlog.command("edit")
@click.argument("item_id")
@click.option("--title", default=None, help="Replacement title.")
@click.option("--summary", default=None, help="Replacement summary. Empty clears the body.")
@click.option("--spec", "spec_id", default=None, help="SPEC-NNNN to record on the item.")
@click.option("--clear-spec", is_flag=True, help="Remove the spec id.")
@click.option("--repo", default=None, help="Git work tree. Defaults to the current directory.")
def edit_cmd(item_id, title, summary, spec_id, clear_spec, repo):
    """Replace the named fields of one item."""
    _run(lambda: edit(item_id, title=title, summary=summary, spec=spec_id,
                      clear_spec=clear_spec, repo=repo))


@backlog.command("close")
@click.argument("item_id")
@click.option("--repo", default=None, help="Git work tree. Defaults to the current directory.")
def close_cmd(item_id, repo):
    """Set one item's status to closed."""
    _run(lambda: close(item_id, repo=repo))


@backlog.command("reopen")
@click.argument("item_id")
@click.option("--repo", default=None, help="Git work tree. Defaults to the current directory.")
def reopen_cmd(item_id, repo):
    """Set one item's status to open."""
    _run(lambda: reopen(item_id, repo=repo))
