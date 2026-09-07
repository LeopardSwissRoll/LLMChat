from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from .models import AppSettings, LlmSettings, PersonaSettings, ServerSettings


_PROVIDER_DEFAULT_EXECUTABLES = {
    "claude": "claude",
    "codex": "codex",
}


def _default_settings_path() -> Path:
    return Path(__file__).resolve().parent / "settings.json"


def _resolve_executable(provider: str, executable: str) -> str:
    raw = executable.strip()
    if not raw:
        return raw

    path = Path(raw).expanduser()
    if path.is_file():
        return str(path.resolve())

    found = shutil.which(raw)
    if found:
        return str(Path(found).resolve())

    if os.name == "nt":
        if provider == "codex":
            roots = [
                Path.home() / ".vscode" / "extensions",
                Path.home() / ".vscode-insiders" / "extensions",
            ]
            candidates: list[Path] = []
            for root in roots:
                if root.exists():
                    candidates.extend(root.glob("openai.chatgpt-*/bin/windows-x86_64/codex.exe"))
            if candidates:
                return str(max(candidates, key=lambda p: p.stat().st_mtime).resolve())
        elif provider == "claude":
            candidate = Path.home() / ".local" / "bin" / "claude.exe"
            if candidate.exists():
                return str(candidate.resolve())

    return raw


def load_settings(path: str | Path | None = None) -> AppSettings:
    settings_path = Path(path) if path else _default_settings_path()
    if not settings_path.exists():
        raise FileNotFoundError(
            f"ExportV2 settings.json not found: {settings_path}",
        )

    payload = json.loads(settings_path.read_text(encoding="utf-8"))
    bot_token = str(payload.get("bot_token", "")).strip()
    if not bot_token:
        raise RuntimeError("settings.json is missing bot_token")

    def _resolve_root(raw: str) -> Path:
        return (
            Path(raw).resolve()
            if Path(raw).is_absolute()
            else (settings_path.parent / raw).resolve()
        )

    data_root = _resolve_root(payload.get("data_root", ".data"))
    bridge_root = _resolve_root(payload.get("bridge_root", ".bridge"))

    llm_cfg = payload.get("llm", {})
    provider = str(llm_cfg.get("provider", "claude")).strip().lower()
    if provider not in _PROVIDER_DEFAULT_EXECUTABLES:
        raise RuntimeError(
            f"Unknown llm.provider '{provider}' (expected 'claude' or 'codex')",
        )
    default_exe = _PROVIDER_DEFAULT_EXECUTABLES[provider]
    llm = LlmSettings(
        provider=provider,
        cli_executable=_resolve_executable(provider, str(llm_cfg.get("cli_executable", default_exe))),
        cli_args=tuple(llm_cfg.get("cli_args", [])),
        model=str(llm_cfg.get("model", "default")),
        effort=str(llm_cfg.get("effort", "default")),
        permission=str(llm_cfg.get("permission", "bypass")),
        fast=bool(llm_cfg.get("fast", False)),
        rows=int(llm_cfg.get("rows", 50)),
        cols=int(llm_cfg.get("cols", 200)),
        idle_seconds=float(llm_cfg.get("idle_seconds", 3.0)),
        startup_timeout=float(llm_cfg.get("startup_timeout", 60.0)),
        response_timeout=float(llm_cfg.get("response_timeout", 1800.0)),
    )

    servers_raw = payload.get("servers", {})
    if not isinstance(servers_raw, dict) or not servers_raw:
        raise RuntimeError("settings.json is missing servers configuration")

    servers: dict[int, ServerSettings] = {}
    for server_id_raw, server_payload in servers_raw.items():
        server_id = int(server_id_raw)
        personas_raw = server_payload.get("personas", {})
        personas: dict[str, PersonaSettings] = {}
        for persona_id, persona_payload in personas_raw.items():
            personas[persona_id] = PersonaSettings(
                persona_id=persona_id,
                display_name=str(persona_payload.get("display_name", persona_id)),
                prompt_dir=str(persona_payload["prompt_dir"]),
                avatar_url=(
                    str(persona_payload["avatar_url"])
                    if persona_payload.get("avatar_url")
                    else None
                ),
            )
        if not personas:
            raise RuntimeError(f"Server {server_id} has no personas configured")

        workspace_raw = str(server_payload["workspace"])
        workspace = Path(workspace_raw).expanduser().resolve()
        servers[server_id] = ServerSettings(
            server_id=server_id,
            workspace=workspace,
            personas=personas,
        )

    return AppSettings(
        bot_token=bot_token,
        data_root=data_root,
        bridge_root=bridge_root,
        llm=llm,
        servers=servers,
    )
