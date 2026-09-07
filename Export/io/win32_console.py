"""Windows console input backend based on ReadConsoleInputW."""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import sys
from typing import NamedTuple

from Export.debug import log

STD_INPUT_HANDLE = -10
STD_OUTPUT_HANDLE = -11
KEY_EVENT = 0x0001
MOUSE_EVENT = 0x0002
ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
ENABLE_MOUSE_INPUT = 0x0010
ENABLE_EXTENDED_FLAGS = 0x0080

kernel32 = ctypes.windll.kernel32
user32 = ctypes.windll.user32


class KEY_EVENT_RECORD(ctypes.Structure):
    _fields_ = [
        ("bKeyDown", wt.BOOL),
        ("wRepeatCount", wt.WORD),
        ("wVirtualKeyCode", wt.WORD),
        ("wVirtualScanCode", wt.WORD),
        ("uChar", wt.WCHAR),
        ("dwControlKeyState", wt.DWORD),
    ]


class COORD(ctypes.Structure):
    _fields_ = [("X", wt.SHORT), ("Y", wt.SHORT)]


class MOUSE_EVENT_RECORD(ctypes.Structure):
    _fields_ = [
        ("dwMousePosition", COORD),
        ("dwButtonState", wt.DWORD),
        ("dwControlKeyState", wt.DWORD),
        ("dwEventFlags", wt.DWORD),
    ]


MOUSE_WHEELED = 0x0004
MOUSE_HWHEELED = 0x0008


class INPUT_RECORD_UNION(ctypes.Union):
    _fields_ = [("KeyEvent", KEY_EVENT_RECORD), ("MouseEvent", MOUSE_EVENT_RECORD)]


class INPUT_RECORD(ctypes.Structure):
    _fields_ = [("EventType", wt.WORD), ("Event", INPUT_RECORD_UNION)]


class KeyEvent(NamedTuple):
    char: str | None
    vk: int
    ctrl: int
    repeat: int


class MouseEvent(NamedTuple):
    x: int
    y: int
    buttons: int
    flags: int


def enable_vt() -> None:
    h_out = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
    out_mode = ctypes.c_ulong()
    kernel32.GetConsoleMode(h_out, ctypes.byref(out_mode))
    kernel32.SetConsoleMode(h_out, out_mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING)


def enter_alt_screen() -> None:
    sys.stdout.write("\x1b[?1049h\x1b[H\x1b[2J\x1b[?1000h\x1b[?1006h")
    sys.stdout.flush()


def exit_alt_screen() -> None:
    sys.stdout.write("\x1b[?1006l\x1b[?1000l\x1b[?1049l")
    sys.stdout.flush()


def terminal_size() -> tuple[int, int]:
    try:
        cols, rows = os.get_terminal_size()
        return max(rows, 10), max(cols, 40)
    except OSError:
        return 30, 120


def is_shift_pressed() -> bool:
    return bool(user32.GetAsyncKeyState(0x10) & 0x8000)


class RawConsoleInput:
    def __init__(self) -> None:
        self.h_in = kernel32.GetStdHandle(STD_INPUT_HANDLE)
        self._old_mode = wt.DWORD()

    def __enter__(self) -> int:
        kernel32.GetConsoleMode(self.h_in, ctypes.byref(self._old_mode))
        new_mode = ENABLE_EXTENDED_FLAGS | ENABLE_MOUSE_INPUT
        kernel32.SetConsoleMode(self.h_in, new_mode)
        log("input", f"raw console mode set old=0x{self._old_mode.value:04x} new=0x{new_mode:04x}")
        return self.h_in

    def __exit__(self, *exc) -> None:
        kernel32.SetConsoleMode(self.h_in, self._old_mode)


def read_one_record(h_in: int) -> KeyEvent | MouseEvent | None:
    record = INPUT_RECORD()
    read_count = wt.DWORD()
    kernel32.ReadConsoleInputW(h_in, ctypes.byref(record), 1, ctypes.byref(read_count))

    if record.EventType == MOUSE_EVENT:
        me = record.Event.MouseEvent
        return MouseEvent(me.dwMousePosition.X, me.dwMousePosition.Y, me.dwButtonState, me.dwEventFlags)

    if record.EventType != KEY_EVENT:
        return None
    ke = record.Event.KeyEvent
    if not ke.bKeyDown:
        return None
    ch = ke.uChar
    return KeyEvent(ch if ch and ch != "\x00" else None, ke.wVirtualKeyCode, ke.dwControlKeyState, ke.wRepeatCount)


def read_key_blocking(h_in: int) -> KeyEvent:
    while True:
        result = read_one_record(h_in)
        if result is None or isinstance(result, MouseEvent):
            continue
        return result


def has_events(h_in: int) -> bool:
    avail = wt.DWORD()
    kernel32.GetNumberOfConsoleInputEvents(h_in, ctypes.byref(avail))
    return avail.value > 0


def read_batch(h_in: int, limit: int = 4096) -> tuple[list[KeyEvent], list[MouseEvent]]:
    keys: list[KeyEvent] = []
    mice: list[MouseEvent] = []
    while has_events(h_in):
        result = read_one_record(h_in)
        if result is None:
            continue
        if isinstance(result, MouseEvent):
            mice.append(result)
        else:
            keys.append(result)
        if len(keys) + len(mice) >= limit:
            break
    return keys, mice

