from __future__ import annotations

from Export.io.input_router import InputState, _da_filter, _detect_paste, route_events
from Export.io.keymap_vk import VK_PROCESSKEY
from Export.io.win32_console import MOUSE_WHEELED


def test_da_filter_korean_passthrough():
    assert _da_filter("한", None, 1) is None


def test_detect_paste_text_with_newline():
    events = [
        ("h", 0x48, 0, 1),
        ("e", 0x45, 0, 1),
        ("l", 0x4C, 0, 1),
        ("l", 0x4C, 0, 1),
        ("o", 0x4F, 0, 1),
        ("\r", 0x0D, 0, 1),
        ("w", 0x57, 0, 1),
    ]
    assert _detect_paste(events) is not None


def test_route_events_handles_korean_ime_and_special_keys():
    state = InputState()
    result = route_events(
        [
            ("한", 0x0000, 0, 1),
            (None, 0x26, 0, 1),
            ("\x08", 0x08, 0, 1),
        ],
        state,
        shift_pressed=lambda: False,
    )
    assert result.writes == ["한", "\x1b[A", "\x7f"]


def test_route_events_skips_vk_processkey():
    state = InputState()
    result = route_events([(None, VK_PROCESSKEY, 0, 1)], state)
    assert result.writes == []


def test_route_events_parses_sgr_mouse_wheel():
    state = InputState()
    result = route_events(
        [
            ("\x1b", 0x1B, 0, 1),
            ("[", ord("["), 0, 1),
            ("<", ord("<"), 0, 1),
            ("6", ord("6"), 0, 1),
            ("4", ord("4"), 0, 1),
            (";", ord(";"), 0, 1),
            ("1", ord("1"), 0, 1),
            ("0", ord("0"), 0, 1),
            (";", ord(";"), 0, 1),
            ("5", ord("5"), 0, 1),
            ("M", ord("M"), 0, 1),
        ],
        state,
    )
    assert result.writes == []
    assert len(result.mouse_events) == 1
    assert result.mouse_events[0].flags == MOUSE_WHEELED

