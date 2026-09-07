from __future__ import annotations

import json
import os
import shutil
import urllib.request
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from .models import (
    BridgeSettings,
    PersonaConfig,
    ProviderCapabilities,
    ProviderConfig,
    ProviderState,
)


def _find_dotenv() -> None:
    """Search cwd/script dirs and their ancestors for .env."""
    search_roots = [
        Path.cwd(),
        Path(__file__).resolve().parent,
        Path(__file__).resolve().parent.parent,
        Path(__file__).resolve().parent.parent.parent,
    ]
    visited: set[Path] = set()
    for root in search_roots:
        p = root.resolve()
        while p not in visited:
            visited.add(p)
            candidate = p / ".env"
            if candidate.is_file():
                load_dotenv(candidate)
                return
            parent = p.parent
            if parent == p:
                break
            p = parent
    load_dotenv()


_find_dotenv()


def _load_default_state_file() -> dict[str, Any]:
    path = Path(__file__).resolve().parent.parent / "default_state.json"
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


_DEFAULT_STATE = _load_default_state_file()


def _lookup(name: str) -> Any | None:
    raw = os.getenv(name)
    if raw is not None and raw.strip():
        return raw.strip()
    return _DEFAULT_STATE.get(name)


def _cfg_str(name: str, fallback: str = "") -> str:
    value = _lookup(name)
    if value is None:
        return fallback
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(value, ensure_ascii=False)


def _cfg_bool(name: str, fallback: bool) -> bool:
    value = _lookup(name)
    if value is None:
        return fallback
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _cfg_int(name: str, fallback: int) -> int:
    value = _lookup(name)
    if value is None:
        return fallback
    return int(value)


def _cfg_float(name: str, fallback: float) -> float:
    value = _lookup(name)
    if value is None:
        return fallback
    return float(value)


def _cfg_list(name: str, fallback: list[str]) -> list[str]:
    value = _lookup(name)
    if value is None:
        return list(fallback)
    if isinstance(value, list):
        items = value
    else:
        raw = str(value).strip()
        if raw.startswith("["):
            try:
                parsed = json.loads(raw)
                items = parsed if isinstance(parsed, list) else [raw]
            except Exception:
                items = raw.split(",")
        else:
            items = raw.split(",")
    return [str(item).strip() for item in items if str(item).strip()]


def _cfg_json_list(name: str, fallback: list[str]) -> list[str]:
    value = _lookup(name)
    if value is None:
        return list(fallback)
    if isinstance(value, list):
        return [str(item) for item in value]
    parsed = json.loads(str(value))
    if not isinstance(parsed, list):
        raise ValueError(f"{name} must be a JSON list")
    return [str(item) for item in parsed]


def _fetch_bot_info(token: str) -> dict:
    """GET /users/@me → {username, global_name, id, ...}"""
    req = urllib.request.Request(
        "https://discord.com/api/v10/users/@me",
        headers={
            "Authorization": f"Bot {token}",
            "User-Agent": "DiscordBot (PTYBridge, 1.0)",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as exc:
        raise RuntimeError(f"Discord API failed: {exc}") from exc


def _persona_from_definition(
    persona_id: str,
    definition: dict,
) -> PersonaConfig:
    """Build PersonaConfig from a persona definition dict.

    Supports both verbose and shorthand keys:

    Verbose:
        {"prompt_dir": "aris", "display_name": "아리스",
         "role_aliases": ["아리스","Aris"], "avatar_url": "..."}

    Shorthand:
        {"dir": "aris", "aka": "Aris", "avatar_url": "..."}
        - key = persona_id = display_name (unless display_name given)
        - "aka": extra aliases (str or list); persona_id always included
        - "dir": shorthand for prompt_dir
    """
    prompt_dir = definition.get("dir") or definition.get("prompt_dir", persona_id)
    display_name = definition.get("display_name", persona_id)

    # Build role_aliases: explicit > aka shorthand > [display_name]
    if "role_aliases" in definition:
        role_aliases = tuple(definition["role_aliases"])
    else:
        aka = definition.get("aka", [])
        if isinstance(aka, str):
            aka = [aka] if aka else []
        # Always include persona_id (= display_name by default)
        seen: set[str] = set()
        aliases: list[str] = []
        for a in [display_name] + aka:
            if a and a not in seen:
                aliases.append(a)
                seen.add(a)
        role_aliases = tuple(aliases)

    avatar_url = definition.get("avatar_url")
    identity_text = definition.get(
        "identity_text",
        _cfg_str("DEFAULT_IDENTITY_TEXT", f"PTY Bridge ({display_name})"),
    )

    print(f"  [config] persona: {persona_id} → display={display_name}, prompt_dir={prompt_dir}")

    return PersonaConfig(
        persona_id=persona_id,
        display_name=display_name,
        role_aliases=role_aliases,
        avatar_url=avatar_url,
        identity_text=identity_text,
        prompt_dir=Path(prompt_dir),
        enabled_providers=tuple(
            _cfg_list("ENABLED_PROVIDERS", ["claude", "codex"])
        ),
        pty_rows=_cfg_int("PTY_ROWS", 50),
        pty_cols=_cfg_int("PTY_COLS", 200),
        message_edit_interval=_cfg_float("MESSAGE_EDIT_INTERVAL", 2.0),
        response_timeout=_cfg_float("RESPONSE_TIMEOUT", 1800.0),
        idle_seconds=_cfg_float("IDLE_SECONDS", 3.0),
    )


def _persona_from_token_legacy(token: str, md_file: str) -> PersonaConfig:
    """Legacy: auto-discover bot info from Discord API (backward compat)."""
    info = _fetch_bot_info(token)
    username = info["username"]
    display = info.get("global_name") or username
    pid = username.lower()

    print(f"  [config] (legacy) {username} → persona_id={pid}, md={md_file}")

    return PersonaConfig(
        persona_id=pid,
        display_name=display,
        role_aliases=(display,) if display == username else (display, username),
        identity_text=_cfg_str("DEFAULT_IDENTITY_TEXT", f"PTY Bridge ({display})"),
        prompt_dir=Path(md_file),
        enabled_providers=tuple(
            _cfg_list("ENABLED_PROVIDERS", ["claude", "codex"])
        ),
        pty_rows=_cfg_int("PTY_ROWS", 50),
        pty_cols=_cfg_int("PTY_COLS", 200),
        message_edit_interval=_cfg_float("MESSAGE_EDIT_INTERVAL", 2.0),
        response_timeout=_cfg_float("RESPONSE_TIMEOUT", 1800.0),
        idle_seconds=_cfg_float("IDLE_SECONDS", 3.0),
    )


def _provider_from_env(provider_id: str) -> ProviderConfig:
    upper = provider_id.upper()

    if provider_id == "claude":
        caps = ProviderCapabilities(
            model_switch=True,
            effort=True,
            permission=True,
            fast=True,
            compact=True,
            clear=True,
            interrupt=True,
            usage=True,
            interactive_prompts=True,
        )
        default_args = '["--verbose"]'
        default_exec = "claude"
        default_model = "opus"
        default_effort = "high"
        default_permission = "bypass"
        default_fast = "false"
    elif provider_id == "codex":
        caps = ProviderCapabilities(
            model_switch=True,
            effort=True,
            permission=True,
            fast=True,
            compact=True,
            clear=True,
            interrupt=True,
            usage=True,
        )
        default_args = (
            '["--dangerously-bypass-approvals-and-sandbox","--no-alt-screen"]'
        )
        default_exec = "codex"
        default_model = "gpt-5.4"
        default_effort = "xhigh"
        default_permission = "bypass"
        default_fast = "false"
    else:
        caps = ProviderCapabilities()
        default_args = "[]"
        default_exec = provider_id
        default_model = "default"
        default_effort = "high"
        default_permission = "bypass"
        default_fast = "false"

    enabled = _cfg_bool(f"{upper}_ENABLED", True)
    cli_args = tuple(_cfg_json_list(f"{upper}_CLI_ARGS", json.loads(default_args)))
    default_state = ProviderState(
        mode=_parse_mode(
            _cfg_str(
                f"{upper}_DEFAULT_MODE",
                _cfg_str("DEFAULT_MODE", "3"),
            )
        ),
        model=_cfg_str(f"{upper}_DEFAULT_MODEL", default_model),
        effort=_cfg_str(f"{upper}_DEFAULT_EFFORT", default_effort),
        permission=_cfg_str(f"{upper}_DEFAULT_PERMISSION", default_permission),
        fast=_cfg_bool(
            f"{upper}_DEFAULT_FAST",
            default_fast.lower() in {"1", "true", "yes", "on"},
        ),
    )
    configured_exec = _cfg_str(f"{upper}_CLI_EXECUTABLE", default_exec)
    return ProviderConfig(
        provider_id=provider_id,
        cli_executable=_resolve_cli_executable(provider_id, configured_exec),
        cli_args=cli_args,
        enabled=enabled,
        default_capabilities=caps,
        default_state=default_state,
    )


def _resolve_cli_executable(provider_id: str, executable: str) -> str:
    raw = executable.strip()
    if not raw:
        return raw

    candidate = Path(raw).expanduser()
    if candidate.is_file():
        return str(candidate.resolve())

    found = shutil.which(raw)
    if found:
        return str(Path(found).resolve())

    if os.name == "nt" and provider_id == "codex":
        fallback = _resolve_windows_codex_executable()
        if fallback:
            return fallback

    return raw


def _resolve_windows_codex_executable() -> str | None:
    roots = [
        Path.home() / ".vscode" / "extensions",
        Path.home() / ".vscode-insiders" / "extensions",
    ]
    candidates: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        candidates.extend(
            root.glob("openai.chatgpt-*/bin/windows-x86_64/codex.exe"),
        )
    if not candidates:
        return None
    best = max(candidates, key=lambda path: path.stat().st_mtime)
    return str(best.resolve())


def _parse_mode(raw: str) -> int:
    value = raw.strip().lower()
    if value.isdigit():
        return max(1, min(5, int(value)))
    aliases = {
        "core": 1,
        "tend": 2,
        "soft": 2,
        "medium": 3,
        "hard": 4,
        "persona": 5,
        "masquerade": 5,
    }
    return aliases.get(value, 3)


def load_bridge_settings() -> BridgeSettings:
    """Load bridge settings.

    Supports two configuration formats:

    **New format (preferred)**:
        DISCORD_BOT_TOKEN = single bot token
        PERSONAS = JSON dict {persona_id: {prompt_dir, avatar_url, role_aliases, ...}}

    **Legacy format (backward compat)**:
        BOT_TOKENS = JSON dict {token: md_file_path}
        → First token becomes bot_token, all personas auto-discovered from Discord API.
    """
    # --- Resolve bot token ---
    bot_token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
    personas: dict[str, PersonaConfig] = {}
    active_ids: list[str] = []

    if bot_token:
        # New format: single token + PERSONAS definition
        bot_info = _fetch_bot_info(bot_token)
        bot_id = str(bot_info["id"])
        print(f"  [config] Bot: {bot_info['username']} (id={bot_id})")

        personas_raw = os.getenv("PERSONAS", "").strip()
        if not personas_raw:
            personas_raw = _cfg_str("PERSONAS", "")
        if not personas_raw:
            raise RuntimeError(
                "DISCORD_BOT_TOKEN is set but PERSONAS is missing. "
                "Define personas as JSON: {\"name\": {\"prompt_dir\": \"...\", ...}}"
            )
        persona_defs: dict[str, dict] = json.loads(personas_raw)
        for pid, definition in persona_defs.items():
            persona = _persona_from_definition(pid, definition)
            personas[pid] = persona
            active_ids.append(pid)
    else:
        # Legacy format: BOT_TOKENS
        tokens_raw = os.getenv("BOT_TOKENS", "").strip()
        if not tokens_raw:
            raise RuntimeError(
                "Missing DISCORD_BOT_TOKEN or BOT_TOKENS in .env"
            )
        token_map: dict[str, str] = json.loads(tokens_raw)
        if not token_map:
            raise RuntimeError("BOT_TOKENS is empty")

        # First token becomes the single bot token
        first_token = True
        bot_id = ""
        for token, md_file in token_map.items():
            if first_token:
                bot_token = token
                info = _fetch_bot_info(token)
                bot_id = str(info["id"])
                print(f"  [config] (legacy) Bot: {info['username']} (id={bot_id})")
                first_token = False

            persona = _persona_from_token_legacy(token, md_file)
            if persona.persona_id in personas:
                print(f"  [config] Warning: duplicate persona '{persona.persona_id}', skipping")
                continue
            personas[persona.persona_id] = persona
            active_ids.append(persona.persona_id)

    if not personas:
        raise RuntimeError("No personas configured")

    providers = {
        cfg.provider_id: cfg
        for cfg in (
            _provider_from_env("claude"),
            _provider_from_env("codex"),
        )
        if cfg.enabled
    }
    if not providers:
        raise RuntimeError("No enabled providers configured")

    default_provider = _cfg_str("DEFAULT_PROVIDER", "claude") or "claude"
    if default_provider not in providers:
        default_provider = next(iter(providers))

    return BridgeSettings(
        default_workspace=Path(
            os.getenv("DEFAULT_WORKSPACE", "Playground")
        ).resolve(),
        bot_token=bot_token,
        bot_id=bot_id,
        active_personas=active_ids,
        personas=personas,
        providers=providers,
        default_provider=default_provider,
        log_level=_cfg_str("LOG_LEVEL", "INFO"),
        default_mode=_parse_mode(_cfg_str("DEFAULT_MODE", "3")),
        command_access=_cfg_str("DEFAULT_COMMAND_ACCESS", "public"),
        lv1_add_dirs=tuple(
            _cfg_list("DEFAULT_LV1_ADD_DIRS", ["channel", "persona"])
        ),
        default_add_dirs=tuple(
            _cfg_list("DEFAULT_ADD_DIRS", ["channel", "users", "persona"])
        ),
    )
