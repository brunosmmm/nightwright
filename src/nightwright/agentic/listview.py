"""Width-adaptive list tables (SPEC-0356): one column fitter shared by `nw campaign list`,
`nw queue list`, `nw queue backlog`, `nw queue unqueued` and (later) the TUI.

The campaign id and the state are `fixed` (natural width, never cut: the id is what a human
copies into the next command, the state answers "what is it doing"); the title is `flex` (takes
what is left, min 12 cells, ellipsized). Every other column has a `rank`; while the table does not
fit, the remaining column(s) with the highest rank number go first (8 = updated/age, 1 =
blocked-by). A column with `fold` set is not lost: its text is prefixed into the title cell, dim.

Documented ranks (`RANK`): 1 blocked-by · 2 park reason · 3 pri (and `resume to`) · 4 cost ·
5 project · 6 loops · 7 size / tone / profile · 8 updated / age."""
from __future__ import annotations

from dataclasses import dataclass

from rich import box
from rich.table import Table
from rich.text import Text

RANK = {"blocked_by": 1, "park": 2, "pri": 3, "cost": 4, "project": 5, "loops": 6,
        "size": 7, "age": 8}
TITLE_MIN = 12
SEP = 3             # SIMPLE_HEAD without edges: pad 1 + divider 1 + pad 1 between columns


@dataclass
class Column:
    key: str
    header: str
    rank: int = 0
    fixed: bool = False
    flex: bool = False
    min_width: int = 0
    fold: str | None = None         # format for the title-cell prefix, e.g. "[{}] "
    justify: str = "left"
    style: str | None = None
    hide_empty: bool = False        # drop the column when no row has a value


def _lines(cell) -> list[str]:
    if cell is None:
        return [""]
    return (cell.plain if isinstance(cell, Text) else str(cell)).split("\n")


def cell_width(cell) -> int:
    return max(Text(line).cell_len for line in _lines(cell))


def natural_width(col: Column, rows: list[dict]) -> int:
    return max([len(col.header), col.min_width] + [cell_width(r.get(col.key)) for r in rows])


def _empty(col: Column, rows: list[dict]) -> bool:
    return all(not "".join(_lines(r.get(col.key))).strip() for r in rows)


def layout(columns: list[Column], rows: list[dict], width: int) -> dict:
    """The fit: `kept` columns in order, `folded` columns (their text goes into the title),
    `widths` per kept key, `narrow` when even id + state + title-min do not fit."""
    cols = [c for c in columns if not (c.hide_empty and _empty(c, rows))]
    nat = {c.key: natural_width(c, rows) for c in cols}
    kept, folded = list(cols), []

    def need(cs):
        rigid = sum(nat[c.key] for c in cs if not c.flex)
        flex = sum(min(TITLE_MIN, nat[c.key]) for c in cs if c.flex)
        return rigid + flex + SEP * (len(cs) - 1)

    while need(kept) > width:
        droppable = [c for c in kept if not c.fixed and not c.flex]
        if not droppable:
            break
        top = max(c.rank for c in droppable)
        for c in [c for c in droppable if c.rank == top]:
            kept.remove(c)
            if c.fold:
                folded.append(c)
    folded.sort(key=cols.index)                     # prefixes in declared order
    widths = {c.key: nat[c.key] for c in kept if not c.flex}
    flex = [c for c in kept if c.flex]
    narrow = need(kept) > width
    avail = width - sum(widths.values()) - SEP * (len(kept) - 1)
    for i, c in enumerate(sorted(flex, key=lambda c: nat[c.key])):
        share = max(TITLE_MIN, avail // (len(flex) - i)) if avail > 0 else TITLE_MIN
        widths[c.key] = max(1, min(nat[c.key] + _fold_len(folded, rows), share))
        avail -= widths[c.key]
    return {"kept": kept, "folded": folded, "widths": widths, "narrow": narrow}


def _fold_len(folded, rows) -> int:
    return max([0] + [sum(len(c.fold.format(_text(r.get(c.key)))) for c in folded
                          if _text(r.get(c.key))) for r in rows])


def fit_columns(columns: list[Column], rows: list[dict], width: int) -> list[Column]:
    """The columns kept at `width`, in their declared order (SPEC-0356 drop order)."""
    return layout(columns, rows, width)["kept"]


def _text(cell) -> str:
    return " ".join(line for line in _lines(cell) if line).strip()


def _as_text(cell) -> Text:
    if isinstance(cell, Text):
        return cell.copy()
    return Text("" if cell is None else str(cell))


def _host(kept: list[Column], widths: dict, keys: tuple) -> str | None:
    """The column the narrow fallback writes the title under."""
    by_key = {c.key: c for c in kept}
    for k in keys:
        if k in by_key:
            return k
    fixed = [c for c in kept if c.fixed] or kept
    return max(fixed, key=lambda c: widths[c.key]).key if fixed else None


def render(title: str | None, columns: list[Column], rows: list[dict], width: int, *,
           title_key: str = "title", title_style: str | None = None,
           title_host: tuple = ("campaign", "id")) -> Table:
    """A rich Table fitted to `width`: dropped columns vanish, folded ones prefix the title cell;
    when even id + state + title-min do not fit, the title moves under the id (folded at its
    width) so id and state stay whole. The host is the first kept `title_host` key, else the
    widest kept fixed column (never a tiny leading one such as the backlog's `#`)."""
    fit = layout(columns, rows, width)
    kept, folded, widths = fit["kept"], fit["folded"], fit["widths"]
    narrow_title = fit["narrow"] and any(c.key == title_key for c in kept)
    if narrow_title:
        kept = [c for c in kept if c.key != title_key]
    table = Table(title=title, box=box.SIMPLE_HEAD, pad_edge=False, show_edge=False,
                  title_style=title_style, title_justify="left")
    for c in kept:
        w = widths[c.key]
        if c.flex:
            table.add_column(c.header, width=w, no_wrap=True, overflow="ellipsis",
                             justify=c.justify, style=c.style)
        else:
            table.add_column(c.header, width=w, min_width=w, no_wrap=True,
                             overflow="fold" if c.fixed else "ellipsis",
                             justify=c.justify, style=c.style)
    id_key = _host(kept, widths, title_host) if narrow_title else None
    for r in rows:
        r = dict(r)
        if folded:
            t = Text()
            for c in folded:
                txt = _text(r.get(c.key))
                if txt:
                    t.append(c.fold.format(txt), style="dim")
            r[title_key] = t + _as_text(r.get(title_key))
        if narrow_title and id_key:
            cell = _as_text(r.get(id_key))
            title = _as_text(r.get(title_key))
            w = widths[id_key]
            for i in range(0, max(1, len(title.plain)), w):
                cell.append("\n")
                cell.append_text(title[i:i + w])
            r[id_key] = cell
        table.add_row(*(_as_text(r.get(c.key)) for c in kept))
    return table
