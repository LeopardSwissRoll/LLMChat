"""CLI entry point for the standalone Export terminal engine."""

from __future__ import annotations

import argparse
from pathlib import Path

from Export.bridge.console_session import ConsoleSession
from Export.vt.virtual_terminal import VirtualTerminal


def _default_shell() -> list[str]:
    return ["powershell.exe"]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m Export",
        description="Run the standalone Export virtual terminal bridge.",
    )
    parser.add_argument(
        "--cwd",
        default=str(Path.cwd()),
        help="Working directory for the child shell.",
    )
    parser.add_argument(
        "--cols",
        type=int,
        default=120,
        help="Initial terminal width before the first real console resize.",
    )
    parser.add_argument(
        "--rows",
        type=int,
        default=30,
        help="Initial terminal height before the first real console resize.",
    )
    parser.add_argument(
        "--session-id",
        default="export-demo",
        help="Logical session id used for debug output.",
    )
    parser.add_argument(
        "--no-alt-screen",
        action="store_true",
        help="Do not switch to the terminal alternate screen.",
    )
    parser.add_argument(
        "cmd",
        nargs=argparse.REMAINDER,
        help="Optional command to launch. Defaults to powershell.exe.",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    cmd = args.cmd or _default_shell()
    if cmd and cmd[0] == "--":
        cmd = cmd[1:] or _default_shell()

    vt = VirtualTerminal(
        args.session_id,
        cmd,
        Path(args.cwd),
        cols=args.cols,
        rows=args.rows,
    )
    ConsoleSession(
        vt,
        use_alt_screen=not args.no_alt_screen,
        passthrough_output=True,
    ).run()


if __name__ == "__main__":
    main()
