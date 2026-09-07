from __future__ import annotations

import argparse
import asyncio
import logging

from .config import load_settings
from .discord_backend import ExportV2DiscordClient


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m ExportV2",
        description="Run the minimal ExportV2 Discord <-> LLM CLI (Claude Code / Codex) bridge.",
    )
    parser.add_argument(
        "--settings",
        default=None,
        help="Path to ExportV2 settings.json",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        help="Python logging level",
    )
    return parser.parse_args()


async def _main() -> None:
    args = _parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    settings = load_settings(args.settings)
    client = ExportV2DiscordClient(settings)
    try:
        await client.start(settings.bot_token)
    finally:
        if not client.is_closed():
            await client.close()


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
