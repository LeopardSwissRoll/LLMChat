"""fakeTerm — Minimal PTY virtual terminal passthrough.

Spawns a CLI process inside a PTY and bridges it to the current terminal.
User sees and interacts with the process as if running it directly.

Uses ReadConsoleInputW instead of msvcrt.getwch() for proper Korean IME support.

Usage:
    python fakeTerm.py                          # default: claude --verbose
    python fakeTerm.py "codex --no-alt-screen"  # custom command
    python fakeTerm.py "python"                 # any CLI
    python fakeTerm.py "claude" --cwd ~/myproject

Detach: Ctrl+C twice within 1 second.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import signal
import sys
import threading
import time

# ── Windows Console API ──

kernel32 = ctypes.windll.kernel32

STD_INPUT_HANDLE = -10
STD_OUTPUT_HANDLE = -11
KEY_EVENT = 0x0001
ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004


class KEY_EVENT_RECORD(ctypes.Structure):
    _fields_ = [
        ("bKeyDown", wt.BOOL),
        ("wRepeatCount", wt.WORD),
        ("wVirtualKeyCode", wt.WORD),
        ("wVirtualScanCode", wt.WORD),
        ("uChar", wt.WCHAR),
        ("dwControlKeyState", wt.DWORD),
    ]

class INPUT_RECORD_UNION(ctypes.Union):
    _fields_ = [("KeyEvent", KEY_EVENT_RECORD)]

class INPUT_RECORD(ctypes.Structure):
    _fields_ = [
        ("EventType", wt.WORD),
        ("Event", INPUT_RECORD_UNION),
    ]


def _enable_vt():
    """Enable ANSI escape processing on Windows console."""
    h = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
    mode = ctypes.c_ulong()
    kernel32.GetConsoleMode(h, ctypes.byref(mode))
    kernel32.SetConsoleMode(h, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING)


# ── Windows special key → ANSI escape sequence ──
_SPECIAL_VK = {
    0x26: "\x1b[A",   # VK_UP
    0x28: "\x1b[B",   # VK_DOWN
    0x27: "\x1b[C",   # VK_RIGHT
    0x25: "\x1b[D",   # VK_LEFT
    0x24: "\x1b[H",   # VK_HOME
    0x23: "\x1b[F",   # VK_END
    0x21: "\x1b[5~",  # VK_PRIOR (Page Up)
    0x22: "\x1b[6~",  # VK_NEXT (Page Down)
    0x2E: "\x1b[3~",  # VK_DELETE
    0x2D: "\x1b[2~",  # VK_INSERT
}


def _terminal_size() -> tuple[int, int]:
    try:
        cols, rows = os.get_terminal_size()
        return max(rows, 10), max(cols, 40)
    except OSError:
        return 30, 120


def _read_console_input(h_in: int) -> tuple[str | None, int]:
    """Read one key event via ReadConsoleInputW.

    Returns (char_or_none, virtual_key_code).
    For IME-composed Korean: char='한', vk=0x0000
    For regular keys:        char='a',  vk=0x41
    For special keys:        char=None, vk=0x26 (VK_UP)
    """
    record = INPUT_RECORD()
    read_count = wt.DWORD()

    while True:
        kernel32.ReadConsoleInputW(
            h_in,
            ctypes.byref(record),
            1,
            ctypes.byref(read_count),
        )
        if record.EventType != KEY_EVENT:
            continue
        ke = record.Event.KeyEvent
        if not ke.bKeyDown:
            continue

        ch = ke.uChar
        vk = ke.wVirtualKeyCode

        if ch:
            return ch, vk
        else:
            return None, vk


def run(command: str, cwd: str | None = None) -> None:
    from winpty import PtyProcess

    _enable_vt()
    rows, cols = _terminal_size()
    proc = PtyProcess.spawn(command, cwd=cwd or ".", dimensions=(rows, cols))

    stop = threading.Event()
    last_size = (rows, cols)

    # ── Ctrl+C handling ──
    last_sigint = [0.0]
    prev_handler = signal.getsignal(signal.SIGINT)

    def on_sigint(_s, _f):
        now = time.monotonic()
        if now - last_sigint[0] < 1.0:
            stop.set()
            return
        last_sigint[0] = now
        try:
            proc.write("\x03")
        except Exception:
            pass

    signal.signal(signal.SIGINT, on_sigint)

    # ── Reader thread: PTY → stdout ──
    def reader():
        nonlocal last_size
        while not stop.is_set():
            try:
                data = proc.read(4096)
                if data:
                    sys.stdout.write(data)
                    sys.stdout.flush()
                new = _terminal_size()
                if new != last_size:
                    last_size = new
                    try:
                        proc.setwinsize(*new)
                    except Exception:
                        pass
            except EOFError:
                break
            except Exception:
                if not stop.is_set():
                    time.sleep(0.05)
        stop.set()

    t = threading.Thread(target=reader, daemon=True)
    t.start()

    # ── Drain DA/xterm.js response garbage ──
    h_in = kernel32.GetStdHandle(STD_INPUT_HANDLE)
    old_mode = wt.DWORD()
    kernel32.GetConsoleMode(h_in, ctypes.byref(old_mode))
    # Disable line input + echo (raw mode) but keep processed input for Ctrl+C
    kernel32.SetConsoleMode(h_in, 0)

    drain_end = time.monotonic() + 4.0
    while time.monotonic() < drain_end:
        # Peek to avoid blocking
        avail = wt.DWORD()
        kernel32.GetNumberOfConsoleInputEvents(h_in, ctypes.byref(avail))
        if avail.value > 0:
            _read_console_input(h_in)  # discard
        else:
            time.sleep(0.02)

    # ── stdin → PTY (ReadConsoleInputW for IME support) ──
    #
    # IME flow:
    #   조합 중: char='g' vk=0x47 (영문 키코드) → IME가 처리 중
    #   확정:    char='한' vk=0x00 (완성된 한글) → PTY에 전달
    #
    # 영문/특수키는 vk로 구분하여 정상 전달.
    # IME 조합 중 영문 키코드(vk != 0, but IME active)는
    # 한글 확정 이벤트와 함께 오므로 둘 다 처리해야 함.
    #
    # 실제 동작: IME ON일 때
    #   ㅎ(g) → ㅏ(k) → ㄴ(s) 입력 시
    #   ReadConsoleInputW는 g, k, s 이벤트를 줌 (vk=영문)
    #   Space 누르면 '한' (vk=0x00) + ' ' (vk=0x20) 이벤트를 줌
    #   → vk=0x00인 한글만 PTY에 전달하면 됨
    #   → 단, 영문 모드에서는 g, k, s를 PTY에 전달해야 함
    #
    # 구분법: IME 확정 글자는 vk=0x0000 + scan=0x0000.
    #         영문 키는 vk != 0.
    #         IME 조합 중 영문 키코드는... 구분이 어려움.
    #
    # 실전 해결: VK_PROCESSKEY(0xE5)를 체크.
    #   IME가 키를 가로챘으면 vk=0xE5로 옴.
    #   이건 무시하고, 뒤따라 오는 vk=0x00 한글만 전달.

    VK_PROCESSKEY = 0xE5

    try:
        while not stop.is_set() and proc.isalive():
            # Peek first to avoid blocking (allows stop check)
            avail = wt.DWORD()
            kernel32.GetNumberOfConsoleInputEvents(h_in, ctypes.byref(avail))
            if avail.value == 0:
                time.sleep(0.01)
                continue

            ch, vk = _read_console_input(h_in)

            # Ctrl+C (already handled by signal, but just in case)
            if ch == "\x03":
                now = time.monotonic()
                if now - last_sigint[0] < 1.0:
                    break
                last_sigint[0] = now
                proc.write("\x03")
                continue

            # IME processed key — skip (한글 확정 이벤트가 뒤따름)
            if vk == VK_PROCESSKEY:
                continue

            # IME confirmed Korean character (vk=0x0000)
            if ch and vk == 0x0000:
                proc.write(ch)
                continue

            # Special keys (arrows, home, end, etc.)
            if ch is None and vk in _SPECIAL_VK:
                proc.write(_SPECIAL_VK[vk])
                continue

            # Regular character (English, numbers, punctuation)
            if ch:
                if ch in ("\r", "\n"):
                    proc.write("\r")
                elif ch == "\t":
                    proc.write("\t")
                elif ch == "\x1b":
                    proc.write("\x1b")
                else:
                    proc.write(ch)

    except KeyboardInterrupt:
        pass
    finally:
        # Restore console mode
        kernel32.SetConsoleMode(h_in, old_mode)
        signal.signal(signal.SIGINT, prev_handler)
        stop.set()
        if proc.isalive():
            proc.terminate()


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Minimal PTY terminal passthrough")
    p.add_argument("command", nargs="?", default="claude --verbose",
                   help="Command to run (default: claude --verbose)")
    p.add_argument("--cwd", default=".", help="Working directory")
    args = p.parse_args()
    run(args.command, args.cwd)
