from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

PERSONA_LV_NAMES: dict[int, str] = {
    1: "core",
    2: "soft",
    3: "medium",
    4: "hard",
    5: "masquerade",
}


@dataclass(frozen=True)
class PersonaConfig:
    persona_id: str
    display_name: str
    role_aliases: tuple[str, ...]
    identity_text: str
    prompt_dir: Path  # relative to v2/prompts, e.g. "aris"
    avatar_url: str | None = None  # webhook avatar
    enabled_providers: tuple[str, ...] = ("claude", "codex")

    # PTY / transport settings (defaults from global, overridable per-persona)
    pty_rows: int = 50
    pty_cols: int = 200
    message_edit_interval: float = 2.0
    response_timeout: float = 1800.0
    idle_seconds: float = 3.0


@dataclass(frozen=True)
class ProviderCapabilities:
    model_switch: bool = False
    effort: bool = False
    permission: bool = False
    fast: bool = False
    compact: bool = False
    clear: bool = False
    interrupt: bool = True
    usage: bool = False
    interactive_prompts: bool = False


@dataclass
class ProviderState:
    cli_session_id: str | None = None
    mode: int = 3
    greeted: bool = False
    notified_dead: bool = False
    model: str = "default"
    effort: str = "default"
    permission: str = "default"
    fast: bool = False
    last_seen_msg_id: str | None = None


@dataclass(frozen=True)
class ProviderConfig:
    provider_id: str
    cli_executable: str
    cli_args: tuple[str, ...]
    enabled: bool = True
    default_capabilities: ProviderCapabilities = field(
        default_factory=ProviderCapabilities,
    )
    default_state: ProviderState = field(default_factory=ProviderState)


@dataclass(frozen=True)
class SessionKey:
    persona_id: str
    provider_id: str
    channel_id: int
    workspace: str

    @property
    def session_id(self) -> str:
        raw = f"{self.workspace}:{self.channel_id}:{self.persona_id}:{self.provider_id}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]


@dataclass
class ResponseMeta:
    opening_line: str = ""
    closing_line: str = ""


@dataclass(frozen=True)
class InteractionPrompt:
    kind: str
    text: str
    choices: tuple[str, ...] = ()


@dataclass
class TurnRecord:
    message_id: str
    channel_id: int
    persona_id: str
    provider_id: str
    workspace: str
    response_text: str
    meta: ResponseMeta = field(default_factory=ResponseMeta)


@dataclass
class BridgeSettings:
    """Global bridge settings (non-persona-specific)."""
    default_workspace: Path
    bot_token: str = ""  # single Discord bot token
    bot_id: str = ""     # resolved at startup via Discord API
    active_personas: list[str] = field(default_factory=list)
    personas: dict[str, PersonaConfig] = field(default_factory=dict)
    providers: dict[str, ProviderConfig] = field(default_factory=dict)
    default_provider: str = "claude"
    log_level: str = "INFO"

    # Mode / --add-dir configuration
    default_mode: int = 3
    command_access: str = "public"  # "public" or "protagonist"
    lv1_add_dirs: tuple[str, ...] = ("channel", "persona")
    default_add_dirs: tuple[str, ...] = ("channel", "users", "persona")
