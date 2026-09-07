"""Thin loop that connects Win32 console input with a VirtualTerminal."""

from __future__ import annotations

import signal
import sys
import time

from Export.io.input_router import InputState, route_events
from Export.io.win32_console import (
    RawConsoleInput,
    enable_vt,
    enter_alt_screen,
    exit_alt_screen,
    has_events,
    read_batch,
    terminal_size,
)
from Export.vt.virtual_terminal import VirtualTerminal


class ConsoleSession:
    def __init__(
        self,
        vt: VirtualTerminal,
        *,
        use_alt_screen: bool = True,
        passthrough_output: bool = True,
    ) -> None:
        self._vt = vt
        self._use_alt_screen = use_alt_screen
        self._passthrough_output = passthrough_output
        self._input_state = InputState()

    def run(self) -> None:
        enable_vt()
        self._vt.add_raw_output_listener(self._input_state.track_bracketed_paste)
        if self._passthrough_output:
            self._vt.add_raw_output_listener(self._write_stdout)

        rows, cols = terminal_size()
        self._vt.resize(cols, rows)

        if self._use_alt_screen:
            enter_alt_screen()

        self._vt.start()

        prev_handler = signal.getsignal(signal.SIGINT)

        def on_sigint(_s, _f):
            try:
                self._vt.write("\x03")
            except Exception:
                pass

        signal.signal(signal.SIGINT, on_sigint)
        last_size = (rows, cols)

        try:
            with RawConsoleInput() as h_in:
                while self._vt.is_alive():
                    new_size = terminal_size()
                    if new_size != last_size:
                        last_size = new_size
                        r, c = new_size
                        self._vt.resize(c, r)

                    if not has_events(h_in):
                        time.sleep(0.01)
                        continue

                    keys, mice = read_batch(h_in)
                    routed = route_events(keys, self._input_state)
                    for chunk in routed.writes:
                        self._vt.write(chunk)
                    # Mouse events are intentionally ignored in this mini export.
                    _ = mice, routed.mouse_events
        finally:
            signal.signal(signal.SIGINT, prev_handler)
            if self._use_alt_screen:
                exit_alt_screen()
            self._vt.stop()

    @staticmethod
    def _write_stdout(data: str) -> None:
        sys.stdout.write(data)
        sys.stdout.flush()
