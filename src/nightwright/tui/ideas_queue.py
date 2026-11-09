"""Ideas board ↔ agentic-queue campaigns (SPEC-0494). No Textual import (same rule as
`ideas_board.py`), so the matching rules are testable on a base install.

A board item is linked to every campaign — live or terminal (MERGED / HANDED_OFF / ABANDONED) —
that names its idea or its spec:

- **idea**: the campaign's `idea.id` / `idea_id`, else an `IDEA-N` prefix of the campaign id
  (`IDEA-1041-20261109-c0c6`). Idea ids are compared by number (`IDEA-587` ≡ `IDEA-0587`):
  the org mixes zero-paddings. Idea ids are global, so no project check.
- **spec**: the campaign's `spec_id` / `spec.id` equals the idea's `:SPEC:` or the live
  successor it resolves to. Spec ids are per project (SPEC-0327), so the key is
  `(project, spec id)`, a project-less idea or campaign counting as the internal project.

The index is built once per board load from `store.list_campaigns(include_terminal=True)`
(mtime-cached `state.json` reads) and lookups are dict hits.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_IDEA_RE = re.compile(r"^IDEA-(\d+)", re.IGNORECASE)
SHOWN = 6                         # detail-pane lines before "+k more"


def idea_key(text) -> str:
    """`IDEA-0587`, `IDEA-587`, `IDEA-587-20261106-47f4` → `IDEA-587`; else ``."""
    m = _IDEA_RE.match(str(text or "").strip())
    return f"IDEA-{int(m.group(1))}" if m else ""


@dataclass(frozen=True)
class CampaignRef:
    """The few `state.json` facts the board shows about one campaign."""
    id: str
    state: str
    kind: str = ""
    spec_id: str = ""
    idea_id: str = ""
    project: str = ""
    created_at: str = ""
    updated_at: str = ""

    @property
    def terminal(self) -> bool:
        from ..agentic.store import TERMINAL_STATES
        return self.state in TERMINAL_STATES

    @property
    def day(self) -> str:
        """Close day for a terminal campaign (its last state write), else its open day."""
        stamp = self.updated_at if self.terminal and self.updated_at else self.created_at
        return (stamp or "")[:10]

    @property
    def status(self) -> str:
        return (self.state or "?").lower().replace("_", "-")

    @property
    def label(self) -> str:
        return f"{self.id}  {self.status}  {self.day}".rstrip()


def _ref(st: dict) -> CampaignRef:
    from ..rules import INTERNAL_PROJECT

    idea = st.get("idea") if isinstance(st.get("idea"), dict) else {}
    spec = st.get("spec") if isinstance(st.get("spec"), dict) else {}
    cid = str(st.get("id") or "")
    return CampaignRef(
        id=cid, state=str(st.get("state") or ""), kind=str(st.get("kind") or ""),
        spec_id=str(st.get("spec_id") or spec.get("id") or ""),
        idea_id=idea_key(idea.get("id") or st.get("idea_id") or cid),
        project=str(st.get("project") or INTERNAL_PROJECT),
        created_at=str(st.get("created_at") or ""), updated_at=str(st.get("updated_at") or ""))


@dataclass
class CampaignIndex:
    by_idea: dict[str, list[CampaignRef]] = field(default_factory=dict)
    by_spec: dict[tuple[str, str], list[CampaignRef]] = field(default_factory=dict)

    @classmethod
    def from_states(cls, states) -> "CampaignIndex":
        index = cls()
        for st in states:
            if not isinstance(st, dict) or not st.get("id"):
                continue
            ref = _ref(st)
            if ref.idea_id:
                index.by_idea.setdefault(ref.idea_id, []).append(ref)
            if ref.spec_id:
                index.by_spec.setdefault((ref.project, ref.spec_id), []).append(ref)
        return index

    def for_item(self, item) -> list[CampaignRef]:
        """Every campaign for a `BoardItem`, newest first (by open time), each once."""
        from ..rules import INTERNAL_PROJECT

        project = (getattr(item.row, "project", "") or "").strip() or INTERNAL_PROJECT
        found = {r.id: r for r in self.by_idea.get(idea_key(item.id), ())}
        link = item.link
        specs = set()
        if link is not None:
            specs.add(link.id)
            if link.via:
                specs.add(link.via)
        for sid in specs:
            for r in self.by_spec.get((project, sid), ()):
                found.setdefault(r.id, r)
        return sorted(found.values(), key=lambda r: (r.created_at, r.id), reverse=True)


def load_campaign_index(cfg) -> CampaignIndex:
    """All campaigns, terminal ones included. A missing / unreadable store reads as empty."""
    from ..agentic import store

    try:
        states = store.list_campaigns(cfg, include_terminal=True)
    except (OSError, ValueError, KeyError):
        states = []
    return CampaignIndex.from_states(states)
