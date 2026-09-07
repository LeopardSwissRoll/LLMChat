from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from dataclasses import replace
from pathlib import Path
from typing import Any, Awaitable, Callable

from ..models import BridgeSettings, ProviderCapabilities, SessionKey
from ..pty.handler import PtyState
from ..providers import ClaudeAdapter, CodexAdapter, ProviderAdapter
from ..transport.base import IncomingMessage, OutputSink
from .channel_manager import ChannelManager, ChannelMessage
from .context_builder import ContextBuilder
from .log_store import LogStore
from .prompt_composer import PromptComposer
from .session_registry import SessionRegistry
from .turn_executor import TurnExecutor, _collapse_tool_blocks, _compress_blank_lines

LOGGER = logging.getLogger(__name__)

# Match @MSG_ID (plain text, not Discord mention <@ID>)
_PIPE_RE = re.compile(r"(?<![<!\w])@(\d{17,20})\b")
_FAILURE_LOG_MARKER = "[실패]"


class PipeSourceFailed(Exception):
    """Raised when a pipe's source turn failed — cascades to downstream turns."""


@dataclass
class _TurnQueueJob:
    msg: IncomingMessage
    sink: OutputSink


@dataclass
class _RuntimeQueueJob:
    label: str
    action: Callable[[], Awaitable[Any]]
    future: asyncio.Future[Any]


class BridgeCore:
    """Facade that ties together all core components."""

    def __init__(
        self,
        settings: BridgeSettings,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self._settings = settings
        self._loop = loop

        v2_root = Path(__file__).resolve().parent.parent.parent
        self._log_store = LogStore(v2_root / ".bridge")
        self._channel_mgr = ChannelManager(
            v2_root / ".bridge",
            default_mode=settings.default_mode,
            default_provider=settings.default_provider,
            command_access=settings.command_access,
            default_provider_states={
                provider_id: provider_cfg.default_state
                for provider_id, provider_cfg in settings.providers.items()
            },
        )
        self._prompt_composer = PromptComposer(v2_root)
        self._context_builder = ContextBuilder(self._channel_mgr, v2_root / ".bridge")
        self._adapters: dict[str, ProviderAdapter] = {}
        for provider_id in settings.providers:
            if provider_id == "claude":
                self._adapters[provider_id] = ClaudeAdapter()
            elif provider_id == "codex":
                self._adapters[provider_id] = CodexAdapter()
            else:
                LOGGER.warning("No adapter registered for provider %s", provider_id)
        # Web publisher (optional — local server with Discord OAuth)
        self._web_publisher = None
        from ..config import _cfg_bool, _cfg_str, _cfg_int
        if _cfg_bool("WEB_PUBLISH_ENABLED", False):
            from .web_publisher import WebPublisher
            import os
            port = _cfg_int("WEB_PUBLISH_PORT", 8080)
            base_url = _cfg_str("WEB_PUBLISH_BASE_URL", "") or f"http://localhost:{port}"
            self._web_publisher = WebPublisher(
                pages_dir=v2_root / ".bridge" / "_pages",
                base_url=base_url,
                port=port,
                client_id=os.getenv("DISCORD_CLIENT_ID", ""),
                client_secret=os.getenv("DISCORD_CLIENT_SECRET", ""),
                guild_id=_cfg_str("WEB_PUBLISH_GUILD_ID", ""),
                threshold=_cfg_int("WEB_PUBLISH_THRESHOLD", 500),
                summary_length=_cfg_int("WEB_PUBLISH_SUMMARY_LENGTH", 200),
            )

        self._turn_executor = TurnExecutor(
            self._log_store, self._context_builder, self._channel_mgr,
            on_turn_complete=self._notify_turn_complete,
            bot_id=settings.bot_id,
            web_publisher=self._web_publisher,
        )

        self._registry = SessionRegistry(
            prompt_composer=self._prompt_composer,
            log_store=self._log_store,
            channel_mgr=self._channel_mgr,
            personas=settings.personas,
            providers=settings.providers,
            adapters=self._adapters,
            loop=loop,
            mode_dirs={
                1: settings.lv1_add_dirs,
                2: settings.default_add_dirs,
                3: settings.default_add_dirs,
                4: settings.default_add_dirs,
                5: settings.default_add_dirs,
            },
        )
        if self._web_publisher:
            self._web_publisher.set_bridge_core(self)

        # Hydrate context streams from disk logs
        self._hydrate_all_channels()

        # Per-session message queues
        self._queues: dict[SessionKey, asyncio.Queue[object]] = {}
        self._queue_tasks: dict[SessionKey, asyncio.Task] = {}

        # Session lock: locked sessions reject Discord input
        self._locked_sessions: set[SessionKey] = set()
        self._session_op_locks: dict[SessionKey, asyncio.Lock] = {}

        # Turn completion events for pipe resolution (channel_id → Event)
        self._turn_events: dict[int, asyncio.Event] = {}

        # Health monitor
        self._health_task: asyncio.Task | None = None

    def _make_key(self, msg: IncomingMessage) -> SessionKey:
        return SessionKey(
            persona_id=msg.persona_id,
            provider_id=msg.provider_id,
            channel_id=msg.channel_id,
            workspace=msg.workspace,
        )

    async def handle_message(
        self, msg: IncomingMessage, sink: OutputSink
    ) -> None:
        key = self._make_key(msg)

        if key not in self._queues:
            self._queues[key] = asyncio.Queue(maxsize=16)
            self._queue_tasks[key] = asyncio.create_task(
                self._queue_worker(key)
            )

        queue = self._queues[key]
        if queue.full():
            await sink.fail("대기열이 가득 찼습니다. 잠시 후 다시 시도해주세요.")
            return

        await queue.put(_TurnQueueJob(msg=msg, sink=sink))

    async def _enqueue_runtime_job(
        self,
        key: SessionKey,
        label: str,
        action: Callable[[], Awaitable[Any]],
    ) -> Any:
        if key not in self._queues:
            self._queues[key] = asyncio.Queue(maxsize=16)
            self._queue_tasks[key] = asyncio.create_task(
                self._queue_worker(key)
            )

        queue = self._queues[key]
        if queue.full():
            raise RuntimeError("대기열이 가득 찼습니다. 잠시 후 다시 시도해주세요.")

        future: asyncio.Future[Any] = self._loop.create_future()
        await queue.put(_RuntimeQueueJob(label=label, action=action, future=future))
        return await future

    async def _queue_worker(self, key: SessionKey) -> None:
        queue = self._queues[key]
        while True:
            job = await queue.get()
            try:
                if isinstance(job, _TurnQueueJob):
                    msg = job.msg
                    sink = job.sink

                    # Check session lock — reject if operator locked this session
                    if key in self._locked_sessions:
                        await sink.begin(f"Processing message from {msg.author_name}")
                        await sink.fail("세션이 잠겨 있습니다 (운영자 관찰 중)")
                        continue

                    # Show "processing..." immediately — before pipe resolution
                    # so users can chain pipes onto this message right away.
                    await sink.begin(f"Processing message from {msg.author_name}")
                    try:
                        # Resolve reply reference as pipe-like enrichment
                        # for already-targeted messages.
                        msg = await self._resolve_reply_pipe(msg)
                        # Resolve [[pipe:MSG_ID]] tokens (may wait for busy sessions)
                        msg = await self._resolve_pipe_tokens(msg)
                    except PipeSourceFailed as pf:
                        # Upstream turn failed — cascade failure, don't send to LLM.
                        await sink.fail(str(pf))
                        self._write_failure_log(job, pf)
                        continue

                    async with self._get_session_op_lock(key):
                        session = await self._registry.get_or_create(
                            key, author_id=msg.author_id,
                            server_id=msg.server_id,
                        )
                        persona = self._settings.personas[key.persona_id]
                        adapter = self._registry.get_provider_adapter(key.provider_id)
                        mode = self._registry.get_session_mode(
                            msg.server_id, msg.channel_id, key.persona_id, key.provider_id,
                        )
                        save_user_log = "users" in self._registry.get_mode_dirs(mode)
                        await self._turn_executor.execute(
                            session, msg, sink, persona, adapter,
                            save_user_log=save_user_log,
                        )
                elif isinstance(job, _RuntimeQueueJob):
                    async with self._get_session_op_lock(key):
                        result = await job.action()
                    if not job.future.done():
                        job.future.set_result(result)
                else:
                    LOGGER.warning("Unknown queue job for %s: %r", key.session_id, job)
            except Exception as exc:
                LOGGER.exception("Queue worker error for %s", key.session_id)
                if isinstance(job, _TurnQueueJob):
                    try:
                        await job.sink.fail(f"Error: {exc}")
                    except Exception:
                        pass
                    # Write failure log so downstream pipe chains cascade-fail
                    # instead of waiting 30 minutes for a log that will never come.
                    self._write_failure_log(job, exc)
                elif isinstance(job, _RuntimeQueueJob):
                    if not job.future.done():
                        job.future.set_exception(exc)
            finally:
                queue.task_done()

    def _write_failure_log(self, job: _TurnQueueJob, exc: Exception) -> None:
        """Write a failure log under the processing message's ID.

        This unblocks downstream pipe chains immediately instead of
        making them wait for the full 30-minute timeout.
        """
        try:
            status_id = getattr(job.sink, "status_message_id", None)
            if not status_id:
                return
            msg = job.msg
            fail_path = (
                self._log_store._root
                / msg.server_id
                / str(msg.channel_id)
                / f"{status_id}.txt"
            )
            fail_path.parent.mkdir(parents=True, exist_ok=True)
            fail_path.write_text(
                f"{_FAILURE_LOG_MARKER}\n{exc}", encoding="utf-8",
            )
            LOGGER.info(
                "Wrote failure log for pipe cascade: %s/%d/%s",
                msg.server_id, msg.channel_id, status_id,
            )
        except Exception:
            LOGGER.warning("Failed to write failure log for pipe cascade")

    def _get_session_op_lock(self, key: SessionKey) -> asyncio.Lock:
        lock = self._session_op_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._session_op_locks[key] = lock
        return lock

    def _hydrate_all_channels(self) -> None:
        """Restore context streams from disk logs for all known channels."""
        bridge_root = self._log_store._root
        if not bridge_root.exists():
            return
        total = 0
        for server_dir in bridge_root.iterdir():
            if not server_dir.is_dir():
                continue
            for channel_dir in server_dir.iterdir():
                if not channel_dir.is_dir() or channel_dir.name == "users":
                    continue
                try:
                    n = self._channel_mgr.hydrate_from_logs(
                        server_dir.name, int(channel_dir.name),
                    )
                    total += n
                except (ValueError, Exception):
                    continue
        if total:
            LOGGER.info("Hydrated %d messages from disk logs", total)

    # ------------------------------------------------------------------
    # Passive logging
    # ------------------------------------------------------------------

    def log_channel_message(
        self, server_id: str, channel_id: int,
        author_id: str, message_id: str,
        author_name: str, text: str,
        reply_to: str | None = None,
    ) -> None:
        self._log_store.save_user_message(
            server_id=server_id,
            channel_id=channel_id,
            author_id=author_id,
            message_id=message_id,
            author_name=author_name,
            text=text,
        )
        self._channel_mgr.append_message(
            server_id, channel_id,
            ChannelMessage(
                message_id=message_id,
                author_name=author_name,
                text=text,
                author_id=author_id,
                reply_to=reply_to,
            ),
        )

    # ------------------------------------------------------------------
    # Mode / provider
    # ------------------------------------------------------------------

    @property
    def channel_mgr(self) -> ChannelManager:
        return self._channel_mgr

    def get_provider_adapter(self, provider_id: str) -> ProviderAdapter:
        return self._registry.get_provider_adapter(provider_id)

    def get_provider_capabilities(self, provider_id: str) -> ProviderCapabilities:
        return self.get_provider_adapter(provider_id).capabilities()

    def supports_control(self, provider_id: str, control: str) -> bool:
        return self.get_provider_adapter(provider_id).supports_control(control)

    async def switch_mode(
        self,
        server_id: str,
        persona_id: str,
        provider_id: str,
        channel_id: int,
        workspace: str,
        mode: int,
    ) -> int:
        key = SessionKey(
            persona_id=persona_id,
            provider_id=provider_id,
            channel_id=channel_id,
            workspace=workspace,
        )
        self._channel_mgr.set_mode(
            server_id, channel_id, persona_id, provider_id, mode,
        )
        provider_state = self._channel_mgr.get_provider_state(
            server_id, channel_id, persona_id, provider_id,
        )
        provider_state.cli_session_id = None
        self._channel_mgr._save_state(server_id, channel_id)
        return mode

    # ------------------------------------------------------------------
    # Raw / interrupt / reset
    # ------------------------------------------------------------------

    async def send_raw(
        self,
        persona_id: str,
        provider_id: str,
        channel_id: int,
        workspace: str,
        raw_input: str,
    ) -> None:
        key = SessionKey(
            persona_id=persona_id,
            provider_id=provider_id,
            channel_id=channel_id,
            workspace=workspace,
        )
        session = self._registry.get_session(key)
        if not session or session.state == PtyState.DEAD:
            raise RuntimeError("No active session")
        if session.state == PtyState.BUSY:
            raise RuntimeError("Session is busy — wait for response to complete")
        session.write_raw(raw_input)

    async def _run_runtime_control_now(
        self,
        server_id: str,
        key: SessionKey,
        control: str,
        *,
        value: str | None = None,
    ) -> None:
        session = self._registry.get_session(key)
        if not session or session.state == PtyState.DEAD:
            raise RuntimeError("No active session")
        if session.state != PtyState.READY:
            raise RuntimeError("Session is busy — wait for response to complete")

        adapter = self.get_provider_adapter(key.provider_id)
        state = self._channel_mgr.get_provider_state(
            server_id, key.channel_id, key.persona_id, key.provider_id,
        )
        inputs = adapter.build_control_inputs(control, value=value, state=state)
        if not inputs:
            raise RuntimeError(f"No control inputs for {key.provider_id}:{control}")
        persona = self._settings.personas[key.persona_id]
        command = inputs[0]
        await session.run_internal_command(
            command,
            inputs=inputs,
            timeout=min(
                adapter.internal_command_timeout(command),
                persona.response_timeout,
            ),
            idle_seconds=min(2.0, persona.idle_seconds),
            inter_input_delay=0.0,
            settle_seconds=adapter.internal_command_stable_seconds(command),
            capture_output=False,
            require_ready_prompt=adapter.internal_command_requires_ready_prompt(command),
            ready_timeout=min(
                adapter.internal_command_timeout(command),
                persona.response_timeout,
            ),
            ready_stable_seconds=adapter.internal_command_stable_seconds(command),
            command_ready_detector=adapter.command_ready_detector,
        )

    async def run_runtime_control(
        self,
        server_id: str,
        persona_id: str,
        provider_id: str,
        channel_id: int,
        workspace: str,
        control: str,
        *,
        value: str | None = None,
    ) -> None:
        key = SessionKey(
            persona_id=persona_id,
            provider_id=provider_id,
            channel_id=channel_id,
            workspace=workspace,
        )
        await self._enqueue_runtime_job(
            key,
            f"control:{control}",
            lambda: self._run_runtime_control_now(
                server_id, key, control, value=value,
            ),
        )

    async def _send_runtime_message_now(
        self,
        key: SessionKey,
        text: str,
    ) -> None:
        session = self._registry.get_session(key)
        if not session or session.state == PtyState.DEAD:
            raise RuntimeError("No active session")
        if session.state != PtyState.READY:
            raise RuntimeError("Session is busy — wait for response to complete")
        adapter = self.get_provider_adapter(key.provider_id)
        persona = self._settings.personas[key.persona_id]
        inputs = adapter.build_message_inputs(text)
        await session.send_message(text, inputs=inputs)
        await session.wait_response(
            timeout=min(60.0, persona.response_timeout),
            idle_seconds=min(2.0, persona.idle_seconds),
        )
        await asyncio.sleep(0.5)
        session.clear_screen_buffer()

    async def send_runtime_message(
        self,
        persona_id: str,
        provider_id: str,
        channel_id: int,
        workspace: str,
        text: str,
    ) -> None:
        key = SessionKey(
            persona_id=persona_id,
            provider_id=provider_id,
            channel_id=channel_id,
            workspace=workspace,
        )
        await self._enqueue_runtime_job(
            key,
            "runtime_message",
            lambda: self._send_runtime_message_now(key, text),
        )

    async def interrupt_session(
        self, persona_id: str, provider_id: str, channel_id: int, workspace: str
    ) -> None:
        key = SessionKey(
            persona_id=persona_id,
            provider_id=provider_id,
            channel_id=channel_id,
            workspace=workspace,
        )
        session = self._registry.get_session(key)
        if not session or session.state == PtyState.DEAD:
            raise RuntimeError("No active session")
        if session.state != PtyState.BUSY:
            raise RuntimeError("No active response to interrupt")
        session.interrupt()

    async def steer_session(
        self, persona_id: str, provider_id: str, channel_id: int, workspace: str
    ) -> bool:
        """Soft interrupt (Ctrl+C) — stops generation, allows steer via next message.

        Returns True if Ctrl+C was sent, False if suppressed (cooldown / not busy).
        """
        key = SessionKey(
            persona_id=persona_id,
            provider_id=provider_id,
            channel_id=channel_id,
            workspace=workspace,
        )
        session = self._registry.get_session(key)
        if not session or session.state == PtyState.DEAD:
            raise RuntimeError("No active session")
        return session.steer()

    async def reset_session(
        self, persona_id: str, provider_id: str, channel_id: int, workspace: str
    ) -> None:
        key = SessionKey(
            persona_id=persona_id,
            provider_id=provider_id,
            channel_id=channel_id,
            workspace=workspace,
        )
        async def _reset() -> None:
            session = self._registry.get_session(key)
            if not session or session.state == PtyState.DEAD:
                raise RuntimeError("No active session")
            await self._registry.restart_session(key)

        await self._enqueue_runtime_job(key, "reset", _reset)

    def stop_other_provider_sessions(
        self,
        persona_id: str,
        provider_id: str,
        channel_id: int,
        workspace: str,
    ) -> int:
        key = SessionKey(
            persona_id=persona_id,
            provider_id=provider_id,
            channel_id=channel_id,
            workspace=workspace,
        )
        return self._registry.stop_other_provider_sessions(key)

    def get_session_list(self) -> list[dict[str, Any]]:
        sessions: list[dict[str, Any]] = []
        for key, session in self._registry.get_all_sessions().items():
            persona = self._settings.personas.get(key.persona_id)
            sessions.append({
                "session_id": key.session_id,
                "persona_id": key.persona_id,
                "display_name": persona.display_name if persona else key.persona_id,
                "provider_id": key.provider_id,
                "channel_id": key.channel_id,
                "workspace": key.workspace,
                "state": session.state.value,
                "alive": session.proc.isalive() if session.proc else False,
                "rows": session.rows,
                "cols": session.cols,
                "model": self._parse_model(session.command),
            })
        sessions.sort(key=lambda item: (
            item["display_name"],
            item["channel_id"],
            item["provider_id"],
            item["workspace"],
        ))
        return sessions

    def get_session_by_id(self, session_id: str):
        return self._registry.get_session_by_id(session_id)

    # ------------------------------------------------------------------
    # Session lock (blocks Discord input while operator is observing)
    # ------------------------------------------------------------------

    def lock_session(self, session_id: str) -> bool:
        """Lock a session — Discord turns will be rejected."""
        result = self._registry.get_session_by_id(session_id)
        if not result:
            return False
        key, _ = result
        self._locked_sessions.add(key)
        LOGGER.info("Session locked: %s (%s/%s)", session_id, key.persona_id, key.provider_id)
        return True

    def unlock_session(self, session_id: str) -> bool:
        """Unlock a session — Discord turns resume."""
        result = self._registry.get_session_by_id(session_id)
        if not result:
            return False
        key, _ = result
        self._locked_sessions.discard(key)
        LOGGER.info("Session unlocked: %s (%s/%s)", session_id, key.persona_id, key.provider_id)
        return True

    def is_session_locked(self, session_id: str) -> bool:
        result = self._registry.get_session_by_id(session_id)
        if not result:
            return False
        key, _ = result
        return key in self._locked_sessions

    # ------------------------------------------------------------------
    # Status / usage
    # ------------------------------------------------------------------

    def get_all_statuses(self) -> dict[str, dict]:
        result = {}
        for key, session in self._registry.get_all_sessions().items():
            label = f"{key.persona_id}/{key.provider_id}@{key.channel_id}"
            model = self._parse_model(session.command)
            result[label] = {
                "provider_id": key.provider_id,
                "state": session.state.value,
                "alive": session.proc.isalive() if session.proc else False,
                "cwd": session.cwd,
                "model": model,
            }
        return result

    def get_dead_sessions(self) -> list[SessionKey]:
        return [
            key for key, session in self._registry.get_all_sessions().items()
            if session.state == PtyState.DEAD
        ]

    async def query_usage(
        self, persona_id: str, provider_id: str, channel_id: int, workspace: str
    ) -> dict[str, str]:
        if not self.supports_control(provider_id, "usage"):
            return {}
        key = SessionKey(
            persona_id=persona_id,
            provider_id=provider_id,
            channel_id=channel_id,
            workspace=workspace,
        )
        adapter = self.get_provider_adapter(provider_id)
        persona = self._settings.personas[persona_id]

        async def _query() -> dict[str, str]:
            session = self._registry.get_session(key)
            if not session or session.state != PtyState.READY:
                return {}
            query_inputs, cleanup_inputs = adapter.usage_query_inputs()
            if not query_inputs:
                return adapter.parse_usage(session.get_display_lines())
            lines = await session.run_internal_command(
                query_inputs[0],
                inputs=query_inputs,
                inter_input_delay=0.0,
                timeout=min(30.0, persona.response_timeout),
                idle_seconds=min(2.0, persona.idle_seconds),
                settle_seconds=0.0,
                capture_output=True,
            )
            for raw in cleanup_inputs:
                session.write_raw(raw)
                await asyncio.sleep(0.2)
            await asyncio.sleep(0.3)
            session.clear_screen_buffer()
            return adapter.parse_usage(lines)

        return await self._enqueue_runtime_job(key, "usage", _query)

    @staticmethod
    def _parse_model(command: str) -> str:
        m = re.search(r"--model\s+(\S+)", command)
        return m.group(1) if m else "default"

    # ------------------------------------------------------------------
    # Health / cleanup / lifecycle
    # ------------------------------------------------------------------

    def start_health_monitor(self, interval: float = 30.0) -> None:
        if self._health_task is None:
            self._health_task = asyncio.create_task(self._health_loop(interval))
        if self._web_publisher:
            asyncio.create_task(self._web_publisher.start())

    async def _health_loop(self, interval: float) -> None:
        while True:
            await asyncio.sleep(interval)
            for key, session in self._registry.get_all_sessions().items():
                if session.state == PtyState.DEAD:
                    LOGGER.warning("Health: dead session %s@%d", key.persona_id, key.channel_id)

    def cleanup_servers(self, valid_server_ids: set[str]) -> int:
        return self._log_store.cleanup_servers(valid_server_ids)

    def cleanup_channels(self, server_id: str, valid_channel_ids: set[str]) -> int:
        return self._log_store.cleanup_channels(server_id, valid_channel_ids)

    # ------------------------------------------------------------------
    # Pipe resolution ([[pipe:MSG_ID]])
    # ------------------------------------------------------------------

    def _notify_turn_complete(self, channel_id: int) -> None:
        """Called by TurnExecutor when a turn finishes — wakes pipe waiters."""
        event = self._turn_events.get(channel_id)
        if event:
            event.set()
        # Fresh event for next cycle
        self._turn_events[channel_id] = asyncio.Event()

    def _any_channel_busy(self, channel_id: int) -> bool:
        """Check if any session in the given channel is currently processing."""
        for key, session in self._registry.get_all_sessions().items():
            if key.channel_id == channel_id and session.state == PtyState.BUSY:
                return True
        return False

    async def _resolve_pipe(
        self, server_id: str, channel_id: int, msg_id: str,
        *, timeout: float = 1800.0,
    ) -> str | None:
        """Wait for and read a message's log content.

        Polls until the log file appears or timeout expires.
        Keeps waiting even if no session is currently busy —
        the target turn may not have started yet (pipe chain).
        Default timeout: 30 minutes (1800s).
        """
        import time
        log_path = self._log_store._root / server_id / str(channel_id) / f"{msg_id}.txt"
        deadline = time.monotonic() + timeout

        while True:
            if log_path.exists():
                content = log_path.read_text(encoding="utf-8")
                if content.startswith(_FAILURE_LOG_MARKER):
                    raise PipeSourceFailed(
                        f"파이프 대상 메시지({msg_id})가 실패했습니다."
                    )
                _, _, body = content.partition("\n")
                # Collapse tool blocks so downstream personas don't
                # see/quote raw tool output from upstream turns.
                body = _collapse_tool_blocks(body.strip())
                body = _compress_blank_lines(body)
                return body

            if time.monotonic() > deadline:
                return None  # genuine timeout

            # Wait for next turn completion in this channel, or poll every 10s
            if channel_id not in self._turn_events:
                self._turn_events[channel_id] = asyncio.Event()
            event = self._turn_events[channel_id]
            remaining = deadline - time.monotonic()
            try:
                await asyncio.wait_for(
                    event.wait(),
                    timeout=min(10.0, max(0.1, remaining)),
                )
            except asyncio.TimeoutError:
                pass  # re-check log file

    async def _resolve_reply_pipe(self, msg: IncomingMessage) -> IncomingMessage:
        """If the message is a reply, inject the replied-to message's content.

        This is not a trigger by itself; it only enriches messages that were
        already targeted by explicit mention / alias. Waits for completion if
        the target message is still being processed.
        """
        if not msg.reply_to_msg_id:
            return msg

        content = await self._resolve_pipe(
            msg.server_id, msg.channel_id, msg.reply_to_msg_id,
        )
        if not content:
            return msg  # Not a logged message (or resolution failed)

        enriched = f"[메시지 참조]\n{content}\n---\n{msg.text}"
        return replace(msg, text=enriched)

    async def _resolve_pipe_tokens(self, msg: IncomingMessage) -> IncomingMessage:
        """Resolve @MSG_ID tokens in message text."""
        matches = list(_PIPE_RE.finditer(msg.text))
        if not matches:
            return msg

        text = msg.text
        for match in reversed(matches):
            pipe_msg_id = match.group(1)
            content = await self._resolve_pipe(msg.server_id, msg.channel_id, pipe_msg_id)
            if content:
                repl = f"[메시지 참조]\n{content}"
            else:
                repl = f"[메시지 참조: {pipe_msg_id} — 찾을 수 없음]"
            text = text[:match.start()] + repl + text[match.end():]

        return replace(msg, text=text)

    async def drain(self) -> None:
        for queue in self._queues.values():
            await queue.join()

    async def stop(self) -> None:
        if self._web_publisher:
            try:
                await self._web_publisher.stop()
            except Exception:
                LOGGER.debug("WebPublisher stop error", exc_info=True)
        if self._health_task:
            self._health_task.cancel()
            try:
                await self._health_task
            except asyncio.CancelledError:
                pass
        for task in self._queue_tasks.values():
            task.cancel()
        if self._queue_tasks:
            await asyncio.gather(*self._queue_tasks.values(), return_exceptions=True)
        self._registry.stop_all()
