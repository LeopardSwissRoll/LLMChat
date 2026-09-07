from __future__ import annotations

import asyncio
import logging
import time

import discord

from .renderer import split_text_for_discord

LOGGER = logging.getLogger(__name__)


class DiscordStreamer:
    """Manages a Discord status message + final response delivery for one turn."""

    def __init__(
        self,
        source_message: discord.Message,
        edit_interval: float = 2.0,
    ) -> None:
        self.source = source_message
        self.edit_interval = edit_interval
        self._status_msg: discord.Message | None = None
        self._last_edit = 0.0

    # ------------------------------------------------------------------
    # Retry helper
    # ------------------------------------------------------------------

    async def _retry(self, name: str, func):
        delays = (0.5, 1.0, 2.0)
        last_err: discord.HTTPException | None = None
        for attempt, delay in enumerate(delays, 1):
            try:
                return await func()
            except discord.HTTPException as exc:
                last_err = exc
                if exc.status not in (429, 500, 502, 503, 504) or attempt == len(delays):
                    break
                wait = getattr(exc, "retry_after", None) or delay
                LOGGER.warning(
                    "Retry %s %s/%s after HTTP %s (%.1fs)",
                    name, attempt, len(delays), exc.status, float(wait),
                )
                await asyncio.sleep(float(wait))
        raise last_err

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self._status_msg is not None:
            return  # already started — idempotent
        self._status_msg = await self._retry(
            "start",
            lambda: self.source.reply("processing...", mention_author=False),
        )

    async def update(self, text: str) -> None:
        now = time.monotonic()
        if now - self._last_edit < self.edit_interval:
            return
        if not self._status_msg:
            return
        self._last_edit = now

        preview = text[:300] + "..." if len(text) > 300 else text
        content = f"streaming... ({len(text)} chars)\n```\n{preview}\n```"
        if len(content) > 2000:
            content = f"streaming... ({len(text)} chars captured)"
        try:
            await self._retry("update", lambda: self._status_msg.edit(content=content))
        except discord.HTTPException:
            LOGGER.warning("Failed to update streaming status")

    @property
    def status_message_id(self) -> str | None:
        """The Discord message ID of the "processing..." message."""
        return str(self._status_msg.id) if self._status_msg else None

    async def finalize(self, text: str) -> discord.Message:
        """Edit the processing message in-place with the final response.

        By editing instead of delete+create, the message ID stays the same.
        This is critical for pipe chains — replies to "processing..." resolve
        correctly because the log is saved under the same ID.
        """
        if not self._status_msg:
            raise RuntimeError("Streamer not started")

        chunks = split_text_for_discord(text, limit=1900)
        self._sent_chunks: list[tuple[discord.Message, str]] = []

        # First chunk: edit the processing message in-place (preserves ID)
        first_msg = self._status_msg
        await self._retry(
            "edit_final",
            lambda c=chunks[0]: first_msg.edit(content=c),
        )
        self._sent_chunks.append((first_msg, chunks[0]))

        # Continuation chunks: new messages
        for chunk in chunks[1:]:
            sent = await self._retry(
                "send",
                lambda c=chunk: self.source.channel.send(c),
            )
            self._sent_chunks.append((sent, chunk))

        return first_msg

    @property
    def continuation_chunks(self) -> list[tuple[str, str]]:
        """Return [(message_id, chunk_text)] for chunks after the first."""
        if len(self._sent_chunks) <= 1:
            return []
        return [(str(msg.id), text) for msg, text in self._sent_chunks[1:]]

    async def fail(self, error: str) -> None:
        if self._status_msg:
            try:
                await self._retry(
                    "fail",
                    lambda: self._status_msg.edit(content=f"failed: {error}"),
                )
            except discord.HTTPException:
                LOGGER.exception("Failed to update failure status")


class WebhookStreamer:
    """Manages a webhook-based status message + final response for one turn.

    Same public interface as ``DiscordStreamer`` but sends via Discord webhook
    so each persona can have its own display name and avatar.

    Note: Webhook messages do NOT support Discord reply chains
    (``message_reference``).  This is an accepted trade-off — pipe chains
    use log files, not reply references.

    If the webhook becomes invalid (deleted externally), the
    *on_webhook_invalid* callback is invoked to refresh it.  After the
    callback returns a new webhook the streamer retries the failed
    operation once.
    """

    def __init__(
        self,
        webhook: discord.Webhook,
        source_message: discord.Message,
        persona_name: str,
        avatar_url: str | None = None,
        edit_interval: float = 2.0,
        on_webhook_invalid: "Callable[[], Awaitable[discord.Webhook]] | None" = None,
    ) -> None:
        self._webhook = webhook
        self.source = source_message
        self._persona_name = persona_name
        self._avatar_url = avatar_url
        self.edit_interval = edit_interval
        self._on_webhook_invalid = on_webhook_invalid
        self._status_msg: discord.WebhookMessage | None = None
        self._last_edit = 0.0
        self._sent_chunks: list[tuple[discord.WebhookMessage, str]] = []

        # Thread context: if source message is in a thread, webhook
        # send/edit must specify thread= so messages land in the
        # correct thread rather than the parent channel.
        self._thread: discord.Thread | None = None
        ch = source_message.channel
        if isinstance(ch, discord.Thread):
            self._thread = ch

    # ------------------------------------------------------------------
    # Retry helper (shared logic with DiscordStreamer)
    # ------------------------------------------------------------------

    async def _retry(self, name: str, func):
        delays = (0.5, 1.0, 2.0)
        last_err: discord.HTTPException | None = None
        for attempt, delay in enumerate(delays, 1):
            try:
                return await func()
            except discord.NotFound:
                # Webhook was deleted — try to refresh once
                if self._on_webhook_invalid and attempt == 1:
                    LOGGER.warning(
                        "Webhook NotFound during %s — refreshing", name,
                    )
                    try:
                        self._webhook = await self._on_webhook_invalid()
                        continue  # retry with new webhook
                    except Exception:
                        LOGGER.warning("Webhook refresh failed")
                raise
            except discord.HTTPException as exc:
                last_err = exc
                if exc.status not in (429, 500, 502, 503, 504) or attempt == len(delays):
                    break
                wait = getattr(exc, "retry_after", None) or delay
                LOGGER.warning(
                    "Retry %s %s/%s after HTTP %s (%.1fs)",
                    name, attempt, len(delays), exc.status, float(wait),
                )
                await asyncio.sleep(float(wait))
        raise last_err

    # ------------------------------------------------------------------
    # Webhook send/edit helpers
    # ------------------------------------------------------------------

    async def _webhook_send(self, content: str) -> discord.WebhookMessage:
        """Send a new webhook message with persona identity."""
        kwargs: dict = {
            "content": content,
            "username": self._persona_name,
            "avatar_url": self._avatar_url,
            "wait": True,
        }
        if self._thread is not None:
            kwargs["thread"] = self._thread
        return await self._retry(
            "webhook_send",
            lambda: self._webhook.send(**kwargs),
        )

    async def _webhook_edit(self, message_id: int, content: str) -> None:
        """Edit an existing webhook message."""
        kwargs: dict = {"message_id": message_id, "content": content}
        if self._thread is not None:
            kwargs["thread"] = self._thread
        await self._retry(
            "webhook_edit",
            lambda: self._webhook.edit_message(**kwargs),
        )

    # ------------------------------------------------------------------
    # Public API (matches DiscordStreamer interface)
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self._status_msg is not None:
            return  # idempotent
        self._status_msg = await self._webhook_send("processing...")

    async def update(self, text: str) -> None:
        now = time.monotonic()
        if now - self._last_edit < self.edit_interval:
            return
        if not self._status_msg:
            return
        self._last_edit = now

        preview = text[:300] + "..." if len(text) > 300 else text
        content = f"streaming... ({len(text)} chars)\n```\n{preview}\n```"
        if len(content) > 2000:
            content = f"streaming... ({len(text)} chars captured)"
        try:
            await self._webhook_edit(self._status_msg.id, content)
        except discord.HTTPException:
            LOGGER.warning("Failed to update webhook streaming status")

    @property
    def status_message_id(self) -> str | None:
        return str(self._status_msg.id) if self._status_msg else None

    async def finalize(self, text: str) -> discord.WebhookMessage:
        """Edit processing message in-place, send continuations via webhook."""
        if not self._status_msg:
            raise RuntimeError("WebhookStreamer not started")

        chunks = split_text_for_discord(text, limit=1900)
        self._sent_chunks = []

        # First chunk: edit in-place (preserves message ID)
        first_msg = self._status_msg
        await self._webhook_edit(first_msg.id, chunks[0])
        self._sent_chunks.append((first_msg, chunks[0]))

        # Continuation chunks: new webhook messages
        for chunk in chunks[1:]:
            sent = await self._webhook_send(chunk)
            self._sent_chunks.append((sent, chunk))

        return first_msg

    @property
    def continuation_chunks(self) -> list[tuple[str, str]]:
        if len(self._sent_chunks) <= 1:
            return []
        return [(str(msg.id), text) for msg, text in self._sent_chunks[1:]]

    async def fail(self, error: str) -> None:
        if self._status_msg:
            try:
                await self._webhook_edit(
                    self._status_msg.id, f"failed: {error}",
                )
            except discord.HTTPException:
                LOGGER.exception("Failed to update webhook failure status")
