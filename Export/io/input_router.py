"""Pure input routing from console events to PTY write chunks."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from .actions import RoutedInput
from .keymap_vk import MODIFIER_VKS, SPECIAL_VK, VK_PROCESSKEY
from .win32_console import KeyEvent, MOUSE_WHEELED, MouseEvent, is_shift_pressed


@dataclass
class InputState:
    da_buf: str | None = None
    bracketed_paste: bool = False
    input_count: int = 0

    def track_bracketed_paste(self, data: str) -> None:
        if "\x1b[?2004h" in data:
            self.bracketed_paste = True
        if "\x1b[?2004l" in data:
            self.bracketed_paste = False


def route_events(
    events: list[KeyEvent],
    state: InputState,
    *,
    shift_pressed: Callable[[], bool] = is_shift_pressed,
) -> RoutedInput:
    result = RoutedInput()

    paste_text = _detect_paste(events)
    if paste_text is not None:
        paste_text = paste_text.replace("\r\n", "\r").replace("\n", "\r")
        if state.bracketed_paste:
            result.writes.append(f"\x1b[200~{paste_text}\x1b[201~")
        else:
            result.writes.append(paste_text)
        state.input_count += len(events)
        return result

    i = 0
    while i < len(events):
        sgr_count, sgr_mouse = _try_parse_sgr_mouse(events, i)
        if sgr_count:
            state.input_count += sgr_count
            if sgr_mouse is not None:
                result.mouse_events.append(sgr_mouse)
            i += sgr_count
            continue

        ch, vk, ctrl, repeat = events[i]
        state.input_count += 1

        if vk == VK_PROCESSKEY:
            i += 1
            continue
        if ch is None and vk in MODIFIER_VKS:
            i += 1
            continue
        if ch and vk == 0x0000:
            filtered = _da_filter(ch, state.da_buf, state.input_count)
            if filtered is None:
                state.da_buf = None
                result.writes.append(ch)
            elif filtered == "":
                state.da_buf = None
            else:
                state.da_buf = filtered
            i += 1
            continue
        if ch == "\x03":
            result.writes.append("\x03")
            i += 1
            continue
        if ch is None and vk in SPECIAL_VK:
            result.writes.append(SPECIAL_VK[vk])
            i += 1
            continue
        if ch:
            result.writes.append(_translate_char(ch, shift_pressed=shift_pressed))
        i += 1

    return result


_SGR_MOUSE_RE = re.compile(r"\x1b\[<(?P<cb>\d+);(?P<x>\d+);(?P<y>\d+)(?P<final>[Mm])$")
_SGR_MOUSE_PREFIX_RE = re.compile(r"\x1b\[<[\d;]*[Mm]?$")


def _try_parse_sgr_mouse(events: list[KeyEvent], start: int) -> tuple[int, MouseEvent | None]:
    if start + 2 >= len(events):
        return 0, None
    if events[start][0] != "\x1b" or events[start + 1][0] != "[" or events[start + 2][0] != "<":
        return 0, None

    seq = "\x1b[<"
    i = start + 3
    while i < len(events):
        ch = events[i][0]
        if ch is None:
            return 0, None
        seq += ch
        match = _SGR_MOUSE_RE.fullmatch(seq)
        if match:
            return i - start + 1, _decode_sgr_mouse(match)
        if not _SGR_MOUSE_PREFIX_RE.fullmatch(seq):
            return 0, None
        i += 1
    return 0, None


def _decode_sgr_mouse(match: re.Match[str]) -> MouseEvent:
    cb = int(match.group("cb"))
    x = max(0, int(match.group("x")) - 1)
    y = max(0, int(match.group("y")) - 1)
    final = match.group("final")

    if cb & 0x40:
        wheel_up = (cb & 0x01) == 0
        return MouseEvent(x=x, y=y, buttons=0x80000000 if wheel_up else 0, flags=MOUSE_WHEELED)

    if final == "m":
        return MouseEvent(x=x, y=y, buttons=0, flags=0)

    button_map = {0: 0x0001, 1: 0x0002, 2: 0x0004}
    return MouseEvent(x=x, y=y, buttons=button_map.get(cb & 0x03, 0), flags=0)


def _detect_paste(events: list[KeyEvent]) -> str | None:
    text_chars: list[str] = []
    has_newline = False
    has_text = False
    is_pure_text = True

    for ch, vk, ctrl, repeat in events:
        if vk == VK_PROCESSKEY:
            continue
        if ch is None and vk in MODIFIER_VKS:
            continue
        if ch and vk == 0x0000 and ch != "\x1b":
            text_chars.append(ch)
            has_text = True
        elif ch and ch in ("\r", "\n"):
            text_chars.append(ch)
            has_newline = True
        elif ch and ord(ch) >= 0x20:
            text_chars.append(ch)
            has_text = True
        else:
            is_pure_text = False

    if has_newline and has_text and is_pure_text and len(text_chars) > 2:
        return "".join(text_chars)
    return None


def _da_filter(ch: str, da_buf: str | None, _input_count: int) -> str | None:
    if ch == "\x1b" or da_buf is not None:
        if ch == "\x1b":
            da_buf = ch
        else:
            da_buf += ch
        if len(da_buf) >= 3 and da_buf[1] == "[" and "\x40" <= da_buf[-1] <= "\x7e":
            return ""
        if len(da_buf) > 50:
            return ""
        return da_buf
    return None


def _translate_char(ch: str, *, shift_pressed: Callable[[], bool]) -> str:
    if ch in ("\r", "\n"):
        return "\n" if shift_pressed() else "\r"
    if ch == "\x08":
        return "\x7f"
    return ch

