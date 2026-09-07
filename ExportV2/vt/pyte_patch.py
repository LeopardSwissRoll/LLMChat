"""pyte patches used by the exported virtual terminal."""

from __future__ import annotations

import re

import pyte


def apply() -> None:
    import pyte.graphics as g

    for table in (g.FG_ANSI, g.FG):
        if 33 in table:
            table[33] = "yellow"
    for table in (g.BG_ANSI, g.BG):
        if 43 in table:
            table[43] = "yellow"

    bright_fg = {
        90: "bright_black",
        91: "bright_red",
        92: "bright_green",
        93: "bright_yellow",
        94: "bright_blue",
        95: "bright_magenta",
        96: "bright_cyan",
        97: "bright_white",
    }
    bright_bg = {k + 10: v for k, v in bright_fg.items()}

    g.FG_AIXTERM.update(bright_fg)
    g.BG_AIXTERM.update(bright_bg)
    if hasattr(g, "FG"):
        g.FG.update(bright_fg)
    if hasattr(g, "BG"):
        g.BG.update(bright_bg)


_PRIVATE_CSI_RE = re.compile(r"\x1b\[[><=!][0-9;]*[A-Za-z~]")


def filter_private_modes(data: str) -> str:
    return _PRIVATE_CSI_RE.sub("", data)


class PreservingScreen(pyte.HistoryScreen):
    """HistoryScreen variant that preserves content on vertical resize."""

    def resize(self, lines: int | None = None, columns: int | None = None) -> None:
        lines = lines or self.lines
        columns = columns or self.columns
        if lines == self.lines and columns == self.columns:
            return

        old_lines = self.lines
        if lines < old_lines:
            self._resize_shrink(lines, columns, old_lines)
        elif lines > old_lines:
            self._resize_expand(lines, columns, old_lines)
        else:
            if columns < self.columns:
                for line in self.buffer.values():
                    for x in range(columns, self.columns):
                        line.pop(x, None)
            self.columns = columns

        self.cursor.y = min(self.cursor.y, lines - 1)
        self.cursor.x = min(self.cursor.x, columns - 1)
        self.dirty.update(range(lines))

    def _resize_shrink(self, lines: int, columns: int, old_lines: int) -> None:
        cursor_y = self.cursor.y
        window_end = min(max(cursor_y, lines - 1), old_lines - 1)
        window_start = max(window_end - lines + 1, 0)
        window_end = window_start + lines - 1

        for y in range(window_start):
            if y in self.buffer:
                self.history.top.append(self.buffer[y])

        new_buf = type(self.buffer)(self.buffer.default_factory)
        for y in range(lines):
            src = window_start + y
            if src in self.buffer:
                new_buf[y] = self.buffer[src]

        if columns < self.columns:
            for line in new_buf.values():
                for x in range(columns, self.columns):
                    line.pop(x, None)

        self.buffer = new_buf
        self.lines = lines
        self.columns = columns
        self.set_margins()
        self.cursor.y = cursor_y - window_start

    def _resize_expand(self, lines: int, columns: int, old_lines: int) -> None:
        pyte.Screen.resize(self, lines, columns)

        if self.history.top:
            n_restore = min(lines - old_lines, len(self.history.top))
            if n_restore > 0:
                for y in range(lines - 1, n_restore - 1, -1):
                    src = y - n_restore
                    if src in self.buffer:
                        self.buffer[y] = self.buffer.pop(src)
                    else:
                        self.buffer.pop(y, None)
                for y in range(n_restore - 1, -1, -1):
                    self.buffer[y] = self.history.top.pop()
                self.cursor.y = min(self.cursor.y + n_restore, lines - 1)

