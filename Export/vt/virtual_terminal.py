"""Generic pyte-backed virtual terminal."""

from __future__ import annotations

import os
import subprocess
import threading
import time
from collections.abc import Callable
from enum import Enum
from pathlib import Path
from types import MappingProxyType

import pyte
from pyte.screens import Char

from Export.debug import is_enabled, log, log_screen_snapshot
from .backend import PtyBackend
from .backend_winpty import WinPtyBackend
from .pyte_patch import PreservingScreen, apply as apply_pyte_patch, filter_private_modes
from .types import VTSnapshot


class VTState(Enum):
    STARTING = "starting"
    RUNNING = "running"
    CLOSED = "closed"


class VirtualTerminal:
    def __init__(
        self,
        session_id: str,
        cmd: list[str],
        workspace: Path,
        env: dict[str, str] | None = None,
        cols: int = 120,
        rows: int = 40,
        backend: PtyBackend | None = None,
    ) -> None:
        apply_pyte_patch()
        self.session_id = session_id
        self.cmd = cmd
        self.workspace = workspace
        self.env = env or {}
        self.state = VTState.CLOSED

        self._backend: PtyBackend = backend or WinPtyBackend()
        self._screen = PreservingScreen(cols, rows, history=5000)
        self._stream = pyte.Stream(self._screen)
        self._lock = threading.Lock()

        self._reader_thread: threading.Thread | None = None
        self._alive = False

        self._dirty_rows: set[int] = set()
        self._on_dirty_listeners: list[Callable[[], None]] = []
        self._dirty_flush_delay_s = 0.003
        self._dirty_flush_lock = threading.Lock()
        self._dirty_flush_timer: threading.Timer | None = None
        self._dirty_flush_pending = False
        self._on_raw_output: list[Callable[[str], None]] = []

    def start(self) -> None:
        cmdline = subprocess.list2cmdline(self.cmd)
        rows, cols = self._screen.lines, self._screen.columns
        cwd = str(self.workspace)
        env_merged = os.environ.copy()
        env_merged.update(self.env)
        self._backend.spawn(cmdline, cwd, rows, cols, env_merged)
        self.state = VTState.STARTING
        self._alive = True
        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader_thread.start()

    spawn = start

    def stop(self) -> None:
        self._alive = False
        if self._backend.is_alive():
            try:
                self._backend.terminate()
            except Exception:
                pass
        if (
            self._reader_thread
            and self._reader_thread.is_alive()
            and self._reader_thread is not threading.current_thread()
        ):
            self._reader_thread.join(timeout=2.0)
        self._cancel_dirty_flush()
        self.state = VTState.CLOSED

    kill = stop

    def is_alive(self) -> bool:
        return self._backend.is_alive()

    def write(self, data: str) -> None:
        if self._backend.is_alive():
            self._backend.write(data)

    write_raw = write

    def resize(self, cols: int, rows: int) -> None:
        try:
            self._backend.set_size(rows, cols)
        except Exception:
            pass
        with self._lock:
            self._screen.resize(rows, cols)

    def add_dirty_listener(self, cb: Callable[[], None]) -> None:
        self._on_dirty_listeners.append(cb)

    def remove_dirty_listener(self, cb: Callable[[], None]) -> None:
        try:
            self._on_dirty_listeners.remove(cb)
        except ValueError:
            pass

    def add_raw_output_listener(self, cb: Callable[[str], None]) -> None:
        self._on_raw_output.append(cb)

    def get_cursor_position(self) -> tuple[int, int]:
        return (self._screen.cursor.x, self._screen.cursor.y)

    def get_screen_snapshot(self, scroll_offset: int = 0) -> VTSnapshot:
        empty = Char(" ", "default", "default", False, False, False, False, False, False)
        with self._lock:
            rows = self._screen.lines
            cols = self._screen.columns
            history_top = list(getattr(getattr(self._screen, "history", None), "top", ()))
            visible_rows = [self._screen.buffer[y] for y in range(rows)]
            viewport_rows, cx, cy = _viewport_rows(
                history_top,
                visible_rows,
                rows,
                self._screen.cursor.x,
                self._screen.cursor.y,
                max(0, scroll_offset),
            )
            grid = [[_row_cell(row, x, empty) for x in range(cols)] for row in viewport_rows]
            return VTSnapshot(grid=grid, cursor_x=cx, cursor_y=cy, cols=cols, rows=rows)

    def get_scrollback_lines(self) -> list[str]:
        with self._lock:
            cols = self._screen.columns
            history_top = list(getattr(getattr(self._screen, "history", None), "top", ()))
            visible_rows = [self._screen.buffer[y] for y in range(self._screen.lines)]
            return [_row_to_text(row, cols) for row in history_top + visible_rows]

    def get_display(self) -> list[str]:
        with self._lock:
            return [self._read_line(y) for y in range(self._screen.lines)]

    def _reader_loop(self) -> None:
        read_count = 0
        try:
            while self._alive:
                if not self._backend.is_alive():
                    break
                try:
                    data = self._backend.read(4096)
                except EOFError:
                    break
                except Exception:
                    if not self._alive:
                        break
                    time.sleep(0.05)
                    continue
                if not data or not self._alive:
                    continue
                read_count += 1

                for cb in self._on_raw_output:
                    try:
                        cb(data)
                    except Exception:
                        pass

                with self._lock:
                    data = filter_private_modes(data)
                    self._stream.feed(data)
                    self._dirty_rows.update(self._screen.dirty)
                    self._screen.dirty.clear()
                    cursor = (self._screen.cursor.x, self._screen.cursor.y)

                if is_enabled():
                    log_screen_snapshot(
                        self.session_id,
                        [self._read_line(y) for y in range(self._screen.lines)],
                        cursor=cursor,
                    )
                self._schedule_dirty_flush()
                if self.state == VTState.STARTING:
                    self.state = VTState.RUNNING
        finally:
            self._flush_dirty_listeners()
            log("pty", f"[{self.session_id}] reader exited after {read_count} reads")
            self._alive = False
            self.state = VTState.CLOSED

    def _schedule_dirty_flush(self) -> None:
        if not self._on_dirty_listeners:
            return
        with self._dirty_flush_lock:
            if self._dirty_flush_pending:
                return
            self._dirty_flush_pending = True
            timer = threading.Timer(self._dirty_flush_delay_s, self._flush_dirty_listeners)
            timer.daemon = True
            self._dirty_flush_timer = timer
            timer.start()

    def _flush_dirty_listeners(self) -> None:
        with self._dirty_flush_lock:
            if not self._dirty_flush_pending:
                return
            self._dirty_flush_pending = False
            self._dirty_flush_timer = None
            self._dirty_rows.clear()
            listeners = list(self._on_dirty_listeners)
        for cb in listeners:
            cb()

    def _cancel_dirty_flush(self) -> None:
        with self._dirty_flush_lock:
            timer = self._dirty_flush_timer
            self._dirty_flush_timer = None
            self._dirty_flush_pending = False
            self._dirty_rows.clear()
        if timer is not None:
            timer.cancel()

    def _read_line(self, y: int) -> str:
        try:
            row = self._screen.buffer[y]
            parts: list[str] = []
            for x in range(self._screen.columns):
                data = row[x].data
                if data == "":
                    continue
                parts.append(data or " ")
            return "".join(parts)
        except (IndexError, KeyError):
            return " " * self._screen.columns


def _row_cell(row, x: int, empty: Char) -> Char:
    try:
        return row[x]
    except (IndexError, KeyError):
        return empty


def _row_to_text(row, cols: int) -> str:
    parts: list[str] = []
    for x in range(cols):
        try:
            data = row[x].data
        except (IndexError, KeyError):
            data = " "
        if data == "":
            continue
        parts.append(data or " ")
    return "".join(parts).rstrip()


_EMPTY_ROW = MappingProxyType({})


def _viewport_rows(
    history_top: list,
    visible_rows: list,
    screen_rows: int,
    cursor_x: int,
    cursor_y: int,
    scroll_offset: int,
) -> tuple[list, int, int]:
    combined = history_top + visible_rows
    if len(combined) < screen_rows:
        combined = ([_EMPTY_ROW] * (screen_rows - len(combined))) + combined

    max_offset = max(0, len(combined) - screen_rows)
    offset = min(max(0, scroll_offset), max_offset)
    start = max(0, len(combined) - screen_rows - offset)
    viewport = combined[start:start + screen_rows]

    cursor_abs = len(history_top) + cursor_y
    cursor_rel = cursor_abs - start
    if 0 <= cursor_rel < screen_rows and offset == 0:
        return viewport, cursor_x, cursor_rel
    return viewport, -1, -1
