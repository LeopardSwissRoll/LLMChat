from __future__ import annotations

import asyncio
import enum
import logging
import os
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any

import pyte
from winpty import PtyProcess

LOGGER = logging.getLogger(__name__)


class PtyState(enum.Enum):
    STARTING = "starting"
    READY = "ready"
    BUSY = "busy"
    DEAD = "dead"


class SessionInterrupted(Exception):
    """Raised when wait_response is interrupted via interrupt()."""


class PtySession:
    """Manages a persistent provider CLI process via PTY + pyte virtual terminal.

    Ready detection (startup): provider-specific predicate.
    Response completion: idle detection — no new PTY data for `idle_seconds`.
    TUI prompt markers differ by provider, so response completion still uses
    idle detection instead of prompt reappearance.
    """

    IDLE_SECONDS = 3.0  # seconds of silence → response complete

    def __init__(
        self,
        command: str,
        cwd: str,
        rows: int = 50,
        cols: int = 200,
        loop: asyncio.AbstractEventLoop | None = None,
        ready_detector: Callable[[list[str]], bool] | None = None,
        raw_ready_detector: Callable[[str], bool] | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self.command = command
        self.cwd = cwd
        self.rows = rows
        self.cols = cols
        self.loop = loop or asyncio.get_event_loop()
        self._ready_detector = ready_detector or self._default_ready_detector
        self._raw_ready_detector = raw_ready_detector
        self.env = env or {}

        self.state = PtyState.DEAD
        self.proc: PtyProcess | None = None
        self.screen: pyte.HistoryScreen | None = None
        self.stream: pyte.Stream | None = None

        self._lock = threading.Lock()
        self._ready_event = asyncio.Event()
        self._stop_flag = threading.Event()
        self._reader_thread: threading.Thread | None = None
        self._data_listeners: list[Callable[[str], None]] = []
        self._raw_buffer: deque[str] = deque(maxlen=200)

        # Idle-based response detection
        self._last_data_time: float = 0.0
        self._activity_event = asyncio.Event()  # set when first data after send
        self._interrupted = asyncio.Event()
        self._last_steer_time: float = 0.0  # prevent double Ctrl+C

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def spawn(self) -> None:
        LOGGER.info(
            "Spawning PTY: %s (cwd=%s, %dx%d)",
            self.command, self.cwd, self.rows, self.cols,
        )

        self.screen = pyte.HistoryScreen(self.cols, self.rows, history=10000)
        self.stream = pyte.Stream(self.screen)

        self.proc = PtyProcess.spawn(
            self.command,
            cwd=self.cwd,
            env={**os.environ, **self.env},
            dimensions=(self.rows, self.cols),
        )

        self.state = PtyState.STARTING
        self._ready_event.clear()
        self._activity_event.clear()
        self._interrupted.clear()
        self._stop_flag.clear()
        self._last_data_time = time.monotonic()

        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader_thread.start()

    async def wait_ready(self, timeout: float = 30.0) -> None:
        deadline = time.monotonic() + timeout
        while True:
            if self.state == PtyState.READY:
                break
            if self._ready_event.is_set():
                break
            if self.state == PtyState.DEAD:
                raise RuntimeError("PTY died during startup")
            if self.proc and not self.proc.isalive():
                self.state = PtyState.DEAD
                self._ready_event.set()
                raise RuntimeError("PTY died during startup")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"PTY did not become ready within {timeout}s")
            await asyncio.sleep(min(0.2, remaining))
        if self.state == PtyState.DEAD:
            raise RuntimeError("PTY died during startup")
        LOGGER.info("PTY ready")

    def mark_ready(self) -> None:
        """Promote the session to READY and wake startup waiters."""
        if self.state == PtyState.DEAD:
            return
        self.state = PtyState.READY
        self._ready_event.set()

    def stop(self) -> None:
        LOGGER.info("Stopping PTY session")
        self._stop_flag.set()

        if self.proc and self.proc.isalive():
            try:
                self.proc.write("/exit\r")
                time.sleep(1)
            except Exception:
                pass
            if self.proc.isalive():
                self.proc.terminate()

        self.state = PtyState.DEAD
        # Unblock any waiters
        self._ready_event.set()
        self._activity_event.set()
        self._interrupted.set()

    async def restart(self) -> None:
        LOGGER.info("Restarting PTY session")
        self.stop()
        await asyncio.sleep(1)
        self.spawn()
        await self.wait_ready()

    # ------------------------------------------------------------------
    # Messaging
    # ------------------------------------------------------------------

    def write_raw(self, data: str) -> None:
        """Write raw data to the PTY without changing session state."""
        if not self.proc or self.state == PtyState.DEAD:
            raise RuntimeError("PTY not available")
        self.proc.write(data)
        LOGGER.debug("Wrote raw data (%d bytes)", len(data))

    def add_data_listener(self, cb: Callable[[str], None]) -> None:
        """Register a non-blocking callback for raw PTY output chunks."""
        with self._lock:
            if cb not in self._data_listeners:
                self._data_listeners.append(cb)

    def remove_data_listener(self, cb: Callable[[str], None]) -> None:
        """Remove a previously registered PTY output callback."""
        with self._lock:
            try:
                self._data_listeners.remove(cb)
            except ValueError:
                pass

    def get_recent_raw_chunks(self) -> list[str]:
        """Return a snapshot of recent raw PTY output chunks."""
        with self._lock:
            return list(self._raw_buffer)

    def interrupt(self) -> None:
        """Send Escape to PTY and signal waiters to abort."""
        if not self.proc or self.state == PtyState.DEAD:
            return
        self.proc.write("\x1b")
        self._interrupted.set()
        LOGGER.info("Session interrupted")

    def steer(self) -> bool:
        """Send exactly one Ctrl+C (soft interrupt).

        Returns True if sent, False if suppressed (cooldown / wrong state).
        A second Ctrl+C within the cooldown window is blocked to prevent
        killing the CLI process.
        """
        STEER_COOLDOWN = 5.0  # seconds — ignore repeated steer within this window

        if not self.proc or self.state == PtyState.DEAD:
            return False
        if self.state != PtyState.BUSY:
            return False

        now = time.monotonic()
        if now - self._last_steer_time < STEER_COOLDOWN:
            LOGGER.warning("Steer suppressed — cooldown (%.1fs remaining)",
                           STEER_COOLDOWN - (now - self._last_steer_time))
            return False

        self._last_steer_time = now
        self.proc.write("\x03")
        LOGGER.info("Session steered (Ctrl+C)")
        return True

    def prepare_for_input(self) -> None:
        """Reset session state for sending additional input (e.g., answering a CLI prompt).

        Clears screen and transitions to BUSY so wait_response() can track
        the next round of output.
        """
        if self.state == PtyState.DEAD:
            raise RuntimeError("PTY not available")
        with self._lock:
            self.screen.reset()
            self.screen.history.top.clear()
            self.screen.history.bottom.clear()
        self.state = PtyState.BUSY
        self._activity_event.clear()
        self._interrupted.clear()

    def clear_screen_buffer(self) -> None:
        """Discard accumulated virtual screen/history without changing PTY state."""
        if self.screen is None:
            return
        with self._lock:
            self.screen.reset()
            self.screen.history.top.clear()
            self.screen.history.bottom.clear()

    async def send_message(
        self,
        text: str,
        inputs: list[str] | None = None,
        inter_input_delay: float = 0.15,
    ) -> None:
        if self.state != PtyState.READY:
            raise RuntimeError(f"PTY not ready (state={self.state.value})")

        self.state = PtyState.BUSY
        self._activity_event.clear()
        self._interrupted.clear()

        # Reset screen so we only capture this turn's output
        with self._lock:
            self.screen.reset()
            self.screen.history.top.clear()
            self.screen.history.bottom.clear()

        chunks = inputs or [text + "\r"]
        for index, chunk in enumerate(chunks):
            self.proc.write(chunk)
            if index < len(chunks) - 1:
                await asyncio.sleep(inter_input_delay)
        LOGGER.debug("Sent message (%d chars, %d chunks): %s", len(text), len(chunks), text[:100])

    async def run_internal_command(
        self,
        text: str,
        *,
        inputs: list[str] | None = None,
        timeout: float = 30.0,
        idle_seconds: float | None = None,
        inter_input_delay: float = 0.0,
        settle_seconds: float = 0.5,
        capture_output: bool = False,
        require_ready_prompt: bool = False,
        ready_timeout: float | None = None,
        ready_stable_seconds: float = 0.5,
        command_ready_detector: Callable[[list[str]], bool] | None = None,
    ) -> list[str]:
        """Run a PTY control command as an isolated transaction.

        The command owns the session until it returns to READY. Its output
        is discarded by default and the virtual screen buffer is cleared
        before handing control back to the next queued job.
        """
        await self.send_message(
            text,
            inputs=inputs,
            inter_input_delay=inter_input_delay,
        )
        await self.wait_response(timeout=timeout, idle_seconds=idle_seconds)
        lines = self.get_screen_lines() if capture_output else []
        if require_ready_prompt:
            await self.wait_until_ready_prompt(
                timeout=ready_timeout or timeout,
                stable_seconds=ready_stable_seconds,
                detector=command_ready_detector,
            )
        elif settle_seconds > 0:
            await asyncio.sleep(settle_seconds)
        self.clear_screen_buffer()
        return lines

    async def wait_until_ready_prompt(
        self,
        timeout: float = 30.0,
        stable_seconds: float = 0.5,
        detector: Callable[[list[str]], bool] | None = None,
    ) -> None:
        deadline = time.monotonic() + timeout
        ready_since: float | None = None
        ready_check = detector or self._ready_detector
        while True:
            if self._interrupted.is_set():
                raise SessionInterrupted("Response interrupted")
            if self.state == PtyState.DEAD:
                raise RuntimeError("PTY process died during response")

            lines = self.get_screen_lines()
            now = time.monotonic()
            if ready_check(lines):
                if ready_since is None:
                    ready_since = now
                elif now - ready_since >= stable_seconds:
                    return
            else:
                ready_since = None

            remaining = deadline - now
            if remaining <= 0:
                raise TimeoutError(
                    f"Ready prompt did not stabilize within {timeout}s"
                )
            await asyncio.sleep(min(0.2, remaining))

    async def wait_response(
        self, timeout: float = 300.0, idle_seconds: float | None = None,
    ) -> None:
        """Wait until PTY output goes idle (no new data for `idle_seconds`).

        1. Wait for first data to arrive (activity started).
        2. Then poll until no data received for `idle_seconds`.
        Raises SessionInterrupted if interrupt() is called during wait.
        """
        idle_s = idle_seconds if idle_seconds is not None else self.IDLE_SECONDS
        deadline = time.monotonic() + timeout

        # Phase 1: wait for first data or interrupt
        while True:
            if self._interrupted.is_set():
                self._finish_response()
                raise SessionInterrupted("Response interrupted")
            if self._activity_event.is_set():
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._finish_response()
                raise TimeoutError("No PTY activity after sending message")
            if self.state == PtyState.DEAD:
                raise RuntimeError("PTY process died during response")
            await asyncio.sleep(min(0.3, remaining))

        if self.state == PtyState.DEAD:
            raise RuntimeError("PTY process died during response")

        # Phase 2: wait for idle
        while True:
            if self._interrupted.is_set():
                self._finish_response()
                raise SessionInterrupted("Response interrupted")

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._finish_response()
                raise TimeoutError(f"Response not completed within {timeout}s")

            elapsed = time.monotonic() - self._last_data_time
            if elapsed >= idle_s:
                break
            await asyncio.sleep(min(0.5, remaining))

        self._finish_response()

    def _finish_response(self) -> None:
        if self.state != PtyState.DEAD:
            self.state = PtyState.READY

    # ------------------------------------------------------------------
    # Screen access
    # ------------------------------------------------------------------

    def get_display_lines(self) -> list[str]:
        """Return current screen display lines only (no history)."""
        with self._lock:
            if self.screen:
                try:
                    return list(self.screen.display)
                except (IndexError, KeyError):
                    pass
            return []

    def get_screen_lines(self) -> list[str]:
        with self._lock:
            history: list[str] = []
            if self.screen and hasattr(self.screen, "history"):
                for line_dict in self.screen.history.top:
                    line = "".join(
                        line_dict[col].data for col in range(self.cols)
                    ).rstrip()
                    history.append(line)

            display: list[str] = []
            if self.screen:
                try:
                    display = list(self.screen.display)
                except (IndexError, KeyError):
                    # pyte bug: render() crashes on empty char data
                    # in screen buffer — return history only
                    pass

            return history + display

    # ------------------------------------------------------------------
    # Reader thread
    # ------------------------------------------------------------------

    def _reader_loop(self) -> None:
        while not self._stop_flag.is_set():
            try:
                data = self.proc.read(4096)
                if not data:
                    continue

                now = time.monotonic()
                self._last_data_time = now

                should_signal_ready = False
                should_signal_activity = False

                if self.state == PtyState.BUSY:
                    LOGGER.debug(
                        "Reader got %d bytes (state=%s, activity=%s)",
                        len(data), self.state.value, self._activity_event.is_set(),
                    )

                listeners: list[Callable[[str], None]] = []
                with self._lock:
                    self.stream.feed(data)
                    self._raw_buffer.append(data)
                    listeners = list(self._data_listeners)

                    # Startup: detect provider-specific ready signal
                    if self.state == PtyState.STARTING:
                        if self._is_ready() or self._is_ready_raw():
                            self.state = PtyState.READY
                            should_signal_ready = True

                for cb in listeners:
                    try:
                        cb(data)
                    except Exception:
                        LOGGER.debug("PTY data listener failed", exc_info=True)

                # Signal first activity after send_message
                if self.state == PtyState.BUSY and not self._activity_event.is_set():
                    should_signal_activity = True

                if should_signal_ready:
                    self.loop.call_soon_threadsafe(self._ready_event.set)
                if should_signal_activity:
                    self.loop.call_soon_threadsafe(self._activity_event.set)

            except EOFError:
                LOGGER.warning("PTY EOF — process died")
                break
            except Exception as exc:
                if not self._stop_flag.is_set():
                    LOGGER.exception("Reader thread error: %s", exc)
                    time.sleep(0.05)

        # Process died — signal waiters but do NOT auto-restart.
        # Session lifecycle is managed by SessionRegistry which handles
        # persona CLAUDE.md swap and --resume on respawn.
        if not self._stop_flag.is_set():
            self.state = PtyState.DEAD
            self.loop.call_soon_threadsafe(self._ready_event.set)
            self.loop.call_soon_threadsafe(self._activity_event.set)
            self.loop.call_soon_threadsafe(self._interrupted.set)

    # ------------------------------------------------------------------
    # Prompt detection (startup only)
    # ------------------------------------------------------------------

    def _is_ready(self) -> bool:
        """Check whether the provider-specific ready detector matches."""
        if not self.screen:
            return False
        history: list[str] = []
        if hasattr(self.screen, "history"):
            for line_dict in self.screen.history.top:
                line = "".join(
                    line_dict[col].data for col in range(self.cols)
                ).rstrip()
                history.append(line)
        try:
            display = list(self.screen.display)
        except (IndexError, KeyError):
            display = []
        return self._ready_detector(history + display)

    def _is_ready_raw(self) -> bool:
        if not self._raw_ready_detector:
            return False
        raw_text = "".join(self._raw_buffer)
        if not raw_text:
            return False
        return self._raw_ready_detector(raw_text)

    @staticmethod
    def _default_ready_detector(lines: list[str]) -> bool:
        return any("❯" in row for row in lines)
