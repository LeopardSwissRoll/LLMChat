from __future__ import annotations

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

CLI_SESSION_RE = re.compile(r"session:\s*([a-f0-9-]+)", re.IGNORECASE)
_CONTEXT_HEADER_RE = re.compile(r"^\[최근 대화\]\s*$")
_CONTEXT_MSG_RE = re.compile(r"^\[msg:\S+\]\s")
_SEPARATOR_RE = re.compile(r"^---\s*$")
_PIPE_REF_RE = re.compile(r"^\[메시지 참조")
_BULLET_RE = re.compile(r"^● ")
_TOOL_MARKER_RE = re.compile(r"^●\s+([A-Za-z][\w ]*?)\((.+)\)\s*$")
_TOOL_RESULT_RE = re.compile(r"^\s+⎿\s+(.+)$")
_BOX_CHARS = set("╭╮╰╯│─┌┐└┘├┤┬┴┼═║╔╗╚╝╠╣╦╩╬")
_ANSI_RE = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")
_PERMISSION_KEYS = {
    "Yes": "\r",
    "Always": "\x1b[B\r",
    "No": "\x1b",
}
_CHROME_PATTERNS: list[re.Pattern] = [
    re.compile(r"\d+\s+tokens"),
    re.compile(r"tokens:"),
    re.compile(r"cost:"),
    re.compile(r"⏵"),
    re.compile(r"bypass permissions"),
    re.compile(r"shift\+tab"),
    re.compile(r"esc to interrupt"),
    re.compile(r"current:\s*[\d.]+"),
    re.compile(r"latest:\s*[\d.]+"),
    re.compile(r"auto-accept"),
    re.compile(r"[●•]\s*(high|low|medium)\s"),
    re.compile(r"·\s*/effort"),
    re.compile(r"\[pasted text"),
    # claude 2.x tool-use summary line ("Searched for 2 patterns, read 2 files,
    # listed 1 directory (ctrl+o to expand)") — no ● marker, plain text.
    re.compile(r"\(ctrl\+o to expand\)"),
    # claude 2.x thinking-time markers — opens with ✻ and varies the verb
    # ("Baked for 2m 22s", "Cogitated for 1m 3s", "Pondering...", etc.).
    # Treat any ✻-prefixed line as chrome.
    re.compile(r"^\s*✻\s"),
    # claude 2.x session feedback survey ("How is Claude doing this session?
    # 1: Bad   2: Fine   3: Good   0: Dismiss"). Shown after rate-limit or
    # error events and lingers across turns until dismissed.
    re.compile(r"how is claude doing this session"),
    re.compile(r"^\s*\d+:\s*(bad|fine|good|dismiss)\b"),
    # API/server errors emitted into the PTY stream — not response content.
    re.compile(r"api error:\s"),
    re.compile(r"rate limited\b"),
]
_PENDING_PATTERNS: list[re.Pattern] = [
    re.compile(r"\bpontificating\b", re.IGNORECASE),
    re.compile(r"\bcultivating\b", re.IGNORECASE),
    re.compile(r"\bthinking with\b", re.IGNORECASE),
    re.compile(r"\bpondering\b", re.IGNORECASE),
    re.compile(r"\bruminating\b", re.IGNORECASE),
]
_CONTROL_ACK_PATTERNS: list[re.Pattern] = [
    re.compile(r"\bfast mode on\b", re.IGNORECASE),
]
_OSC_RE = re.compile(r"\x1b\][^\x1b\x07]*(?:\x1b\\|\x07)")
_MASCOT_LINE_RE = re.compile(
    r"""
    (?:
        \.----\. |
        /\s*[°\-]\s{1,3}[°\-]\s*\\ |
        \|\s{4,}\|
    )
    """,
    re.VERBOSE,
)


def _is_status_bar(line: str) -> bool:
    s = line.strip()
    return bool(s and "|" in s and (":\\" in s or ":/" in s or "% remaining" in s))


def _is_ready_prompt(line: str) -> bool:
    normalized = line.replace("\xa0", " ").strip()
    if normalized in {"❯", ">"}:
        return True
    # claude 2.x idle prompt embeds a suggestion (e.g. ❯ Try "fix lint errors").
    # Any "❯ " / "> " prefix (with a space) at idle state is a ready prompt;
    # claude never starts response body lines with this pattern.
    if normalized.startswith(("❯ ", "> ")):
        return True
    if normalized.startswith(("❯", ">")):
        tail = normalized[1:].strip()
        if not tail:
            return True
        if set(tail) <= {"/", "\\", "°", ".", "_", "-", "`", "'", "(", ")", "|", " "}:
            return True
    return False


def _is_chrome(line: str) -> bool:
    lower = line.lower()
    return any(pat.search(lower) for pat in _CHROME_PATTERNS)


def _is_control_ack(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if any(pat.search(stripped) for pat in _CONTROL_ACK_PATTERNS):
        return True
    # Claude sometimes renders a decorative divider after the fast-mode
    # acknowledgement; treat it as non-response chrome as well.
    if "↯" in stripped and set(stripped) <= {"─", "-", "↯", " "}:
        return True
    return False


def _is_control_only_response(text: str) -> bool:
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    if not lines:
        return False
    return all(_is_control_ack(line) for line in lines)


def _is_terminal_art_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if _MASCOT_LINE_RE.search(stripped):
        return True
    if stripped.count("─") >= 20 or stripped.count("-") >= 20:
        residue = stripped.replace("─", "").replace("-", "")
        residue = residue.replace(" ", "")
        if not residue:
            return True
        if _MASCOT_LINE_RE.search(residue):
            return True
        if set(residue) <= {".", "/", "\\", "|", "°", "`", "~", "_", "(", ")"}:
            return True
    return False


def _extract_response_text(lines: list[str]) -> str:
    cleaned: list[str] = []
    for line in lines:
        clean = _ANSI_RE.sub("", line).rstrip()
        if clean and all(ch in _BOX_CHARS or ch == " " for ch in clean):
            continue
        if "❯" in clean:
            continue
        if _is_chrome(clean):
            continue
        if _is_control_ack(clean):
            continue
        if _is_terminal_art_line(clean):
            continue

        s = clean
        while s.startswith("│") or s.startswith(" │"):
            s = s.lstrip(" ").lstrip("│")
        while s.endswith("│"):
            s = s[:-1]
        cleaned.append(s.rstrip())

    content_start = 0
    for i, line in enumerate(cleaned):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("> ") or stripped == ">":
            content_start = i + 1
            continue
        break

    result_lines = cleaned[content_start:]
    for i, line in enumerate(result_lines):
        stripped = line.lstrip()
        if stripped.startswith("● "):
            result_lines[i] = line.replace("● ", "", 1)
            break
        if stripped:
            break

    while result_lines and not result_lines[0].strip():
        result_lines.pop(0)
    while result_lines and not result_lines[-1].strip():
        result_lines.pop()
    while result_lines and _is_status_bar(result_lines[-1]):
        result_lines.pop()
    while result_lines and not result_lines[-1].strip():
        result_lines.pop()
    return "\n".join(result_lines)


def _strip_tool_blocks(text: str) -> str:
    lines = text.split("\n")
    result: list[str] = []
    i = 0
    while i < len(lines):
        marker = _TOOL_MARKER_RE.match(lines[i])
        if not marker:
            result.append(lines[i])
            i += 1
            continue

        j = i + 1
        while j < len(lines):
            res = _TOOL_RESULT_RE.match(lines[j])
            if res:
                j += 1
                while j < len(lines):
                    cont = lines[j]
                    if not cont.strip():
                        j += 1
                        break
                    if not cont.startswith("  ") and not cont.startswith("\t"):
                        break
                    if _TOOL_MARKER_RE.match(cont):
                        break
                    j += 1
                break
            if _TOOL_MARKER_RE.match(lines[j]):
                break
            j += 1

        i = j
    return "\n".join(result)


def _strip_context_echo(response: str) -> str:
    lines = response.split("\n")
    pipe_start = -1
    for i, line in enumerate(lines):
        s = line.strip()
        if _PIPE_REF_RE.match(s):
            pipe_start = i
        elif _SEPARATOR_RE.match(s) and pipe_start >= 0:
            remaining = lines[i + 1:]
            return _strip_context_echo("\n".join(remaining))

    # Claude 2.x emits a *short* single-line thinking preamble marked with ●
    # before tool use, then the actual response also marked with ●. Detect
    # that pattern (short ● + blank lines + another ●) and skip the first ●.
    bullet_indices = [
        i for i, line in enumerate(lines) if _BULLET_RE.match(line.lstrip())
    ]
    if bullet_indices:
        chosen = bullet_indices[0]
        if len(bullet_indices) >= 2:
            first_idx = bullet_indices[0]
            next_idx = bullet_indices[1]
            first_text = _BULLET_RE.sub("", lines[first_idx].lstrip()).strip()
            gap = next_idx - first_idx
            in_between_blank = all(
                not l.strip() for l in lines[first_idx + 1:next_idx]
            )
            if len(first_text) <= 80 and 1 < gap <= 5 and in_between_blank:
                chosen = next_idx

        chosen_line = lines[chosen].lstrip()
        if _TOOL_MARKER_RE.match(chosen_line):
            result = "\n".join(lines[chosen:]).strip()
        else:
            lines[chosen] = _BULLET_RE.sub("", chosen_line)
            result = "\n".join(lines[chosen:]).strip()
        if result:
            return result

    last_sep = -1
    for i, line in enumerate(lines):
        stripped = line.strip()
        if _SEPARATOR_RE.match(stripped):
            last_sep = i
        elif _CONTEXT_HEADER_RE.match(stripped) or _CONTEXT_MSG_RE.match(stripped):
            continue
        elif stripped and last_sep == -1:
            break
    if last_sep >= 0:
        rest = lines[last_sep + 1:]
        while rest and not rest[0].strip():
            rest.pop(0)
        if rest:
            return "\n".join(rest).strip()

    start = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        if _CONTEXT_HEADER_RE.match(stripped) or _CONTEXT_MSG_RE.match(stripped):
            start = i + 1
            continue
        break
    if start > 0:
        rest = lines[start:]
        while rest and not rest[0].strip():
            rest.pop(0)
        return "\n".join(rest).strip()

    return response.strip()


class ClaudeAdapter(ProviderAdapter):
    provider_id = "claude"

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
            interactive_prompts=True,
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
        args = [provider_cfg.cli_executable, *provider_cfg.cli_args]
        if state and not resume_id:
            if state.model and state.model != "default":
                args.extend(["--model", state.model])
            if state.effort and state.effort != "default":
                args.extend(["--effort", self._normalize_effort(state.effort)])
            permission_map = {
                "plan": "plan",
                "bypass": "bypassPermissions",
            }
            permission_mode = permission_map.get(state.permission)
            if permission_mode:
                args.extend(["--permission-mode", permission_mode])
        args.extend(["--append-system-prompt-file", str(prompt_file)])
        for add_dir in add_dirs:
            args.extend(["--add-dir", str(add_dir)])
        if resume_id:
            args.extend(["--resume", resume_id])
        return subprocess.list2cmdline(args)

    def build_bootstrap_prompt(
        self,
        *,
        persona_prompt: str,
    ) -> str | None:
        return None

    def detect_trust_prompt(self, lines: list[str]) -> str | None:
        """Detect 'Is this a project you trust?' prompt on first workspace use."""
        for line in lines:
            if "Yes, I trust this folder" in line:
                return "\r"  # Enter to accept
        return None

    def ready_detector(self, lines: list[str]) -> bool:
        return any(_is_ready_prompt(line) for line in lines)

    def raw_ready_detector(self, raw_text: str) -> bool:
        if not raw_text:
            return False
        cleaned = _OSC_RE.sub("", raw_text)
        cleaned = _ANSI_RE.sub("", cleaned).replace("\u00a0", " ")
        return any(_is_ready_prompt(line) for line in cleaned.splitlines())

    def extract_response(self, lines: list[str]) -> str:
        response = _extract_response_text(lines)
        response = _strip_context_echo(response)
        response = _strip_tool_blocks(response)
        response_lines = [
            line for line in response.split("\n")
            if not _is_terminal_art_line(line)
        ]
        return "\n".join(response_lines).strip()

    def extract_metadata(self, text: str) -> ResponseMeta:
        lines = [line for line in text.split("\n") if line.strip()]
        return ResponseMeta(
            opening_line=lines[0].strip() if lines else "",
            closing_line=lines[-1].strip() if lines else "",
        )

    def detect_interaction(self, raw_lines: list[str]) -> InteractionPrompt | None:
        tail = " ".join(line.strip().lower() for line in raw_lines[-15:])
        if "esc to cancel" in tail:
            display = _extract_response_text(raw_lines) or "(permission prompt)"
            display = _strip_context_echo(display)
            return InteractionPrompt(
                kind="permission",
                text=display,
                choices=("Yes", "Always", "No"),
            )
        if "do you want to" in tail and ("1. yes" in tail or "3. no" in tail):
            display = _extract_response_text(raw_lines) or "(permission prompt)"
            display = _strip_context_echo(display)
            return InteractionPrompt(
                kind="permission",
                text=display,
                choices=("Yes", "Always", "No"),
            )
        return None

    def encode_interaction_reply(
        self, prompt: InteractionPrompt, choice: str,
    ) -> str:
        if prompt.kind == "permission":
            return _PERMISSION_KEYS.get(choice, "\x1b")
        return choice + "\r"

    def extract_resume_id(self, lines: list[str]) -> str | None:
        for line in lines:
            match = CLI_SESSION_RE.search(line)
            if match:
                return match.group(1)
        return None

    def has_pending_output(self, lines: list[str]) -> bool:
        tail = "\n".join(line.strip() for line in lines[-12:])
        return any(pat.search(tail) for pat in _PENDING_PATTERNS)

    def is_control_output(self, text: str) -> bool:
        return _is_control_only_response(text)

    def build_control_inputs(
        self,
        control: str,
        *,
        value: str | None = None,
        state: ProviderState | None = None,
    ) -> list[str]:
        if control == "model" and value:
            return self.build_command_inputs(f"/model {value}")
        if control == "effort" and value:
            return self.build_command_inputs(f"/effort {self._normalize_effort(value)}")
        if control == "permission" and value and state:
            order = ("default", "plan", "bypass")
            try:
                current_idx = order.index(state.permission)
                target_idx = order.index(value)
                cycles = (target_idx - current_idx) % len(order)
            except ValueError:
                cycles = 1
            return ["\x1b[Z"] * cycles
        if control == "fast":
            return self.build_command_inputs("/fast")
        if control == "compact":
            return self.build_command_inputs("/compact")
        if control == "clear":
            return self.build_command_inputs("/clear")
        if control == "usage":
            return [*self.build_command_inputs("/usage"), "\x1b"]
        return []

    def build_startup_inputs(self, state: ProviderState) -> list[str]:
        if state.fast:
            return ["/fast"]
        return []

    def internal_command_requires_ready_prompt(
        self,
        command: str,
        *,
        startup: bool = False,
    ) -> bool:
        return command in {"/fast", "/compact", "/clear"}

    def internal_command_stable_seconds(
        self,
        command: str,
        *,
        startup: bool = False,
    ) -> float:
        if command == "/fast":
            return 3.0
        if command in {"/compact", "/clear"}:
            return 3.0
        return 0.5

    def internal_command_timeout(
        self,
        command: str,
        *,
        startup: bool = False,
    ) -> float:
        if command in {"/fast", "/compact", "/clear"}:
            return 45.0
        return 30.0

    @staticmethod
    def _normalize_effort(value: str) -> str:
        if value == "xhigh":
            return "high"
        return value

    def usage_query_inputs(self) -> tuple[list[str], list[str]]:
        return (self.build_command_inputs("/usage"), ["\x1b"])

    def parse_usage(self, lines: list[str]) -> dict[str, str]:
        result: dict[str, str] = {}
        section: str | None = None
        for line in lines:
            low = line.strip().lower()
            if "current session" in low:
                section = "session"
            elif "current week" in low and "all models" in low:
                section = "week"
            elif "current week" in low:
                section = None
            m = re.search(r"(\d+)%\s*used", line)
            if m and section:
                result[section] = m.group(1)
                section = None
        return result

    def build_message_inputs(self, text: str) -> list[str]:
        # Claude's TUI is more reliable when multiline payloads are pasted first
        # and submit is sent as a separate Enter keypress.
        return [text, "\r"]

    def build_command_inputs(self, command: str) -> list[str]:
        return [command, "\r"]
