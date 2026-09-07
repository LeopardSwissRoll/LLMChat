from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import re
import shutil
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from ..core import BridgeCore
from ..pty.handler import PtyState
from ..models import BridgeSettings, PersonaConfig, ProviderCapabilities, ProviderState, ResponseMeta
from ..streamer import DiscordStreamer, WebhookStreamer
from .base import IncomingMessage, TransportAdapter
from .webhook_tracker import WebhookTracker

LOGGER = logging.getLogger(__name__)

_CLAUDE_MODEL_CHOICES: tuple[tuple[str, str], ...] = (
    ("Opus", "opus"),
    ("Sonnet", "sonnet"),
    ("Haiku", "haiku"),
)


@functools.lru_cache(maxsize=1)
def _load_codex_model_specs() -> tuple[tuple[str, str], ...]:
    fallback = (
        ("gpt-5.4", "gpt-5.4"),
        ("gpt-5.4-mini", "gpt-5.4-mini"),
        ("gpt-5.3-codex", "gpt-5.3-codex"),
        ("gpt-5.2-codex", "gpt-5.2-codex"),
    )
    cache_path = Path.home() / ".codex" / "models_cache.json"
    if not cache_path.exists():
        return fallback
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
        models = sorted(
            (
                item for item in payload.get("models", [])
                if item.get("visibility") == "list" and item.get("slug")
            ),
            key=lambda item: item.get("priority", 9999),
        )
    except Exception:
        return fallback
    results: list[tuple[str, str]] = []
    for item in models[:8]:
        value = str(item["slug"])
        label = value
        results.append((label, value))
    return tuple(results) or fallback


def _provider_model_specs(
    provider_id: str,
    current_model: str,
) -> tuple[tuple[str, str], ...]:
    if provider_id == "claude":
        base = list(_CLAUDE_MODEL_CHOICES)
    elif provider_id == "codex":
        base = list(_load_codex_model_specs())
    else:
        base = []

    effective_current = _effective_model_for_provider(provider_id, current_model)
    known_values = {value for _, value in base}
    if effective_current and effective_current not in known_values:
        base.insert(0, (effective_current, effective_current))
    return tuple(base)


def _provider_model_options(
    provider_id: str,
    current_model: str,
) -> list[discord.SelectOption]:
    effective_current = _effective_model_for_provider(provider_id, current_model)
    return [
        discord.SelectOption(
            label=label,
            value=value,
            default=(effective_current == value),
        )
        for label, value in _provider_model_specs(provider_id, current_model)
    ]


def _provider_label(provider_id: str) -> str:
    labels = {
        "claude": "Claude",
        "codex": "Codex",
    }
    return labels.get(provider_id, provider_id.title())


def _encode_provider_model_value(provider_id: str, model: str) -> str:
    return f"{provider_id}:{model}"


def _decode_provider_model_value(raw: str) -> tuple[str, str]:
    provider_id, sep, model = raw.partition(":")
    if not provider_id or not sep or not model:
        raise ValueError(f"Invalid provider/model selection: {raw!r}")
    return provider_id, model


def _provider_model_panel_options(
    enabled_providers: tuple[str, ...],
    state: "PersonaState",
    active_provider: str,
) -> list[discord.SelectOption]:
    options: list[discord.SelectOption] = []
    for provider_id in enabled_providers:
        provider_state = state.get_provider(provider_id)
        effective_model = _effective_model_for_provider(
            provider_id, provider_state.model,
        )
        for label, value in _provider_model_specs(provider_id, provider_state.model):
            options.append(
                discord.SelectOption(
                    label=f"{_provider_label(provider_id)} · {label}",
                    value=_encode_provider_model_value(provider_id, value),
                    default=(
                        active_provider == provider_id
                        and effective_model == value
                    ),
                ),
            )
    return options


def _provider_effort_options(
    provider_id: str,
    current_effort: str,
) -> list[discord.SelectOption]:
    effective_current = _effective_effort_for_provider(provider_id, current_effort)
    if provider_id == "codex":
        levels = ("xhigh", "high", "medium", "low")
    else:
        levels = ("high", "medium", "low")
    return [
        discord.SelectOption(
            label=level.upper() if level == "xhigh" else level.title(),
            value=level,
            default=(effective_current == level),
        )
        for level in levels
    ]


def _provider_permission_options(current_permission: str) -> list[discord.SelectOption]:
    effective_current = _effective_permission(current_permission)
    return [
        discord.SelectOption(
            label="Plan",
            value="plan",
            emoji="\U0001f4cb",
            default=(effective_current == "plan"),
        ),
        discord.SelectOption(
            label="Bypass",
            value="bypass",
            emoji="\U0001f513",
            default=(effective_current == "bypass"),
        ),
    ]


def _normalize_effort_for_provider(provider_id: str, effort: str) -> str:
    if provider_id == "claude" and effort == "xhigh":
        return "high"
    return effort


def _effective_model_for_provider(provider_id: str, model: str) -> str:
    if model and model != "default":
        return model
    return "gpt-5.4" if provider_id == "codex" else "opus"


def _effective_effort_for_provider(provider_id: str, effort: str) -> str:
    if effort and effort != "default":
        return _normalize_effort_for_provider(provider_id, effort)
    return "xhigh" if provider_id == "codex" else "high"


def _effective_permission(permission: str) -> str:
    if permission and permission != "default":
        return permission
    return "bypass"


def _normalize(text: str) -> str:
    return "".join(text.casefold().split())


def _strip_self_mention(text: str, self_id: int) -> str:
    """Remove only this bot's own mention, keep everything else.

    Preserves newlines and multi-line formatting — only collapses
    runs of spaces on each individual line.
    """
    text = re.sub(rf"<@!?{self_id}>", " ", text)
    # Collapse horizontal whitespace per line, but keep newlines intact
    lines = text.split("\n")
    return "\n".join(" ".join(line.split()) for line in lines)


def _inject_attachment_refs(text: str, paths: tuple[str, ...]) -> str:
    """Prefix attachment paths so the model can open them on demand."""
    refs = "\n".join(f"[파일: {path}]" for path in paths)
    body = text.strip()
    if refs and body:
        return f"{refs}\n{body}"
    if refs:
        return refs
    return body


class DiscordOutputSink:
    """OutputSink implementation backed by DiscordStreamer or WebhookStreamer."""

    def __init__(
        self, streamer: "DiscordStreamer | WebhookStreamer", bot: commands.Bot, author_id: str = "0",
    ) -> None:
        self._streamer = streamer
        self._bot = bot
        self._author_id = author_id  # restrict interactive prompts to this user
        self._web_url: str | None = None
        self._web_threshold: int = 500
        self._web_summary_len: int = 200

    def set_web_url(
        self, url: str, threshold: int = 500, summary_length: int = 200,
    ) -> None:
        """Set the web viewer URL and display thresholds."""
        self._web_url = url
        self._web_threshold = threshold
        self._web_summary_len = summary_length

    async def begin(self, context: str) -> None:
        await self._streamer.start()

    async def stream_update(self, text: str) -> None:
        await self._streamer.update(text)

    async def finalize(self, text: str, meta: ResponseMeta) -> str | None:
        if self._web_url and len(text) >= self._web_threshold:
            # Summary mode: short preview + web link
            summary = meta.opening_line or text[:self._web_summary_len]
            if len(summary) < len(text):
                summary = summary.rstrip().rstrip(".") + "..."
            discord_text = f"{summary}\n\n-# [\U0001f4c4 전체 응답 보기]({self._web_url})"
        elif self._web_url:
            # Short response: full text + web link footer
            discord_text = f"{text}\n\n-# [\U0001f517 웹에서 보기]({self._web_url})"
        else:
            discord_text = text
        sent = await self._streamer.finalize(discord_text)
        return str(sent.id)

    @property
    def continuation_chunks(self) -> list[tuple[str, str]]:
        """[(message_id, chunk_text)] for continuation messages (B, C, ...)."""
        return self._streamer.continuation_chunks

    @property
    def status_message_id(self) -> str | None:
        return self._streamer.status_message_id

    async def fail(self, error: str) -> None:
        await self._streamer.fail(error)

    # Phase 2: interactive prompt support

    async def send_choices(self, text: str, choices: list[str]) -> str:
        """Send choice buttons and wait for user selection.

        Only the original message author can interact with the buttons.
        """
        future: asyncio.Future[str] = asyncio.get_event_loop().create_future()
        view = ChoiceView(choices, future, allowed_user_id=self._author_id)
        display = text[-500:] if len(text) > 500 else text
        channel = self._streamer.source.channel
        msg = await channel.send(
            f"**\U0001f512 권한 요청:**\n```\n{display}\n```",
            view=view,
        )
        try:
            result = await asyncio.wait_for(future, timeout=120)
        except asyncio.TimeoutError:
            result = "No"
        try:
            await msg.delete()
        except discord.HTTPException:
            pass
        return result

    async def ask_user(self, text: str) -> str:
        """Send a question and wait for the original author's text reply."""
        channel = self._streamer.source.channel
        display = text[-500:] if len(text) > 500 else text
        author_id = self._author_id
        await channel.send(
            f"**\u2753 질문:**\n```\n{display}\n```\n*<@{author_id}> 채널에 답변을 입력하세요...*"
        )

        def check(m: discord.Message) -> bool:
            return (
                m.channel.id == channel.id
                and not m.author.bot
                and str(m.author.id) == author_id
            )

        try:
            reply = await self._bot.wait_for("message", check=check, timeout=120)
            return reply.content
        except asyncio.TimeoutError:
            return ""


# ------------------------------------------------------------------
# Control Panel (Phase 1)
# ------------------------------------------------------------------

_PERM_ORDER = ("default", "plan", "bypass")


def _perm_cycles(current: str, target: str) -> int:
    """Calculate number of Shift+Tab presses to cycle from current to target."""
    if current == target:
        return 0
    try:
        ci = _PERM_ORDER.index(current)
        ti = _PERM_ORDER.index(target)
        return (ti - ci) % len(_PERM_ORDER)
    except ValueError:
        return 1


_STATE_ICONS = {
    "ready": "\U0001f7e2", "busy": "\U0001f7e1", "starting": "\U0001f535",
    "dead": "\U0001f534", "offline": "\u26aa",
}


def _build_panel_embed(
    persona_id: str,
    provider_id: str,
    persona_state: "PersonaState",
    provider_state: ProviderState,
    pty_state: str = "ready",
    channel_sessions: list[dict] | None = None,
    usage: dict[str, str] | None = None,
) -> discord.Embed:
    embed = discord.Embed(title="Status", color=discord.Color.blurple())

    # Session list (all bots in this channel)
    if channel_sessions:
        lines = []
        for s in channel_sessions:
            icon = _STATE_ICONS.get(s["state"], "\u26aa")
            marker = " \u25c0" if (
                s["persona_id"] == persona_id and s["provider_id"] == provider_id
            ) else ""
            fast = " \u26a1" if s.get("fast") else ""
            lines.append(
                f"{icon} **{s['persona_id']}** \u2014 "
                f"{s['provider_id']} \u00b7 "
                f"{_effective_model_for_provider(s['provider_id'], s['model'])} \u00b7 "
                f"{_effective_effort_for_provider(s['provider_id'], s['effort'])} \u00b7 "
                f"{_effective_permission(s['permission'])} \u00b7 LV{s['mode']}"
                f"{fast}{marker}"
            )
        embed.description = "\n".join(lines)
    else:
        icon = _STATE_ICONS.get(pty_state.lower(), "\u26aa")
        fast = " \u26a1" if provider_state.fast else ""
        embed.description = (
            f"{icon} **{persona_id}** \u2014 "
            f"{provider_id} \u00b7 {_effective_model_for_provider(provider_id, provider_state.model)} "
            f"\u00b7 {_effective_effort_for_provider(provider_id, provider_state.effort)} "
            f"\u00b7 {_effective_permission(provider_state.permission)} \u00b7 LV{provider_state.mode}{fast}"
        )

    # Usage info
    if usage:
        parts = []
        if "session" in usage:
            parts.append(f"Session {usage['session']}%")
        if "week" in usage:
            parts.append(f"Week {usage['week']}%")
        if parts:
            embed.add_field(name="Usage", value=" \u00b7 ".join(parts), inline=False)

    access_icon = (
        "\U0001f513" if persona_state.command_access == "public" else "\U0001f512"
    )
    embed.set_footer(
        text=f"\u25b6 {persona_id} | provider={provider_id} | "
             f"{access_icon} {persona_state.command_access}"
    )
    return embed


class PanelButton(discord.ui.Button["ControlPanelView"]):
    async def callback(self, interaction: discord.Interaction) -> None:
        view: ControlPanelView = self.view  # type: ignore[assignment]
        await view.handle_click(interaction, self.custom_id)


class PanelSelect(discord.ui.Select["ControlPanelView"]):
    async def callback(self, interaction: discord.Interaction) -> None:
        view: ControlPanelView = self.view  # type: ignore[assignment]
        await view.handle_select(interaction, self.custom_id, self.values[0])


class ControlPanelView(discord.ui.View):
    """Persistent control panel — select menus + action buttons."""

    def __init__(
        self,
        persona_id: str = "",
        provider_id: str = "claude",
        state: "PersonaState | None" = None,
        provider_state: ProviderState | None = None,
        capabilities: ProviderCapabilities | None = None,
        enabled_providers: tuple[str, ...] = ("claude",),
    ) -> None:
        super().__init__(timeout=None)
        self._persona_id = persona_id
        from ..core.channel_manager import PersonaState as _PS

        s = state or _PS()
        provider = provider_state or s.get_provider(provider_id)
        caps = capabilities or ProviderCapabilities()

        pfx = f"panel:{persona_id}"
        # Row 0: Provider + Model
        self.add_item(PanelSelect(
            custom_id=f"{pfx}:model",
            placeholder="Provider + Model",
            options=_provider_model_panel_options(
                enabled_providers, s, provider_id,
            ),
            row=0,
            disabled=False,
        ))
        # Row 1: Effort
        self.add_item(PanelSelect(
            custom_id=f"{pfx}:effort",
            placeholder="Effort",
            options=_provider_effort_options(provider_id, provider.effort),
            row=1,
            disabled=not caps.effort,
        ))
        # Row 2: Permission
        self.add_item(PanelSelect(
            custom_id=f"{pfx}:permission",
            placeholder="Permission",
            options=_provider_permission_options(provider.permission),
            row=2,
            disabled=not caps.permission,
        ))
        # Row 3: Persona Level
        self.add_item(PanelSelect(
            custom_id=f"{pfx}:mode",
            placeholder="Persona LV",
            options=[
                discord.SelectOption(label="LV1 · core", value="1", default=(provider.mode == 1)),
                discord.SelectOption(label="LV2 · soft", value="2", default=(provider.mode == 2)),
                discord.SelectOption(label="LV3 · medium", value="3", default=(provider.mode == 3)),
                discord.SelectOption(label="LV4 · hard", value="4", default=(provider.mode == 4)),
                discord.SelectOption(label="LV5 · masquerade", value="5", default=(provider.mode == 5)),
            ],
            row=3,
        ))
        # Row 4: Action buttons
        self.add_item(PanelButton(
            label="Fast", custom_id=f"{pfx}:fast:toggle", emoji="\u26a1",
            style=discord.ButtonStyle.primary if provider.fast else discord.ButtonStyle.secondary,
            row=4,
            disabled=not caps.fast,
        ))
        self.add_item(PanelButton(
            label="Compact", custom_id=f"{pfx}:action:compact",
            style=discord.ButtonStyle.secondary, row=4,
            disabled=not caps.compact,
        ))
        self.add_item(PanelButton(
            label="Interrupt", custom_id=f"{pfx}:action:stop",
            style=discord.ButtonStyle.danger, row=4,
            disabled=not caps.interrupt,
        ))
        self.add_item(PanelButton(
            label="Reset", custom_id=f"{pfx}:action:reset",
            style=discord.ButtonStyle.danger, row=4,
        ))

    async def handle_select(self, interaction: discord.Interaction, custom_id: str, value: str) -> None:
        transport: "DiscordTransport" = interaction.client._transport  # type: ignore[attr-defined]
        if await transport._reject_explorer_thread_command(interaction):
            return
        if not transport._check_access(interaction):
            await interaction.response.send_message("\uad8c\ud55c \uc5c6\uc74c", ephemeral=True)
            return
        # Parse custom_id: "panel:{persona_id}:{setting}"
        parts = custom_id.split(":")
        pid = parts[1] if len(parts) >= 3 else self._persona_id
        setting = parts[2] if len(parts) >= 3 else parts[1]
        cm = transport._core.channel_mgr
        server_id = str(interaction.guild_id) if interaction.guild_id else "0"
        channel_id = transport._resolve_parent_channel(interaction.channel_id)
        provider_id = transport._active_provider_for(server_id, channel_id, pid)
        provider_state = cm.get_provider_state(server_id, channel_id, pid, provider_id)
        if setting not in {"model", "mode"} and not transport._supports_for(server_id, channel_id, pid, setting):
            await interaction.response.send_message(
                f"{provider_id}에서는 `{setting}` 제어를 지원하지 않습니다.",
                ephemeral=True,
            )
            return

        try:
            persona_cfg = transport._personas.get(pid)
            if setting == "model":
                selected_provider, selected_model = _decode_provider_model_value(value)
                enabled = persona_cfg.enabled_providers if persona_cfg else ()
                if (
                    selected_provider not in enabled
                    or selected_provider not in transport._core._settings.providers
                ):
                    await interaction.response.send_message(
                        f"알 수 없는 provider 선택: `{selected_provider}`",
                        ephemeral=True,
                    )
                    return
                if selected_provider != provider_id:
                    cm.set_active_provider(
                        server_id, channel_id, pid, selected_provider,
                    )
                    provider_id = selected_provider
                    provider_state = cm.get_provider_state(
                        server_id, channel_id, pid, provider_id,
                    )
                model_changed = provider_state.model != selected_model
                provider_state.model = selected_model
                if model_changed:
                    transport._invalidate_provider_resume_for(
                        server_id, channel_id, provider_id, pid,
                    )
            elif setting == "effort":
                normalized = _normalize_effort_for_provider(provider_id, value)
                if provider_state.effort != normalized:
                    provider_state.effort = normalized
                    transport._invalidate_provider_resume_for(
                        server_id, channel_id, provider_id, pid,
                    )
            elif setting == "permission":
                if provider_state.permission != value:
                    provider_state.permission = value
                    transport._invalidate_provider_resume_for(
                        server_id, channel_id, provider_id, pid,
                    )
            elif setting == "mode":
                int_value = int(value)
                if int_value != provider_state.mode:
                    provider_state.mode = int_value
                    transport._invalidate_provider_resume_for(
                        server_id, channel_id, provider_id, pid,
                    )
            cm._save_state(server_id, channel_id)
        except Exception as exc:
            LOGGER.warning("Panel select error: %s", exc)

        await transport._rebuild_panel_for(interaction, server_id, channel_id, pid)

    async def handle_click(self, interaction: discord.Interaction, custom_id: str) -> None:
        transport: "DiscordTransport" = interaction.client._transport  # type: ignore[attr-defined]
        if await transport._reject_explorer_thread_command(interaction):
            return
        if not transport._check_access(interaction):
            await interaction.response.send_message("\uad8c\ud55c \uc5c6\uc74c", ephemeral=True)
            return
        # Parse custom_id: "panel:{persona_id}:{action}:{value}"
        parts = custom_id.split(":")
        pid = parts[1] if len(parts) >= 4 else self._persona_id
        action = parts[2] if len(parts) >= 4 else parts[1]
        value = parts[3] if len(parts) >= 4 else (parts[2] if len(parts) >= 3 else "")
        cm = transport._core.channel_mgr
        server_id = str(interaction.guild_id) if interaction.guild_id else "0"
        channel_id = transport._resolve_parent_channel(interaction.channel_id)
        provider_id = transport._active_provider_for(server_id, channel_id, pid)
        provider_state = cm.get_provider_state(server_id, channel_id, pid, provider_id)

        try:
            if action == "fast":
                if not transport._supports_for(server_id, channel_id, pid, "fast"):
                    await interaction.response.send_message(
                        f"{provider_id}에서는 `fast` 제어를 지원하지 않습니다.",
                        ephemeral=True,
                    )
                    return
                provider_state.fast = not provider_state.fast
                transport._invalidate_provider_resume_for(
                    server_id, channel_id, provider_id, pid,
                )
            elif action == "action":
                if value == "compact":
                    if not transport._supports_for(server_id, channel_id, pid, "compact"):
                        await interaction.response.send_message(
                            f"{provider_id}에서는 `compact` 제어를 지원하지 않습니다.",
                            ephemeral=True,
                        )
                        return
                    try:
                        await transport._send_provider_control_for(
                            server_id, channel_id, "compact", pid,
                        )
                    except RuntimeError:
                        pass
                elif value == "stop":
                    if not transport._supports_for(server_id, channel_id, pid, "interrupt"):
                        await interaction.response.send_message(
                            f"{provider_id}에서는 `interrupt` 제어를 지원하지 않습니다.",
                            ephemeral=True,
                        )
                        return
                    try:
                        await transport._core.interrupt_session(
                            pid,
                            provider_id,
                            channel_id,
                            str(transport._workspace(
                                server_id=server_id,
                                channel_id=channel_id,
                            )),
                        )
                    except RuntimeError as exc:
                        if not interaction.response.is_done():
                            await interaction.response.send_message(
                                str(exc), ephemeral=True,
                            )
                            return
                elif value == "reset":
                    await interaction.response.defer()
                    try:
                        await transport._core.reset_session(
                            pid,
                            provider_id,
                            channel_id,
                            str(transport._workspace(
                                server_id=server_id,
                                channel_id=channel_id,
                            )),
                        )
                    except Exception:
                        pass
                elif value == "instruct":
                    await interaction.response.send_modal(InstructModal(transport, pid))
                    return
            cm._save_state(server_id, channel_id)
        except Exception as exc:
            LOGGER.warning("Panel button error: %s", exc)

        await transport._rebuild_panel_for(interaction, server_id, channel_id, pid)


# ------------------------------------------------------------------
# Choice View (Phase 2)
# ------------------------------------------------------------------

class ChoiceButton(discord.ui.Button["ChoiceView"]):
    async def callback(self, interaction: discord.Interaction) -> None:
        view: ChoiceView = self.view  # type: ignore[assignment]
        if not view._future.done():
            view._future.set_result(self.label)
        await interaction.response.defer()
        view.stop()


class ChoiceView(discord.ui.View):
    """Ephemeral view for permission / choice prompts.

    Only the user identified by *allowed_user_id* can interact.
    """

    def __init__(
        self,
        choices: list[str],
        future: asyncio.Future[str],
        allowed_user_id: str = "0",
    ) -> None:
        super().__init__(timeout=120)
        self._future = future
        self._allowed_user_id = allowed_user_id
        _CHOICE_STYLES = {"yes": discord.ButtonStyle.success, "always": discord.ButtonStyle.primary}
        for choice in choices:
            style = _CHOICE_STYLES.get(choice.lower(), discord.ButtonStyle.danger)
            self.add_item(ChoiceButton(label=choice, style=style))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if str(interaction.user.id) != self._allowed_user_id:
            await interaction.response.send_message(
                "\uc694\uccad\uc790\ub9cc \uc751\ub2f5\ud560 \uc218 \uc788\uc2b5\ub2c8\ub2e4.", ephemeral=True,
            )
            return False
        return True

    async def on_timeout(self) -> None:
        if not self._future.done():
            self._future.set_result("No")


# ------------------------------------------------------------------
# Instruct Modal (Phase 3)
# ------------------------------------------------------------------

class InstructModal(discord.ui.Modal, title="\ucd94\uac00 \uc9c0\uc2dc"):
    instruction = discord.ui.TextInput(
        label="\uc9c0\uc2dc\uc0ac\ud56d",
        style=discord.TextStyle.long,
        placeholder="\uc138\uc158\uc5d0 \ubcf4\ub0bc \uc9c0\uc2dc\ub97c \uc785\ub825\ud558\uc138\uc694 (\uc0ac\uc6a9\uc790 \uba54\uc2dc\uc9c0\ub85c \uc804\uc1a1\ub428, \uc138\uc158 \uc7ac\uc2dc\uc791 \uc2dc \uc18c\uba78)...",
        max_length=1000,
    )

    def __init__(self, transport: "DiscordTransport", persona_id: str = "") -> None:
        super().__init__()
        self._transport = transport
        self._persona_id = persona_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if await self._transport._reject_explorer_thread_command(interaction):
            return
        pid = self._persona_id or self._transport._default_persona_id
        server_id = str(interaction.guild_id) if interaction.guild_id else "0"
        channel_id = self._transport._resolve_parent_channel(interaction.channel_id)
        provider_id = self._transport._active_provider_for(server_id, channel_id, pid)
        # Sent as user-level message (not system prompt).
        # The model will follow the instruction, but it won't persist
        # across session restarts.
        text = self.instruction.value
        try:
            await self._transport._core.send_runtime_message(
                pid,
                provider_id,
                channel_id,
                str(self._transport._workspace(
                    server_id=server_id,
                    channel_id=channel_id,
                )),
                text,
            )
            await interaction.response.send_message(
                f"\uc9c0\uc2dc \uc804\uc1a1\ub428 (\uc0ac\uc6a9\uc790 \uba54\uc2dc\uc9c0): {text[:100]}",
                ephemeral=True,
            )
        except Exception as exc:
            await interaction.response.send_message(f"\uc2e4\ud328: {exc}", ephemeral=True)


# ------------------------------------------------------------------
# File Explorer (Thread-based)
# ------------------------------------------------------------------

@dataclass
class ExplorerState:
    thread_id: int
    parent_channel_id: int
    message_id: int | None = None
    current_path: str = ""  # relative to workspace root


def _scan_directory(ws: Path, rel_path: str, limit: int = 25) -> list[tuple[str, str, bool]]:
    """List items in directory. Returns [(display_label, rel_path, is_dir)]."""
    target = (ws / rel_path).resolve() if rel_path else ws.resolve()
    # Validate path stays within workspace
    try:
        target.relative_to(ws.resolve())
    except ValueError:
        return []
    if not target.exists() or not target.is_dir():
        return []
    items: list[tuple[str, str, bool]] = []
    try:
        for entry in sorted(target.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower())):
            if entry.name.startswith("."):
                continue
            rel = str(entry.relative_to(ws)).replace("\\", "/")
            if entry.is_dir():
                count = sum(1 for _ in entry.iterdir() if not _.name.startswith("."))
                items.append((f"{entry.name}/ ({count})", rel, True))
            else:
                size = entry.stat().st_size
                if size < 1024:
                    sz = f"{size} B"
                elif size < 1024 * 1024:
                    sz = f"{size / 1024:.1f} KB"
                else:
                    sz = f"{size / 1024 / 1024:.1f} MB"
                items.append((f"{entry.name} ({sz})", rel, False))
            if len(items) >= limit:
                break
    except PermissionError:
        pass
    return items


def _build_explorer_embed(ws: Path, state: ExplorerState) -> discord.Embed:
    display_path = state.current_path or ws.name
    embed = discord.Embed(
        title=f"\U0001f4c1 {display_path}",
        color=discord.Color.green(),
    )
    items = _scan_directory(ws, state.current_path)
    if items:
        lines = []
        for label, _, is_dir in items:
            icon = "\U0001f4c2" if is_dir else "\U0001f4c4"
            lines.append(f"{icon} {label}")
        embed.description = "\n".join(lines)
    else:
        embed.description = "(\ube48 \ud3f4\ub354)"
    embed.set_footer(text="\ud30c\uc77c\uc744 \ub4dc\ub86d\ud558\uba74 \ud604\uc7ac \ud3f4\ub354\uc5d0 \uc800\uc7a5\ub429\ub2c8\ub2e4")
    return embed


def _build_explorer_view(ws: Path, state: ExplorerState) -> "FileExplorerView":
    items = _scan_directory(ws, state.current_path)
    select_items: list[tuple[str, str, str]] = []
    for label, rel, is_dir in items:
        emoji = "\U0001f4c2" if is_dir else "\U0001f4c4"
        select_items.append((label[:100], rel, emoji))
    return FileExplorerView(select_items)


class ExplorerButton(discord.ui.Button["FileExplorerView"]):
    async def callback(self, interaction: discord.Interaction) -> None:
        transport: "DiscordTransport" = interaction.client._transport  # type: ignore[attr-defined]
        await transport._handle_explorer_button(interaction, self.custom_id)


class FileSelectMenu(discord.ui.Select["FileExplorerView"]):
    def __init__(self, items: list[tuple[str, str, str]] | None = None) -> None:
        options = []
        if items:
            for label, value, emoji in items:
                options.append(discord.SelectOption(label=label, value=value, emoji=emoji))
        if not options:
            options = [discord.SelectOption(label="(\ube48 \ud3f4\ub354)", value="_empty")]
        super().__init__(
            custom_id="explorer:select",
            placeholder="\ud30c\uc77c/\ud3f4\ub354 \uc120\ud0dd...",
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        transport: "DiscordTransport" = interaction.client._transport  # type: ignore[attr-defined]
        selected = self.values[0] if self.values else ""
        await transport._handle_explorer_select(interaction, selected)


class FileExplorerView(discord.ui.View):
    """Persistent file explorer view for thread."""

    def __init__(self, items: list[tuple[str, str, str]] | None = None) -> None:
        super().__init__(timeout=None)
        self.add_item(FileSelectMenu(items))
        # Row 1: write operations
        for label, cid, emoji in [
            ("New", "explorer:new", "\U0001f4c1"),
            ("Del", "explorer:del", "\U0001f5d1"),
            ("Get", "explorer:get", "\U0001f4e5"),
        ]:
            self.add_item(ExplorerButton(
                label=label, custom_id=cid, emoji=emoji,
                style=discord.ButtonStyle.secondary, row=1,
            ))
        # Row 2: navigation
        for label, cid, emoji in [
            ("Refresh", "explorer:refresh", "\U0001f504"),
            ("Up", "explorer:up", "\u2b06\ufe0f"),
            ("Link", "explorer:link", "\U0001f517"),
        ]:
            self.add_item(ExplorerButton(
                label=label, custom_id=cid, emoji=emoji,
                style=discord.ButtonStyle.secondary, row=2,
            ))


class NewFolderModal(discord.ui.Modal, title="\uc0c8 \ud3f4\ub354"):
    folder_name = discord.ui.TextInput(
        label="\ud3f4\ub354\uba85",
        placeholder="\uc0dd\uc131\ud560 \ud3f4\ub354 \uc774\ub984...",
        max_length=100,
    )

    def __init__(self, transport: "DiscordTransport", thread_id: int) -> None:
        super().__init__()
        self._transport = transport
        self._thread_id = thread_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        state = self._transport._explorer_states.get(self._thread_id)
        if not state:
            await interaction.response.send_message("Explorer not found", ephemeral=True)
            return
        server_id = str(interaction.guild_id) if interaction.guild_id else "0"
        ws = self._transport._workspace(
            server_id=server_id,
            channel_id=state.parent_channel_id,
        )
        target = ws / state.current_path / self.folder_name.value
        resolved = target.resolve()
        try:
            resolved.relative_to(ws.resolve())
        except ValueError:
            await interaction.response.send_message("Invalid path", ephemeral=True)
            return
        resolved.mkdir(parents=True, exist_ok=True)
        await self._transport._refresh_explorer(interaction, state)


class CopyDestModal(discord.ui.Modal, title="\ud30c\uc77c \ubcf5\uc0ac"):
    destination = discord.ui.TextInput(
        label="\ub300\uc0c1 \uacbd\ub85c",
        placeholder="\ubcf5\uc0ac\ud560 \uc704\uce58 (\uc608: src/backup/file.txt)",
        max_length=200,
    )

    def __init__(self, transport: "DiscordTransport", src_rel: str, thread_id: int) -> None:
        super().__init__()
        self._transport = transport
        self._src_rel = src_rel
        self._thread_id = thread_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not self._transport._check_access(interaction):
            await interaction.response.send_message("권한 없음", ephemeral=True)
            return
        server_id = str(interaction.guild_id) if interaction.guild_id else "0"
        parent_ch = self._transport._resolve_parent_channel(interaction.channel_id)
        ws = self._transport._workspace(server_id=server_id, channel_id=parent_ch)
        src = (ws / self._src_rel).resolve()
        dest = (ws / self.destination.value).resolve()
        try:
            src.relative_to(ws.resolve())
            dest.relative_to(ws.resolve())
        except ValueError:
            await interaction.response.send_message("Invalid path", ephemeral=True)
            return
        if not src.exists():
            await interaction.response.send_message("Source not found", ephemeral=True)
            return
        if dest.exists():
            await interaction.response.send_message("대상이 이미 존재합니다", ephemeral=True)
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            if src.is_dir():
                shutil.copytree(str(src), str(dest))
            else:
                shutil.copy2(str(src), str(dest))
            await interaction.response.send_message(
                f"\u2705 \ubcf5\uc0ac: `{self._src_rel}` \u2192 `{self.destination.value}`",
                ephemeral=True,
            )
            # Refresh explorer
            state = self._transport._explorer_states.get(self._thread_id)
            if state and state.message_id:
                embed = _build_explorer_embed(ws, state)
                view = _build_explorer_view(ws, state)
                channel = self._transport._bot.get_channel(self._thread_id)
                if channel:
                    try:
                        msg = await channel.fetch_message(state.message_id)
                        await msg.edit(embed=embed, view=view)
                    except discord.HTTPException:
                        pass
        except Exception as exc:
            await interaction.response.send_message(f"Failed: {exc}", ephemeral=True)


class FileActionView(discord.ui.View):
    """Ephemeral view shown when a file is selected."""

    def __init__(self, transport: "DiscordTransport", rel_path: str, thread_id: int) -> None:
        super().__init__(timeout=60)
        self._transport = transport
        self._rel_path = rel_path
        self._thread_id = thread_id

    @discord.ui.button(label="Download", emoji="\U0001f4e5", style=discord.ButtonStyle.primary)
    async def download(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self._transport._check_access(interaction):
            await interaction.response.send_message("권한 없음", ephemeral=True)
            return
        server_id = str(interaction.guild_id) if interaction.guild_id else "0"
        parent_ch = self._transport._resolve_parent_channel(interaction.channel_id)
        ws = self._transport._workspace(server_id=server_id, channel_id=parent_ch)
        target = (ws / self._rel_path).resolve()
        try:
            target.relative_to(ws.resolve())
        except ValueError:
            await interaction.response.send_message("Invalid path", ephemeral=True)
            return
        if not target.is_file():
            await interaction.response.send_message("File not found", ephemeral=True)
            return
        await interaction.response.send_message(
            file=discord.File(str(target), filename=target.name),
            ephemeral=True,
        )

    @discord.ui.button(label="Copy", emoji="\U0001f4cb", style=discord.ButtonStyle.secondary)
    async def copy_file(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self._transport._check_access(interaction):
            await interaction.response.send_message("권한 없음", ephemeral=True)
            return
        await interaction.response.send_modal(
            CopyDestModal(self._transport, self._rel_path, self._thread_id),
        )

    @discord.ui.button(label="Link", emoji="\U0001f517", style=discord.ButtonStyle.secondary)
    async def copy_link(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self._transport._check_access(interaction):
            await interaction.response.send_message("권한 없음", ephemeral=True)
            return
        # Relative path from workspace root — usable by LLM and user
        rel = self._rel_path.replace("\\", "/")
        await interaction.response.send_message(
            f"`{rel}`", ephemeral=True,
        )

    @discord.ui.button(label="Delete", emoji="\U0001f5d1", style=discord.ButtonStyle.danger)
    async def delete_ask(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self._transport._check_access(interaction):
            await interaction.response.send_message("권한 없음", ephemeral=True)
            return
        server_id = str(interaction.guild_id) if interaction.guild_id else "0"
        parent_ch = self._transport._resolve_parent_channel(interaction.channel_id)
        ws = self._transport._workspace(server_id=server_id, channel_id=parent_ch)
        target = (ws / self._rel_path).resolve()
        name = target.name
        view = DeleteConfirmView(self._transport, self._rel_path, self._thread_id)
        await interaction.response.edit_message(
            content=f"\u26a0\ufe0f **{name}** \uc0ad\uc81c\ud558\uc2dc\uaca0\uc2b5\ub2c8\uae4c?",
            view=view,
        )


class DeleteConfirmView(discord.ui.View):
    """Final confirmation before deleting a file."""

    def __init__(self, transport: "DiscordTransport", rel_path: str, thread_id: int) -> None:
        super().__init__(timeout=30)
        self._transport = transport
        self._rel_path = rel_path
        self._thread_id = thread_id

    @discord.ui.button(label="\uc0ad\uc81c", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self._transport._check_access(interaction):
            await interaction.response.send_message("권한 없음", ephemeral=True)
            return
        server_id = str(interaction.guild_id) if interaction.guild_id else "0"
        parent_ch = self._transport._resolve_parent_channel(interaction.channel_id)
        ws = self._transport._workspace(server_id=server_id, channel_id=parent_ch)
        target = (ws / self._rel_path).resolve()
        try:
            target.relative_to(ws.resolve())
        except ValueError:
            await interaction.response.edit_message(content="Invalid path", view=None)
            return
        if target.is_file():
            target.unlink()
        elif target.is_dir():
            shutil.rmtree(target)
        else:
            await interaction.response.edit_message(content="Not found", view=None)
            return
        await interaction.response.edit_message(
            content=f"\u2705 \uc0ad\uc81c\ub428: {target.name}", view=None,
        )
        # Refresh explorer
        state = self._transport._explorer_states.get(self._thread_id)
        if state and state.message_id:
            embed = _build_explorer_embed(ws, state)
            view = _build_explorer_view(ws, state)
            channel = self._transport._bot.get_channel(self._thread_id)
            if channel:
                try:
                    msg = await channel.fetch_message(state.message_id)
                    await msg.edit(embed=embed, view=view)
                except discord.HTTPException:
                    pass

    @discord.ui.button(label="\ucde8\uc18c", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="\ucde8\uc18c\ub428", view=None)


class FolderActionView(discord.ui.View):
    """Ephemeral view shown when a folder is selected — navigate or delete."""

    def __init__(self, transport: "DiscordTransport", rel_path: str, thread_id: int) -> None:
        super().__init__(timeout=60)
        self._transport = transport
        self._rel_path = rel_path
        self._thread_id = thread_id

    @discord.ui.button(label="열기", emoji="\U0001f4c2", style=discord.ButtonStyle.primary)
    async def navigate(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        state = self._transport._explorer_states.get(self._thread_id)
        if not state:
            await interaction.response.send_message("Explorer not found", ephemeral=True)
            return
        state.current_path = self._rel_path
        self._transport._save_explorer_states()
        await self._transport._refresh_explorer(interaction, state)

    @discord.ui.button(label="삭제", emoji="\U0001f5d1", style=discord.ButtonStyle.danger)
    async def delete_ask(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self._transport._check_access(interaction):
            await interaction.response.send_message("권한 없음", ephemeral=True)
            return
        server_id = str(interaction.guild_id) if interaction.guild_id else "0"
        parent_ch = self._transport._resolve_parent_channel(interaction.channel_id)
        ws = self._transport._workspace(server_id=server_id, channel_id=parent_ch)
        target = (ws / self._rel_path).resolve()
        name = target.name
        view = DeleteConfirmView(self._transport, self._rel_path, self._thread_id)
        await interaction.response.edit_message(
            content=f"\u26a0\ufe0f 폴더 **{name}/** 를 삭제하시겠습니까?",
            view=view,
        )


class DiscordTransport(TransportAdapter):
    """Discord transport — single bot, multiple personas via webhooks.

    Uses one Discord bot and sends persona-specific messages through
    webhooks (each with its own display name and avatar).
    """

    def __init__(
        self,
        settings: BridgeSettings,
        core: BridgeCore,
        default_workspace: str,
    ) -> None:
        self._settings = settings
        self._core = core
        self._default_workspace = default_workspace

        # All personas managed by this single transport
        self._personas: dict[str, PersonaConfig] = dict(settings.personas)
        self._default_persona_id = settings.active_personas[0] if settings.active_personas else ""

        intents = discord.Intents.default()
        intents.message_content = True
        self._bot = commands.Bot(command_prefix="!", intents=intents)

        # Build alias → persona_id reverse mapping (normalized)
        self._alias_to_persona: dict[str, str] = {}
        for pid, cfg in self._personas.items():
            for alias in cfg.role_aliases:
                if alias.strip():
                    self._alias_to_persona[_normalize(alias)] = pid
        self._all_aliases = set(self._alias_to_persona.keys())

        # Webhook management
        self._webhook_cache: dict[int, discord.Webhook] = {}  # channel_id → webhook
        self._webhook_tracker = WebhookTracker(
            persist_path=Path(default_workspace) / ".bridge" / "webhook_map.json",
        )

        # Track discovered channels: {channel_id: (guild_name, channel_name)}
        self._seen_channels: dict[int, tuple[str, str]] = {}

        # Back-reference for persistent views
        self._bot._transport = self  # type: ignore[attr-defined]

        # File explorer state (thread_id -> ExplorerState)
        self._explorer_states: dict[int, ExplorerState] = {}
        self._load_explorer_states()

        self._register_events()
        self._register_commands()
        self._register_file_commands()

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    def _register_events(self) -> None:
        @self._bot.event
        async def on_ready() -> None:
            # Register persistent views scoped to known message IDs
            self._bot.add_view(FileExplorerView())
            # Panel views for ALL personas
            for (sid, cid), ch_state in self._core.channel_mgr._channels.items():
                for p_id, p_state in ch_state.personas.items():
                    if p_id in self._personas and p_state.panel_message_id:
                        persona_cfg = self._personas[p_id]
                        provider_id = p_state.active_provider
                        self._bot.add_view(
                            ControlPanelView(
                                persona_id=p_id,
                                provider_id=provider_id,
                                state=p_state,
                                provider_state=p_state.get_provider(provider_id),
                                capabilities=self._core.get_provider_capabilities(
                                    provider_id,
                                ),
                                enabled_providers=persona_cfg.enabled_providers,
                            ),
                            message_id=p_state.panel_message_id,
                        )

            # Guild-specific sync for instant command propagation
            for guild in self._bot.guilds:
                try:
                    await self._bot.tree.sync(guild=guild)
                except Exception:
                    LOGGER.warning("Failed to sync commands for guild %s", guild.name)
            # Global sync as fallback (may take up to 1 hour)
            try:
                await self._bot.tree.sync()
            except Exception:
                pass

            personas_str = ", ".join(self._personas.keys())
            guilds = [g.name for g in self._bot.guilds]
            LOGGER.info(
                "Bot ready as %s | personas: [%s] | guilds: %s | mode: %s",
                self._bot.user,
                personas_str,
                guilds,
                "listen-everywhere",
            )

            # Cleanup stale logs for servers/channels the bot no longer has access to
            valid_servers = {str(g.id) for g in self._bot.guilds}
            removed = self._core.cleanup_servers(valid_servers)
            if removed:
                LOGGER.info("Cleaned up %d stale server(s)", removed)

            for guild in self._bot.guilds:
                valid_channels = {str(ch.id) for ch in guild.channels}
                ch_removed = self._core.cleanup_channels(str(guild.id), valid_channels)
                if ch_removed:
                    LOGGER.info(
                        "Cleaned up %d stale channel(s) in '%s'",
                        ch_removed, guild.name,
                    )

            # Auto-create mentionable roles for persona aliases
            for guild in self._bot.guilds:
                existing_roles = {r.name.lower(): r for r in guild.roles}
                for pid, cfg in self._personas.items():
                    for alias in cfg.role_aliases:
                        if alias.lower() not in existing_roles:
                            try:
                                role = await guild.create_role(
                                    name=alias,
                                    mentionable=True,
                                    reason=f"PTY Bridge persona: {pid}",
                                )
                                LOGGER.info(
                                    "Created role '%s' for persona '%s' in '%s'",
                                    alias, pid, guild.name,
                                )
                            except discord.Forbidden:
                                LOGGER.warning(
                                    "Cannot create role '%s' in '%s' — "
                                    "move the bot role higher in Server Settings → Roles",
                                    alias, guild.name,
                                )
                            except discord.HTTPException as exc:
                                LOGGER.warning(
                                    "Failed to create role '%s': %s", alias, exc,
                                )

            # Start health monitor loop
            self._bot.loop.create_task(self._health_loop())

        @self._bot.event
        async def on_thread_delete(thread: discord.Thread) -> None:
            if thread.id in self._explorer_states:
                del self._explorer_states[thread.id]
                self._save_explorer_states()
                LOGGER.info("Explorer thread deleted: %d", thread.id)

        @self._bot.event
        async def on_message(message: discord.Message) -> None:
            if message.author.bot:
                return

            attachment_paths: tuple[str, ...] = ()
            if message.attachments and message.channel.id not in self._explorer_states:
                attachment_paths = await self._save_attachments(message)
            text_with_attachments = _inject_attachment_refs(
                message.content,
                attachment_paths,
            )

            # Passive logging: record all human messages (deduped across bots)
            if message.guild:
                reply_to = None
                if message.reference and message.reference.message_id:
                    reply_to = str(message.reference.message_id)
                self._core.log_channel_message(
                    server_id=str(message.guild.id),
                    channel_id=message.channel.id,
                    author_id=str(message.author.id),
                    message_id=str(message.id),
                    author_name=message.author.display_name,
                    text=text_with_attachments,
                    reply_to=reply_to,
                )

            # Explorer threads are UI-only — never route to PTY
            if message.channel.id in self._explorer_states:
                if message.attachments:
                    await self._explorer_save_attachments(message)
                return

            targets = await self._target_reasons(message)
            if not targets:
                return

            bot_id = self._bot.user.id if self._bot.user else 0
            text = _strip_self_mention(text_with_attachments, bot_id)
            # Strip role mentions (replace with space, preserve newlines)
            for role in message.role_mentions:
                text = re.sub(rf"<@&{role.id}>", " ", text)
            text = text.strip()
            if not text:
                text = " "

            # Log channel discovery
            self._track_channel(message)

            # Dispatch to each targeted persona
            for trigger_reason, persona_id in targets:
                await self._handle_message(
                    message,
                    text,
                    trigger_reason=trigger_reason,
                    persona_id=persona_id,
                    attachment_paths=attachment_paths,
                )

    def _track_channel(self, message: discord.Message) -> None:
        """Log when the bot is mentioned in a channel for the first time."""
        cid = message.channel.id
        if cid in self._seen_channels:
            return

        guild_name = message.guild.name if message.guild else "DM"
        channel_name = getattr(message.channel, "name", str(cid))
        self._seen_channels[cid] = (guild_name, channel_name)

        LOGGER.info(
            "New channel discovered: #%s in '%s' (channel_id=%d, guild_id=%s)",
            channel_name,
            guild_name,
            cid,
            message.guild.id if message.guild else "N/A",
        )

    async def _target_reasons(self, message: discord.Message) -> list[tuple[str, str]]:
        """Determine which persona(s) this message targets.

        Returns list of (trigger_reason, persona_id). May contain multiple
        entries when multiple role mentions are present.

        Priority:
        1. Role mentions → alias lookup → [("role_alias", pid), ...]
        2. Reply to webhook message → tracker lookup → [("reply", pid)]
        3. Direct @bot mention → channel default persona → [("mention", pid)]
        """
        targets: list[tuple[str, str]] = []
        seen_pids: set[str] = set()

        # 1. Role mentions (all matching personas)
        for role in message.role_mentions:
            norm = _normalize(role.name)
            pid = self._alias_to_persona.get(norm)
            if pid and pid not in seen_pids:
                targets.append(("role_alias", pid))
                seen_pids.add(pid)

        if targets:
            return targets

        # 2. Reply to webhook message (persona-specific reply)
        if message.reference and message.reference.message_id:
            ref_id = message.reference.message_id
            reply_pid = self._webhook_tracker.resolve(
                message.channel.id, ref_id,
            )
            if reply_pid:
                return [("reply", reply_pid)]

        # 3. Direct @bot mention → channel default persona
        self_id = getattr(self._bot.user, "id", None)
        if self_id and any(u.id == self_id for u in message.mentions):
            server_id = str(message.guild.id) if message.guild else "0"
            default_pid = self._resolve_default_persona(server_id, message.channel.id)
            return [("mention", default_pid)]

        return []

    def _resolve_default_persona(self, server_id: str, channel_id: int) -> str:
        """Get the default persona for a channel.

        Priority:
        1. Explicit ``default_persona`` stored in ChannelState (set via /init)
        2. First persona with an active panel in this channel
        3. First globally active persona
        """
        cm = self._core.channel_mgr
        ch_state = cm._channels.get((server_id, channel_id))
        if ch_state:
            # 1. Explicit channel-level default
            if ch_state.default_persona and ch_state.default_persona in self._personas:
                return ch_state.default_persona
            # 2. First persona with a panel here
            for pid in self._settings.active_personas:
                if pid in ch_state.personas and pid in self._personas:
                    ps = ch_state.personas[pid]
                    if ps.panel_message_id:
                        return pid
        # 3. First active persona
        for pid in self._settings.active_personas:
            if pid in self._personas:
                return pid
        return self._default_persona_id

    # ------------------------------------------------------------------
    # Message handling
    # ------------------------------------------------------------------

    async def _handle_message(
        self,
        message: discord.Message,
        text: str,
        *,
        trigger_reason: str,
        persona_id: str,
        attachment_paths: tuple[str, ...] = (),
    ) -> None:
        persona = self._personas.get(persona_id)
        if not persona:
            LOGGER.warning("Unknown persona_id: %s", persona_id)
            return

        ch_id = message.channel.id
        cm = self._core.channel_mgr
        server_id = str(message.guild.id) if message.guild else "0"
        provider_id = self._active_provider_for(server_id, ch_id, persona_id)
        if not cm.is_greeted(ch_id, persona_id, provider_id):
            cm.mark_greeted(ch_id, persona_id, provider_id)

        author_id = str(message.author.id)

        # Get or create webhook for this channel (fallback to bot reply)
        streamer: WebhookStreamer | DiscordStreamer
        try:
            webhook = await self._get_webhook(message.channel)

            async def _refresh_webhook() -> discord.Webhook:
                self._webhook_cache.pop(ch_id, None)
                return await self._get_webhook(message.channel)

            streamer = WebhookStreamer(
                webhook=webhook,
                source_message=message,
                persona_name=persona.display_name,
                avatar_url=persona.avatar_url,
                edit_interval=persona.message_edit_interval,
                on_webhook_invalid=_refresh_webhook,
            )
        except (discord.Forbidden, discord.HTTPException):
            LOGGER.warning(
                "Webhook unavailable for channel %d, falling back to bot reply",
                ch_id,
            )
            streamer = DiscordStreamer(
                source_message=message,
                edit_interval=persona.message_edit_interval,
            )

        # Eagerly start the streamer so the "processing..." message exists
        # BEFORE core.handle_message() enqueues the turn.  This lets pipe
        # chains reply to the processing message immediately.
        await streamer.start()

        # Track the processing message right away so reply-targeting works
        # even while the turn is still running.
        if streamer.status_message_id:
            self._webhook_tracker.track(
                ch_id, int(streamer.status_message_id), persona_id,
            )

        sink = DiscordOutputSink(streamer, self._bot, author_id=author_id)

        # Extract reply reference for pipe resolution
        reply_to = None
        if message.reference and message.reference.message_id:
            reply_to = str(message.reference.message_id)

        incoming = IncomingMessage(
            message_id=str(message.id),
            server_id=server_id,
            channel_id=message.channel.id,
            author_id=author_id,
            author_name=message.author.display_name,
            text=text,
            workspace=str(self._workspace(server_id=server_id, channel_id=message.channel.id)),
            persona_id=persona_id,
            provider_id=provider_id,
            reply_to_msg_id=reply_to,
            visible_message_id=str(message.id),
            trigger_reason=trigger_reason,
            attachment_paths=attachment_paths,
        )

        await self._core.handle_message(incoming, sink)

        # Track continuation chunks after finalization
        if hasattr(streamer, 'continuation_chunks'):
            for chunk_id, _ in streamer.continuation_chunks:
                self._webhook_tracker.track(ch_id, int(chunk_id), persona_id)

    # ------------------------------------------------------------------
    # Webhook management
    # ------------------------------------------------------------------

    async def _get_webhook(self, channel: discord.abc.Messageable) -> discord.Webhook:
        """Get or create a webhook for the given channel (cached)."""
        ch_id = channel.id  # type: ignore[union-attr]
        if ch_id in self._webhook_cache:
            return self._webhook_cache[ch_id]

        # For threads, use parent channel
        target = channel
        if isinstance(channel, discord.Thread):
            target = channel.parent

        try:
            webhooks = await target.webhooks()  # type: ignore[union-attr]
            # Reuse existing PTY bridge webhook
            for wh in webhooks:
                if wh.name == "PTY Bridge" and wh.user and wh.user.id == self._bot.user.id:
                    self._webhook_cache[ch_id] = wh
                    return wh

            # Create new webhook
            wh = await target.create_webhook(name="PTY Bridge")  # type: ignore[union-attr]
            self._webhook_cache[ch_id] = wh
            return wh
        except discord.Forbidden:
            LOGGER.error(
                "Missing MANAGE_WEBHOOKS permission in channel %s. "
                "Falling back to bot messages (no persona identity).",
                ch_id,
            )
            raise
        except discord.NotFound:
            # Channel deleted or webhook invalidated — remove cache and retry
            self._webhook_cache.pop(ch_id, None)
            raise

    # ------------------------------------------------------------------
    # Health monitoring
    # ------------------------------------------------------------------

    async def _health_loop(self) -> None:
        """Periodically check for dead sessions and notify channels."""
        cm = self._core.channel_mgr
        await self._bot.wait_until_ready()

        while not self._bot.is_closed():
            await asyncio.sleep(30)
            try:
                dead_keys = self._core.get_dead_sessions()
                for key in dead_keys:
                    if key.persona_id not in self._personas:
                        continue
                    pid = key.persona_id
                    if cm.is_notified_dead(key.channel_id, pid, key.provider_id):
                        continue
                    cm.mark_notified_dead(key.channel_id, pid, key.provider_id)
                    channel = self._bot.get_channel(key.channel_id)
                    if channel and isinstance(channel, discord.abc.Messageable):
                        try:
                            await channel.send(
                                f"[{pid}] 다음 메시지에서 자동으로 재시작됩니다."
                            )
                        except discord.HTTPException:
                            pass

                # Clear notifications for revived sessions
                for pid in self._personas:
                    alive_channels = {
                        (k.channel_id, k.provider_id)
                        for k, s in self._core._registry.get_all_sessions().items()
                        if k.persona_id == pid and s.state != PtyState.DEAD
                    }
                    for ch_id, provider_id in alive_channels:
                        cm.clear_notified_dead(ch_id, pid, provider_id)
            except Exception:
                LOGGER.debug("Health loop error", exc_info=True)

    # ------------------------------------------------------------------
    # Slash commands
    # ------------------------------------------------------------------

    def _persona_autocomplete(self) -> list[app_commands.Choice[str]]:
        """Build persona choices for slash command autocomplete."""
        return [
            app_commands.Choice(name=pid, value=pid)
            for pid in self._settings.active_personas
        ]

    def _resolve_cmd_persona(self, persona_str: str | None) -> str:
        """Resolve persona_id from command parameter or default."""
        if persona_str and persona_str in self._personas:
            return persona_str
        return self._default_persona_id

    def _register_commands(self) -> None:
        _denied = "\uad8c\ud55c \uc5c6\uc74c: protagonist\ub9cc \uc0ac\uc6a9 \uac00\ub2a5"

        # -- Access control command --

        @self._bot.tree.command(
            name="access",
            description="Toggle command access (public/protagonist)",
        )
        @app_commands.describe(persona="대상 페르소나")
        async def access(interaction: discord.Interaction, persona: str | None = None) -> None:
            if await self._reject_explorer_thread_command(interaction):
                return
            pid = self._resolve_cmd_persona(persona)
            if not self._check_access_admin(interaction, pid):
                await interaction.response.send_message(_denied, ephemeral=True)
                return
            server_id = str(interaction.guild_id) if interaction.guild_id else "0"
            channel_id = self._resolve_parent_channel(interaction.channel_id)
            cm = self._core.channel_mgr
            ps = cm.get_persona_state(server_id, channel_id, pid)
            new = "protagonist" if ps.command_access == "public" else "public"
            if new == "protagonist" and not cm.get_protagonist(server_id, channel_id, pid):
                cm.set_protagonist(server_id, channel_id, pid, str(interaction.user.id))
            ps.command_access = new
            cm._save_state(server_id, channel_id)
            await interaction.response.send_message(
                f"[{pid}] Command access \u2192 **{new}**", ephemeral=True,
            )
            await self._update_panel_for(server_id, channel_id, pid)

        @access.autocomplete("persona")
        async def access_persona_ac(
            interaction: discord.Interaction, current: str,
        ) -> list[app_commands.Choice[str]]:
            return self._persona_autocomplete()

        @self._bot.tree.command(
            name="init",
            description="Initialize workspace + show status panels (creates folder/channel if needed)",
        )
        @app_commands.describe(
            folder="워크스페이스 폴더 (자동완성으로 탐색, 없으면 생성. 비우면 루트)",
            channel="채널 이름 (비우면 현재 채널, 새 이름이면 채널 생성)",
        )
        async def init_cmd(
            interaction: discord.Interaction,
            folder: str | None = None,
            channel: str | None = None,
        ) -> None:
            if await self._reject_explorer_thread_command(interaction):
                return
            if not self._check_access_admin(interaction):
                await interaction.response.send_message(_denied, ephemeral=True)
                return

            server_id = str(interaction.guild_id) if interaction.guild_id else "0"
            guild = interaction.guild

            # --- Resolve target channel ---
            if channel and guild:
                channel_name = channel.strip().lower().replace(" ", "-")
                # Check existing channels
                existing = discord.utils.get(guild.text_channels, name=channel_name)
                if existing:
                    target_channel_id = existing.id
                else:
                    # Create new channel in the same category
                    category = (
                        interaction.channel.category
                        if hasattr(interaction.channel, "category")
                        else None
                    )
                    try:
                        new_ch = await guild.create_text_channel(
                            channel_name, category=category,
                        )
                        target_channel_id = new_ch.id
                    except discord.HTTPException as exc:
                        await interaction.response.send_message(
                            f"채널 생성 실패: {exc}", ephemeral=True,
                        )
                        return
            else:
                target_channel_id = self._resolve_parent_channel(interaction.channel_id)

            # --- Resolve workspace folder ---
            workspace_rel: str | None = None
            if folder is not None:
                resolved = self._resolve_workspace_subdir(folder)
                if resolved is None:
                    await interaction.response.send_message(
                        "경로가 유효하지 않습니다. DEFAULT_WORKSPACE 아래의 상대 경로만 가능합니다.",
                        ephemeral=True,
                    )
                    return
                workspace_rel, workspace_path = resolved
                workspace_path.mkdir(parents=True, exist_ok=True)

            # --- Apply to ALL personas ---
            cm = self._core.channel_mgr
            persona_ids = list(self._core._settings.active_personas)
            cm.configure_channel_defaults(
                server_id,
                target_channel_id,
                persona_ids,
                workspace_rel=workspace_rel,
            )
            self.__class__._file_cache.clear()

            # --- Show status panels for ALL personas (ephemeral) ---
            ws = self._workspace(server_id=server_id, channel_id=target_channel_id)
            target_label = (
                f"<#{target_channel_id}>" if target_channel_id != self._resolve_parent_channel(interaction.channel_id)
                else "현재 채널"
            )
            cm._save_state(server_id, target_channel_id)

            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)

            await interaction.followup.send(
                f"채널 초기화 완료\n- 채널: {target_label}\n- workspace: `{ws}`",
                ephemeral=True,
            )

            sessions = self._get_channel_sessions(server_id, target_channel_id)
            for pid in self._settings.active_personas:
                persona_cfg = self._personas.get(pid)
                if not persona_cfg:
                    continue
                persona_state = cm.get_persona_state(server_id, target_channel_id, pid)
                provider_id = persona_state.active_provider
                provider_state = cm.get_provider_state(
                    server_id, target_channel_id, pid, provider_id,
                )
                pty_state = self._get_pty_state(
                    target_channel_id, provider_id,
                    server_id=server_id, persona_id=pid,
                )
                embed = _build_panel_embed(
                    pid, provider_id, persona_state, provider_state,
                    pty_state, sessions,
                )
                view = ControlPanelView(
                    persona_id=pid,
                    provider_id=provider_id,
                    state=persona_state,
                    provider_state=provider_state,
                    capabilities=self._core.get_provider_capabilities(provider_id),
                    enabled_providers=persona_cfg.enabled_providers,
                )
                msg = await interaction.followup.send(
                    embed=embed, view=view, wait=True, ephemeral=True,
                )
                persona_state.panel_message_id = msg.id

            cm._save_state(server_id, target_channel_id)

        @init_cmd.autocomplete("folder")
        async def init_folder_autocomplete(
            interaction: discord.Interaction, current: str,
        ) -> list[app_commands.Choice[str]]:
            """Directories under DEFAULT_WORKSPACE — drill down with /."""
            root = Path(self._default_workspace).resolve()
            normalized = current.strip().replace("\\", "/")

            if "/" in normalized:
                parent_rel, partial = normalized.rsplit("/", 1)
                parent = root / parent_rel
            else:
                parent_rel = ""
                partial = normalized
                parent = root

            if not parent.is_dir():
                return []

            partial_lower = partial.lower()
            choices: list[app_commands.Choice[str]] = []
            try:
                for item in sorted(parent.iterdir()):
                    if item.name.startswith("."):
                        continue
                    if not item.is_dir():
                        continue
                    if partial_lower and not item.name.lower().startswith(partial_lower):
                        continue
                    rel = f"{parent_rel}/{item.name}" if parent_rel else item.name
                    choices.append(app_commands.Choice(name=f"{rel}/", value=rel))
                    if len(choices) >= 25:
                        break
            except PermissionError:
                pass

            if not choices and normalized:
                choices.append(app_commands.Choice(
                    name=f"{normalized} (새 폴더 생성)",
                    value=normalized,
                ))
            return choices

        @self._bot.tree.command(
            name="reset",
            description="Reset PTY session in this channel",
        )
        @app_commands.describe(persona="대상 페르소나")
        async def reset(interaction: discord.Interaction, persona: str | None = None) -> None:
            if await self._reject_explorer_thread_command(interaction):
                return
            pid = self._resolve_cmd_persona(persona)
            if not self._check_access(interaction, pid):
                await interaction.response.send_message(_denied, ephemeral=True)
                return
            server_id = str(interaction.guild_id) if interaction.guild_id else "0"
            channel_id = self._resolve_parent_channel(interaction.channel_id)
            provider_id = self._active_provider_for(server_id, channel_id, pid)
            await interaction.response.defer(ephemeral=True)
            try:
                await self._core.reset_session(
                    persona_id=pid,
                    provider_id=provider_id,
                    channel_id=channel_id,
                    workspace=str(self._workspace(server_id=server_id, channel_id=channel_id)),
                )
                await interaction.followup.send(
                    f"[{pid}/{provider_id}] PTY session reset successfully.", ephemeral=True,
                )
            except Exception as exc:
                await interaction.followup.send(
                    f"Reset failed: {exc}", ephemeral=True,
                )

        @reset.autocomplete("persona")
        async def reset_persona_ac(
            interaction: discord.Interaction, current: str,
        ) -> list[app_commands.Choice[str]]:
            return self._persona_autocomplete()

        # -- CLI passthrough commands (fire-and-forget) --

        @self._bot.tree.command(
            name="model",
            description="Change active provider model",
        )
        @app_commands.describe(name="Model name", persona="대상 페르소나")
        async def model(interaction: discord.Interaction, name: str, persona: str | None = None) -> None:
            if await self._reject_explorer_thread_command(interaction):
                return
            pid = self._resolve_cmd_persona(persona)
            if not self._check_access(interaction, pid):
                await interaction.response.send_message(_denied, ephemeral=True)
                return
            server_id = str(interaction.guild_id) if interaction.guild_id else "0"
            channel_id = self._resolve_parent_channel(interaction.channel_id)
            provider_id = self._active_provider_for(server_id, channel_id, pid)
            name = name.strip()
            if not self._supports_for(server_id, channel_id, pid, "model"):
                await interaction.response.send_message(
                    f"{provider_id}에서는 `model` 제어를 지원하지 않습니다.",
                    ephemeral=True,
                )
                return
            if not name:
                await interaction.response.send_message(
                    "model 이름이 비어 있습니다.",
                    ephemeral=True,
                )
                return
            if name == "default":
                await interaction.response.send_message(
                    "`default` model은 더 이상 사용하지 않습니다. 명시적인 모델명을 선택하세요.",
                    ephemeral=True,
                )
                return
            if provider_id == "claude" and name not in {value for _, value in _CLAUDE_MODEL_CHOICES}:
                await interaction.response.send_message(
                    "Claude model은 `opus/sonnet/haiku` 중 하나여야 합니다.",
                    ephemeral=True,
                )
                return
            try:
                state = self._core.channel_mgr.get_provider_state(
                    server_id, channel_id, pid, provider_id,
                )
                state.model = name
                self._invalidate_provider_resume_for(server_id, channel_id, provider_id, pid)
                self._core.channel_mgr._save_state(server_id, channel_id)
                await interaction.response.send_message(
                    f"[{pid}] Model \u2192 {name} (saved; next turn will apply)",
                    ephemeral=True,
                )
            except Exception as exc:
                await interaction.response.send_message(f"Failed: {exc}", ephemeral=True)
            await self._update_panel_for(server_id, channel_id, pid)

        @model.autocomplete("name")
        async def model_autocomplete(
            interaction: discord.Interaction,
            current: str,
        ) -> list[app_commands.Choice[str]]:
            server_id = str(interaction.guild_id) if interaction.guild_id else "0"
            channel_id = self._resolve_parent_channel(interaction.channel_id)
            pid = self._default_persona_id
            provider_id = self._active_provider_for(server_id, channel_id, pid)
            provider_state = self._core.channel_mgr.get_provider_state(
                server_id, channel_id, pid, provider_id,
            )
            specs = _provider_model_specs(provider_id, provider_state.model)
            current_norm = current.strip().casefold()
            if current_norm:
                specs = tuple(
                    (label, value) for label, value in specs
                    if current_norm in label.casefold() or current_norm in value.casefold()
                )
            return [
                app_commands.Choice(name=label[:100], value=value)
                for label, value in specs[:25]
            ]

        @model.autocomplete("persona")
        async def model_persona_ac(
            interaction: discord.Interaction, current: str,
        ) -> list[app_commands.Choice[str]]:
            return self._persona_autocomplete()

        @self._bot.tree.command(
            name="persona",
            description="Set persona level",
        )
        @app_commands.choices(level=[
            app_commands.Choice(name="LV1 · core", value=1),
            app_commands.Choice(name="LV2 · soft", value=2),
            app_commands.Choice(name="LV3 · medium", value=3),
            app_commands.Choice(name="LV4 · hard", value=4),
            app_commands.Choice(name="LV5 · masquerade", value=5),
        ])
        @app_commands.describe(persona="대상 페르소나")
        async def persona_cmd(
            interaction: discord.Interaction,
            level: app_commands.Choice[int],
            persona: str | None = None,
        ) -> None:
            if await self._reject_explorer_thread_command(interaction):
                return
            pid = self._resolve_cmd_persona(persona)
            if not self._check_access(interaction, pid):
                await interaction.response.send_message(_denied, ephemeral=True)
                return
            server_id = str(interaction.guild_id) if interaction.guild_id else "0"
            channel_id = self._resolve_parent_channel(interaction.channel_id)
            provider_id = self._active_provider_for(server_id, channel_id, pid)
            try:
                from ..models import PERSONA_LV_NAMES
                result = await self._core.switch_mode(
                    server_id,
                    pid,
                    provider_id,
                    channel_id,
                    str(self._workspace(server_id=server_id, channel_id=channel_id)),
                    level.value,
                )
                self._invalidate_provider_resume_for(server_id, channel_id, provider_id, pid)
                name = PERSONA_LV_NAMES.get(result, "?")
                await interaction.response.send_message(
                    f"[{pid}] Persona \u2192 LV{result}:{name} (saved; next turn will apply)",
                    ephemeral=True,
                )
            except Exception as exc:
                await interaction.response.send_message(f"Failed: {exc}", ephemeral=True)
            await self._update_panel_for(server_id, channel_id, pid)

        @persona_cmd.autocomplete("persona")
        async def persona_cmd_persona_ac(
            interaction: discord.Interaction, current: str,
        ) -> list[app_commands.Choice[str]]:
            return self._persona_autocomplete()

        @self._bot.tree.command(
            name="effort",
            description="Set effort level",
        )
        @app_commands.choices(level=[
            app_commands.Choice(name="xhigh", value="xhigh"),
            app_commands.Choice(name="high", value="high"),
            app_commands.Choice(name="medium", value="medium"),
            app_commands.Choice(name="low", value="low"),
        ])
        @app_commands.describe(persona="대상 페르소나")
        async def effort(
            interaction: discord.Interaction,
            level: app_commands.Choice[str],
            persona: str | None = None,
        ) -> None:
            if await self._reject_explorer_thread_command(interaction):
                return
            pid = self._resolve_cmd_persona(persona)
            if not self._check_access(interaction, pid):
                await interaction.response.send_message(_denied, ephemeral=True)
                return
            server_id = str(interaction.guild_id) if interaction.guild_id else "0"
            channel_id = self._resolve_parent_channel(interaction.channel_id)
            provider_id = self._active_provider_for(server_id, channel_id, pid)
            if not self._supports_for(server_id, channel_id, pid, "effort"):
                await interaction.response.send_message(
                    f"{provider_id}에서는 `effort` 제어를 지원하지 않습니다.",
                    ephemeral=True,
                )
                return
            try:
                state = self._core.channel_mgr.get_provider_state(
                    server_id, channel_id, pid, provider_id,
                )
                normalized = _normalize_effort_for_provider(provider_id, level.value)
                state.effort = normalized
                self._invalidate_provider_resume_for(server_id, channel_id, provider_id, pid)
                self._core.channel_mgr._save_state(server_id, channel_id)
                await interaction.response.send_message(
                    f"[{pid}] Effort \u2192 {normalized} (saved; next turn will apply)",
                    ephemeral=True,
                )
            except Exception as exc:
                await interaction.response.send_message(f"Failed: {exc}", ephemeral=True)
            await self._update_panel_for(server_id, channel_id, pid)

        @effort.autocomplete("persona")
        async def effort_persona_ac(
            interaction: discord.Interaction, current: str,
        ) -> list[app_commands.Choice[str]]:
            return self._persona_autocomplete()

        @self._bot.tree.command(
            name="compact",
            description="Compact context",
        )
        @app_commands.describe(persona="대상 페르소나")
        async def compact(interaction: discord.Interaction, persona: str | None = None) -> None:
            if await self._reject_explorer_thread_command(interaction):
                return
            pid = self._resolve_cmd_persona(persona)
            if not self._check_access(interaction, pid):
                await interaction.response.send_message(_denied, ephemeral=True)
                return
            server_id = str(interaction.guild_id) if interaction.guild_id else "0"
            channel_id = self._resolve_parent_channel(interaction.channel_id)
            if not self._supports_for(server_id, channel_id, pid, "compact"):
                provider_id = self._active_provider_for(server_id, channel_id, pid)
                await interaction.response.send_message(
                    f"{provider_id}에서는 `compact` 제어를 지원하지 않습니다.",
                    ephemeral=True,
                )
                return
            try:
                await self._send_provider_control_for(server_id, channel_id, "compact", pid)
                await interaction.response.send_message(f"[{pid}] Context compacted.", ephemeral=True)
            except Exception as exc:
                await interaction.response.send_message(f"Failed: {exc}", ephemeral=True)

        @compact.autocomplete("persona")
        async def compact_persona_ac(
            interaction: discord.Interaction, current: str,
        ) -> list[app_commands.Choice[str]]:
            return self._persona_autocomplete()

        @self._bot.tree.command(
            name="clear",
            description="Clear conversation",
        )
        @app_commands.describe(persona="대상 페르소나")
        async def clear(interaction: discord.Interaction, persona: str | None = None) -> None:
            if await self._reject_explorer_thread_command(interaction):
                return
            pid = self._resolve_cmd_persona(persona)
            if not self._check_access(interaction, pid):
                await interaction.response.send_message(_denied, ephemeral=True)
                return
            server_id = str(interaction.guild_id) if interaction.guild_id else "0"
            channel_id = self._resolve_parent_channel(interaction.channel_id)
            if not self._supports_for(server_id, channel_id, pid, "clear"):
                provider_id = self._active_provider_for(server_id, channel_id, pid)
                await interaction.response.send_message(
                    f"{provider_id}에서는 `clear` 제어를 지원하지 않습니다.",
                    ephemeral=True,
                )
                return
            try:
                await self._send_provider_control_for(server_id, channel_id, "clear", pid)
                await interaction.response.send_message(f"[{pid}] Conversation cleared.", ephemeral=True)
            except Exception as exc:
                await interaction.response.send_message(f"Failed: {exc}", ephemeral=True)

        @clear.autocomplete("persona")
        async def clear_persona_ac(
            interaction: discord.Interaction, current: str,
        ) -> list[app_commands.Choice[str]]:
            return self._persona_autocomplete()

        @self._bot.tree.command(
            name="interrupt",
            description="Interrupt current response",
        )
        @app_commands.describe(persona="대상 페르소나")
        async def interrupt(interaction: discord.Interaction, persona: str | None = None) -> None:
            if await self._reject_explorer_thread_command(interaction):
                return
            pid = self._resolve_cmd_persona(persona)
            if not self._check_access(interaction, pid):
                await interaction.response.send_message(_denied, ephemeral=True)
                return
            server_id = str(interaction.guild_id) if interaction.guild_id else "0"
            channel_id = self._resolve_parent_channel(interaction.channel_id)
            provider_id = self._active_provider_for(server_id, channel_id, pid)
            if not self._supports_for(server_id, channel_id, pid, "interrupt"):
                await interaction.response.send_message(
                    f"{provider_id}에서는 `interrupt` 제어를 지원하지 않습니다.",
                    ephemeral=True,
                )
                return
            try:
                await self._core.interrupt_session(
                    pid,
                    provider_id,
                    channel_id,
                    str(self._workspace(server_id=server_id, channel_id=channel_id)),
                )
                await interaction.response.send_message(f"[{pid}] Interrupt sent.", ephemeral=True)
            except Exception as exc:
                await interaction.response.send_message(f"Failed: {exc}", ephemeral=True)

        @interrupt.autocomplete("persona")
        async def interrupt_persona_ac(
            interaction: discord.Interaction, current: str,
        ) -> list[app_commands.Choice[str]]:
            return self._persona_autocomplete()

        @self._bot.tree.command(
            name="permission",
            description="Cycle permission mode",
        )
        @app_commands.describe(persona="대상 페르소나")
        async def permission(interaction: discord.Interaction, persona: str | None = None) -> None:
            if await self._reject_explorer_thread_command(interaction):
                return
            pid = self._resolve_cmd_persona(persona)
            if not self._check_access(interaction, pid):
                await interaction.response.send_message(_denied, ephemeral=True)
                return
            server_id = str(interaction.guild_id) if interaction.guild_id else "0"
            channel_id = self._resolve_parent_channel(interaction.channel_id)
            provider_id = self._active_provider_for(server_id, channel_id, pid)
            if not self._supports_for(server_id, channel_id, pid, "permission"):
                await interaction.response.send_message(
                    f"{provider_id}에서는 `permission` 제어를 지원하지 않습니다.",
                    ephemeral=True,
                )
                return
            try:
                state = self._core.channel_mgr.get_provider_state(
                    server_id, channel_id, pid, provider_id,
                )
                order = ("plan", "bypass")
                try:
                    idx = order.index(state.permission)
                    state.permission = order[(idx + 1) % len(order)]
                except ValueError:
                    state.permission = "bypass"
                self._invalidate_provider_resume_for(server_id, channel_id, provider_id, pid)
                self._core.channel_mgr._save_state(server_id, channel_id)
                await interaction.response.send_message(
                    f"[{pid}] Permission \u2192 {state.permission} (saved; next turn will apply)",
                    ephemeral=True,
                )
            except Exception as exc:
                await interaction.response.send_message(f"Failed: {exc}", ephemeral=True)

        @permission.autocomplete("persona")
        async def permission_persona_ac(
            interaction: discord.Interaction, current: str,
        ) -> list[app_commands.Choice[str]]:
            return self._persona_autocomplete()

        @self._bot.tree.command(
            name="provider",
            description="Switch active provider",
        )
        @app_commands.choices(name=[
            app_commands.Choice(name="claude", value="claude"),
            app_commands.Choice(name="codex", value="codex"),
        ])
        @app_commands.describe(persona="대상 페르소나")
        async def provider(
            interaction: discord.Interaction,
            name: app_commands.Choice[str],
            persona: str | None = None,
        ) -> None:
            if await self._reject_explorer_thread_command(interaction):
                return
            pid = self._resolve_cmd_persona(persona)
            if not self._check_access(interaction, pid):
                await interaction.response.send_message(_denied, ephemeral=True)
                return
            persona_cfg = self._personas.get(pid)
            enabled = persona_cfg.enabled_providers if persona_cfg else ()
            if name.value not in enabled or name.value not in self._core._settings.providers:
                await interaction.response.send_message(
                    f"{name.value} provider는 {pid}에서 사용할 수 없습니다.",
                    ephemeral=True,
                )
                return
            server_id = str(interaction.guild_id) if interaction.guild_id else "0"
            channel_id = self._resolve_parent_channel(interaction.channel_id)
            self._core.channel_mgr.set_active_provider(server_id, channel_id, pid, name.value)
            await interaction.response.send_message(
                f"[{pid}] Active provider \u2192 **{name.value}** (saved; next turn will apply)",
                ephemeral=True,
            )
            await self._update_panel_for(server_id, channel_id, pid)

        @provider.autocomplete("persona")
        async def provider_persona_ac(
            interaction: discord.Interaction, current: str,
        ) -> list[app_commands.Choice[str]]:
            return self._persona_autocomplete()

        # -- Status / Control panel --

        @self._bot.tree.command(
            name="status",
            description="Show status & control panel",
        )
        @app_commands.describe(persona="대상 페르소나")
        async def status(interaction: discord.Interaction, persona: str | None = None) -> None:
            if await self._reject_explorer_thread_command(interaction):
                return
            pid = self._resolve_cmd_persona(persona)
            server_id = str(interaction.guild_id) if interaction.guild_id else "0"
            channel_id = self._resolve_parent_channel(interaction.channel_id)
            await self._show_status_panel(interaction, server_id, channel_id, persona_id=pid)

        @status.autocomplete("persona")
        async def status_persona_ac(
            interaction: discord.Interaction, current: str,
        ) -> list[app_commands.Choice[str]]:
            return self._persona_autocomplete()

        @self._bot.tree.command(
            name="instruct",
            description="Send additional instructions to the session",
        )
        @app_commands.describe(persona="대상 페르소나")
        async def instruct(interaction: discord.Interaction, persona: str | None = None) -> None:
            if await self._reject_explorer_thread_command(interaction):
                return
            pid = self._resolve_cmd_persona(persona)
            await interaction.response.send_modal(InstructModal(self, pid))

        @instruct.autocomplete("persona")
        async def instruct_persona_ac(
            interaction: discord.Interaction, current: str,
        ) -> list[app_commands.Choice[str]]:
            return self._persona_autocomplete()

        # -- File explorer --

        @self._bot.tree.command(
            name="browse",
            description="Open file explorer thread",
        )
        async def browse_cmd(interaction: discord.Interaction) -> None:
            if await self._reject_explorer_thread_command(interaction):
                return
            if not self._check_access(interaction):
                await interaction.response.send_message(
                    _denied, ephemeral=True,
                )
                return

            # Check for existing explorer thread (verify it still exists)
            stale_tid = None
            for state in self._explorer_states.values():
                if state.parent_channel_id == interaction.channel_id:
                    thread = self._bot.get_channel(state.thread_id)
                    if not thread:
                        try:
                            thread = await self._bot.fetch_channel(state.thread_id)
                        except (discord.NotFound, discord.Forbidden):
                            thread = None
                    if thread:
                        await interaction.response.send_message(
                            f"\uc774\ubbf8 \uc5f4\ub9bc: <#{state.thread_id}>",
                            ephemeral=True,
                        )
                        return
                    # Thread gone — clean up stale state
                    stale_tid = state.thread_id
                    break
            if stale_tid:
                del self._explorer_states[stale_tid]
                self._save_explorer_states()

            await interaction.response.defer(ephemeral=True)
            server_id = str(interaction.guild_id) if interaction.guild_id else "0"
            ws = self._workspace(server_id=server_id, channel_id=interaction.channel_id)
            thread = await interaction.channel.create_thread(
                name=f"\U0001f4c1 {ws.name}",
                type=discord.ChannelType.public_thread,
                auto_archive_duration=1440,  # 24h
            )
            state = ExplorerState(
                thread_id=thread.id,
                parent_channel_id=interaction.channel_id,
            )
            embed = _build_explorer_embed(ws, state)
            view = _build_explorer_view(ws, state)
            msg = await thread.send(embed=embed, view=view)
            state.message_id = msg.id
            self._explorer_states[thread.id] = state
            self._save_explorer_states()
            await interaction.followup.send(
                f"\ud30c\uc77c \ud0d0\uc0c9\uae30: {thread.mention}", ephemeral=True,
            )

    # ------------------------------------------------------------------
    # Panel helpers
    # ------------------------------------------------------------------

    def _resolve_parent_channel(self, channel_id: int) -> int:
        """Map explorer thread ID back to parent channel ID."""
        state = self._explorer_states.get(channel_id)
        return state.parent_channel_id if state else channel_id

    def _is_explorer_thread(self, channel_id: int) -> bool:
        return channel_id in self._explorer_states

    def _check_access_admin(
        self, interaction: discord.Interaction, persona_id: str | None = None,
    ) -> bool:
        """Administrative access for changing access policy itself."""
        pid = persona_id or self._default_persona_id
        server_id = str(interaction.guild_id) if interaction.guild_id else "0"
        cm = self._core.channel_mgr
        parent_ch = self._resolve_parent_channel(interaction.channel_id)
        protagonist = cm.get_protagonist(server_id, parent_ch, pid)
        return not protagonist or str(interaction.user.id) == protagonist

    async def _reject_explorer_thread_command(
        self, interaction: discord.Interaction,
    ) -> bool:
        """Explorer threads are UI-only; LLM commands must run in the parent channel."""
        if not self._is_explorer_thread(interaction.channel_id):
            return False
        parent_ch = self._resolve_parent_channel(interaction.channel_id)
        await interaction.response.send_message(
            f"파일 탐색기 스레드는 UI 전용입니다. 상위 채널 <#{parent_ch}> 에서 명령을 사용해주세요.",
            ephemeral=True,
        )
        return True

    def _check_access(
        self, interaction: discord.Interaction, persona_id: str | None = None,
    ) -> bool:
        """Return True if the user may use control commands.

        For explorer threads, checks the *parent* channel's access setting.
        When *persona_id* is given, checks that persona's access policy;
        otherwise uses the default persona.
        """
        pid = persona_id or self._default_persona_id
        server_id = str(interaction.guild_id) if interaction.guild_id else "0"
        cm = self._core.channel_mgr
        parent_ch = self._resolve_parent_channel(interaction.channel_id)
        ps = cm.get_persona_state(server_id, parent_ch, pid)
        if ps.command_access == "public":
            return True
        protagonist = cm.get_protagonist(server_id, parent_ch, pid)
        return not protagonist or str(interaction.user.id) == protagonist

    def _active_provider_for(self, server_id: str, channel_id: int, persona_id: str) -> str:
        """Get active provider for a specific persona."""
        persona_cfg = self._personas.get(persona_id)
        if not persona_cfg:
            return self._core._settings.default_provider
        provider_id = self._core.channel_mgr.get_active_provider(
            server_id, channel_id, persona_id,
        )
        if (
            provider_id not in persona_cfg.enabled_providers
            or provider_id not in self._core._settings.providers
        ):
            provider_id = next(
                (
                    candidate for candidate in persona_cfg.enabled_providers
                    if candidate in self._core._settings.providers
                ),
                self._core._settings.default_provider,
            )
            self._core.channel_mgr.set_active_provider(
                server_id, channel_id, persona_id, provider_id,
            )
        return provider_id

    def _supports_for(self, server_id: str, channel_id: int, persona_id: str, control: str) -> bool:
        return self._core.supports_control(
            self._active_provider_for(server_id, channel_id, persona_id), control,
        )

    async def _send_provider_control_for(
        self,
        server_id: str,
        channel_id: int,
        control: str,
        persona_id: str,
        *,
        value: str | None = None,
    ) -> None:
        provider_id = self._active_provider_for(server_id, channel_id, persona_id)
        if not self._core.supports_control(provider_id, control):
            raise RuntimeError(f"{provider_id} provider does not support {control}")
        adapter = self._core.get_provider_adapter(provider_id)
        if adapter.control_requires_restart(control):
            from ..models import SessionKey

            key = SessionKey(
                persona_id=persona_id,
                provider_id=provider_id,
                channel_id=channel_id,
                workspace=str(self._workspace(server_id=server_id, channel_id=channel_id)),
            )
            if not self._core._registry.get_session(key):
                return
            await self._core.reset_session(
                persona_id,
                provider_id,
                channel_id,
                str(self._workspace(server_id=server_id, channel_id=channel_id)),
            )
            return
        await self._core.run_runtime_control(
            server_id,
            persona_id,
            provider_id,
            channel_id,
            str(self._workspace(server_id=server_id, channel_id=channel_id)),
            control,
            value=value,
        )

    def _invalidate_provider_resume_for(
        self,
        server_id: str,
        channel_id: int,
        provider_id: str,
        persona_id: str,
    ) -> None:
        state = self._core.channel_mgr.get_provider_state(
            server_id, channel_id, persona_id, provider_id,
        )
        state.cli_session_id = None

    def _get_pty_state(
        self,
        channel_id: int,
        provider_id: str | None = None,
        *,
        server_id: str = "0",
        persona_id: str | None = None,
    ) -> str:
        """Get the PTY state string for a channel's session."""
        from ..models import SessionKey

        resolved_provider = provider_id or self._core._settings.default_provider
        key = SessionKey(
            persona_id=persona_id or self._default_persona_id,
            provider_id=resolved_provider,
            channel_id=channel_id,
            workspace=str(self._workspace(server_id=server_id, channel_id=channel_id)),
        )
        session = self._core._registry.get_session(key)
        if not session:
            return "offline"
        return session.state.value

    def _get_channel_sessions(self, server_id: str, channel_id: int) -> list[dict]:
        """Get desired active status of all personas in this channel."""
        results = []
        for persona_id in self._core._settings.active_personas:
            persona_state = self._core.channel_mgr.get_persona_state(
                server_id, channel_id, persona_id,
            )
            provider_id = persona_state.active_provider
            provider_state = self._core.channel_mgr.get_provider_state(
                server_id, channel_id, persona_id, provider_id,
            )
            results.append({
                "persona_id": persona_id,
                "provider_id": provider_id,
                "state": self._get_pty_state(
                    channel_id,
                    provider_id,
                    server_id=server_id,
                    persona_id=persona_id,
                ),
                "model": provider_state.model,
                "effort": provider_state.effort,
                "permission": provider_state.permission,
                "mode": provider_state.mode,
                "fast": provider_state.fast,
                "active": True,
            })
        return results

    async def _show_status_panel(
        self,
        interaction: discord.Interaction,
        server_id: str,
        channel_id: int,
        *,
        persona_id: str | None = None,
        ensure_state_saved: bool = False,
        summary_lines: list[str] | None = None,
    ) -> None:
        pid = persona_id or self._default_persona_id
        persona_cfg = self._personas.get(pid)
        cm = self._core.channel_mgr
        persona_state = cm.get_persona_state(server_id, channel_id, pid)
        provider_id = persona_state.active_provider
        provider_state = cm.get_provider_state(server_id, channel_id, pid, provider_id)

        if ensure_state_saved:
            cm._save_state(server_id, channel_id)

        if not interaction.response.is_done():
            await interaction.response.defer()

        usage: dict[str, str] = {}
        if self._supports_for(server_id, channel_id, pid, "usage"):
            try:
                usage = await self._core.query_usage(
                    pid,
                    provider_id,
                    channel_id,
                    str(self._workspace(server_id=server_id, channel_id=channel_id)),
                )
            except Exception:
                pass

        pty_state = self._get_pty_state(
            channel_id, provider_id, server_id=server_id, persona_id=pid,
        )
        sessions = self._get_channel_sessions(server_id, channel_id)
        embed = _build_panel_embed(
            pid, provider_id, persona_state, provider_state, pty_state, sessions, usage,
        )
        enabled = persona_cfg.enabled_providers if persona_cfg else ("claude",)
        view = ControlPanelView(
            persona_id=pid,
            provider_id=provider_id,
            state=persona_state,
            provider_state=provider_state,
            capabilities=self._core.get_provider_capabilities(provider_id),
            enabled_providers=enabled,
        )

        if persona_state.panel_message_id:
            try:
                ch = interaction.channel
                old_msg = await ch.fetch_message(persona_state.panel_message_id)
                await old_msg.delete()
            except discord.HTTPException:
                pass

        if summary_lines:
            await interaction.followup.send(
                "채널 기본 설정을 초기화했습니다.\n"
                + "\n".join(f"- {line}" for line in summary_lines),
                ephemeral=True,
            )

        msg = await interaction.followup.send(embed=embed, view=view, wait=True)
        persona_state.panel_message_id = msg.id
        cm._save_state(server_id, channel_id)

    async def _rebuild_panel_for(
        self, interaction: discord.Interaction,
        server_id: str, channel_id: int,
        persona_id: str,
    ) -> None:
        """Rebuild panel embed + view after a setting change."""
        pid = persona_id
        persona_cfg = self._personas.get(pid)
        persona_state = self._core.channel_mgr.get_persona_state(server_id, channel_id, pid)
        provider_id = persona_state.active_provider
        provider_state = self._core.channel_mgr.get_provider_state(
            server_id, channel_id, pid, provider_id,
        )
        pty_state = self._get_pty_state(
            channel_id, provider_id, server_id=server_id, persona_id=pid,
        )
        sessions = self._get_channel_sessions(server_id, channel_id)
        embed = _build_panel_embed(
            pid, provider_id, persona_state, provider_state, pty_state, sessions,
        )
        enabled = persona_cfg.enabled_providers if persona_cfg else ("claude",)
        new_view = ControlPanelView(
            persona_id=pid,
            provider_id=provider_id,
            state=persona_state,
            provider_state=provider_state,
            capabilities=self._core.get_provider_capabilities(provider_id),
            enabled_providers=enabled,
        )

        try:
            if not interaction.response.is_done():
                await interaction.response.edit_message(embed=embed, view=new_view)
            else:
                message = getattr(interaction, "message", None)
                if message is not None:
                    await message.edit(embed=embed, view=new_view)
                else:
                    await self._update_panel_for(server_id, channel_id, pid)
        except (discord.NotFound, discord.HTTPException):
            await self._update_panel_for(server_id, channel_id, pid)

    async def _update_panel_for(self, server_id: str, channel_id: int, persona_id: str) -> None:
        """Update the control panel embed if it exists in this channel."""
        pid = persona_id
        persona_cfg = self._personas.get(pid)
        cm = self._core.channel_mgr
        persona_state = cm.get_persona_state(server_id, channel_id, pid)
        if not persona_state.panel_message_id:
            return

        channel = self._bot.get_channel(channel_id)
        if not channel or not isinstance(channel, discord.abc.Messageable):
            return

        provider_id = persona_state.active_provider
        provider_state = cm.get_provider_state(server_id, channel_id, pid, provider_id)
        pty_state = self._get_pty_state(
            channel_id, provider_id, server_id=server_id, persona_id=pid,
        )
        sessions = self._get_channel_sessions(server_id, channel_id)
        embed = _build_panel_embed(
            pid, provider_id, persona_state, provider_state, pty_state, sessions,
        )
        enabled = persona_cfg.enabled_providers if persona_cfg else ("claude",)
        view = ControlPanelView(
            persona_id=pid,
            provider_id=provider_id,
            state=persona_state,
            provider_state=provider_state,
            capabilities=self._core.get_provider_capabilities(provider_id),
            enabled_providers=enabled,
        )

        try:
            msg = await channel.fetch_message(persona_state.panel_message_id)
            await msg.edit(embed=embed, view=view)
        except discord.NotFound:
            persona_state.panel_message_id = None
            cm._save_state(server_id, channel_id)
        except discord.HTTPException:
            pass

    # ------------------------------------------------------------------
    # File Explorer handlers
    # ------------------------------------------------------------------

    async def _handle_explorer_select(
        self, interaction: discord.Interaction, selected: str,
    ) -> None:
        if selected == "_empty":
            await interaction.response.defer()
            return
        if not self._check_access(interaction):
            await interaction.response.send_message(
                "\uad8c\ud55c \uc5c6\uc74c", ephemeral=True,
            )
            return
        state = self._explorer_states.get(interaction.channel_id)
        if not state:
            await interaction.response.send_message("Explorer not found", ephemeral=True)
            return
        server_id = str(interaction.guild_id) if interaction.guild_id else "0"
        ws = self._workspace(server_id=server_id, channel_id=state.parent_channel_id)
        target = (ws / selected).resolve()
        try:
            target.relative_to(ws.resolve())
        except ValueError:
            await interaction.response.send_message("Invalid path", ephemeral=True)
            return

        if target.is_dir():
            # Show folder actions: navigate or delete
            count = sum(1 for _ in target.iterdir() if not _.name.startswith("."))
            view = FolderActionView(self, selected, interaction.channel_id)
            await interaction.response.send_message(
                f"\U0001f4c2 **{target.name}/** ({count} items)",
                view=view,
                ephemeral=True,
            )
        elif target.is_file():
            # Show file actions: download + delete confirm
            size = target.stat().st_size
            if size > 10 * 1024 * 1024:
                sz_label = f"{size / 1024 / 1024:.1f} MB"
            elif size > 1024:
                sz_label = f"{size / 1024:.1f} KB"
            else:
                sz_label = f"{size} B"
            view = FileActionView(self, selected, interaction.channel_id)
            await interaction.response.send_message(
                f"\U0001f4c4 **{target.name}** ({sz_label})",
                view=view,
                ephemeral=True,
            )

    async def _handle_explorer_button(
        self, interaction: discord.Interaction, custom_id: str,
    ) -> None:
        if not self._check_access(interaction):
            await interaction.response.send_message(
                "\uad8c\ud55c \uc5c6\uc74c", ephemeral=True,
            )
            return
        state = self._explorer_states.get(interaction.channel_id)
        if not state:
            await interaction.response.send_message("Explorer not found", ephemeral=True)
            return
        server_id = str(interaction.guild_id) if interaction.guild_id else "0"
        ws = self._workspace(server_id=server_id, channel_id=state.parent_channel_id)

        if custom_id == "explorer:up":
            if state.current_path:
                parent = str(Path(state.current_path).parent)
                state.current_path = "" if parent == "." else parent
                self._save_explorer_states()
            await self._refresh_explorer(interaction, state)

        elif custom_id == "explorer:new":
            await interaction.response.send_modal(
                NewFolderModal(self, interaction.channel_id),
            )

        elif custom_id == "explorer:del":
            await interaction.response.send_message(
                "Select \uba54\ub274\uc5d0\uc11c \ud56d\ubaa9\uc744 \uc120\ud0dd\ud558\uba74 \uc0ad\uc81c \ud655\uc778\uc774 \ub098\ud0c0\ub0a9\ub2c8\ub2e4.",
                ephemeral=True,
            )

        elif custom_id == "explorer:get":
            # Download current folder as zip
            await interaction.response.defer()
            target = (ws / state.current_path).resolve() if state.current_path else ws.resolve()
            tmp_zip = self._tmp_dir(
                server_id=server_id,
                channel_id=state.parent_channel_id,
            ) / f"{target.name}.zip"
            try:
                with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED) as zf:
                    for f in target.rglob("*"):
                        if f.is_file() and not any(p.startswith(".") for p in f.relative_to(target).parts):
                            zf.write(f, f.relative_to(target))
                if tmp_zip.stat().st_size > 10 * 1024 * 1024:
                    await interaction.followup.send("Zip too large (>10MB)")
                else:
                    await interaction.followup.send(
                        file=discord.File(str(tmp_zip), filename=tmp_zip.name),
                    )
            finally:
                tmp_zip.unlink(missing_ok=True)

        elif custom_id == "explorer:link":
            rel = state.current_path.replace("\\", "/") if state.current_path else ""
            display = rel or ws.name
            await interaction.response.send_message(f"`{display}`", ephemeral=True)

        elif custom_id == "explorer:refresh":
            await self._refresh_explorer(interaction, state)

    async def _refresh_explorer(
        self, interaction: discord.Interaction, state: ExplorerState,
    ) -> None:
        """Rebuild embed + view and edit the explorer message."""
        server_id = str(interaction.guild_id) if interaction.guild_id else "0"
        ws = self._workspace(server_id=server_id, channel_id=state.parent_channel_id)
        embed = _build_explorer_embed(ws, state)
        view = _build_explorer_view(ws, state)
        if not interaction.response.is_done():
            await interaction.response.edit_message(embed=embed, view=view)
        elif state.message_id:
            channel = self._bot.get_channel(state.thread_id)
            if channel:
                try:
                    msg = await channel.fetch_message(state.message_id)
                    await msg.edit(embed=embed, view=view)
                except discord.HTTPException:
                    pass

    async def _explorer_save_attachments(self, message: discord.Message) -> None:
        """Save attachments dropped in explorer thread to current directory."""
        state = self._explorer_states.get(message.channel.id)
        if not state:
            return
        # Access check: use parent channel's settings
        pid = self._default_persona_id
        server_id = str(message.guild.id) if message.guild else "0"
        cm = self._core.channel_mgr
        parent_ch = state.parent_channel_id
        ps = cm.get_persona_state(server_id, parent_ch, pid)
        if ps.command_access != "public":
            protagonist = cm.get_protagonist(server_id, parent_ch, pid)
            if protagonist and str(message.author.id) != protagonist:
                return  # silently ignore unauthorized uploads
        ws = self._workspace(server_id=server_id, channel_id=parent_ch)
        target_dir = (ws / state.current_path).resolve() if state.current_path else ws.resolve()
        try:
            target_dir.relative_to(ws.resolve())
        except ValueError:
            return

        saved: list[str] = []
        for att in message.attachments:
            safe_name = re.sub(r'[<>:"|?*]', "_", att.filename).lstrip(".")
            if not safe_name:
                safe_name = f"file_{att.id}"
            dest = target_dir / safe_name
            try:
                await att.save(dest)
                saved.append(safe_name)
            except Exception:
                LOGGER.warning("Failed to save explorer attachment: %s", safe_name)

        if saved:
            names = ", ".join(f"`{n}`" for n in saved)
            path_display = state.current_path or ws.name
            await message.reply(f"\u2705 {path_display}/ \uc5d0 \uc800\uc7a5: {names}", mention_author=False)
            # Refresh explorer embed
            if state.message_id:
                channel = self._bot.get_channel(state.thread_id)
                if channel:
                    try:
                        embed = _build_explorer_embed(ws, state)
                        view = _build_explorer_view(ws, state)
                        msg = await channel.fetch_message(state.message_id)
                        await msg.edit(embed=embed, view=view)
                    except discord.HTTPException:
                        pass

    # ------------------------------------------------------------------
    # Explorer state persistence
    # ------------------------------------------------------------------

    def _explorer_state_path(self) -> Path:
        return self._workspace() / ".explorer_state.json"

    def _save_explorer_states(self) -> None:
        data = {}
        for tid, s in self._explorer_states.items():
            data[str(tid)] = {
                "thread_id": s.thread_id,
                "parent_channel_id": s.parent_channel_id,
                "message_id": s.message_id,
                "current_path": s.current_path,
            }
        try:
            path = self._explorer_state_path()
            path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception:
            LOGGER.warning("Failed to save explorer state")

    def _load_explorer_states(self) -> None:
        path = self._explorer_state_path()
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            for tid_str, info in data.items():
                tid = int(tid_str)
                self._explorer_states[tid] = ExplorerState(
                    thread_id=info["thread_id"],
                    parent_channel_id=info["parent_channel_id"],
                    message_id=info.get("message_id"),
                    current_path=info.get("current_path", ""),
                )
            LOGGER.info("Loaded %d explorer state(s)", len(self._explorer_states))
        except Exception:
            LOGGER.warning("Failed to load explorer state")

    # ------------------------------------------------------------------
    # File I/O helpers
    # ------------------------------------------------------------------

    def _tmp_dir(
        self,
        server_id: str | None = None,
        channel_id: int | None = None,
    ) -> Path:
        """Shared temp directory for uploaded files — inside channel workspace."""
        d = self._workspace(server_id=server_id, channel_id=channel_id) / ".tmp"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _workspace(
        self,
        *,
        server_id: str | None = None,
        channel_id: int | None = None,
    ) -> Path:
        root = Path(self._default_workspace).resolve()
        if server_id is None or channel_id is None:
            return root
        resolved_channel_id = self._resolve_parent_channel(channel_id)
        workspace_rel = self._core.channel_mgr.get_workspace_rel(
            server_id, resolved_channel_id,
        )
        target = (root / workspace_rel).resolve() if workspace_rel else root
        try:
            target.relative_to(root)
        except ValueError:
            return root
        return target

    def _workspace_for_interaction(self, interaction: discord.Interaction) -> Path:
        server_id = str(interaction.guild_id) if interaction.guild_id else "0"
        return self._workspace(
            server_id=server_id,
            channel_id=interaction.channel_id,
        )

    def _workspace_for_message(self, message: discord.Message) -> Path:
        server_id = str(message.guild.id) if message.guild else "0"
        return self._workspace(
            server_id=server_id,
            channel_id=message.channel.id,
        )

    def _resolve_workspace_subdir(self, subdir: str) -> tuple[str, Path] | None:
        normalized = subdir.strip().replace("\\", "/").strip("/")
        if not normalized:
            return ("", Path(self._default_workspace).resolve())
        if ".." in normalized or normalized.startswith("/") or ":" in normalized:
            return None
        root = Path(self._default_workspace).resolve()
        target = (root / normalized).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            return None
        return (normalized, target)

    async def _save_attachments(self, message: discord.Message) -> tuple[str, ...]:
        """Download message attachments to workspace/.tmp/.

        Deduplicates across multiple bot instances by checking if the
        file already exists (same channel message → same attachment).
        """
        server_id = str(message.guild.id) if message.guild else "0"
        tmp = self._tmp_dir(server_id=server_id, channel_id=message.channel.id)
        saved_paths: list[str] = []
        for att in message.attachments:
            # Sanitize filename
            safe_name = re.sub(r'[<>:"|?*]', "_", att.filename)
            safe_name = safe_name.lstrip(".")
            if not safe_name:
                safe_name = f"file_{att.id}"
            stored_name = f"{att.id}_{safe_name}"
            dest = tmp / stored_name
            if dest.exists():
                saved_paths.append(f".tmp/{stored_name}")
                continue  # already downloaded by another bot instance
            try:
                await att.save(dest)
                LOGGER.info("Saved attachment: %s (%d bytes)", stored_name, att.size)
                saved_paths.append(f".tmp/{stored_name}")
            except Exception:
                LOGGER.warning("Failed to save attachment: %s", stored_name)
        return tuple(saved_paths)

    def _resolve_workspace_path(
        self,
        path_str: str,
        *,
        server_id: str | None = None,
        channel_id: int | None = None,
    ) -> Path | None:
        """Resolve a path relative to workspace. Returns None if unsafe."""
        # Block path traversal patterns
        if ".." in path_str or path_str.startswith("/") or ":" in path_str:
            return None
        ws = self._workspace(server_id=server_id, channel_id=channel_id).resolve()
        target = (ws / path_str).resolve()
        try:
            target.relative_to(ws)
        except ValueError:
            return None
        return target

    # File tree cache for autocomplete (rebuilt periodically)
    _file_cache: dict[str, tuple[float, list[str]]] = {}
    _FILE_CACHE_TTL = 30.0  # seconds

    def _list_files(
        self,
        prefix: str,
        *,
        server_id: str | None = None,
        channel_id: int | None = None,
        max_results: int = 25,
    ) -> list[str]:
        """List files/dirs in workspace matching prefix for autocomplete.

        Uses a cached file tree (rebuilt every 30s) to stay under
        Discord's 3-second autocomplete deadline.
        """
        import time
        now = time.monotonic()
        ws = self._workspace(server_id=server_id, channel_id=channel_id)
        cache_key = str(ws)

        # Rebuild cache if stale
        cached = self._file_cache.get(cache_key)
        if not cached or now - cached[0] > self._FILE_CACHE_TTL:
            cache: list[str] = []
            try:
                for item in sorted(ws.rglob("*")):
                    if any(p.startswith(".") for p in item.relative_to(ws).parts):
                        continue  # skip hidden files/dirs
                    rel = str(item.relative_to(ws)).replace("\\", "/")
                    if item.is_dir():
                        rel += "/"
                    cache.append(rel)
                    if len(cache) >= 500:  # cap cache size
                        break
            except PermissionError:
                pass
            self.__class__._file_cache[cache_key] = (now, cache)
        else:
            cache = cached[1]

        # Filter by prefix
        prefix_lower = prefix.lower()
        results = [
            f for f in cache
            if f.lower().startswith(prefix_lower)
        ]
        return results[:max_results]

    def _register_file_commands(self) -> None:
        """Register /a, /get, /put slash commands."""

        # -- /a: file reference with autocomplete --
        @self._bot.tree.command(
            name="a",
            description="Reference a file and send message to LLM",
        )
        @app_commands.describe(
            path="File path (autocomplete)",
            message="Message to send",
            persona="대상 페르소나",
        )
        async def a_cmd(
            interaction: discord.Interaction,
            path: str,
            message: str,
            persona: str | None = None,
        ) -> None:
            if await self._reject_explorer_thread_command(interaction):
                return
            server_id = str(interaction.guild_id) if interaction.guild_id else "0"
            channel_id = self._resolve_parent_channel(interaction.channel_id)
            resolved = self._resolve_workspace_path(
                path,
                server_id=server_id,
                channel_id=channel_id,
            )
            if not resolved or not resolved.exists():
                await interaction.response.send_message(f"File not found: {path}", ephemeral=True)
                return

            # Send as a regular message referencing the file
            full_text = f"[파일: {path}]\n{message}"
            await interaction.response.send_message(f"`{path}` {message}", ephemeral=False)
            visible_message = await interaction.original_response()
            pid = self._resolve_cmd_persona(persona)
            persona_cfg = self._personas.get(pid)
            provider_id = self._active_provider_for(server_id, channel_id, pid)

            self._core.log_channel_message(
                server_id=server_id,
                channel_id=channel_id,
                author_id=str(interaction.user.id),
                message_id=str(visible_message.id),
                author_name=interaction.user.display_name,
                text=full_text,
            )

            # Create incoming message for the LLM
            incoming = IncomingMessage(
                message_id=str(visible_message.id),
                server_id=server_id,
                channel_id=channel_id,
                author_id=str(interaction.user.id),
                author_name=interaction.user.display_name,
                text=full_text,
                workspace=str(self._workspace(server_id=server_id, channel_id=channel_id)),
                persona_id=pid,
                provider_id=provider_id,
                visible_message_id=str(visible_message.id),
                trigger_reason="slash_a",
                attachment_paths=(path,),
            )
            # /a uses webhook streamer for persona identity (fallback to bot reply)
            try:
                webhook = await self._get_webhook(visible_message.channel)
                streamer: WebhookStreamer | DiscordStreamer = WebhookStreamer(
                    webhook=webhook,
                    source_message=visible_message,
                    persona_name=persona_cfg.display_name if persona_cfg else pid,
                    avatar_url=persona_cfg.avatar_url if persona_cfg else None,
                    edit_interval=persona_cfg.message_edit_interval if persona_cfg else 2.0,
                )
            except (discord.Forbidden, discord.HTTPException):
                streamer = DiscordStreamer(
                    source_message=visible_message,
                    edit_interval=persona_cfg.message_edit_interval if persona_cfg else 2.0,
                )
            sink = DiscordOutputSink(streamer, self._bot, author_id=str(interaction.user.id))
            await self._core.handle_message(incoming, sink)

        @a_cmd.autocomplete("path")
        async def a_autocomplete(
            interaction: discord.Interaction, current: str,
        ) -> list[app_commands.Choice[str]]:
            server_id = str(interaction.guild_id) if interaction.guild_id else "0"
            results = self._list_files(
                current,
                server_id=server_id,
                channel_id=interaction.channel_id,
            )
            return [
                app_commands.Choice(name=r[:100], value=r[:100])
                for r in results
            ]

        @a_cmd.autocomplete("persona")
        async def a_persona_ac(
            interaction: discord.Interaction, current: str,
        ) -> list[app_commands.Choice[str]]:
            return self._persona_autocomplete()

        # -- /get: download file from workspace --
        @self._bot.tree.command(
            name="get",
            description="Download a file/folder from workspace",
        )
        @app_commands.describe(path="File or folder path")
        async def get_cmd(interaction: discord.Interaction, path: str) -> None:
            if await self._reject_explorer_thread_command(interaction):
                return
            await interaction.response.defer(ephemeral=False)
            server_id = str(interaction.guild_id) if interaction.guild_id else "0"
            channel_id = self._resolve_parent_channel(interaction.channel_id)
            resolved = self._resolve_workspace_path(
                path,
                server_id=server_id,
                channel_id=channel_id,
            )
            if not resolved or not resolved.exists():
                await interaction.followup.send(f"Not found: `{path}`")
                return

            try:
                MAX_SIZE = 10 * 1024 * 1024
                if resolved.is_file():
                    size = resolved.stat().st_size
                    if size > MAX_SIZE:
                        await interaction.followup.send(
                            f"File too large: {size / 1024 / 1024:.1f}MB (max 10MB). "
                            f"Try a subdirectory or specific file."
                        )
                        return
                    await interaction.followup.send(
                        f"`{path}`",
                        file=discord.File(str(resolved), filename=resolved.name),
                    )
                elif resolved.is_dir():
                    # Zip the folder
                    zip_path = self._tmp_dir(
                        server_id=server_id,
                        channel_id=channel_id,
                    ) / f"{resolved.name}.zip"
                    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                        for f in resolved.rglob("*"):
                            if f.is_file():
                                zf.write(f, f.relative_to(resolved))
                    size = zip_path.stat().st_size
                    if size > MAX_SIZE:
                        zip_path.unlink()
                        await interaction.followup.send(
                            f"Zip too large: {size / 1024 / 1024:.1f}MB (max 10MB). "
                            f"Try a smaller subdirectory."
                        )
                        return
                    await interaction.followup.send(
                        f"`{path}/` (zipped)",
                        file=discord.File(str(zip_path), filename=zip_path.name),
                    )
                    zip_path.unlink()
            except Exception as exc:
                await interaction.followup.send(f"Failed: {exc}")

        @get_cmd.autocomplete("path")
        async def get_autocomplete(
            interaction: discord.Interaction, current: str,
        ) -> list[app_commands.Choice[str]]:
            server_id = str(interaction.guild_id) if interaction.guild_id else "0"
            results = self._list_files(
                current,
                server_id=server_id,
                channel_id=interaction.channel_id,
            )
            return [app_commands.Choice(name=r[:100], value=r[:100]) for r in results]

        # -- /put: save temp file to workspace --
        @self._bot.tree.command(
            name="put",
            description="Save uploaded file to workspace path",
        )
        @app_commands.describe(
            filename="File from .tmp (autocomplete)",
            destination="Destination path in workspace",
        )
        async def put_cmd(
            interaction: discord.Interaction, filename: str, destination: str,
        ) -> None:
            if await self._reject_explorer_thread_command(interaction):
                return
            server_id = str(interaction.guild_id) if interaction.guild_id else "0"
            channel_id = self._resolve_parent_channel(interaction.channel_id)
            src = self._tmp_dir(server_id=server_id, channel_id=channel_id) / filename
            if not src.exists():
                await interaction.response.send_message(
                    f"Temp file not found: `{filename}`", ephemeral=True,
                )
                return

            dest = self._resolve_workspace_path(
                destination,
                server_id=server_id,
                channel_id=channel_id,
            )
            if not dest:
                await interaction.response.send_message("Invalid path.", ephemeral=True)
                return

            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dest))
                await interaction.response.send_message(
                    f"Saved: `{filename}` → `{destination}`", ephemeral=False,
                )
            except Exception as exc:
                await interaction.response.send_message(f"Failed: {exc}", ephemeral=True)

        @put_cmd.autocomplete("filename")
        async def put_filename_autocomplete(
            interaction: discord.Interaction, current: str,
        ) -> list[app_commands.Choice[str]]:
            server_id = str(interaction.guild_id) if interaction.guild_id else "0"
            tmp = self._tmp_dir(server_id=server_id, channel_id=interaction.channel_id)
            files = [f.name for f in tmp.iterdir() if f.is_file()]
            filtered = [f for f in files if current.lower() in f.lower()]
            return [app_commands.Choice(name=f[:100], value=f[:100]) for f in filtered[:25]]

        @put_cmd.autocomplete("destination")
        async def put_dest_autocomplete(
            interaction: discord.Interaction, current: str,
        ) -> list[app_commands.Choice[str]]:
            server_id = str(interaction.guild_id) if interaction.guild_id else "0"
            results = self._list_files(
                current,
                server_id=server_id,
                channel_id=interaction.channel_id,
            )
            return [app_commands.Choice(name=r[:100], value=r[:100]) for r in results]

    # ------------------------------------------------------------------
    # Transport interface
    # ------------------------------------------------------------------

    async def start(self) -> None:
        await self._bot.start(self._settings.bot_token)

    async def stop(self) -> None:
        await self._bot.close()
