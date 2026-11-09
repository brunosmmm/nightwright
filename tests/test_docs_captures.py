"""SPEC-0149/0152/0154: committed docs SVG captures + org example fixtures."""
from pathlib import Path
import pytest

pytestmark = pytest.mark.fast

ROOT = Path(__file__).resolve().parent.parent
CAPTURES = ROOT / "docs" / "product" / "assets" / "captures"
EXAMPLES = ROOT / "docs" / "product" / "assets" / "examples"
GUIDE = ROOT / "docs" / "product" / "guides" / "org-storage.md"

REQUIRED_SVGS = (
    # ideas suite (SPEC-0149/0152)
    "cli-ideas.svg",
    "cli-next.svg",
    "cli-idea-show.svg",
    "cli-hub.svg",
    "cli-ideas-tree.svg",
    "tui-desk.svg",
    # Wave A/B time + ops (SPEC-0154)
    "cli-nw-dashboard.svg",
    "cli-report-week.svg",
    "cli-topics-unmapped.svg",
    "cli-map.svg",
    "cli-review-week.svg",
    "cli-agenda.svg",
    "cli-tasks.svg",
)

REQUIRED_EXAMPLES = (
    "ideas-demo.org",
    "ideas-archive-demo.org",
    "ideas-20260901.org",
)


def test_required_capture_svgs_exist_and_are_nontrivial():
    missing = [n for n in REQUIRED_SVGS if not (CAPTURES / n).is_file()]
    assert not missing, f"missing captures (run scripts/docs-capture.py): {missing}"
    for name in REQUIRED_SVGS:
        text = (CAPTURES / name).read_text(encoding="utf-8")
        assert "<svg" in text.lower()
        assert len(text) > 500


def test_org_example_fixtures_exist():
    for name in REQUIRED_EXAMPLES:
        path = EXAMPLES / name
        assert path.is_file(), name
        body = path.read_text(encoding="utf-8")
        assert "#+TODO:" in body or "#+TODO:" in body.upper()
        assert ":ID:" in body or ":ID:" in body


def test_org_storage_guide_covers_core_topics():
    text = GUIDE.read_text(encoding="utf-8").lower()
    for needle in (
        "org-mode",
        "summary",
        "open questions",
        "rotation",
        "archive",
        "org_ideas_file",
        "logbook",
    ):
        assert needle in text, needle
    assert "cli-idea-show.svg" in GUIDE.read_text(encoding="utf-8")


def test_docs_capture_script_mentions_wave_ab_outputs():
    script = (ROOT / "scripts" / "docs-capture.py").read_text(encoding="utf-8")
    assert "cli-idea-show.svg" in script
    assert "cli-hub.svg" in script
    assert "ideas-demo.org" in script
    for name in (
        "cli-nw-dashboard.svg",
        "cli-report-week.svg",
        "cli-topics-unmapped.svg",
        "cli-map.svg",
        "cli-review-week.svg",
        "cli-agenda.svg",
        "cli-tasks.svg",
    ):
        assert name in script, name


GUIDES = ROOT / "docs" / "product" / "guides"
HOME = ROOT / "docs" / "product" / "index.md"
CONCEPTS = ROOT / "docs" / "product" / "concepts"


def test_home_embeds_time_and_ideas_svgs():
    text = HOME.read_text(encoding="utf-8")
    assert "cli-nw-dashboard.svg" in text
    assert "cli-ideas.svg" in text
    assert "tui-desk.svg" in text
    assert "guides/daily-ops.md" in text
    assert "guides/weekly-ops.md" in text


def test_getting_started_has_three_forks():
    text = (GUIDES / "getting-started.md").read_text(encoding="utf-8").lower()
    assert "path a" in text and "path b" in text and "path c" in text
    raw = (GUIDES / "getting-started.md").read_text(encoding="utf-8")
    assert "cli-nw-dashboard.svg" in raw
    assert "cli-ideas.svg" in raw
    assert "cli-hub.svg" in raw


def test_daily_and_weekly_ops_guides_embed_wave_ab():
    daily = (GUIDES / "daily-ops.md").read_text(encoding="utf-8")
    weekly = (GUIDES / "weekly-ops.md").read_text(encoding="utf-8")
    for name in ("cli-agenda.svg", "cli-tasks.svg", "cli-nw-dashboard.svg"):
        assert name in daily, name
    for name in ("cli-report-week.svg", "cli-topics-unmapped.svg", "cli-review-week.svg"):
        assert name in weekly, name
    wave = (
        "cli-nw-dashboard.svg", "cli-report-week.svg", "cli-topics-unmapped.svg",
        "cli-map.svg", "cli-review-week.svg", "cli-agenda.svg", "cli-tasks.svg",
    )
    combined = daily + weekly
    assert sum(1 for n in wave if n in combined) >= 3


def test_architecture_and_lifecycle_concept_pages():
    arch = (CONCEPTS / "architecture.md").read_text(encoding="utf-8").lower()
    life = (CONCEPTS / "lifecycle.md").read_text(encoding="utf-8").lower()
    assert "mermaid" in arch and "outbox" in arch and "xdg" in arch
    assert "researched" in life
    assert "shipped" in life and "exported" in life and "promoted" in life
    assert "definition of done" in life or "done matrix" in life
    assert "done" in life and "verify" in life
    idx = (CONCEPTS / "index.md").read_text(encoding="utf-8")
    assert "architecture.md" in idx and "lifecycle.md" in idx


def test_campaign_stamp_docs_state_field_precedence_and_opt_out():
    """SPEC-0431: both product docs describe campaign_from, inference precedence and --no-campaign."""
    surface = " ".join((GUIDES / "agent-surface.md").read_text(encoding="utf-8").split())
    agentic = " ".join(
        (ROOT / "docs" / "product" / "capabilities" / "agentic.md").read_text(encoding="utf-8").split())
    for name, text in (("agent-surface.md", surface), ("agentic.md", agentic)):
        assert "campaign_from" in text, name
        assert "--no-campaign" in text, name
        assert "the one existing campaign" in text, name
        assert "inferred" in text, name
        assert "--re" in text, name
    assert "explicit `--campaign` wins" in surface
    assert "campaign of the `--re` message" in surface
    assert "skips inference" in surface
    assert "without `--campaign`" in agentic
    assert "campaign of the `--re` message" in agentic
    assert "else the one existing campaign id" in agentic
    assert "conflicts with `--campaign`" in agentic
    assert "campaign of the `--event N` queue event" in agentic
    assert "`--no-campaign` skips inference" in agentic


def test_agent_surface_guide_schemas_and_skills():
    text = (GUIDES / "agent-surface.md").read_text(encoding="utf-8")
    for schema in (
        "nw.hub.v1",
        "nw.ideas.v1",
        "nw.next.v1",
        "nw.idea.v1",
        "nw.tasks.v1",
        "nw.spec.verify.v1",
    ):
        assert schema in text, schema
    for skill in (
        "/nw-orient",
        "/nw-explore",
        "/nw-new-work",
        "/nw-generate",
        "/nw-export",
        "/nw-verify",
    ):
        assert skill in text, skill
    idx = (GUIDES / "index.md").read_text(encoding="utf-8")
    assert "agent-surface.md" in idx


def test_wave_c_captures_and_outbound_guide():
    for name in ("cli-projects.svg", "cli-spec-schemes.svg"):
        path = CAPTURES / name
        assert path.is_file(), name
        assert len(path.read_text(encoding="utf-8")) > 500
    outbound = (GUIDES / "outbound.md").read_text(encoding="utf-8")
    assert "pull-status" in outbound
    assert "sweep" in outbound
    assert "SHIPPED" in outbound
    assert "seed-children" in (GUIDES / "multi-project.md").read_text(encoding="utf-8")
    assert "cli-projects.svg" in outbound
    assert "required" in outbound.lower()
