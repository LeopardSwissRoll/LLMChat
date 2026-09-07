"""Windows IME 입력 테스트 — ReadConsoleInputW vs msvcrt.getwch()

msvcrt.getwch()는 IME 조합 완료 전까지 블로킹.
ReadConsoleInputW는 개별 KEY_EVENT를 줌.
어떤 게 한글 조합 중에도 반응하는지 테스트.
"""
import ctypes
import ctypes.wintypes as wt
import sys
import msvcrt
import time

kernel32 = ctypes.windll.kernel32

# Console input structures
class KEY_EVENT_RECORD(ctypes.Structure):
    _fields_ = [
        ("bKeyDown", wt.BOOL),
        ("wRepeatCount", wt.WORD),
        ("wVirtualKeyCode", wt.WORD),
        ("wVirtualScanCode", wt.WORD),
        ("uChar", wt.WCHAR),  # UnicodeChar
        ("dwControlKeyState", wt.DWORD),
    ]

class INPUT_RECORD_UNION(ctypes.Union):
    _fields_ = [
        ("KeyEvent", KEY_EVENT_RECORD),
        # other event types exist but we only care about key
    ]

class INPUT_RECORD(ctypes.Structure):
    _fields_ = [
        ("EventType", wt.WORD),
        ("Event", INPUT_RECORD_UNION),
    ]

KEY_EVENT = 0x0001
STD_INPUT_HANDLE = -10


def test_read_console_input():
    """ReadConsoleInputW로 키 이벤트 직접 읽기"""
    print("=== ReadConsoleInputW 테스트 ===")
    print("한글을 입력해 보세요. 각 키 이벤트가 표시됩니다.")
    print("Ctrl+C = 종료")
    print()

    h_in = kernel32.GetStdHandle(STD_INPUT_HANDLE)

    # Enable raw input mode
    old_mode = wt.DWORD()
    kernel32.GetConsoleMode(h_in, ctypes.byref(old_mode))
    # Disable ENABLE_PROCESSED_INPUT (1) and ENABLE_LINE_INPUT (2) and ENABLE_ECHO_INPUT (4)
    kernel32.SetConsoleMode(h_in, 0)

    try:
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

            if vk == 0x03:  # Ctrl+C
                print("\n종료")
                break

            if ch:
                print(
                    f"  KEY_DOWN: char={ch!r} ord={ord(ch)} "
                    f"vk=0x{vk:04X} scan=0x{ke.wVirtualScanCode:04X} "
                    f"ctrl=0x{ke.dwControlKeyState:08X}"
                )
            else:
                print(
                    f"  KEY_DOWN: (no char) "
                    f"vk=0x{vk:04X} scan=0x{ke.wVirtualScanCode:04X}"
                )

    finally:
        kernel32.SetConsoleMode(h_in, old_mode)


def test_getwch():
    """msvcrt.getwch() 비교 테스트"""
    print("=== msvcrt.getwch() 테스트 ===")
    print("한글을 입력해 보세요. getwch()가 반환하는 시점을 표시합니다.")
    print("Ctrl+C = 종료")
    print()

    try:
        while True:
            ch = msvcrt.getwch()
            now = time.monotonic()
            if ch == "\x03":
                print("\n종료")
                break
            print(f"  getwch: {ch!r} ord={ord(ch)} time={now:.3f}")
    except KeyboardInterrupt:
        print("\n종료")


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--mode", choices=["raw", "getwch", "both"], default="both")
    args = p.parse_args()

    if args.mode in ("raw", "both"):
        test_read_console_input()
    if args.mode in ("getwch", "both"):
        test_getwch()
