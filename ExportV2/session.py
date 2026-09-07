from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import shutil
import threading
import time
from collections import deque
from pathlib import Path

from Export.vt.virtual_terminal import VirtualTerminal
from v2.bridge.core.turn_executor import _collapse_tool_blocks, _compress_blank_lines

from .prompt_composer import PromptComposer
from v2.bridge.providers.base import ProviderAdapter
from v2.bridge.providers.claude import ClaudeAdapter
from v2.bridge.providers.codex import CodexAdapter

from .logs import ServerStore
from .models import (
    LlmSettings,
    PersonaChannelState,
    PersonaConfig as V2PersonaConfig,
    PersonaSettings,
    ProviderState,
    ServerRuntimeState,
    ServerSettings,
)

LOGGER = logging.getLogger(__name__)

_MAX_INTERACTIVE_ROUNDS = 5


def _make_adapter(provider: str) -> ProviderAdapter:
    if provider == "codex":
        return CodexAdapter()
    if provider == "claude":
        return ClaudeAdapter()
    raise ValueError(f"Unknown provider: {provider}")


class PersonaSession:
    _CLAUDE_SESSION_FILE_RE = re.compile(
        r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
        re.IGNORECASE,
    )

    def __init__(
        self,
        *,
        llm: LlmSettings,
        server: ServerSettings,
        persona: PersonaSettings,
        channel_id: int,
        store: ServerStore,
        server_state: ServerRuntimeState,
        state: PersonaChannelState,
    ) -> None:
        self._llm = llm
        self._server = server
        self._persona = persona
        self._channel_id = channel_id
        self._store = store
        self._server_state = server_state
        self._state = state
        self._adapter = _make_adapter(llm.provider)

        self._lock = asyncio.Lock()
        self._vt: VirtualTerminal | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._last_output_time = 0.0
        self._activity_event: asyncio.Event | None = None
        self._raw_chunks: deque[str] = deque(maxlen=200)
        self._raw_lock = threading.Lock()
        self._prompt_composer = PromptComposer(Path(__file__).resolve().parent.parent / "v2")
        self._ready = False
        self._spawn_started_at = 0.0

    @property
    def persona_id(self) -> str:
        return self._persona.persona_id

    @property
    def channel_id(self) -> int:
        return self._channel_id

    @property
    def provider(self) -> str:
        return self._llm.provider

    def _session_id(self) -> str:
        raw = f"{self._server.server_id}:{self._channel_id}:{self._persona.persona_id}:{self._llm.provider}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def _session_dir(self) -> Path:
        return self._store.session_dir(self._channel_id, self._session_id())

    def _runtime_dir(self) -> Path | None:
        if self._llm.provider != "codex":
            return None
        path = self._session_dir() / ".codex_home"
        path.mkdir(parents=True, exist_ok=True)
        self._sync_codex_runtime_home(path)
        return path

    def _sync_codex_runtime_home(self, runtime_dir: Path) -> None:
        source_home = Path.home() / ".codex"
        for filename in ("auth.json",):
            src = source_home / filename
            dest = runtime_dir / filename
            if not src.exists():
                continue
            if dest.exists():
                try:
                    if dest.stat().st_mtime >= src.stat().st_mtime:
                        continue
                except OSError:
                    pass
            try:
                shutil.copy2(src, dest)
            except OSError:
                LOGGER.warning("Failed to sync Codex runtime auth file: %s", filename)

    def _prompt_file(self) -> Path:
        return self._session_dir() / f".prompt_{self._session_id()}.md"

    def _provider_state(self) -> ProviderState:
        return ProviderState(
            cli_session_id=self._state.cli_session_id,
            model=self._llm.model,
            effort=self._llm.effort,
            permission=self._llm.permission,
            fast=self._llm.fast,
        )

    def _v2_persona(self) -> V2PersonaConfig:
        return V2PersonaConfig(
            persona_id=self._persona.persona_id,
            display_name=self._persona.display_name,
            role_aliases=(self._persona.display_name,),
            identity_text=f"ExportV2 ({self._persona.display_name})",
            prompt_dir=Path(self._persona.prompt_dir),
            avatar_url=self._persona.avatar_url,
            enabled_providers=(self._llm.provider,),
            pty_rows=self._llm.rows,
            pty_cols=self._llm.cols,
            response_timeout=self._llm.response_timeout,
            idle_seconds=self._llm.idle_seconds,
        )

    def _compose_prompt(self) -> str:
        return self._prompt_composer.compose(
            self._llm.provider,
            self._v2_persona(),
            protagonist_id=None,
            mode=5,
        )

    def _add_dirs(self) -> list[Path]:
        prompts_root = (
            Path(__file__).resolve().parent.parent / "v2" / "prompts" / self._persona.prompt_dir
        ).resolve()
        return [
            self._store.channel_log_dir(self._channel_id).resolve(),
            prompts_root,
        ]

    def _spawn_args(self, *, resume_id: str | None) -> tuple[list[str], dict[str, str]]:
        prompt_file = self._prompt_file()
        prompt_text = self._compose_prompt()
        prompt_file.write_text(prompt_text, encoding="utf-8")

        if self._llm.provider == "codex":
            return self._spawn_args_codex(prompt_file=prompt_file, resume_id=resume_id)
        return self._spawn_args_claude(prompt_file=prompt_file, resume_id=resume_id)

    def _spawn_args_codex(
        self, *, prompt_file: Path, resume_id: str | None
    ) -> tuple[list[str], dict[str, str]]:
        adapter = self._adapter
        assert isinstance(adapter, CodexAdapter)
        runtime_dir = self._runtime_dir()
        adapter._prepare_runtime_home(  # pyright: ignore[reportPrivateUsage]
            runtime_dir=runtime_dir,
            prompt_text=prompt_file.read_text(encoding="utf-8"),
            workspace=str(self._server.workspace),
            state=self._provider_state(),
        )
        args = [
            self._llm.cli_executable,
            *adapter._sanitize_cli_args(self._llm.cli_args),  # pyright: ignore[reportPrivateUsage]
        ]
        for add_dir in self._add_dirs():
            args.extend(["--add-dir", str(add_dir)])
        if resume_id:
            args.extend(["resume", resume_id])
        env = {"CODEX_HOME": str(runtime_dir)} if runtime_dir else {}
        return args, env

    def _spawn_args_claude(
        self, *, prompt_file: Path, resume_id: str | None
    ) -> tuple[list[str], dict[str, str]]:
        state = self._provider_state()
        args: list[str] = [self._llm.cli_executable, *self._llm.cli_args]
        if not resume_id:
            if state.model and state.model != "default":
                args.extend(["--model", state.model])
            if state.effort and state.effort != "default":
                args.extend(["--effort", state.effort])
            permission_map = {"plan": "plan", "bypass": "bypassPermissions"}
            mode = permission_map.get(state.permission)
            if mode:
                args.extend(["--permission-mode", mode])
        args.extend(["--append-system-prompt-file", str(prompt_file)])
        for add_dir in self._add_dirs():
            args.extend(["--add-dir", str(add_dir)])
        if resume_id:
            args.extend(["--resume", resume_id])
        return args, {}

    def _on_raw_output(self, data: str) -> None:
        with self._raw_lock:
            self._raw_chunks.append(data)
        self._last_output_time = time.monotonic()
        if self._loop is not None and self._activity_event is not None:
            self._loop.call_soon_threadsafe(self._activity_event.set)

    def _recent_raw_text(self) -> str:
        with self._raw_lock:
            return "".join(self._raw_chunks)

    def _persist_state(self) -> None:
        self._store.save_state(self._server_state)

    def _set_cli_session_id(self, cli_session_id: str | None) -> None:
        if not cli_session_id:
            return
        if (
            self._state.cli_session_id == cli_session_id
            and self._state.provider_id == self._llm.provider
        ):
            return
        self._state.provider_id = self._llm.provider
        self._state.cli_session_id = cli_session_id
        self._persist_state()

    def _set_last_message_id(self, message_id: int) -> None:
        if self._state.last_message_id == message_id:
            return
        self._state.last_message_id = message_id
        self._persist_state()

    def _clear_local_screen(self) -> None:
        if self._vt is None:
            return
        with self._vt._lock:  # noqa: SLF001 - intentional Export internals access
            self._vt._screen.reset()
            self._vt._screen.history.top.clear()
            self._vt._screen.history.bottom.clear()

    def _screen_lines(self) -> list[str]:
        if self._vt is None:
            return []
        return self._vt.get_scrollback_lines()

    def _message_file_path(self, message_id: int) -> Path:
        return self._store.channel_log_dir(self._channel_id) / f"{message_id}.txt"

    def _codex_payload_file_path(self, message_id: int) -> Path:
        return self._store.channel_log_dir(self._channel_id) / f"{message_id}.codex.json"

    def _build_turn_input(
        self,
        *,
        author_name: str,
        author_id: int,
        raw_text: str,
        message_id: int,
    ) -> str:
        if self._llm.provider != "codex":
            return f"[{author_name}:{author_id}] : {raw_text}"

        payload_path = self._codex_payload_file_path(message_id).resolve()
        payload = {
            "author_name": author_name,
            "author_id": author_id,
            "text": raw_text,
        }
        payload_path.write_text(
            json.dumps(payload, ensure_ascii=True, indent=2),
            encoding="utf-8",
        )
        return (
            f'Read and decode the JSON message in "{payload_path}". '
            "The JSON strings use Unicode escapes. "
            "Use the decoded author and text as the exact user input and answer it directly."
        )

    def _response_looks_incomplete(self, response: str) -> bool:
        if not response.strip():
            return True

        lines = [line.strip() for line in response.splitlines() if line.strip()]
        if not lines:
            return True

        incomplete_markers = (
            "Read and decode the JSON message in",
            "The JSON strings use Unicode escapes.",
            "Use the decoded author and text as the exact user input",
            "◦ Working (",
        )
        if any(marker in response for marker in incomplete_markers):
            return True

        if len(lines) == 1 and "gpt-5.4 xhigh" in lines[0]:
            return True

        return False

    @staticmethod
    def _sanitize_response(response: str) -> str:
        response = _collapse_tool_blocks(response)
        response = _compress_blank_lines(response)
        return response.strip()

    def _detect_startup_key(self, *, lines: list[str], raw_text: str) -> str | None:
        adapter = self._adapter
        trust_key = adapter.detect_trust_prompt(lines) if hasattr(adapter, "detect_trust_prompt") else None
        if trust_key:
            return trust_key
        if hasattr(adapter, "detect_startup_prompt"):
            startup_key = adapter.detect_startup_prompt(raw_text)
            if startup_key:
                return startup_key
        return None

    def _should_resume(self) -> bool:
        if not self._state.cli_session_id:
            return False
        if self._state.provider_id != self._llm.provider:
            return False
        return self._store.channel_has_logs(self._channel_id)

    def _claude_project_dir(self) -> Path | None:
        if self._llm.provider != "claude":
            return None
        sanitized = re.sub(r"[^A-Za-z0-9]", "-", str(self._server.workspace))
        path = Path.home() / ".claude" / "projects" / sanitized
        return path if path.exists() else None

    def _resolve_claude_resume_id_from_disk(self) -> str | None:
        project_dir = self._claude_project_dir()
        if project_dir is None:
            return None

        candidates: list[Path] = []
        try:
            for path in project_dir.glob("*.jsonl"):
                if not self._CLAUDE_SESSION_FILE_RE.match(path.stem):
                    continue
                candidates.append(path)
        except OSError:
            return None

        if not candidates:
            return None

        started_at = self._spawn_started_at or time.time()
        fresh_candidates = [
            path
            for path in candidates
            if path.stat().st_mtime >= started_at - 5.0
        ]
        pool = fresh_candidates or candidates
        try:
            latest = max(pool, key=lambda path: path.stat().st_mtime)
        except (OSError, ValueError):
            return None
        return latest.stem

    def _resolve_resume_id(
        self,
        lines: list[str],
        *,
        allow_disk_fallback: bool = False,
    ) -> str | None:
        resolved = self._adapter.resolve_resume_id(
            lines=lines,
            workspace=str(self._server.workspace),
            runtime_dir=self._runtime_dir(),
        )
        if resolved:
            return resolved
        if allow_disk_fallback:
            return self._resolve_claude_resume_id_from_disk()
        return None

    async def ensure_started(self) -> None:
        if self._vt is not None and self._vt.is_alive() and self._ready:
            return

        try:
            await self._start_once(allow_resume=True)
        except TimeoutError:
            if not self._state.cli_session_id:
                raise
            LOGGER.warning(
                "Resume startup timed out — invalidating cli_session_id and retrying fresh: persona=%s channel=%s",
                self._persona.persona_id, self._channel_id,
            )
            self._set_cli_session_id(None)
            await self._start_once(allow_resume=False)

    async def _start_once(self, *, allow_resume: bool) -> None:
        self._loop = asyncio.get_running_loop()
        self._activity_event = asyncio.Event()
        self._activity_event.clear()
        self._ready = False
        with self._raw_lock:
            self._raw_chunks.clear()

        resume_id = (
            self._state.cli_session_id
            if allow_resume and self._should_resume()
            else None
        )
        args, env = self._spawn_args(resume_id=resume_id)
        LOGGER.info(
            "Starting ExportV2 %s session: server=%s channel=%s persona=%s resume=%s",
            self._llm.provider,
            self._server.server_id,
            self._channel_id,
            self._persona.persona_id,
            bool(resume_id),
        )
        self._spawn_started_at = time.time()
        vt = VirtualTerminal(
            self._session_id(),
            args,
            self._server.workspace,
            env=env,
            cols=self._llm.cols,
            rows=self._llm.rows,
        )
        vt.add_raw_output_listener(self._on_raw_output)
        vt.start()
        self._vt = vt

        deadline = time.monotonic() + self._llm.startup_timeout
        last_prompt_send = 0.0
        try:
            while True:
                if self._vt is None or not self._vt.is_alive():
                    raise RuntimeError(f"{self._llm.provider} session died during startup")

                lines = self._screen_lines()
                raw_text = self._recent_raw_text()
                if self._adapter.ready_detector(lines) or self._adapter.raw_ready_detector(raw_text):
                    break

                now = time.monotonic()
                key = self._detect_startup_key(lines=lines, raw_text=raw_text)
                if key and now - last_prompt_send >= 1.0:
                    self._vt.write(key)
                    last_prompt_send = now

                if now >= deadline:
                    self._dump_startup_failure(lines=lines, raw_text=raw_text, resume=bool(resume_id))
                    raise TimeoutError(
                        f"{self._llm.provider} session for {self._persona.persona_id} did not become ready within {self._llm.startup_timeout}s",
                    )
                await asyncio.sleep(0.2)

            resolved_id = self._resolve_resume_id(self._screen_lines())
            self._set_cli_session_id(resolved_id)

            # Settle: claude --resume keeps streaming the replayed context for
            # a while after the ready prompt appears. If we send the first
            # input now, those replayed lines stay on-screen and get extracted
            # as part of the response. Wait until output goes quiet for a full
            # idle window before declaring the session truly ready.
            if resume_id:
                settle_deadline = time.monotonic() + max(
                    self._llm.startup_timeout, 30.0,
                )
                while time.monotonic() < settle_deadline:
                    if self._vt is None or not self._vt.is_alive():
                        raise RuntimeError(
                            f"{self._llm.provider} session died during settle"
                        )
                    if (
                        time.monotonic() - self._last_output_time
                        >= self._llm.idle_seconds
                    ):
                        break
                    await asyncio.sleep(0.5)
                LOGGER.info(
                    "Resume settle complete: persona=%s channel=%s",
                    self._persona.persona_id, self._channel_id,
                )

            self._ready = True
        except Exception:
            self.stop()
            raise

    def _dump_startup_failure(
        self, *, lines: list[str], raw_text: str, resume: bool,
    ) -> None:
        tail_lines = [line for line in lines[-30:] if line.strip()]
        screen_dump = "\n".join(f"  | {line}" for line in tail_lines) or "  | (empty)"
        raw_tail = raw_text[-2000:] if raw_text else ""
        LOGGER.warning(
            "Startup timeout dump: persona=%s channel=%s resume=%s\n"
            "  Last screen lines (max 30 non-empty):\n%s\n"
            "  Raw tail (max 2000 chars): %r",
            self._persona.persona_id,
            self._channel_id,
            resume,
            screen_dump,
            raw_tail,
        )

    async def _send_input(self, text: str) -> None:
        assert self._vt is not None and self._activity_event is not None
        await self._dismiss_leftover_prompts()
        self._clear_local_screen()
        self._activity_event.clear()
        self._last_output_time = time.monotonic()
        for chunk in self._adapter.build_message_inputs(text):
            self._vt.write(chunk)
            await asyncio.sleep(0.1)

    async def _dismiss_leftover_prompts(self) -> None:
        """Dismiss interactive prompts (e.g. session feedback survey) left
        over from a previous turn. Claude shows a "How is Claude doing this
        session?" survey after rate-limit/error events; if it isn't dismissed
        it stays on-screen and contaminates the next response."""
        if self._vt is None:
            return
        for _attempt in range(3):
            lines = self._screen_lines()
            tail = " ".join(l.lower() for l in lines[-20:])
            if (
                "how is claude doing" not in tail
                and "0: dismiss" not in tail
                and "0:dismiss" not in tail
            ):
                return
            LOGGER.info(
                "Dismissing leftover chrome prompt (survey/feedback): persona=%s channel=%s",
                self._persona.persona_id, self._channel_id,
            )
            self._vt.write("\x1b")
            await asyncio.sleep(0.5)

    async def run_turn(
        self,
        *,
        author_name: str,
        author_id: int,
        raw_text: str,
        message_id: int,
    ) -> str:
        async with self._lock:
            await self.ensure_started()
            assert self._vt is not None
            assert self._activity_event is not None

            input_text = self._build_turn_input(
                author_name=author_name,
                author_id=author_id,
                raw_text=raw_text,
                message_id=message_id,
            )
            await self._send_input(input_text)

            response = ""
            lines: list[str] = []
            resent_after_empty = False
            for _round in range(_MAX_INTERACTIVE_ROUNDS):
                await self._wait_for_response()
                lines = self._screen_lines()
                response = self._adapter.extract_response(lines).strip()

                # Empty response: idle may have fired during the API latency
                # gap right after the input echo rendered. Wait one more idle
                # window and re-extract; if still empty, resend the input once.
                if not response and _round == 0:
                    LOGGER.info(
                        "Empty response — retrying after idle delay (premature idle?): persona=%s msg=%s",
                        self._persona.persona_id, message_id,
                    )
                    await asyncio.sleep(self._llm.idle_seconds + 2.0)
                    lines = self._screen_lines()
                    response = self._adapter.extract_response(lines).strip()
                    if not response and not resent_after_empty:
                        if self._vt is None or not self._vt.is_alive():
                            raise RuntimeError(
                                f"{self._llm.provider} session died during response"
                            )
                        LOGGER.warning(
                            "Empty response persisted after idle retry; resending turn once: persona=%s msg=%s",
                            self._persona.persona_id, message_id,
                        )
                        resent_after_empty = True
                        await self._send_input(input_text)
                        continue

                # Adapter says output is still streaming — wait for stabilization.
                pending_checks = 0
                while self._adapter.has_pending_output(lines) and pending_checks < 4:
                    LOGGER.info(
                        "Provider output still pending after idle; waiting for stabilization"
                    )
                    await asyncio.sleep(1.0)
                    lines = self._screen_lines()
                    newer = self._adapter.extract_response(lines).strip()
                    if newer:
                        response = newer
                    pending_checks += 1

                # Control-only output (ANSI cursor moves etc.) is invalid.
                if response and self._adapter.is_control_output(response):
                    LOGGER.warning(
                        "Control-only output detected; ignoring as final response: persona=%s msg=%s",
                        self._persona.persona_id, message_id,
                    )
                    await asyncio.sleep(self._llm.idle_seconds + 2.0)
                    lines = self._screen_lines()
                    response = self._adapter.extract_response(lines).strip()
                    if response and self._adapter.is_control_output(response):
                        response = ""

                # Application-specific "looks incomplete" retry (JSON envelope
                # fallback markers, partial CLI banners, etc.).
                inc_retries = 0
                while self._response_looks_incomplete(response) and inc_retries < 4:
                    await asyncio.sleep(self._llm.idle_seconds + 2.0)
                    lines = self._screen_lines()
                    response = self._adapter.extract_response(lines).strip()
                    inc_retries += 1

                if not response:
                    raise RuntimeError("Empty response after retries")

                break

            response = self._sanitize_response(response)

            resolved_id = self._resolve_resume_id(lines, allow_disk_fallback=True)
            self._set_cli_session_id(resolved_id)
            self._set_last_message_id(message_id)
            return response

    async def _wait_for_response(self) -> None:
        assert self._activity_event is not None
        deadline = time.monotonic() + self._llm.response_timeout

        while True:
            if self._vt is None or not self._vt.is_alive():
                raise RuntimeError(f"{self._llm.provider} session died during response")
            if self._activity_event.is_set():
                break
            if time.monotonic() >= deadline:
                raise TimeoutError("No PTY activity after sending message")
            await asyncio.sleep(0.2)

        while True:
            if self._vt is None or not self._vt.is_alive():
                raise RuntimeError(f"{self._llm.provider} session died during response")
            if time.monotonic() >= deadline:
                raise TimeoutError(f"{self._llm.provider} response timed out")
            if time.monotonic() - self._last_output_time >= self._llm.idle_seconds:
                return
            await asyncio.sleep(0.25)

    def stop(self) -> None:
        self._ready = False
        if self._vt is not None:
            self._vt.stop()
            self._vt = None
