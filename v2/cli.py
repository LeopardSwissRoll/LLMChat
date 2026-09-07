"""v2 PTY Bridge — local CLI test harness.

  python v2/cli.py                  # raw passthrough — TUI 그대로 터미널 출력
  python v2/cli.py --parsed         # pyte 파싱 REPL — BridgeCore 경유
  python v2/cli.py --parsed "hello" # one-shot parsed
"""
from __future__ import annotations

import os
import signal
import sys
import threading
import time
from pathlib import Path

_v2_dir = str(Path(__file__).resolve().parent)
if _v2_dir not in sys.path:
    sys.path.insert(0, _v2_dir)


def _get_terminal_size() -> tuple[int, int]:
    """Return (rows, cols)."""
    try:
        cols, rows = os.get_terminal_size()
        return max(rows, 10), max(cols, 40)
    except OSError:
        return 30, 120


# Windows special key code → ANSI escape sequence
_WIN_KEY_MAP = {
    "H": "\x1b[A",   # Up
    "P": "\x1b[B",   # Down
    "M": "\x1b[C",   # Right
    "K": "\x1b[D",   # Left
    "G": "\x1b[H",   # Home
    "O": "\x1b[F",   # End
    "I": "\x1b[5~",  # Page Up
    "Q": "\x1b[6~",  # Page Down
    "S": "\x1b[3~",  # Delete
    "R": "\x1b[2~",  # Insert
}


def _enable_vt_processing() -> None:
    """Enable ANSI/VT escape processing on Windows console."""
    import ctypes
    kernel32 = ctypes.windll.kernel32
    # stdout: ENABLE_VIRTUAL_TERMINAL_PROCESSING
    h_out = kernel32.GetStdHandle(-11)
    mode = ctypes.c_ulong()
    kernel32.GetConsoleMode(h_out, ctypes.byref(mode))
    kernel32.SetConsoleMode(h_out, mode.value | 0x0004)


def run_raw(cwd: str) -> None:
    """Passthrough mode: PTY ↔ terminal, character-at-a-time."""
    import msvcrt
    from winpty import PtyProcess

    _enable_vt_processing()

    rows, cols = _get_terminal_size()
    proc = PtyProcess.spawn(
        "claude --verbose", cwd=cwd, dimensions=(rows, cols),
    )

    stop = threading.Event()
    last_size = (rows, cols)

    # -- Ctrl+C: single → forward \x03 to PTY, double (< 1s) → exit --
    last_sigint = [0.0]
    prev_handler = signal.getsignal(signal.SIGINT)

    def sigint_handler(_sig, _frame):
        now = time.monotonic()
        if now - last_sigint[0] < 1.0:
            stop.set()
            return
        last_sigint[0] = now
        try:
            proc.write("\x03")
        except Exception:
            pass

    signal.signal(signal.SIGINT, sigint_handler)

    # -- Reader thread: PTY → stdout + resize detection --
    def reader():
        nonlocal last_size
        while not stop.is_set():
            try:
                data = proc.read(4096)
                if data:
                    sys.stdout.write(data)
                    sys.stdout.flush()
                new_size = _get_terminal_size()
                if new_size != last_size:
                    last_size = new_size
                    try:
                        proc.setwinsize(*new_size)
                    except Exception:
                        pass
            except EOFError:
                break
            except Exception:
                if not stop.is_set():
                    time.sleep(0.05)
        stop.set()

    t = threading.Thread(target=reader, daemon=True)
    t.start()

    # -- Drain DA response garbage from stdin --
    # Claude CLI sends \e[c → terminal responds \e[?61;...c → appears in stdin.
    # Wait for TUI to render + DA roundtrip, then discard everything in buffer.
    drain_end = time.monotonic() + 2.0
    while time.monotonic() < drain_end:
        if msvcrt.kbhit():
            msvcrt.getwch()  # discard
        time.sleep(0.02)

    # -- stdin → PTY --
    try:
        while not stop.is_set() and proc.isalive():
            if not msvcrt.kbhit():
                time.sleep(0.01)
                continue

            ch = msvcrt.getwch()

            if ch in ("\r", "\n"):
                proc.write("\r")
            elif ch in ("\x00", "\xe0"):
                # Windows special key prefix → read actual key code
                key = msvcrt.getwch()
                ansi = _WIN_KEY_MAP.get(key, "")
                if ansi:
                    proc.write(ansi)
            elif ch == "\t":
                proc.write("\t")
            elif ch == "\x1b":
                proc.write("\x1b")
            else:
                proc.write(ch)
    except KeyboardInterrupt:
        pass
    finally:
        signal.signal(signal.SIGINT, prev_handler)
        stop.set()
        if proc.isalive():
            proc.write("/exit\r")
            time.sleep(1)
            if proc.isalive():
                proc.terminate()


# ------------------------------------------------------------------
# Parsed mode (BridgeCore path)
# ------------------------------------------------------------------

def run_parsed(cwd: str, message: str | None, timeout: float, dump: bool) -> None:
    import asyncio
    import logging
    from bridge.config import load_bridge_settings
    from bridge.core import BridgeCore
    from bridge.transport.base import IncomingMessage
    from bridge.transport.cli_transport import CliOutputSink

    settings = load_bridge_settings()
    configured_level = getattr(logging, settings.log_level.upper(), logging.INFO)
    resolved_level = logging.DEBUG if dump else configured_level
    logging.basicConfig(
        level=resolved_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stderr)],
    )
    logging.getLogger("bridge").setLevel(resolved_level)

    async def _run():
        loop = asyncio.get_running_loop()
        core = BridgeCore(settings, loop)

        # Use first active persona for CLI
        pid = settings.active_personas[0]
        provider_id = settings.default_provider
        workspace = cwd or str(settings.default_workspace)

        sink = CliOutputSink(verbose=dump)
        turn_counter = 0

        try:
            if message:
                turn_counter += 1
                msg = IncomingMessage(
                    message_id=f"cli-{turn_counter}",
                    server_id="0",
                    channel_id=0,
                    author_id="0",
                    author_name="cli-user",
                    text=message,
                    workspace=workspace,
                    persona_id=pid,
                    provider_id=provider_id,
                )
                await core.handle_message(msg, sink)
                # Wait for queue to drain
                await asyncio.sleep(0.5)
                await core.drain()
            else:
                print("REPL mode. Ctrl+C to quit.", file=sys.stderr)
                while True:
                    try:
                        text = await loop.run_in_executor(
                            None, lambda: input("\n> ")
                        )
                    except EOFError:
                        break
                    text = text.strip()
                    if not text or text.lower() in ("/exit", "quit"):
                        break

                    turn_counter += 1
                    msg = IncomingMessage(
                        message_id=f"cli-{turn_counter}",
                        server_id="0",
                        channel_id=0,
                        author_id="0",
                        author_name="cli-user",
                        text=text,
                        workspace=workspace,
                        persona_id=pid,
                        provider_id=provider_id,
                    )
                    await core.handle_message(msg, sink)
                    # Wait for the turn to complete
                    await core.drain()
        except KeyboardInterrupt:
            print("\nInterrupted.", file=sys.stderr)
        finally:
            await core.stop()

    asyncio.run(_run())


def _dump_lines(lines: list[str]) -> None:
    print("\n--- RAW SCREEN DUMP ---", file=sys.stderr)
    for i, line in enumerate(lines):
        if line.rstrip():
            print(f"  {i:4d} | {line}", file=sys.stderr)
    print("--- END DUMP ---\n", file=sys.stderr)


def _decode_probe_escapes(text: str) -> str:
    result: list[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch != "\\" or i + 1 >= len(text):
            result.append(ch)
            i += 1
            continue

        nxt = text[i + 1]
        if nxt == "r":
            result.append("\r")
            i += 2
            continue
        if nxt == "n":
            result.append("\n")
            i += 2
            continue
        if nxt == "t":
            result.append("\t")
            i += 2
            continue
        if nxt == "e":
            result.append("\x1b")
            i += 2
            continue
        if nxt == "\\":
            result.append("\\")
            i += 2
            continue
        if nxt == "x" and i + 3 < len(text):
            hex_part = text[i + 2:i + 4]
            try:
                result.append(chr(int(hex_part, 16)))
                i += 4
                continue
            except ValueError:
                pass
        if nxt == "u" and i + 5 < len(text):
            hex_part = text[i + 2:i + 6]
            try:
                result.append(chr(int(hex_part, 16)))
                i += 6
                continue
            except ValueError:
                pass

        # Unknown escape: keep the backslash literally.
        result.append("\\")
        i += 1

    return "".join(result)


def run_startup_probe(
    *,
    server_id: str,
    channel_id: int,
    persona_id: str,
    provider_id: str,
    workspace: str | None,
    auto_wait: float,
) -> None:
    import asyncio
    import logging
    import re

    from bridge.config import load_bridge_settings
    from bridge.core import BridgeCore
    from bridge.models import SessionKey
    from bridge.pty.handler import PtySession

    ansi_re = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")
    osc_re = re.compile(r"\x1b\][^\x1b\x07]*(?:\x1b\\|\x07)")

    settings = load_bridge_settings()
    configured_level = getattr(logging, settings.log_level.upper(), logging.INFO)
    logging.basicConfig(
        level=configured_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stderr)],
    )
    logging.getLogger("bridge").setLevel(configured_level)

    def _clean_raw(text: str) -> str:
        text = osc_re.sub("", text)
        text = ansi_re.sub("", text)
        return text.replace("\xa0", " ")

    async def _run() -> None:
        loop = asyncio.get_running_loop()
        core = BridgeCore(settings, loop)
        session: PtySession | None = None
        try:
            if persona_id not in settings.personas:
                raise ValueError(f"Unknown persona: {persona_id}")
            persona = settings.personas[persona_id]
            if provider_id not in persona.enabled_providers:
                raise ValueError(
                    f"Persona {persona_id} does not support provider {provider_id}",
                )

            registry = core._registry
            adapter = registry.get_provider_adapter(provider_id)
            provider_cfg = registry.get_provider_config(provider_id)
            workspace_path = workspace or str(settings.default_workspace)
            key = SessionKey(
                persona_id=persona_id,
                provider_id=provider_id,
                channel_id=channel_id,
                workspace=workspace_path,
            )
            protagonist = core._channel_mgr.get_protagonist(server_id, channel_id, persona_id)
            mode = core._channel_mgr.get_mode(server_id, channel_id, persona_id, provider_id)
            prompt = core._prompt_composer.compose(
                provider_id,
                persona,
                protagonist_id=protagonist,
                mode=mode,
            )
            prompt_file = (
                core._log_store.channel_dir(server_id, channel_id)
                / f".prompt_probe_{key.session_id}.md"
            )
            prompt_file.write_text(prompt, encoding="utf-8")

            dir_map = {
                "channel": lambda: core._log_store.channel_dir(server_id, channel_id),
                "users": lambda: core._log_store.users_dir(server_id),
                "persona": lambda: core._prompt_composer.get_persona_dir(persona),
            }
            add_dirs: list[Path] = []
            for dir_type in registry.get_mode_dirs(mode):
                resolver = dir_map.get(dir_type)
                if resolver:
                    add_dirs.append(resolver())

            runtime_dir = registry._provider_runtime_dir(key, server_id)
            provider_state = core._channel_mgr.get_provider_state(
                server_id, channel_id, persona_id, provider_id,
            )
            spawn_env = adapter.build_spawn_env(
                provider_cfg=provider_cfg,
                persona=persona,
                workspace=workspace_path,
                prompt_file=prompt_file,
                state=provider_state,
                runtime_dir=runtime_dir,
            )
            command = adapter.build_spawn_command(
                provider_cfg=provider_cfg,
                persona=persona,
                workspace=workspace_path,
                prompt_file=prompt_file,
                add_dirs=add_dirs,
                resume_id=None,
                state=provider_state,
                runtime_dir=runtime_dir,
            )

            print("=== STARTUP PROBE ===")
            print(f"persona   : {persona_id}")
            print(f"provider  : {provider_id}")
            print(f"server    : {server_id}")
            print(f"channel   : {channel_id}")
            print(f"workspace : {workspace_path}")
            print(f"prompt    : {prompt_file}")
            print(f"command   : {command}")

            session = PtySession(
                command=command,
                cwd=workspace_path,
                rows=persona.pty_rows,
                cols=persona.pty_cols,
                loop=loop,
                ready_detector=adapter.ready_detector,
                raw_ready_detector=adapter.raw_ready_detector,
                env=spawn_env,
            )
            session.spawn()

            async def show_status(*, dump_screen: bool = False, dump_raw: bool = False) -> None:
                assert session is not None
                raw_text = "".join(session.get_recent_raw_chunks())
                lines = session.get_screen_lines()
                trust_key = adapter.detect_trust_prompt(lines)
                detect_startup_prompt = getattr(adapter, "detect_startup_prompt", None)
                startup_key = None
                if callable(detect_startup_prompt):
                    try:
                        startup_key = detect_startup_prompt(raw_text)
                    except Exception:
                        startup_key = None
                print("")
                print(
                    f"state={session.state.value} "
                    f"alive={session.proc.isalive() if session.proc else False} "
                    f"screen_ready={adapter.ready_detector(lines)} "
                    f"raw_ready={adapter.raw_ready_detector(raw_text)} "
                    f"trust_key={trust_key!r} startup_key={startup_key!r}"
                )
                non_empty = [line for line in lines if line.strip()]
                tail = non_empty[-12:]
                if tail:
                    print("--- screen tail ---")
                    for line in tail:
                        print(line)
                else:
                    print("--- screen tail ---")
                    print("(empty)")
                if dump_raw:
                    cleaned = _clean_raw(raw_text)
                    print("--- raw tail ---")
                    print(cleaned[-2000:])
                if dump_screen:
                    _dump_lines(lines)

            await asyncio.sleep(max(0.0, auto_wait))
            await show_status(dump_raw=True)

            help_text = (
                "commands: status, dump, raw, repr, wait [sec], ready [sec], "
                "send <text>, sendraw <text>, enter, esc, ctrlc, auto, quit"
            )
            print(help_text)

            while True:
                try:
                    raw_cmd = await loop.run_in_executor(None, lambda: input("probe> "))
                except EOFError:
                    break
                cmd = raw_cmd.strip()
                if not cmd:
                    continue
                lowered = cmd.lower()
                if lowered in {"quit", "exit"}:
                    break
                if lowered == "status":
                    await show_status()
                    continue
                if lowered == "dump":
                    await show_status(dump_screen=True, dump_raw=True)
                    continue
                if lowered == "raw":
                    await show_status(dump_raw=True)
                    continue
                if lowered == "repr":
                    assert session is not None
                    lines = [line for line in session.get_screen_lines() if line.strip()]
                    print("--- screen repr tail ---")
                    for line in lines[-12:]:
                        print(repr(line))
                    continue
                if lowered.startswith("wait"):
                    parts = cmd.split(maxsplit=1)
                    seconds = float(parts[1]) if len(parts) > 1 else 2.0
                    await asyncio.sleep(max(0.0, seconds))
                    await show_status()
                    continue
                if lowered.startswith("ready"):
                    parts = cmd.split(maxsplit=1)
                    seconds = float(parts[1]) if len(parts) > 1 else 20.0
                    try:
                        assert session is not None
                        await session.wait_ready(timeout=seconds)
                        print("wait_ready: READY")
                    except Exception as exc:
                        print(f"wait_ready: {exc}")
                    await show_status()
                    continue
                if lowered == "enter":
                    assert session is not None
                    session.write_raw("\r")
                    await asyncio.sleep(0.5)
                    await show_status()
                    continue
                if lowered == "esc":
                    assert session is not None
                    session.write_raw("\x1b")
                    await asyncio.sleep(0.5)
                    await show_status()
                    continue
                if lowered == "ctrlc":
                    assert session is not None
                    session.write_raw("\x03")
                    await asyncio.sleep(0.5)
                    await show_status()
                    continue
                if lowered == "auto":
                    assert session is not None
                    raw_text = "".join(session.get_recent_raw_chunks())
                    lines = session.get_screen_lines()
                    key_to_send = adapter.detect_trust_prompt(lines)
                    detect_startup_prompt = getattr(adapter, "detect_startup_prompt", None)
                    if not key_to_send and callable(detect_startup_prompt):
                        try:
                            key_to_send = detect_startup_prompt(raw_text)
                        except Exception:
                            key_to_send = None
                    if not key_to_send:
                        print("auto: no detected startup/trust key")
                    else:
                        session.write_raw(key_to_send)
                        print(f"auto: sent {key_to_send!r}")
                    await asyncio.sleep(1.0)
                    await show_status()
                    continue
                if lowered.startswith("sendraw "):
                    assert session is not None
                    payload = _decode_probe_escapes(cmd[8:])
                    session.write_raw(payload)
                    await asyncio.sleep(0.5)
                    await show_status()
                    continue
                if lowered.startswith("send "):
                    assert session is not None
                    payload = _decode_probe_escapes(cmd[5:])
                    for ch in payload:
                        session.write_raw(ch)
                        await asyncio.sleep(0.03)
                    await asyncio.sleep(0.5)
                    await show_status()
                    continue
                print(help_text)
        finally:
            if session is not None:
                session.stop()
            await core.stop()

    asyncio.run(_run())


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="v2 PTY Bridge CLI tester")
    parser.add_argument("message", nargs="?", help="One-shot message (parsed mode)")
    parser.add_argument("--parsed", action="store_true", help="Parsed mode (pyte)")
    parser.add_argument("--dump", action="store_true", help="Raw screen dump (parsed)")
    parser.add_argument(
        "--probe-startup",
        action="store_true",
        help="Spawn one provider session with bridge settings and inspect startup manually",
    )
    parser.add_argument("--server-id", default="0", help="Server ID for startup probe")
    parser.add_argument("--channel-id", type=int, default=0, help="Channel ID for startup probe")
    parser.add_argument("--persona", default="", help="Persona ID for startup probe")
    parser.add_argument("--provider", default="claude", help="Provider ID for startup probe")
    parser.add_argument(
        "--wait",
        type=float,
        default=3.0,
        help="Initial seconds to wait before showing startup probe status",
    )
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument(
        "--cwd",
        default=os.getenv(
            "PLAYGROUND_DIR",
            str(Path(__file__).resolve().parent.parent / "Playground"),
        ),
    )
    args = parser.parse_args()

    if args.probe_startup:
        if not args.persona or not args.channel_id:
            parser.error("--probe-startup requires --persona and --channel-id")
        run_startup_probe(
            server_id=args.server_id,
            channel_id=args.channel_id,
            persona_id=args.persona,
            provider_id=args.provider,
            workspace=args.cwd,
            auto_wait=args.wait,
        )
    elif args.parsed or args.message:
        run_parsed(args.cwd, args.message, args.timeout, args.dump)
    else:
        run_raw(args.cwd)


if __name__ == "__main__":
    main()
