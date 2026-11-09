"""SPEC-0402: `/api/stream` from a real `nw serve --dashboard` subprocess over real HTTP."""
import json
import urllib.request

from fixtures.dashboard_data import ACTIVE, seed
from test_dashboard_serve_cli import start, stop, wait_server
from test_dashboard_snapshot import ledger_run_end
import pytest

pytestmark = pytest.mark.integration


def events(resp):
    """Yield `(event, id, data)` for each SSE event on a `urlopen` response."""
    ev = {}
    for raw in resp:
        line = raw.decode().rstrip("\r\n")
        if line == "":
            if "event" in ev:
                yield ev["event"], ev.get("id"), json.loads(ev["data"])
            ev = {}
        elif not line.startswith(":"):
            k, _, v = line.partition(": ")
            ev[k] = v


def test_stream_delivers_a_snapshot_and_then_a_change(tmp_path):
    fx = seed(tmp_path)
    proc = start(fx, "--bind", "127.0.0.1:0")
    try:
        rec = wait_server(fx, proc)
        req = urllib.request.Request(f"{rec['url']}/api/stream")      # no credential (SPEC-0428)
        with urllib.request.urlopen(req, timeout=60) as resp:
            assert resp.headers["Content-Type"].startswith("text/event-stream")
            it = events(resp)
            name, rev, doc = next(it)
            assert name == "snapshot" and rev == doc["rev"] and doc["schema"] == "nw.dashboard.v1"
            ledger_run_end(fx["cfg"], ACTIVE, 7.0)
            name2, rev2, doc2 = next(it)
            assert name2 == "snapshot" and rev2 == doc2["rev"] != rev
    finally:
        stop(proc)


