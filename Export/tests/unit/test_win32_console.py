from __future__ import annotations

from Export.io import win32_console as win32


class _FakeKernel32:
    def __init__(self) -> None:
        self.set_calls: list[tuple[int, int]] = []

    def GetStdHandle(self, _which: int) -> int:
        return 123

    def GetConsoleMode(self, _handle: int, out_mode) -> int:
        out_mode._obj.value = 0x01F7
        return 1

    def SetConsoleMode(self, handle: int, mode: int) -> int:
        self.set_calls.append((handle, mode))
        return 1


def test_raw_console_input_enables_extended_flags_and_mouse(monkeypatch):
    fake = _FakeKernel32()
    monkeypatch.setattr(win32, "kernel32", fake)

    with win32.RawConsoleInput() as handle:
        assert handle == 123

    assert fake.set_calls[0] == (
        123,
        win32.ENABLE_EXTENDED_FLAGS | win32.ENABLE_MOUSE_INPUT,
    )
    restored_handle, restored_mode = fake.set_calls[-1]
    assert restored_handle == 123
    assert getattr(restored_mode, "value", restored_mode) == 0x01F7

