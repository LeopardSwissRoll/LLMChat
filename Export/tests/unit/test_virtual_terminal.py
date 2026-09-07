from __future__ import annotations

from pathlib import Path
import threading
import time

from pyte.screens import Char

from Export.vt.virtual_terminal import VirtualTerminal
from Export.vt.virtual_terminal import VTState


def _vt(cols: int = 10, rows: int = 3) -> VirtualTerminal:
    return VirtualTerminal("s", ["cmd"], Path("."), cols=cols, rows=rows)


def _feed(vt: VirtualTerminal, text: str) -> None:
    with vt._lock:
        vt._stream.feed(text)


class _FakeBackend:
    def __init__(self) -> None:
        self.pid = 1

    def spawn(self, *_args, **_kwargs) -> None:
        return None

    def is_alive(self) -> bool:
        return False

    def write(self, _data: str) -> None:
        return None

    def terminate(self) -> None:
        return None

    def set_size(self, _rows: int, _cols: int) -> None:
        return None

    def read(self, _size: int) -> str:
        raise EOFError


def test_get_scrollback_lines_combines_history_and_visible_rows():
    vt = _vt(rows=3)
    _feed(vt, "L1\r\nL2\r\nL3\r\nL4")
    assert vt.get_scrollback_lines() == ["L1", "L2", "L3", "L4"]


def test_get_scrollback_lines_handles_sparse_rows():
    vt = _vt(cols=8, rows=2)
    with vt._lock:
        row = vt._screen.buffer[0]
        row[3] = Char("x", "default", "default", False, False, False, False, False, False)
    assert vt.get_scrollback_lines()[0] == "   x"


def test_get_scrollback_lines_skips_cjk_stub_cells():
    vt = _vt(cols=8, rows=2)
    _feed(vt, "한글")
    assert vt.get_scrollback_lines()[0] == "한글"


def test_dirty_notifications_are_coalesced_within_debounce_window():
    vt = _vt()
    vt._dirty_flush_delay_s = 0.01
    called = threading.Event()
    count = 0
    count_lock = threading.Lock()

    def listener() -> None:
        nonlocal count
        with count_lock:
            count += 1
        called.set()

    vt.add_dirty_listener(listener)
    vt._dirty_rows.add(1)
    vt._schedule_dirty_flush()
    time.sleep(0.002)
    vt._dirty_rows.add(2)
    vt._schedule_dirty_flush()
    assert called.wait(0.2)
    assert count == 1


def test_dirty_notifications_fire_again_after_debounce_window():
    vt = _vt()
    vt._dirty_flush_delay_s = 0.005
    first = threading.Event()
    second = threading.Event()
    count = 0
    count_lock = threading.Lock()

    def listener() -> None:
        nonlocal count
        with count_lock:
            count += 1
            if count == 1:
                first.set()
            elif count == 2:
                second.set()

    vt.add_dirty_listener(listener)
    vt._dirty_rows.add(1)
    vt._schedule_dirty_flush()
    assert first.wait(0.2)
    vt._dirty_rows.add(3)
    vt._schedule_dirty_flush()
    assert second.wait(0.2)
    assert count == 2


def test_start_sets_starting_before_reader_thread_starts(monkeypatch):
    vt = VirtualTerminal("s", ["cmd"], Path("."), backend=_FakeBackend())
    seen: list[VTState] = []

    class FakeThread:
        def __init__(self, *, target, daemon):
            self.target = target
            self.daemon = daemon

        def start(self) -> None:
            seen.append(vt.state)

        def is_alive(self) -> bool:
            return False

    monkeypatch.setattr("Export.vt.virtual_terminal.threading.Thread", FakeThread)

    vt.start()

    assert seen == [VTState.STARTING]
