"""Console input subsystem."""

from .actions import RoutedInput
from .input_router import InputState, route_events
from .win32_console import KeyEvent, MouseEvent, RawConsoleInput, read_batch

__all__ = [
    "InputState",
    "KeyEvent",
    "MouseEvent",
    "RawConsoleInput",
    "RoutedInput",
    "read_batch",
    "route_events",
]

