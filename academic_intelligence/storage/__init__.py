"""Storage module for persistent data backends.

Provides abstract base class and concrete implementations for SQLite and JSON storage.
"""

from academic_intelligence.storage.base import BaseStorage
from academic_intelligence.storage.json_store import JSONStorage
from academic_intelligence.storage.sqlite_store import SQLiteStorage

__all__ = ["BaseStorage", "JSONStorage", "SQLiteStorage"]
