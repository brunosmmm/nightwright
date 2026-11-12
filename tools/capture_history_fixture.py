#!/usr/bin/env python3
"""SPEC-0415: capture a sanitized, size-bounded, deterministic subset of closed campaigns from a
nightwright data dir into `tests/fixtures/agentic_history/` (a valid `data_dir` of its own).

Standard library only (runs as `python3 tools/capture_history_fixture.py`). The source tree is
opened read-only. Exit codes: 0 ok, 1 `--check` found drift/leaks, 2 usage error or a required
scenario has no campaign, 3 output over the byte budget, 4 the verification scan found a leak.

Env: NW_DATA_DIR (default --src), NW_FIXTURE_MAX_BYTES (byte budget, default 3 MiB),
NW_FIXTURE_DENY_FILE (deny list, default tools/capture_history_deny.txt).
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEFAULT_OUT = REPO / "tests" / "fixtures" / "agentic_history"
DEFAULT_DENY = Path(__file__).resolve().parent / "capture_history_deny.txt"
SCHEMA = "nw.history.fixture.v1"
TOOL_VERSION = 1
MAX_CAMPAIGNS = 12
MAX_BYTES = 3 * 1024 * 1024
STREAM_LINES, STREAM_LINE_BYTES, STREAM_TEXT_CHARS = 120, 4096, 512
LOG_LINES, LOG_LINE_BYTES = 200, 1024
JSON_CAP_BYTES, JSON_VALUE_CHARS = 64 * 1024, 4096
RUNNER_LINES_PER_INTERVAL = 500
CANDIDATE_STATES = {"MERGED", "HANDED_OFF", "ABANDONED"}
TERMINAL = {"MERGED", "HANDED_OFF", "ABANDONED"}
PLACEHOLDER_EMAIL = "user@example.invalid"
DERIVED_PR_URL = "https://example.invalid/pr/1"
RUN_FILES = ("run.json", "result.json", "exit.json", "request.json", "stderr.log", "stream.jsonl")
# (key, required). `no_run_json`, `legacy_schema` and `handed_off` may be absent_in_source.
SCENARIOS = [("merged_multi_loop", True), ("abandoned", True), ("handed_off", False),
             ("human_escalation", True), ("pre_stream", True), ("flaky_and_triage", True),
             ("legacy_schema", False), ("no_run_json", False)]

# ---- sanitization rules --------------------------------------------------------------------

PATH_RE = re.compile(r"(?:/home|/Users)/([A-Za-z0-9_.-]+)")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
EMAIL_DOMAIN_RE = re.compile(r"@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
EMAIL_LOCAL_RE = re.compile(r"[A-Za-z0-9._%+-]")
TAIL_DOMAIN_RE = re.compile(r"[\w.-]*")            # an email cut in half by a truncation: local@domain
TAIL_LOCAL_RE = re.compile(r"[\w.%+-]")               # ... ends the string; this is one local-part char
TAIL_PATH_RE = re.compile(r"(?:/home|/Users)(?:/[A-Za-z0-9_.-]*)?$")   # a home path cut in half
SECRET_RES = [re.compile(p) for p in (
    r"sk-[A-Za-z0-9_-]{16,}", r"ghp_[A-Za-z0-9]{20,}", r"github_pat_[A-Za-z0-9_]{20,}",
    r"xox[abprs]-[A-Za-z0-9-]{10,}", r"AKIA[0-9A-Z]{16}", r"Bearer [A-Za-z0-9._~+/=-]{20,}")]
PEM_RE = re.compile(r"-----BEGIN [A-Z ]*-----.*?-----END [A-Z ]*-----", re.S)
PEM_BEGIN_RE = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
SECRET_KEY_RE = re.compile(r"(?i)(token|secret|password|api[_-]?key|authorization|cookie)")
IPV4_RE = re.compile(r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}"
                     r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b")
URL_HOST_RE = re.compile(r"^(https?://)([^/\s:]+)")
# Account names that are also ordinary words: rewriting them everywhere would mangle prose
# ("the runner log"), so they are rewritten only where they identify an account: a path segment
# (what `scan` checks), `name@host`, and the value of USER= / LOGNAME= / USERNAME=.
COMMON_WORD_USERS = frozenset({
    "root", "runner", "dev", "admin", "ubuntu", "debian", "build", "ci", "test", "worker",
    "agent", "deploy", "github", "jenkins", "docker", "service", "nobody", "ec2-user"})
SAFE_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")   # a spec_id / project used as a path part (fullmatch)
RULES = ("home_path", "user_name", "email", "secret", "host", "ipv4", "pr_url_host", "env")


class Sanitizer:
    """One table of rules (module constants above); hits are counted per rule. Host and IPv4 maps
    are stable by order of first appearance, so callers must feed files in a fixed order."""

    def __init__(self, hosts=(), users=()):
        self.counts = Counter({r: 0 for r in RULES})
        self.hosts = [h for h in sorted(set(hosts), key=lambda h: (-len(h), h)) if len(h) >= 3]
        self.users = [u for u in sorted(set(users)) if len(u) >= 3 and u != "user"]
        self.host_map, self.ip_map = {}, {}
        self.host_res = [re.compile(r"(?<![\w.-])" + re.escape(h) + r"(?![\w-])", re.I)
                         for h in self.hosts]
        self.user_res = []
        for u in self.users:
            e = re.escape(u)
            if u in COMMON_WORD_USERS:
                self.user_res += [
                    (re.compile(r"/" + e + r"(?![\w.-])"), "/user"),
                    (re.compile(r"(?<![\w.-])" + e + r"(?=@)"), "user"),
                    (re.compile(r"\b(USER|LOGNAME|USERNAME)=" + e + r"(?![\w.-])"), r"\1=user")]
            else:
                self.user_res.append((re.compile(r"(?<![\w])" + e + r"(?![\w])"), "user"))

    def _host(self, m):
        self.counts["host"] += 1
        key = m.group(0).lower()
        if key not in self.host_map:
            self.host_map[key] = f"worker-{len(self.host_map) + 1}"
        return self.host_map[key]

    def _ip(self, m):
        ip = m.group(0)
        if ip.startswith("10.0.0."):
            return ip
        self.counts["ipv4"] += 1
        if ip not in self.ip_map:
            self.ip_map[ip] = f"10.0.0.{len(self.ip_map) + 1}"
        return self.ip_map[ip]

    def text(self, s: str) -> str:
        def path(m):
            if m.group(1) == "user":
                return m.group(0)
            self.counts["home_path"] += 1
            return "/home/user"
        s = PATH_RE.sub(path, s)

        def email(m):
            if m.group(0) == PLACEHOLDER_EMAIL:
                return m.group(0)
            self.counts["email"] += 1
            return PLACEHOLDER_EMAIL
        s = sub_emails(email, s)
        for rx in (PEM_RE, *SECRET_RES):
            s, n = rx.subn("[REDACTED]", s)
            self.counts["secret"] += n
        for rx in self.host_res:
            s = rx.sub(self._host, s)
        s = IPV4_RE.sub(self._ip, s)
        for rx, repl in self.user_res:
            s, n = rx.subn(repl, s)
            self.counts["user_name"] += n
        return s

    def value(self, v, key=None, secret=False):
        """Sanitize a parsed JSON value (keys are never touched). Every string under a
        secret-named key is redacted, however deep (a list or dict value included)."""
        secret = secret or (key is not None and bool(SECRET_KEY_RE.search(key)))
        if isinstance(v, str):
            if secret and v and v != "[REDACTED]":
                self.counts["secret"] += 1
                return "[REDACTED]"
            if key == "pr_url":
                m = URL_HOST_RE.match(v)
                if m and m.group(2) not in ("github.com", "example.invalid"):
                    self.counts["pr_url_host"] += 1
                    v = m.group(1) + "example.invalid" + v[m.end():]
            return self.text(v)
        if isinstance(v, list):
            return [self.value(x, None, secret) for x in v]
        if isinstance(v, dict):
            out = {}
            for k, x in v.items():
                if k == "env" and isinstance(x, dict):
                    if x:
                        self.counts["env"] += 1
                    out[k] = {}
                else:
                    out[k] = self.value(x, k, secret)
            return out
        return v


class NullSanitizer(Sanitizer):
    """Identity: used to rank candidates by their size after caps."""

    def text(self, s):
        return s

    def value(self, v, key=None, secret=False):
        return v


def dumps(obj, indent=None) -> str:
    if indent:
        return json.dumps(obj, ensure_ascii=False, indent=indent)
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def decode(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace")


def san_json_text(raw: bytes, san: Sanitizer) -> bytes:
    """A .json file: unchanged bytes unless a rule changed a value."""
    try:
        obj = json.loads(raw)
    except ValueError:
        return san_plain(raw, san)
    new = san.value(obj)
    if new == obj:
        return raw
    text = decode(raw)
    return (dumps(new, 2 if text.startswith("{\n") else None) + ("\n" if text.endswith("\n") else "")).encode()


def jsonl_lines(text: str) -> list[str]:
    """Split on '\\n' only, endings kept. str.splitlines also splits on U+2028/U+2029/U+0085 and
    \\x0b-\\x1e, which JSON writers leave unescaped inside strings; a split line would dodge the
    key-based rules."""
    parts = text.split("\n")
    out = [p + "\n" for p in parts[:-1]]
    if parts[-1]:
        out.append(parts[-1])
    return out


def san_jsonl_line(line: str, san: Sanitizer) -> str:
    body = line.rstrip("\r\n")
    ending = line[len(body):]
    if not body.strip():
        return line
    try:
        obj = json.loads(body)
    except ValueError:
        return san.text(body) + ending
    new = san.value(obj)
    return line if new == obj else dumps(new) + ending


def san_jsonl(raw: bytes, san: Sanitizer) -> bytes:
    text = decode(raw)
    return "".join(san_jsonl_line(ln, san) for ln in jsonl_lines(text)).encode()


def san_plain(raw: bytes, san: Sanitizer) -> bytes:
    text = decode(raw)
    new = san.text(text)
    return raw if new == text else new.encode()


def sanitize_file(rel: str, raw: bytes, san: Sanitizer) -> bytes:
    if rel.endswith(".jsonl"):
        return san_jsonl(raw, san)
    if rel.endswith(".json"):
        return san_json_text(raw, san)
    return san_plain(raw, san)


# ---- caps ----------------------------------------------------------------------------------

def strings(v):
    """Every string value of a parsed JSON value, in order (keys excluded)."""
    if isinstance(v, str):
        yield v
    elif isinstance(v, list):
        for x in v:
            yield from strings(x)
    elif isinstance(v, dict):
        for x in v.values():
            yield from strings(x)


def sub_emails(repl, s: str) -> str:
    """`EMAIL_RE.sub(repl, s)` in linear time (the regex is quadratic on a long local-part run):
    find each `@domain`, then extend the local part backwards, never into the previous match."""
    out, pos = [], 0
    for m in EMAIL_DOMAIN_RE.finditer(s):
        j = m.start()
        while j > pos and EMAIL_LOCAL_RE.fullmatch(s[j - 1]):
            j -= 1
        if j == m.start():
            continue
        out.append(s[pos:j])
        out.append(repl(_Span(s, j, m.end())))
        pos = m.end()
    out.append(s[pos:])
    return "".join(out)


class _Span:
    """Just enough of a match object for the sanitizer's replacement callbacks."""

    def __init__(self, s, start, end):
        self._t = s[start:end]

    def group(self, _=0):
        return self._t


def trim_cut(s: str) -> str:
    """Drop a trailing email or home path that a truncation cut in half: the half no longer matches
    the placeholder, so the verification scan would flag it. Applied to cut strings only."""
    # an email can only be cut at the last '@' (a linear scan: a regex is quadratic on long runs)
    body, nl = (s[:-1], "\n") if s.endswith("\n") else (s, "")
    i = body.rfind("@")
    if i >= 0 and TAIL_DOMAIN_RE.fullmatch(body, i + 1):
        j = i
        while j > 0 and TAIL_LOCAL_RE.fullmatch(body[j - 1]):
            j -= 1
        s = body[:j] + nl
    m = TAIL_PATH_RE.search(s)
    if m and m.group(0) != "/home/user":
        s = s[:m.start()]
    return s


def cut_bytes(s: str, n: int) -> str:
    b = s.encode()
    return s if len(b) <= n else trim_cut(b[:n].decode("utf-8", errors="ignore"))


def cap_stream(raw: bytes, san: Sanitizer) -> bytes:
    lines = jsonl_lines(decode(raw))
    if len(lines) > STREAM_LINES:
        lines = lines[:1] + lines[-(STREAM_LINES - 1):]
    out = []
    for ln in lines:
        ln = san_jsonl_line(ln if ln.endswith("\n") else ln + "\n", san)
        if len(ln.encode()) > STREAM_LINE_BYTES:
            try:
                obj = json.loads(ln)
            except ValueError:
                obj = None
            typ = obj.get("type") if isinstance(obj, dict) else None
            text = "\n".join(strings(obj)) if obj is not None else ln
            ln = dumps({"type": typ, "truncated": True,
                        "text": trim_cut(text[:STREAM_TEXT_CHARS])}) + "\n"
        out.append(ln)
    return "".join(out).encode()


def cap_log(raw: bytes, san: Sanitizer) -> bytes:
    lines = san.text(decode(raw)).splitlines(keepends=True)[-LOG_LINES:]
    return "".join(cut_bytes(ln.rstrip("\n"), LOG_LINE_BYTES) + ("\n" if ln.endswith("\n") else "")
                   for ln in lines).encode()


def cap_big_json(raw: bytes) -> bytes:
    """Cut the free-text values of an (already sanitized) JSON document to JSON_VALUE_CHARS."""
    try:
        obj = json.loads(raw)
    except ValueError:
        return raw

    def cut(v):
        if isinstance(v, str):
            return v if len(v) <= JSON_VALUE_CHARS else trim_cut(v[:JSON_VALUE_CHARS])
        if isinstance(v, list):
            return [cut(x) for x in v]
        if isinstance(v, dict):
            return {k: cut(x) for k, x in v.items()}
        return v
    obj = cut(obj)
    if isinstance(obj, dict):
        obj["truncated_by_fixture"] = True
    return (dumps(obj, 2) + "\n").encode()


# ---- reading the source --------------------------------------------------------------------

def read_jsonl(path: Path) -> list[tuple[str, object]]:
    """(raw line with newline, parsed object or None). Missing file: []."""
    try:
        text = decode(path.read_bytes())
    except OSError:
        return []
    out = []
    for ln in jsonl_lines(text):
        try:
            out.append((ln, json.loads(ln)))
        except ValueError:
            out.append((ln, None))
    return out


def read_json(path: Path):
    try:
        return json.loads(path.read_bytes())
    except (OSError, ValueError):
        return None


def parse_ts(s):
    try:
        t = dt.datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


class Camp:
    """A campaign dir in the source (or in a fixture): state, parsed ledger, run dir listing."""

    def __init__(self, path: Path):
        self.path = path
        self.cid = path.name
        self.state = read_json(path / "state.json") or {}
        self.ledger = read_jsonl(path / "ledger.jsonl")
        self.events = [o for _, o in self.ledger if isinstance(o, dict)]
        runs = path / "runs"
        self.runs = sorted(d.name for d in runs.iterdir() if d.is_dir()) if runs.is_dir() else []
        self._size = None

    def run_has(self, run, name):
        return (self.path / "runs" / run / name).is_file()

    def of(self, typ):
        return [e for e in self.events if e.get("type") == typ]

    def interval(self):
        ts = [t for t in (parse_ts(e.get("ts")) for e in self.events) if t]
        return (ts[0], ts[-1]) if ts else None


def predicates(c: Camp, esc: dict) -> dict:
    """Scenario key -> bool for one campaign (also used by the tests on the committed data)."""
    st = c.state.get("state")
    into = Counter(e.get("to") for e in c.of("transition") if e.get("to") not in TERMINAL)
    run_ends = c.of("run.end")
    answered = False
    for e in c.of("human_decision"):
        x = esc.get(e.get("escalation"))
        if isinstance(x, dict) and x.get("status") == "answered":
            answered = True
    waive = False
    for r in c.runs:
        v = (read_json(c.path / "runs" / r / "run.json") or {}).get("verdict")
        if isinstance(v, dict) and v.get("waive"):
            waive = True
    some_json = any(c.run_has(r, "run.json") or c.run_has(r, "result.json") for r in c.runs)
    return {
        "merged_multi_loop": st == "MERGED" and any(n >= 2 for n in into.values())
        and len(c.of("gate")) >= 2 and len(c.of("finding")) >= 2
        and any((e.get("cost_usd") or 0) > 0 for e in run_ends),
        "abandoned": st == "ABANDONED" and bool(run_ends),
        "handed_off": st == "HANDED_OFF",
        "human_escalation": answered,
        "pre_stream": some_json and not any(c.run_has(r, "stream.jsonl") for r in c.runs),
        "flaky_and_triage": bool(c.of("flaky_recorded")) and waive,
        "legacy_schema": str(c.state.get("schema", "")).startswith("wt."),
        "no_run_json": any(c.run_has(r, "result.json") and not c.run_has(r, "run.json")
                           for r in c.runs),
    }


# ---- building campaign files ---------------------------------------------------------------

def campaign_files(c: Camp, san: Sanitizer) -> dict[str, bytes]:
    """relative path (inside the campaign dir) -> capped and sanitized bytes."""
    out: dict[str, bytes] = {}

    def put(rel, raw, how="file"):
        if how == "stream":
            out[rel] = cap_stream(raw, san)
        elif how == "log":
            out[rel] = cap_log(raw, san)
        else:
            # sanitize first, then cut: a cut must not split a secret below its pattern's length
            data = sanitize_file(rel, raw, san)
            big = rel.endswith(("run.json", "result.json")) and len(raw) > JSON_CAP_BYTES
            out[rel] = cap_big_json(data) if big else data

    for name in ("state.json", "ledger.jsonl", "spec_at_open.md"):
        if (c.path / name).is_file():
            put(name, (c.path / name).read_bytes())
    for r in c.runs:
        for name in RUN_FILES:
            p = c.path / "runs" / r / name
            if p.is_file():
                put(f"runs/{r}/{name}", p.read_bytes(),
                    "stream" if name == "stream.jsonl" else "log" if name.endswith(".log") else "file")
    gates = c.path / "gates"
    if gates.is_dir():
        for p in sorted(gates.iterdir()):
            if p.is_file():
                put(f"gates/{p.name}", p.read_bytes(), "log" if p.suffix == ".log" else "file")
    return out


def camp_size(c: Camp) -> int:
    if c._size is None:
        c._size = sum(len(v) for v in campaign_files(c, NullSanitizer()).values())
    return c._size


def _loads_or_none(line: str):
    try:
        return json.loads(line) if line.strip() else None
    except ValueError:
        return None


def derive_handed_off(files: dict[str, bytes], orig: str, new: str) -> dict[str, bytes]:
    """Copy of a merged campaign's files as a HANDED_OFF one with its own cid throughout."""
    out = {}
    for rel, raw in files.items():
        raw = raw.replace(orig.encode(), new.encode())
        if rel == "state.json":
            st = json.loads(raw)
            st["id"] = new
            st["state"] = "HANDED_OFF"
            close = dict(st.get("close") or {})
            close.update(mode="pr", merged_sha=None, pr_url=DERIVED_PR_URL)
            st["close"] = close
            raw = (dumps(st, 2) + "\n").encode()
        elif rel == "ledger.jsonl":
            lines = jsonl_lines(raw.decode())
            objs = [_loads_or_none(x) for x in lines]
            for typ, edit in (("transition", lambda e: e.update(to="HANDED_OFF")
                               if e.get("to") == "MERGED" else None),
                              ("close", lambda e: e.update(mode="pr", merged_sha=None,
                                                           pr_url=DERIVED_PR_URL))):
                idx = [i for i, o in enumerate(objs) if o and o.get("type") == typ
                       and (typ != "transition" or o.get("to") == "MERGED")]
                if idx:
                    edit(objs[idx[-1]])
                    lines[idx[-1]] = dumps(objs[idx[-1]]) + "\n"
            raw = "".join(lines).encode()
        out[rel] = raw
    return out


# ---- selection -----------------------------------------------------------------------------

class Missing(Exception):
    pass


def select(camps: list[Camp], esc: dict, pins: list[str], derive: bool):
    """-> (chosen: list[Camp] in cid order, picked: scenario -> [cid], absent: [scenario])."""
    preds = {c.cid: predicates(c, esc) for c in camps}
    chosen: dict[str, Camp] = {}
    picked: dict[str, list[str]] = {}
    for key, required in SCENARIOS:
        cands = [c for c in camps if preds[c.cid][key]]
        pinned = [c for c in cands if c.cid in pins]
        if pinned:
            pick = pinned
        elif cands:
            fresh = [c for c in cands if c.cid not in chosen] or cands
            pick = [min(fresh, key=lambda c: (camp_size(c), c.cid))]
        else:
            pick = []
        if not pick:
            if required:
                raise Missing(key)
            continue
        picked[key] = [c.cid for c in pick]
        for c in pick:
            chosen[c.cid] = c
    return sorted(chosen.values(), key=lambda c: c.cid), picked, preds


def derived_id(cid: str) -> str:
    return cid.rsplit("-", 1)[0] + "-hdof"


# ---- cross-campaign records ----------------------------------------------------------------

def mentions(line: str, cids) -> bool:
    return any(cid in line for cid in cids)


def _safe_name(v) -> bool:
    """A spec_id / project from a (possibly hostile) state.json that is safe as one path part."""
    return isinstance(v, str) and SAFE_NAME_RE.fullmatch(v) is not None and v not in (".", "..")


def _inside(path: Path, root: Path) -> bool:
    """`path` resolves (symlinks followed) inside `root` as given (callers pass a resolved parent plus a
    validated name, so a symlinked `root` itself fails); a symlink loop or unreadable link is outside."""
    try:
        # strict for anything that exists or is a link: Python 3.13+ does not raise on a loop otherwise
        return path.resolve(strict=os.path.lexists(path)).is_relative_to(root)
    except (OSError, RuntimeError):
        return False


def _names(values, kind: str, note) -> set:
    """The `str` values only: anything else (a list or dict would be unhashable) is noted, never hashed."""
    out = set()
    for v in values:
        if isinstance(v, str):
            out.add(v)
        elif v is not None:
            note(f"unsafe {kind} {v!r}: skipped")
    return out


def collect_records(agentic: Path, chosen: list[Camp], esc: dict, note) -> tuple[dict, dict, dict]:
    """-> (files: rel path under agentic/ -> raw bytes, sources, record counts)."""
    cids = {c.cid for c in chosen}
    specs = _names((c.state.get("spec_id") for c in chosen), "spec_id", note)
    projects = _names((c.state.get("project") for c in chosen), "project", note)
    files: dict[str, bytes] = {}
    sources: dict[str, str] = {}
    counts = {"decisions": 0, "escalations": 0, "mail": 0, "events": 0}

    def src(name, path):
        sources[name] = "present" if path.exists() else "absent_in_source"
        return path.exists()

    p = agentic / "conductor" / "decisions.jsonl"
    if src("decisions", p):
        keep = [ln for ln, o in read_jsonl(p) if isinstance(o, dict) and (
            o.get("campaign") in cids or (not o.get("campaign") and mentions(ln, cids)))]
        counts["decisions"] = len(keep)
        files["conductor/decisions.jsonl"] = "".join(keep).encode()

    cited = set()
    for c in chosen:
        for ln, _ in c.ledger:
            cited.update(re.findall(r"ESC-\d{4}", ln))
    if src("escalations", agentic / "escalations"):
        for eid, e in sorted(esc.items()):
            if e.get("campaign") in cids or (eid in cited and not e.get("campaign")):
                files[f"escalations/{eid}.json"] = (agentic / "escalations" / f"{eid}.json").read_bytes()
                counts["escalations"] += 1
            elif eid in cited:
                note(f"{eid} is cited by a chosen ledger but belongs to {e.get('campaign')}: skipped")

    mb = agentic / "mailbox"
    if src("mailbox", mb):
        for p in sorted(mb.glob("*.jsonl")):
            keep = [ln for ln, o in read_jsonl(p) if isinstance(o, dict) and (
                o.get("campaign") in cids or (not o.get("campaign") and mentions(ln, cids)))]
            if keep:
                counts["mail"] += len(keep)
                files[f"mailbox/{p.name}"] = "".join(keep).encode()

    p = agentic / "events.jsonl"
    if src("events", p):
        keep = [ln for ln, o in read_jsonl(p) if isinstance(o, dict) and (
            o.get("campaign") in cids or (o.get("source") or {}).get("campaign") in cids)]
        counts["events"] = len(keep)
        files["events.jsonl"] = "".join(keep).encode()

    p = agentic / "runner.jsonl"
    if src("runner", p):
        rows = [(ln, parse_ts((o or {}).get("ts")) if isinstance(o, dict) else None)
                for ln, o in read_jsonl(p)]
        keep: set[int] = set()
        for c in chosen:
            iv = c.interval()
            if not iv:
                continue
            idx = [i for i, (_, t) in enumerate(rows) if t and iv[0] <= t <= iv[1]]
            keep.update(idx[-RUNNER_LINES_PER_INTERVAL:])
        files["runner.jsonl"] = "".join(rows[i][0] for i in sorted(keep)).encode()

    p = agentic / "spec-reviews.jsonl"
    if src("spec-reviews", p):
        keep = [ln for ln, o in read_jsonl(p) if isinstance(o, dict) and isinstance(o.get("spec_id"), str) and o["spec_id"] in specs]
        files["spec-reviews.jsonl"] = "".join(keep).encode()

    rv = agentic / "reviews"
    if src("reviews", rv):
        for s in sorted(specs):
            if not _safe_name(s):
                note(f"unsafe spec_id {s!r}: skipped")
                continue
            d = rv / s
            if d.is_dir():
                root = rv.resolve() / s     # a symlinked <spec_id> dir resolves elsewhere: every file fails
                for f in sorted(d.rglob("*")):
                    if not _inside(f, root):
                        note(f"reviews/{s}/{f.relative_to(d).as_posix()} resolves outside its own directory: skipped")
                    elif f.is_file():
                        files[f"reviews/{s}/{f.relative_to(d).as_posix()}"] = f.read_bytes()

    p = agentic / "flaky-tests.json"
    if src("flaky-tests", p):
        data = read_json(p)
        if isinstance(data, dict):
            projs = {}
            for proj, tests in (data.get("projects") or {}).items():
                kept = {}
                for tid, ent in (tests or {}).items():
                    cs = [x for x in (ent.get("campaigns") or []) if x in cids]
                    if cs:
                        kept[tid] = dict(ent, campaigns=cs)
                if kept:
                    projs[proj] = kept
            data["projects"] = projs
            files["flaky-tests.json"] = (dumps(data, 2) + "\n").encode()

    sd = agentic / "sentinel"
    if src("sentinel", sd):
        for proj in sorted(projects):
            if not _safe_name(proj):
                note(f"unsafe project {proj!r}: skipped")
                continue
            f = sd / f"{proj}.last.json"
            try:    # equality, not a root check: `sentinel/` also holds every other project's file
                own = f.resolve() == sd.resolve() / f"{proj}.last.json"
            except (OSError, RuntimeError):
                own = False
            if not own:
                note(f"sentinel/{proj}.last.json does not resolve to itself: skipped")
            elif f.is_file():
                files[f"sentinel/{proj}.last.json"] = f.read_bytes()
    return files, sources, counts


# ---- verification scan ---------------------------------------------------------------------

def _walk_hits(v, key=None, secret=False):
    secret = secret or bool(key and SECRET_KEY_RE.search(key))
    if isinstance(v, dict):
        for k, x in v.items():
            if k == "env" and isinstance(x, dict) and x:
                yield "env"
            yield from _walk_hits(x, k, secret)
    elif isinstance(v, list):
        for x in v:
            yield from _walk_hits(x, None, secret)
    elif isinstance(v, str) and secret and v and v != "[REDACTED]":
        yield "secret_key"


def _text_rules(part: str, deny, seg_res) -> set:
    rules = set()
    if any(m.group(1) != "user" for m in PATH_RE.finditer(part)):
        rules.add("home_path")
    if any(rx.search(part) for rx in seg_res):
        rules.add("user_segment")
    if any(m.group(0) != PLACEHOLDER_EMAIL for m in EMAIL_RE.finditer(part)):
        rules.add("email")
    if any(rx.search(part) for rx in (PEM_BEGIN_RE, *SECRET_RES)):
        rules.add("secret")
    if any(d and d in part for d in deny):
        rules.add("deny")
    return rules


def scan(root: Path, deny: list[str], users=()) -> list[tuple[str, int, str]]:
    """Re-read every file under `root` and report (relative path, line, rule) hits: a home path
    of anyone but `user`, a name in `users` as a path segment, an email other than the
    placeholder, a secret pattern or key, a non-empty `env` object, any literal in `deny`.
    JSON is scanned by decoded string value (line 0 for a whole .json file).
    `users` is empty by default and is filled only by a capture run (from the environment and
    --src), so the scan of a committed tree gives the same answer on every machine."""
    users = {u for u in users if u and u != "user"}
    seg_res = [re.compile(r"/" + re.escape(u) + r"(?![\w.-])") for u in users]
    hits = []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()
        text = decode(p.read_bytes())
        if rel.endswith(".json"):
            try:
                obj = json.loads(text)
            except ValueError:
                obj = None
            if obj is not None:
                rules = set(_walk_hits(obj))
                for part in strings(obj):
                    rules |= _text_rules(part, deny, seg_res)
                hits.extend((rel, 0, r) for r in sorted(rules))
                continue
        for n, ln in enumerate(jsonl_lines(text), 1):
            ln = ln.rstrip("\n")
            rules, parts = set(), [ln]
            if rel.endswith(".jsonl"):
                try:
                    obj = json.loads(ln)
                    parts = list(strings(obj))
                    rules |= set(_walk_hits(obj))
                except ValueError:
                    pass
            for part in parts:
                rules |= _text_rules(part, deny, seg_res)
            hits.extend((rel, n, r) for r in sorted(rules))
    return hits


def load_deny() -> list[str]:
    p = Path(os.environ.get("NW_FIXTURE_DENY_FILE") or DEFAULT_DENY)
    try:
        return [x.strip() for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
    except OSError:
        return []


# ---- manifest / check ----------------------------------------------------------------------

def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def tree_files(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file() and p.relative_to(root).as_posix() != "MANIFEST.json"}


def check(out: Path) -> int:
    mf = out / "MANIFEST.json"
    if not mf.is_file():
        print(f"check: {mf} missing", file=sys.stderr)
        return 1
    try:
        manifest = json.loads(mf.read_text(encoding="utf-8"))
    except (ValueError, OSError) as e:
        print(f"check: {mf} is not valid JSON: {e}", file=sys.stderr)
        return 1
    listed = manifest.get("files") if isinstance(manifest, dict) else None
    if (not isinstance(listed, dict) or manifest.get("schema") != SCHEMA
            or not all(isinstance(e, dict) and "sha256" in e and "bytes" in e for e in listed.values())):
        print(f"check: {mf} is malformed (want schema {SCHEMA} and files{{path: {{sha256, bytes}}}})",
              file=sys.stderr)
        return 1
    bad = []
    actual = tree_files(out)
    for rel, raw in actual.items():
        ent = listed.get(rel)
        if ent is None:
            bad.append(f"unlisted file: {rel}")
        elif ent["sha256"] != sha(raw) or ent["bytes"] != len(raw):
            bad.append(f"modified file: {rel}")
    bad += [f"missing file: {rel}" for rel in listed if rel not in actual]
    for rel, line, rule in scan(out, load_deny()):
        bad.append(f"sanitization: {rel}:{line} {rule}")
    for b in bad:
        print(f"check: {b}", file=sys.stderr)
    if not bad:
        print(f"check: ok ({len(actual)} files)")
    return 1 if bad else 0


# ---- main ----------------------------------------------------------------------------------

def fail(code, msg):
    print(f"capture_history_fixture: {msg}", file=sys.stderr)
    return code


def worker_hosts(agentic: Path) -> list[str]:
    data = read_json(agentic / "workers.json")
    hosts = []
    for w in (data or {}).get("workers") or []:
        for k in ("ssh", "host"):
            v = w.get(k)
            if isinstance(v, str) and v:
                hosts.append(v.rsplit("@", 1)[-1].split(":")[0])
    return hosts


def out_refusal(out: Path):
    """Why `out` may not be replaced, or None. A capture deletes the old `out`, so it only
    replaces an absent or empty dir or a dir that is itself a fixture (its MANIFEST.json has our
    schema); anything else is somebody's data."""
    if not out.exists():
        return None
    if not out.is_dir():
        return f"--out {out} exists and is not a directory"
    if not any(out.iterdir()):
        return None
    try:
        schema = json.loads((out / "MANIFEST.json").read_text(encoding="utf-8")).get("schema")
    except (ValueError, OSError, AttributeError):
        schema = None
    if schema != SCHEMA:
        return (f"--out {out} is not empty and has no {SCHEMA} MANIFEST.json: "
                "refusing to delete it")
    return None


def capture(args) -> int:
    src = Path(args.src).expanduser().resolve()
    out = Path(args.out).resolve()
    agentic = src / "agentic"
    if not (agentic / "campaigns").is_dir():
        return fail(2, f"{src} has no agentic/campaigns/")
    if out == src or src in out.parents:
        return fail(2, "--out must not be inside --src")
    if out in src.parents:
        return fail(2, "--src must not be inside --out (it would be deleted on write)")
    refusal = out_refusal(out)
    if refusal:
        return fail(2, refusal)
    max_bytes = int(os.environ.get("NW_FIXTURE_MAX_BYTES") or MAX_BYTES)
    notes = []
    esc = {}
    for p in sorted((agentic / "escalations").glob("ESC-*.json")):
        e = read_json(p)
        if isinstance(e, dict):
            esc[p.stem] = e
    camps = []
    for d in sorted((agentic / "campaigns").iterdir()):
        if d.is_dir() and (d / "state.json").is_file():
            c = Camp(d)
            if c.state.get("state") in CANDIDATE_STATES:
                camps.append(c)
    try:
        chosen, picked, preds = select(camps, esc, args.cid or [], not args.no_derive_handed_off)
    except Missing as m:
        return fail(2, f"required scenario has no candidate campaign: {m}")

    derive = (not args.no_derive_handed_off and "handed_off" not in picked
              and "merged_multi_loop" in picked)
    base = next((c for c in chosen if c.cid == picked["merged_multi_loop"][0]), None) if derive else None
    if len(chosen) + (1 if derive else 0) > MAX_CAMPAIGNS:
        return fail(2, f"more than {MAX_CAMPAIGNS} campaigns selected")

    users = {os.environ.get("USER", ""), Path.home().name}
    m = re.match(r"(?:/home|/Users)/([^/]+)", str(src))
    if m:
        users.add(m.group(1))
    san = Sanitizer(worker_hosts(agentic), users)

    tree: dict[str, bytes] = {}         # rel path under agentic/ -> final bytes
    entries = []
    max_ts = None

    def add_campaign(cid, files, c, origin, derived_from, scen):
        for rel, raw in files.items():
            tree[f"campaigns/{cid}/{rel}"] = raw
        st = json.loads(files["state.json"])
        entries.append({"cid": cid, "project": st.get("project"), "state": st.get("state"),
                        "origin": origin, "derived_from": derived_from,
                        "schema": st.get("schema"), "scenarios": scen,
                        "runs": len(c.runs), "ledger_lines": len(c.ledger),
                        "has_stream": any(rel.endswith("stream.jsonl") for rel in files)})

    scen_of = {c.cid: [k for k, _ in SCENARIOS if preds[c.cid][k]] for c in chosen}
    for c in chosen:
        add_campaign(c.cid, campaign_files(c, san), c, "captured", None, scen_of[c.cid])
        for e in c.events:
            t = parse_ts(e.get("ts"))
            if t and (max_ts is None or t > max_ts[0]):
                max_ts = (t, e["ts"])
    if derive:
        did = derived_id(base.cid)
        files = derive_handed_off(campaign_files(base, san), base.cid, did)
        add_campaign(did, files, base, "derived", base.cid, ["handed_off"])
        picked["handed_off"] = [did]
        ledger = files["ledger.jsonl"].decode().count("\n")
        entries[-1]["ledger_lines"] = ledger

    rec, sources, counts = collect_records(agentic, chosen, esc, lambda x: notes.append(x))
    for rel in sorted(rec):
        tree[rel] = sanitize_file(rel, rec[rel], san)
    tree = {f"agentic/{k}": v for k, v in sorted(tree.items())}

    total = sum(len(v) for v in tree.values())
    if total > max_bytes:
        big = max(entries, key=lambda e: sum(len(v) for k, v in tree.items()
                                             if k.startswith(f"agentic/campaigns/{e['cid']}/")))
        return fail(3, f"output is {total} bytes, over the budget of {max_bytes}; "
                       f"largest campaign: {big['cid']}")

    scenarios = {key: ([e["cid"] for e in entries if key in e["scenarios"]] or "absent_in_source")
                 for key, _ in SCENARIOS}
    manifest = {
        "schema": SCHEMA, "tool": "tools/capture_history_fixture.py", "tool_version": TOOL_VERSION,
        "source_label": "~/.local/share/nightwright",
        "source_max_ts": max_ts[1] if max_ts else None,
        "campaigns": entries, "scenarios": scenarios, "sources": dict(sorted(sources.items())),
        "sanitization": {"rules": dict(sorted(san.counts.items()))},
        "totals": {"campaigns": len(entries), "files": len(tree), "bytes": total, "records": counts},
        "files": {k: {"bytes": len(v), "sha256": sha(v)} for k, v in tree.items()},
    }

    tmp = out.parent / f".{out.name}.tmp-{os.getpid()}"
    shutil.rmtree(tmp, ignore_errors=True)
    try:
        for rel, raw in tree.items():
            p = tmp / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(raw)
        (tmp / "MANIFEST.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                                           encoding="utf-8")
        hits = scan(tmp, load_deny(), users)
        if hits:
            for rel, line, rule in hits[:20]:
                print(f"scan: {rel}:{line} {rule}", file=sys.stderr)
            return fail(4, f"verification scan found {len(hits)} hit(s); nothing written")
        out.parent.mkdir(parents=True, exist_ok=True)
        old = None
        if out.exists():
            old = out.parent / f".{out.name}.old-{os.getpid()}"
            shutil.rmtree(old, ignore_errors=True)
            out.rename(old)
        try:
            tmp.rename(out)
        except OSError:
            if old is not None:
                old.rename(out)
            raise
        if old is not None:
            shutil.rmtree(old, ignore_errors=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"captured {len(entries)} campaigns, {len(tree)} files, {total} bytes -> {out}")
    for key, v in scenarios.items():
        print(f"  {key}: {v if isinstance(v, str) else ', '.join(v)}")
    print("  sanitization hits: " + ", ".join(f"{k}={v}" for k, v in sorted(san.counts.items())))
    for n in notes:
        print(f"  note: {n}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Capture a sanitized subset of closed campaigns into the history fixture "
                    "(SPEC-0415). Exit 0 ok, 1 --check failed, 2 usage/missing scenario, "
                    "3 over budget, 4 verification scan hit.",
        epilog="Env: NW_DATA_DIR (default for --src); NW_FIXTURE_MAX_BYTES overrides the 3 MiB "
               "output budget; NW_FIXTURE_DENY_FILE overrides tools/capture_history_deny.txt "
               "(one literal per line that must not appear in the output).")
    ap.add_argument("--src", default=os.environ.get("NW_DATA_DIR") or "~/.local/share/nightwright",
                    help="nightwright data dir (contains agentic/); opened read-only")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="fixture dir to write (default tests/fixtures/agentic_history)")
    ap.add_argument("--cid", action="append", default=[], help="pin a scenario's campaign by id (repeatable)")
    ap.add_argument("--no-derive-handed-off", action="store_true",
                    help="do not derive a HANDED_OFF campaign; with no real one the scenario is absent_in_source")
    ap.add_argument("--check", action="store_true",
                    help="write nothing; verify --out against its MANIFEST.json and the sanitization scan")
    args = ap.parse_args(argv)
    if args.check:
        return check(Path(args.out))
    return capture(args)


if __name__ == "__main__":
    sys.exit(main())
