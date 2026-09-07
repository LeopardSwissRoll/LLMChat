from __future__ import annotations

from ExportV2.textutil import collapse_tool_blocks, compress_blank_lines


def test_collapse_claude_tool_block_shortens_path_and_keeps_result():
    text = (
        "● Write(C:\\proj\\out\\index.html)\n"
        "  <html>...</html>\n"
        "  ⎿  Wrote 1310 lines to C:\\proj\\out\\index.html\n"
        "\n"
        "Done."
    )
    out = collapse_tool_blocks(text)
    assert out.splitlines()[0] == "\U0001f527 Write(index.html) \u2192 Wrote 1310 lines to C:\\proj\\out\\index.html"
    assert out.splitlines()[-1] == "Done."


def test_collapse_claude_marker_without_result_consumes_until_next_marker():
    # A ● marker with no ⎿ result line swallows the following lines until the
    # next tool marker (or end of text) — that span is treated as the tool's
    # content block. Documented as-is so a future change is deliberate.
    out = collapse_tool_blocks("● Bash(git status)\nnext line")
    assert out == "\U0001f527 Bash(git status)"

    out = collapse_tool_blocks("● Bash(git status)\nstuff\n● Read(a.py)\n  ⎿  ok\nafter")
    assert out == "\U0001f527 Bash(git status)\n\U0001f527 Read(a.py) \u2192 ok\nafter"


def test_collapse_codex_running_then_ran_with_result():
    text = (
        "• Running git status --short --branch\n"
        "• Ran git status --short --branch\n"
        "└ fatal: not a git repository\n"
        "answer"
    )
    out = collapse_tool_blocks(text)
    assert out == (
        "\U0001f527 git status --short --branch \u2192 fatal: not a git repository\n"
        "answer"
    )


def test_collapse_leaves_plain_text_untouched():
    text = "hello\n\nworld"
    assert collapse_tool_blocks(text) == text


def test_compress_blank_lines_collapses_runs():
    assert compress_blank_lines("a\n\n\n\nb\n\nc") == "a\n\nb\n\nc"
