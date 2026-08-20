"""Fetch the full citing-paper list of MBLLEN via the skill's S2 adapter.

Uses academic_intelligence.sources.semantic_scholar.SemanticScholarSource
(SKILL.md §4 Python-library form) because the CLI surface caps citations at 50
without pagination and without author fields (known limitation).
Polite: single source, backoff on RateLimitError, no unbounded retries.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from academic_intelligence.core.exceptions import RateLimitError, SourceUnavailableError
from academic_intelligence.sources.semantic_scholar import SemanticScholarSource

PAPER_ID = "70cb4bdd05cccc1f99cf690582e66b7637b81da7"  # MBLLEN
OUT = Path(__file__).with_name("s2_citing_full.json")

FIELDS = (
    "citingPaper.paperId,citingPaper.title,citingPaper.year,citingPaper.venue,"
    "citingPaper.citationCount,citingPaper.externalIds,citingPaper.authors"
)
PAGE = 1000  # S2 citations endpoint maximum per page


async def main() -> None:
    src = SemanticScholarSource()
    rows: list[dict] = []
    offset = 0
    attempts = 0
    try:
        while True:
            data = None
            while data is None:
                attempts += 1
                if attempts > 60:
                    raise SystemExit("too many throttled attempts, giving up")
                try:
                    data = await src._get_json(
                        f"/paper/{PAPER_ID}/citations",
                        params={"limit": PAGE, "offset": offset, "fields": FIELDS},
                    )
                except RateLimitError:
                    wait = 75
                    print(f"429 at offset={offset}; sleeping {wait}s "
                          f"(attempt {attempts})", flush=True)
                    await asyncio.sleep(wait)
                except SourceUnavailableError as exc:
                    print(f"unavailable: {exc}; sleeping 75s", flush=True)
                    await asyncio.sleep(75)
            items = data.get("data") or []
            nxt = (data.get("next") or 0)
            for item in items:
                cp = item.get("citingPaper") or {}
                if not cp.get("paperId") or cp["paperId"] == PAPER_ID:
                    continue
                rows.append(
                    {
                        "paperId": cp.get("paperId"),
                        "title": cp.get("title"),
                        "year": cp.get("year"),
                        "venue": cp.get("venue"),
                        "citationCount": cp.get("citationCount"),
                        "externalIds": cp.get("externalIds"),
                        "authors": [
                            {"id": a.get("authorId"), "name": a.get("name")}
                            for a in cp.get("authors") or []
                        ],
                    }
                )
            print(f"offset={offset}: page {len(items)} rows, total {len(rows)}, "
                  f"next={nxt}", flush=True)
            if not items or not nxt or nxt <= offset:
                break
            offset = nxt
            await asyncio.sleep(2)
    finally:
        await src.close()
        OUT.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"wrote {len(rows)} rows -> {OUT}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
