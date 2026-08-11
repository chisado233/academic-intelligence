"""Caching utilities for academic data collection.

Provides in-memory and optional JSON-file persistent caching with TTL.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import os
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class Cache:
    """Simple cache with TTL support.

    Supports both in-memory and persistent (JSON file) caching.
    """

    def __init__(
        self,
        ttl: int = 3600,
        persistent: bool = False,
        persist_path: str | Path | None = None,
    ) -> None:
        """Initialize cache.

        Args:
            ttl: Time-to-live in seconds.
            persistent: Whether to persist cache to disk.
            persist_path: Path for the JSON cache file when persistent.
        """
        self.ttl = ttl
        self.persistent = persistent
        self.persist_path = Path(persist_path or "./.ai_cache.json")
        # key -> (value, expires_at)
        self._memory: dict[str, tuple[Any, float]] = {}
        self._lock = asyncio.Lock()
        self._inflight: dict[str, asyncio.Task[Any]] = {}
        if self.persistent and self.persist_path.exists():
            self._load_from_disk()

    def _load_from_disk(self) -> None:
        try:
            raw = json.loads(self.persist_path.read_text(encoding="utf-8"))
            now = time.time()
            for key, entry in raw.items():
                expires_at = float(entry.get("expires_at", 0))
                if expires_at > now:
                    self._memory[key] = (entry.get("value"), expires_at)
        except Exception as exc:
            logger.warning("Failed to load cache from %s: %s", self.persist_path, exc)

    def _save_to_disk(self) -> None:
        if not self.persistent:
            return
        try:
            now = time.time()
            payload = {
                k: {"value": v, "expires_at": exp}
                for k, (v, exp) in self._memory.items()
                if exp > now
            }
            self.persist_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.persist_path.with_name(
                f"{self.persist_path.name}.tmp-{uuid.uuid4().hex}"
            )
            try:
                with temporary.open("w", encoding="utf-8") as handle:
                    json.dump(payload, handle, ensure_ascii=False, default=str)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.persist_path)
            finally:
                temporary.unlink(missing_ok=True)
        except Exception as exc:
            logger.warning("Failed to persist cache to %s: %s", self.persist_path, exc)

    async def get(self, key: str) -> Any | None:
        """Get value from cache.

        Args:
            key: Cache key.

        Returns:
            Cached value or None if not found/expired.
        """
        async with self._lock:
            entry = self._memory.get(key)
            if entry is None:
                return None
            value, expires_at = entry
            if time.time() >= expires_at:
                del self._memory[key]
                return None
            return value

    async def set(self, key: str, value: Any, ttl: int | None = None) -> None:
        """Set value in cache.

        Args:
            key: Cache key.
            value: Value to cache.
            ttl: Optional per-entry TTL override (seconds).
        """
        async with self._lock:
            expires_at = time.time() + float(ttl if ttl is not None else self.ttl)
            self._memory[key] = (value, expires_at)
            await asyncio.to_thread(self._save_to_disk)

    async def get_or_set(
        self,
        key: str,
        factory: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Get from cache or compute and store.

        Args:
            key: Cache key.
            factory: Sync or async function to compute value if not cached.
            *args: Positional arguments for factory.
            **kwargs: Keyword arguments for factory.

        Returns:
            Cached or computed value.
        """
        cached = await self.get(key)
        if cached is not None:
            return cached
        async with self._lock:
            entry = self._memory.get(key)
            if entry is not None and time.time() < entry[1]:
                return entry[0]
            task = self._inflight.get(key)
            if task is None:
                task = asyncio.create_task(
                    self._compute_and_store(key, factory, args, kwargs)
                )
                self._inflight[key] = task
        # A caller cancelling its own wait must not cancel the shared
        # single-flight computation for every other waiter.
        return await asyncio.shield(task)

    async def _compute_and_store(
        self,
        key: str,
        factory: Callable[..., Any],
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Any:
        task = asyncio.current_task()
        try:
            result = factory(*args, **kwargs)
            if inspect.isawaitable(result):
                result = await result
            await self.set(key, result)
            return result
        finally:
            async with self._lock:
                if self._inflight.get(key) is task:
                    self._inflight.pop(key, None)

    async def invalidate(self, key: str) -> None:
        """Remove a key from cache."""
        async with self._lock:
            self._memory.pop(key, None)
            await asyncio.to_thread(self._save_to_disk)

    async def clear(self) -> None:
        """Clear all cached entries."""
        async with self._lock:
            self._memory.clear()
            if self.persistent and self.persist_path.exists():
                await asyncio.to_thread(self.persist_path.unlink, missing_ok=True)

    async def size(self) -> int:
        """Return number of non-expired entries (purges expired first)."""
        async with self._lock:
            now = time.time()
            expired = [k for k, (_, exp) in self._memory.items() if exp <= now]
            for k in expired:
                del self._memory[k]
            return len(self._memory)

    @staticmethod
    def make_key(*parts: Any) -> str:
        """Create a cache key from multiple parts.

        Args:
            *parts: Parts to combine into a key.

        Returns:
            Hashed cache key.
        """
        data = json.dumps(parts, sort_keys=True, default=str)
        return hashlib.sha256(data.encode()).hexdigest()
