"""Citation tracing primitives (reverse-citation pull).

Public surface:

- :func:`~academic_intelligence.trace.citing.fetch_citing_papers` — pull
  papers citing a given paper from OpenAlex + OpenCitations (COCI), merged
  and deduplicated by ``citing_paper_id``;
- :class:`~academic_intelligence.trace.citing.CitingResult` — aggregated
  result (papers / resume cursor / per-source stats / fail-soft errors);
- :class:`~academic_intelligence.trace.citing.CitingPaper` — a citing
  paper record (W-id or citing DOI, plus OpenAlex metadata when available).
"""

from __future__ import annotations

from academic_intelligence.trace.citing import (
    CitingPaper,
    CitingResult,
    fetch_citing_papers,
)

__all__ = [
    "CitingPaper",
    "CitingResult",
    "fetch_citing_papers",
]
