"""Persistence of already-notified request IDs (survives restarts)"""
from __future__ import annotations

import json
import logging
import tempfile
from collections import OrderedDict
from pathlib import Path

logger = logging.getLogger(__name__)


class SeenStore:
    def __init__(self, path: Path, max_ids: int) -> None:
        self.path = path
        self.max_ids = max_ids
        # feed_key -> OrderedDict[request_id, None] (insertion order = oldest first)
        self._seen: dict[str, OrderedDict[int, None]] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("could not read the state file, starting fresh: %s", exc)
            return
        for key, ids in (raw.get("seen") or {}).items():
            self._seen[key] = OrderedDict((int(i), None) for i in ids)
        logger.info(
            "state loaded: %s",
            {k: len(v) for k, v in self._seen.items()} or "(empty)",
        )

    def save(self) -> None:
        payload = {"seen": {k: list(v.keys()) for k, v in self._seen.items()}}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Write via a temp file so a crash mid-write cannot corrupt the state
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=self.path.parent, delete=False
            ) as fh:
                json.dump(payload, fh, ensure_ascii=False)
                tmp = Path(fh.name)
            tmp.replace(self.path)
        except OSError as exc:
            logger.error("failed to save the state file: %s", exc)

    def is_known(self, feed_key: str) -> bool:
        """Whether this feed has been polled at least once before"""
        return feed_key in self._seen

    def has(self, feed_key: str, request_id: int) -> bool:
        return request_id in self._seen.get(feed_key, {})

    def add(self, feed_key: str, request_id: int) -> None:
        bucket = self._seen.setdefault(feed_key, OrderedDict())
        bucket.pop(request_id, None)
        bucket[request_id] = None
        while len(bucket) > self.max_ids:
            bucket.popitem(last=False)

    def add_many(self, feed_key: str, request_ids: list[int]) -> None:
        self._seen.setdefault(feed_key, OrderedDict())
        for rid in request_ids:
            self.add(feed_key, rid)
