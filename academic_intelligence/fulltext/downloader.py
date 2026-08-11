"""Download OA PDFs to a local cache (fulltext/ downloader).

Reuses ``utils.HTTPClient`` for rate limiting / retries / UA rotation and
adds a *file-level* cache: a PDF already present on disk for a paper id is
reused without another network round trip. The transport-level text cache is
disabled for binary downloads (``use_cache=False``) so PDF bytes never pass
through the text-oriented HTTP response cache.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
from pathlib import Path

from academic_intelligence.fulltext.exceptions import FulltextDownloadError
from academic_intelligence.utils.http import HTTPClient

logger = logging.getLogger(__name__)

_PDF_MAGIC = b"%PDF-"
# Default local PDF cache directory (project tmp convention, design §1.3).
DEFAULT_CACHE_DIR = "tmp/fulltext"
_MAX_PDF_BYTES = 200 * 1024 * 1024  # defensive cap: 200 MB

# Sanitize any identifier into a filesystem-safe stem.
_UNSAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_stem(paper_id: str) -> str:
    """Return a filesystem-safe filename stem for *paper_id*."""
    stem = _UNSAFE_FILENAME_RE.sub("_", paper_id.strip()).strip("._")
    return stem or "paper"


class FulltextDownloader:
    """Download and cache an OA PDF at ``<cache_dir>/<safe_paper_id>.pdf``."""

    def __init__(
        self,
        http_client: HTTPClient | None = None,
        *,
        cache_dir: str | os.PathLike[str] = DEFAULT_CACHE_DIR,
    ) -> None:
        """Initialize the downloader.

        Args:
            http_client: Connected ``HTTPClient`` to download through (must be
                connected by the caller; rate limiting / retries / UA rotation
                come from it). When ``None`` the downloader is a no-op that
                raises ``FulltextDownloadError`` — a downloader without a
                transport cannot download.
            cache_dir: Directory for cached PDFs (created on demand).
        """
        self._http = http_client
        self.cache_dir = Path(cache_dir)

    def cache_path_for(self, paper_id: str) -> Path:
        """Return the cache path a paper's PDF would be stored at."""
        return self.cache_dir / f"{_safe_stem(paper_id)}.pdf"

    async def download(self, url: str, paper_id: str) -> Path:
        """Download *url* into the cache and return the local path.

        The file is cached under ``<cache_dir>/<safe_paper_id>.pdf``; a
        previously downloaded file is reused without a network request.
        Non-200 responses and bodies that are not PDF files raise
        ``FulltextDownloadError``.

        Args:
            url: Legal OA PDF URL (from the locator).
            paper_id: Paper id used for the cache filename.

        Returns:
            The local ``Path`` of the cached PDF.

        Raises:
            FulltextDownloadError: On transport failure, non-200 response,
                non-PDF body, or oversized download.
        """
        target = self.cache_path_for(paper_id)
        if target.exists() and target.stat().st_size > 0:
            logger.debug("Reusing cached PDF %s", target)
            return target
        if self._http is None:
            raise FulltextDownloadError(
                "No HTTP transport configured; cannot download full text",
                url=url,
            )

        self.cache_dir.mkdir(parents=True, exist_ok=True)
        try:
            # use_cache=False: PDF bytes must not round-trip through the
            # text-oriented HTTP response cache.
            response = await self._http.get(url, use_cache=False)
        except Exception as exc:
            raise FulltextDownloadError(
                f"Download failed for {url}: {exc}",
                url=url,
                context={"reason": str(exc)},
            ) from exc

        if response.status_code != 200:
            raise FulltextDownloadError(
                f"Download returned HTTP {response.status_code} for {url}",
                url=url,
                http_status=response.status_code,
            )
        content = response.content
        if not content.startswith(_PDF_MAGIC):
            raise FulltextDownloadError(
                "Downloaded body is not a PDF (missing %PDF header); "
                f"the URL may point to an HTML landing page: {url}",
                url=url,
                http_status=response.status_code,
            )
        if len(content) > _MAX_PDF_BYTES:
            raise FulltextDownloadError(
                f"Downloaded PDF exceeds the {_MAX_PDF_BYTES} byte cap",
                url=url,
                http_status=response.status_code,
            )

        # Atomic write: temp file in the same directory, then rename.
        tmp = target.with_suffix(f".{os.getpid()}.tmp")
        try:
            tmp.write_bytes(content)
            os.replace(tmp, target)
        except OSError as exc:
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)
            raise FulltextDownloadError(
                f"Failed to write PDF cache file: {exc}",
                url=url,
                context={"reason": str(exc)},
            ) from exc
        logger.info("Downloaded full text %s -> %s", url, target)
        return target
