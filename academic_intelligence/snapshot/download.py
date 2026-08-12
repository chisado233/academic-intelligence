"""Partition download with resume, progress and gz integrity verification.

Each manifest file downloads to ``<downloads>/<key>`` via a ``<key>.part``
temporary file:

- **Resume**: a leftover ``.part`` file is resumed with an HTTP ``Range``
  header.  If the server answers ``200`` (Range ignored), the partial file is
  discarded and the download restarts from scratch.
- **Integrity**: a fully-downloaded file is stream-verified through gzip
  (the CRC32 trailer only passes when every decompressed byte is valid).
  A corrupt file is deleted so the next run re-downloads it.
- **Idempotence**: a verified final file is skipped without re-downloading.

Downloads run concurrently (default 4 workers) sharing one
``httpx.AsyncClient``; any failed partition is reported as a
:class:`~academic_intelligence.snapshot.SnapshotError` with the failed URLs —
the ``.part`` files are left in place so a re-run resumes them.
"""

from __future__ import annotations

import asyncio
import contextlib
import gzip
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from academic_intelligence.snapshot import SnapshotError
from academic_intelligence.snapshot.manifest import SnapshotFile

#: Default concurrent partition downloads.
DEFAULT_WORKERS = 4

#: Chunk size for the gzip streaming verify (decompressed bytes are not kept).
_GZ_VERIFY_CHUNK = 4 * 1024 * 1024


def verify_gz(path: Path) -> None:
    """Stream-verify *path* is a valid gzip file (decompress to EOF).

    Raises :class:`~academic_intelligence.snapshot.SnapshotError` when the
    gzip stream is corrupt or truncated.  Note this fully decompresses the
    file once — for a real multi-GB partition that is the intended integrity
    cost of "校验 gz" (decompressed bytes are discarded on the fly).
    """
    try:
        with gzip.open(path, "rb") as handle:
            while handle.read(_GZ_VERIFY_CHUNK):
                pass
    except OSError as exc:
        raise SnapshotError(f"gz 校验失败: {path.name} 不是有效的 gzip 文件（{exc}）") from exc


def _gzip_ok(path: Path) -> bool:
    """Return True when *path* exists and verifies as gzip (corrupt → False)."""
    if not path.is_file():
        return False
    try:
        verify_gz(path)
    except SnapshotError:
        return False
    return True


@dataclass
class DownloadSummary:
    """Result of a partition-download run.

    Attributes:
        downloaded: Files freshly downloaded (or resumed) this run.
        skipped: Files already present and verified.
        bytes_written: Total compressed bytes written this run.
        failed: URLs that failed (their ``.part`` files remain for resume).
    """

    downloaded: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    bytes_written: int = 0
    failed: list[str] = field(default_factory=list)


ProgressCallback = Callable[[str, int, int | None], Awaitable[None] | None]


async def _stream_response(
    client: httpx.AsyncClient,
    url: str,
    part_path: Path,
    start: int,
    total: int | None,
    on_progress: ProgressCallback | None,
) -> None:
    """Stream one GET into *part_path*, appending for resumed ranges.

    ``start > 0`` sends ``Range: bytes=<start>-``; a ``200`` reply means the
    server ignored the range, so the partial file is discarded and the body
    is written from byte 0.
    """
    headers = {"Range": f"bytes={start}-"} if start > 0 else {}
    async with client.stream("GET", url, headers=headers) as response:
        if response.status_code in (401, 403, 404, 410):
            await response.aread()
            raise SnapshotError(
                f"下载失败: HTTP {response.status_code} — {url}"
                "（分区文件可能已从 S3 移除，请尝试最新快照）"
            )
        if start > 0 and response.status_code == 200:
            part_path.unlink(missing_ok=True)
            start = 0
        elif response.status_code not in (200, 206):
            await response.aread()
            raise SnapshotError(f"下载失败: 意外 HTTP {response.status_code} — {url}")
        mode = "ab" if (start > 0 and response.status_code == 206) else "wb"
        written = start
        with open(part_path, mode) as handle:
            async for chunk in response.aiter_bytes():
                handle.write(chunk)
                written += len(chunk)
                if on_progress is not None:
                    result = on_progress(url, written, total)
                    if result is not None:
                        await result


async def download_partition(
    file: SnapshotFile,
    dest_dir: Path,
    *,
    client: httpx.AsyncClient | None = None,
    on_progress: ProgressCallback | None = None,
) -> Path:
    """Download one partition file into *dest_dir* (resume + verify).

    Args:
        file: Manifest partition entry (url + size + unique key).
        dest_dir: Download directory (created if missing).
        client: Optional shared httpx client; when omitted one is created and
            closed around the call.
        on_progress: Async callback ``(url, bytes_written, total_bytes)``.

    Returns:
        The final verified file path.

    Raises:
        SnapshotError: On HTTP failure or a gzip integrity failure.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / file.key
    if _gzip_ok(dest):
        return dest
    part_path = dest.with_name(file.key + ".part")

    own_client = client is None
    if client is None:
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(120.0, connect=30.0), follow_redirects=True
        )
    try:
        start = part_path.stat().st_size if part_path.is_file() else 0
        await _stream_response(client, file.url, part_path, start, file.size, on_progress)
        await asyncio.to_thread(verify_gz, part_path)
        os.replace(part_path, dest)
    finally:
        if own_client:
            await client.aclose()
    return dest


async def download_all(
    files: list[SnapshotFile],
    dest_dir: Path,
    *,
    client: httpx.AsyncClient | None = None,
    on_progress: ProgressCallback | None = None,
    workers: int = DEFAULT_WORKERS,
) -> DownloadSummary:
    """Download *files* into *dest_dir* with up to *workers* in flight.

    A single shared httpx client is used (connection pooling).  Any failure
    raises :class:`~academic_intelligence.snapshot.SnapshotError` listing the
    failed URLs — the partial files stay for a resumable re-run.
    """
    if not files:
        return DownloadSummary()
    own_client = client is None
    if client is None:
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(120.0, connect=30.0), follow_redirects=True
        )
    summary = DownloadSummary()
    semaphore = asyncio.Semaphore(max(1, workers))

    async def _one(file: SnapshotFile) -> tuple[str, str]:
        async with semaphore:
            if _gzip_ok(dest_dir / file.key):
                return "skipped", file.key
            await download_partition(file, dest_dir, client=client, on_progress=on_progress)
            return "downloaded", file.key

    try:
        results: list[Any] = await asyncio.gather(
            *[_one(file) for file in files], return_exceptions=True
        )
    finally:
        if own_client:
            await client.aclose()

    for file, result in zip(files, results, strict=True):
        if isinstance(result, BaseException):
            summary.failed.append(file.url)
            continue
        kind, key = result
        if kind == "skipped":
            summary.skipped.append(key)
        else:
            summary.downloaded.append(key)
            with contextlib.suppress(OSError):
                summary.bytes_written += (dest_dir / key).stat().st_size
    if summary.failed:
        raise SnapshotError(
            f"{len(summary.failed)}/{len(files)} 个分区下载失败: "
            + ", ".join(summary.failed)
            + " — 重跑 paper snapshot download 将断点续传"
        )
    return summary
