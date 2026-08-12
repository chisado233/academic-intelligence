"""OpenAlex works-snapshot manifest discovery and parsing.

The OpenAlex public bucket publishes a per-entity ``manifest.json`` that is
written *last* (after every partition file is uploaded), so its presence is
the completeness signal.  Two layouts are probed in order (both verified on
the live bucket, 2026-06):

1. **Current (post-2026)**: ``data/jsonl/works/manifest.json`` —
   ``{"date": "2026-06-26", "entity": "works", "record_count": …, "content_length": …,
   "files": [{"url": "s3://openalex/data/jsonl/works/updated_date=YYYY-MM-DD/part_0000.gz",
   "meta": {"content_length": …, "record_count": …}}]}``.
2. **Legacy (pre-2026, relocated under ``legacy-data/``)**:
   ``legacy-data/works/manifest`` — ``{"entries": [{"url": …, "meta": {…}}]}``
   (no top-level date; the latest ``updated_date=`` inside the URLs is used).

Both layout manifests carry ``s3://openalex/…`` URLs, which are rewritten to
``https://openalex.s3.amazonaws.com/…`` for plain HTTPS download.

The URL probing the original design described (``data/work/manifests/latest/``
and the flat ``data/work/{date}/part_000.gz`` layout) now 404s on the live
bucket — those paths moved to the ``legacy-data/`` prefix in 2026 — so this
module probes the two manifest files above instead, keeping the *manifest
file* discovery approach intact.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import httpx

from academic_intelligence.snapshot import SnapshotError
from academic_intelligence.utils.http import HTTPClient

#: Current works JSONL manifest (post-2026 layout) — verified live.
CURRENT_MANIFEST_URL = "https://openalex.s3.amazonaws.com/data/jsonl/works/manifest.json"
#: Legacy works manifest (pre-2026 layout, relocated under ``legacy-data/``).
LEGACY_MANIFEST_URL = "https://openalex.s3.amazonaws.com/legacy-data/works/manifest"

_S3_URL_PREFIXES = ("s3://openalex/", "s3://openalex")
_UPDATED_DATE_RE = re.compile(r"updated_date=(\d{4}-\d{2}-\d{2})")


@dataclass(frozen=True)
class SnapshotFile:
    """One partition file of the works snapshot.

    Attributes:
        url: Rewritten HTTPS download URL.
        size: Compressed size in bytes (``None`` when the manifest omitted it).
        record_count: Expected JSONL record count in the partition (``None``
            when unknown) — used for build progress only.
        date: ``updated_date`` partition key (``YYYY-MM-DD``) parsed from the
            URL, ``None`` when the URL carries no partition date.
        key: Unique local filename stem — ``<date>_<basename>`` when a date is
            known, else ``part<index>_<basename>``.
    """

    url: str
    size: int | None = None
    record_count: int | None = None
    date: str | None = None
    key: str = ""


@dataclass(frozen=True)
class SnapshotManifest:
    """Parsed works-snapshot manifest.

    Attributes:
        snapshot_date: Manifest date (``date`` field, or the latest partition
            ``updated_date`` for legacy manifests).
        format: Snapshot format label (``jsonl``), when present.
        record_count: Total expected works (when the manifest carries it).
        content_length: Total compressed bytes (when the manifest carries it).
        files: Partition files in manifest order.
        url: The manifest URL the data came from (diagnostics).
    """

    snapshot_date: str | None = None
    format: str | None = None
    record_count: int | None = None
    content_length: int | None = None
    files: list[SnapshotFile] = field(default_factory=list)
    url: str = ""


def rewrite_s3_url(url: str) -> str:
    """Rewrite ``s3://openalex/…`` object URLs to plain HTTPS download URLs."""
    if url.startswith("s3://openalex/"):
        return "https://openalex.s3.amazonaws.com/" + url[len("s3://openalex/") :]
    if url.startswith("s3://openalex"):
        return "https://openalex.s3.amazonaws.com/" + url[len("s3://openalex") :]
    return url


def _date_from_url(url: str) -> str | None:
    """Extract the ``updated_date=YYYY-MM-DD`` partition key from a URL."""
    match = _UPDATED_DATE_RE.search(url)
    return match.group(1) if match else None


def _int_value(value: Any) -> int | None:
    """Coerce a manifest numeric field to int (tolerating None/garbage)."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _file_key(date: str | None, url: str, index: int) -> str:
    """Build a unique local filename for a partition file."""
    basename = url.rsplit("/", 1)[-1] if "/" in url else url
    if date:
        return f"{date}_{basename}"
    return f"part{index:03d}_{basename}"


def _parse_file_entry(entry: dict[str, Any], index: int, url_prefix_hint: str) -> SnapshotFile:
    """Map one manifest ``files[]``/``entries[]`` item to a :class:`SnapshotFile`."""
    raw_url = entry.get("url")
    if not isinstance(raw_url, str) or not raw_url:
        raise SnapshotError(f"invalid manifest entry #{index}: missing 'url' in {url_prefix_hint}")
    url = rewrite_s3_url(raw_url)
    meta = entry.get("meta")
    size: int | None = None
    record_count: int | None = None
    if isinstance(meta, dict):
        size = _int_value(meta.get("content_length"))
        record_count = _int_value(meta.get("record_count"))
    date = _date_from_url(url)
    return SnapshotFile(
        url=url,
        size=size,
        record_count=record_count,
        date=date,
        key=_file_key(date, url, index),
    )


def parse_manifest(data: dict[str, Any], source_url: str) -> SnapshotManifest:
    """Parse a manifest payload (current ``files`` shape or legacy ``entries`` shape)."""
    entries_raw = data.get("files")
    legacy = False
    if not isinstance(entries_raw, list):
        entries_raw = data.get("entries")
        legacy = True
    if not isinstance(entries_raw, list):
        raise SnapshotError(
            f"unexpected manifest shape from {source_url}: expected a 'files' "
            "(or legacy 'entries') array"
        )

    files: list[SnapshotFile] = []
    for index, entry in enumerate(entries_raw):
        if not isinstance(entry, dict):
            raise SnapshotError(
                f"invalid manifest entry #{index} in {source_url}: expected an object"
            )
        files.append(_parse_file_entry(entry, index, source_url))

    snapshot_date: str | None
    if legacy:
        dates = [f.date for f in files if f.date is not None]
        snapshot_date = max(dates) if dates else None
    else:
        raw_date = data.get("date")
        snapshot_date = raw_date if isinstance(raw_date, str) and raw_date else None

    record_count = _int_value(data.get("record_count"))
    content_length = _int_value(data.get("content_length"))
    if record_count is None:
        record_count = sum(f.record_count or 0 for f in files) or None
    if content_length is None:
        content_length = sum(f.size or 0 for f in files) or None
    raw_format = data.get("format")
    snapshot_format = raw_format if isinstance(raw_format, str) else None

    return SnapshotManifest(
        snapshot_date=snapshot_date,
        format=snapshot_format,
        record_count=record_count,
        content_length=content_length,
        files=files,
        url=source_url,
    )


async def fetch_manifest(http: HTTPClient | None = None) -> SnapshotManifest:
    """Probe and fetch the latest works snapshot manifest.

    Tries the current ``data/jsonl/works/manifest.json`` first, then the
    legacy ``legacy-data/works/manifest``.  Raises a
    :class:`~academic_intelligence.snapshot.SnapshotError` (with both probed
    URLs and a pointer to the OpenAlex docs) when neither is reachable.

    Args:
        http: Optional shared :class:`HTTPClient`; when omitted a client is
            created, connected and closed for the duration of the call.
    """
    client = http
    own_client = http is None
    if client is None:
        client = HTTPClient()
        await client.connect()
    try:
        errors: list[str] = []
        for url in (CURRENT_MANIFEST_URL, LEGACY_MANIFEST_URL):
            try:
                data = await client.get_json(url)
            except (httpx.HTTPStatusError, httpx.RequestError, httpx.TransportError) as exc:
                errors.append(f"{url}: {exc}")
                continue
            if not isinstance(data, dict):
                errors.append(f"{url}: expected a JSON object")
                continue
            try:
                return parse_manifest(data, source_url=url)
            except SnapshotError as exc:
                errors.append(str(exc))
                continue
        detail = "; ".join(errors) if errors else "all manifest probes failed"
        raise SnapshotError(
            f"无法获取 OpenAlex works 快照清单（{detail}）。"
            "快照布局说明见 https://developers.openalex.org/download/snapshot-format"
        ) from None
    finally:
        if own_client:
            await client.close()
