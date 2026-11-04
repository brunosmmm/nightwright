"""Campaign detail screen (SPEC-0246): header, filterable ledger timeline, detail pane.

SPEC-0433: the timeline opens as the campaign *story* (`agentic.timeline`): a tree of loops,
phase groups, collapsed lines (one per run, gate, review round, rebase, park ...) and their
details. Enter or space expands a node; `r` toggles the raw SPEC-0246 event table, which is
unchanged. `f` filters both views by the same groups.

Read-only like the queue tab: nothing here syncs an event cursor or takes a reader lock; a
`stat()` signature poll catches ledger and state writes. Writes happen only through the shared
`QueueActions` flow after a confirm.
"""
import asyncio

from rich.markup import escape
from rich.text import Text

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import DataTable, Static, Tree

from .. import report as R
from . import agentic_model as AM
from .queue_tab import QueueActions

POLL_S = 2.0
_FIXED_COST = 30       # seq + time + type columns and padding
FILTERS = ("all",) + AM.GROUPS
GROUP_STYLE = {"transitions": "cyan", "runs": "", "gates": "magenta", "findings": "yellow",
               "notes": "dim"}


class CampaignScreen(QueueActions, Screen):
    DEFAULT_CSS = """
    CampaignScreen { layout: vertical; }
    #campaign-header { height: auto; padding: 0 1; background: $boost; }
    #campaign-timeline { height: 1fr; }
    #campaign-story { height: 1fr; }
    #campaign-status { height: 1; color: $text-muted; padding: 0 1; }
    #campaign-detail { height: 40%; border-top: solid $primary; padding: 0 1; }
    """

    BINDINGS = [
        Binding("f", "cycle_filter", "filter"),
        Binding("v", "toggle_review", "review"),
        Binding("r", "toggle_raw", "raw"),
        Binding("g", "first", "first", show=False),
        Binding("G", "last", "last", show=False),
        Binding("j", "down", "down", show=False),
        Binding("k", "up", "up", show=False),
        Binding("escape", "close", "back"),
        Binding("q", "close", "back", show=False),
        Binding("p", "act('park')", "park"),
        Binding("u", "act('resume')", "resume"),
        Binding("P", "act('prioritize')", "priority"),
        Binding("A", "act('abandon')", "abandon"),
    ]

    def __init__(self, cfg, campaign_id: str, **kw):
        super().__init__(**kw)
        self.cfg = cfg
        self.campaign_id = campaign_id
        self.detail: AM.CampaignDetail | None = None
        self.filter = "all"
        self.review_mode = False
        self.shown: list[AM.TimelineEntry] = []
        self.raw_mode = False                   # SPEC-0433: the story tree is the default
        self.expanded: set[str] | None = None   # story node ids open (None: first paint)
        self.story_nodes: dict = {}             # node id -> TreeNode, rebuilt by each paint
        self._story_cursor: str | None = None
        self._story_last: str | None = None
        self._signature = None
        self._loading = False
        self._first_paint = True

    def compose(self) -> ComposeResult:
        yield Static("", id="campaign-header")
        story = Tree("story", id="campaign-story")
        story.show_root = False
        story.guide_depth = 2
        yield story
        yield DataTable(id="campaign-timeline", cursor_type="row", zebra_stripes=False)
        yield Static("", id="campaign-status")
        with VerticalScroll(id="campaign-detail"):
            yield Static("", id="campaign-detail-text")

    @property
    def table(self) -> DataTable:
        return self.query_one("#campaign-timeline", DataTable)

    @property
    def tree(self) -> Tree:
        return self.query_one("#campaign-story", Tree)

    def on_mount(self) -> None:
        self.table.add_columns("seq", "time", "type", "text")
        self._apply_view()
        self.set_interval(POLL_S, self._poll)
        self.reload()

    def on_resize(self, event) -> None:
        if self.detail:
            self._safe_paint()

    def _safe_paint(self) -> None:
        try:
            self.paint()
        except Exception as e:
            self._show_error(e)

    def _show_error(self, e: Exception) -> None:
        self.query_one("#campaign-status", Static).update(
            f"[red]campaign failed: {escape(str(e) or repr(e))}[/]")

    # --- loading -------------------------------------------------------------------------

    def _poll(self) -> None:
        try:
            changed = AM.campaign_signature(self.cfg, self.campaign_id) != self._signature
        except Exception:
            changed = True                  # reload() reports the error in the status line
        if changed:
            self.reload()

    def reload(self) -> None:
        if self._loading:
            return
        self._loading = True
        self.run_worker(self._reload_async(), name="campaign-reload", group="campaign-reload")

    async def _reload_async(self) -> None:
        try:
            detail = await asyncio.to_thread(AM.load_campaign, self.cfg, self.campaign_id)
            self._signature = detail.signature
            previous = self._cursor_target()
            self.detail = detail
            self.paint(previous)
        except Exception as e:      # any failure shows in the status line; the next poll retries
            self._signature = None  # so a failed paint does not look up to date
            self._show_error(e)
        finally:
            self._loading = False

    # --- painting ------------------------------------------------------------------------

    def _cursor_target(self):
        """`(seq, on_last_row)` of the row under the cursor, against the rows shown now."""
        i = self.table.cursor_row
        if not self.shown or not 0 <= i < len(self.shown):
            return (None, True)
        return (self.shown[i].seq, i == len(self.shown) - 1)

    def paint(self, previous=None) -> None:
        detail = self.detail
        if detail is None:
            return
        table = self.table
        seq, on_last = previous or self._cursor_target()
        self.query_one("#campaign-header", Static).update(Text("\n").join(detail.header))
        self.shown = [e for e in detail.entries
                        if self.filter == "all" or e.group == self.filter]
        text_w = max(20, (table.size.width or 100) - _FIXED_COST)
        table.clear()
        for e in self.shown:
            table.add_row(Text(str(e.seq)),
                          Text(e.ts[11:19] if len(e.ts) >= 19 else e.ts),
                          Text(e.type, style=GROUP_STYLE.get(e.group, "")),
                          Text(R._clip(e.text.replace("\n", " "), text_w)), key=str(e.seq))
        if self.shown:
            table.move_cursor(row=self._restore_index(seq, on_last), scroll=True)
        self.paint_story()
        self._paint_status()
        self._first_paint = False
        self.show_detail()

    def _paint_status(self) -> None:
        detail = self.detail
        if self.raw_mode:
            text = f"filter {self.filter} · {len(self.shown)}/{len(detail.entries)} events"
        else:
            story = detail.story or {}
            text = (f"story · filter {self.filter} · {story.get('lines', 0)} lines from "
                    f"{story.get('events', 0)} events · enter/space expands · r raw")
        self.query_one("#campaign-status", Static).update(Text(text, style="dim"))

    # --- the SPEC-0433 story tree -----------------------------------------------------------

    def _keep(self, line) -> bool:
        return self.filter == "all" or line.get("category") == self.filter

    def paint_story(self) -> None:
        tree = self.tree
        story = (self.detail.story if self.detail else None) or {}
        loops = story.get("loops") or []
        first = self.expanded is None
        if first:                                   # newest loop and its newest group open
            self.expanded = set()
            if loops:
                self.expanded.add(loops[-1]["id"])
                groups = loops[-1].get("groups") or []
                if groups:
                    self.expanded.add(groups[-1]["id"])
                with_lines = [g for g in groups if g.get("lines")]
                if with_lines:                      # the newest group that says something
                    self.expanded.add(with_lines[-1]["id"])
        cursor, follow = self._story_cursor, self._story_cursor in (None, self._story_last)
        tree.clear()
        nodes: dict = {}
        last_line = None
        for lp in loops:
            groups = [(g, [ln for ln in g.get("lines") or [] if self._keep(ln)])
                      for g in lp.get("groups") or []]
            groups = [(g, lines) for g, lines in groups if lines or self.filter == "all"]
            if not groups:
                continue
            lnode = tree.root.add(Text(lp.get("header") or "", style="bold"),
                                  data=("loop", lp, lp["id"]), expand=lp["id"] in self.expanded)
            nodes[lp["id"]] = lnode
            for g, lines in groups:
                gnode = lnode.add(Text(g.get("header") or "", style="bold cyan"),
                                  data=("group", g, g["id"]), expand=g["id"] in self.expanded)
                nodes[g["id"]] = gnode
                quiet = [ln for ln in lines if ln.get("quiet")]
                for ln in lines:
                    if ln.get("quiet"):
                        continue
                    last_line = self._add_line(gnode, ln, nodes) or last_line
                if quiet:
                    qid = f"q{g['id']}"
                    qnode = gnode.add(Text(f"details ({len(quiet)})", style="dim"),
                                      data=("details", {"text": f"{len(quiet)} quiet details",
                                                        "children": [], "seqs": []}, qid),
                                      expand=qid in self.expanded)
                    nodes[qid] = qnode
                    for ln in quiet:
                        self._add_line(qnode, ln, nodes, dim=True)
        self.story_nodes = nodes
        self._story_last = last_line
        target = last_line if (first or follow) else cursor
        node = nodes.get(target) if target else None
        if node is None and last_line:
            node = nodes.get(last_line)
        if node is not None:
            parent = node.parent
            while parent is not None and not parent.is_root:   # make it visible
                parent.expand()
                self.expanded.add(parent.data[2])
                parent = parent.parent
            self._story_cursor = node.data[2]
            # a fresh node has no line number until the tree lays its lines out
            self.call_after_refresh(self._move_story_cursor, node.data[2])

    def _move_story_cursor(self, nid) -> None:
        node = self.story_nodes.get(nid)
        if node is not None:
            self.tree.move_cursor(node)
            self.tree.scroll_to_node(node, animate=False)

    def _add_line(self, parent, ln, nodes, dim=False):
        style = "dim" if dim else AM.STORY_TONE.get(ln.get("tone"), "")
        kids = ln.get("children") or []
        label = Text(ln.get("text") or "", style=style)
        if kids:
            label.append(f"  [+{len(kids)}]", style="dim")
            node = parent.add(label, data=("line", ln, ln["id"]), expand=ln["id"] in self.expanded)
            for i, c in enumerate(kids):
                cid = f"{ln['id']}.{i}"
                nodes[cid] = node.add_leaf(Text(c.get("text") or "",
                                                style=AM.STORY_TONE.get(c.get("tone"), "")),
                                           data=("child", c, cid))
        else:
            node = parent.add_leaf(label, data=("line", ln, ln["id"]))
        nodes[ln["id"]] = node
        return ln["id"]

    def on_tree_node_expanded(self, event: Tree.NodeExpanded) -> None:
        if event.node.data and self.expanded is not None:
            self.expanded.add(event.node.data[2])

    def on_tree_node_collapsed(self, event: Tree.NodeCollapsed) -> None:
        if event.node.data and self.expanded is not None:
            self.expanded.discard(event.node.data[2])

    def on_tree_node_highlighted(self, event: Tree.NodeHighlighted) -> None:
        if event.node.data:
            self._story_cursor = event.node.data[2]
        if not self.raw_mode:
            self.show_detail()

    def _apply_view(self) -> None:
        self.tree.display = not self.raw_mode
        self.table.display = self.raw_mode
        (self.table if self.raw_mode else self.tree).focus()

    def _restore_index(self, seq, on_last) -> int:
        if self._first_paint or on_last or seq is None:
            return len(self.shown) - 1
        for i, e in enumerate(self.shown):
            if e.seq == seq:
                return i
        return len(self.shown) - 1

    def current_entry(self) -> AM.TimelineEntry | None:
        i = self.table.cursor_row
        return self.shown[i] if self.shown and 0 <= i < len(self.shown) else None

    def show_detail(self) -> None:
        pane = self.query_one("#campaign-detail-text", Static)
        if self.detail is None:
            return
        if self.review_mode:
            pane.update(self.detail.review)
            return
        try:
            if not self.raw_mode:
                node = self.tree.cursor_node
                pane.update(AM.story_detail(self.cfg, self.campaign_id, self.detail,
                                            node.data[:2])
                            if node is not None and node.data else Text("no events", style="dim"))
                return
            entry = self.current_entry()
            pane.update(AM.entry_detail(self.cfg, self.campaign_id, entry)
                        if entry else Text("no events", style="dim"))
        except Exception as e:
            pane.update(Text(f"(detail unavailable: {e})", style="red"))

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id == "campaign-timeline":
            self.show_detail()

    # --- actions -------------------------------------------------------------------------

    def action_cycle_filter(self) -> None:
        self.filter = FILTERS[(FILTERS.index(self.filter) + 1) % len(FILTERS)]
        self._safe_paint()

    def action_toggle_review(self) -> None:
        self.review_mode = not self.review_mode
        self.show_detail()

    def action_toggle_raw(self) -> None:
        self.raw_mode = not self.raw_mode
        self._apply_view()
        if self.detail is not None:
            self._paint_status()
        self.show_detail()

    def action_first(self) -> None:
        if not self.raw_mode:
            self.tree.cursor_line = 0
            return
        if self.shown:
            self.table.move_cursor(row=0, scroll=True)

    def action_last(self) -> None:
        if not self.raw_mode:
            self.tree.cursor_line = max(0, self.tree.last_line)
            return
        if self.shown:
            self.table.move_cursor(row=len(self.shown) - 1, scroll=True)

    def action_down(self) -> None:
        (self.table if self.raw_mode else self.tree).action_cursor_down()

    def action_up(self) -> None:
        (self.table if self.raw_mode else self.tree).action_cursor_up()

    def action_close(self) -> None:
        self.app.pop_screen()

    def action_act(self, action: str) -> None:
        if self.detail is None:
            return
        self.run_queue_action(action, self.campaign_id, self.detail.state.get("state"),
                              self.detail.actions)
