from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import threading
import time
from pathlib import Path

# Allow running as `python v2/main.py` from project root
_v2_dir = str(Path(__file__).resolve().parent)
if _v2_dir not in sys.path:
    sys.path.insert(0, _v2_dir)

from bridge.config import load_bridge_settings
from bridge.core import BridgeCore
from bridge.transport.discord_transport import DiscordTransport


def setup_logging(level: str = "INFO", *, monitor: bool = False) -> None:
    resolved_level = getattr(logging, level.upper(), logging.INFO)
    handlers: list[logging.Handler] = [
        logging.FileHandler("v2_bridge.log", encoding="utf-8"),
    ]
    if not monitor:
        handlers.insert(0, logging.StreamHandler(sys.stdout))
    logging.basicConfig(
        level=resolved_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=handlers,
    )
    logging.getLogger("bridge").setLevel(resolved_level)


def _keyboard_thread(
    input_queue: asyncio.Queue[str],
    loop: asyncio.AbstractEventLoop,
    stop_flag: threading.Event,
) -> None:
    import msvcrt

    while not stop_flag.is_set():
        if msvcrt.kbhit():
            ch = msvcrt.getwch()
            loop.call_soon_threadsafe(input_queue.put_nowait, ch)
        time.sleep(0.05)


def _clear_console() -> None:
    os.system("cls")


def _resolve_monitor_session_id(
    sessions: list[dict[str, object]],
    token: str,
) -> tuple[str | None, str | None]:
    if not token:
        return None, "세션 id를 입력해 주세요."
    token = token.strip()
    exact = next(
        (item["session_id"] for item in sessions if item["session_id"] == token),
        None,
    )
    if exact:
        return str(exact), None
    partial = [
        str(item["session_id"])
        for item in sessions
        if str(item["session_id"]).startswith(token)
    ]
    if len(partial) == 1:
        return partial[0], None
    if len(partial) > 1:
        return None, f"prefix가 모호합니다: {token}"
    return None, f"세션을 찾지 못했습니다: {token}"


def _apply_monitor_command(
    command: str,
    sessions: list[dict[str, object]],
    selected_session_id: str | None,
) -> tuple[str | None, bool, str]:
    text = command.strip()
    if not text:
        return selected_session_id, False, ""
    if not text.startswith("/"):
        return selected_session_id, False, "명령은 /monitor, /list, /quit 중 하나를 사용하세요."

    parts = text.split(maxsplit=1)
    name = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    if name in {"/quit", "/exit"}:
        return selected_session_id, True, ""
    if name in {"/list", "/back"}:
        return None, False, ""
    if name == "/help":
        return (
            selected_session_id,
            False,
            "명령: /monitor <session_id|prefix>, /list, /quit",
        )
    if name == "/monitor":
        resolved, error = _resolve_monitor_session_id(sessions, arg)
        if error:
            return selected_session_id, False, error
        return resolved, False, ""

    return selected_session_id, False, f"알 수 없는 명령입니다: {parts[0]}"


async def run_monitor(
    core: BridgeCore,
    loop: asyncio.AbstractEventLoop,
    transport_task: asyncio.Task,
    initial_session_id: str | None = None,
) -> None:
    input_queue: asyncio.Queue[str] = asyncio.Queue()
    stop_flag = threading.Event()
    keyboard = threading.Thread(
        target=_keyboard_thread,
        args=(input_queue, loop, stop_flag),
        daemon=True,
    )
    keyboard.start()

    selected_session_id: str | None = initial_session_id
    command_buffer = ""
    status_message = ""

    try:
        while True:
            sessions = core.get_session_list()
            session_ids = {item["session_id"] for item in sessions}
            if selected_session_id and selected_session_id not in session_ids:
                selected_session_id = None
            if initial_session_id:
                resolved, error = _resolve_monitor_session_id(sessions, initial_session_id)
                if resolved:
                    selected_session_id = resolved
                    initial_session_id = None
                elif error:
                    status_message = error
                    initial_session_id = None

            while True:
                try:
                    ch = input_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break

                lowered = ch.lower()
                if ch in {"\r", "\n"} and command_buffer:
                    selected_session_id, should_exit, status_message = _apply_monitor_command(
                        command_buffer,
                        sessions,
                        selected_session_id,
                    )
                    command_buffer = ""
                    if should_exit:
                        return
                    continue
                if ch in {"\x08", "\u007f"} and command_buffer:
                    command_buffer = command_buffer[:-1]
                    continue
                if ch == "\x1b":
                    if command_buffer:
                        command_buffer = ""
                        status_message = "입력을 취소했습니다."
                    continue
                if command_buffer:
                    if ch.isprintable():
                        command_buffer += ch
                    continue
                if ch == "/":
                    command_buffer = "/"
                    status_message = ""
                    continue
                if selected_session_id is None:
                    if lowered == "x":
                        return
                    if ch.isdigit():
                        index = int(ch) - 1
                        if 0 <= index < len(sessions):
                            selected_session_id = sessions[index]["session_id"]
                            status_message = ""
                else:
                    if lowered == "q":
                        selected_session_id = None
                        status_message = ""
                    elif lowered == "x":
                        return
                    elif lowered == "n" and sessions:
                        current_index = next(
                            (i for i, item in enumerate(sessions)
                             if item["session_id"] == selected_session_id),
                            0,
                        )
                        selected_session_id = sessions[(current_index + 1) % len(sessions)]["session_id"]
                        status_message = ""
                    elif lowered == "p" and sessions:
                        current_index = next(
                            (i for i, item in enumerate(sessions)
                             if item["session_id"] == selected_session_id),
                            0,
                        )
                        selected_session_id = sessions[(current_index - 1) % len(sessions)]["session_id"]
                        status_message = ""
                    elif lowered == "l" and selected_session_id:
                        if core.is_session_locked(selected_session_id):
                            core.unlock_session(selected_session_id)
                            status_message = "\U0001f513 세션 잠금 해제"
                        else:
                            core.lock_session(selected_session_id)
                            status_message = "\U0001f512 세션 잠금 (Discord 입력 차단)"

            _clear_console()
            if selected_session_id is None:
                print("Bridge Monitor")
                print("1-9: 세션 보기 | /monitor <session_id> | x: 종료")
                print("Enter: 명령 실행 | Esc: 입력 취소")
                print("")
                if not sessions:
                    print("활성 세션이 없습니다.")
                else:
                    for idx, item in enumerate(sessions[:9], start=1):
                        workspace_name = Path(item["workspace"]).name or item["workspace"]
                        print(
                            f"{idx}. {item['display_name']} | {item['provider_id']} | "
                            f"#{item['channel_id']} | {item['state']} | {workspace_name}"
                        )
            else:
                resolved = core.get_session_by_id(selected_session_id)
                if not resolved:
                    selected_session_id = None
                    continue
                key, session = resolved
                lines = session.get_display_lines()
                tail = lines[-max(10, min(len(lines), session.rows)):]
                lock_icon = "\U0001f512" if core.is_session_locked(selected_session_id) else "\U0001f513"
                print(
                    f"Bridge Monitor — {key.persona_id} | {key.provider_id} | "
                    f"#{key.channel_id} | {session.state.value} | {lock_icon}"
                )
                print("q: 목록 | n/p: 다음/이전 | l: 잠금 토글 | x: 종료")
                print("Enter: 명령 실행 | Esc: 입력 취소")
                print("-" * 100)
                print("\n".join(tail) if tail else "(no output yet)")

            if command_buffer:
                print("")
                print(f"command> {command_buffer}")
            if status_message:
                print("")
                print(status_message)

            if transport_task.done():
                exc = transport_task.exception()
                if exc:
                    print("")
                    print(f"transport stopped: {exc}")
                    return

            await asyncio.sleep(0.25)
    finally:
        stop_flag.set()


async def main(
    *,
    monitor: bool = False,
    monitor_session: str | None = None,
) -> None:
    settings = load_bridge_settings()
    setup_logging(settings.log_level, monitor=monitor)
    logger = logging.getLogger(__name__)
    transport_task: asyncio.Task | None = None

    logger.info(
        "Bridge starting: personas=%s, workspace=%s",
        settings.active_personas,
        settings.default_workspace,
    )

    loop = asyncio.get_running_loop()
    core = BridgeCore(settings, loop)

    logger.info("Bot will listen in all channels where it is mentioned")

    # Single transport handles all personas via webhooks
    transport = DiscordTransport(
        settings=settings,
        core=core,
        default_workspace=str(settings.default_workspace),
    )
    logger.info(
        "Created single transport for %d persona(s): %s",
        len(settings.active_personas),
        ", ".join(settings.active_personas),
    )

    # Start health monitor
    core.start_health_monitor()

    # Start transport
    try:
        if monitor:
            transport_task = asyncio.create_task(transport.start())
            await run_monitor(core, loop, transport_task, initial_session_id=monitor_session)
        else:
            await transport.start()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    finally:
        await core.stop()
        try:
            await transport.stop()
        except Exception:
            pass
        if transport_task and not transport_task.done():
            transport_task.cancel()
            try:
                await transport_task
            except asyncio.CancelledError:
                pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--monitor",
        action="store_true",
        help="Run a local read-only session monitor in the console.",
    )
    parser.add_argument(
        "--monitor-session",
        help="Open the monitor directly on a specific session id or unique prefix.",
    )
    args = parser.parse_args()
    asyncio.run(main(
        monitor=args.monitor or bool(args.monitor_session),
        monitor_session=args.monitor_session,
    ))
