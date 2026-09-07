"""Abstract PTY backend interface."""

from __future__ import annotations

from abc import ABC, abstractmethod


class PtyBackend(ABC):
    @abstractmethod
    def spawn(
        self,
        cmdline: str,
        cwd: str,
        rows: int,
        cols: int,
        env: dict[str, str] | None = None,
    ) -> None:
        """Start a child process in a PTY."""

    @abstractmethod
    def read(self, size: int = 4096) -> str:
        """Read output from PTY. Raises EOFError on process exit."""

    @abstractmethod
    def write(self, data: str) -> None:
        """Write data to PTY stdin."""

    @abstractmethod
    def set_size(self, rows: int, cols: int) -> None:
        """Resize the PTY."""

    @abstractmethod
    def terminate(self) -> None:
        """Force-kill the child process."""

    @abstractmethod
    def is_alive(self) -> bool:
        """Return True if child process is still running."""

    @property
    @abstractmethod
    def pid(self) -> int | None:
        """Return child PID, or None if not spawned."""

