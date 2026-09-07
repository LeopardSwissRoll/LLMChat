"""한글 PTY 출력 테스트 — Claude 없이 PTY 한글 처리 디버깅.

Usage:
    python test_hangul.py          # 자동 테스트
    python test_hangul.py --shell  # cmd.exe를 PTY로 열어서 수동 테스트
"""
from __future__ import annotations

import os
import sys
import threading
import time

def _enable_vt():
    if os.name != "nt":
        return
    import ctypes
    k32 = ctypes.windll.kernel32
    h = k32.GetStdHandle(-11)
    mode = ctypes.c_ulong()
    k32.GetConsoleMode(h, ctypes.byref(mode))
    k32.SetConsoleMode(h, mode.value | 0x0004)


def test_basic_stdout():
    """1. Python stdout 한글 테스트 (PTY 없이)"""
    print("=" * 50)
    print("TEST 1: Python stdout 직접 출력")
    print("  한글 테스트: 안녕하세요 세계")
    print("  혼합: Hello 안녕 World 세계 123")
    print(f"  stdout.encoding = {sys.stdout.encoding}")
    print(f"  console code page = {os.popen('chcp').read().strip()}")
    print()


def test_pty_echo():
    """2. PTY를 통한 한글 echo 테스트"""
    from winpty import PtyProcess

    print("=" * 50)
    print("TEST 2: PTY echo 테스트")

    # cmd /c echo 로 한글 출력
    proc = PtyProcess.spawn(
        'cmd /c echo 안녕하세요 & echo Hello세계 & echo 混合テスト',
        cwd=".",
        dimensions=(10, 80),
    )

    time.sleep(1)
    output = ""
    while True:
        try:
            data = proc.read(4096)
            if data:
                output += data
            else:
                break
        except EOFError:
            break

    print(f"  Raw output repr: {output!r}")
    print(f"  Rendered:")
    for line in output.split("\n"):
        if line.strip():
            print(f"    {line.rstrip()}")
    print()


def test_pty_python_print():
    """3. PTY 안에서 Python print 한글 테스트"""
    from winpty import PtyProcess

    print("=" * 50)
    print("TEST 3: PTY → Python print 한글")

    cmd = 'python -c "import sys; print(sys.stdout.encoding); print(\'한글 출력 테스트: 안녕하세요\')"'
    proc = PtyProcess.spawn(cmd, cwd=".", dimensions=(10, 120))

    time.sleep(2)
    output = ""
    while True:
        try:
            data = proc.read(4096)
            if data:
                output += data
            else:
                break
        except EOFError:
            break

    print(f"  Raw repr: {output!r}")
    print(f"  Rendered:")
    for line in output.split("\n"):
        if line.strip():
            print(f"    {line.rstrip()}")
    print()


def test_pty_passthrough_encoding():
    """4. PTY read의 인코딩 확인"""
    from winpty import PtyProcess

    print("=" * 50)
    print("TEST 4: PTY read 타입/인코딩 확인")

    proc = PtyProcess.spawn(
        'cmd /c echo 테스트',
        cwd=".",
        dimensions=(5, 80),
    )

    time.sleep(0.5)
    try:
        data = proc.read(4096)
        print(f"  type(data) = {type(data)}")
        print(f"  len(data)  = {len(data)}")
        print(f"  repr(data) = {data!r}")

        if isinstance(data, bytes):
            print(f"  decode utf-8: {data.decode('utf-8', errors='replace')}")
            print(f"  decode cp949: {data.decode('cp949', errors='replace')}")
        else:
            print(f"  (already str, encoding handled by pywinpty)")
            # Check for mojibake patterns
            try:
                roundtrip = data.encode('utf-8').decode('utf-8')
                print(f"  UTF-8 roundtrip OK: {roundtrip}")
            except Exception as e:
                print(f"  UTF-8 roundtrip FAIL: {e}")
    except EOFError:
        print("  (EOF)")
    print()


def test_stdout_write_timing():
    """5. stdout.write + flush 타이밍 테스트"""
    print("=" * 50)
    print("TEST 5: stdout.write 한글 타이밍")
    print("  한 글자씩:")
    for ch in "안녕하세요":
        sys.stdout.write(ch)
        sys.stdout.flush()
        time.sleep(0.1)
    print()
    print("  완료")
    print()


def test_pty_live_passthrough():
    """6. PTY live passthrough — reader thread 방식 (fakeTerm.py와 동일)"""
    from winpty import PtyProcess

    print("=" * 50)
    print("TEST 6: PTY live passthrough (reader thread)")
    print("  cmd /c 로 한글 echo 후 reader thread가 stdout에 출력")

    proc = PtyProcess.spawn(
        'cmd /c "echo 라이브패스스루테스트 & echo 두번째줄한글 & echo Third_Line_영어"',
        cwd=".",
        dimensions=(10, 120),
    )

    collected = []
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            try:
                data = proc.read(4096)
                if data:
                    sys.stdout.write(data)
                    sys.stdout.flush()
                    collected.append(data)
            except EOFError:
                break
        stop.set()

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    time.sleep(2)
    stop.set()
    t.join(timeout=1)

    print(f"\n  Collected {len(collected)} chunks, total {sum(len(c) for c in collected)} chars")
    for i, chunk in enumerate(collected):
        print(f"    chunk[{i}] ({len(chunk)} chars): {chunk!r}")
    print()


def run_shell():
    """수동 테스트: cmd.exe를 PTY passthrough로 열기"""
    import msvcrt
    from winpty import PtyProcess

    _enable_vt()
    print("PTY Shell (cmd.exe) — 한글 입출력 테스트")
    print("  'echo 안녕하세요' 등을 직접 입력해 보세요")
    print("  Ctrl+C 두 번 = 종료")
    print()

    rows, cols = 30, 120
    try:
        c, r = os.get_terminal_size()
        rows, cols = max(r, 10), max(c, 40)
    except OSError:
        pass

    proc = PtyProcess.spawn("cmd", cwd=".", dimensions=(rows, cols))
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            try:
                data = proc.read(4096)
                if data:
                    sys.stdout.write(data)
                    sys.stdout.flush()
            except EOFError:
                break
        stop.set()

    t = threading.Thread(target=reader, daemon=True)
    t.start()

    import signal
    last_sigint = [0.0]
    def on_sigint(_s, _f):
        now = time.monotonic()
        if now - last_sigint[0] < 1.0:
            stop.set()
            return
        last_sigint[0] = now
        proc.write("\x03")
    signal.signal(signal.SIGINT, on_sigint)

    time.sleep(0.5)
    # drain
    while msvcrt.kbhit():
        msvcrt.getwch()

    try:
        while not stop.is_set() and proc.isalive():
            if not msvcrt.kbhit():
                time.sleep(0.01)
                continue
            ch = msvcrt.getwch()
            if ch in ("\r", "\n"):
                proc.write("\r")
            elif ch in ("\x00", "\xe0"):
                msvcrt.getwch()  # skip special key
            else:
                proc.write(ch)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--shell", action="store_true", help="수동 cmd.exe 셸 테스트")
    args = p.parse_args()

    if args.shell:
        run_shell()
    else:
        _enable_vt()
        test_basic_stdout()
        test_pty_echo()
        test_pty_python_print()
        test_pty_passthrough_encoding()
        test_stdout_write_timing()
        test_pty_live_passthrough()
        print("=" * 50)
        print("모든 테스트 완료")
