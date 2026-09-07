"""Tracks webhook message ID → persona_id mapping for reply targeting.

When the single bot sends responses via webhooks (different name/avatar per
persona), Discord reply chains lose the persona identity.  This tracker
maintains the mapping so ``_target_reason()`` can resolve which persona
authored a given webhook message.

Storage: in-memory dict + optional JSON persistence (``webhook_map.json``).
Per-channel LRU capped at 200 entries.
"""
from __future__ import annotations

import json
import logging
from collections import OrderedDict
from pathlib import Path

LOGGER = logging.getLogger(__name__)

_MAX_PER_CHANNEL = 200


class WebhookTracker:
    """Maps ``(channel_id, message_id)`` → ``persona_id``."""

    def __init__(self, persist_path: Path | None = None) -> None:
        # {channel_id: OrderedDict[message_id, persona_id]}
        self._data: dict[int, OrderedDict[int, str]] = {}
        self._persist_path = persist_path
        if persist_path:
            self._load()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def track(self, channel_id: int, message_id: int, persona_id: str) -> None:
        """Record that *message_id* in *channel_id* was sent by *persona_id*."""
        bucket = self._data.setdefault(channel_id, OrderedDict())
        bucket[message_id] = persona_id
        # LRU eviction
        while len(bucket) > _MAX_PER_CHANNEL:
            bucket.popitem(last=False)
        self._save()

    def resolve(self, channel_id: int, message_id: int) -> str | None:
        """Return the persona_id for a webhook message, or ``None``."""
        bucket = self._data.get(channel_id)
        if not bucket:
            return None
        return bucket.get(message_id)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _save(self) -> None:
        if not self._persist_path:
            return
        try:
            serializable: dict[str, dict[str, str]] = {}
            for ch_id, bucket in self._data.items():
                serializable[str(ch_id)] = {
                    str(mid): pid for mid, pid in bucket.items()
                }
            self._persist_path.parent.mkdir(parents=True, exist_ok=True)
            self._persist_path.write_text(
                json.dumps(serializable, indent=2), encoding="utf-8",
            )
        except Exception:
            LOGGER.debug("Failed to save webhook tracker", exc_info=True)

    def _load(self) -> None:
        if not self._persist_path or not self._persist_path.exists():
            return
        try:
            raw = json.loads(self._persist_path.read_text(encoding="utf-8"))
            for ch_str, entries in raw.items():
                ch_id = int(ch_str)
                bucket = OrderedDict()
                for mid_str, pid in entries.items():
                    bucket[int(mid_str)] = pid
                # Trim to max
                while len(bucket) > _MAX_PER_CHANNEL:
                    bucket.popitem(last=False)
                self._data[ch_id] = bucket
            LOGGER.info(
                "Loaded webhook tracker: %d channels",
                len(self._data),
            )
        except Exception:
            LOGGER.warning("Failed to load webhook tracker", exc_info=True)
