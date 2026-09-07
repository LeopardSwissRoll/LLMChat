from __future__ import annotations

import re


ANSI_ESCAPE_RE = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")

# Lines starting with these are tool/meta output → render as -# subtext
_TOOL_META_RE = re.compile(
    r"^\s*("
    r"[🔧⚙🛠✻●◆▶►]"       # tool emoji prefixes
    r"|Took \d"              # "Took 3.2s" timing lines
    r"|Done \("              # "Done (45 tool uses · ...)"
    r"|\d+ tool use"         # "3 tool uses"
    r")"
)

# Markdown headers at line start (not inside code blocks)
_HEADER_RE = re.compile(r"^(#{1,3})\s+(.+)$")

# Horizontal rules
_HR_RE = re.compile(r"^-{3,}\s*$")


def strip_ansi_sequences(text: str) -> str:
    return ANSI_ESCAPE_RE.sub("", text)


def normalize_markdown_for_discord(text: str) -> str:
    normalized = strip_ansi_sequences(text).replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    output: list[str] = []
    in_fence = False
    index = 0
    while index < len(lines):
        line = lines[index]

        # Track code fence state — don't transform inside code blocks
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            output.append(line)
            index += 1
            continue

        if in_fence:
            output.append(line)
            index += 1
            continue

        # Table detection (unchanged)
        next_line = lines[index + 1] if index + 1 < len(lines) else ""
        if _looks_like_table_header(line, next_line):
            table_lines = [line, next_line]
            index += 2
            while index < len(lines) and "|" in lines[index]:
                table_lines.append(lines[index])
                index += 1
            output.append("```text")
            output.extend(table_lines)
            output.append("```")
            continue

        # Headers → bold (Discord headers are too large)
        header_match = _HEADER_RE.match(line)
        if header_match:
            output.append(f"**{header_match.group(2)}**")
            index += 1
            continue

        # Horizontal rules → subtext separator
        if _HR_RE.match(stripped):
            output.append("-# ───")
            index += 1
            continue

        # Tool/meta lines → subtext
        if _TOOL_META_RE.match(stripped):
            output.append(f"-# {stripped}")
            index += 1
            continue

        output.append(line)
        index += 1
    return "\n".join(output).strip()


def split_text_for_discord(text: str, limit: int = 1900) -> list[str]:
    if limit < 50:
        raise ValueError("limit must be at least 50 characters")

    normalized = normalize_markdown_for_discord(text)
    if not normalized:
        return ["(empty response)"]

    lines = normalized.splitlines(keepends=True)
    chunks: list[str] = []
    current = ""
    fence_stack: list[str] = []

    for line in lines:
        for piece in _split_line_to_fit(line, limit):
            projected = current + piece
            closing_cost = len(_closing_fence_suffix(fence_stack))
            if current and len(projected) + closing_cost > limit:
                chunks.append(_finalize_chunk(current, fence_stack))
                current = _opening_fence_prefix(fence_stack)
            current += piece
            _update_fence_stack(fence_stack, piece)

    if current:
        chunks.append(_finalize_chunk(current, fence_stack))

    return [chunk if chunk.strip() else "(empty response)" for chunk in chunks]


def _looks_like_table_header(header: str, separator: str) -> bool:
    if "|" not in header or "|" not in separator:
        return False
    cells = [cell.strip() for cell in separator.strip().strip("|").split("|")]
    if not cells:
        return False
    return all(cell and set(cell) <= {":", "-"} and "-" in cell for cell in cells)


def _opening_fence_prefix(fence_stack: list[str]) -> str:
    if not fence_stack:
        return ""
    return "".join(f"```{language}\n" for language in fence_stack)


def _closing_fence_suffix(fence_stack: list[str]) -> str:
    if not fence_stack:
        return ""
    return "".join("\n```" for _ in fence_stack)


def _finalize_chunk(text: str, fence_stack: list[str]) -> str:
    chunk = text.rstrip("\n")
    if fence_stack:
        chunk += _closing_fence_suffix(fence_stack)
    return chunk


def _update_fence_stack(fence_stack: list[str], text: str) -> None:
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("```"):
            continue
        language = stripped[3:].strip()
        if fence_stack:
            fence_stack.pop()
        else:
            fence_stack.append(language)


def _split_line_to_fit(line: str, limit: int) -> list[str]:
    if len(line) <= limit:
        return [line]

    pieces: list[str] = []
    remaining = line
    while remaining:
        if len(remaining) <= limit:
            pieces.append(remaining)
            break
        split_at = _preferred_split_index(remaining[:limit], remaining)
        pieces.append(remaining[:split_at])
        remaining = remaining[split_at:]
    return pieces


def _preferred_split_index(candidate: str, full_text: str) -> int:
    for marker in ("\n\n", "\n", ". ", "! ", "? ", " "):
        index = candidate.rfind(marker)
        if index >= max(40, len(candidate) // 2):
            return index + len(marker)
    return max(1, len(candidate))
