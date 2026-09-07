from __future__ import annotations

import json
import logging
import re
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..models import ProviderState

LOGGER = logging.getLogger(__name__)


@dataclass
class ChannelMessage:
    """A single message in the context stream."""
    message_id: str
    author_name: str
    text: str
    author_id: str | None = None
    reply_to: str | None = None
    provider_id: str | None = None


@dataclass
class PersonaState:
    """Per-persona state within a channel."""
    protagonist_id: str | None = None
    command_access: str = "public"  # "public" or "protagonist"
    panel_message_id: int | None = None
    active_provider: str = "claude"
    providers: dict[str, ProviderState] = field(default_factory=dict)

    def get_provider(
        self,
        provider_id: str,
        default_mode: int = 3,
        default_state: ProviderState | None = None,
    ) -> ProviderState:
        if provider_id not in self.providers:
            if default_state is not None:
                self.providers[provider_id] = ProviderState(**asdict(default_state))
            else:
                self.providers[provider_id] = ProviderState(mode=default_mode)
        return self.providers[provider_id]


@dataclass
class ChannelState:
    """All state for a single (server, channel) pair."""
    stream: deque = field(default_factory=lambda: deque(maxlen=20))
    personas: dict[str, PersonaState] = field(default_factory=dict)
    workspace_rel: str = ""
    default_mode: int | None = None
    default_command_access: str | None = None
    default_provider: str | None = None
    default_persona: str | None = None  # explicit default persona for @bot mentions
    provider_defaults: dict[str, ProviderState] = field(default_factory=dict)

    def get_persona(
        self,
        persona_id: str,
        default_provider: str = "claude",
        default_command_access: str = "public",
    ) -> PersonaState:
        if persona_id not in self.personas:
            ps = PersonaState(
                command_access=self.default_command_access or default_command_access,
                active_provider=self.default_provider or default_provider,
            )
            for provider_id, provider_state in self.provider_defaults.items():
                ps.providers[provider_id] = ProviderState(**asdict(provider_state))
            self.personas[persona_id] = ps
        return self.personas[persona_id]


class ChannelManager:
    """Centralized per-channel state manager.

    Manages: context stream, protagonist, mode, CLI session IDs,
    greeting/death notification state — all keyed by (server, channel).
    """

    STREAM_MAX_MESSAGES = 20
    STREAM_MAX_CHARS_PER_MSG = 500

    _LEGACY_MODE_MAP: dict[str, int] = {"persona": 5, "tend": 2, "core": 1}

    def __init__(
        self,
        bridge_root: Path,
        default_mode: int = 3,
        default_provider: str = "claude",
        command_access: str = "public",
        default_provider_states: dict[str, ProviderState] | None = None,
    ) -> None:
        self._root = bridge_root
        self._default_mode = default_mode
        self._default_provider = default_provider
        self._default_command_access = command_access
        self._default_provider_states = {
            provider_id: self._copy_provider_state(provider_state)
            for provider_id, provider_state in (default_provider_states or {}).items()
        }
        self._channels: dict[tuple[str, int], ChannelState] = {}

    def _get(self, server_id: str, channel_id: int) -> ChannelState:
        key = (server_id, channel_id)
        if key not in self._channels:
            state = ChannelState(
                stream=deque(maxlen=self.STREAM_MAX_MESSAGES),
                default_mode=self._default_mode,
                default_command_access=self._default_command_access,
                default_provider=self._default_provider,
                provider_defaults={
                    provider_id: self._copy_provider_state(provider_state)
                    for provider_id, provider_state
                    in self._default_provider_states.items()
                },
            )
            self._channels[key] = state
            # Load persisted state from disk
            self._load_state(server_id, channel_id, state)
        return self._channels[key]

    @staticmethod
    def _copy_provider_state(provider_state: ProviderState) -> ProviderState:
        return ProviderState(**asdict(provider_state))

    # ------------------------------------------------------------------
    # Context stream
    # ------------------------------------------------------------------

    def append_message(
        self, server_id: str, channel_id: int, msg: ChannelMessage,
    ) -> None:
        state = self._get(server_id, channel_id)
        # Dedup
        if state.stream and state.stream[-1].message_id == msg.message_id:
            return
        # Truncate text at storage time
        if len(msg.text) > self.STREAM_MAX_CHARS_PER_MSG:
            msg = ChannelMessage(
                message_id=msg.message_id,
                author_name=msg.author_name,
                text=msg.text[:self.STREAM_MAX_CHARS_PER_MSG] + "...",
                author_id=msg.author_id,
                reply_to=msg.reply_to,
                provider_id=msg.provider_id,
            )
        state.stream.append(msg)

    def get_recent(
        self, server_id: str, channel_id: int, limit: int = 10,
    ) -> list[ChannelMessage]:
        state = self._get(server_id, channel_id)
        return list(state.stream)[-limit:]

    # Patterns for parsing log files back into ChannelMessages
    # [name @id]  or  @[trigger] [name @id]
    _USER_HEADER_RE = re.compile(r"^\[(.+?)\s+@(\d+)\]$")
    _BOT_HEADER_RE = re.compile(
        r"^@\[(\d+)\]\s+\[(.+?)(?:\|([a-z0-9_-]+))?(?:\s+@(\d+))?\]$",
    )

    def hydrate_from_logs(self, server_id: str, channel_id: int) -> int:
        """Restore context stream from disk logs on startup.

        Parses log files to reconstruct ChannelMessages with full metadata.
        Returns number of messages loaded.
        """
        ch_dir = self._root / server_id / str(channel_id)
        if not ch_dir.exists():
            return 0

        # Sort by filename (Discord snowflake = chronological)
        files = sorted(
            (f for f in ch_dir.glob("*.txt") if not f.name.startswith(".")),
            key=lambda f: f.name,
        )
        # Take last N
        files = files[-self.STREAM_MAX_MESSAGES:]

        count = 0
        for f in files:
            try:
                content = f.read_text(encoding="utf-8")
                msg = self._parse_log_file(f.stem, content)
                if msg:
                    self.append_message(server_id, channel_id, msg)
                    count += 1
            except Exception:
                continue

        if count:
            LOGGER.info(
                "Hydrated stream: %s/%d (%d msgs from disk)",
                server_id, channel_id, count,
            )
        return count

    def _parse_log_file(self, msg_id: str, content: str) -> ChannelMessage | None:
        """Parse a log file back into a ChannelMessage.

        User message:  [author @id]\\ntext
        Bot response:  @[trigger] [persona]\\ntext
        """
        if not content:
            return None

        first_line, _, rest = content.partition("\n")
        first_line = first_line.strip()

        # Bot response: @[trigger_id] [persona @bot_id]
        bot_match = self._BOT_HEADER_RE.match(first_line)
        if bot_match:
            return ChannelMessage(
                message_id=msg_id,
                author_name=bot_match.group(2),
                text=rest.strip(),
                author_id=bot_match.group(4),  # bot_id (may be None for old logs)
                reply_to=bot_match.group(1),
                provider_id=bot_match.group(3),
            )

        # User message: [name @id]
        user_match = self._USER_HEADER_RE.match(first_line)
        if user_match:
            return ChannelMessage(
                message_id=msg_id,
                author_name=user_match.group(1),
                text=rest.strip(),
                author_id=user_match.group(2),
            )

        return None

    # ------------------------------------------------------------------
    # Protagonist
    # ------------------------------------------------------------------

    def get_protagonist(
        self, server_id: str, channel_id: int, persona_id: str,
    ) -> str | None:
        return self._get(server_id, channel_id).get_persona(
            persona_id,
            self._default_provider,
            self._default_command_access,
        ).protagonist_id

    def set_protagonist(
        self, server_id: str, channel_id: int, persona_id: str, user_id: str,
    ) -> None:
        ps = self._get(server_id, channel_id).get_persona(
            persona_id,
            self._default_provider,
            self._default_command_access,
        )
        if ps.protagonist_id != user_id:
            ps.protagonist_id = user_id
            self._save_state(server_id, channel_id)
            LOGGER.info("Protagonist set: %s ch=%d → <@%s>", persona_id, channel_id, user_id)

    # ------------------------------------------------------------------
    # Mode
    # ------------------------------------------------------------------

    def get_mode(
        self, server_id: str, channel_id: int, persona_id: str, provider_id: str,
    ) -> int:
        return self.get_provider_state(server_id, channel_id, persona_id, provider_id).mode

    def set_mode(
        self, server_id: str, channel_id: int,
        persona_id: str, provider_id: str, mode: int,
    ) -> None:
        self.get_provider_state(server_id, channel_id, persona_id, provider_id).mode = mode
        self._save_state(server_id, channel_id)

    # ------------------------------------------------------------------
    # CLI session ID
    # ------------------------------------------------------------------

    def get_cli_session_id(
        self, server_id: str, channel_id: int, persona_id: str, provider_id: str,
    ) -> str | None:
        return self.get_provider_state(
            server_id, channel_id, persona_id, provider_id,
        ).cli_session_id

    def set_cli_session_id(
        self, server_id: str, channel_id: int,
        persona_id: str, provider_id: str, sid: str,
    ) -> None:
        self.get_provider_state(
            server_id, channel_id, persona_id, provider_id,
        ).cli_session_id = sid
        self._save_state(server_id, channel_id)

    # ------------------------------------------------------------------
    # Panel state
    # ------------------------------------------------------------------

    def get_persona_state(
        self, server_id: str, channel_id: int, persona_id: str,
    ) -> PersonaState:
        state = self._get(server_id, channel_id)
        return state.get_persona(
            persona_id,
            self._default_provider,
            self._default_command_access,
        )

    def get_provider_state(
        self,
        server_id: str,
        channel_id: int,
        persona_id: str,
        provider_id: str,
    ) -> ProviderState:
        state = self._get(server_id, channel_id)
        ps = state.get_persona(
            persona_id,
            self._default_provider,
            self._default_command_access,
        )
        return ps.get_provider(
            provider_id,
            state.default_mode or self._default_mode,
            state.provider_defaults.get(provider_id),
        )

    def get_active_provider(
        self, server_id: str, channel_id: int, persona_id: str,
    ) -> str:
        return self.get_persona_state(server_id, channel_id, persona_id).active_provider

    def set_active_provider(
        self, server_id: str, channel_id: int, persona_id: str, provider_id: str,
    ) -> None:
        ps = self.get_persona_state(server_id, channel_id, persona_id)
        ps.active_provider = provider_id
        state = self._get(server_id, channel_id)
        ps.get_provider(
            provider_id,
            state.default_mode or self._default_mode,
            state.provider_defaults.get(provider_id),
        )
        self._save_state(server_id, channel_id)

    def get_workspace_rel(self, server_id: str, channel_id: int) -> str:
        return self._get(server_id, channel_id).workspace_rel

    def set_workspace_rel(
        self, server_id: str, channel_id: int, workspace_rel: str,
    ) -> None:
        state = self._get(server_id, channel_id)
        normalized = workspace_rel.strip().replace("\\", "/").strip("/")
        state.workspace_rel = normalized
        self._save_state(server_id, channel_id)

    def configure_channel_defaults(
        self,
        server_id: str,
        channel_id: int,
        persona_ids: list[str],
        *,
        workspace_rel: str | None = None,
        mode: int | None = None,
        command_access: str | None = None,
        active_provider: str | None = None,
        target_provider: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        permission: str | None = None,
        fast: bool | None = None,
    ) -> None:
        state = self._get(server_id, channel_id)

        if workspace_rel is not None:
            state.workspace_rel = workspace_rel.strip().replace("\\", "/").strip("/")
        if mode is not None:
            state.default_mode = mode
            for provider_state in state.provider_defaults.values():
                provider_state.mode = mode
        if command_access is not None:
            state.default_command_access = command_access
        if active_provider is not None:
            state.default_provider = active_provider

        provider_id = target_provider or active_provider
        if provider_id:
            default_provider_state = self._copy_provider_state(
                state.provider_defaults.get(provider_id, ProviderState(
                    mode=state.default_mode or self._default_mode,
                ))
            )
            if mode is not None:
                default_provider_state.mode = mode
            if model is not None:
                default_provider_state.model = model
            if effort is not None:
                default_provider_state.effort = effort
            if permission is not None:
                default_provider_state.permission = permission
            if fast is not None:
                default_provider_state.fast = fast
            state.provider_defaults[provider_id] = default_provider_state

        for persona_id in persona_ids:
            ps = state.get_persona(
                persona_id,
                self._default_provider,
                self._default_command_access,
            )
            if command_access is not None:
                ps.command_access = command_access
            if active_provider is not None:
                ps.active_provider = active_provider
            if mode is not None:
                for existing_provider in ps.providers.values():
                    existing_provider.mode = mode
                    existing_provider.cli_session_id = None
            if provider_id:
                provider_state = ps.get_provider(
                    provider_id,
                    state.default_mode or self._default_mode,
                    state.provider_defaults.get(provider_id),
                )
                if mode is not None:
                    provider_state.mode = mode
                if model is not None:
                    provider_state.model = model
                if effort is not None:
                    provider_state.effort = effort
                if permission is not None:
                    provider_state.permission = permission
                if fast is not None:
                    provider_state.fast = fast
                if any(value is not None for value in (mode, model, effort, permission, fast)):
                    provider_state.cli_session_id = None

        self._save_state(server_id, channel_id)

    def set_panel_field(
        self, server_id: str, channel_id: int, persona_id: str,
        **kwargs: object,
    ) -> None:
        ps = self._get(server_id, channel_id).get_persona(persona_id)
        for key, value in kwargs.items():
            if hasattr(ps, key):
                setattr(ps, key, value)
        self._save_state(server_id, channel_id)

    # ------------------------------------------------------------------
    # Greeting / death notification
    # ------------------------------------------------------------------

    def is_greeted(self, channel_id: int, persona_id: str, provider_id: str) -> bool:
        for key, state in self._channels.items():
            if key[1] == channel_id:
                return state.get_persona(persona_id).get_provider(
                    provider_id, self._default_mode,
                ).greeted
        return False

    def mark_greeted(self, channel_id: int, persona_id: str, provider_id: str) -> None:
        for key, state in self._channels.items():
            if key[1] == channel_id:
                state.get_persona(persona_id).get_provider(
                    provider_id,
                    state.default_mode or self._default_mode,
                    state.provider_defaults.get(provider_id),
                ).greeted = True
                return

    def clear_greeted(self, channel_id: int, persona_id: str, provider_id: str) -> None:
        for key, state in self._channels.items():
            if key[1] == channel_id:
                state.get_persona(persona_id).get_provider(
                    provider_id,
                    state.default_mode or self._default_mode,
                    state.provider_defaults.get(provider_id),
                ).greeted = False

    def is_notified_dead(self, channel_id: int, persona_id: str, provider_id: str) -> bool:
        for key, state in self._channels.items():
            if key[1] == channel_id:
                return state.get_persona(persona_id).get_provider(
                    provider_id,
                    state.default_mode or self._default_mode,
                    state.provider_defaults.get(provider_id),
                ).notified_dead
        return False

    def mark_notified_dead(self, channel_id: int, persona_id: str, provider_id: str) -> None:
        for key, state in self._channels.items():
            if key[1] == channel_id:
                ps = state.get_persona(persona_id).get_provider(
                    provider_id,
                    state.default_mode or self._default_mode,
                    state.provider_defaults.get(provider_id),
                )
                ps.notified_dead = True
                ps.greeted = False  # re-greet after respawn

    def clear_notified_dead(self, channel_id: int, persona_id: str, provider_id: str) -> None:
        for key, state in self._channels.items():
            if key[1] == channel_id:
                state.get_persona(persona_id).get_provider(
                    provider_id,
                    state.default_mode or self._default_mode,
                    state.provider_defaults.get(provider_id),
                ).notified_dead = False

    # ------------------------------------------------------------------
    # Persistence (protagonist + cli_session_id)
    # ------------------------------------------------------------------

    def _state_path(self, server_id: str, channel_id: int) -> Path:
        return self._root / server_id / str(channel_id) / "state.json"

    def _save_state(self, server_id: str, channel_id: int) -> None:
        state = self._channels.get((server_id, channel_id))
        if not state:
            return
        data: dict[str, dict] = {
            "__channel__": {
                "workspace_rel": state.workspace_rel,
                "default_mode": state.default_mode or self._default_mode,
                "default_command_access": (
                    state.default_command_access or self._default_command_access
                ),
                "default_provider": (
                    state.default_provider or self._default_provider
                ),
                "default_persona": state.default_persona,
                "provider_defaults": {
                    provider_id: {
                        "mode": provider_state.mode,
                        "model": provider_state.model,
                        "effort": provider_state.effort,
                        "permission": provider_state.permission,
                        "fast": provider_state.fast,
                    }
                    for provider_id, provider_state in state.provider_defaults.items()
                },
            },
        }
        for pid, ps in state.personas.items():
            data[pid] = {
                "protagonist_id": ps.protagonist_id,
                "command_access": ps.command_access,
                "panel_message_id": ps.panel_message_id,
                "active_provider": ps.active_provider,
                "providers": {
                    provider_id: {
                        "cli_session_id": provider_state.cli_session_id,
                        "mode": provider_state.mode,
                        "greeted": provider_state.greeted,
                        "notified_dead": provider_state.notified_dead,
                        "model": provider_state.model,
                        "effort": provider_state.effort,
                        "permission": provider_state.permission,
                        "fast": provider_state.fast,
                        "last_seen_msg_id": provider_state.last_seen_msg_id,
                    }
                    for provider_id, provider_state in ps.providers.items()
                },
            }
        path = self._state_path(server_id, channel_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _load_state(self, server_id: str, channel_id: int, state: ChannelState) -> None:
        path = self._state_path(server_id, channel_id)
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            channel_info = data.get("__channel__", {})
            if isinstance(channel_info, dict):
                state.workspace_rel = channel_info.get("workspace_rel", "")
                raw_default_mode = channel_info.get(
                    "default_mode", self._default_mode,
                )
                if isinstance(raw_default_mode, str):
                    state.default_mode = (
                        int(raw_default_mode) if raw_default_mode.isdigit()
                        else self._LEGACY_MODE_MAP.get(
                            raw_default_mode, self._default_mode,
                        )
                    )
                else:
                    state.default_mode = int(raw_default_mode)
                state.default_command_access = channel_info.get(
                    "default_command_access", self._default_command_access,
                )
                state.default_provider = channel_info.get(
                    "default_provider", self._default_provider,
                )
                state.default_persona = channel_info.get("default_persona")
                provider_defaults = channel_info.get("provider_defaults", {})
                if isinstance(provider_defaults, dict):
                    for provider_id, provider_info in provider_defaults.items():
                        provider_state = ProviderState(
                            mode=state.default_mode or self._default_mode,
                        )
                        raw_mode = provider_info.get(
                            "mode", state.default_mode or self._default_mode,
                        )
                        if isinstance(raw_mode, str):
                            provider_state.mode = (
                                int(raw_mode) if raw_mode.isdigit()
                                else self._LEGACY_MODE_MAP.get(
                                    raw_mode,
                                    state.default_mode or self._default_mode,
                                )
                            )
                        else:
                            provider_state.mode = int(raw_mode)
                        model = provider_info.get("model")
                        if model and model != "default":
                            provider_state.model = model
                        effort = provider_info.get("effort")
                        if effort and effort != "default":
                            provider_state.effort = effort
                        permission = provider_info.get("permission")
                        if permission and permission != "default":
                            provider_state.permission = permission
                        provider_state.fast = provider_info.get("fast", False)
                        state.provider_defaults[provider_id] = provider_state
            for pid, info in data.items():
                if pid == "__channel__":
                    continue
                ps = state.get_persona(pid)
                ps.protagonist_id = info.get("protagonist_id")
                if info.get("command_access"):
                    ps.command_access = info["command_access"]
                ps.panel_message_id = info.get("panel_message_id")
                ps.active_provider = info.get(
                    "active_provider", state.default_provider or self._default_provider,
                )

                providers = info.get("providers")
                if isinstance(providers, dict):
                    for provider_id, provider_info in providers.items():
                        provider_state = ps.get_provider(
                            provider_id,
                            state.default_mode or self._default_mode,
                            state.provider_defaults.get(provider_id),
                        )
                        provider_state.cli_session_id = provider_info.get("cli_session_id")
                        raw_mode = provider_info.get(
                            "mode", state.default_mode or self._default_mode,
                        )
                        if isinstance(raw_mode, str):
                            provider_state.mode = (
                                int(raw_mode) if raw_mode.isdigit()
                                else self._LEGACY_MODE_MAP.get(
                                    raw_mode, state.default_mode or self._default_mode,
                                )
                            )
                        else:
                            provider_state.mode = int(raw_mode)
                        provider_state.greeted = provider_info.get("greeted", False)
                        provider_state.notified_dead = provider_info.get(
                            "notified_dead", False,
                        )
                        model = provider_info.get("model")
                        if model and model != "default":
                            provider_state.model = model
                        effort = provider_info.get("effort")
                        if effort and effort != "default":
                            provider_state.effort = effort
                        permission = provider_info.get("permission")
                        if permission and permission != "default":
                            provider_state.permission = permission
                        provider_state.fast = provider_info.get("fast", False)
                        provider_state.last_seen_msg_id = provider_info.get(
                            "last_seen_msg_id",
                        )
                    continue

                # Legacy flat Claude-only state migration
                legacy = ps.get_provider(
                    "claude",
                    state.default_mode or self._default_mode,
                    state.provider_defaults.get("claude"),
                )
                legacy.cli_session_id = info.get("cli_session_id")
                raw_mode = info.get("mode", state.default_mode or self._default_mode)
                if isinstance(raw_mode, str):
                    legacy.mode = (
                        int(raw_mode) if raw_mode.isdigit()
                        else self._LEGACY_MODE_MAP.get(
                            raw_mode, state.default_mode or self._default_mode,
                        )
                    )
                else:
                    legacy.mode = int(raw_mode)
                legacy.greeted = info.get("greeted", False)
                legacy.notified_dead = info.get("notified_dead", False)
                model = info.get("model")
                if model and model != "default":
                    legacy.model = model
                effort = info.get("effort")
                if effort and effort != "default":
                    legacy.effort = effort
                permission = info.get("permission")
                if permission and permission != "default":
                    legacy.permission = permission
                legacy.fast = info.get("fast", False)
                legacy.last_seen_msg_id = info.get("last_seen_msg_id")
                ps.active_provider = info.get(
                    "active_provider", state.default_provider or self._default_provider,
                )
            LOGGER.debug("Loaded channel state: %s/%d (%d personas)", server_id, channel_id, len(data))
        except Exception:
            LOGGER.warning("Failed to load channel state: %s/%d", server_id, channel_id)
