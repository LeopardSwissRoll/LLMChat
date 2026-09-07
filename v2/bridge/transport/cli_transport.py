from __future__ import annotations

import sys

from ..models import ResponseMeta
from .base import OutputSink, TransportAdapter


class CliOutputSink:
    """OutputSink that writes to stdout for CLI usage."""

    def __init__(self, verbose: bool = False) -> None:
        self._verbose = verbose

    async def begin(self, context: str) -> None:
        if self._verbose:
            print(f"[begin] {context}", file=sys.stderr)

    async def stream_update(self, text: str) -> None:
        if self._verbose:
            print(f"[streaming... {len(text)} chars]", file=sys.stderr, end="\r")

    async def finalize(self, text: str, meta: ResponseMeta) -> str | None:
        print(text)
        if self._verbose:
            print(f"\n[meta] opening: {meta.opening_line[:60]}", file=sys.stderr)
            print(f"[meta] closing: {meta.closing_line[:60]}", file=sys.stderr)
        return None

    async def fail(self, error: str) -> None:
        print(f"[ERROR] {error}", file=sys.stderr)
