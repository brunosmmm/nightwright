"""SPEC-0401 / SPEC-0400 invariant 1: building the snapshot (directly, through the cache with
every call rebuilding, and through `nw dashboard snapshot` in both forms) writes nothing under
the data dir: every file stays byte-identical and no file or directory appears."""
import hashlib

from fixtures.dashboard_data import seed
from test_dashboard_cli import run_nw
from nightwright.agentic import store as S
from nightwright.agentic.dashboard import snapshot as SN
import pytest

pytestmark = pytest.mark.fast


def tree(root):
    out = {}
    for p in sorted(root.rglob("*")):
        rel = str(p.relative_to(root))
        out[rel] = "dir" if p.is_dir() else hashlib.sha256(p.read_bytes()).hexdigest()
    return out


class Stepping:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        self.t += 1.0
        return self.t


def test_snapshot_paths_write_nothing(tmp_path):
    fx = seed(tmp_path)
    cfg, data = fx["cfg"], fx["data"]
    before = tree(data)
    for _ in range(50):
        SN.build(cfg)
    cache = SN.SnapshotCache(cfg, clock=Stepping(), refresh_s=1e-6, max_stale_s=1e-6)
    for _ in range(10):
        cache.get()
    assert cache.builds == 10
    for args in ((), ("--json",)):
        r = run_nw(fx, *args)
        assert r.returncode == 0, r.stderr
    after = tree(data)
    assert after == before, {k for k in set(after) ^ set(before)} or \
        {k for k in after if after[k] != before.get(k)}
    assert not (S.agentic_dir(cfg) / "dashboard").exists()


