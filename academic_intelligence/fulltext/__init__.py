"""Full-text pipeline: locate -> download -> parse -> segment -> persist.

Legal open-access full text only. The pipeline never bypasses a paywall:
when no locator (Unpaywall / CORE / arXiv) finds a legal OA copy, it raises
``NoLegalOAFulltextError`` ("无合法 OA 全文").
"""

from __future__ import annotations

from academic_intelligence.fulltext.downloader import FulltextDownloader
from academic_intelligence.fulltext.exceptions import (
    FulltextDownloadError,
    FulltextError,
    FulltextParseError,
    NoLegalOAFulltextError,
)
from academic_intelligence.fulltext.locator import FulltextLocator
from academic_intelligence.fulltext.models import FullText, OALocation, Segment
from academic_intelligence.fulltext.parser import ParsedPage, PDFParser, TextLine
from academic_intelligence.fulltext.pipeline import FulltextPipeline
from academic_intelligence.fulltext.segmenter import Segmenter

__all__ = [
    "FulltextPipeline",
    "FulltextLocator",
    "FulltextDownloader",
    "PDFParser",
    "Segmenter",
    "FullText",
    "OALocation",
    "Segment",
    "ParsedPage",
    "TextLine",
    "FulltextError",
    "NoLegalOAFulltextError",
    "FulltextDownloadError",
    "FulltextParseError",
]
