from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path

from ..models import PersonaConfig, ProviderConfig, SessionKey
from ..pty.handler import PtySession, PtyState
from ..providers import ProviderAdapter
from .channel_manager import ChannelManager
from .log_store import LogStore
from .prompt_composer import PromptComposer

LOGGER = logging.getLogger(__name__)


class SessionRegistry:
    """Manages SessionKey → PtySession mapping."""

    def __init__(
        self,
        prompt_composer: PromptComposer,
        log_store: LogStore,
        channel_mgr: ChannelManager,
        personas: dict[str, PersonaConfig],
        providers: dict[str, ProviderConfig],
        adapters: dict[str, ProviderAdapter],
        loop: asyncio.AbstractEventLoop,
        mode_dirs: dict[int | str, tuple[str, ...]] | None = None,
    ) -> None:
        self._prompt_composer = prompt_composer
        self._log_store = log_store
        self._cm = channel_mgr
        self._personas = personas
        self._providers = providers
        self._adapters = adapters
        self._loop = loop
        self._mode_dirs = mode_dirs or {
            "persona": ("channel", "users", "persona"),
            "tend": ("channel", "users", "persona"),
            "core": ("channel", "persona"),
        }
        self._sessions: dict[SessionKey, PtySession] = {}
        self._server_ids: dict[SessionKey, str] = {}
        self._spawn_locks: dict[SessionKey, asyncio.Lock] = {}
        self._applied_states: dict[SessionKey, tuple[int, str, str, str, bool]] = {}

    def _get_spawn_lock(self, key: SessionKey) -> asyncio.Lock:
        if key not in self._spawn_locks:
            self._spawn_locks[key] = asyncio.Lock()
        return self._spawn_locks[key]

    def get_provider_adapter(self, provider_id: str) -> ProviderAdapter:
        adapter = self._adapters.get(provider_id)
        if not adapter:
            raise ValueError(f"Unknown provider adapter: {provider_id}")
        return adapter

    def get_provider_config(self, provider_id: str) -> ProviderConfig:
        cfg = self._providers.get(provider_id)
        if not cfg:
            raise ValueError(f"Unknown provider config: {provider_id}")
        return cfg

    def get_session_mode(
        self, server_id: str, channel_id: int, persona_id: str, provider_id: str,
    ) -> int:
        return self._cm.get_mode(server_id, channel_id, persona_id, provider_id)

    def get_mode_dirs(self, mode: int | str) -> tuple[str, ...]:
        if mode in self._mode_dirs:
            return self._mode_dirs[mode]
        return self._mode_dirs.get("persona", ())

    def _desired_signature(
        self,
        server_id: str,
        key: SessionKey,
        mode: int | None = None,
    ) -> tuple[int, str, str, str, bool]:
        provider_state = self._cm.get_provider_state(
            server_id, key.channel_id, key.persona_id, key.provider_id,
        )
        return (
            mode if mode is not None else provider_state.mode,
            provider_state.model,
            provider_state.effort,
            provider_state.permission,
            provider_state.fast,
        )

    async def get_or_create(
        self,
        key: SessionKey,
        author_id: str | None = None,
        server_id: str = "0",
        mode: int | None = None,
    ) -> PtySession:
        lock = self._get_spawn_lock(key)
        async with lock:
            self.stop_other_provider_sessions(key)
            desired_signature = self._desired_signature(server_id, key, mode)
            self._server_ids[key] = server_id
            existing = self._sessions.get(key)
            if existing and existing.state != PtyState.DEAD:
                if self._applied_states.get(key) == desired_signature:
                    return existing
                LOGGER.info(
                    "Session config drift detected: provider=%s persona=%s channel=%d",
                    key.provider_id, key.persona_id, key.channel_id,
                )
                existing.stop()

            persona = self._personas.get(key.persona_id)
            if not persona:
                raise ValueError(f"Unknown persona: {key.persona_id}")
            if key.provider_id not in persona.enabled_providers:
                raise ValueError(
                    f"Persona {key.persona_id} does not support provider {key.provider_id}",
                )
            provider_cfg = self.get_provider_config(key.provider_id)
            adapter = self.get_provider_adapter(key.provider_id)

            protagonist = self._cm.get_protagonist(server_id, key.channel_id, key.persona_id)
            if not protagonist and author_id:
                protagonist = author_id
                self._cm.set_protagonist(server_id, key.channel_id, key.persona_id, author_id)

            effective_mode = mode or self._cm.get_mode(
                server_id, key.channel_id, key.persona_id, key.provider_id,
            )
            cli_sid = self._cm.get_cli_session_id(
                server_id, key.channel_id, key.persona_id, key.provider_id,
            )

            try:
                session = await self._spawn_session(
                    provider_cfg=provider_cfg,
                    adapter=adapter,
                    persona=persona,
                    key=key,
                    resume_id=cli_sid,
                    protagonist_id=protagonist,
                    mode=effective_mode,
                    server_id=server_id,
                )
            except Exception:
                if not cli_sid:
                    raise
                LOGGER.warning(
                    "Resume spawn failed; retrying fresh session: provider=%s persona=%s channel=%d resume_id=%s",
                    key.provider_id,
                    key.persona_id,
                    key.channel_id,
                    cli_sid,
                )
                self._cm.set_cli_session_id(
                    server_id, key.channel_id, key.persona_id, key.provider_id, None,
                )
                session = await self._spawn_session(
                    provider_cfg=provider_cfg,
                    adapter=adapter,
                    persona=persona,
                    key=key,
                    resume_id=None,
                    protagonist_id=protagonist,
                    mode=effective_mode,
                    server_id=server_id,
                )
            self._sessions[key] = session
            self._applied_states[key] = desired_signature
            self._cm.set_mode(
                server_id, key.channel_id, key.persona_id, key.provider_id, effective_mode,
            )
            return session

    async def _spawn_session(
        self,
        *,
        provider_cfg: ProviderConfig,
        adapter: ProviderAdapter,
        persona: PersonaConfig,
        key: SessionKey,
        resume_id: str | None = None,
        protagonist_id: str | None = None,
        mode: int = 3,
        server_id: str = "0",
    ) -> PtySession:
        prompt = self._prompt_composer.compose(
            key.provider_id, persona, protagonist_id, mode,
        )
        prompt_file = (
            self._log_store.channel_dir(server_id, key.channel_id)
            / f".prompt_{key.session_id}.md"
        )
        prompt_file.write_text(prompt, encoding="utf-8")

        dir_types = self.get_mode_dirs(mode)
        dir_map = {
            "channel": lambda: self._log_store.channel_dir(server_id, key.channel_id),
            "users": lambda: self._log_store.users_dir(server_id),
            "persona": lambda: self._prompt_composer.get_persona_dir(persona),
        }
        add_dirs: list[Path] = []
        for dir_type in dir_types:
            resolver = dir_map.get(dir_type)
            if resolver:
                add_dirs.append(resolver())

        runtime_dir = self._provider_runtime_dir(key, server_id)
        provider_state = self._cm.get_provider_state(
            server_id, key.channel_id, key.persona_id, key.provider_id,
        )
        spawn_env = adapter.build_spawn_env(
            provider_cfg=provider_cfg,
            persona=persona,
            workspace=key.workspace,
            prompt_file=prompt_file,
            state=(provider_state if resume_id is None else None),
            runtime_dir=runtime_dir,
        )
        command = adapter.build_spawn_command(
            provider_cfg=provider_cfg,
            persona=persona,
            workspace=key.workspace,
            prompt_file=prompt_file,
            add_dirs=add_dirs,
            resume_id=resume_id,
            state=(provider_state if resume_id is None else None),
            runtime_dir=runtime_dir,
        )
        session = PtySession(
            command=command,
            cwd=key.workspace,
            rows=persona.pty_rows,
            cols=persona.pty_cols,
            loop=self._loop,
            ready_detector=adapter.ready_detector,
            raw_ready_detector=adapter.raw_ready_detector,
            env=spawn_env,
        )
        session.spawn()

        # Handle workspace trust prompts before waiting for ready.
        # Some CLIs (Claude) ask "do you trust this folder?" on first use.
        trust_deadline = asyncio.get_event_loop().time() + 20
        while asyncio.get_event_loop().time() < trust_deadline:
            await asyncio.sleep(1)
            lines = session.get_screen_lines()
            raw_text = "".join(session.get_recent_raw_chunks())
            trust_key = adapter.detect_trust_prompt(lines)
            if not trust_key:
                detect_startup_prompt = getattr(adapter, "detect_startup_prompt", None)
                if callable(detect_startup_prompt):
                    try:
                        trust_key = detect_startup_prompt(raw_text)
                    except Exception:
                        trust_key = None
            if trust_key:
                LOGGER.info(
                    "Startup prompt detected, sending accept key: provider=%s persona=%s channel=%d",
                    key.provider_id,
                    key.persona_id,
                    key.channel_id,
                )
                session.proc.write(trust_key)
                await asyncio.sleep(2)
                continue
            if adapter.ready_detector(lines) or adapter.raw_ready_detector(raw_text):
                session.mark_ready()
                break  # already ready, no trust prompt needed

        await session.wait_ready(timeout=60)

        if resume_id is None:
            for raw in adapter.build_startup_inputs(provider_state):
                try:
                    await session.run_internal_command(
                        raw,
                        inputs=adapter.build_command_inputs(raw),
                        timeout=min(
                            adapter.internal_command_timeout(raw, startup=True),
                            persona.response_timeout,
                        ),
                        idle_seconds=min(2.0, persona.idle_seconds),
                        inter_input_delay=0.0,
                        settle_seconds=adapter.internal_command_stable_seconds(
                            raw, startup=True,
                        ),
                        capture_output=False,
                        require_ready_prompt=adapter.internal_command_requires_ready_prompt(
                            raw, startup=True,
                        ),
                        ready_timeout=min(
                            adapter.internal_command_timeout(raw, startup=True),
                            persona.response_timeout,
                        ),
                        ready_stable_seconds=adapter.internal_command_stable_seconds(
                            raw, startup=True,
                        ),
                        command_ready_detector=adapter.command_ready_detector,
                    )
                except Exception:
                    LOGGER.warning(
                        "Startup control failed: provider=%s persona=%s raw=%r",
                        key.provider_id, key.persona_id, raw,
                    )
                    break

        bootstrap_token: str | None = None
        bootstrap_prompt = None if resume_id else adapter.build_bootstrap_prompt(
            persona_prompt=prompt,
        )
        if bootstrap_prompt:
            bootstrap_token = f"[bridge-bootstrap:{key.session_id}]"
            hidden_prompt = f"{bootstrap_token}\n{bootstrap_prompt}"
            await session.send_message(hidden_prompt)
            await session.wait_response(
                timeout=persona.response_timeout,
                idle_seconds=persona.idle_seconds,
            )

        LOGGER.info(
            "Session spawned: provider=%s persona=%s channel=%d",
            key.provider_id, persona.persona_id, key.channel_id,
        )

        cli_sid = adapter.resolve_resume_id(
            lines=session.get_screen_lines(),
            workspace=key.workspace,
            bootstrap_token=bootstrap_token,
            runtime_dir=runtime_dir,
        )
        if cli_sid:
            self._cm.set_cli_session_id(
                server_id, key.channel_id, key.persona_id, key.provider_id, cli_sid,
            )
            LOGGER.info(
                "Captured CLI session ID for %s/%s: %s",
                key.persona_id, key.provider_id, cli_sid,
            )

        return session

    def get_session(self, key: SessionKey) -> PtySession | None:
        return self._sessions.get(key)

    def get_all_sessions(self) -> dict[SessionKey, PtySession]:
        return dict(self._sessions)

    def get_session_by_id(self, session_id: str) -> tuple[SessionKey, PtySession] | None:
        for key, session in self._sessions.items():
            if key.session_id == session_id:
                return key, session
        return None

    def stop_other_provider_sessions(self, key: SessionKey) -> int:
        stopped = 0
        siblings = [
            other_key
            for other_key in list(self._sessions.keys())
            if (
                other_key.persona_id == key.persona_id
                and other_key.channel_id == key.channel_id
                and other_key.workspace == key.workspace
                and other_key.provider_id != key.provider_id
            )
        ]
        for sibling_key in siblings:
            session = self._sessions.pop(sibling_key, None)
            self._server_ids.pop(sibling_key, None)
            self._applied_states.pop(sibling_key, None)
            if not session:
                continue
            try:
                session.stop()
            except Exception:
                LOGGER.warning("Failed to stop sibling session %s", sibling_key.session_id)
            stopped += 1
        return stopped

    async def restart_session(
        self, key: SessionKey, mode: int | None = None,
    ) -> PtySession:
        lock = self._get_spawn_lock(key)
        async with lock:
            self.stop_other_provider_sessions(key)
            server_id = self._server_ids.get(key, "0")
            desired_signature = self._desired_signature(server_id, key, mode)
            existing = self._sessions.get(key)
            if existing:
                existing.stop()

            persona = self._personas.get(key.persona_id)
            if not persona:
                raise ValueError(f"Unknown persona: {key.persona_id}")
            if key.provider_id not in persona.enabled_providers:
                raise ValueError(
                    f"Persona {key.persona_id} does not support provider {key.provider_id}",
                )
            provider_cfg = self.get_provider_config(key.provider_id)
            adapter = self.get_provider_adapter(key.provider_id)

            effective_mode = mode or self._cm.get_mode(
                server_id, key.channel_id, key.persona_id, key.provider_id,
            )
            protagonist = self._cm.get_protagonist(server_id, key.channel_id, key.persona_id)

            session = await self._spawn_session(
                provider_cfg=provider_cfg,
                adapter=adapter,
                persona=persona,
                key=key,
                resume_id=None,
                protagonist_id=protagonist,
                mode=effective_mode,
                server_id=server_id,
            )
            self._sessions[key] = session
            self._applied_states[key] = desired_signature
            self._cm.set_mode(
                server_id, key.channel_id, key.persona_id, key.provider_id, effective_mode,
            )
            return session

    def _provider_runtime_dir(self, key: SessionKey, server_id: str) -> Path | None:
        if key.provider_id != "codex":
            return None
        lane_home = (
            self._log_store.channel_dir(server_id, key.channel_id)
            / f".codex_home_{str(self._personas[key.persona_id].prompt_dir)}"
        )
        lane_home.mkdir(parents=True, exist_ok=True)
        default_home = Path.home() / ".codex"
        for filename in ("auth.json",):
            src = default_home / filename
            dest = lane_home / filename
            if src.exists() and not dest.exists():
                shutil.copy2(src, dest)
        return lane_home

    def stop_all(self) -> None:
        for key, session in self._sessions.items():
            try:
                session.stop()
            except Exception:
                LOGGER.warning("Failed to stop session %s", key.session_id)
        self._sessions.clear()
        self._applied_states.clear()
