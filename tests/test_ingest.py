"""Claude transcript ingestion against a synthetic fixture (SPEC-0001)."""
import os

from nightwright import ingest
import pytest

pytestmark = pytest.mark.fast

FIXROOT = os.path.join(os.path.dirname(__file__), "fixtures", "claude_root")


def test_load_streams_reads_fixture_session():
    cfg = {"_root": FIXROOT}
    streams, sessions = ingest.load_streams(cfg)
    assert sessions == {"S1"}
    assert len(streams) == 1
    evs = streams[0]
    assert len(evs) == 4
    # events sorted ascending; cwd carried across the events that omit it
    assert [e["ts"] for e in evs] == sorted(e["ts"] for e in evs)
    assert all(e["cwd"] == "/home/user/work/proj" for e in evs)
    # kind classification: typed prompt vs assistant vs tool_result (mid-turn)
    kinds = [e["kind"] for e in evs]
    assert kinds == ["prompt", "assistant", "tool", "prompt"]
