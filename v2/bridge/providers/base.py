from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from ..models import (
    InteractionPrompt,
    PersonaConfig,
    ProviderCapabilities,
    ProviderConfig,
    ProviderState,
    ResponseMeta,
)


class ProviderAdapter(ABC):
    provider_id: str

    @abstractmethod
    def capabilities(self) -> ProviderCapabilities:
        raise NotImplementedError

    @abstractmethod
    def build_spawn_command(
        self,
        *,
        provider_cfg: ProviderConfig,
        persona: PersonaConfig,
        workspace: str,
        prompt_file: Path,
        add_dirs: list[Path],
        resume_id: str | None,
        state: ProviderState | None = None,
        runtime_dir: Path | None = None,
    ) -> str:
        raise NotImplementedError

    def build_spawn_env(
        self,
        *,
        provider_cfg: ProviderConfig,
        persona: PersonaConfig,
        workspace: str,
        prompt_file: Path,
        state: ProviderState | None = None,
        runtime_dir: Path | None = None,
    ) -> dict[str, str] | None:
        return None

    @abstractmethod
    def build_bootstrap_prompt(
        self,
        *,
        persona_prompt: str,
    ) -> str | None:
        raise NotImplementedError

    @abstractmethod
    def ready_detector(self, lines: list[str]) -> bool:
        raise NotImplementedError

    def raw_ready_detector(self, raw_text: str) -> bool:
        return False

    def command_ready_detector(self, lines: list[str]) -> bool:
        return self.ready_detector(lines)

    @abstractmethod
    def extract_response(self, lines: list[str]) -> str:
        raise NotImplementedError

    @abstractmethod
    def extract_metadata(self, text: str) -> ResponseMeta:
        raise NotImplementedError

    @abstractmethod
    def detect_interaction(self, raw_lines: list[str]) -> InteractionPrompt | None:
        raise NotImplementedError

    @abstractmethod
    def encode_interaction_reply(
        self, prompt: InteractionPrompt, choice: str,
    ) -> str:
        raise NotImplementedError

    @abstractmethod
    def extract_resume_id(self, lines: list[str]) -> str | None:
        raise NotImplementedError

    def resolve_resume_id(
        self,
        *,
        lines: list[str],
        workspace: str,
        bootstrap_token: str | None = None,
        runtime_dir: Path | None = None,
    ) -> str | None:
        return self.extract_resume_id(lines)

    def supports_control(self, control: str) -> bool:
        caps = self.capabilities()
        mapping = {
            "model": caps.model_switch,
            "effort": caps.effort,
            "permission": caps.permission,
            "fast": caps.fast,
            "compact": caps.compact,
            "clear": caps.clear,
            "interrupt": caps.interrupt,
            "usage": caps.usage,
        }
        return mapping.get(control, False)

    def control_requires_restart(self, control: str) -> bool:
        return False

    def build_control_inputs(
        self,
        control: str,
        *,
        value: str | None = None,
        state: ProviderState | None = None,
    ) -> list[str]:
        return []

    def build_command_inputs(self, command: str) -> list[str]:
        return [command + "\r"]

    def usage_query_inputs(self) -> tuple[list[str], list[str]]:
        return ([], [])

    def parse_usage(self, lines: list[str]) -> dict[str, str]:
        return {}

    def build_message_inputs(self, text: str) -> list[str]:
        return [text + "\r"]

    def build_startup_inputs(self, state: ProviderState) -> list[str]:
        return []

    def detect_trust_prompt(self, lines: list[str]) -> str | None:
        """If the CLI shows a workspace trust prompt, return the keystroke to accept it."""
        return None

    def has_pending_output(self, lines: list[str]) -> bool:
        return False

    def is_control_output(self, text: str) -> bool:
        return False

    def internal_command_requires_ready_prompt(
        self,
        command: str,
        *,
        startup: bool = False,
    ) -> bool:
        return False

    def internal_command_stable_seconds(
        self,
        command: str,
        *,
        startup: bool = False,
    ) -> float:
        return 0.5

    def internal_command_timeout(
        self,
        command: str,
        *,
        startup: bool = False,
    ) -> float:
        return 30.0
