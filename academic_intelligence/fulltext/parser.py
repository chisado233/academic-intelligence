"""PDF page-level text extraction (fulltext/ parser).

**pdfplumber (MIT) is the default backend** (upgrade I2 decision: the project
is MIT-licensed, so the AGPL-licensed PyMuPDF is only an *optional* backend
for personal local use). Both backends return the same ``ParsedPage`` /
``TextLine`` structure so the segmenter is backend-agnostic.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from academic_intelligence.fulltext.exceptions import FulltextParseError

logger = logging.getLogger(__name__)

__all__ = ["TextLine", "ParsedPage", "PDFParser"]


@dataclass
class TextLine:
    """One extracted text line with layout hints for the segmenter.

    Attributes:
        text: The line text (whitespace-collapsed).
        top: Y position of the line on its page (for paragraph-gap
            heuristics); ``None`` when the backend cannot report it.
        size: Font size of the line (for heading heuristics); ``None`` when
            unknown.
        font: Font name of the line (``"*Bold*"`` names mark headings).
    """

    text: str
    top: float | None = None
    size: float | None = None
    font: str | None = None


@dataclass
class ParsedPage:
    """One page of extracted lines."""

    page: int
    lines: list[TextLine] = field(default_factory=list)


class PDFParser:
    """Extract per-page text lines from a PDF file.

    Args:
        backend: ``"pdfplumber"`` (default, MIT) or ``"pymupdf"`` (optional,
            AGPL — only for personal local use). An unavailable backend raises
            ``FulltextParseError`` at construction.
    """

    def __init__(self, backend: str = "pdfplumber") -> None:
        if backend not in {"pdfplumber", "pymupdf"}:
            raise ValueError(f"Unknown PDF backend: {backend!r}")
        self.backend = backend
        if backend == "pymupdf":
            try:
                import fitz  # type: ignore[import-untyped]  # noqa: F401
            except ImportError as exc:
                raise FulltextParseError(
                    "pymupdf backend requires PyMuPDF (AGPL-3.0-only); install "
                    "the optional extra: pip install 'academic-intelligence[pymupdf]'"
                ) from exc

    def parse(self, path: str | Path) -> list[ParsedPage]:
        """Extract pages of lines from the PDF at *path*.

        Raises:
            FulltextParseError: When the file cannot be opened/parsed.
        """
        if self.backend == "pymupdf":
            return self._parse_pymupdf(path)
        return self._parse_pdfplumber(path)

    # ------------------------------------------------------------------
    # pdfplumber backend (default)
    # ------------------------------------------------------------------
    def _parse_pdfplumber(self, path: str | Path) -> list[ParsedPage]:
        try:
            import pdfplumber
        except ImportError as exc:
            raise FulltextParseError(
                "pdfplumber is required for the default full-text parser; "
                "install it: pip install pdfplumber"
            ) from exc
        try:
            pages: list[ParsedPage] = []
            with pdfplumber.open(path) as pdf:
                for number, page in enumerate(pdf.pages, start=1):
                    lines: list[TextLine] = []
                    try:
                        raw_lines = page.extract_text_lines()
                        for item in raw_lines:
                            text = str(item.get("text") or "").strip()
                            if not text:
                                continue
                            chars = item.get("chars") or []
                            size = max(
                                (c.get("size") or 0.0 for c in chars if isinstance(c, dict)),
                                default=0.0,
                            )
                            font = next(
                                (
                                    c.get("fontname")
                                    for c in chars
                                    if isinstance(c, dict) and c.get("fontname")
                                ),
                                None,
                            )
                            lines.append(
                                TextLine(
                                    text=text,
                                    top=float(item.get("top") or 0.0),
                                    size=float(size) if size else None,
                                    font=str(font) if font else None,
                                )
                            )
                    except Exception as exc:  # per-page degradation, keep others
                        logger.warning(
                            "pdfplumber line extraction failed on page %d: %s",
                            number,
                            exc,
                        )
                    if not lines:
                        lines = self._fallback_lines(page.extract_text())
                    pages.append(ParsedPage(page=number, lines=lines))
            return pages
        except FulltextParseError:
            raise
        except Exception as exc:
            raise FulltextParseError(
                f"Failed to parse PDF {path}: {exc}",
                file_path=str(path),
            ) from exc

    @staticmethod
    def _fallback_lines(text: str | None) -> list[TextLine]:
        """Build lines from plain text when the line-layout API is empty.

        Blank lines are dropped; no position/font metadata is available in
        this mode, so the segmenter falls back to blank-line paragraph
        grouping.
        """
        if not text:
            return []
        return [TextLine(text=line.strip()) for line in text.splitlines() if line.strip()]

    # ------------------------------------------------------------------
    # PyMuPDF backend (optional, AGPL)
    # ------------------------------------------------------------------
    def _parse_pymupdf(self, path: str | Path) -> list[ParsedPage]:
        import fitz

        try:
            doc = fitz.open(str(path))
        except Exception as exc:
            raise FulltextParseError(
                f"Failed to open PDF {path} with PyMuPDF: {exc}",
                file_path=str(path),
            ) from exc
        pages: list[ParsedPage] = []
        try:
            for number in range(doc.page_count):
                page = doc.load_page(number)
                data = page.get_text("dict")
                lines: list[TextLine] = []
                for block in data.get("blocks", []):
                    if not isinstance(block, dict) or block.get("type") != 0:
                        continue  # skip image blocks
                    for line in block.get("lines", []):
                        if not isinstance(line, dict):
                            continue
                        spans = line.get("spans") or []
                        text = "".join(
                            str(span.get("text") or "") for span in spans if isinstance(span, dict)
                        ).strip()
                        if not text:
                            continue
                        size = max(
                            (float(span.get("size") or 0.0) for span in spans if isinstance(span, dict)),
                            default=0.0,
                        )
                        font = next(
                            (
                                span.get("font")
                                for span in spans
                                if isinstance(span, dict) and span.get("font")
                            ),
                            None,
                        )
                        bbox = line.get("bbox") or (0.0, 0.0, 0.0, 0.0)
                        lines.append(
                            TextLine(
                                text=text,
                                top=float(bbox[1]) if len(bbox) > 1 else 0.0,
                                size=float(size) if size else None,
                                font=str(font) if font else None,
                            )
                        )
                pages.append(ParsedPage(page=number + 1, lines=lines))
        except Exception as exc:
            raise FulltextParseError(
                f"Failed to extract text from {path} with PyMuPDF: {exc}",
                file_path=str(path),
            ) from exc
        finally:
            doc.close()
        return pages
