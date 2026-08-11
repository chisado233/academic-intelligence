"""Paragraph segmentation of extracted PDF pages (fulltext/ segmenter).

Heuristics (upgrade §1.3: "段落切分（按空白行/字体启发式），记录页码"):

- **paragraph breaks**: a vertical gap between consecutive lines on the same
  page larger than ~1.5× the estimated line height starts a new paragraph;
  a page change also starts a new paragraph;
- **headings**: a line whose font size clearly exceeds the body median
  (or whose font name is ``*Bold*``) is a heading — it becomes the
  ``heading`` of the paragraphs that follow it until the next heading;
- **pages**: every paragraph records the page its first line appears on.

Each paragraph becomes one :class:`~academic_intelligence.fulltext.models.Segment`
(``heading`` / ``text`` / ``page``), so ``paragraph_count`` equals
``len(segments)``.
"""

from __future__ import annotations

import logging
import statistics
from collections.abc import Sequence

from academic_intelligence.fulltext.models import Segment
from academic_intelligence.fulltext.parser import ParsedPage, TextLine

logger = logging.getLogger(__name__)

# Heading heuristic: size must exceed body median by this factor AND by at
# least this absolute delta (so 0.1pt numeric noise is not a heading).
_HEADING_SIZE_FACTOR = 1.15
_HEADING_MIN_DELTA = 1.0
# Paragraph-break heuristic: gap > this multiple of the estimated line height.
_PARAGRAPH_GAP_FACTOR = 1.5
# Gap ratios at or below this are treated as "within-paragraph" line spacing.
_TIGHT_GAP_FACTOR = 1.6
# Fallback line height when no layout evidence exists (typical body pt size).
_FALLBACK_LINE_HEIGHT = 15.0
# Fallback body-line spacing factor when no tight gaps are observed.
_SPACING_FACTOR = 1.5


class Segmenter:
    """Split parsed pages into paragraph segments with headings and pages."""

    def segment(self, pages: Sequence[ParsedPage]) -> list[Segment]:
        """Return paragraph segments for the parsed pages (in reading order)."""
        lines: list[tuple[int, float, TextLine]] = []
        for parsed_page in pages:
            for index, line in enumerate(parsed_page.lines):
                top = line.top if line.top is not None else float(index)
                lines.append((parsed_page.page, top, line))
        lines.sort(key=lambda entry: (entry[0], entry[1]))
        if not lines:
            return []

        sizes = [line.size for _, _, line in lines if line.size]
        body_median = statistics.median(sizes) if sizes else None
        line_height = self._estimate_line_height(lines, body_median)
        gap_threshold = max(
            _PARAGRAPH_GAP_FACTOR * line_height,
            body_median * _HEADING_SIZE_FACTOR if body_median else 0.0,
        )

        segments: list[Segment] = []
        current_heading: str | None = None
        paragraph: list[str] = []
        paragraph_page = lines[0][0]
        prev_page = lines[0][0]
        prev_top = lines[0][1]

        def flush() -> None:
            nonlocal paragraph
            if not paragraph:
                return
            segments.append(
                Segment(
                    heading=current_heading,
                    text=" ".join(paragraph),
                    page=paragraph_page,
                )
            )
            paragraph = []

        for page, top, line in lines:
            if self._is_heading(line, body_median):
                flush()
                current_heading = line.text
                prev_page, prev_top = page, top
                continue
            if paragraph and (page != prev_page or top - prev_top > gap_threshold):
                flush()
            if not paragraph:
                paragraph_page = page
            paragraph.append(line.text)
            prev_page, prev_top = page, top
        flush()
        return segments

    @staticmethod
    def _is_heading(line: TextLine, body_median: float | None) -> bool:
        """Return True when *line* looks like a heading (size/font heuristic)."""
        if line.font and "bold" in line.font.lower():
            return True
        if line.size is None or body_median is None:
            return False
        return (
            line.size > body_median * _HEADING_SIZE_FACTOR
            and line.size - body_median >= _HEADING_MIN_DELTA
        )

    @staticmethod
    def _estimate_line_height(
        lines: Sequence[tuple[int, float, TextLine]],
        body_median: float | None,
    ) -> float:
        """Estimate the typical within-paragraph line spacing.

        The font-size estimate (``body_median × _SPACING_FACTOR``) anchors the
        search: only gaps at most ``_TIGHT_GAP_FACTOR`` above it can be real
        line spacing (PDFs where every paragraph is a single line have *no*
        within-paragraph gaps, so all observed gaps are paragraph gaps and
        must not bias the estimate). When no such gap exists the font-based
        estimate is used directly.
        """
        positive: list[float] = []
        for index in range(1, len(lines)):
            if lines[index][0] != lines[index - 1][0]:
                continue
            gap = lines[index][1] - lines[index - 1][1]
            if gap > 0:
                positive.append(gap)
        font_based = (
            body_median * _SPACING_FACTOR if body_median else _FALLBACK_LINE_HEIGHT
        )
        if not positive:
            return font_based
        tight = [gap for gap in positive if gap <= font_based * _TIGHT_GAP_FACTOR]
        if tight:
            return statistics.median(tight)
        return font_based
