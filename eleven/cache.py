
from __future__ import annotations

from collections import OrderedDict
from dataclasses import asdict
from hashlib import sha256
from json import dumps

from .models import SliceJob, SliceSettings


class JobCache:
    """Small in-memory LRU: changing a control never reimports the STL."""

    def __init__(self, capacity: int = 6):
        self.capacity = capacity
        self._items: OrderedDict[str, SliceJob] = OrderedDict()

    @staticmethod
    def key(fingerprint: str, settings: SliceSettings) -> str:
        payload = dumps({"mesh": fingerprint, "settings": asdict(settings)}, sort_keys=True)
        return sha256(payload.encode("utf-8")).hexdigest()

    def get(self, fingerprint: str, settings: SliceSettings) -> SliceJob | None:
        key = self.key(fingerprint, settings)
        value = self._items.get(key)
        if value is not None:
            self._items.move_to_end(key)
        return value

    def put(self, job: SliceJob) -> None:
        key = self.key(job.source_fingerprint, job.settings)
        self._items[key] = job
        self._items.move_to_end(key)
        while len(self._items) > self.capacity:
            self._items.popitem(last=False)
