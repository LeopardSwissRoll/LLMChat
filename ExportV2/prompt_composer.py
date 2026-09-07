from __future__ import annotations

import logging
import re
from pathlib import Path

from .models import PersonaConfig

LOGGER = logging.getLogger(__name__)

SECTION_RE = re.compile(r'^## @@(\d+):', re.MULTILINE)
REFERENCE_MARKER_RE = re.compile(r'^\s*@레퍼런스\s*$', re.MULTILINE)


class PromptComposer:
    """Combines shared + provider + persona prompt assets under ``prompts_root``.

    Layout:
        prompts_root/bot_global.md            shared preamble (optional)
        prompts_root/providers/<provider>.md  per-provider instructions (optional)
        prompts_root/<prompt_dir>/PROMPT.md   persona document (required)

    Persona Levels (``mode``):
        LV1 (core)       — @@1 only
        LV2 (soft)       — @@1~@@3
        LV3 (medium)     — @@1~@@4
        LV4 (hard)       — @@1~@@5
        LV5 (masquerade) — full PROMPT.md
    """

    def __init__(self, prompts_root: Path) -> None:
        self._prompts_root = Path(prompts_root)

    def compose(
        self,
        provider_id: str,
        persona: PersonaConfig,
        protagonist_id: str | None = None,
        mode: int | str = 3,
    ) -> str:
        parts: list[str] = []
        mode_level = self._normalize_mode(mode)

        bot_global = self._prompts_root / "bot_global.md"
        if bot_global.exists():
            parts.append(bot_global.read_text(encoding="utf-8"))

        provider_prompt = self._prompts_root / "providers" / f"{provider_id}.md"
        if provider_prompt.exists():
            parts.append(provider_prompt.read_text(encoding="utf-8"))

        persona_dir = self._prompts_root / persona.prompt_dir
        persona_path = persona_dir / "PROMPT.md"
        if not persona_path.exists():
            raise FileNotFoundError(f"Persona PROMPT.md not found: {persona_path}")
        full_text = persona_path.read_text(encoding="utf-8")

        if mode_level == 1:
            persona_text = self._extract_section(full_text, 1)
        elif mode_level <= 4:
            persona_text = self._extract_before_section(full_text, mode_level + 2)
        else:
            persona_text = full_text

        persona_text = self._resolve_reference_marker(persona_text, persona_dir)

        if protagonist_id:
            persona_text = self._inject_protagonist(persona_text, protagonist_id)
            persona_text = persona_text.replace("@주인공", f"<@{protagonist_id}>")

        parts.append(persona_text)
        combined = "\n\n".join(parts)

        LOGGER.debug(
            "Composed prompt for '%s' provider=%s mode=%s (%d chars, protagonist=%s)",
            persona.persona_id, provider_id, mode_level, len(combined), protagonist_id,
        )
        return combined

    @staticmethod
    def _normalize_mode(mode: int | str) -> int:
        if isinstance(mode, int):
            return max(1, min(5, mode))
        raw = str(mode).strip().lower()
        if raw.isdigit():
            return max(1, min(5, int(raw)))
        aliases = {
            "core": 1,
            "tend": 2,
            "soft": 2,
            "medium": 3,
            "hard": 4,
            "persona": 5,
            "masquerade": 5,
        }
        return aliases.get(raw, 3)

    @staticmethod
    def _extract_before_section(text: str, section_num: int) -> str:
        for m in SECTION_RE.finditer(text):
            if int(m.group(1)) >= section_num:
                return text[:m.start()].strip()
        return text

    @staticmethod
    def _extract_section(text: str, section_num: int) -> str:
        matches = list(SECTION_RE.finditer(text))
        for i, m in enumerate(matches):
            if int(m.group(1)) == section_num:
                start = m.start()
                end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
                return text[start:end].strip()
        return text

    @staticmethod
    def _resolve_reference_marker(text: str, persona_dir: Path) -> str:
        if not REFERENCE_MARKER_RE.search(text):
            return text
        replacement = f"추가 레퍼런스 폴더: {persona_dir.resolve()}"
        return REFERENCE_MARKER_RE.sub(lambda _: replacement, text)

    @staticmethod
    def _inject_protagonist(text: str, protagonist_id: str) -> str:
        protagonist_line = f"\n주인공 = <@{protagonist_id}>"
        matches = list(SECTION_RE.finditer(text))
        for i, m in enumerate(matches):
            if int(m.group(1)) == 1:
                if i + 1 < len(matches):
                    insert_pos = matches[i + 1].start()
                else:
                    insert_pos = len(text)
                return text[:insert_pos].rstrip() + protagonist_line + "\n\n" + text[insert_pos:]
        return text + protagonist_line

    def get_persona_dir(self, persona: PersonaConfig) -> Path:
        return (self._prompts_root / persona.prompt_dir).resolve()
