"""pywinpty-backed PTY backend."""

from __future__ import annotations

from winpty import PtyProcess

from Export.debug import log
from .backend import PtyBackend


class WinPtyBackend(PtyBackend):
    def __init__(self) -> None:
        self._proc: PtyProcess | None = None

    def spawn(
        self,
        cmdline: str,
        cwd: str,
        rows: int,
        cols: int,
        env: dict[str, str] | None = None,
    ) -> None:
        log("pty", f"WinPtyBackend.spawn: {cmdline!r} cwd={cwd} dims=({rows},{cols})")
        self._proc = PtyProcess.spawn(
            cmdline,
            cwd=cwd,
            dimensions=(rows, cols),
            env=env,
        )

    def read(self, size: int = 4096) -> str:
        if not self._proc:
            raise EOFError("PTY not spawned")
        return self._proc.read(size)

    def write(self, data: str) -> None:
        if self._proc and self._proc.isalive():
            self._proc.write(data)

    def set_size(self, rows: int, cols: int) -> None:
        if self._proc and self._proc.isalive():
            self._proc.setwinsize(rows, cols)

    def terminate(self) -> None:
        if self._proc and self._proc.isalive():
            self._proc.terminate()

    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.isalive()

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc else None

