"""SPEC-0402: the page's live stream client (static/app.js) — the SSE parser and the Live logic
under node on a fake DOM (tests/js/live.test.js), and the same client over REAL HTTP against a
real dashboard server (tests/js/live_http.js: node's fetch, the real SSE endpoint, the real
snapshot builder). Names `live.test.js`, `live_http.js`, `app.js` and imports `assets` so
`affected_tests.py` selects this file when any of them changes."""
import json
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

import waits

from fixtures.dashboard_data import ACTIVE, seed
from test_dashboard_snapshot import ledger_run_end
from nightwright.agentic.dashboard import assets
from nightwright.agentic.dashboard import server as DS
from nightwright.agentic.dashboard import snapshot as SN
from waits import WAIT_S, until

pytestmark = pytest.mark.fast

ROOT = Path(__file__).resolve().parent.parent
NODE = shutil.which("node")
JS = ROOT / "tests" / "js"


def test_live_client_under_node():
    assert NODE, "node is required to run tests/js/live.test.js (the page's live-stream tests)"
    assert assets.read_asset("/app.js") is not None
    r = subprocess.run([NODE, "--test", str(JS / "live.test.js")], cwd=ROOT, capture_output=True,
                       text=True, timeout=WAIT_S)
    assert r.returncode == 0, f"node --test live.test.js failed:\n{r.stdout}\n{r.stderr}"
    assert re.search(r"skipped\s+0\b", r.stdout)


class Live:
    def __init__(self, tmp_path, max_clients=None):
        self.fx = seed(tmp_path)
        cfg = self.fx["cfg"]
        if max_clients:
            cfg.setdefault("agentic", {}).setdefault("dashboard", {})["max_clients"] = max_clients
        self.cfg = cfg
        cache = SN.SnapshotCache(cfg, refresh_s=0.1, max_stale_s=3600)
        self.srv = DS.make_server(cfg, "127.0.0.1:0", cache=cache)
        self.t = threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.05},
                                  daemon=True)
        self.t.start()
        self.port = self.srv.server_address[1]

    def node(self, scenario):
        env = {"PATH": str(Path(NODE).parent), "NW_LIVE_BASE": f"http://127.0.0.1:{self.port}",
               "NW_LIVE_SCENARIO": scenario}      # SPEC-0428: no token
        return subprocess.Popen([NODE, str(JS / "live_http.js")], cwd=ROOT, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def close(self):
        self.srv.shutdown()
        waits.join(self.t, 10)
        self.srv.server_close()


def events(proc):
    out = []
    for ln in proc.stdout:
        out.append(json.loads(ln))
        yield out[-1]


@pytest.fixture
def live(tmp_path):
    made = []

    def mk(**kw):
        s = Live(tmp_path, **kw)
        made.append(s)
        return s
    yield mk
    for s in made:
        s.close()


@pytest.mark.skipif(not NODE, reason="node is required")
def test_page_goes_live_updates_and_reopens_over_real_http(live):
    s = live()
    p = s.node("live")
    try:
        seen = {}
        for ev in events(p):
            seen[ev["at"]] = ev
            if ev["at"] == "live":
                assert ev["indicator"] == "live"
                assert all("token" not in u and "Bearer" not in u for u in ev["urls"])
                ledger_run_end(s.cfg, ACTIVE, 9.0)            # a queue change; no reload
            if ev["at"] in ("reopened", "timeout", "error"):
                break
        assert p.wait(WAIT_S) == 0, (seen, p.stderr.read())
    finally:
        p.kill()
    assert {"live", "changed", "hidden", "reopened"} <= set(seen), seen
    assert seen["changed"]["rev"] != seen["live"]["rev"]
    assert seen["changed"]["polls"] == 1, "only the initial poll: the stream carried the change"
    assert seen["hidden"]["mode"] == "off"
    assert [u.split("?")[0] for u in seen["reopened"]["urls"]] == ["/api/stream", "/api/stream"]
    assert seen["reopened"]["urls"][1].startswith("/api/stream?rev=")
    assert until(lambda: s.srv.hub.stats()["snapshot_clients"] == 0), "a closed page holds no slot"


@pytest.mark.skipif(not NODE, reason="node is required")
def test_page_falls_back_to_polling_when_the_stream_answers_503(live):
    s = live(max_clients=1)
    from test_dashboard_stream import Client
    hog = Client(s.port, None)                                # takes the only slot
    try:
        p = s.node("busy")
        out, err = p.communicate(timeout=WAIT_S)
    finally:
        hog.close()
    assert p.returncode == 0, err
    ev = [json.loads(ln) for ln in out.splitlines()][-1]
    assert ev["indicator"] == "polling" and ev["mode"] == "polling"
    assert ev["spend"].startswith("spend today"), "the snapshot still rendered through polling"

