"""Shared virtual-terminal data types."""

from __future__ import annotations

from dataclasses import dataclass

from pyte.screens import Char


@dataclass(frozen=True)
class VTSnapshot:
    grid: list[list[Char]]
    cursor_x: int
    cursor_y: int
    cols: int
    rows: int

