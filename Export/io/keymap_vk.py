"""VK -> ANSI translation tables."""

from __future__ import annotations

SPECIAL_VK: dict[int, str] = {
    0x26: "\x1b[A",
    0x28: "\x1b[B",
    0x27: "\x1b[C",
    0x25: "\x1b[D",
    0x24: "\x1b[H",
    0x23: "\x1b[F",
    0x21: "\x1b[5~",
    0x22: "\x1b[6~",
    0x2E: "\x1b[3~",
    0x2D: "\x1b[2~",
    0x70: "\x1bOP",
    0x71: "\x1bOQ",
    0x72: "\x1bOR",
    0x73: "\x1bOS",
    0x74: "\x1b[15~",
    0x75: "\x1b[17~",
    0x76: "\x1b[18~",
    0x77: "\x1b[19~",
    0x78: "\x1b[20~",
    0x79: "\x1b[21~",
    0x7A: "\x1b[23~",
    0x7B: "\x1b[24~",
}

MODIFIER_VKS: frozenset[int] = frozenset({
    0x10, 0x11, 0x12, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5,
})

VK_PROCESSKEY = 0xE5

VT100_MAP: dict[str, str] = {
    "up": "\x1b[A",
    "down": "\x1b[B",
    "right": "\x1b[C",
    "left": "\x1b[D",
    "home": "\x1b[H",
    "end": "\x1b[F",
    "insert": "\x1b[2~",
    "delete": "\x1b[3~",
    "pageup": "\x1b[5~",
    "pagedown": "\x1b[6~",
    "enter": "\r",
    "tab": "\t",
    "escape": "\x1b",
    "backspace": "\x7f",
}

