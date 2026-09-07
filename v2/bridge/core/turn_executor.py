from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import asdict
from pathlib import Path
from typing import Callable

from ..models import PersonaConfig
from ..pty.handler import PtySession, PtyState, SessionInterrupted
from ..providers import ProviderAdapter
from ..transport.base import IncomingMessage, OutputSink
from .channel_manager import ChannelManager, ChannelMessage
from .context_builder import ContextBuilder
from .log_store import LogStore

LOGGER = logging.getLogger(__name__)

# Patterns that identify leaked context prefix in extracted response
_CONTEXT_HEADER_RE = re.compile(r"^\[최근 대화\]\s*$")
_CONTEXT_MSG_RE = re.compile(r"^\[msg:\S+\]\s")
_SEPARATOR_RE = re.compile(r"^---\s*$")
_PIPE_REF_RE = re.compile(r"^\[메시지 참조")
_BULLET_RE = re.compile(r"^● ")

# Tool block patterns: ● ToolName(args) ... ⎿ result  (Claude)
_TOOL_MARKER_RE = re.compile(r"^●\s+([A-Za-z][\w ]*?)\((.+)\)\s*$")
_TOOL_RESULT_RE = re.compile(r"^\s+⎿\s+(.+)$")
# Codex tool patterns: • Running/Ran command ... └ result
_CODEX_RAN_RE = re.compile(r"^(?:•\s+)?Ran\s+(.+)$")
_CODEX_RUNNING_RE = re.compile(r"^(?:•\s+)?Running\s+(.+)$")
_CODEX_RESULT_RE = re.compile(r"^└\s+(.+)$")


def _collapse_tool_blocks(text: str) -> str:
    """Collapse tool use blocks into single summary lines.

    Claude format:
        ● Write(C:\\path\\file.html)
          ...content...
          ⎿  Wrote 1310 lines to C:\\path\\file.html
        → 🔧 Write(file.html) → Wrote 1310 lines

    Codex format:
        • Running git status --short --branch
        • Ran git status --short --branch
        └ fatal: not a git repository
        → 🔧 Ran git status → fatal: not a git repository
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
            # Shorten long commands
            if len(cmd) > 60:
                cmd = cmd[:57] + "..."
            tool_result = ""
            j = i + 1
            while j < len(lines):
                res = _CODEX_RESULT_RE.match(lines[j])
                if res:
                    tool_result = res.group(1).strip()
                    j += 1
                    # Skip indented continuation lines
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


def _compress_blank_lines(text: str) -> str:
    """Collapse 2+ consecutive blank lines into a single blank line."""
    return re.sub(r"\n{3,}", "\n\n", text)


def _strip_context_echo(response: str) -> str:
    """Strip context prefix + user input echo that leaked from PTY screen.

    The PTY screen may contain (after screen_parser's basic filtering):
        [최근 대화]                         ← context header
        [msg:123] opening ··· closing       ← context history
        ---                                 ← separator
        {user's original message text}      ← user input echo
        ● {Claude's actual response}        ← response start (● marker)

    This function strips everything before the actual response.
    """
    lines = response.split("\n")

    # Step 0: Strip [메시지 참조] blocks (pipe injection echo).
    # Format on PTY screen: [메시지 참조]\n{injected content}\n---\n{user text}
    pipe_start = -1
    for i, line in enumerate(lines):
        s = line.strip()
        if _PIPE_REF_RE.match(s):
            pipe_start = i
        elif _SEPARATOR_RE.match(s) and pipe_start >= 0:
            # Strip everything from [메시지 참조] through ---
            remaining = lines[i + 1:]
            return _strip_context_echo("\n".join(remaining))

    # Step 1: Find ● marker — the definitive start of Claude's response.
    # Everything before it is context echo / user input echo.
    #
    # Claude 2.x emits a *short* single-line thinking preamble marked with ●
    # before tool use, then the actual response also marked with ●. If we see
    # that pattern (short ● + blank lines + another ●), treat the first ● as
    # thinking and start from the second.
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
            # Tool call marker (● Bash(...), ● Write(...), etc.)
            # Keep ● so _collapse_tool_blocks can process it.
            result = "\n".join(lines[chosen:]).strip()
        else:
            lines[chosen] = _BULLET_RE.sub("", chosen_line)
            result = "\n".join(lines[chosen:]).strip()
        if result:
            return result

    # Step 2: No ● marker found — fall back to stripping known patterns.
    # Find the last "---" separator and take everything after it.
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

    # Step 3: Strip any leading [최근 대화] / [msg:] lines without separator
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


# ------------------------------------------------------------------
# Phase 2: CLI prompt detection (experimental)
# ------------------------------------------------------------------

_MAX_INTERACTIVE_ROUNDS = 5  # safety limit for multi-round loops

# PTY key codes for responding to CLI permission prompts (TUI selection)
_PERMISSION_KEYS: dict[str, str] = {
    "Yes": "\r",             # Enter — accept default "1. Yes"
    "Always": "\x1b[B\r",   # Down arrow + Enter — "2. Yes, allow all"
    "No": "\x1b",            # Escape — cancel
}


def _detect_prompt_raw(lines: list[str]) -> str | None:
    """Detect CLI permission prompt from *raw* PTY screen lines.

    Works on unprocessed screen output so TUI elements (❯, shift+tab, etc.)
    are still present.  Returns "permission" or None.

    Actual prompt format observed:
        Do you want to create test_perm_check.txt?
        ❯ 1. Yes
          2. Yes, allow all edits during this session (shift+tab)
          3. No
        Esc to cancel · Tab to amend
    """
    tail = " ".join(l.strip().lower() for l in lines[-15:])
    # Very specific: "Esc to cancel" is unique to the CLI permission UI
    if "esc to cancel" in tail:
        return "permission"
    # Fallback: numbered options pattern
    if "do you want to" in tail and ("1. yes" in tail or "3. no" in tail):
        return "permission"
    return None


class TurnExecutor:
    """Orchestrates a single turn: send → stream → wait → parse → log → finalize."""

    def __init__(
        self,
        log_store: LogStore,
        context_builder: ContextBuilder,
        channel_mgr: ChannelManager,
        on_turn_complete: "Callable[[int], None] | None" = None,
        bot_id: str = "",
        web_publisher: "Any | None" = None,
    ) -> None:
        self._log_store = log_store
        self._context_builder = context_builder
        self._cm = channel_mgr
        self._on_turn_complete = on_turn_complete
        self._bot_id = bot_id
        self._web_publisher = web_publisher

    async def execute(
        self,
        session: PtySession,
        msg: IncomingMessage,
        sink: OutputSink,
        persona: PersonaConfig,
        adapter: ProviderAdapter,
        save_user_log: bool = True,
    ) -> None:
        """Execute a full turn for the given message.

        Supports multi-round interaction: if the CLI outputs a permission
        or question prompt, delegates to the sink for user input and
        continues the loop.
        """
        await sink.begin(f"Processing message from {msg.author_name}")

        debug_payload: dict = {
            "status": "starting",
            "message_id": msg.message_id,
            "visible_message_id": msg.visible_message_id or msg.message_id,
            "server_id": msg.server_id,
            "channel_id": msg.channel_id,
            "persona_id": msg.persona_id,
            "provider_id": msg.provider_id,
            "author_id": msg.author_id,
            "author_name": msg.author_name,
            "workspace": msg.workspace,
            "trigger_reason": msg.trigger_reason,
            "reply_to_msg_id": msg.reply_to_msg_id,
            "attachment_paths": list(msg.attachment_paths),
            "raw_user_text": msg.text,
        }

        def _save_debug() -> None:
            try:
                self._log_store.save_turn_debug(
                    msg.server_id, msg.channel_id, msg.message_id, debug_payload,
                )
            except Exception:
                LOGGER.exception("Failed to save turn debug log: %s", msg.message_id)

        try:
            # Session should already be alive (SessionRegistry handles lifecycle).
            if session.state != PtyState.READY:
                debug_payload["status"] = "failed"
                debug_payload["error"] = (
                    f"PTY not ready (state={session.state.value})"
                )
                _save_debug()
                await sink.fail(
                    f"PTY not ready (state={session.state.value}). "
                    "다음 메시지에서 자동으로 재시작됩니다."
                )
                return

            # Build context-enriched message (skip self + current msg + already-seen)
            provider_state = self._cm.get_provider_state(
                msg.server_id, msg.channel_id, persona.persona_id, msg.provider_id,
            )
            last_seen = provider_state.last_seen_msg_id

            full_text = self._context_builder.build_context(
                msg.server_id, msg.channel_id, msg.text,
                self_persona_id=persona.persona_id,
                skip_message_id=msg.message_id,
                last_seen_msg_id=last_seen,
            )
            debug_payload["input_text"] = full_text
            _save_debug()

            # Send to PTY
            await session.send_message(
                full_text,
                inputs=adapter.build_message_inputs(full_text),
            )

            # --- Multi-round loop ---
            response = ""
            resent_after_empty = False
            for _round in range(_MAX_INTERACTIVE_ROUNDS):
                # Streaming loop — read screen periodically and push updates
                response_done = asyncio.Event()

                async def _stream() -> None:
                    while not response_done.is_set():
                        lines = session.get_screen_lines()
                        current = adapter.extract_response(lines)
                        if current:
                            await sink.stream_update(current)
                        await asyncio.sleep(persona.message_edit_interval)

                stream_task = asyncio.create_task(_stream())
                try:
                    await session.wait_response(
                        timeout=persona.response_timeout,
                        idle_seconds=persona.idle_seconds,
                    )
                finally:
                    response_done.set()
                    stream_task.cancel()
                    try:
                        await stream_task
                    except asyncio.CancelledError:
                        pass

                # Phase 2: detect interactive prompts from RAW screen
                # (before screen_parser strips TUI elements like ❯, shift+tab)
                raw_lines = session.get_screen_lines()
                prompt = adapter.detect_interaction(raw_lines)

                if prompt and prompt.choices and hasattr(sink, "send_choices"):
                    try:
                        choice = await sink.send_choices(
                            prompt.text or "(interactive prompt)",
                            list(prompt.choices),
                        )
                        key = adapter.encode_interaction_reply(prompt, choice)
                        LOGGER.info(
                            "Interactive prompt answered: provider=%s kind=%s choice=%s",
                            msg.provider_id, prompt.kind, choice,
                        )
                        # Route interactive replies through the normal PTY
                        # message path so they share the same state handling
                        # and screen reset semantics as regular turns.
                        await session.send_message(
                            "",
                            inputs=[key],
                            inter_input_delay=0.0,
                        )
                        continue  # next round
                    except Exception:
                        LOGGER.warning("send_choices failed, treating as normal response")
                elif prompt and hasattr(sink, "ask_user"):
                    try:
                        answer = await sink.ask_user(prompt.text or "응답을 입력하세요.")
                        key = adapter.encode_interaction_reply(prompt, answer)
                        await session.send_message(
                            "",
                            inputs=[key],
                            inter_input_delay=0.0,
                        )
                        continue
                    except Exception:
                        LOGGER.warning("ask_user failed, treating as normal response")

                # Normal extraction
                response = adapter.extract_response(raw_lines)

                # Empty response retry: idle detection can trigger during the
                # API latency gap right after the input echo renders.
                # Observed: ~3.3s gap between input echo and actual response,
                # which exceeds the default IDLE_SECONDS (3.0).
                if not response and _round == 0 and session.state != PtyState.DEAD:
                    LOGGER.info("Empty response — retrying after idle delay (premature idle?)")
                    await asyncio.sleep(persona.idle_seconds + 2.0)
                    raw_lines = session.get_screen_lines()
                    response = adapter.extract_response(raw_lines)
                    if (
                        not response
                        and not resent_after_empty
                        and session.state == PtyState.READY
                    ):
                        LOGGER.warning(
                            "Empty response persisted after idle retry; resending turn once: provider=%s msg=%s",
                            msg.provider_id,
                            msg.message_id,
                        )
                        resent_after_empty = True
                        debug_payload["empty_response_resend"] = True
                        _save_debug()
                        await session.send_message(
                            full_text,
                            inputs=adapter.build_message_inputs(full_text),
                        )
                        continue

                pending_checks = 0
                while adapter.has_pending_output(raw_lines) and pending_checks < 4:
                    LOGGER.info(
                        "Provider output still pending after idle; waiting for stabilization"
                    )
                    await asyncio.sleep(1.0)
                    raw_lines = session.get_screen_lines()
                    newer_response = adapter.extract_response(raw_lines)
                    if newer_response:
                        response = newer_response
                    pending_checks += 1

                if response and adapter.is_control_output(response):
                    LOGGER.warning(
                        "Control-only output detected; ignoring as final response: provider=%s msg=%s",
                        msg.provider_id, msg.message_id,
                    )
                    await asyncio.sleep(persona.idle_seconds + 2.0)
                    raw_lines = session.get_screen_lines()
                    response = adapter.extract_response(raw_lines)
                    if response and adapter.is_control_output(response):
                        response = ""

                if not response:
                    debug_payload["status"] = "failed"
                    debug_payload["error"] = "Empty response"
                    _save_debug()
                    await sink.fail("Empty response")
                    return

                # Normal response — exit loop
                break

            # Extract metadata and finalize (returns response message ID)
            meta = adapter.extract_metadata(response)

            # Collapse tool blocks + compress blank lines for display
            display_response = _collapse_tool_blocks(response)
            display_response = _compress_blank_lines(display_response)

            actual_response_id = (
                sink.status_message_id or msg.message_id
            )

            response_msg_id = await sink.finalize(display_response, meta)

            # Update actual_response_id from finalize result
            actual_response_id = response_msg_id or actual_response_id

            # Save FULL response to logs (uncollapsed)
            self._log_store.save_response(
                server_id=msg.server_id,
                channel_id=msg.channel_id,
                author_id=msg.author_id,
                persona_id=persona.persona_id,
                provider_id=msg.provider_id,
                bot_id=self._bot_id,
                response_message_id=actual_response_id,
                trigger_message_id=msg.message_id,
                response_text=response,
                save_user_log=save_user_log,
            )

            # Enqueue web page generation (non-blocking background push)
            if self._web_publisher:
                try:
                    from ..models import SessionKey as _SK
                    sk = _SK(
                        persona_id=persona.persona_id,
                        provider_id=msg.provider_id,
                        channel_id=msg.channel_id,
                        workspace=msg.workspace,
                    )
                    await self._web_publisher.enqueue(
                        msg_id=actual_response_id,
                        persona=persona,
                        provider_id=msg.provider_id,
                        response_raw=response,
                        meta=meta,
                        session_key=sk.session_id,
                    )
                except Exception:
                    LOGGER.debug("WebPublisher enqueue failed", exc_info=True)

            # Save continuation chunk files (B, C, ...) with pointer to A
            if hasattr(sink, "continuation_chunks"):
                for chunk_id, chunk_text in sink.continuation_chunks:
                    self._log_store.save_continuation(
                        server_id=msg.server_id,
                        channel_id=msg.channel_id,
                        chunk_message_id=chunk_id,
                        first_message_id=actual_response_id,
                        chunk_text=chunk_text,
                    )

            # Append COLLAPSED response to context stream
            self._cm.append_message(
                msg.server_id, msg.channel_id,
                ChannelMessage(
                    message_id=actual_response_id,
                    author_name=persona.persona_id,
                    text=display_response,
                    author_id=self._bot_id,
                    reply_to=msg.message_id,
                    provider_id=msg.provider_id,
                ),
            )

            runtime_dir = None
            if session.env.get("CODEX_HOME"):
                runtime_dir = Path(session.env["CODEX_HOME"])
            cli_sid = adapter.resolve_resume_id(
                lines=session.get_screen_lines(),
                workspace=msg.workspace,
                runtime_dir=runtime_dir,
            )
            if cli_sid and provider_state.cli_session_id != cli_sid:
                provider_state.cli_session_id = cli_sid

            # Mark this user message as seen only after the turn completed successfully.
            provider_state.last_seen_msg_id = msg.message_id
            self._cm._save_state(msg.server_id, msg.channel_id)

            debug_payload["status"] = "completed"
            debug_payload["response_message_id"] = actual_response_id
            debug_payload["continuation_message_ids"] = [
                chunk_id for chunk_id, _ in getattr(sink, "continuation_chunks", [])
            ]
            debug_payload["response_text"] = response
            debug_payload["response_meta"] = asdict(meta)
            debug_payload["cli_session_id"] = provider_state.cli_session_id
            _save_debug()

            # Notify pipe waiters that this channel's turn is done
            if self._on_turn_complete:
                self._on_turn_complete(msg.channel_id)

        except SessionInterrupted:
            debug_payload["status"] = "failed"
            debug_payload["error"] = "Interrupted"
            _save_debug()
            LOGGER.info("Turn interrupted by user")
            await sink.fail("Interrupted")
        except TimeoutError as exc:
            debug_payload["status"] = "failed"
            debug_payload["error"] = str(exc)
            _save_debug()
            await sink.fail(str(exc))
        except Exception as exc:
            debug_payload["status"] = "failed"
            debug_payload["error"] = str(exc)
            _save_debug()
            LOGGER.exception("Error executing turn")
            await sink.fail(f"Error: {exc}")
