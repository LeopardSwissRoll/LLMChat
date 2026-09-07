from __future__ import annotations

import logging
import re

from ..models import ResponseMeta

LOGGER = logging.getLogger(__name__)

BOX_CHARS = set("╭╮╰╯│─┌┐└┘├┤┬┴┼═║╔╗╚╝╠╣╦╩╬")
ANSI_RE = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")

# Lines matching any of these patterns are TUI chrome, not response content.
CHROME_PATTERNS: list[re.Pattern] = [
    re.compile(r"\d+\s+tokens"),               # "0 tokens", "16761 tokens"
    re.compile(r"tokens:"),                     # "tokens: 1234"
    re.compile(r"cost:"),                       # cost info
    re.compile(r"⏵"),                           # TUI navigation arrows
    re.compile(r"bypass permissions"),           # permission mode indicator
    re.compile(r"shift\+tab"),                  # keyboard hint
    re.compile(r"esc to interrupt"),             # interrupt hint
    re.compile(r"current:\s*[\d.]+"),            # version info
    re.compile(r"latest:\s*[\d.]+"),             # version info
    re.compile(r"auto-accept"),                  # auto-accept mode
    re.compile(r"[●•]\s*(high|low|medium)\s"),   # effort indicator
    re.compile(r"·\s*/effort"),                  # /effort tag
    re.compile(r"\[pasted text"),               # multi-line input echo in TUI
]


def _is_status_bar(line: str) -> bool:
    """Detect TUI bottom status bar (pipe-separated path/metrics zone)."""
    s = line.strip()
    return bool(s and "|" in s and (":\\" in s or ":/" in s or "% remaining" in s))


def _is_chrome(line: str) -> bool:
    """Return True if `line` is TUI chrome (not user content)."""
    lower = line.lower()
    for pat in CHROME_PATTERNS:
        if pat.search(lower):
            return True
    return False


def extract_response_text(lines: list[str]) -> str:
    """Extract Claude's response text from raw PTY screen lines.

    Filtering stages:
      1. ANSI strip
      2. Pure border-line removal (box-drawing chars only)
      3. Prompt / user-input removal (❯ lines)
      4. TUI chrome removal (status bar, version, controls)
      5. │ panel border strip
      6. Leading ● response marker strip
    """
    # --- raw dump for debugging ---
    raw_dump = "\n".join(f"  {i:4d} | {line}" for i, line in enumerate(lines))
    LOGGER.debug("Raw screen dump (%d lines):\n%s", len(lines), raw_dump)

    cleaned: list[str] = []

    for line in lines:
        clean = ANSI_RE.sub("", line).rstrip()

        # Pure border lines
        if clean and all(ch in BOX_CHARS or ch == " " for ch in clean):
            continue

        # Prompt / user input echo
        if "❯" in clean:
            continue

        # TUI chrome
        if _is_chrome(clean):
            continue

        # Strip │ borders from panel edges
        s = clean
        while s.startswith("│") or s.startswith(" │"):
            s = s.lstrip(" ").lstrip("│")
        while s.endswith("│"):
            s = s[:-1]

        cleaned.append(s.rstrip())

    # Remove user input echo at the start (> prefix lines)
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

    # Strip leading ● (Claude response marker) from first content line
    for i, line in enumerate(result_lines):
        stripped = line.lstrip()
        if stripped.startswith("● "):
            result_lines[i] = line.replace("● ", "", 1)
            break
        if stripped:
            break

    # Trim leading/trailing blank lines
    while result_lines and not result_lines[0].strip():
        result_lines.pop(0)
    while result_lines and not result_lines[-1].strip():
        result_lines.pop()

    # Strip trailing status bar zone (path | model | usage)
    while result_lines and _is_status_bar(result_lines[-1]):
        result_lines.pop()
    while result_lines and not result_lines[-1].strip():
        result_lines.pop()

    result = "\n".join(result_lines)
    LOGGER.debug("Extracted response (%d chars)", len(result))
    return result


def extract_metadata(response_text: str) -> ResponseMeta:
    """Extract opening and closing lines from a response text."""
    lines = [line for line in response_text.split("\n") if line.strip()]
    return ResponseMeta(
        opening_line=lines[0].strip() if lines else "",
        closing_line=lines[-1].strip() if lines else "",
    )
