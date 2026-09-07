"""Manual PTY integration smoke test for Export.VirtualTerminal."""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from Export.vt.virtual_terminal import VirtualTerminal


def wait_for_output(vt: VirtualTerminal, needle: str, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if any(needle in line for line in vt.get_display()):
            return True
        time.sleep(0.1)
    return False


def main() -> int:
    vt = VirtualTerminal("io_test", ["powershell.exe"], Path.cwd(), cols=100, rows=30)
    vt.start()
    try:
        time.sleep(1.0)
        vt.write("echo export_io_test\r")
        ok = wait_for_output(vt, "export_io_test", timeout=15.0)
        print("PASS" if ok else "FAIL")
        return 0 if ok else 1
    finally:
        vt.stop()


if __name__ == "__main__":
    raise SystemExit(main())

