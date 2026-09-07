"""Tiny debug helpers for the virtual terminal (opt-in file log)."""

from __future__ import annotations

from pathlib import Path

_enabled = False
_log_path: Path | None = None


def init_debug(*, enabled: bool = False, log_path: str | None = None) -> None:
    global _enabled, _log_path
    _enabled = enabled
    _log_path = Path(log_path) if log_path else None
    if _enabled and _log_path is not None:
        _log_path.write_text("", encoding="utf-8")


def is_enabled() -> bool:
    return _enabled


def log(_category: str, message: str) -> None:
    if not _enabled:
        return
    if _log_path is not None:
        with _log_path.open("a", encoding="utf-8") as fp:
            fp.write(message + "\n")


def log_screen_snapshot(_session_id: str, _lines: list[str], *, cursor: tuple[int, int] | None = None) -> None:
    if not _enabled:
        return
    suffix = f" cursor={cursor}" if cursor is not None else ""
    log("screen", f"snapshot{suffix}")

