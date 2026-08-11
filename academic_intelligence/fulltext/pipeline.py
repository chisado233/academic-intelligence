"""Full-text pipeline orchestration (fulltext/ pipeline).

``FulltextPipeline.fetch(paper, sources=...)`` runs the whole legal-OA
full-text flow (upgrade technical-design §1.3):

0.  ID normalization: the pipeline works on a :class:`Paper` — the caller
    resolves an input id (internal id / arXiv id / DOI) to a ``Paper`` with
    ``doi`` / ``arxiv_id`` populated (M15). A paper without a DOI can only be
    located through the arXiv path.
1.  Locate: Unpaywall -> CORE -> arXiv priority (first legal hit wins).
2.  Download: reuse ``utils.HTTPClient`` (rate limit / retry / UA) with a
    file-level cache under ``tmp/fulltext/``.
3.  Parse: pdfplumber (default) or PyMuPDF (optional) page extraction.
4.  Segment: paragraphs with heading / text / page.
5.  Persist: ``full_text`` table via ``storage.save_full_text`` (when a
    SQLite storage instance is attached).
6.  Failure path: no legal OA anywhere -> ``NoLegalOAFulltextError``
    ("无合法 OA 全文") — a paywall is never bypassed.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from academic_intelligence.core.models import Paper
from academic_intelligence.core.types import AntiCrawlStrategy
from academic_intelligence.fulltext.downloader import FulltextDownloader
from academic_intelligence.fulltext.exceptions import NoLegalOAFulltextError
from academic_intelligence.fulltext.locator import DEFAULT_SOURCES, FulltextLocator
from academic_intelligence.fulltext.models import FullText
from academic_intelligence.fulltext.parser import PDFParser
from academic_intelligence.fulltext.segmenter import Segmenter
from academic_intelligence.utils.http import HTTPClient

if TYPE_CHECKING:
    # TYPE_CHECKING only: importing SQLiteStorage here would create a cycle
    # (sqlite_store imports fulltext.models; its annotation is never
    # evaluated at runtime thanks to ``from __future__ import annotations``).
    from academic_intelligence.storage.sqlite_store import SQLiteStorage

logger = logging.getLogger(__name__)

# Policy-rate-limited default transport for the full-text pipeline.
_DEFAULT_BASE_DELAY = 1.0  # 1 request / second default polite rate


class FulltextPipeline:
    """Orchestrates locate -> download -> parse -> segment -> persist.

    Args:
        http_client: Optional shared ``HTTPClient``. When omitted the
            pipeline creates (and owns) a polite-rate client; when given, the
            caller owns its lifecycle and must call :meth:`close` on the
            pipeline only if the pipeline created the client.
        locator: Optional custom locator (defaults to ``FulltextLocator``).
        downloader: Optional custom downloader.
        parser: Optional custom parser (defaults to pdfplumber).
        segmenter: Optional custom segmenter.
        storage: Optional ``SQLiteStorage``; when set and ``persist=True``
            the result is written to the ``full_text`` table.
        cache_dir: Local PDF cache directory (default ``tmp/fulltext``).
        unpaywall_email: Polite-pool email for the default locator's
            Unpaywall queries (falls back to ``UNPAYWALL_EMAIL`` env).
        core_api_key: CORE API key for the default locator (falls back to
            ``CORE_API_KEY`` env). Ignored when ``locator`` is provided.
    """

    def __init__(
        self,
        *,
        http_client: HTTPClient | None = None,
        locator: FulltextLocator | None = None,
        downloader: FulltextDownloader | None = None,
        parser: PDFParser | None = None,
        segmenter: Segmenter | None = None,
        storage: SQLiteStorage | None = None,
        cache_dir: str | Path = "tmp/fulltext",
        unpaywall_email: str | None = None,
        core_api_key: str | None = None,
    ) -> None:
        self._owns_http = http_client is None
        self._http = http_client or HTTPClient(
            strategy=AntiCrawlStrategy(
                base_delay=_DEFAULT_BASE_DELAY,
                adaptive_delay=False,
            ),
            enable_cache=True,
        )
        self.locator = locator or FulltextLocator(
            self._http,
            unpaywall_email=unpaywall_email,
            core_api_key=core_api_key,
        )
        self.downloader = downloader or FulltextDownloader(
            self._http,
            cache_dir=cache_dir,
        )
        self.parser = parser or PDFParser()
        self.segmenter = segmenter or Segmenter()
        self.storage = storage

    async def connect(self) -> None:
        """Connect the owned HTTP transport (no-op for caller-owned clients)."""
        if self._owns_http:
            await self._http.connect()

    async def close(self) -> None:
        """Close the owned HTTP transport (caller-owned clients are untouched)."""
        if self._owns_http:
            await self._http.close()

    async def __aenter__(self) -> FulltextPipeline:
        await self.connect()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.close()

    async def fetch(
        self,
        paper: Paper,
        sources: tuple[str, ...] = DEFAULT_SOURCES,
        *,
        persist: bool = True,
    ) -> FullText:
        """Fetch the legal OA full text of *paper*.

        Args:
            paper: Paper to fetch full text for (``doi`` / ``arxiv_id`` are
                the identifiers the locator uses; M15 normalization is the
                caller's responsibility, see class docstring).
            sources: Locator priority order (default Unpaywall -> CORE ->
                arXiv -> Europe PMC, mirroring :data:`DEFAULT_SOURCES`).
            persist: Write the result to ``storage.full_text`` when a storage
                instance is attached.

        Returns:
            The parsed ``FullText`` (segments with heading / text / page).

        Raises:
            NoLegalOAFulltextError: When no legal OA full text could be
                located — including paywalled papers. The pipeline never
                bypasses a paywall.
            FulltextDownloadError: The located file could not be downloaded.
            FulltextParseError: The downloaded PDF could not be parsed.
        """
        await self.connect()

        paper_id = self._resolve_paper_id(paper)
        location = await self.locator.locate(paper, sources=sources)
        if location is None:
            raise NoLegalOAFulltextError(
                "无合法 OA 全文：未能定位到任何合法开放获取全文"
                f"（paper={paper_id!r}, doi={paper.doi!r}, arxiv_id={paper.arxiv_id!r}）；"
                "付费墙内容请通过合法渠道获取，系统不会绕过付费墙。"
                "提示：可尝试 Unpaywall（https://unpaywall.org）手动查询该 DOI。",
                paper_id=paper_id,
                sources_attempted=list(sources),
            )

        path = await self.downloader.download(location.url, paper_id)
        pages = self.parser.parse(path)
        segments = self.segmenter.segment(pages)

        fulltext = FullText(
            paper_id=paper_id,
            source=location.source,
            oa_license=location.license,
            file_path=str(path),
            paragraph_count=len(segments),
            segments=segments,
        )
        if persist and self.storage is not None:
            await self.storage.save_full_text(fulltext)
        logger.info(
            "Fulltext fetched for %s from %s (%d paragraphs, %d pages)",
            paper_id,
            location.source,
            len(segments),
            len(pages),
        )
        return fulltext

    @staticmethod
    def _resolve_paper_id(paper: Paper) -> str:
        """Return the best stable identifier to key the cache/database row."""
        return (
            paper.id
            or paper.arxiv_id
            or paper.doi
            or paper.pmid
            or "unknown"
        )
