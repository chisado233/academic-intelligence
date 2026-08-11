"""Budget persistence: the :class:`BudgetStore` protocol and an in-memory store.

The SQLite backend's ``SQLiteStorage.get_budget_usage`` /
``save_budget_usage`` (WP5 storage migration) structurally satisfy the
:class:`BudgetStore` protocol, so a connected
:class:`~academic_intelligence.storage.sqlite_store.SQLiteStorage` can be
passed straight to :class:`~academic_intelligence.budget.manager.BudgetManager`
as the ``store``.  :class:`MemoryBudgetStore` is the default when no store is
supplied (pure in-process bookkeeping, also used by tests as a mock).
"""

from __future__ import annotations

import asyncio
from typing import Protocol, runtime_checkable

__all__ = ["BudgetStore", "MemoryBudgetStore"]


@runtime_checkable
class BudgetStore(Protocol):
    """Persistence contract for budget usage rows (``source`` + period bucket)."""

    async def get_budget_usage(self, source: str, period: str) -> float | None:
        """Return the accumulated usage for ``(source, period)``, or ``None``.

        ``None`` and ``0.0`` both mean "no usage recorded"; callers treat a
        missing row as a fresh bucket.
        """
        ...

    async def save_budget_usage(self, source: str, period: str, used: float, unit: str) -> None:
        """Persist (upsert) the usage of a ``(source, period)`` bucket."""
        ...


class MemoryBudgetStore:
    """In-process budget usage store (tests, no-DB usage)."""

    def __init__(self) -> None:
        self._data: dict[tuple[str, str], tuple[float, str]] = {}
        self._lock = asyncio.Lock()

    async def get_budget_usage(self, source: str, period: str) -> float | None:
        """Return the recorded usage for ``(source, period)`` or ``None``."""
        async with self._lock:
            row = self._data.get((source, period))
            return row[0] if row else None

    async def save_budget_usage(self, source: str, period: str, used: float, unit: str) -> None:
        """Store (upsert) the usage of a ``(source, period)`` bucket."""
        async with self._lock:
            self._data[(source, period)] = (used, unit)
