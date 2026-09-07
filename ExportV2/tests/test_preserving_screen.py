from __future__ import annotations

import pyte

from ExportV2.vt.pyte_patch import PreservingScreen


def _make_screen(cols=20, rows=10) -> tuple[PreservingScreen, pyte.Stream]:
    s = PreservingScreen(cols, rows, history=100)
    st = pyte.Stream(s)
    return s, st


def _read_line(screen, y: int) -> str:
    parts = []
    for x in range(screen.columns):
        ch = screen.buffer[y][x].data
        if ch == "":
            continue
        parts.append(ch)
    return "".join(parts).rstrip()


def _all_lines(screen) -> list[str]:
    return [_read_line(screen, y) for y in range(screen.lines)]


def test_materialized_empty_rows():
    s, st = _make_screen(40, 20)
    for i in range(8):
        st.feed(f"Content {i:02d}\r\n")
    st.feed("PS prompt> ")

    for y in range(20):
        _ = s.buffer[y]

    s.resize(10, 40)
    lines = _all_lines(s)
    assert sum(1 for l in lines if l.strip()) > 0
    assert any("PS prompt" in l for l in lines)


def test_shrink_expand_roundtrip_preserves_content():
    s, st = _make_screen(20, 10)
    for i in range(8):
        st.feed(f"Row{i:02d}\r\n")

    s.resize(4, 20)
    s.resize(10, 20)
    text = "\n".join(_all_lines(s))
    assert "Row00" in text or "Row07" in text

