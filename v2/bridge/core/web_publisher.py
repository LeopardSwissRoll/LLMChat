"""Local web server for bot response logs with Discord OAuth.

Replaces GitHub Pages approach — renders HTML locally and serves via
aiohttp with Discord OAuth2 authentication (guild membership check).

Usage:
    Set WEB_PUBLISH_ENABLED=true in .env along with:
    - DISCORD_CLIENT_ID
    - DISCORD_CLIENT_SECRET
    - WEB_PUBLISH_PORT (default 8080)
    - WEB_PUBLISH_GUILD_ID (Discord server ID to check membership)
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from urllib.parse import urlencode

from ..models import PersonaConfig, ResponseMeta
from ..web_template import render_response_html

LOGGER = logging.getLogger(__name__)

try:
    from aiohttp import web, ClientSession
    _AIOHTTP = True
except ImportError:
    _AIOHTTP = False


class WebPublisher:
    """Renders bot responses as HTML and serves via local web server."""

    def __init__(
        self,
        pages_dir: Path,
        base_url: str = "http://localhost:8080",
        port: int = 8080,
        client_id: str = "",
        client_secret: str = "",
        guild_id: str = "",
        threshold: int = 500,
        summary_length: int = 200,
    ) -> None:
        self._pages_dir = pages_dir
        self._base_url = base_url.rstrip("/")
        self._port = port
        self._client_id = client_id
        self._client_secret = client_secret
        self._guild_id = guild_id
        self.threshold = threshold
        self.summary_length = summary_length

        self._pages_dir.mkdir(parents=True, exist_ok=True)
        (self._pages_dir / "r").mkdir(exist_ok=True)

        # Session store: token → {user_id, username, avatar, guilds}
        self._sessions: dict[str, dict] = {}
        # OAuth state store: state_token → next_path (CSRF protection)
        self._oauth_states: dict[str, str] = {}
        self._bridge_core = None

        self._runner: web.AppRunner | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the web server."""
        if not _AIOHTTP:
            LOGGER.error("aiohttp not installed — web publisher disabled")
            return

        app = web.Application()
        app.router.add_get("/", self._handle_index)
        app.router.add_get("/terminal", self._handle_terminal_index)
        app.router.add_get("/terminal/{session_id}", self._handle_terminal_page)
        app.router.add_get("/ws/terminal/{session_id}", self._handle_terminal_ws)
        app.router.add_get("/auth/login", self._handle_login)
        app.router.add_get("/auth/callback", self._handle_callback)
        app.router.add_get("/auth/logout", self._handle_logout)
        app.router.add_get("/r/{msg_id}", self._handle_response_page)
        app.router.add_get("/api/responses", self._handle_api_responses)

        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "0.0.0.0", self._port)
        await site.start()
        LOGGER.info("Web server started on port %d (%s)", self._port, self._base_url)
        if "localhost" in self._base_url or "127.0.0.1" in self._base_url:
            LOGGER.warning(
                "WEB_PUBLISH_BASE_URL is localhost — Discord OAuth "
                "will only work locally. Set a public URL (e.g. via "
                "Cloudflare Tunnel) for external access."
            )

    async def stop(self) -> None:
        """Stop the web server."""
        if self._runner:
            await self._runner.cleanup()

    def set_bridge_core(self, bridge_core) -> None:
        """Attach BridgeCore for live session inspection routes."""
        self._bridge_core = bridge_core

    async def enqueue(
        self,
        msg_id: str,
        persona: PersonaConfig,
        provider_id: str,
        response_raw: str,
        meta: ResponseMeta,
        session_key: str = "",
    ) -> None:
        """Render HTML and save to disk (immediate, no git)."""
        timestamp = datetime.now(timezone.utc).isoformat()

        # Read previous msg_id from manifest for prev/next navigation
        prev_msg_id = self._get_last_msg_id()

        html_content = render_response_html(
            msg_id=msg_id,
            persona_name=persona.display_name,
            persona_avatar_url=persona.avatar_url,
            provider_id=provider_id,
            response_raw=response_raw,
            opening_line=meta.opening_line,
            timestamp=timestamp,
            prev_msg_id=prev_msg_id,
            session_key=session_key,
        )

        # Write HTML file
        path = self._pages_dir / "r" / f"{msg_id}.html"
        await asyncio.to_thread(path.write_text, html_content, "utf-8")

        # Update previous page's "next" link
        if prev_msg_id:
            await self._patch_next_link(prev_msg_id, msg_id)

        # Append to manifest
        entry = {
            "msg_id": msg_id,
            "persona": persona.persona_id,
            "provider": provider_id,
            "timestamp": timestamp,
            "opening_line": meta.opening_line[:200] if meta.opening_line else "",
            "chars": len(response_raw),
        }
        manifest = self._pages_dir / "_manifest.jsonl"
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        await asyncio.to_thread(_append_text, manifest, line)

        LOGGER.debug("Saved response page: r/%s.html", msg_id)

    def _get_last_msg_id(self) -> str:
        """Read the last msg_id from manifest for prev navigation."""
        manifest = self._pages_dir / "_manifest.jsonl"
        if not manifest.exists():
            return ""
        try:
            lines = manifest.read_text("utf-8").strip().split("\n")
            for line in reversed(lines):
                if line.strip():
                    return json.loads(line).get("msg_id", "")
        except Exception:
            pass
        return ""

    async def _patch_next_link(self, prev_msg_id: str, next_msg_id: str) -> None:
        """Update the previous page's Next button to point to this page."""
        prev_path = self._pages_dir / "r" / f"{prev_msg_id}.html"
        if not prev_path.exists():
            return
        try:
            content = await asyncio.to_thread(prev_path.read_text, "utf-8")
            # Replace the disabled next button with an active one
            content = content.replace(
                'class="nav-btn disabled" href="/r/"',
                f'class="nav-btn " href="/r/{next_msg_id}"',
            )
            await asyncio.to_thread(prev_path.write_text, content, "utf-8")
        except Exception:
            LOGGER.debug("Failed to patch next link on %s", prev_msg_id)

    def url_for(self, msg_id: str) -> str:
        """URL for a response page."""
        return f"{self._base_url}/r/{msg_id}"

    # ------------------------------------------------------------------
    # Auth helpers
    # ------------------------------------------------------------------

    def _get_session(self, request: "web.Request") -> dict | None:
        """Get session from cookie token."""
        token = request.cookies.get("bridge_session")
        if token and token in self._sessions:
            return self._sessions[token]
        return None

    def _require_auth(self, request: "web.Request") -> dict:
        """Get session or raise 401 redirect."""
        session = self._get_session(request)
        if not session:
            raise web.HTTPFound(f"/auth/login?next={request.path}")
        return session

    @property
    def _redirect_uri(self) -> str:
        return f"{self._base_url}/auth/callback"

    # ------------------------------------------------------------------
    # Route handlers
    # ------------------------------------------------------------------

    async def _handle_index(self, request: "web.Request") -> "web.Response":
        """Index page — list recent responses."""
        session = self._get_session(request)
        if not session:
            return web.Response(
                text=_LOGIN_PAGE.format(base_url=self._base_url),
                content_type="text/html",
            )

        # Read manifest
        manifest = self._pages_dir / "_manifest.jsonl"
        entries: list[dict] = []
        if manifest.exists():
            for line in manifest.read_text("utf-8").strip().split("\n"):
                if line.strip():
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        entries.reverse()  # newest first

        rows = []
        for e in entries[:100]:
            opening = e.get("opening_line", "")[:80]
            rows.append(
                f'<tr>'
                f'<td><a href="/r/{e["msg_id"]}">{e.get("persona", "?")}</a></td>'
                f'<td>{e.get("provider", "")}</td>'
                f'<td>{opening}</td>'
                f'<td>{e.get("chars", 0)}</td>'
                f'<td>{e.get("timestamp", "")[:19]}</td>'
                f'</tr>'
            )

        return web.Response(
            text=_INDEX_PAGE.format(
                username=session.get("username", "?"),
                rows="\n".join(rows),
            ),
            content_type="text/html",
        )

    async def _handle_terminal_index(self, request: "web.Request") -> "web.Response":
        """List active PTY sessions for live inspection."""
        session = self._require_auth(request)
        if not self._bridge_core:
            raise web.HTTPServiceUnavailable(text="Bridge core unavailable")

        rows = []
        for item in self._bridge_core.get_session_list():
            workspace_name = Path(item["workspace"]).name or item["workspace"]
            rows.append(
                f'<tr>'
                f'<td><a href="/terminal/{item["session_id"]}">{escape(item["display_name"])}</a></td>'
                f'<td>{escape(item["provider_id"])}</td>'
                f'<td>{item["channel_id"]}</td>'
                f'<td>{escape(workspace_name)}</td>'
                f'<td>{escape(item["state"])}</td>'
                f'<td>{item["rows"]}x{item["cols"]}</td>'
                f'<td><code>{item["session_id"]}</code></td>'
                f'</tr>'
            )

        return web.Response(
            text=_TERMINAL_INDEX_PAGE.format(
                username=escape(session.get("username", "?")),
                rows="\n".join(rows) or '<tr><td colspan="7">활성 세션이 없습니다.</td></tr>',
            ),
            content_type="text/html",
        )

    async def _handle_terminal_page(self, request: "web.Request") -> "web.Response":
        """Serve the xterm.js live terminal viewer page."""
        self._require_auth(request)
        if not self._bridge_core:
            raise web.HTTPServiceUnavailable(text="Bridge core unavailable")

        session_id = request.match_info["session_id"]
        resolved = self._bridge_core.get_session_by_id(session_id)
        if not resolved:
            raise web.HTTPNotFound()

        key, session = resolved
        title = f"{key.persona_id} · {key.provider_id} · #{key.channel_id}"
        return web.Response(
            text=_TERMINAL_PAGE.format(
                title=escape(title),
                session_id=escape(session_id),
                cols=session.cols,
                rows=session.rows,
            ),
            content_type="text/html",
        )

    async def _handle_terminal_ws(self, request: "web.Request") -> "web.StreamResponse":
        """Read-only WebSocket stream for a live PTY session."""
        if not self._get_session(request):
            return web.Response(text="Unauthorized", status=401)
        if not self._bridge_core:
            raise web.HTTPServiceUnavailable(text="Bridge core unavailable")

        session_id = request.match_info["session_id"]
        resolved = self._bridge_core.get_session_by_id(session_id)
        if not resolved:
            raise web.HTTPNotFound()

        key, pty_session = resolved
        ws = web.WebSocketResponse(heartbeat=30.0)
        await ws.prepare(request)

        loop = asyncio.get_running_loop()
        data_queue: asyncio.Queue[str] = asyncio.Queue(maxsize=256)

        def _on_data(raw: str) -> None:
            def _enqueue() -> None:
                if ws.closed:
                    return
                if not data_queue.full():
                    data_queue.put_nowait(raw)
            loop.call_soon_threadsafe(_enqueue)

        pty_session.add_data_listener(_on_data)

        snapshot = "\n".join(pty_session.get_display_lines()).rstrip()
        await ws.send_json({
            "type": "init",
            "session_id": session_id,
            "persona_id": key.persona_id,
            "provider_id": key.provider_id,
            "channel_id": key.channel_id,
            "state": pty_session.state.value,
            "alive": pty_session.proc.isalive() if pty_session.proc else False,
            "rows": pty_session.rows,
            "cols": pty_session.cols,
            "snapshot": snapshot,
        })

        async def _sender() -> None:
            last_state = pty_session.state.value
            while not ws.closed:
                try:
                    raw = await asyncio.wait_for(data_queue.get(), timeout=1.0)
                    await ws.send_json({"type": "data", "data": raw})
                except asyncio.TimeoutError:
                    pass

                current_state = pty_session.state.value
                if current_state != last_state:
                    await ws.send_json({
                        "type": "state",
                        "state": current_state,
                        "alive": pty_session.proc.isalive() if pty_session.proc else False,
                    })
                    last_state = current_state

        async def _receiver() -> None:
            async for _msg in ws:
                # Read-only for now — ignore client input/messages.
                continue

        sender_task = asyncio.create_task(_sender())
        receiver_task = asyncio.create_task(_receiver())
        done, pending = await asyncio.wait(
            {sender_task, receiver_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        for task in done:
            try:
                await task
            except asyncio.CancelledError:
                pass
        pty_session.remove_data_listener(_on_data)
        return ws

    async def _handle_login(self, request: "web.Request") -> "web.Response":
        """Redirect to Discord OAuth2 with CSRF-safe state token."""
        next_path = request.query.get("next", "/")
        # Validate next_path is a safe relative path (no open redirect)
        if not next_path.startswith("/") or next_path.startswith("//"):
            next_path = "/"
        state_token = secrets.token_urlsafe(24)
        self._oauth_states[state_token] = next_path
        params = {
            "client_id": self._client_id,
            "redirect_uri": self._redirect_uri,
            "response_type": "code",
            "scope": "identify guilds",
            "state": state_token,
        }
        url = f"https://discord.com/oauth2/authorize?{urlencode(params)}"
        raise web.HTTPFound(url)

    async def _handle_callback(self, request: "web.Request") -> "web.Response":
        """Discord OAuth2 callback with CSRF state validation."""
        code = request.query.get("code")
        state_token = request.query.get("state", "")
        if not code:
            return web.Response(text="Missing code", status=400)
        # Validate CSRF state token
        next_path = self._oauth_states.pop(state_token, None)
        if next_path is None:
            return web.Response(text="Invalid state (CSRF check failed)", status=403)

        async with ClientSession() as http:
            # Exchange code for token
            token_resp = await http.post(
                "https://discord.com/api/oauth2/token",
                data={
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": self._redirect_uri,
                },
            )
            if token_resp.status != 200:
                return web.Response(text="Token exchange failed", status=401)
            token_data = await token_resp.json()
            access_token = token_data["access_token"]

            headers = {"Authorization": f"Bearer {access_token}"}

            # Get user info
            user_resp = await http.get(
                "https://discord.com/api/users/@me", headers=headers,
            )
            user_data = await user_resp.json()

            # Check guild membership
            if self._guild_id:
                guilds_resp = await http.get(
                    "https://discord.com/api/users/@me/guilds",
                    headers=headers,
                )
                guilds = await guilds_resp.json()
                guild_ids = {str(g["id"]) for g in guilds}
                if self._guild_id not in guild_ids:
                    return web.Response(
                        text=_DENIED_PAGE,
                        content_type="text/html",
                        status=403,
                    )

        # Create session
        session_token = secrets.token_urlsafe(32)
        self._sessions[session_token] = {
            "user_id": user_data["id"],
            "username": user_data["username"],
            "avatar": user_data.get("avatar"),
        }

        is_https = self._base_url.startswith("https://")
        response = web.HTTPFound(next_path)
        response.set_cookie(
            "bridge_session", session_token,
            max_age=86400 * 7,  # 7 days
            httponly=True,
            secure=is_https,
            samesite="Lax",
        )
        raise response

    async def _handle_logout(self, request: "web.Request") -> "web.Response":
        """Clear session."""
        token = request.cookies.get("bridge_session")
        if token:
            self._sessions.pop(token, None)
        response = web.HTTPFound("/")
        response.del_cookie("bridge_session")
        raise response

    async def _handle_response_page(self, request: "web.Request") -> "web.Response":
        """Serve a rendered response HTML page."""
        self._require_auth(request)
        msg_id = request.match_info["msg_id"]

        # Sanitize msg_id (only digits allowed — Discord snowflake)
        if not msg_id.isdigit():
            raise web.HTTPNotFound()

        path = self._pages_dir / "r" / f"{msg_id}.html"
        if not path.exists():
            raise web.HTTPNotFound()

        html_content = await asyncio.to_thread(path.read_text, "utf-8")
        return web.Response(text=html_content, content_type="text/html")

    async def _handle_api_responses(self, request: "web.Request") -> "web.Response":
        """JSON API: list recent responses."""
        self._require_auth(request)
        manifest = self._pages_dir / "_manifest.jsonl"
        entries: list[dict] = []
        if manifest.exists():
            for line in manifest.read_text("utf-8").strip().split("\n"):
                if line.strip():
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        entries.reverse()
        limit = min(int(request.query.get("limit", "50")), 200)
        return web.json_response(entries[:limit])


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _append_text(path: Path, text: str) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(text)


# ---------------------------------------------------------------------------
# HTML templates (inline)
# ---------------------------------------------------------------------------

_LOGIN_PAGE = """\
<!DOCTYPE html>
<html lang="ko">
<head><meta charset="utf-8"><title>Bridge Logs</title>
<style>
  body {{ background:#1e1e2e; color:#cdd6f4; font-family:sans-serif;
         display:flex; align-items:center; justify-content:center; height:100vh; }}
  .card {{ background:#313244; padding:2rem 3rem; border-radius:12px; text-align:center; }}
  a {{ background:#5865F2; color:#fff; padding:0.8rem 2rem; border-radius:8px;
       text-decoration:none; font-weight:600; display:inline-block; margin-top:1rem; }}
  a:hover {{ background:#4752c4; }}
</style></head>
<body><div class="card">
  <h2>Bridge Logs</h2>
  <p>Discord 계정으로 로그인하세요</p>
  <a href="/auth/login">Discord로 로그인</a>
</div></body></html>
"""

_INDEX_PAGE = """\
<!DOCTYPE html>
<html lang="ko">
<head><meta charset="utf-8"><title>Bridge Logs</title>
<style>
  body {{ background:#1e1e2e; color:#cdd6f4; font-family:sans-serif; padding:2rem; max-width:1100px; margin:0 auto; }}
  h1 {{ color:#cba6f7; }}
  .user {{ float:right; color:#6c7086; }}
  .user a {{ color:#89b4fa; text-decoration:none; }}
  table {{ width:100%; border-collapse:collapse; margin-top:1rem; }}
  th {{ text-align:left; color:#6c7086; border-bottom:1px solid #45475a; padding:0.5rem; }}
  td {{ padding:0.5rem; border-bottom:1px solid #313244; }}
  td a {{ color:#89b4fa; text-decoration:none; }}
  td a:hover {{ text-decoration:underline; }}
</style></head>
<body>
<div class="user">{username} · <a href="/auth/logout">로그아웃</a></div>
<h1>Bridge Logs</h1>
<p><a href="/terminal">Live Terminal 보기</a></p>
<table>
<tr><th>Persona</th><th>Provider</th><th>Opening</th><th>Chars</th><th>Time</th></tr>
{rows}
</table>
</body></html>
"""

_DENIED_PAGE = """\
<!DOCTYPE html>
<html lang="ko">
<head><meta charset="utf-8"><title>Access Denied</title>
<style>
  body { background:#1e1e2e; color:#cdd6f4; font-family:sans-serif;
         display:flex; align-items:center; justify-content:center; height:100vh; }
  .card { background:#313244; padding:2rem 3rem; border-radius:12px; text-align:center; }
  .card h2 { color:#f38ba8; }
</style></head>
<body><div class="card">
  <h2>접근 거부</h2>
  <p>이 서버의 멤버만 로그를 열람할 수 있습니다.</p>
</div></body></html>
"""

_TERMINAL_INDEX_PAGE = """\
<!DOCTYPE html>
<html lang="ko">
<head><meta charset="utf-8"><title>Live Terminal</title>
<style>
  body {{ background:#11111b; color:#cdd6f4; font-family:sans-serif; padding:2rem; max-width:1200px; margin:0 auto; }}
  h1 {{ color:#94e2d5; }}
  .user {{ float:right; color:#6c7086; }}
  .user a {{ color:#89b4fa; text-decoration:none; }}
  a {{ color:#89b4fa; text-decoration:none; }}
  a:hover {{ text-decoration:underline; }}
  code {{ color:#f9e2af; }}
  table {{ width:100%; border-collapse:collapse; margin-top:1rem; }}
  th {{ text-align:left; color:#6c7086; border-bottom:1px solid #45475a; padding:0.6rem; }}
  td {{ padding:0.6rem; border-bottom:1px solid #313244; }}
</style></head>
<body>
<div class="user">{username} · <a href="/auth/logout">로그아웃</a></div>
<h1>Live Terminal</h1>
<p><a href="/">응답 로그로 돌아가기</a></p>
<table>
<tr><th>Persona</th><th>Provider</th><th>Channel</th><th>Workspace</th><th>State</th><th>Size</th><th>Session</th></tr>
{rows}
</table>
</body></html>
"""

_TERMINAL_PAGE = """\
<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <title>{title}</title>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@xterm/xterm@5.5.0/css/xterm.min.css">
  <style>
    body {{
      margin: 0;
      background: #11111b;
      color: #cdd6f4;
      font-family: sans-serif;
      display: flex;
      flex-direction: column;
      min-height: 100vh;
    }}
    .topbar {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 0.9rem 1.2rem;
      border-bottom: 1px solid #313244;
      background: rgba(17,17,27,0.96);
    }}
    .meta {{
      display: flex;
      gap: 0.8rem;
      align-items: center;
      flex-wrap: wrap;
    }}
    .badge {{
      border-radius: 999px;
      padding: 0.2rem 0.7rem;
      font-size: 0.88rem;
      background: #313244;
      color: #cdd6f4;
    }}
    .badge.ready {{ background: #a6e3a1; color: #11111b; }}
    .badge.busy {{ background: #f9e2af; color: #11111b; }}
    .badge.dead {{ background: #f38ba8; color: #11111b; }}
    .badge.starting {{ background: #89b4fa; color: #11111b; }}
    a {{ color:#89b4fa; text-decoration:none; }}
    #terminal {{
      flex: 1;
      padding: 1rem;
    }}
  </style>
</head>
<body>
  <div class="topbar">
    <div class="meta">
      <strong>{title}</strong>
      <span id="status" class="badge">connecting</span>
      <span class="badge">{cols}x{rows}</span>
      <code>{session_id}</code>
    </div>
    <a href="/terminal">세션 목록</a>
  </div>
  <div id="terminal"></div>
  <script src="https://cdn.jsdelivr.net/npm/@xterm/xterm@5.5.0/lib/xterm.min.js"></script>
  <script>
    const statusEl = document.getElementById('status');
    const term = new Terminal({{
      cols: {cols},
      rows: {rows},
      convertEol: true,
      cursorBlink: false,
      disableStdin: true,
      theme: {{
        background: '#11111b',
        foreground: '#cdd6f4',
        cursor: '#f5e0dc',
        black: '#45475a',
        red: '#f38ba8',
        green: '#a6e3a1',
        yellow: '#f9e2af',
        blue: '#89b4fa',
        magenta: '#f5c2e7',
        cyan: '#94e2d5',
        white: '#bac2de',
        brightBlack: '#585b70',
        brightRed: '#f38ba8',
        brightGreen: '#a6e3a1',
        brightYellow: '#f9e2af',
        brightBlue: '#89b4fa',
        brightMagenta: '#f5c2e7',
        brightCyan: '#94e2d5',
        brightWhite: '#a6adc8'
      }}
    }});
    term.open(document.getElementById('terminal'));

    function setStatus(state, alive) {{
      const safeState = state || 'unknown';
      statusEl.textContent = alive === false ? `${{safeState}} · dead` : safeState;
      statusEl.className = 'badge ' + safeState;
    }}

    function connect() {{
      const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
      const ws = new WebSocket(`${{scheme}}://${{location.host}}/ws/terminal/{session_id}`);

      ws.onopen = () => setStatus('connecting', true);
      ws.onmessage = (event) => {{
        const msg = JSON.parse(event.data);
        if (msg.type === 'init') {{
          term.reset();
          if (msg.snapshot) {{
            term.write(msg.snapshot.replace(/\\n/g, '\\r\\n'));
          }}
          setStatus(msg.state, msg.alive);
          return;
        }}
        if (msg.type === 'data') {{
          term.write(msg.data);
          return;
        }}
        if (msg.type === 'state') {{
          setStatus(msg.state, msg.alive);
        }}
      }};
      ws.onclose = () => {{
        setStatus('disconnected', false);
        setTimeout(connect, 3000);
      }};
      ws.onerror = () => ws.close();
    }}

    connect();
  </script>
</body>
</html>
"""
