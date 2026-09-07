from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ..models import ResponseMeta


@dataclass
class IncomingMessage:
    message_id: str        # Discord msg ID, CLI turn#, SSH session#
    server_id: str         # Discord guild ID, CLI/DM="0"
    channel_id: int        # Discord channel, CLI=0
    author_id: str         # Discord user ID, CLI="0"
    author_name: str
    text: str
    workspace: str
    persona_id: str
    provider_id: str
    reply_to_msg_id: str | None = None
    visible_message_id: str | None = None
    trigger_reason: str = "message"
    attachment_paths: tuple[str, ...] = ()


@runtime_checkable
class OutputSink(Protocol):
    """Protocol for delivering responses back to the user."""

    async def begin(self, context: str) -> None:
        """Called when processing starts."""
        ...

    async def stream_update(self, text: str) -> None:
        """Called periodically with partial response text."""
        ...

    async def finalize(self, text: str, meta: ResponseMeta) -> str | None:
        """Called when the full response is ready. Returns response message ID if available."""
        ...

    async def fail(self, error: str) -> None:
        """Called on error."""
        ...

    @property
    def status_message_id(self) -> str | None:
        """The visible message ID of the "processing..." message, if available."""
        return None

    # Optional interactive prompt support (Phase 2).
    # Implementations that don't support these should either omit them
    # or raise NotImplementedError.  TurnExecutor checks via hasattr().

    async def send_choices(self, text: str, choices: list[str]) -> str:
        """Present choices (e.g. permission prompt) and return the selected label."""
        raise NotImplementedError

    async def ask_user(self, text: str) -> str:
        """Display a question and return the user's text reply."""
        raise NotImplementedError


class TransportAdapter(ABC):
    """Base class for transport implementations (Discord, CLI, SSH, etc.)."""

    @abstractmethod
    async def start(self) -> None:
        """Start the transport (connect, listen, etc.)."""
        ...

    @abstractmethod
    async def stop(self) -> None:
        """Gracefully shut down the transport."""
        ...
