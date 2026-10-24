"""Byte-budgeted LRU storage for the controller's raster intermediates."""

from collections import OrderedDict
from collections.abc import MutableMapping
from decimal import Decimal
from sys import getsizeof
from threading import RLock

from invariant_gfx.artifacts import BlobArtifact, ImageArtifact


def artifact_size(value):
    if isinstance(value, ImageArtifact):
        # RGBA pixels plus the lazily retained PNG, including encoding overhead.
        return value.width * value.height * 8 + 4096
    if isinstance(value, BlobArtifact):
        return getsizeof(value.data) + getsizeof(value.content_type) + 256
    if isinstance(value, dict):
        sizes = [artifact_size(item) for pair in value.items() for item in pair]
    elif isinstance(value, (tuple, list)):
        sizes = [artifact_size(item) for item in value]
    elif value is None or isinstance(value, (str, bytes, int, float, bool, Decimal)):
        return getsizeof(value)
    else:
        # Unknown domain objects can use the disk tier; never guess their size.
        return float("inf")
    return getsizeof(value) + sum(sizes)


class ByteBudgetCache(MutableMapping):
    def __init__(self, max_bytes, max_entries):
        self.max_bytes, self.max_entries = max_bytes, max_entries
        self._entries = OrderedDict()
        self._bytes = 0
        self._lock = RLock()

    def __getitem__(self, key):
        with self._lock:
            value, _ = self._entries[key]
            self._entries.move_to_end(key)
            return value

    def __setitem__(self, key, value):
        size = artifact_size(value) + getsizeof(key) + 128
        with self._lock:
            if key in self._entries:
                del self[key]
            if size > self.max_bytes:
                return
            while self._entries and (
                self._bytes + size > self.max_bytes
                or len(self._entries) >= self.max_entries
            ):
                _, (_, removed) = self._entries.popitem(last=False)
                self._bytes -= removed
            self._entries[key] = (value, size)
            self._bytes += size

    def __delitem__(self, key):
        with self._lock:
            _, size = self._entries.pop(key)
            self._bytes -= size

    def __iter__(self):
        with self._lock:
            return iter(tuple(self._entries))

    def __len__(self):
        with self._lock:
            return len(self._entries)

    def clear(self):
        with self._lock:
            self._entries.clear()
            self._bytes = 0
