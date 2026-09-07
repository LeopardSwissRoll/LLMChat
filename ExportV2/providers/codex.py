from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

from ..models import (
    InteractionPrompt,
    PersonaConfig,
    ProviderCapabilities,
    ProviderConfig,
    ProviderState,
    ResponseMeta,
)
from .base import ProviderAdapter

_ANSI_RE = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")
_BOX_CHARS = set("╭╮╰╯│─┌┐└┘├┤┬┴┼═║╔╗╚╝╠╣╦╩╬")
_STATUS_RE = re.compile(r"\d+%\s+left")
_MANAGED_KEY_LINE_RE = re.compile(
    r'^\s*(approval_policy|developer_instructions|model|model_reasoning_effort|'
    r'plan_mode_reasoning_effort|sandbox_mode|service_tier)\s*=',
)
_MANAGED_CONFIG_KEYS = {
    "approval_policy",
    "model",
    "model_reasoning_effort",
    "plan_mode_reasoning_effort",
    "sandbox_mode",
    "service_tier",
}
_PERMISSION_PRESETS = {
    "default": ("on-request", "workspace-write"),
    "plan": ("on-request", "read-only"),
    "bypass": ("never", "danger-full-access"),
}
_CONTROL_ACK_PATTERNS = (
    re.compile(r"^context compacted$", re.IGNORECASE),
    re.compile(r"^cleared conversation history$", re.IGNORECASE),
)
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\].*?\x1b\\", re.DOTALL)


def _is_box(line: str) -> bool:
    return bool(line and all(ch in _BOX_CHARS or ch == " " for ch in line))


def _clean_lines(lines: list[str]) -> list[str]:
    cleaned: list[str] = []
    for line in lines:
        s = _ANSI_RE.sub("", line).rstrip()
        if not s.strip():
            cleaned.append("")
            continue
        if _is_box(s.strip()):
            continue
        cleaned.append(s)
    return cleaned


class CodexAdapter(ProviderAdapter):
    provider_id = "codex"
    _BRIDGE_MARKER = "# --- bridge generated developer instructions ---"

    def __init__(self) -> None:
        self._codex_home = Path.home() / ".codex"

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            model_switch=True,
            effort=True,
            permission=True,
            fast=True,
            compact=True,
            clear=True,
            interrupt=True,
            usage=True,
        )

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
        self._prepare_runtime_home(
            runtime_dir=runtime_dir,
            prompt_text=prompt_file.read_text(encoding="utf-8"),
            workspace=workspace,
            state=state,
        )
        args = [provider_cfg.cli_executable, *self._sanitize_cli_args(provider_cfg.cli_args)]
        for add_dir in add_dirs:
            args.extend(["--add-dir", str(add_dir)])
        if resume_id:
            args.extend(["resume", resume_id])
        return subprocess.list2cmdline(args)

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
        home = runtime_dir or self._codex_home
        return {"CODEX_HOME": str(home)}

    def _prepare_runtime_home(
        self,
        *,
        runtime_dir: Path | None,
        prompt_text: str,
        workspace: str,
        state: ProviderState | None = None,
    ) -> None:
        if runtime_dir is None:
            return

        runtime_dir.mkdir(parents=True, exist_ok=True)
        config_path = runtime_dir / "config.toml"
        base_config_path = self._codex_home / "config.toml"

        base_text = ""
        if base_config_path.exists():
            try:
                base_text = base_config_path.read_text(encoding="utf-8")
            except Exception:
                base_text = ""

        if self._BRIDGE_MARKER in base_text:
            base_text = base_text.split(self._BRIDGE_MARKER, 1)[0].rstrip()
        if base_text:
            base_text = "\n".join(
                line
                for line in base_text.splitlines()
                if not _MANAGED_KEY_LINE_RE.match(line)
            ).strip()

        generated_lines = [
            self._BRIDGE_MARKER,
            f"developer_instructions = {json.dumps(prompt_text, ensure_ascii=False)}",
        ]
        if state:
            if state.model and state.model != "default":
                generated_lines.append(
                    f"model = {json.dumps(state.model, ensure_ascii=False)}",
                )
            if state.effort and state.effort != "default":
                generated_lines.append(
                    f"model_reasoning_effort = {json.dumps(state.effort, ensure_ascii=False)}",
                )
                generated_lines.append(
                    f"plan_mode_reasoning_effort = {json.dumps(state.effort, ensure_ascii=False)}",
                )
            if state.fast:
                generated_lines.append(
                    f"service_tier = {json.dumps('fast')}",
                )
            approval_policy, sandbox_mode = self._permission_preset(state.permission)
            generated_lines.append(
                f"approval_policy = {json.dumps(approval_policy)}",
            )
            generated_lines.append(
                f"sandbox_mode = {json.dumps(sandbox_mode)}",
            )
        for trusted_path in self._workspace_trust_variants(workspace):
            if self._has_project_entry(base_text, trusted_path):
                continue
            generated_lines.extend(
                [
                    "",
                    f"[projects.{json.dumps(trusted_path, ensure_ascii=False)}]",
                    'trust_level = "trusted"',
                ]
            )
        generated = "\n".join(generated_lines) + "\n"
        if base_text.strip():
            content = generated + "\n" + base_text.lstrip()
        else:
            content = generated
        config_path.write_text(content, encoding="utf-8")

    @staticmethod
    def _workspace_trust_variants(workspace: str) -> list[str]:
        if not workspace:
            return []

        seen: set[str] = set()
        variants: list[str] = []

        def add(value: str) -> None:
            if not value:
                return
            normalized = value.replace("/", "\\")
            if normalized in seen:
                return
            seen.add(normalized)
            variants.append(normalized)

        try:
            resolved = str(Path(workspace).expanduser().resolve())
        except Exception:
            resolved = workspace

        add(workspace)
        add(resolved)
        add(os.path.normpath(resolved))
        add(os.path.normcase(resolved))

        canonical = resolved.replace("/", "\\")
        drive, tail = os.path.splitdrive(canonical)
        if drive:
            add(drive.upper() + tail)
            add(drive.lower() + tail)

        return variants

    @staticmethod
    def _has_project_entry(config_text: str, path: str) -> bool:
        headers = (
            f"[projects.'{path}']",
            f"[projects.{json.dumps(path, ensure_ascii=False)}]",
        )
        return any(header in config_text for header in headers)

    @staticmethod
    def _permission_preset(value: str | None) -> tuple[str, str]:
        return _PERMISSION_PRESETS.get(value or "default", _PERMISSION_PRESETS["default"])

    @staticmethod
    def _sanitize_cli_args(args: tuple[str, ...]) -> list[str]:
        cleaned: list[str] = []
        index = 0
        while index < len(args):
            arg = args[index]
            if arg == "--dangerously-bypass-approvals-and-sandbox":
                index += 1
                continue
            if arg in {"--model", "-m", "--sandbox", "-s", "--ask-for-approval", "-a"}:
                index += 2
                continue
            if arg in {"-c", "--config"}:
                if index + 1 >= len(args):
                    index += 1
                    continue
                config_arg = args[index + 1]
                key = config_arg.split("=", 1)[0].strip()
                if key in _MANAGED_CONFIG_KEYS:
                    index += 2
                    continue
                cleaned.extend([arg, config_arg])
                index += 2
                continue
            cleaned.append(arg)
            index += 1
        return cleaned

    def build_bootstrap_prompt(
        self,
        *,
        persona_prompt: str,
    ) -> str | None:
        return None

    def detect_trust_prompt(self, lines: list[str]) -> str | None:
        """Detect 'Do you trust the contents of this directory?' prompt."""
        for line in lines:
            if "Yes, continue" in line:
                return "\r"
        return None

    def detect_startup_prompt(self, raw_text: str) -> str | None:
        """Detect startup prompts that pyte may fail to render on Windows."""
        if not raw_text:
            return None
        cleaned = _ANSI_ESCAPE_RE.sub("", raw_text).replace("\u00a0", " ")
        compact = " ".join(cleaned.split())
        if (
            "Do you trust the contents of this directory?" in compact
            and "Yes, continue" in compact
        ):
            # Choose the highlighted safe path and continue.
            return "1\r"
        return None

    def ready_detector(self, lines: list[str]) -> bool:
        joined = "\n".join(lines)
        return (
            "OpenAI Codex" in joined
            and any("›" in line for line in lines)
        )

    def command_ready_detector(self, lines: list[str]) -> bool:
        for line in _clean_lines(lines):
            if line.strip().startswith("›"):
                return True
        return False

    def extract_response(self, lines: list[str]) -> str:
        cleaned = _clean_lines(lines)

        start = -1
        for i, line in enumerate(cleaned):
            stripped = line.strip()
            if stripped.startswith("• "):
                start = i
                break
        if start >= 0:
            result: list[str] = []
            first = True
            for line in cleaned[start:]:
                stripped = line.strip()
                if not stripped:
                    if result:
                        result.append("")
                    continue
                if stripped.startswith("› "):
                    break
                if stripped.startswith("Tip:"):
                    continue
                if _STATUS_RE.search(stripped):
                    continue
                if first and stripped.startswith("• "):
                    result.append(stripped[2:].strip())
                    first = False
                else:
                    result.append(stripped)
                    first = False
            return "\n".join(result).strip()

        fallback: list[str] = []
        for line in cleaned:
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("│ >_ OpenAI Codex"):
                continue
            if stripped.startswith(">_ OpenAI Codex"):
                continue
            if stripped.startswith("│ model:"):
                continue
            if stripped.startswith("│ directory:"):
                continue
            if stripped.startswith("model:"):
                continue
            if stripped.startswith("directory:"):
                continue
            if stripped.startswith("Tip:"):
                continue
            if stripped.startswith("› "):
                continue
            if stripped.startswith("Press enter to continue"):
                continue
            if _STATUS_RE.search(stripped):
                continue
            fallback.append(stripped)
        return "\n".join(fallback).strip()

    def extract_metadata(self, text: str) -> ResponseMeta:
        lines = [line for line in text.split("\n") if line.strip()]
        return ResponseMeta(
            opening_line=lines[0].strip() if lines else "",
            closing_line=lines[-1].strip() if lines else "",
        )

    def detect_interaction(self, raw_lines: list[str]) -> InteractionPrompt | None:
        return None

    def encode_interaction_reply(
        self, prompt: InteractionPrompt, choice: str,
    ) -> str:
        return choice + "\r"

    def build_message_inputs(self, text: str) -> list[str]:
        return [text, "\r"]

    def extract_resume_id(self, lines: list[str]) -> str | None:
        for line in lines:
            match = re.search(
                r"\b([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b",
                line,
                re.IGNORECASE,
            )
            if match:
                return match.group(1)
        return None

    def control_requires_restart(self, control: str) -> bool:
        return control in {"model", "effort", "permission", "fast"}

    def build_control_inputs(
        self,
        control: str,
        *,
        value: str | None = None,
        state: ProviderState | None = None,
    ) -> list[str]:
        if control == "compact":
            return self.build_command_inputs("/compact")
        if control == "clear":
            return self.build_command_inputs("/clear")
        return []

    def build_command_inputs(self, command: str) -> list[str]:
        return [command, "\r"]

    def usage_query_inputs(self) -> tuple[list[str], list[str]]:
        return ([], [])

    def parse_usage(self, lines: list[str]) -> dict[str, str]:
        result: dict[str, str] = {}
        for line in reversed(_clean_lines(lines)):
            stripped = line.strip()
            if not stripped:
                continue
            match = re.search(r"(?:^|·)\s*(\d+)%\s+left\b", stripped)
            if match:
                result["session"] = match.group(1)
                break
        return result

    def is_control_output(self, text: str) -> bool:
        lines = [line.strip() for line in text.split("\n") if line.strip()]
        if not lines:
            return False
        return all(any(pat.match(line) for pat in _CONTROL_ACK_PATTERNS) for line in lines)

    def internal_command_requires_ready_prompt(
        self,
        command: str,
        *,
        startup: bool = False,
    ) -> bool:
        return command in {"/compact", "/clear"}

    def internal_command_stable_seconds(
        self,
        command: str,
        *,
        startup: bool = False,
    ) -> float:
        if command in {"/compact", "/clear"}:
            return 4.5
        return 0.5

    def internal_command_timeout(
        self,
        command: str,
        *,
        startup: bool = False,
    ) -> float:
        if command in {"/compact", "/clear"}:
            return 120.0
        return 30.0

    def resolve_resume_id(
        self,
        *,
        lines: list[str],
        workspace: str,
        bootstrap_token: str | None = None,
        runtime_dir: Path | None = None,
    ) -> str | None:
        screen_id = self.extract_resume_id(lines)
        if screen_id:
            return screen_id

        home = runtime_dir or self._codex_home
        index_path = home / "session_index.jsonl"
        sessions_root = home / "sessions"
        if index_path.exists():
            try:
                entries = index_path.read_text(encoding="utf-8").splitlines()
            except Exception:
                entries = []

            for raw in reversed(entries[-100:]):
                try:
                    item = json.loads(raw)
                except Exception:
                    continue
                session_id = item.get("id")
                if not session_id:
                    continue
                if sessions_root.exists():
                    matches = list(sessions_root.rglob(f"*{session_id}.jsonl"))
                    if matches:
                        return session_id

        if sessions_root.exists():
            candidates = sorted(
                sessions_root.rglob("*.jsonl"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            for path in candidates[:20]:
                file_id = self.extract_resume_id([path.name])
                if file_id:
                    return file_id
                try:
                    with path.open("r", encoding="utf-8") as fh:
                        for _ in range(5):
                            line = fh.readline()
                            if not line:
                                break
                            line_id = self.extract_resume_id([line])
                            if line_id:
                                return line_id
                except Exception:
                    continue
        return None
