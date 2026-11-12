"""Related-idea search via `nw ideas --query` (SPEC-0086)."""
import json
from zoneinfo import ZoneInfo

from click.testing import CliRunner

from nightwright import explore as EX
from nightwright import org_write as W
from nightwright import report as R
from nightwright.cli import cli
from nightwright.org import load_tasks
import pytest

pytestmark = pytest.mark.fast


def _cfg(tmp_path):
    org = tmp_path / "org"
    org.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    return {
        "org_files": [str(org)],
        "org_todo_keywords": ["TODO", "|", "DONE"],
        "org_idea_keywords": [
            "IDEA", "INCUBATE", "SPECCED", "|", "PROMOTED", "EXPORTED", "DROPPED", "RESEARCHED",
        ],
        "timezone": "America/New_York",
        "_tz": ZoneInfo("America/New_York"),
        "data_dir": str(data),
        "org_capture_file": str(org / "inbox.org"),
        "org_ideas_file": str(org / "ideas.org"),
        "org_ideas_archive_file": str(org / "ideas-archive.org"),
        "specs_dir": str(tmp_path / "specs"),
    }


def _patch(monkeypatch, cfg):
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)


def test_tokenize_and_score_weights():
    assert R.tokenize_idea_query("  SpecRef  Resolver ") == ["specref", "resolver"]
    assert R.score_idea_match(["specref"], "Unified SpecRef resolver", "", "", "") == 3
    assert R.score_idea_match(["specref"], "other", "mentions specref here", "", "") == 2
    assert R.score_idea_match(["specref"], "x", "", "", "log specref") == 1
    assert R.score_idea_match(["specref", "zzz"], "SpecRef only", "", "", "") is None


def test_heading_and_summary_hits(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "Unified SpecRef resolver")
    _, _, body_id = W.add_idea(cfg, "Namespace-aware known_epics")
    EX.set_summary(cfg, body_id, "Couples tightly with SpecRef world")
    W.add_idea(cfg, "unrelated clocks")

    hits = R.search_idea_tasks(cfg, "specref")
    ids = [t.properties["ID"] for t, _s, _snip in hits]
    assert body_id in ids
    assert any("SpecRef" in (t.heading or "") for t, *_ in hits)
    assert all("clocks" not in (t.heading or "").lower() for t, *_ in hits)


def test_and_compose_and_empty_miss(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "Unified SpecRef resolver")
    assert R.search_idea_tasks(cfg, "specref missingtoken") == []
    hits = R.search_idea_tasks(cfg, "specref resolver")
    assert len(hits) == 1


def test_closed_ideas_included(tmp_path):
    cfg = _cfg(tmp_path)
    path, _, idea_id = W.add_idea(cfg, "shipped SpecRef generate footgun")
    task = next(t for t in load_tasks(cfg) if t.properties.get("ID") == idea_id)
    W.set_state(cfg, task, "PROMOTED")
    # Default list excludes done; search must still find it.
    assert R.collect_idea_tasks(cfg, all_done=False) == []
    hits = R.search_idea_tasks(cfg, "specref")
    assert [t.properties["ID"] for t, *_ in hits] == [idea_id]
    assert path  # silence lint


def test_non_idea_tasks_excluded(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "plain idea about widgets")
    inbox = cfg["org_capture_file"]
    open(inbox, "w").write("#+TODO: TODO | DONE\n* TODO search for related widgets in inbox\n")
    hits = R.search_idea_tasks(cfg, "related")
    assert len(hits) == 0
    hits = R.search_idea_tasks(cfg, "widgets")
    assert len(hits) == 1
    assert hits[0][0].is_idea


def test_ranking_prefers_heading(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "alpha topic in heading")
    _, _, body_id = W.add_idea(cfg, "something else entirely")
    EX.set_summary(cfg, body_id, "mentions alpha only in summary")
    hits = R.search_idea_tasks(cfg, "alpha")
    assert hits[0][1] > hits[1][1]
    assert "heading" in hits[0][0].heading


def test_limit(tmp_path):
    cfg = _cfg(tmp_path)
    for i in range(5):
        W.add_idea(cfg, f"needle item {i}")
    assert len(R.search_idea_tasks(cfg, "needle", limit=2)) == 2
    assert len(R.search_idea_tasks(cfg, "needle", limit=0)) == 5


def test_cli_json_search_schema(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "Unified SpecRef resolver")
    _patch(monkeypatch, cfg)

    r = CliRunner().invoke(cli, ["ideas", "--query", "specref", "--json"])
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert data["schema"] == "nw.ideas.search.v1"
    assert data["query"] == "specref"
    assert data["ideas"]
    hit = data["ideas"][0]
    assert "score" in hit and "snippet" in hit
    assert hit["id"].startswith("IDEA-")

    r2 = CliRunner().invoke(cli, ["ideas", "--json"])
    assert r2.exit_code == 0, r2.output
    assert json.loads(r2.output)["schema"] == "nw.ideas.v1"


def test_cli_empty_query_errors(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _patch(monkeypatch, cfg)
    r = CliRunner().invoke(cli, ["ideas", "--query", "   "])
    assert r.exit_code != 0


def test_cli_kind_filter(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "needle as idea kind")
    W.add_idea(cfg, "needle as bug kind", kind="bug")
    _patch(monkeypatch, cfg)
    r = CliRunner().invoke(cli, ["ideas", "--query", "needle", "--kind", "bug", "--json"])
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert len(data["ideas"]) == 1
    assert data["ideas"][0]["kind"] == "bug"


def test_resolve_selector_still_ambiguous(tmp_path, monkeypatch):
    """Search is multi-hit; resolve_selector remains single-match (SPEC-0086 non-goal)."""
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "related alpha")
    W.add_idea(cfg, "related beta")
    _patch(monkeypatch, cfg)
    r = CliRunner().invoke(cli, ["idea", "show", "related"])
    assert r.exit_code != 0
    assert "ambiguous" in r.output.lower()
    r2 = CliRunner().invoke(cli, ["ideas", "--query", "related", "--json"])
    assert r2.exit_code == 0
    assert len(json.loads(r2.output)["ideas"]) == 2


def _ideas_bytes(cfg):
    import os
    p = cfg["org_ideas_file"]
    return open(p, "rb").read() if os.path.exists(p) else None


def _seed_search(cfg):
    W.add_idea(cfg, "Unified SpecRef resolver")
    _, _, pid = W.add_idea(cfg, "shipped SpecRef generate footgun")
    task = next(t for t in load_tasks(cfg) if t.properties.get("ID") == pid)
    W.set_state(cfg, task, "PROMOTED")
    W.add_idea(cfg, "SpecRef resolver cache")
    W.add_idea(cfg, "unrelated clocks")


def test_idea_search_matches_ideas_query(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _patch(monkeypatch, cfg)
    _seed_search(cfg)
    before = _ideas_bytes(cfg)
    a = CliRunner().invoke(cli, ["idea", "search", "specref", "resolver"])
    b = CliRunner().invoke(cli, ["ideas", "--query", "specref resolver"])
    assert a.exit_code == 0 and a.output == b.output
    assert "clocks" not in a.output
    a = CliRunner().invoke(cli, ["idea", "search", "specref", "--json"])
    b = CliRunner().invoke(cli, ["ideas", "--query", "specref", "--json"])
    assert a.exit_code == 0 and json.loads(a.output) == json.loads(b.output)
    assert len(json.loads(a.output)["ideas"]) == 3          # includes the PROMOTED one
    assert _ideas_bytes(cfg) == before


def test_idea_search_is_read_only(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _patch(monkeypatch, cfg)
    _seed_search(cfg)
    before = _ideas_bytes(cfg)
    for persona in (None, "1"):
        if persona:
            monkeypatch.setenv("NW_PERSONA_RUN", persona)
        r = CliRunner().invoke(cli, ["idea", "search", "specref"])
        assert r.exit_code == 0, r.output
        assert _ideas_bytes(cfg) == before


def test_idea_search_limit(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _patch(monkeypatch, cfg)
    _seed_search(cfg)
    r = CliRunner().invoke(cli, ["idea", "search", "specref", "--limit", "1", "--json"])
    assert r.exit_code == 0 and len(json.loads(r.output)["ideas"]) == 1
    r = CliRunner().invoke(cli, ["idea", "search", "specref", "--limit", "0", "--json"])
    assert len(json.loads(r.output)["ideas"]) == 3


def test_idea_search_requires_text(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _patch(monkeypatch, cfg)
    W.add_idea(cfg, "seed")
    before = _ideas_bytes(cfg)
    for args in (["idea", "search"], ["idea", "search", "   "]):
        assert CliRunner().invoke(cli, args).exit_code != 0
    assert _ideas_bytes(cfg) == before
