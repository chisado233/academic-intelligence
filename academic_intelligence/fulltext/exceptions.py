"""Full-text pipeline exceptions.

``NoLegalOAFulltextError`` is the copyright-red-line contract: when no
locator finds a legal open-access copy, the pipeline raises this explicitly
instead of trying to bypass a paywall.
"""

from __future__ import annotations

from typing import Any

from academic_intelligence.core.exceptions import AcademicIntelligenceError

__all__ = [
    "FulltextError",
    "NoLegalOAFulltextError",
    "FulltextDownloadError",
    "FulltextParseError",
]


class FulltextError(AcademicIntelligenceError):
    """Base class for full-text pipeline errors."""


class NoLegalOAFulltextError(FulltextError):
    """No legal open-access full text could be located.

    Raised when every configured locator (Unpaywall / CORE / arXiv) fails to
    find a legal OA copy — including paywalled papers. The pipeline never
    bypasses a paywall; the message is an explicit rejection.
    """

    def __init__(
        self,
        message: str,
        *,
        paper_id: str | None = None,
        sources_attempted: list[str] | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, context=context)
        self.paper_id = paper_id
        self.sources_attempted = list(sources_attempted or [])


class FulltextDownloadError(FulltextError):
    """The located OA file could not be downloaded or is not a valid PDF."""

    def __init__(
        self,
        message: str,
        *,
        url: str | None = None,
        http_status: int | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, context=context)
        self.url = url
        self.http_status = http_status


class FulltextParseError(FulltextError):
    """The downloaded PDF could not be parsed into text pages."""

    def __init__(
        self,
        message: str,
        *,
        file_path: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, context=context)
        self.file_path = file_path
