from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from .models import (
    ChannelRuntimeState,
    PersonaChannelState,
    ServerRuntimeState,
)


class ServerStore:
    """State + bridge log layout per server.

    data_root/{server_id}/state.json                            — persisted runtime state
    data_root/{server_id}/{channel_id}/{session_id}/            — per-session scratch (CODEX_HOME, prompt files)
    bridge_root/{server_id}/{channel_id}/{message_id}.txt       — call/response log (--add-dir target)
    """

    def __init__(self, data_root: Path, bridge_root: Path, server_id: int) -> None:
        self._server_id = server_id
        self._data_dir = data_root / str(server_id)
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._bridge_dir = bridge_root / str(server_id)
        self._bridge_dir.mkdir(parents=True, exist_ok=True)

    @property
    def state_path(self) -> Path:
        return self._data_dir / "state.json"

    def session_dir(self, channel_id: int, session_id: str) -> Path:
        path = self._data_dir / str(channel_id) / session_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def channel_log_dir(self, channel_id: int) -> Path:
        path = self._bridge_dir / str(channel_id)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def channel_has_logs(self, channel_id: int) -> bool:
        path = self._bridge_dir / str(channel_id)
        if not path.exists():
            return False
        try:
            return any(path.glob("*.txt"))
        except OSError:
            return False

    def load_state(self) -> ServerRuntimeState:
        if not self.state_path.exists():
            return ServerRuntimeState()
        payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        channels: dict[int, ChannelRuntimeState] = {}
        for ch_raw, ch_data in payload.get("channels", {}).items():
            personas: dict[str, PersonaChannelState] = {}
            for pid, p_data in ch_data.get("personas", {}).items():
                personas[pid] = PersonaChannelState(
                    provider_id=p_data.get("provider_id"),
                    cli_session_id=p_data.get("cli_session_id"),
                    last_message_id=p_data.get("last_message_id"),
                )
            channels[int(ch_raw)] = ChannelRuntimeState(personas=personas)
        return ServerRuntimeState(channels=channels)

    def save_state(self, state: ServerRuntimeState) -> None:
        payload = {
            "channels": {
                str(ch_id): {
                    "personas": {
                        pid: asdict(p_state)
                        for pid, p_state in ch_state.personas.items()
                    }
                }
                for ch_id, ch_state in state.channels.items()
            }
        }
        self.state_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def save_user_message(
        self,
        *,
        channel_id: int,
        message_id: int,
        author_name: str,
        author_id: int,
        text: str,
    ) -> None:
        path = self.channel_log_dir(channel_id) / f"{message_id}.txt"
        if path.exists():
            return
        path.write_text(
            f"[{author_name} @{author_id}]\n{text}",
            encoding="utf-8",
        )

    def save_response(
        self,
        *,
        channel_id: int,
        response_message_id: int,
        trigger_message_id: int,
        persona_id: str,
        provider_id: str,
        bot_id: int,
        text: str,
    ) -> None:
        path = self.channel_log_dir(channel_id) / f"{response_message_id}.txt"
        path.write_text(
            f"@[{trigger_message_id}] [{persona_id}|{provider_id} @{bot_id}]\n{text}",
            encoding="utf-8",
        )
