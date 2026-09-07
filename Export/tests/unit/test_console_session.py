from __future__ import annotations

from types import SimpleNamespace

from Export.bridge.console_session import ConsoleSession


class _FakeVT:
    def __init__(self) -> None:
        self.listeners = []
        self.events: list[str] = []
        self._alive = False

    def add_raw_output_listener(self, cb) -> None:
        self.listeners.append(cb)

    def resize(self, cols: int, rows: int) -> None:
        self.events.append(f"resize:{cols}x{rows}")

    def start(self) -> None:
        self.events.append("vt.start")
        self._alive = False

    def stop(self) -> None:
        self.events.append("vt.stop")

    def is_alive(self) -> bool:
        return self._alive

    def write(self, _data: str) -> None:
        self.events.append("vt.write")


class _FakeRawConsoleInput:
    def __enter__(self):
        return 1

    def __exit__(self, exc_type, exc, tb):
        return False


def test_bracketed_paste_tracking_is_kept_without_passthrough(monkeypatch):
    vt = _FakeVT()
    session = ConsoleSession(vt, passthrough_output=False, use_alt_screen=False)

    monkeypatch.setattr("Export.bridge.console_session.enable_vt", lambda: None)
    monkeypatch.setattr("Export.bridge.console_session.terminal_size", lambda: (24, 80))
    monkeypatch.setattr("Export.bridge.console_session.RawConsoleInput", _FakeRawConsoleInput)
    monkeypatch.setattr("Export.bridge.console_session.has_events", lambda _h: False)
    monkeypatch.setattr("Export.bridge.console_session.signal.getsignal", lambda _sig: None)
    monkeypatch.setattr("Export.bridge.console_session.signal.signal", lambda *_args: None)

    session.run()

    assert len(vt.listeners) == 1
    vt.listeners[0]("\x1b[?2004h")
    assert session._input_state.bracketed_paste is True


def test_alt_screen_is_entered_before_vt_start(monkeypatch):
    vt = _FakeVT()
    session = ConsoleSession(vt, passthrough_output=False, use_alt_screen=True)
    events: list[str] = []

    monkeypatch.setattr("Export.bridge.console_session.enable_vt", lambda: events.append("enable_vt"))
    monkeypatch.setattr("Export.bridge.console_session.enter_alt_screen", lambda: events.append("enter_alt_screen"))
    monkeypatch.setattr("Export.bridge.console_session.exit_alt_screen", lambda: events.append("exit_alt_screen"))
    monkeypatch.setattr("Export.bridge.console_session.terminal_size", lambda: (24, 80))
    monkeypatch.setattr("Export.bridge.console_session.RawConsoleInput", _FakeRawConsoleInput)
    monkeypatch.setattr("Export.bridge.console_session.has_events", lambda _h: False)
    monkeypatch.setattr("Export.bridge.console_session.signal.getsignal", lambda _sig: None)
    monkeypatch.setattr("Export.bridge.console_session.signal.signal", lambda *_args: None)

    original_start = vt.start

    def start_and_record() -> None:
        events.append("vt.start")
        original_start()

    vt.start = start_and_record

    session.run()

    assert events.index("enter_alt_screen") < events.index("vt.start")
