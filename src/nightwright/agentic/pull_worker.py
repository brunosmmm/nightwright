"""`nw worker serve` (SPEC-0334): a pull worker's loop against `nw serve --jobs`.

Lease a job (long-poll), fetch its bundle, verify the signed header (signature, expiry, nonce,
bundle hash), start the job process exactly as `nw worker submit` does, then tail its
`frames.jsonl` to `POST frames` every second. A `done` frame's result bundle is PUT first and the
frame carries its sha256 instead. Every successful frames post or heartbeat renews the local
`lease.json` by the coordinator's `job_lease_s`, so a job outlives an outage only while that
lease lasts. `cancel: true` runs `nw worker cancel`; 410 cancels that job; 401 (revoked) cancels
every local job and exits non-zero. The posted cursor comes from the server, so a restart
re-posts from there with no duplicates; the tracked job ids survive in `pull-jobs.json`."""
from __future__ import annotations

import base64
import hashlib
import http.client
import io
import json
import ssl
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from . import pull_jobs as PJ
from . import worker as W
from . import worker_job as WJ
from .store import now_iso

EXIT_REVOKED = 3
BACKOFF_START_S, BACKOFF_MAX_S = 2.0, 60.0
TICK_S = 1.0
MAX_BATCH_BYTES = 3 << 20           # under the server's 4 MiB frames cap
CALL_TIMEOUT_S = PJ.LEASE_WAIT_S + 30


class Revoked(Exception):
    """The coordinator answered 401: this worker's credential no longer works."""


class Transport(Exception):
    """The coordinator could not be reached (or answered garbage)."""


def conf_path(cfg) -> Path:
    return Path(cfg["config_dir"]) / "coordinator.json"


def load_conf(cfg, blob: str | None, conf: str | None) -> dict:
    """The coordinator document: from `blob` (stored 0600 in `conf` or `coordinator.json`), else
    read from `conf` or `coordinator.json`. ValueError when there is none or it is unusable."""
    p = Path(conf) if conf else conf_path(cfg)
    if blob:
        doc = PJ.parse_blob(blob)
        PJ.write_private(p, json.dumps(doc, indent=2, sort_keys=True) + "\n")
        return doc
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ValueError(f"no {p}: enrol first with `nw worker serve --enrol BLOB`")
    except (OSError, ValueError) as e:
        raise ValueError(f"{p} is unreadable: {e}")
    return PJ.parse_blob(base64.b64encode(json.dumps(doc).encode()).decode())


def _log(msg: str) -> None:
    print(f"nw worker serve: {msg}", file=sys.stderr, flush=True)


class Client:
    """One HTTP(S) call per request to the coordinator, with the bearer token. `ca`, `cert` and
    `key` in the coordinator document configure TLS (a private CA, a client certificate)."""

    def __init__(self, doc: dict):
        u = urlsplit(doc["coordinator_url"])
        if u.scheme not in ("http", "https") or not u.hostname:
            raise ValueError(f"coordinator_url {doc['coordinator_url']!r} is not http(s)://HOST")
        self.https, self.host = u.scheme == "https", u.hostname
        self.port = u.port or (443 if self.https else 80)
        self.base = u.path.rstrip("/")
        self.token = doc["token"]
        self.ctx = None
        if self.https:
            self.ctx = ssl.create_default_context(cafile=doc.get("ca"))
            if doc.get("cert"):
                self.ctx.load_cert_chain(doc["cert"], doc.get("key"))

    def call(self, method: str, path: str, body: bytes | None = None,
             timeout: float = CALL_TIMEOUT_S, ctype: str = "application/json"):
        """`(status, body bytes)`; `Revoked` on 401, `Transport` on a network failure."""
        if self.https:
            conn = http.client.HTTPSConnection(self.host, self.port, timeout=timeout,
                                               context=self.ctx)
        else:
            conn = http.client.HTTPConnection(self.host, self.port, timeout=timeout)
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": ctype,
                   "Content-Length": str(len(body or b""))}
        try:
            conn.request(method, self.base + path, body=body or b"", headers=headers)
            r = conn.getresponse()
            data = r.read()
        except (OSError, http.client.HTTPException) as e:
            raise Transport(f"{type(e).__name__}: {e}")
        finally:
            conn.close()
        if r.status == 401:
            raise Revoked(data.decode("utf-8", "replace"))
        return r.status, data

    def json(self, method: str, path: str, doc: dict, timeout: float = CALL_TIMEOUT_S):
        status, data = self.call(method, path, json.dumps(doc).encode("utf-8"), timeout)
        try:
            body = json.loads(data) if data else {}
        except ValueError:
            raise Transport(f"{path}: a non-JSON answer (HTTP {status})")
        return status, body if isinstance(body, dict) else {}


class Loop:
    def __init__(self, cfg, doc: dict, *, kinds=("gate", "persona"), slots: int = 1,
                 client: Client | None = None):
        self.cfg, self.secret = cfg, doc["secret"]
        self.client = client or Client(doc)
        self.kinds, self.slots = list(kinds), slots
        self.lease_s = WJ.settings(cfg)["job_lease_s"]     # until the coordinator says
        self.next_hb = 0.0
        self.jobs: dict = {jid: {"posted": None, "cancelled": False}
                           for jid in self._read_tracked()}

    # ---- local state ---------------------------------------------------------------------

    def _tracked_path(self) -> Path:
        return W.worker_dir(self.cfg) / "pull-jobs.json"

    def _read_tracked(self) -> list[str]:
        try:
            ids = json.loads(self._tracked_path().read_text(encoding="utf-8")).get("jobs")
        except (OSError, ValueError, AttributeError):
            return []
        return [i for i in ids if isinstance(i, str)] if isinstance(ids, list) else []

    def _save_tracked(self) -> None:
        p = self._tracked_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        WJ._write_atomic(p, {"jobs": sorted(self.jobs)})

    def _nonces_path(self) -> Path:
        return W.worker_dir(self.cfg) / "nonces.jsonl"

    def _nonces(self) -> list[dict]:
        try:
            lines = self._nonces_path().read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        out = []
        for ln in lines:
            try:
                rec = json.loads(ln)
            except ValueError:
                continue
            if isinstance(rec, dict):
                out.append(rec)
        return out

    def seen(self, nonce: str) -> bool:
        return any(r.get("nonce") == nonce for r in self._nonces())

    def remember(self, nonce: str, expires_at) -> None:
        """Keep the nonce until its job's `expires_at` (+ the clock skew) has passed."""
        now = time.time()
        keep = [r for r in self._nonces() if isinstance(r.get("expires_at"), (int, float))
                and r["expires_at"] + PJ.CLOCK_SKEW_S >= now]
        keep.append({"nonce": nonce, "expires_at": expires_at})
        p = self._nonces_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f".{p.name}.tmp")
        tmp.write_text("".join(json.dumps(r) + "\n" for r in keep), encoding="utf-8")
        tmp.replace(p)

    # ---- the loop ------------------------------------------------------------------------

    def run(self) -> int:
        delay = BACKOFF_START_S
        while True:
            try:
                self.tick()
                delay = BACKOFF_START_S
            except Revoked:
                _log("the coordinator revoked this worker (401); cancelling every local job")
                self.cancel_all()
                return EXIT_REVOKED
            except Transport as e:
                _log(f"coordinator unreachable ({e}); retrying in {delay:g}s")
                time.sleep(delay)
                delay = min(delay * 2, BACKOFF_MAX_S)
            except KeyboardInterrupt:
                return 130

    def tick(self) -> None:
        for jid in list(self.jobs):
            self.sync(jid)
        if self.jobs and time.monotonic() >= self.next_hb:
            self.heartbeat()
        free = self.slots - len(self.jobs)
        if free <= 0:
            time.sleep(TICK_S)
            return
        got = self.lease(free, TICK_S if self.jobs else PJ.LEASE_WAIT_S)
        if got is not None:
            self.start(got)

    def lease(self, free: int, wait_s: float) -> dict | None:
        status, body = self.client.json("POST", "/v1/lease", {
            "kinds": self.kinds, "slots_free": free, "wait_s": wait_s})
        if status == 204:
            return None
        if status != 200 or not isinstance(body.get("job"), dict):
            raise Transport(f"lease: HTTP {status}: {body.get('error')}")
        return body

    def _set_lease_s(self, body: dict) -> None:
        v = body.get("job_lease_s")
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
            self.lease_s = float(v)

    def start(self, got: dict) -> None:
        """Fetch, verify and start one leased job; a refused one gets an `error` frame
        (`stage: "verify"`) and nothing runs."""
        self._set_lease_s(got)
        h = got["job"]
        jid = h.get("job_id") if isinstance(h.get("job_id"), str) else None
        if jid is None or not W.KEY_RE.match(jid):
            _log("leased a job without a usable job_id; ignored")
            return
        status, data = self.client.call("GET", f"/v1/jobs/{jid}/bundle")
        if status != 200:
            _log(f"{jid}: bundle fetch got HTTP {status}; dropped")
            return
        why = PJ.verify(self.secret, h, data, self.seen)
        if why is not None:
            _log(f"{jid}: refused: {why}")
            return self._refuse(jid, "verify", why)
        self.remember(h["nonce"], h["expires_at"])
        out = io.BytesIO()
        line = json.dumps(h, ensure_ascii=False).encode("utf-8") + b"\n"
        rc = WJ.submit(self.cfg, io.BytesIO(line + data), out)
        if rc != 0:
            err = next((r for r in _lines(out.getvalue()) if r.get("type") == "error"), {})
            return self._refuse(jid, str(err.get("stage") or "submit"),
                                str(err.get("error") or f"submit exited {rc}"))
        self.jobs[jid] = {"posted": 0, "cancelled": False}
        self._save_tracked()
        self._renew(jid)

    def _refuse(self, jid: str, stage: str, error: str) -> None:
        frame = {"schema": W.FRAME_SCHEMA, "type": "error", "job_id": jid, "at": now_iso(),
                 "n": 1, "stage": stage, "error": error}
        self.client.json("POST", f"/v1/jobs/{jid}/frames", {"frames": [frame]})

    def _job(self, jid: str) -> Path:
        return W.jobs_dir(self.cfg) / jid

    def _renew(self, jid: str) -> None:
        job = self._job(jid)
        if job.is_dir() and not (job / "result.json").exists():
            WJ._renew_live(self.cfg, job, self.lease_s)

    def cancel(self, jid: str) -> None:
        st = self.jobs.get(jid)
        if st is not None and st["cancelled"]:
            return
        if st is not None:
            st["cancelled"] = True
        _log(f"{jid}: cancelling")
        WJ.cancel(self.cfg, jid, io.BytesIO())

    def cancel_all(self) -> None:
        for jid in list(self.jobs):
            self.cancel(jid)

    def drop(self, jid: str) -> None:
        self.jobs.pop(jid, None)
        self._save_tracked()

    def _outgoing(self, jid: str, frame: dict) -> dict | None:
        """A `done` frame's result bundle goes up first (`PUT result-bundle`); the frame then
        carries `result_bundle_sha256` instead of the bytes. None when the upload failed."""
        bundle = frame.get("bundle")
        if frame.get("type") != "done" or not isinstance(bundle, dict) \
                or not bundle.get("bundle_b64"):
            return frame
        data = base64.b64decode(bundle["bundle_b64"])
        status, _ = self.client.call("PUT", f"/v1/jobs/{jid}/result-bundle", data,
                                     ctype="application/octet-stream")
        if status != 200:
            _log(f"{jid}: result-bundle upload got HTTP {status}")
            if status == 410:
                self._gone(jid)
            return None
        return {**frame, "bundle": {**bundle, "bundle_b64": None},
                "result_bundle_sha256": hashlib.sha256(data).hexdigest()}

    def _gone(self, jid: str) -> None:
        _log(f"{jid}: the coordinator gave the job up (410)")
        self.cancel(jid)
        self.drop(jid)

    def _post(self, jid: str, frames: list) -> bool:
        """POST one batch; True when the job is still tracked afterwards."""
        status, body = self.client.json("POST", f"/v1/jobs/{jid}/frames", {"frames": frames})
        if status == 410:
            self._gone(jid)
            return False
        if status == 403:
            _log(f"{jid}: not leased to this worker (403); dropped")
            self.drop(jid)
            return False
        if status == 409:                       # the bundle is not stored yet: next tick
            return True
        if status != 200:
            raise Transport(f"frames: HTTP {status}: {body.get('error')}")
        self._set_lease_s(body)
        st = self.jobs[jid]
        if isinstance(body.get("cursor"), int):
            st["posted"] = body["cursor"]
        self._renew(jid)
        if body.get("cancel"):
            self.cancel(jid)
        return True

    def sync(self, jid: str) -> None:
        """Post the frames after the server's cursor (learnt with an empty post after a
        restart); drop the job once it is final here and every frame is posted."""
        st = self.jobs[jid]
        if st["posted"] is None and not self._post(jid, []):
            return
        job = self._job(jid)
        if not job.is_dir():
            _log(f"{jid}: the job dir is gone; dropped")
            return self.drop(jid)
        reader = WJ._Reader(job / "frames.jsonl", st["posted"])
        batch, size = [], 0
        for raw in reader.read():
            try:
                frame = json.loads(raw)
            except ValueError:
                continue
            frame = self._outgoing(jid, frame) if isinstance(frame, dict) else None
            if frame is None or jid not in self.jobs:
                break
            n = len(json.dumps(frame))
            if batch and size + n > MAX_BATCH_BYTES:
                if not self._post(jid, batch):
                    return
                batch, size = [], 0
            batch.append(frame)
            size += n
        if jid not in self.jobs or not self._post(jid, batch):
            return
        state = WJ.job_state(self.cfg, job)["state"]
        if state in ("done", "error", "lost") and st["posted"] >= WJ._last_n(job):
            if state == "lost" and not reader.final:
                _log(f"{jid}: the job process was lost")
            self.drop(jid)

    def heartbeat(self) -> None:
        jobs = [{"id": jid, "n": st["posted"] or 0,
                 "state": WJ.job_state(self.cfg, self._job(jid))["state"]
                 if self._job(jid).is_dir() else "unknown"} for jid, st in self.jobs.items()]
        status, body = self.client.json("POST", "/v1/heartbeat", {"jobs": jobs})
        if status != 200:
            raise Transport(f"heartbeat: HTTP {status}")
        self._set_lease_s(body)
        self.next_hb = time.monotonic() + self.lease_s / 3
        for jid, ans in (body.get("jobs") or {}).items():
            if jid not in self.jobs or not isinstance(ans, dict):
                continue
            if ans.get("gone"):
                self._gone(jid)
                continue
            self._renew(jid)
            if ans.get("cancel"):
                self.cancel(jid)


def _lines(raw: bytes) -> list[dict]:
    out = []
    for ln in raw.splitlines():
        try:
            rec = json.loads(ln)
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out
