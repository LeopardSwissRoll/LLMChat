from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


# ---------------------------------------------------------------------------
# Provider adapter contract types
# ---------------------------------------------------------------------------
# The provider adapters (providers/*) and PromptComposer receive these.
# Settings types further below are separate; PersonaSession constructs
# PersonaConfig / ProviderState on the fly when talking to adapters.


@dataclass(frozen=True)
class PersonaConfig:
    persona_id: str
    display_name: str
    role_aliases: tuple[str, ...]
    identity_text: str
    prompt_dir: Path  # relative to prompts_root, e.g. "example"
    avatar_url: str | None = None
    enabled_providers: tuple[str, ...] = ("claude", "codex")
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


@dataclass
class ResponseMeta:
    opening_line: str = ""
    closing_line: str = ""


@dataclass(frozen=True)
class InteractionPrompt:
    kind: str
    text: str
    choices: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# ExportV2 settings + runtime state
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LlmSettings:
    provider: str = "claude"
    cli_executable: str = "claude"
    cli_args: tuple[str, ...] = ()
    model: str = "default"
    effort: str = "default"
    permission: str = "bypass"
    fast: bool = False
    rows: int = 50
    cols: int = 200
    idle_seconds: float = 3.0
    startup_timeout: float = 60.0
    response_timeout: float = 1800.0


@dataclass(frozen=True)
class PersonaSettings:
    persona_id: str
    display_name: str
    prompt_dir: str
    avatar_url: str | None = None


@dataclass(frozen=True)
class ServerSettings:
    server_id: int
    workspace: Path
    personas: dict[str, PersonaSettings] = field(default_factory=dict)


@dataclass(frozen=True)
class AppSettings:
    bot_token: str
    data_root: Path
    bridge_root: Path
    prompts_root: Path
    llm: LlmSettings
    servers: dict[int, ServerSettings]


@dataclass
class PersonaChannelState:
    provider_id: str | None = None
    cli_session_id: str | None = None
    last_message_id: int | None = None


@dataclass
class ChannelRuntimeState:
    personas: dict[str, PersonaChannelState] = field(default_factory=dict)


@dataclass
class ServerRuntimeState:
    channels: dict[int, ChannelRuntimeState] = field(default_factory=dict)
