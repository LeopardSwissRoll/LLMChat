from __future__ import annotations

from pathlib import Path

import pytest

from ExportV2.models import PersonaConfig
from ExportV2.prompt_composer import PromptComposer

PROMPTS_ROOT = Path(__file__).resolve().parent.parent / "prompts"


def _persona() -> PersonaConfig:
    return PersonaConfig(
        persona_id="example",
        display_name="Example",
        role_aliases=("Example",),
        identity_text="test",
        prompt_dir=Path("example"),
    )


@pytest.fixture
def composer() -> PromptComposer:
    return PromptComposer(PROMPTS_ROOT)


def test_lv1_keeps_only_first_section(composer):
    text = composer.compose("claude", _persona(), mode=1)
    assert "## @@1:" in text
    assert "## @@2:" not in text


def test_lv3_cuts_before_section_5(composer):
    text = composer.compose("claude", _persona(), mode=3)
    assert "## @@4:" in text
    assert "## @@5:" not in text


def test_lv5_is_full_document(composer):
    text = composer.compose("claude", _persona(), mode=5)
    assert "## @@5:" in text


def test_reference_marker_is_replaced_with_persona_dir(composer):
    text = composer.compose("claude", _persona(), mode=5)
    assert "@레퍼런스" not in text
    assert "추가 레퍼런스 폴더:" in text
    assert str((PROMPTS_ROOT / "example").resolve()) in text


def test_protagonist_is_injected_and_mention_resolved(composer):
    text = composer.compose("claude", _persona(), protagonist_id="123", mode=5)
    assert "주인공 = <@123>" in text
    assert "@주인공" not in text
    assert "<@123>" in text


def test_mode_aliases_normalize():
    assert PromptComposer._normalize_mode("core") == 1
    assert PromptComposer._normalize_mode("masquerade") == 5
    assert PromptComposer._normalize_mode("9") == 5
    assert PromptComposer._normalize_mode("nonsense") == 3


def test_missing_persona_raises(composer):
    missing = PersonaConfig(
        persona_id="nope", display_name="nope", role_aliases=("nope",),
        identity_text="", prompt_dir=Path("does-not-exist"),
    )
    with pytest.raises(FileNotFoundError):
        composer.compose("claude", missing)
