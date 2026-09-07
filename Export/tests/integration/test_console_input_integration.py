"""Manual console input roundtrip via WriteConsoleInputW."""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from Export.io.win32_console import INPUT_RECORD, KEY_EVENT, kernel32, read_batch


def _write_events(h_in: int, events: list[tuple[str | None, int, int, int]]) -> int:
    records = (INPUT_RECORD * len(events))()
    for i, (ch, vk, ctrl, repeat) in enumerate(events):
        records[i].EventType = KEY_EVENT
        ke = records[i].Event.KeyEvent
        ke.bKeyDown = True
        ke.wRepeatCount = repeat
        ke.wVirtualKeyCode = vk
        ke.wVirtualScanCode = 0
        ke.uChar = ch if ch else "\x00"
        ke.dwControlKeyState = ctrl
    written = wt.DWORD()
    kernel32.WriteConsoleInputW(h_in, records, len(events), ctypes.byref(written))
    return written.value


def main() -> int:
    conin = kernel32.CreateFileW(
        "CONIN$",
        0x80000000 | 0x40000000,
        0x01 | 0x02,
        None,
        3,
        0,
        None,
    )
    if conin == -1:
        print("SKIPPED: no console available")
        return 0

    kernel32.FlushConsoleInputBuffer(conin)
    _write_events(conin, [("a", 0x41, 0, 1), ("한", 0x0000, 0, 1)])
    events, _ = read_batch(conin)
    chars = [ch for ch, vk, ctrl, repeat in events if ch]
    kernel32.CloseHandle(conin)
    ok = chars == ["a", "한"]
    print("PASS" if ok else f"FAIL: {chars!r}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
