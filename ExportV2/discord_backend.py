from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import discord

from .logs import ServerStore
from .models import (
    AppSettings,
    ChannelRuntimeState,
    PersonaChannelState,
    ServerRuntimeState,
)
from .session import PersonaSession

LOGGER = logging.getLogger(__name__)


@dataclass
class _ServerRuntime:
    store: ServerStore
    state: ServerRuntimeState
    sessions: dict[tuple[int, str], PersonaSession]
    persona_role_ids: dict[str, int]
    webhook: dict[int, discord.Webhook]


class ExportV2DiscordClient(discord.Client):
    def __init__(self, settings: AppSettings) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True
        intents.messages = True
        super().__init__(intents=intents)
        self._settings = settings
        self._servers: dict[int, _ServerRuntime] = {}
        self._bot_mention_re: re.Pattern[str] | None = None

        for server_id, server in settings.servers.items():
            store = ServerStore(settings.data_root, settings.bridge_root, server_id)
            state = store.load_state()
            self._servers[server_id] = _ServerRuntime(
                store=store,
                state=state,
                sessions={},
                persona_role_ids={},
                webhook={},
            )

    async def setup_hook(self) -> None:
        self._bot_mention_re = re.compile(rf"<@!?{self.user.id}>") if self.user else None

    async def on_ready(self) -> None:
        LOGGER.info(
            "ExportV2 bot ready as %s | guilds=%s",
            self.user,
            [(g.id, g.name) for g in self.guilds],
        )
        configured = set(self._settings.servers.keys())
        joined = {g.id for g in self.guilds}
        missing = configured - joined
        unconfigured = joined - configured
        if missing:
            LOGGER.warning(
                "Configured server(s) not joined by bot: %s — invite the bot or fix server_id in settings.json",
                sorted(missing),
            )
        if unconfigured:
            LOGGER.info(
                "Bot is in unconfigured server(s) (will be ignored): %s",
                sorted(unconfigured),
            )
        await self._ensure_persona_roles()

    async def _ensure_persona_roles(self) -> None:
        for guild in self.guilds:
            server = self._settings.servers.get(guild.id)
            if server is None:
                continue
            runtime = self._servers[guild.id]
            existing = {role.name: role for role in guild.roles}
            for persona_id, persona in server.personas.items():
                role = existing.get(persona.display_name)
                if role is None:
                    try:
                        role = await guild.create_role(
                            name=persona.display_name,
                            mentionable=True,
                            reason=f"ExportV2 persona: {persona_id}",
                        )
                        LOGGER.info(
                            "Created role '%s' for persona '%s' in '%s'",
                            persona.display_name, persona_id, guild.name,
                        )
                    except discord.Forbidden:
                        LOGGER.warning(
                            "Cannot create role '%s' in '%s' — move the bot role higher in Server Settings → Roles",
                            persona.display_name, guild.name,
                        )
                        continue
                    except discord.HTTPException as exc:
                        LOGGER.warning(
                            "Failed to create role '%s': %s", persona.display_name, exc,
                        )
                        continue
                runtime.persona_role_ids[persona_id] = role.id

    async def close(self) -> None:
        for runtime in self._servers.values():
            runtime.store.save_state(runtime.state)
            for session in runtime.sessions.values():
                session.stop()
        await super().close()

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.webhook_id:
            return
        if message.guild is None:
            return

        runtime = self._servers.get(message.guild.id)
        if runtime is None:
            return
        server = self._settings.servers[message.guild.id]

        targets = self._resolve_targets(message)
        if not targets:
            return

        clean_text = self._strip_mentions(message.content, server)
        if not clean_text:
            return

        author_name = getattr(message.author, "display_name", message.author.name)
        channel_id = message.channel.id

        runtime.store.save_user_message(
            channel_id=channel_id,
            message_id=message.id,
            author_name=author_name,
            author_id=message.author.id,
            text=clean_text,
        )

        for persona_id in targets:
            persona = server.personas[persona_id]
            session = self._get_or_create_session(
                runtime=runtime,
                server=server,
                channel_id=channel_id,
                persona_id=persona_id,
            )
            try:
                response = await session.run_turn(
                    author_name=author_name,
                    author_id=message.author.id,
                    raw_text=clean_text,
                    message_id=message.id,
                )
                sent_ids = await self._send_persona_reply(
                    message.channel,
                    persona.display_name,
                    persona.avatar_url,
                    response,
                )
                if sent_ids:
                    runtime.state.channels[channel_id].personas[persona_id].last_message_id = sent_ids[0]
                    runtime.store.save_response(
                        channel_id=channel_id,
                        response_message_id=sent_ids[0],
                        trigger_message_id=message.id,
                        persona_id=persona_id,
                        provider_id=session.provider,
                        bot_id=self.user.id if self.user else 0,
                        text=response,
                    )
                runtime.store.save_state(runtime.state)
            except Exception as exc:
                LOGGER.exception(
                    "ExportV2 turn failed: server=%s channel=%s persona=%s msg=%s",
                    message.guild.id, channel_id, persona_id, message.id,
                )
                await self._send_persona_reply(
                    message.channel,
                    persona.display_name,
                    persona.avatar_url,
                    f"failed: {exc}",
                )

    def _get_or_create_session(
        self,
        *,
        runtime: _ServerRuntime,
        server,
        channel_id: int,
        persona_id: str,
    ) -> PersonaSession:
        key = (channel_id, persona_id)
        existing = runtime.sessions.get(key)
        if existing is not None:
            return existing

        channel_state = runtime.state.channels.setdefault(channel_id, ChannelRuntimeState())
        persona_state = channel_state.personas.setdefault(persona_id, PersonaChannelState())
        session = PersonaSession(
            llm=self._settings.llm,
            server=server,
            persona=server.personas[persona_id],
            channel_id=channel_id,
            store=runtime.store,
            server_state=runtime.state,
            state=persona_state,
        )
        runtime.sessions[key] = session
        return session

    def _resolve_targets(self, message: discord.Message) -> list[str]:
        runtime = self._servers[message.guild.id]
        mentioned = {role.id for role in message.role_mentions}
        seen: set[str] = set()
        deduped: list[str] = []
        for persona_id, role_id in runtime.persona_role_ids.items():
            if role_id in mentioned and persona_id not in seen:
                deduped.append(persona_id)
                seen.add(persona_id)
        return deduped

    def _strip_mentions(self, text: str, server) -> str:
        cleaned = text
        runtime = self._servers[server.server_id]
        for role_id in runtime.persona_role_ids.values():
            cleaned = re.sub(rf"<@&{role_id}>", " ", cleaned)
        if self.user is not None:
            cleaned = re.sub(rf"<@!?{self.user.id}>", " ", cleaned)
        return " ".join(cleaned.split()).strip()

    async def _send_persona_reply(
        self,
        channel: discord.abc.Messageable,
        display_name: str,
        avatar_url: str | None,
        text: str,
    ) -> list[int]:
        parts = _split_message(text)
        webhook = await self._get_webhook(channel)
        sent_ids: list[int] = []
        for part in parts:
            msg = await webhook.send(
                part,
                wait=True,
                username=display_name,
                avatar_url=avatar_url,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            sent_ids.append(msg.id)
        return sent_ids

    async def _get_webhook(self, channel: discord.abc.Messageable) -> discord.Webhook:
        if not hasattr(channel, "webhooks"):
            raise RuntimeError("Configured channel does not support webhooks")
        guild_id = channel.guild.id  # type: ignore[attr-defined]
        runtime = self._servers[guild_id]
        cached = runtime.webhook.get(channel.id)  # type: ignore[union-attr]
        if cached is not None:
            return cached

        webhooks = await channel.webhooks()  # type: ignore[union-attr]
        for wh in webhooks:
            if wh.name == "ExportV2":
                runtime.webhook[channel.id] = wh  # type: ignore[union-attr]
                return wh

        wh = await channel.create_webhook(name="ExportV2")  # type: ignore[union-attr]
        runtime.webhook[channel.id] = wh  # type: ignore[union-attr]
        return wh


def _split_message(text: str, limit: int = 1900) -> list[str]:
    stripped = text.strip()
    if not stripped:
        return ["..."]
    if len(stripped) <= limit:
        return [stripped]

    parts: list[str] = []
    remaining = stripped
    while len(remaining) > limit:
        split_at = remaining.rfind("\n", 0, limit)
        if split_at < limit // 2:
            split_at = remaining.rfind(" ", 0, limit)
        if split_at < limit // 2:
            split_at = limit
        parts.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()
    if remaining:
        parts.append(remaining)
    return parts
