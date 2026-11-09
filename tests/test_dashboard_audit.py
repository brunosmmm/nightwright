"""SPEC-0414/0429: the dashboard audit log (unit)."""
import json
import os
import stat

from nightwright.agentic.dashboard import audit as AU
import pytest

pytestmark = pytest.mark.fast

KEYS = {"schema", "ts", "action", "reason", "status", "method", "path", "login", "node",
        "node_ip", "user_agent", "detail", "esc_id", "form", "summary", "result"}


def lines(tmp_path, name="audit.jsonl"):
    p = tmp_path / "agentic" / "dashboard" / name
    return [json.loads(x) for x in p.read_text().splitlines()]


def test_line_has_the_schema_keys_and_no_query_or_secrets(tmp_path):
    cfg = {"data_dir": str(tmp_path)}
    AU.audit(cfg, reason="bad_host", status=403, method="GET",
             path="/api/snapshot?token=SECRET#frag", user_agent="curl/8",
             authorization="Bearer SECRET", token="SECRET")
    (rec,) = lines(tmp_path)
    assert set(rec) == KEYS and rec["schema"] == "nw.dashboard.audit.v1"
    assert rec["action"] == "refused" and rec["reason"] == "bad_host" and rec["status"] == 403
    assert rec["path"] == "/api/snapshot"
    assert rec["login"] is rec["node"] is rec["node_ip"] is None
    assert rec["esc_id"] is rec["form"] is rec["summary"] is rec["result"] is None
    assert "SECRET" not in (tmp_path / "agentic/dashboard/audit.jsonl").read_text()
    mode = stat.S_IMODE((tmp_path / "agentic/dashboard/audit.jsonl").stat().st_mode)
    assert mode == 0o600


def test_path_and_user_agent_are_truncated(tmp_path):
    AU.audit({"data_dir": str(tmp_path)}, reason="bad_host", status=403, method="GET",
             path="/" + "a" * 500, user_agent="u" * 500)
    (rec,) = lines(tmp_path)
    assert len(rec["path"]) == 200 and len(rec["user_agent"]) == 200


def test_rotation_keeps_one_backup(tmp_path, monkeypatch):
    cfg = {"data_dir": str(tmp_path)}
    monkeypatch.setattr(AU, "MAX_BYTES", 2000)
    for i in range(60):
        AU.audit(cfg, reason="bad_host", status=403, method="GET", path=f"/p{i}")
    d = tmp_path / "agentic" / "dashboard"
    assert sorted(p.name for p in d.iterdir()) == ["audit.jsonl", "audit.jsonl.1"]
    for p in d.iterdir():
        assert p.stat().st_size <= 2000
    assert lines(tmp_path)[-1]["path"] == "/p59"


def test_a_failed_write_does_not_raise(tmp_path):
    cfg = {"data_dir": str(tmp_path)}
    d = tmp_path / "agentic" / "dashboard"
    d.mkdir(parents=True)
    (d / "audit.jsonl").mkdir()                 # open() fails with EISDIR, even as root
    AU.audit(cfg, reason="bad_host", status=403, method="GET", path="/")
    os.rmdir(d / "audit.jsonl")
    ro = tmp_path / "ro"
    ro.write_text("")                           # data_dir is a file: dashboard_dir() fails
    AU.audit({"data_dir": str(ro)}, reason="bad_host", status=403, method="GET", path="/")


def test_action_is_plan_answer_or_refused(tmp_path):
    cfg = {"data_dir": str(tmp_path)}
    for a in ("plan", "answer", "bogus", None):
        AU.audit(cfg, action=a, status=200, method="POST", path="/api/escalations/E1/plan",
                 esc_id="E1", form="choice", summary="s" * 500, result="ok")
    recs = lines(tmp_path)
    assert [r["action"] for r in recs] == ["plan", "answer", "refused", "refused"]
    assert recs[0]["esc_id"] == "E1" and recs[0]["form"] == "choice"
    assert recs[0]["result"] == "ok" and len(recs[0]["summary"]) == 400


