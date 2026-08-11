"""LRU graph cache management.

The knowledge graph is a session-level working set bounded by
``Config.graph_cache_size``.  :class:`GraphCache` provides a simple
``OrderedDict``-based LRU store used by :class:`KnowledgeGraph` to cap the
number of resident nodes: when the capacity is exceeded the least-recently
used entry is evicted (optionally invoking a callback so the owning graph can
drop the associated adjacency data).
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Iterator
from typing import Any


class GraphCache:
    """A small LRU key-value store with eviction notifications.

    Args:
        max_size: Maximum number of entries to keep.  Entries beyond this
            limit evict the least-recently used key.
        on_evict: Optional callback invoked as ``on_evict(key, value)`` when
            an entry is evicted (either by overflow or by :meth:`pop`).
    """

    def __init__(
        self,
        max_size: int = 5000,
        on_evict: Callable[[str, Any], None] | None = None,
    ) -> None:
        self.max_size = max(1, int(max_size))
        self._store: OrderedDict[str, Any] = OrderedDict()
        self.on_evict = on_evict

    def __len__(self) -> int:
        return len(self._store)

    def __contains__(self, key: str) -> bool:
        return key in self._store

    def has(self, key: str) -> bool:
        """Return whether *key* is present without marking it as used."""
        return key in self._store

    def peek(self, key: str) -> Any | None:
        """Return the value for *key* without refreshing its recency."""
        return self._store.get(key)

    def get(self, key: str) -> Any | None:
        """Return the value for *key*, marking it as most-recently used."""
        if key not in self._store:
            return None
        value = self._store.pop(key)
        self._store[key] = value  # re-insert at the end (MRU)
        return value

    def put(self, key: str, value: Any) -> None:
        """Insert or refresh *key*; evict LRU entries when over capacity."""
        if key in self._store:
            del self._store[key]
        self._store[key] = value
        while len(self._store) > self.max_size:
            self._evict_oldest()

    def pop(self, key: str) -> Any | None:
        """Remove *key* if present (invoking the eviction callback)."""
        if key not in self._store:
            return None
        value = self._store.pop(key)
        if self.on_evict is not None:
            self.on_evict(key, value)
        return value

    def clear(self) -> None:
        """Remove all entries, notifying the eviction callback for each."""
        keys = list(self._store.keys())
        for key in keys:
            self.pop(key)

    def items(self) -> Iterator[tuple[str, Any]]:
        """Iterate over ``(key, value)`` pairs in recency order."""
        yield from self._store.items()

    def _evict_oldest(self) -> None:
        key, value = self._store.popitem(last=False)
        if self.on_evict is not None:
            self.on_evict(key, value)
