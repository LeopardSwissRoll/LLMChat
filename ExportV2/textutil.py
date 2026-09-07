"""Pure text helpers for post-processing extracted CLI responses."""

from __future__ import annotations

import re

# Claude tool block:  ● ToolName(args) ... ⎿ result
_TOOL_MARKER_RE = re.compile(r"^●\s+([A-Za-z][\w ]*?)\((.+)\)\s*$")
_TOOL_RESULT_RE = re.compile(r"^\s+⎿\s+(.+)$")
# Codex tool block:   • Running/Ran command ... └ result
_CODEX_RAN_RE = re.compile(r"^(?:•\s+)?Ran\s+(.+)$")
_CODEX_RUNNING_RE = re.compile(r"^(?:•\s+)?Running\s+(.+)$")
_CODEX_RESULT_RE = re.compile(r"^└\s+(.+)$")


def collapse_tool_blocks(text: str) -> str:
    """Collapse tool use blocks into single summary lines.

    Claude format:
        ● Write(C:\\path\\file.html)
          ...content...
          ⎿  Wrote 1310 lines to C:\\path\\file.html
        → 🔧 Write(file.html) → Wrote 1310 lines to C:\\path\\file.html

    Codex format:
        • Running git status --short --branch
        • Ran git status --short --branch
        └ fatal: not a git repository
        → 🔧 git status --short --branch → fatal: not a git repository
    """
    lines = text.split("\n")
    result: list[str] = []
    i = 0
    while i < len(lines):
        # --- Claude: ● ToolName(args) ---
        marker = _TOOL_MARKER_RE.match(lines[i])
        if marker:
            tool_name = marker.group(1)
            tool_args = marker.group(2)
            short_args = tool_args
            if tool_name in ("Write", "Read", "Edit", "Glob", "Grep"):
                for sep in ("/", "\\"):
                    if sep in short_args:
                        short_args = short_args.rsplit(sep, 1)[-1]
                short_args = short_args.rstrip(')"\'')

            tool_result = ""
            j = i + 1
            while j < len(lines):
                res = _TOOL_RESULT_RE.match(lines[j])
                if res:
                    tool_result = res.group(1).strip()
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
                if _TOOL_MARKER_RE.match(lines[j]) or _CODEX_RAN_RE.match(lines[j]):
                    break
                j += 1

            if tool_result:
                result.append(f"\U0001f527 {tool_name}({short_args}) \u2192 {tool_result}")
            else:
                result.append(f"\U0001f527 {tool_name}({short_args})")
            i = j
            continue

        # --- Codex: Running ... (skip, wait for Ran) ---
        if _CODEX_RUNNING_RE.match(lines[i]):
            i += 1
            continue

        # --- Codex: Ran command ... └ result ---
        ran = _CODEX_RAN_RE.match(lines[i])
        if ran:
            cmd = ran.group(1).strip()
            if len(cmd) > 60:
                cmd = cmd[:57] + "..."
            tool_result = ""
            j = i + 1
            while j < len(lines):
                res = _CODEX_RESULT_RE.match(lines[j])
                if res:
                    tool_result = res.group(1).strip()
                    j += 1
                    while j < len(lines):
                        cont = lines[j]
                        if not cont.strip():
                            j += 1
                            break
                        if not cont.startswith("  ") and not cont.startswith("\t"):
                            break
                        if _CODEX_RAN_RE.match(cont) or _TOOL_MARKER_RE.match(cont):
                            break
                        j += 1
                    break
                if _CODEX_RAN_RE.match(lines[j]) or _TOOL_MARKER_RE.match(lines[j]):
                    break
                j += 1

            if tool_result:
                result.append(f"\U0001f527 {cmd} \u2192 {tool_result}")
            else:
                result.append(f"\U0001f527 {cmd}")
            i = j
            continue

        result.append(lines[i])
        i += 1

    return "\n".join(result)


def compress_blank_lines(text: str) -> str:
    """Collapse 3+ consecutive newlines into a single blank line."""
    return re.sub(r"\n{3,}", "\n\n", text)
