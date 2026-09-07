from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

LOGGER = logging.getLogger(__name__)


class LogStore:
    """Manages per-channel and per-user turn logs.

    Storage layout:
        {bridge_root}/{server_id}/{channel_id}/{msg_id}.txt   — channel log
        {bridge_root}/{server_id}/users/{user_id}/{msg_id}.txt — user log

    Each response is saved to both locations.
    """

    def __init__(self, bridge_root: Path) -> None:
        self._root = bridge_root

    def save_user_message(
        self,
        server_id: str,
        channel_id: int,
        author_id: str,
        message_id: str,
        author_name: str,
        text: str,
    ) -> None:
        """Save user's original message to channel log + user log."""
        content = f"[{author_name} @{author_id}]\n{text}"
        filename = f"{message_id}.txt"

        # Channel log (skip if already saved by another persona)
        ch_dir = self._root / server_id / str(channel_id)
        ch_dir.mkdir(parents=True, exist_ok=True)
        ch_file = ch_dir / filename
        if not ch_file.exists():
            ch_file.write_text(content, encoding="utf-8")

        # User log
        user_dir = self._root / server_id / "users" / author_id
        user_dir.mkdir(parents=True, exist_ok=True)
        user_file = user_dir / filename
        if not user_file.exists():
            user_file.write_text(content, encoding="utf-8")

    def save_response(
        self,
        server_id: str,
        channel_id: int,
        author_id: str,
        persona_id: str,
        provider_id: str,
        bot_id: str,
        response_message_id: str,
        trigger_message_id: str,
        response_text: str,
        save_user_log: bool = True,
    ) -> None:
        """Save bot response with trigger reference to channel + user logs."""
        content = (
            f"@[{trigger_message_id}] [{persona_id}|{provider_id} @{bot_id}]\n"
            f"{response_text}"
        )
        filename = f"{response_message_id}.txt"

        # Channel log (always)
        ch_dir = self._root / server_id / str(channel_id)
        ch_dir.mkdir(parents=True, exist_ok=True)
        (ch_dir / filename).write_text(content, encoding="utf-8")

        # User log (persona mode only)
        if save_user_log:
            user_dir = self._root / server_id / "users" / author_id
            user_dir.mkdir(parents=True, exist_ok=True)
            (user_dir / filename).write_text(content, encoding="utf-8")

        LOGGER.debug(
            "Saved response log: %s → @[%s] (%d chars)",
            response_message_id, trigger_message_id, len(response_text),
        )

    def save_continuation(
        self,
        server_id: str,
        channel_id: int,
        chunk_message_id: str,
        first_message_id: str,
        chunk_text: str,
    ) -> None:
        """Save a continuation chunk file pointing back to the full response."""
        ch_dir = self._root / server_id / str(channel_id)
        ch_dir.mkdir(parents=True, exist_ok=True)
        content = f"전체: {first_message_id}.txt\n---\n{chunk_text}"
        (ch_dir / f"{chunk_message_id}.txt").write_text(content, encoding="utf-8")

    def save_turn_debug(
        self,
        server_id: str,
        channel_id: int,
        message_id: str,
        payload: dict,
    ) -> Path:
        """Save per-turn debug info, including actual input/output."""
        debug_dir = self._root / server_id / str(channel_id) / "_turns"
        debug_dir.mkdir(parents=True, exist_ok=True)
        path = debug_dir / f"{message_id}.json"
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return path

    def load_recent(
        self,
        server_id: str,
        channel_id: int,
        limit: int = 5,
    ) -> list[tuple[str, str]]:
        """Load most recent channel turn logs. Returns [(msg_id, text)]."""
        dir_path = self._root / server_id / str(channel_id)
        if not dir_path.exists():
            return []

        files = sorted(dir_path.glob("*.txt"), key=lambda p: p.stat().st_mtime)
        recent = files[-limit:] if len(files) > limit else files

        result: list[tuple[str, str]] = []
        for f in recent:
            try:
                text = f.read_text(encoding="utf-8")
                result.append((f.stem, text))
            except Exception:
                LOGGER.warning("Failed to read log file: %s", f)

        return result

    def channel_dir(self, server_id: str, channel_id: int) -> Path:
        """Return channel log directory path (for --add-dir)."""
        d = self._root / server_id / str(channel_id)
        d.mkdir(parents=True, exist_ok=True)
        return d.resolve()

    def users_dir(self, server_id: str) -> Path:
        """Return users directory path (for --add-dir)."""
        d = self._root / server_id / "users"
        d.mkdir(parents=True, exist_ok=True)
        return d.resolve()

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def cleanup_servers(self, valid_server_ids: set[str]) -> int:
        """Remove log dirs for servers the bot is no longer in. Returns count removed."""
        if not self._root.exists():
            return 0
        removed = 0
        for d in self._root.iterdir():
            # Skip internal dirs (e.g. _pages, _web_repo) and non-server dirs
            if not d.is_dir() or d.name.startswith("_") or d.name.startswith("."):
                continue
            if d.name not in valid_server_ids:
                shutil.rmtree(d, ignore_errors=True)
                LOGGER.info("Cleaned up stale server logs: %s", d.name)
                removed += 1
        return removed

    def cleanup_channels(self, server_id: str, valid_channel_ids: set[str]) -> int:
        """Remove log dirs for channels that no longer exist. Returns count removed."""
        server_dir = self._root / server_id
        if not server_dir.exists():
            return 0
        removed = 0
        for d in server_dir.iterdir():
            if d.is_dir() and d.name != "users" and d.name not in valid_channel_ids:
                shutil.rmtree(d, ignore_errors=True)
                LOGGER.info("Cleaned up stale channel logs: %s/%s", server_id, d.name)
                removed += 1
        return removed
