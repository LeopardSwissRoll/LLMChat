"""Pure routed input results."""

from __future__ import annotations

from dataclasses import dataclass, field

from .win32_console import MouseEvent


@dataclass
class RoutedInput:
    writes: list[str] = field(default_factory=list)
    mouse_events: list[MouseEvent] = field(default_factory=list)

