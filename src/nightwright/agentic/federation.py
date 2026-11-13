"""Federation signed channel (SPEC-0529): HMAC helpers and `HubClient`. No relay or queue."""
from __future__ import annotations

import concurrent.futures as cf
import datetime as dt
import hashlib
import hmac
import http.client
import json
import logging
import os
import re
import secrets
import socket
import ssl
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from . import decisions as D
from . import events as E
from . import hosts as H
from . import mailbox as MB
from . import pull_jobs as PJ
from . import store
from . import workers as WK

log = logging.getLogger(__name__)

FEDERATION_REF = "secrets/federation.json"
EMPTY_BODY_SHA = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
SKEW_S = 120
MAX_RESPONSE_BYTES = PJ.MAX_FRAMES_BYTES
SIG_V_HEADER = "X-NW-Sig-V"
SIG_VERSION = "2"
UNAUTHORIZED_BODY = b'{"error":"unauthorized"}'
POOL_WAIT_S = 2.5                 # SPEC-0530: the relay tick's bound on every host's calls
UNREACHABLE_S = 600
STALE_S = 600
EVENTS_LIMIT = 500
EVENTS_MAX_BYTES = 1048576
MAIL_LIMIT = 200                  # SPEC-0532
MAIL_MAX_BYTES = 1048576
MAIL_POST_BYTES = 2097152         # one POST batch stops here (the host refuses bodies over 4 MiB)
MAX_CAMPAIGNS = 1000
MAX_STR = 256


def federation_path(cfg) -> Path:
    return Path(cfg["config_dir"]) / FEDERATION_REF


def host_secret_ref(name: str) -> str:
    return f"secrets/hosts/{name}.key"


def host_token_path(cfg, name: str) -> Path:
    return Path(cfg["config_dir"]) / "secrets" / "hosts" / f"{name}.token"


def host_secret_path(cfg, name: str) -> Path:
    return Path(cfg["config_dir"]) / host_secret_ref(name)


def new_host_credential() -> tuple[str, str]:
    return secrets.token_urlsafe(32), secrets.token_hex(32)


def _mkdir_private(d: Path) -> None:
    """`mkdir -p` where every directory this call creates is 0700 (`mkdir(mode=)` only covers the
    leaf, and the umask can widen it), so a fresh `secrets/` is not left at the umask default."""
    missing = []
    while not d.exists():
        missing.append(d)
        d = d.parent
    for m in reversed(missing):
        try:
            m.mkdir(mode=0o700)
        except FileExistsError:
            continue
        os.chmod(m, 0o700)


def _write_private_fsync(path: Path, text: str) -> None:
    """`PJ.write_private` plus an fsync of the temp file before the rename, so a crash cannot
    leave the registry naming a host whose secret file is empty."""
    _mkdir_private(path.parent)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, text.encode("utf-8"))
        os.fsync(fd)
    except OSError:
        os.close(fd)
        tmp.unlink(missing_ok=True)
        raise
    os.close(fd)
    os.replace(tmp, path)


def write_host_secrets(cfg, name: str, token: str, secret: str) -> None:
    tok_p = host_token_path(cfg, name)
    _write_private_fsync(tok_p, token + "\n")
    try:
        _write_private_fsync(host_secret_path(cfg, name), secret + "\n")
    except OSError:
        tok_p.unlink(missing_ok=True)
        raise


def drop_host_secrets(cfg, name: str) -> None:
    host_token_path(cfg, name).unlink(missing_ok=True)
    host_secret_path(cfg, name).unlink(missing_ok=True)


def _strip_one_trailing_newline(s: str) -> str:
    return s[:-1] if s.endswith("\n") else s


def read_host_token(cfg, name: str) -> str | None:
    try:
        s = _strip_one_trailing_newline(host_token_path(cfg, name).read_text(encoding="utf-8"))
    except OSError:
        return None
    return s or None


def read_host_secret(cfg, name: str) -> str | None:
    try:
        s = _strip_one_trailing_newline(host_secret_path(cfg, name).read_text(encoding="utf-8"))
    except OSError:
        return None
    return s or None


def read_federation(cfg) -> dict | None:
    p = federation_path(cfg)
    if not p.is_file():
        return None
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict):
        return None
    for k in ("hub_name", "host_name", "token", "secret"):
        if not isinstance(doc.get(k), str) or not doc[k]:
            return None
    return doc


def write_federation(cfg, doc: dict) -> None:
    keys = ("hub_name", "host_name", "token", "secret")
    out = {k: doc[k] for k in keys if k in doc}
    if set(out) != set(keys):
        raise ValueError("federation document needs hub_name, host_name, token, and secret")
    _write_private_fsync(federation_path(cfg), json.dumps(out, sort_keys=True) + "\n")


def _body_sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest() if body else EMPTY_BODY_SHA


def _mac_line(method: str, path: str, body: bytes, ts: int, *, direction: str,
              status: int | None) -> str:
    """The signed ASCII line (SPEC-0595): a direction tag, then the fields joined by `|`."""
    if direction == "req":
        if status is not None:
            raise ValueError("status is only for direction='resp'")
        return f"nw-req|{method.upper()}|{path}|{_body_sha(body)}|{ts}"
    if direction == "resp":
        if (not isinstance(status, int) or isinstance(status, bool)
                or not 100 <= status <= 599):
            raise ValueError("direction='resp' needs a status from 100 to 599")
        return f"nw-resp|{status}|{method.upper()}|{path}|{_body_sha(body)}|{ts}"
    raise ValueError("direction must be 'req' or 'resp'")


def sign(secret: str, method: str, path: str, body: bytes, ts: int, *, direction: str,
         status: int | None = None) -> str:
    """Lowercase hex HMAC-SHA256 over `nw-req|METHOD|PATH|BODYSHA|TS` (a request) or
    `nw-resp|STATUS|METHOD|PATH|BODYSHA|TS` (a response). `direction` is required; `status` goes
    with `"resp"` only, so neither MAC can verify in the other's place."""
    line = _mac_line(method, path, body, ts, direction=direction, status=status)
    return hmac.new(secret.encode("utf-8"), line.encode(), hashlib.sha256).hexdigest()


def verify(secret: str, method: str, path: str, body: bytes, mac: str, ts: int,
           *, direction: str, status: int | None = None, now: float | None = None) -> str | None:
    """Why the MAC is invalid, else None."""
    now = time.time() if now is None else now
    if abs(int(now) - int(ts)) > SKEW_S:
        return "stale signature"
    want = sign(secret, method, path, body, ts, direction=direction, status=status)
    try:
        good = isinstance(mac, str) and hmac.compare_digest(mac, want)
    except (TypeError, ValueError):
        good = False
    if not good:
        return "bad signature"
    return None


def request_headers(secret: str, method: str, path: str, body: bytes, *,
                    now: float | None = None) -> dict[str, str]:
    ts = int(now if now is not None else time.time())
    return {"X-NW-Req-Ts": str(ts),
            "X-NW-Req-Sig": sign(secret, method, path, body, ts, direction="req"),
            SIG_V_HEADER: SIG_VERSION}


def response_headers(secret: str, method: str, path: str, body: bytes, *, status: int,
                     now: float | None = None) -> dict[str, str]:
    ts = int(now if now is not None else time.time())
    return {"X-NW-Sig-Ts": str(ts),
            "X-NW-Sig": sign(secret, method, path, body, ts, direction="resp", status=status),
            SIG_V_HEADER: SIG_VERSION}


class HubClient:
    """HTTP client from hub to a federation host."""

    CONNECT_TIMEOUT = 2
    READ_TIMEOUT = 2

    def __init__(self, record: dict, cfg, *, now: float | None = None):
        self.record, self.cfg, self._now = record, cfg, now
        url = record.get("url")
        if not isinstance(url, str):
            raise ValueError("host record has no url")
        u = urlsplit(url)
        if u.scheme not in ("http", "https") or not u.hostname:
            raise ValueError(f"url {url!r} is not http(s)://HOST")
        self.https = u.scheme == "https"
        self.host = u.hostname
        self.port = u.port if u.port is not None else (443 if self.https else 80)
        self.base = u.path.rstrip("/")
        name = record.get("name")
        if not isinstance(name, str) or not WK.NAME_RE.fullmatch(name):
            raise ValueError(f"host name must match {WK.NAME_RE.pattern}")
        token = read_host_token(cfg, name)
        secret = read_host_secret(cfg, name)
        if not token or not secret:
            raise ValueError("missing host credentials")
        self.token, self.secret = token, secret

    def _target(self, path: str) -> str:
        return self.base + path if self.base else path

    def _header(self, hdrs: dict[str, str], name: str) -> str | None:
        low = name.lower()
        for k, v in hdrs.items():
            if k.lower() == low:
                return v
        return None

    def _resolve_addrs(self, deadline: float) -> list:
        holder: dict = {}
        exc_holder: dict = {}

        def worker() -> None:
            try:
                holder["addrs"] = socket.getaddrinfo(
                    self.host, self.port, type=socket.SOCK_STREAM)
            except OSError as e:
                exc_holder["e"] = e

        th = threading.Thread(target=worker, daemon=True)
        th.start()
        wait = deadline - time.monotonic()
        th.join(wait if wait > 0 else 0)
        if th.is_alive():
            raise ConnectionError("TimeoutError: timed out")
        if "e" in exc_holder:
            e = exc_holder["e"]
            raise ConnectionError(f"{type(e).__name__}: {e}") from e
        addrs = holder.get("addrs")
        if not addrs:
            raise ConnectionError("gaierror: no addresses")
        return addrs

    def _try_connect_one(self, family, stype, proto, sockaddr, deadline: float,
                         out: list, lock: threading.Lock, state: dict) -> None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        sock = None
        try:
            sock = socket.socket(family, stype, proto)
            sock.settimeout(remaining)
            sock.connect(sockaddr)
            with lock:
                if not state["done"]:
                    out.append(("ok", sock))
                    return
            sock.close()  # the race was already decided: a late winner is closed, not leaked
        except OSError as e:
            if sock is not None:
                sock.close()
            with lock:
                out.append(("err", e))

    def _connect(self) -> socket.socket:
        deadline = time.monotonic() + self.CONNECT_TIMEOUT
        addrs = self._resolve_addrs(deadline)
        if len(addrs) == 1:
            family, stype, proto, _canon, sockaddr = addrs[0]
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ConnectionError("TimeoutError: timed out")
            sock = socket.socket(family, stype, proto)
            try:
                sock.settimeout(remaining)
                sock.connect(sockaddr)
                return sock
            except OSError as e:
                sock.close()
                raise ConnectionError(f"{type(e).__name__}: {e}") from e
        results: list = []
        lock = threading.Lock()
        state = {"done": False}
        threads: list[threading.Thread] = []
        for family, stype, proto, _canon, sockaddr in addrs:
            t = threading.Thread(
                target=self._try_connect_one,
                args=(family, stype, proto, sockaddr, deadline, results, lock, state),
                daemon=True,
            )
            t.start()
            threads.append(t)
        winner: socket.socket | None = None
        last: OSError | None = None
        seen = 0
        try:
            while winner is None and time.monotonic() < deadline:
                with lock:
                    pending = results[seen:]
                    seen = len(results)
                for kind, val in pending:
                    if kind == "ok":
                        if winner is None:
                            winner = val
                        else:
                            val.close()
                    else:
                        last = val
                if winner is not None:
                    break
                if seen >= len(addrs) and all(not t.is_alive() for t in threads):
                    break
                time.sleep(min(0.05, max(0, deadline - time.monotonic())))
        finally:
            with lock:
                state["done"] = True
                # Close every success the loop did not take (found between the last poll and now).
                for kind, val in results[seen:]:
                    if kind == "ok" and val is not winner:
                        val.close()
        if winner is not None:
            return winner
        if last is not None:
            raise ConnectionError(f"{type(last).__name__}: {last}") from last
        raise ConnectionError("TimeoutError: timed out")

    def request(self, method: str, path: str, body: bytes = b"",
                allow: tuple | None = (200,)) -> tuple[int, bytes, dict]:
        """A verified response. A status outside `allow` is an error after the signature is checked;
        one inside it (SPEC-0530: a `409`) must still carry a valid signature. `allow=None` returns
        any verified status (SPEC-0532: the mail relay tells a 4xx from a 5xx)."""
        data = body or b""
        target = self._target(path)
        headers = {"Authorization": f"Bearer {self.token}",
                   **request_headers(self.secret, method, target, data, now=self._now)}
        if data:
            headers["Content-Length"] = str(len(data))
        conn = None
        sock = None
        live: list = []
        timed_out = threading.Event()

        def _expire() -> None:
            # One wall-clock budget for handshake, send, headers and body: a peer that drips
            # bytes inside each recv timeout is cut off by shutting the socket under it.
            timed_out.set()
            for s_ in list(live):
                try:
                    socket.socket.shutdown(s_, socket.SHUT_RDWR)
                except (OSError, ValueError):
                    pass

        watchdog = None
        raw = b""
        try:
            sock = self._connect()
            live.append(sock)
            watchdog = threading.Timer(self.READ_TIMEOUT, _expire)
            watchdog.daemon = True
            watchdog.start()
            if self.https:
                ctx = ssl.create_default_context()
                ssl_sock = ctx.wrap_socket(sock, server_hostname=self.host)
                sock = None
                live.append(ssl_sock)
                conn = http.client.HTTPSConnection(self.host, self.port)
                conn.sock = ssl_sock
            else:
                conn = http.client.HTTPConnection(self.host, self.port)
                conn.sock = sock
                sock = None
            conn.sock.settimeout(self.READ_TIMEOUT)
            conn.request(method, target, body=data, headers=headers)
            resp = conn.getresponse()
            while True:
                chunk = resp.read(min(65536, MAX_RESPONSE_BYTES + 1 - len(raw)))
                if not chunk:
                    break
                raw += chunk
                if len(raw) > MAX_RESPONSE_BYTES:
                    break
            hdrs = {k: v for k, v in resp.getheaders()}
            status = resp.status
        except (OSError, ssl.SSLError, ValueError, http.client.HTTPException) as e:
            if timed_out.is_set():
                raise ConnectionError("TimeoutError: timed out") from e
            raise ConnectionError(f"{type(e).__name__}: {e}") from e
        finally:
            if watchdog is not None:
                watchdog.cancel()
            if conn is not None:
                conn.close()
            if sock is not None:
                sock.close()
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ConnectionError("response too large")
        ts_raw = self._header(hdrs, "X-NW-Sig-Ts")
        mac = self._header(hdrs, "X-NW-Sig")
        if status == 401 and ts_raw is None and mac is None and raw == UNAUTHORIZED_BODY:
            raise ConnectionError("host not enrolled")
        if self._header(hdrs, SIG_V_HEADER) != SIG_VERSION:
            raise ConnectionError("host uses an old signature version")
        if (not ts_raw or not str(ts_raw).isascii() or not str(ts_raw).isdigit()
                or not mac):
            raise ConnectionError("missing response signature")
        why = verify(self.secret, method, target, raw, mac, int(ts_raw),
                     direction="resp", status=status, now=self._now)
        if why:
            raise ConnectionError(why)
        if allow is not None and status not in allow:
            raise ConnectionError(f"HTTP {status}")
        return status, raw, hdrs

    def get_json(self, path: str) -> dict:
        _, raw, _ = self.request("GET", path)
        try:
            doc = json.loads(raw)
        except (ValueError, RecursionError) as e:
            raise ConnectionError("response is not JSON") from e
        if not isinstance(doc, dict):
            raise ConnectionError("response is not a JSON object")
        return doc


# ---- the hub's event relay (SPEC-0530) -------------------------------------------------------
# Network calls run in a pool; appends, fsyncs and `hosts.update` run on the calling thread.

def clean_str(v):
    """A host-supplied string that is safe to cache and echo, else None."""
    if isinstance(v, str) and len(v) <= MAX_STR and v.isprintable():
        return v
    return None


def clean_campaigns(raw) -> list[dict]:
    """Only the five documented keys of dict items, as bounded printable strings or None."""
    out = []
    for item in raw[:MAX_CAMPAIGNS] if isinstance(raw, list) else []:
        if isinstance(item, dict):
            out.append({k: clean_str(item.get(k))
                        for k in ("campaign", "project", "state", "spec_id", "kind")})
    return out


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _fetch_events(rec: dict, cfg, offset: int):
    """`("page", doc)`, `("reset", None)` or `("fail", exc)`. Runs in the pool; touches no file."""
    try:
        client = HubClient(rec, cfg)
        status, raw, _ = client.request(
            "GET", f"/v1/events?offset={offset}&limit={EVENTS_LIMIT}", allow=(200, 409))
        doc = json.loads(raw)
        if not isinstance(doc, dict):
            raise ConnectionError("response is not a JSON object")
        if status == 409:
            if doc.get("error") != "bad_offset":
                raise ConnectionError("HTTP 409")
            return "reset", None
        nxt = doc.get("next_offset")
        if (doc.get("schema") != "nw.federation.events.v1" or not isinstance(doc.get("events"), list)
                or not _is_int(nxt) or nxt < offset):
            raise ConnectionError("bad events page")
        return "page", doc
    except Exception as e:                                  # noqa: BLE001 - reported as a failure
        return "fail", e


def _fetch_status(rec: dict, cfg):
    """`("ok", doc)` or `("fail", exc)`. Runs in the pool; touches no file."""
    try:
        doc = HubClient(rec, cfg).get_json("/v1/status")
        if not isinstance(doc.get("projects"), list) or not isinstance(doc.get("campaigns"), list):
            raise ConnectionError("bad status document")
        return "ok", doc
    except Exception as e:                                  # noqa: BLE001 - reported as a failure
        return "fail", e


def _events_size(cfg) -> int:
    try:
        return E.events_path(cfg).stat().st_size
    except OSError:
        return 0


def _fsync_events(cfg) -> None:
    fd = os.open(E.events_path(cfg), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _scan_tail(cfg, offset) -> list[dict]:
    """The event objects of the hub log from `offset` to the end; earlier bytes are not read."""
    out: list[dict] = []
    try:
        with open(E.events_path(cfg), "rb") as f:
            size = os.fstat(f.fileno()).st_size
            f.seek(offset if _is_int(offset) and 0 <= offset <= size else 0)
            for raw in f:
                try:
                    obj = json.loads(raw)
                except (ValueError, RecursionError):
                    continue
                if isinstance(obj, dict):
                    out.append(obj)
    except OSError:
        pass
    return out


def _tail_origin_max(tail: list[dict], name: str) -> int:
    best = 0
    for ev in tail:
        o = ev.get("origin")
        if isinstance(o, dict) and o.get("host") == name and _is_int(o.get("id")):
            best = max(best, o["id"])
    return best


def _data_host(ev: dict):
    d = ev.get("data")
    return d.get("host") if isinstance(d, dict) else None


def _own_health(ev: dict, name: str, type_: str | None = None) -> bool:
    """A health event this hub wrote for `name`: no `origin`, so a `host_*` record relayed from a
    host that is itself a hub (it has an `origin`) is never mistaken for one."""
    t = ev.get("type")
    return (ev.get("origin") is None and isinstance(t, str) and t.startswith("host_")
            and (type_ is None or t == type_) and _data_host(ev) == name)


def _mine(ev: dict, name: str) -> bool:
    """A record in the rescan tail that this host's relay wrote (it may not be durable yet)."""
    o = ev.get("origin")
    return (isinstance(o, dict) and o.get("host") == name) or _own_health(ev, name)


def _pins_offset(tail: list[dict], name: str) -> bool:
    """The tail holds a `host_unreachable`/`host_reachable` this hub wrote for `name`. Its checkpoint
    may not have landed (a landed one moves `hub_events_offset` past it), so a page checkpoint keeps
    the rescan offset where it is: the health append stays visible to its dedupe."""
    return any(_own_health(ev, name, t) for ev in tail for t in ("host_unreachable", "host_reachable"))


def _uncheckpointed(tail: list[dict], name: str, cursor: dict) -> bool:
    """The tail holds copied records of this host beyond `events_origin_id`: a page was appended but
    its checkpoint is lost, so a health checkpoint must not move the rescan offset past them."""
    return _tail_origin_max(tail, name) > (cursor.get("events_origin_id") or 0)


def _locked_append(cfg, name: str, offset, fn):
    """Under `events._locked`: rescan the tail from `offset`, let `fn(tail)` return
    `(records to append, extra)`, append them, fsync when anything of this host's may still be
    unsynced, and measure the new size. Returns `(size, extra)`."""
    with E._locked(E._lock_path(cfg)):
        tail = _scan_tail(cfg, offset)
        kws, extra = fn(tail)
        for kw in kws:
            E._append_locked(cfg, **kw)
        if kws or any(_mine(ev, name) for ev in tail):
            _fsync_events(cfg)
        return _events_size(cfg), extra


def _host_record(obj: dict, name: str, now) -> dict:
    typ, data = obj.get("type"), obj.get("data")
    ok = isinstance(typ, str) and isinstance(data, dict)
    if ok:
        try:
            E.validate(typ, data)
        except Exception:                                   # noqa: BLE001 - any invalid record
            ok = False
    if not ok:
        data = {**(data if isinstance(data, dict) else {}), "type": typ if isinstance(typ, str)
                else str(typ)}
        typ = "host_event"
    camp = obj.get("campaign")
    by = obj.get("by")
    return {"type_": typ, "data": data,
            "campaign": f"{camp}@{name}" if isinstance(camp, str) and camp else None,
            "spec_id": obj["spec_id"] if isinstance(obj.get("spec_id"), str) else None,
            "project": obj["project"] if isinstance(obj.get("project"), str) else None,
            "by": by if isinstance(by, str) and by else "host", "host": name,
            "origin": {"host": name, "id": obj["id"]},
            "ts": obj["ts"] if isinstance(obj.get("ts"), str) else store.now_iso(now)}


def _health_event(type_: str, name: str, now, **data) -> dict:
    return {"type_": type_, "data": {"host": name, **data}, "campaign": None, "spec_id": None,
            "project": None, "by": "federation", "host": name, "ts": store.now_iso(now)}


def _set_cursor(cfg, name: str, **fields) -> None:
    def fn(recs):
        r = H.find(recs, name)
        cur = r.get("cursor") if isinstance(r.get("cursor"), dict) else {}
        r["cursor"] = {**cur, **fields}
    H.update(cfg, fn)


def _apply_page(cfg, name: str, cursor: dict, doc: dict, now) -> None:
    def build(tail):
        eff = max(cursor.get("events_origin_id") or 0, _tail_origin_max(tail, name))
        top, seen, kws = eff, set(), []
        for obj in doc["events"]:
            if not isinstance(obj, dict) or not _is_int(obj.get("id")):
                continue
            top = max(top, obj["id"])
            if obj["id"] <= eff or obj["id"] in seen or obj.get("type") == "message":
                continue
            seen.add(obj["id"])
            kws.append(_host_record(obj, name, now))
        return kws, (top, _pins_offset(tail, name))

    size, (top, pin) = _locked_append(cfg, name, cursor.get("hub_events_offset"), build)
    _set_cursor(cfg, name, events_offset=doc["next_offset"], events_origin_id=top,
                hub_events_offset=cursor.get("hub_events_offset") if pin else size)


def _apply_reset(cfg, name: str, cursor: dict, now) -> None:
    def build(tail):
        eff = max(cursor.get("events_origin_id") or 0, _tail_origin_max(tail, name))
        have = any(_own_health(ev, name, "host_cursor_reset") for ev in tail)
        return ([] if have else [_health_event("host_cursor_reset", name, now)]), \
            (eff, _pins_offset(tail, name))

    size, (eff, pin) = _locked_append(cfg, name, cursor.get("hub_events_offset"), build)
    _set_cursor(cfg, name, events_offset=0, events_origin_id=eff,
                hub_events_offset=cursor.get("hub_events_offset") if pin else size)


def _apply_status(cfg, name: str, doc: dict, now) -> None:
    cache = {"fetched_at": store.now_iso(now),
             "projects": [p for p in map(clean_str, doc["projects"]) if p],
             "campaigns": clean_campaigns(doc["campaigns"])}

    def fn(recs):
        H.find(recs, name)["status_cache"] = cache
    H.update(cfg, fn)


def _failure_text(exc) -> str:
    return f"{type(exc).__name__}: {exc}"[:200]


def _apply_health(cfg, rec: dict, ev, st, now, moved: bool) -> None:
    """`ev` and `st` are the finished `(kind, value)` outcomes, or None for a dropped call.
    `moved` is false when this tick's page failed to checkpoint: the rescan offset then stays put
    so the next tick still sees that page's records."""
    name = rec["name"]
    health = dict(rec["health"]) if isinstance(rec.get("health"), dict) else {}
    cursor = rec["cursor"]
    fails = [o[1] for o in (ev, st) if o is not None and o[0] == "fail"]
    reached = (ev is not None and ev[0] in ("page", "reset")) or (st is not None and st[0] == "ok")
    update = {}
    event = None
    if reached:
        update["last_ok"] = store.now_iso(now)
        update["last_error"] = _failure_text(fails[0]) if fails else None
        if health.get("unreachable_reported"):
            event = ("host_reachable", lambda tail: not any(
                _own_health(ev_, name, "host_reachable") for ev_ in tail), {})
            update["unreachable_reported"] = False
        else:                       # an outage event whose flag checkpoint was lost is still closed
            event = ("host_reachable", lambda tail: _open_outage(tail, name), {})
    else:
        if fails:
            update["last_error"] = _failure_text(fails[0])
        last_ok = health.get("last_ok")
        ts = D.parse_ts(last_ok) if isinstance(last_ok, str) else None
        if (ts is not None and not health.get("unreachable_reported")
                and (now - ts).total_seconds() >= UNREACHABLE_S):
            event = ("host_unreachable", lambda tail: not _open_outage(tail, name),
                     {"since": last_ok})
            update["unreachable_reported"] = True
    if not update:
        return
    fields = {}
    if event is not None:
        type_, want, extra = event

        def build(tail):
            return ([_health_event(type_, name, now, **extra)] if want(tail) else []), \
                _uncheckpointed(tail, name, cursor)

        size, pending = _locked_append(cfg, name, cursor.get("hub_events_offset"), build)
        if moved and not pending:                           # else a page's records stay in the rescan
            fields["hub_events_offset"] = size

    def fn(recs):
        r = H.find(recs, name)
        r["health"] = {**(r["health"] if isinstance(r.get("health"), dict) else {}), **update}
        if fields:
            r["cursor"] = {**(r["cursor"] if isinstance(r.get("cursor"), dict) else {}), **fields}
    H.update(cfg, fn)


def _open_outage(tail: list[dict], name: str) -> bool:
    """The tail holds a `host_unreachable` for this host with no later `host_reachable`."""
    open_ = False
    for ev in tail:
        if _own_health(ev, name, "host_unreachable"):
            open_ = True
        elif _own_health(ev, name, "host_reachable"):
            open_ = False
    return open_


def relay_tick(cfg, *, now=None) -> None:
    """One relay pass over every enabled host (SPEC-0530): fetch events and status together,
    drop what is still running after `POOL_WAIT_S`, then apply on this thread in registry order."""
    recs = H.load(cfg)
    if not recs:
        return
    now = now or dt.datetime.now().astimezone()

    def live(rs):
        return [r for r in rs if isinstance(r, dict) and isinstance(r.get("name"), str)
                and r.get("enabled", True)]

    def needs_init(r):
        return not isinstance(r.get("cursor"), dict) or not isinstance(r.get("health"), dict)

    if any(needs_init(r) for r in live(recs)):
        size = _events_size(cfg)

        def init(rs):
            for r in live(rs):
                if not isinstance(r.get("cursor"), dict):
                    r["cursor"] = {"events_offset": 0, "events_origin_id": 0,
                                   "hub_events_offset": size}
                if not isinstance(r.get("health"), dict):
                    r["health"] = {"last_ok": None, "last_error": None,
                                   "unreachable_reported": False}
        recs = H.update(cfg, init)
    hosts = live(recs)
    if not hosts:
        return

    pool = cf.ThreadPoolExecutor(max_workers=2 * len(hosts), thread_name_prefix="nw-relay")
    jobs = {}
    try:
        for r in hosts:
            off = r["cursor"].get("events_offset")
            off = off if _is_int(off) and off >= 0 else 0
            jobs[r["name"]] = (pool.submit(_fetch_events, r, cfg, off), pool.submit(_fetch_status, r, cfg))
        cf.wait([f for pair in jobs.values() for f in pair], timeout=POOL_WAIT_S)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    def outcome(fut):
        if not fut.done():
            return None                                     # dropped: neither success nor failure
        try:
            return fut.result()
        except Exception as e:                              # noqa: BLE001
            return "fail", e

    results = {n: (outcome(a), outcome(b)) for n, (a, b) in jobs.items()}
    moved = {r["name"]: True for r in hosts}
    for r in hosts:                                         # event pages and resets
        ev = results[r["name"]][0]
        try:
            if ev is not None and ev[0] == "page":
                _apply_page(cfg, r["name"], r["cursor"], ev[1], now)
            elif ev is not None and ev[0] == "reset":
                _apply_reset(cfg, r["name"], r["cursor"], now)
        except Exception as e:                              # noqa: BLE001 - one host must not stop the rest
            moved[r["name"]] = False
            log.warning("federation relay: events for host %s not applied: %s", r["name"], e)
    for r in hosts:                                         # status caches
        st = results[r["name"]][1]
        if st is not None and st[0] == "ok":
            try:
                _apply_status(cfg, r["name"], st[1], now)
            except Exception as e:                          # noqa: BLE001
                log.warning("federation relay: status for host %s not stored: %s", r["name"], e)
    fresh = {x["name"]: x for x in live(H.load(cfg))}
    for r in hosts:                                         # health
        cur = fresh.get(r["name"])
        if cur is None or not isinstance(cur.get("cursor"), dict):
            continue
        try:
            _apply_health(cfg, cur, *results[r["name"]], now, moved[r["name"]])
        except Exception as e:                              # noqa: BLE001
            log.warning("federation relay: health for host %s not stored: %s", r["name"], e)


# ---- read-only views (SPEC-0530): the cached status only, no socket ---------------------------

def _views(cfg, now, project, state) -> list[tuple[dict, list[dict]]]:
    now = now or dt.datetime.now().astimezone()
    out = []
    for rec in H.load(cfg):
        if not isinstance(rec, dict) or not isinstance(rec.get("name"), str):
            continue
        cache = rec.get("status_cache") if isinstance(rec.get("status_cache"), dict) else {}
        fetched = cache.get("fetched_at") if isinstance(cache.get("fetched_at"), str) else None
        ts = D.parse_ts(fetched)
        age = int((now - ts).total_seconds()) if ts is not None else None
        stale = age is None or age > STALE_S
        camps = cache.get("campaigns") if isinstance(cache.get("campaigns"), list) else []
        kept = [c for c in camps if isinstance(c, dict) and isinstance(c.get("state"), str)
                and (project is None or c.get("project") == project)
                and (state is None or c["state"] == state)]
        counts: dict[str, int] = {}
        for c in kept:
            counts[c["state"]] = counts.get(c["state"], 0) + 1
        counts = dict(sorted(counts.items()))
        n = len(kept)
        ago = "never" if age is None else f"{age}s"
        line = f"host {rec['name']}: last contact {ago}, {n} campaigns"
        if n:
            line += " (" + ", ".join(f"{k}: {v}" for k, v in counts.items()) + ")"
        if stale:
            line += ", stale"
        view = {"host": rec["name"], "enabled": bool(rec.get("enabled", True)), "stale": stale,
                "fetched_at": fetched, "last_contact_s": age, "n": n, "counts": counts,
                "line": line}
        rows = [{"campaign": c.get("campaign"), "project": c.get("project"), "state": c["state"],
                 "spec_id": c.get("spec_id"), "kind": c.get("kind"), "host": rec["name"],
                 "stale": stale} for c in kept]
        out.append((view, rows))
    return out


def host_views(cfg, *, now=None, project=None, state=None) -> list[dict]:
    """One summary object per registered host (registry order, disabled included)."""
    return [v for v, _ in _views(cfg, now, project, state)]


def host_rows(cfg, *, now=None, project=None, state=None) -> list[dict]:
    """One thin item per cached host campaign, host order then cache order."""
    return [r for _, rows in _views(cfg, now, project, state) for r in rows]


# ---- exactly-once mail (SPEC-0532) -----------------------------------------------------------
# The outbox is `<agentic_dir>/mailbox/outbox-<host>.jsonl`: message lines and ack lines. Network
# calls run in a pool; every append, fsync and `hosts.update` runs on the calling thread.

_UID_RE = re.compile(r"[0-9a-f]{32}")
_ACK_RESULTS = ("delivered", "dup", "rejected")


def _fsync_fd(fd: int) -> None:
    os.fsync(fd)


def _append_durable(path: Path, line: bytes) -> None:
    """One `os.write` (O_APPEND) then an fsync; an fsync failure raises."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, line)
        _fsync_fd(fd)
    finally:
        os.close(fd)


def outbox_path(cfg, name: str) -> Path:
    return MB.mailbox_dir(cfg) / f"outbox-{E._safe(name)}.jsonl"


def _outbox_lock(cfg, name: str) -> Path:
    return MB.mailbox_dir(cfg) / f"outbox-{E._safe(name)}.lock"


def enqueue_remote(cfg, *, to, frm, text, kind="info", campaign=None, re=None,
                   needs_reply=None) -> dict:
    """Queue one message for `<local>@<host>` (`to` is the full address). Raises ValueError when
    the host is unknown or disabled, the local part is not `human` / `steward:<Name>`, or the
    host's cached project list lacks the project. Returns the outbox record, fsynced."""
    local, at, host = (to or "").strip().rpartition("@")
    rec = next((r for r in H.load(cfg) if isinstance(r, dict) and r.get("name") == host), None) \
        if at else None
    if rec is None:
        raise ValueError(f"unknown host {host!r}" if at else f"{to!r} has no @<host>")
    if not rec.get("enabled", True):
        raise ValueError(f"host {host!r} is disabled")
    local = local.strip()
    if local.lower().startswith("steward:") and local[len("steward:"):].strip():
        name = local[len("steward:"):].strip()
        cache = rec.get("status_cache")
        projects = cache.get("projects") if isinstance(cache, dict) else None
        if isinstance(projects, list):
            hit = {p.casefold(): p for p in projects if isinstance(p, str)}.get(name.casefold())
            if not hit:
                raise ValueError(f"host {host!r} has no project {name!r}; "
                                 f"known: {', '.join(p for p in projects if isinstance(p, str)) or '(none)'}")
            name = hit
        local = f"steward:{name}"
    elif local != "human":
        raise ValueError(f"unknown remote address {local!r} (human | steward:<Project>)@<host>")
    if kind not in MB.KINDS:
        raise ValueError(f"unknown kind {kind!r} ({' | '.join(MB.KINDS)})")
    if not (text or "").strip():
        raise ValueError("empty message text")
    out = {"msg_uid": uuid.uuid4().hex, "to": local, "from": frm, "kind": kind, "text": text,
           "campaign": campaign, "re": re,
           "needs_reply": bool(kind in ("question", "request") if needs_reply is None
                               else needs_reply)}
    line = (json.dumps(out) + "\n").encode("utf-8")
    with E._locked(_outbox_lock(cfg, host)):
        _append_durable(outbox_path(cfg, host), line)
    return out


def _read_outbox(cfg, name: str) -> tuple[list[dict], set[str]]:
    """`(message lines in file order, msg_uids that have an ack line)`."""
    msgs: list[dict] = []
    acked: set[str] = set()
    try:
        with open(outbox_path(cfg, name), "rb") as f:
            for raw in f:
                try:
                    obj = json.loads(raw)
                except (ValueError, RecursionError):
                    continue
                if not isinstance(obj, dict) or not isinstance(obj.get("msg_uid"), str):
                    continue
                if "result" in obj:
                    acked.add(obj["msg_uid"])
                else:
                    msgs.append(obj)
    except OSError:
        pass
    return msgs, acked


def pending_mail(cfg, name: str, limit: int = MAIL_LIMIT,
                 max_bytes: int = MAIL_POST_BYTES) -> list[dict]:
    """The unacked outbox lines in file order: at most `limit`, and at most `max_bytes` of body
    (the first line is always taken, so one large message is not stuck behind the cap)."""
    msgs, acked = _read_outbox(cfg, name)
    out, seen, size = [], set(), 0
    for m in msgs:
        u = m["msg_uid"]
        if u in acked or u in seen:
            continue
        seen.add(u)
        size += len(json.dumps(m, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) + 1
        if out and size > max_bytes:
            break
        out.append(m)
        if len(out) >= limit:
            break
    return out


def _mail_body(msgs: list[dict]) -> bytes:
    return json.dumps({"messages": msgs}, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")


def _mail_task(rec: dict, cfg, offset: int, pending: list[dict]) -> dict:
    """Pool task: HTTP only. `get` is `("page", doc)`, `("reset", None)` or `("fail", exc)`;
    `post` is None, `("resp", status, raw)` or `("fail", exc)`."""
    out: dict = {"get": None, "post": None}
    try:
        client = HubClient(rec, cfg)
        status, raw, _ = client.request(
            "GET", f"/v1/mail?offset={offset}&limit={MAIL_LIMIT}", allow=None)
        doc = json.loads(raw)
        if not isinstance(doc, dict):
            raise ConnectionError("response is not a JSON object")
        if status == 409 and doc.get("error") == "bad_offset":
            out["get"] = ("reset", None)
        elif status != 200:
            raise ConnectionError(f"HTTP {status}")
        else:
            nxt = doc.get("next_offset")
            if (doc.get("schema") != "nw.federation.mail.page.v1"
                    or not isinstance(doc.get("messages"), list) or not _is_int(nxt)
                    or nxt < offset):
                raise ConnectionError("bad mail page")
            out["get"] = ("page", doc)
    except Exception as e:                                  # noqa: BLE001 - reported as a failure
        out["get"] = ("fail", e)
        return out
    if pending:
        try:
            status, raw, _ = client.request("POST", "/v1/mail", _mail_body(pending), allow=None)
            out["post"] = ("resp", status, raw)
        except Exception as e:                              # noqa: BLE001
            out["post"] = ("fail", e)
    return out


def _inbox_size(cfg) -> int:
    try:
        return MB._inbox_path(cfg, "conductor").stat().st_size
    except OSError:
        return 0


def _find_in_inbox(path: Path, offset, uid: str) -> dict | None:
    """The inbox record carrying `uid`, parsing only lines from byte `offset` (a bad or non-line
    boundary offset falls back to 0)."""
    try:
        with open(path, "rb") as f:
            size = os.fstat(f.fileno()).st_size
            if not (_is_int(offset) and 0 < offset <= size):
                offset = 0
            if offset:
                f.seek(offset - 1)
                if f.read(1) != b"\n":
                    offset = 0
            f.seek(offset)
            for raw in f:
                try:
                    rec = json.loads(raw)
                except (ValueError, RecursionError):
                    continue
                if isinstance(rec, dict) and rec.get("msg_uid") == uid:
                    return rec
    except OSError:
        pass
    return None


def _valid_mail(m) -> bool:
    return (isinstance(m, dict) and isinstance(m.get("msg_uid"), str)
            and bool(_UID_RE.fullmatch(m["msg_uid"])) and m.get("kind") in MB.KINDS
            and isinstance(m.get("text"), str) and bool(m["text"].strip()))


def deliver_conductor(cfg, message: dict, host: str, *, cursor: dict | None = None,
                      now=None) -> str:
    """Write one host conductor message into the hub conductor inbox and one `message` event
    (SPEC-0532). Both appends are idempotent on `msg_uid` over a rescan of the log tails from the
    cursor's offsets (from byte 0 after a mail 409). Returns the hub inbox id. Does not call
    `mailbox.send` or `validate_address`."""
    if cursor is None:
        cursor = H.find(H.load(cfg), host).get("cursor") or {}
    replay = bool(_replay_mark(cursor))
    uid = message["msg_uid"]
    sender = message.get("from") if isinstance(message.get("from"), str) and message["from"] \
        else "host"
    frm = f"{sender}@{host}"
    camp = f"{message['campaign']}@{host}" \
        if isinstance(message.get("campaign"), str) and message["campaign"] else None
    ts = message["ts"] if isinstance(message.get("ts"), str) else store.now_iso(now)
    kind, text = message["kind"], message["text"]
    re_ = message["re"] if isinstance(message.get("re"), str) else None
    ipath = MB._inbox_path(cfg, "conductor")
    with E._locked(MB._inbox_lock(cfg, "conductor")):
        have = _find_in_inbox(ipath, 0 if replay else cursor.get("hub_inbox_offset"), uid)
        if have is not None and isinstance(have.get("id"), str):
            msg_id = have["id"]
        else:
            seq = MB._last_seq(ipath) + 1
            msg_id = f"conductor:{seq}"
            rec = {"schema": MB.SCHEMA, "id": msg_id, "seq": seq, "ts": ts, "from": frm,
                   "to": "conductor", "kind": kind, "campaign": camp, "re": re_, "text": text,
                   "needs_reply": message["needs_reply"] if isinstance(message.get("needs_reply"), bool)
                   else kind in ("question", "request"), "msg_uid": uid}
            _append_durable(ipath, (json.dumps(rec, ensure_ascii=False) + "\n").encode("utf-8"))
    with E._locked(E._lock_path(cfg)):
        tail = _scan_tail(cfg, 0 if replay else cursor.get("hub_mail_events_offset"))
        if not any(ev.get("type") == "message" and ev.get("host") == host
                   and isinstance(ev.get("data"), dict) and ev["data"].get("msg_uid") == uid
                   for ev in tail):
            E._append_locked(cfg, "message", campaign=camp, spec_id=None, project=None, by=frm,
                             host=host, ts=ts,
                             data={"msg_id": msg_id, "from": frm, "kind": kind, "text": text,
                                   "re": re_, "msg_uid": uid})
        _fsync_events(cfg)
    return msg_id


def _apply_mail_page(cfg, name: str, cursor: dict, doc: dict, now, *, events_ok: bool) -> None:
    for m in doc["messages"]:
        if _valid_mail(m):                  # anything else is consumed without a delivery
            deliver_conductor(cfg, m, name, cursor=cursor, now=now)
    nxt, mark = doc["next_offset"], _replay_mark(cursor)
    if nxt == cursor.get("mail_offset") and not mark:
        return
    if mark and nxt < mark and nxt != cursor.get("mail_offset"):
        _set_cursor(cfg, name, mail_offset=nxt)             # still replaying: window stays open
        return
    fields = {"mail_offset": nxt, "hub_inbox_offset": _inbox_size(cfg)}
    if events_ok:
        fields["hub_mail_events_offset"] = _events_size(cfg)

    def fn(recs):
        r = H.find(recs, name)
        cur = dict(r["cursor"]) if isinstance(r.get("cursor"), dict) else {}
        cur.update(fields)
        cur.pop("mail_replay", None)
        r["cursor"] = cur
    H.update(cfg, fn)


def _replay_mark(cursor: dict) -> int:
    """The host offset a mail replay runs up to (0 when none is open)."""
    m = cursor.get("mail_replay")
    return m if _is_int(m) and m > 0 else 0


def _apply_mail_reset(cfg, name: str, cursor: dict) -> None:
    """A verified mail 409: read from 0 next tick. `mail_replay` (the old `mail_offset`, the
    high-water mark of what may already be on the hub) makes every rescan until a page checkpoint
    reaches that mark start at byte 0 of the hub logs, so an already delivered `msg_uid` is found;
    the two hub offsets and the events log are left as they were."""
    off = cursor.get("mail_offset")
    mark = max(_replay_mark(cursor), off if _is_int(off) else 0, 1)
    _set_cursor(cfg, name, mail_offset=0, mail_replay=mark)


def _emit_rejected(cfg, name: str, cursor: dict, line: dict, reason: str, now) -> None:
    """One `host_mail_rejected` per rejected id (a rescan of the events tail dedupes), fsynced."""
    uid = line["msg_uid"]

    def build(tail):
        have = any(ev.get("type") == "host_mail_rejected" and _own_health(ev, name)
                   and ev["data"].get("msg_uid") == uid for ev in tail)
        return ([] if have else [_health_event("host_mail_rejected", name, now, msg_uid=uid,
                                               to=line.get("to"), reason=reason)]), None

    _locked_append(cfg, name, 0 if _replay_mark(cursor) else cursor.get("hub_mail_events_offset"),
                   build)


def _post_acks(posted: list[dict], status: int, raw: bytes) -> list[dict]:
    """The ack lines a verified POST response earns, or [] when every line stays pending."""
    uids = {m["msg_uid"] for m in posted}
    try:
        doc = json.loads(raw)
    except (ValueError, RecursionError):
        doc = None
    if status == 200:
        if not (isinstance(doc, dict) and doc.get("schema") == "nw.federation.mail.v1"
                and isinstance(doc.get("results"), list)):
            return []
        acks, seen = [], set()
        for el in doc["results"]:
            if not isinstance(el, dict) or el.get("result") not in _ACK_RESULTS:
                continue
            u = el.get("msg_uid")
            if u not in uids or u in seen:
                continue
            if el["result"] == "rejected":
                reason = el.get("reason")
                acks.append({"msg_uid": u, "result": "rejected",
                             "reason": (reason if isinstance(reason, str) else "rejected")[:200]})
            elif _is_int(el.get("seq")):
                acks.append({"msg_uid": u, "result": el["result"], "seq": el["seq"]})
            else:
                continue
            seen.add(u)
        return acks
    if 400 <= status <= 499:
        err = doc.get("error") if isinstance(doc, dict) else None
        reason = err[:200] if isinstance(err, str) else f"http_{status}"
        return [{"msg_uid": m["msg_uid"], "result": "rejected", "reason": reason} for m in posted]
    return []


def _apply_post(cfg, name: str, cursor: dict, posted: list[dict], resp, now) -> bool:
    """Write the acks a verified response earns. False when an ack or its event may not be durable."""
    if resp is None or resp[0] != "resp":
        return True
    acks = _post_acks(posted, resp[1], resp[2])
    by_uid = {m["msg_uid"]: m for m in posted}
    clean, keep = True, []
    for a in acks:
        if a["result"] == "rejected":
            try:
                _emit_rejected(cfg, name, cursor, by_uid[a["msg_uid"]], a["reason"], now)
            except Exception as e:                          # noqa: BLE001 - do not ack
                log.warning("federation mail: rejection of %s on host %s not recorded: %s",
                            a["msg_uid"], name, e)
                clean = False
                continue
        keep.append(a)
    if keep:
        data = "".join(json.dumps(a) + "\n" for a in keep).encode("utf-8")
        try:
            with E._locked(_outbox_lock(cfg, name)):
                _append_durable(outbox_path(cfg, name), data)
        except OSError as e:
            log.warning("federation mail: acks for host %s not durable: %s", name, e)
            clean = False
    return clean


def relay_mail(cfg, *, now=None) -> None:
    """One mail pass over every enabled host that has an event cursor (SPEC-0532): GET the host's
    conductor inbox page and POST pending outbox lines in a pool, drop what is still running after
    `POOL_WAIT_S`, then apply on this thread in registry order."""
    recs = H.load(cfg)
    if not recs:
        return
    now = now or dt.datetime.now().astimezone()

    def live(rs):
        return [r for r in rs if isinstance(r, dict) and isinstance(r.get("name"), str)
                and r.get("enabled", True) and isinstance(r.get("cursor"), dict)]

    todo = [r["name"] for r in live(recs) if "mail_offset" not in r["cursor"]]
    if todo:
        inbox, events = _inbox_size(cfg), _events_size(cfg)

        def init(rs):
            for n in todo:
                c = H.find(rs, n).get("cursor")
                if isinstance(c, dict) and "mail_offset" not in c:
                    c.update({"mail_offset": 0, "hub_inbox_offset": inbox,
                              "hub_mail_events_offset": events})
        recs = H.update(cfg, init)
    hosts = live(recs)
    if not hosts:
        return

    pool = cf.ThreadPoolExecutor(max_workers=len(hosts), thread_name_prefix="nw-mail")
    jobs = {}
    try:
        for r in hosts:
            off = r["cursor"].get("mail_offset")
            off = off if _is_int(off) and off >= 0 else 0
            posted = pending_mail(cfg, r["name"])
            jobs[r["name"]] = (posted, pool.submit(_mail_task, r, cfg, off, posted))
        finished, _ = cf.wait([f for _, f in jobs.values()], timeout=POOL_WAIT_S)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    for r in hosts:
        name = r["name"]
        posted, fut = jobs[name]
        if fut not in finished:
            continue                                        # dropped: neither acked nor moved
        try:
            out = fut.result()
        except Exception as e:                              # noqa: BLE001
            log.warning("federation mail: host %s failed: %s", name, e)
            continue
        try:
            cur = H.find(H.load(cfg), name).get("cursor")
            cur = cur if isinstance(cur, dict) else r["cursor"]
            ok = _apply_post(cfg, name, cur, posted, out["post"], now)
            kind, val = out["get"]
            if kind == "page":
                _apply_mail_page(cfg, name, cur, val, now, events_ok=ok)
            elif kind == "reset":
                _apply_mail_reset(cfg, name, cur)
        except Exception as e:                              # noqa: BLE001 - one host must not stop the rest
            log.warning("federation mail: host %s not applied: %s", name, e)
