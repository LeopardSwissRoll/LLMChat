"""HTML rendering for the web log viewer.

Converts bot responses into self-contained HTML pages with:
- Dark theme matching Discord aesthetics
- Persona avatar + name + provider badge
- Syntax-highlighted code blocks (Pygments, inline styles)
- Markdown-to-HTML via regex (no ``markdown`` dependency)
- Path sanitization for public hosting
"""
from __future__ import annotations

import html
import re
from datetime import datetime, timezone

try:
    from pygments import highlight
    from pygments.formatters import HtmlFormatter
    from pygments.lexers import get_lexer_by_name, guess_lexer, TextLexer

    _PYGMENTS = True
    _FORMATTER = HtmlFormatter(style="monokai", noclasses=True, nowrap=False)
    _PYGMENTS_CSS = _FORMATTER.get_style_defs(".highlight")
except ImportError:
    _PYGMENTS = False
    _PYGMENTS_CSS = ""

# ---------------------------------------------------------------------------
# Path sanitization
# ---------------------------------------------------------------------------

_PATH_PATTERNS = [
    re.compile(r"[A-Z]:\\Users\\[^\s\\]+", re.IGNORECASE),
    re.compile(r"/(?:home|Users)/[^\s/]+", re.IGNORECASE),
]


def _sanitize_paths(text: str) -> str:
    for pat in _PATH_PATTERNS:
        text = pat.sub("[user]", text)
    return text


# ---------------------------------------------------------------------------
# Minimal Markdown → HTML
# ---------------------------------------------------------------------------

_FENCED_CODE_RE = re.compile(
    r"```(\w*)\n(.*?)```", re.DOTALL,
)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_HEADING_RE = re.compile(r"^(#{1,3})\s+(.+)$", re.MULTILINE)
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_ITALIC_RE = re.compile(r"(?<!\*)\*([^*]+)\*(?!\*)")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_TOOL_SUMMARY_RE = re.compile(r"^(🔧\s+.+)$", re.MULTILINE)
_UNORDERED_LIST_RE = re.compile(r"^[-*]\s+(.+)$", re.MULTILINE)


def _highlight_code(code: str, lang: str) -> str:
    """Highlight a code block with Pygments (or plain <pre> fallback)."""
    if not _PYGMENTS:
        escaped = html.escape(code)
        return f'<pre class="code-block"><code>{escaped}</code></pre>'
    try:
        if lang:
            lexer = get_lexer_by_name(lang, stripall=True)
        else:
            lexer = guess_lexer(code)
    except Exception:
        lexer = TextLexer()
    return highlight(code, lexer, _FORMATTER)


def _md_to_html(text: str) -> str:
    """Convert a limited markdown subset to HTML."""
    # Escape HTML first (we'll re-add our own tags)
    text = html.escape(text)

    # Restore fenced code blocks (were escaped, need re-processing)
    # We work on the escaped text, so ``` becomes ```
    # Actually, let's work differently — process code blocks BEFORE escaping.
    return _md_to_html_impl(text)


def _md_to_html_impl(raw: str) -> str:
    """Two-pass markdown conversion: code blocks first, then inline."""
    # --- Pass 1: extract fenced code blocks ---
    code_blocks: list[str] = []

    def _replace_code(m: re.Match) -> str:
        lang = m.group(1)
        code = m.group(2).rstrip("\n")
        rendered = _highlight_code(code, lang)
        placeholder = f"\x00CODE{len(code_blocks)}\x00"
        code_blocks.append(rendered)
        return placeholder

    text = _FENCED_CODE_RE.sub(_replace_code, raw)

    # --- Escape HTML (after extracting code blocks) ---
    text = html.escape(text)

    # --- Pass 2: inline formatting ---
    # Headings
    def _heading_repl(m: re.Match) -> str:
        level = len(m.group(1))
        return f"<h{level}>{m.group(2)}</h{level}>"
    text = _HEADING_RE.sub(_heading_repl, text)

    # Bold / italic
    text = _BOLD_RE.sub(r"<strong>\1</strong>", text)
    text = _ITALIC_RE.sub(r"<em>\1</em>", text)

    # Links
    text = _LINK_RE.sub(r'<a href="\2" target="_blank">\1</a>', text)

    # Inline code
    text = _INLINE_CODE_RE.sub(r'<code class="inline">\1</code>', text)

    # Tool summaries (🔧 lines)
    text = _TOOL_SUMMARY_RE.sub(r'<div class="tool-summary">\1</div>', text)

    # Unordered lists (simple — consecutive lines only)
    def _list_repl(m: re.Match) -> str:
        return f"<li>{m.group(1)}</li>"
    text = _UNORDERED_LIST_RE.sub(_list_repl, text)
    text = re.sub(r"(<li>.*?</li>\n?)+", lambda m: f"<ul>{m.group(0)}</ul>", text)

    # Paragraphs: double newline → <p>
    parts = re.split(r"\n{2,}", text)
    processed: list[str] = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        # Don't wrap block elements in <p>
        if part.startswith(("<h", "<ul", "<div", "\x00CODE")):
            processed.append(part)
        else:
            processed.append(f"<p>{part.replace(chr(10), '<br>')}</p>")
    text = "\n".join(processed)

    # --- Restore code blocks ---
    for i, block in enumerate(code_blocks):
        text = text.replace(f"\x00CODE{i}\x00", block)
        # Also check escaped version
        text = text.replace(html.escape(f"\x00CODE{i}\x00"), block)

    return text


# ---------------------------------------------------------------------------
# HTML page template
# ---------------------------------------------------------------------------

_PAGE_TEMPLATE = """\
<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="robots" content="noindex, nofollow">
<meta name="theme-color" content="#11111b">
<title>{persona_name} \u00b7 Response</title>
<style>
  :root {{
    --bg-base: #11111b;
    --bg-surface: #181825;
    --bg-overlay: #1e1e2e;
    --bg-card: #24243a;
    --border: #313244;
    --border-hover: #45475a;
    --text: #cdd6f4;
    --text-dim: #6c7086;
    --text-muted: #45475a;
    --accent: #cba6f7;
    --accent-glow: rgba(203, 166, 247, 0.12);
    --pink: #f5c2e7;
    --peach: #fab387;
    --blue: #89b4fa;
    --green: #a6e3a1;
    --red: #f38ba8;
    --rosewater: #f5e0dc;
    --mono: 'Cascadia Code', 'Fira Code', 'JetBrains Mono', 'SF Mono', monospace;
  }}
  *, *::before, *::after {{ margin: 0; padding: 0; box-sizing: border-box; }}
  html {{ scroll-behavior: smooth; }}
  body {{
    background: var(--bg-base);
    color: var(--text);
    font-family: 'Pretendard', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    line-height: 1.72;
    min-height: 100dvh;
    -webkit-font-smoothing: antialiased;
  }}

  /* ── Ambient background ── */
  body::before {{
    content: '';
    position: fixed; inset: 0; z-index: -1;
    background:
      radial-gradient(ellipse 60% 50% at 20% 0%, var(--accent-glow) 0%, transparent 70%),
      radial-gradient(ellipse 40% 40% at 80% 100%, rgba(137, 180, 250, 0.06) 0%, transparent 60%);
  }}

  /* ── Layout shell ── */
  .shell {{
    max-width: 52rem;
    margin: 0 auto;
    padding: 0 1rem;
  }}

  /* ── Sticky header ── */
  .hdr {{
    position: sticky; top: 0; z-index: 10;
    background: linear-gradient(var(--bg-base) 70%, transparent);
    padding: 1.25rem 0 1.5rem;
  }}
  .hdr-inner {{
    display: flex; align-items: center; gap: 0.875rem;
  }}
  .av {{
    width: 44px; height: 44px; border-radius: 50%;
    background: var(--bg-card);
    flex-shrink: 0;
    box-shadow: 0 0 0 2px var(--border), 0 2px 8px rgba(0,0,0,0.3);
    overflow: hidden;
  }}
  .av img {{ width: 100%; height: 100%; object-fit: cover; }}
  .hdr-text {{ flex: 1; min-width: 0; }}
  .hdr-name {{
    font-size: 1.05rem; font-weight: 700;
    color: var(--accent);
    letter-spacing: -0.01em;
  }}
  .hdr-badge {{
    display: inline-block;
    font-size: 0.65rem; font-weight: 600;
    text-transform: uppercase; letter-spacing: 0.06em;
    color: var(--bg-base);
    background: var(--accent);
    padding: 0.1rem 0.45rem;
    border-radius: 3px;
    margin-left: 0.5rem;
    vertical-align: middle;
    position: relative; top: -1px;
  }}
  .hdr-time {{
    display: block;
    font-size: 0.75rem; color: var(--text-dim);
    margin-top: 0.1rem;
  }}

  /* ── Navigation bar ── */
  .nav {{
    display: flex; align-items: center; gap: 0.5rem;
    padding: 0.6rem 0;
    margin-bottom: 0.25rem;
    border-bottom: 1px solid var(--border);
    flex-wrap: wrap;
  }}
  .nav-btn {{
    display: inline-flex; align-items: center; gap: 0.3rem;
    padding: 0.35rem 0.7rem;
    border-radius: 6px;
    border: 1px solid var(--border);
    background: var(--bg-surface);
    color: var(--text-dim);
    font-size: 0.75rem;
    font-family: inherit;
    text-decoration: none;
    cursor: pointer;
    transition: all 0.15s;
    white-space: nowrap;
  }}
  .nav-btn:hover {{
    border-color: var(--accent);
    color: var(--accent);
    background: var(--accent-glow);
  }}
  .nav-btn.disabled {{
    opacity: 0.3;
    pointer-events: none;
  }}
  .nav-btn svg {{
    width: 14px; height: 14px;
    fill: currentColor;
    flex-shrink: 0;
  }}
  .nav-spacer {{ flex: 1; }}
  .nav-btn.terminal {{
    border-color: var(--green);
    color: var(--green);
  }}
  .nav-btn.terminal:hover {{
    background: rgba(166, 227, 161, 0.1);
    box-shadow: 0 0 8px rgba(166, 227, 161, 0.15);
  }}
  @media (max-width: 640px) {{
    .nav {{ gap: 0.35rem; }}
    .nav-btn {{ padding: 0.3rem 0.55rem; font-size: 0.7rem; }}
    .nav-btn svg {{ width: 12px; height: 12px; }}
  }}

  /* ── Content area ── */
  .body {{
    padding-bottom: 3rem;
    animation: fadeUp 0.4s ease-out;
  }}
  @keyframes fadeUp {{
    from {{ opacity: 0; transform: translateY(12px); }}
    to {{ opacity: 1; transform: translateY(0); }}
  }}

  .body p {{
    margin: 0.65rem 0;
    font-size: 0.938rem;
  }}
  .body h1 {{
    font-size: 1.35rem; font-weight: 800;
    color: var(--pink);
    margin: 1.75rem 0 0.5rem;
    letter-spacing: -0.02em;
  }}
  .body h2 {{
    font-size: 1.15rem; font-weight: 700;
    color: var(--pink);
    margin: 1.4rem 0 0.4rem;
  }}
  .body h3 {{
    font-size: 1rem; font-weight: 600;
    color: var(--rosewater);
    margin: 1.1rem 0 0.35rem;
  }}
  .body strong {{ color: var(--rosewater); font-weight: 600; }}
  .body em {{ color: var(--text); font-style: italic; }}
  .body a {{
    color: var(--blue);
    text-decoration: underline;
    text-decoration-color: rgba(137, 180, 250, 0.3);
    text-underline-offset: 2px;
    transition: text-decoration-color 0.2s;
  }}
  .body a:hover {{ text-decoration-color: var(--blue); }}

  /* ── Lists ── */
  .body ul {{
    margin: 0.5rem 0 0.5rem 1.25rem;
    list-style: none;
  }}
  .body ul li {{
    position: relative;
    padding-left: 0.2rem;
    margin: 0.3rem 0;
    font-size: 0.938rem;
  }}
  .body ul li::before {{
    content: '\25B8';
    position: absolute; left: -1.1rem;
    color: var(--accent);
    font-size: 0.7em;
    top: 0.35em;
  }}

  /* ── Inline code ── */
  .body code.inline {{
    background: var(--bg-card);
    color: var(--green);
    padding: 0.12rem 0.4rem;
    border-radius: 4px;
    font-family: var(--mono);
    font-size: 0.85em;
    border: 1px solid var(--border);
    word-break: break-word;
  }}

  /* ── Code blocks ── */
  .body .highlight, .body .code-block {{
    position: relative;
    background: var(--bg-surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 1rem;
    margin: 0.75rem 0;
    font-family: var(--mono);
    font-size: 0.8rem;
    line-height: 1.55;
    overflow-x: auto;
    -webkit-overflow-scrolling: touch;
  }}
  .body .code-block code {{ color: var(--text); }}
  /* Collapse long code blocks on mobile */
  @media (max-width: 640px) {{
    .body .highlight, .body .code-block {{
      max-height: 20rem;
      overflow-y: auto;
      font-size: 0.75rem;
      padding: 0.75rem;
    }}
  }}

  /* ── Tool summaries ── */
  .tool-summary {{
    display: flex; align-items: baseline; gap: 0.5rem;
    background: var(--bg-surface);
    border: 1px solid var(--border);
    border-left: 3px solid var(--peach);
    padding: 0.5rem 0.75rem;
    margin: 0.5rem 0;
    border-radius: 0 8px 8px 0;
    font-family: var(--mono);
    font-size: 0.8rem;
    color: var(--peach);
    line-height: 1.45;
    word-break: break-word;
    overflow-wrap: anywhere;
  }}

  /* ── Footer ── */
  .ftr {{
    border-top: 1px solid var(--border);
    padding: 1rem 0 2rem;
    display: flex;
    justify-content: space-between;
    align-items: center;
    font-size: 0.7rem;
    color: var(--text-muted);
    flex-wrap: wrap;
    gap: 0.5rem;
  }}
  .ftr a {{
    color: var(--text-dim);
    text-decoration: none;
  }}
  .ftr a:hover {{ color: var(--accent); }}

  /* ── Mobile refinements ── */
  @media (max-width: 640px) {{
    .shell {{ padding: 0 0.75rem; }}
    .hdr {{ padding: 0.875rem 0 1rem; }}
    .av {{ width: 36px; height: 36px; }}
    .hdr-name {{ font-size: 0.95rem; }}
    .body p, .body ul li {{ font-size: 0.875rem; }}
    .body h1 {{ font-size: 1.15rem; }}
    .body h2 {{ font-size: 1.05rem; }}
    .ftr {{ font-size: 0.65rem; }}
  }}

  /* ── Scrollbar ── */
  ::-webkit-scrollbar {{ width: 6px; height: 6px; }}
  ::-webkit-scrollbar-track {{ background: transparent; }}
  ::-webkit-scrollbar-thumb {{
    background: var(--border);
    border-radius: 3px;
  }}
  ::-webkit-scrollbar-thumb:hover {{ background: var(--border-hover); }}

  {pygments_css}
</style>
</head>
<body>
<div class="shell">
  <header class="hdr">
    <div class="hdr-inner">
      <div class="av">{avatar_html}</div>
      <div class="hdr-text">
        <span class="hdr-name">{persona_name}</span>
        <span class="hdr-badge">{provider_id}</span>
        <time class="hdr-time" data-ts="{timestamp_iso}">{timestamp_display}</time>
      </div>
    </div>
  </header>
  <nav class="nav">
    <a class="nav-btn {prev_cls}" href="/r/{prev_id}" title="Previous">
      <svg viewBox="0 0 24 24"><path d="M15.41 7.41L14 6l-6 6 6 6 1.41-1.41L10.83 12z"/></svg>
      <span>Prev</span>
    </a>
    <a class="nav-btn {next_cls}" href="/r/{next_id}" title="Next">
      <span>Next</span>
      <svg viewBox="0 0 24 24"><path d="M10 6L8.59 7.41 13.17 12l-4.58 4.59L10 18l6-6z"/></svg>
    </a>
    <span class="nav-spacer"></span>
    <a class="nav-btn terminal" href="/terminal/{session_key}" title="Live Terminal">
      <svg viewBox="0 0 24 24"><path d="M20 4H4c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V6c0-1.1-.9-2-2-2zm0 14H4V8h16v10zm-7-2h5v-2h-5v2zm-4.71-3.29L5.7 10.12 7.12 8.7 10.41 12l-3.29 3.29-1.42-1.41 1.88-1.88z"/></svg>
      <span>Terminal</span>
    </a>
  </nav>
  <main class="body">
{content_html}
  </main>
  <footer class="ftr">
    <span>{msg_id}</span>
    <a href="/">all responses</a>
  </footer>
</div>
<script>
  document.querySelectorAll('[data-ts]').forEach(el => {{
    const d = new Date(el.dataset.ts);
    if (!isNaN(d)) el.textContent = d.toLocaleString();
  }});
</script>
</body>
</html>
"""


def render_response_html(
    msg_id: str,
    persona_name: str,
    persona_avatar_url: str | None,
    provider_id: str,
    response_raw: str,
    opening_line: str = "",
    timestamp: str = "",
    prev_msg_id: str = "",
    next_msg_id: str = "",
    session_key: str = "",
) -> str:
    """Render a bot response as a self-contained HTML page."""
    # Sanitize paths for public hosting
    sanitized = _sanitize_paths(response_raw)

    # Convert markdown to HTML
    content_html = _md_to_html_impl(sanitized)

    # Avatar
    if persona_avatar_url:
        avatar_html = f'<img src="{html.escape(persona_avatar_url)}" alt="{html.escape(persona_name)}">'
    else:
        avatar_html = f'<span style="display:flex;align-items:center;justify-content:center;width:100%;height:100%;font-size:1.5rem;color:#cdd6f4">{html.escape(persona_name[:1])}</span>'

    # Timestamp
    if not timestamp:
        timestamp = datetime.now(timezone.utc).isoformat()
    try:
        dt = datetime.fromisoformat(timestamp)
        ts_display = dt.strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        ts_display = timestamp

    return _PAGE_TEMPLATE.format(
        msg_id=html.escape(msg_id),
        persona_name=html.escape(persona_name),
        provider_id=html.escape(provider_id),
        avatar_html=avatar_html,
        timestamp_iso=html.escape(timestamp),
        timestamp_display=html.escape(ts_display),
        content_html=content_html,
        pygments_css=_PYGMENTS_CSS,
        prev_id=html.escape(prev_msg_id) if prev_msg_id else "",
        next_id=html.escape(next_msg_id) if next_msg_id else "",
        prev_cls="" if prev_msg_id else "disabled",
        next_cls="" if next_msg_id else "disabled",
        session_key=html.escape(session_key) if session_key else "",
    )
