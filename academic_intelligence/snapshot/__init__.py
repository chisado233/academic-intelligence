"""OpenAlex quarterly free snapshot tooling.

The OpenAlex public S3 bucket publishes a full, free quarterly dump of every
entity (works, authors, …).  This package downloads the *works* JSONL
partitions and builds a local SQLite reverse-citation index, so
``paper trace-citing`` can answer "who cites this paper" with **zero API
quota** once the index exists.

Module map:

- :mod:`~academic_intelligence.snapshot.manifest` — fetch/parse the latest
  works manifest (snapshot date, partition file list, sizes);
- :mod:`~academic_intelligence.snapshot.download` — resume-capable partition
  download with gz integrity verification and progress;
- :mod:`~academic_intelligence.snapshot.build` — decompress JSONL.gz → SQLite
  index (works table + inverted citation edges);
- :mod:`~academic_intelligence.snapshot.store` — SQLite schema + queries
  (including the reverse-citation lookup used by the routing switch).

Downloading the full works snapshot is a **user-initiated, deliberately
large** operation (~665 GB compressed for 510M works as of 2026-06).  The
CLI prints an explicit size notice before downloading; tests use tiny mock
fixtures and never touch the network.
"""

from __future__ import annotations

from pathlib import Path

#: Directory name of the snapshot store inside the project (``<project>/snapshot_data/``).
SNAPSHOT_DIR_NAME = "snapshot_data"

#: SQLite index file name inside the snapshot dir.
INDEX_DB_NAME = "index.db"

#: Downloaded partition files live under ``<snapshot_dir>/downloads/``.
DOWNLOADS_DIR_NAME = "downloads"

#: Routing-switch config file inside the snapshot dir (``paper snapshot enable/disable``).
ROUTING_CONFIG_NAME = "config.json"


class SnapshotError(RuntimeError):
    """Raised for any snapshot failure (manifest, download, build, query).

    Carries an actionable, user-facing message (no secrets, no huge dumps).
    """


def default_snapshot_dir() -> Path:
    """Return the default snapshot store: ``<project root>/snapshot_data/``.

    The project root is inferred from this package's location
    (``academic_intelligence/snapshot/`` → project root).
    """
    return Path(__file__).resolve().parents[2] / SNAPSHOT_DIR_NAME


def snapshot_index_path(snapshot_dir: Path) -> Path:
    """Return the SQLite index path inside *snapshot_dir*."""
    return snapshot_dir / INDEX_DB_NAME


def downloads_dir(snapshot_dir: Path) -> Path:
    """Return the partition-download directory inside *snapshot_dir*."""
    return snapshot_dir / DOWNLOADS_DIR_NAME


def routing_config_path(snapshot_dir: Path) -> Path:
    """Return the routing-switch config path inside *snapshot_dir*."""
    return snapshot_dir / ROUTING_CONFIG_NAME


__all__ = [
    "DOWNLOADS_DIR_NAME",
    "INDEX_DB_NAME",
    "ROUTING_CONFIG_NAME",
    "SNAPSHOT_DIR_NAME",
    "SnapshotError",
    "default_snapshot_dir",
    "downloads_dir",
    "routing_config_path",
    "snapshot_index_path",
]
