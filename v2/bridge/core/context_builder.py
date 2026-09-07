from __future__ import annotations

import logging
import re
from pathlib import Path

from .channel_manager import ChannelManager

LOGGER = logging.getLogger(__name__)

_MENTION_RE = re.compile(r"<@!?(\d+)>")
_MSG_LINK_RE = re.compile(
    r"https?://(?:discord\.com|discordapp\.com)/channels/(\d+)/(\d+)/(\d+)"
)
# Log file first-line headers
_USER_HEADER_RE = re.compile(r"^\[(.+?)\s+@\d+\]$")
_BOT_HEADER_RE = re.compile(
    r"^@\[\d+\]\s+\[(.+?)(?:\|([a-z0-9_-]+))?(?:\s+@\d+)?\]$",
)


def _is_control_only_bot_message(text: str) -> bool:
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    if not lines:
        return False

    def _is_control_line(line: str) -> bool:
        lower = line.lower()
        if "fast mode on" in lower:
            return True
        return "↯" in line and set(line) <= {"─", "-", "↯", "⎿", " "}

    return all(_is_control_line(line) for line in lines)


def _calc_budget(input_len: int) -> int:
    """Adaptive context budget based on user input length.

    ≤500 chars input → 3000 chars context
    ≥2000 chars input → 500 chars context
    Linear interpolation between.
    """
    if input_len <= 500:
        return 3000
    if input_len >= 2000:
        return 500
    ratio = (input_len - 500) / 1500
    return int(3000 - ratio * 2500)


def _parse_log_content(raw: str) -> tuple[str, str]:
    """Parse a log file → (author_name, content)."""
    first_line, _, rest = raw.partition("\n")
    first_line = first_line.strip()
    content = rest.strip()

    m = _BOT_HEADER_RE.match(first_line)
    if m:
        provider = m.group(2)
        author = m.group(1)
        return (f"{author}·{provider}" if provider else author), content
    m = _USER_HEADER_RE.match(first_line)
    if m:
        return m.group(1), content
    return "", raw.strip()


class ContextBuilder:
    """Builds inline context from ChannelManager's stream.

    Output format (token-efficient):
        [name] message text
        [name] (→name: snippet) reply text
        ---
        current message
    """

    def __init__(self, channel_mgr: ChannelManager, bridge_root: Path) -> None:
        self._cm = channel_mgr
        self._bridge_root = bridge_root

    def build_context(
        self,
        server_id: str,
        channel_id: int,
        user_text: str,
        self_bot_id: str | None = None,
        self_persona_id: str | None = None,
        skip_message_id: str | None = None,
        last_seen_msg_id: str | None = None,
    ) -> str:
        recent = self._cm.get_recent(server_id, channel_id)
        if not recent:
            return self._resolve_all(user_text, {}, server_id)

        budget = _calc_budget(len(user_text))

        # Build ID → name lookup from recent messages
        name_map: dict[str, str] = {}
        for msg in recent:
            if msg.author_id and msg.author_name:
                name_map[msg.author_id] = msg.author_name

        # Convert last_seen to int for comparison (Discord snowflakes)
        last_seen_int = int(last_seen_msg_id) if last_seen_msg_id else 0

        # Build entries newest-first, skip self, then reverse
        entries: list[str] = []
        total = 0
        for msg in reversed(recent):
            # Skip own messages: by persona_id (preferred) or legacy bot_id
            if self_persona_id and msg.author_name == self_persona_id:
                continue
            if self_bot_id and msg.author_id == self_bot_id:
                continue
            if skip_message_id and msg.message_id == skip_message_id:
                continue
            if msg.provider_id and _is_control_only_bot_message(msg.text):
                continue
            # Skip messages already seen by this bot in previous turns
            if last_seen_int and int(msg.message_id) <= last_seen_int:
                break

            text = self._resolve_all(msg.text, name_map, server_id)

            # Inline reply reference (content snippet instead of ID)
            if msg.reply_to:
                ref = next((m for m in recent if m.message_id == msg.reply_to), None)
                if ref:
                    snippet = ref.text[:40].replace("\n", " ")
                    author = (
                        f"{msg.author_name}\u00b7{msg.provider_id}"
                        if msg.provider_id else msg.author_name
                    )
                    ref_author = (
                        f"{ref.author_name}\u00b7{ref.provider_id}"
                        if ref.provider_id else ref.author_name
                    )
                    entry = f"[{author}] (\u2192{ref_author}: {snippet}) {text}"
                else:
                    author = (
                        f"{msg.author_name}\u00b7{msg.provider_id}"
                        if msg.provider_id else msg.author_name
                    )
                    entry = f"[{author}] {text}"
            else:
                author = (
                    f"{msg.author_name}\u00b7{msg.provider_id}"
                    if msg.provider_id else msg.author_name
                )
                entry = f"[{author}] {text}"

            if total + len(entry) > budget:
                break
            entries.append(entry)
            total += len(entry)

        if not entries:
            return self._resolve_all(user_text, name_map, server_id)

        entries.reverse()

        resolved_input = self._resolve_all(user_text, name_map, server_id)
        lines = entries + ["---", resolved_input]
        result = "\n".join(lines)
        LOGGER.debug(
            "Context: %d msgs, %d/%d chars (input=%d)",
            len(entries), total, budget, len(user_text),
        )
        return result

    # ------------------------------------------------------------------
    # Text resolution helpers
    # ------------------------------------------------------------------

    def _resolve_all(self, text: str, name_map: dict[str, str], server_id: str) -> str:
        """Resolve mentions + message links in one pass."""
        text = self._resolve_mentions(text, name_map)
        text = self._resolve_message_links(text, server_id)
        return text

    @staticmethod
    def _resolve_mentions(text: str, name_map: dict[str, str]) -> str:
        """Replace <@ID> with @name."""
        def _replace(m: re.Match) -> str:
            uid = m.group(1)
            name = name_map.get(uid)
            return f"@{name}" if name else m.group(0)
        return _MENTION_RE.sub(_replace, text)

    def _resolve_message_links(self, text: str, default_server: str) -> str:
        """Replace Discord message URLs with inline content snippets.

        https://discord.com/channels/guild/channel/msgID
        → [name: content snippet...]
        """
        def _replace(m: re.Match) -> str:
            guild_id, channel_id, msg_id = m.group(1), m.group(2), m.group(3)
            log_path = self._bridge_root / guild_id / channel_id / f"{msg_id}.txt"
            if not log_path.exists():
                return m.group(0)  # leave URL as-is
            try:
                raw = log_path.read_text(encoding="utf-8")
                author, content = _parse_log_content(raw)
                snippet = content[:80].replace("\n", " ")
                label = f"{author}: {snippet}" if author else snippet
                return f"[{label}]"
            except Exception:
                return m.group(0)
        return _MSG_LINK_RE.sub(_replace, text)
