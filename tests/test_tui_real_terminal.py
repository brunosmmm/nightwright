"""What a REAL terminal renders (SPEC-0115; SPEC-0490 re-pointed it at the bucket board).

Every desk defect the human reported got past headless pilots. The reason is structural: Textual
repaints *incrementally*, so raw pty bytes cannot be turned back into a screen by picking a frame
— you need a terminal emulator to apply the updates. `pyte` is that emulator.

This is deliberately one slow test rather than a suite: it spawns `nw tui` under a pty against a
throwaway org, and asserts on the composited screen.
"""
import os
import shutil
import sys
import time
from pathlib import Path

import pyte
import pytest


pytestmark = pytest.mark.skipif(not hasattr(os, "forkpty"), reason="needs a POSIX pty")

COLS, ROWS = 160, 30   # SPEC-0490: three panes (rail | board | detail)


def _throwaway_org(tmp_path):
    """A config dir + org file with an epic, two children, a loose idea and a project-less one."""
    conf, org = tmp_path / "config", tmp_path / "org"
    conf.mkdir(), org.mkdir()
    (conf / "config.yaml").write_text(
        f'org_files: ["{org}"]\n'
        f"org_ideas_file: {org}/ideas.org\n"
        f"org_capture_file: {org}/inbox.org\n"
        f"specs_dir: {tmp_path}/specs\n"
        "project_axis: bucket\n")
    (conf / "mappings.yaml").write_text("a:\n  bucket: Alpha\n")
    specs = tmp_path / "specs"
    specs.mkdir()
    # `source_idea` claims each spec for its Alpha idea: without it a bare SPEC-NNNN on a
    # non-internal project's idea is foreign (SPEC-0327) and the board shows no status.
    for num, extra in ((1, "kind: epic\nsource_idea: IDEA-001\n"),
                       (2, "kind: feature\nparent: SPEC-0001\nsource_idea: IDEA-002\n")):
        (specs / f"{num:04d}-x.md").write_text(
            f"---\nid: SPEC-{num:04d}\ntitle: \"s{num}\"\nstatus: accepted\nowner: b\n"
            f"created: 2026-07-31\nupdated: 2026-07-31\n{extra}---\n\n## Context\n\nx\n")
    (org / "ideas.org").write_text(
        "#+TODO: IDEA INCUBATE SPECCED | PROMOTED EXPORTED DROPPED RESEARCHED\n\n"
        # :UPDATED: stamps make the order deterministic. Without them freshness ties and falls
        # back to the heading, so the epic sorted *after* the loose idea and the cursor landed on
        # a leaf — the test then "proved" Enter did not expand.
        "* SPECCED the parent epic idea\n"
        "  :PROPERTIES:\n  :ID: IDEA-001\n  :PROJECT: Alpha\n  :SPEC: SPEC-0001\n"
        "  :UPDATED: [2026-07-31 Fri 12:00]\n  :END:\n"
        "* SPECCED the nested child idea\n"
        "  :PROPERTIES:\n  :ID: IDEA-002\n  :PROJECT: Alpha\n  :SPEC: SPEC-0002\n"
        "  :UPDATED: [2026-07-31 Fri 11:00]\n  :END:\n"
        "* IDEA a loose alpha idea\n"
        "  :PROPERTIES:\n  :ID: IDEA-003\n  :PROJECT: Alpha\n"
        "  :UPDATED: [2026-07-30 Thu 09:00]\n  :END:\n")
    return conf


def left_pane(lines):
    """The board pane (SPEC-0490): each line cut between the rail divider and the detail
    divider, so neither the rail's counts nor the detail pane's colours leak into a check."""
    out = []
    for line in lines:
        first = line.find("│")
        if first < 0:
            out.append(line)
            continue
        second = line.find("│", first + 1)
        out.append(line[first + 1:second] if second >= 0 else line[first + 1:])
    return out


def _loaded(lines):
    """The desk has painted its board (keys typed before that may be lost): the rail lists
    the project and the All view's activity feed shows an idea."""
    return any("Alpha" in line for line in lines) and \
        any("IDEA-00" in line for line in left_pane(lines))


def _render(conf_dir, steps):
    """Spawn `nw tui` under a pty, wait for the desk to load, then for each `(key, ready)` type
    `key` and pump until `ready(lines)` holds (SPEC-0269: a condition, never a fixed settle).
    Returns the composited screen as lines and per-cell colours."""
    import fcntl
    import pty
    import select
    import struct
    import termios

    import waits

    pid, fd = pty.fork()
    if pid == 0:                                            # child
        os.environ.update(TERM="xterm-256color", COLUMNS=str(COLS), LINES=str(ROWS),
                          NW_CONFIG_DIR=str(conf_dir), NW_DATA_DIR=str(conf_dir))
        os.execvp(sys.executable, [sys.executable, "-m", "nightwright.cli", "tui"])
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLS, 0, 0))

    screen = pyte.Screen(COLS, ROWS)
    stream = pyte.ByteStream(screen)
    closed = []

    def read_once(timeout):
        """Feed what the app wrote within `timeout`; False when nothing came."""
        if closed:
            return False
        r, _, _ = select.select([fd], [], [], timeout)
        if not r:
            return False
        try:
            data = os.read(fd, 1 << 20)
        except OSError:
            data = b""
        if not data:
            closed.append(True)
            return False
        stream.feed(data)
        return True

    def lines():
        return [line.rstrip() for line in screen.display]

    def pump_until(ready, what):
        end = time.monotonic() + waits.WAIT_S
        while not ready(lines()):
            if closed or time.monotonic() >= end:
                raise AssertionError(f"the tui never showed {what}:\n" + "\n".join(lines()))
            read_once(0.1)
        while read_once(0.3):           # then let the repaint in progress finish
            pass

    def cells():
        """Per-cell (char, fg) so a test can assert on **colour**, not just text."""
        return [[(screen.buffer[y][x].data, screen.buffer[y][x].fg) for x in range(COLS)]
                for y in range(ROWS)]

    try:
        pump_until(_loaded, "the ideas")
        for key, ready in steps:
            named = {"ENTER": b"\r", "ESC": b"\x1b", "SPACE": b" ", "TAB": b"\t"}
            if len(key) > 1 and key not in named:
                raise AssertionError(f"unknown key name {key!r} — it would be typed literally")
            os.write(fd, named.get(key, key.encode()))
            pump_until(ready, f"the effect of {key!r}")
        return lines(), cells()
    finally:
        try:
            os.write(fd, b"q")
            read_once(0.6)
            os.close(fd)
        except OSError:
            pass
        try:
            os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            pass


def _alpha(lines):
    """The Alpha project view is up: bucket headers in the board pane."""
    board = "\n".join(left_pane(lines))
    return "OPEN" in board and "IN FLIGHT" in board and "IDEA-001" in board


def test_board_renders_buckets_and_spec_status_in_a_real_terminal(tmp_path):
    """The composited screen shows the rail, the buckets, and the spec status + epic roll-up on
    the in-flight row — the three things the old flat table could not show (SPEC-0490)."""
    conf = _throwaway_org(tmp_path)
    if shutil.which("uv") is None and not Path(sys.executable).exists():
        pytest.skip("no interpreter to spawn")

    lines, _cells = _render(conf, [("]", _alpha)])
    screen = "\n".join(lines)
    board = left_pane(lines)
    assert "All" in screen and "Alpha" in screen, f"no project rail:\n{screen}"

    def row_of(needle):
        return next((i for i, line in enumerate(board) if needle in line), None)

    open_at, flight_at, built_at = row_of("OPEN"), row_of("IN FLIGHT"), row_of("BUILT")
    assert None not in (open_at, flight_at, built_at), f"missing a bucket:\n{screen}"
    assert open_at < flight_at < built_at, f"buckets out of order:\n{screen}"
    loose = row_of("IDEA-003")
    epic = row_of("IDEA-001")
    assert open_at < loose < flight_at, f"the loose idea is not OPEN:\n{screen}"
    assert flight_at < epic < built_at, f"the specced epic is not IN FLIGHT:\n{screen}"
    assert "SPEC-0001 accepted" in board[epic], f"no spec status on the row:\n{board[epic]}"
    assert "0/1 built" in board[epic], f"no epic roll-up on the row:\n{board[epic]}"


def test_board_is_not_monochrome(tmp_path):
    """Reported against the old tree: "there is no color at all". The board reuses SPEC-0082's
    state hue and the yellow open-question count; distinct states must get distinct hues.
    """
    conf = _throwaway_org(tmp_path)
    org = conf.parent / "org" / "ideas.org"
    org.write_text(org.read_text() +
                   "* INCUBATE has questions\n  :PROPERTIES:\n  :ID: IDEA-004\n"
                   "  :PROJECT: Alpha\n  :UPDATED: [2026-07-31 Fri 10:00]\n  :END:\n"
                   "** Open questions\n*** OPEN why?\n"
                   "* DROPPED a dead one\n  :PROPERTIES:\n  :ID: IDEA-005\n"
                   "  :PROJECT: Alpha\n  :UPDATED: [2026-07-31 Fri 09:00]\n  :END:\n")

    tree_lines, tree_cells = _render(conf, [
        ("]", _alpha),
        ("d", lambda ls: all(w in "\n".join(left_pane(ls))
                             for w in ("INCUBATE", "IDEA-005", "1q"))),
        # move the cursor off IDEA-004 so its INCUBATE hue is not the cursor highlight
        ("j", lambda ls: any("a loose alpha idea" in line[line.rfind("│"):] for line in ls))])

    def colour_in_row(row_id, word):
        """The fg of `word`'s first character on `row_id`'s line, after the id, searched in
        the **board pane only** (the detail pane is lexed in colour and would mask a bug)."""
        for y, line in enumerate(tree_lines):
            first = line.find("│")
            second = line.find("│", first + 1)
            if first < 0 or second < 0:
                continue
            start = line.find(row_id, first + 1, second)
            if start < 0:
                continue
            at = line.find(word, start + len(row_id), second)
            return tree_cells[y][at][1] if at >= 0 else None
        return None

    # Compare NON-cursor rows only: the cursor highlight would make any state look distinct.
    # IDEA-004 (INCUBATE, one open question, so it sorts first) and IDEA-005 (DROPPED) are not
    # under the cursor once `j` moved it to IDEA-003.
    incubate_row = next(line for line in tree_lines if "IDEA-004" in line)
    assert "1q" in incubate_row
    incubate = colour_in_row("IDEA-004", "INCUBATE")
    dropped = colour_in_row("IDEA-005", "DROPPED")
    heading = colour_in_row("IDEA-004", "has questions")
    q_colour = colour_in_row("IDEA-004", "1q")
    assert incubate and dropped and heading and q_colour
    assert incubate != dropped, f"states share one colour: {incubate}"
    assert incubate != heading, f"state is not coloured: {incubate} == body {heading}"
    assert q_colour != heading, f"question count not coloured: {q_colour}"
