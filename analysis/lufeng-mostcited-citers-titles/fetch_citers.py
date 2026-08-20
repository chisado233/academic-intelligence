"""Fetch all S2 citing papers of MBLLEN via the library's own polite primitive.

Uses SemanticScholarSource._get_json (built-in 429 -> RateLimitError with
retry_after) with a bounded explicit back-off loop per SKILL §4.1 (no
unbounded outer retries). Output CSV matches `paper trace-authors` input
schema (citing_paper_id/doi/title/year/venue/authors_raw/authors_detail).
"""
import asyncio
import csv
import json
import sys

sys.path.insert(0, ".")

from academic_intelligence.core.exceptions import RateLimitError
from academic_intelligence.sources.semantic_scholar import SemanticScholarSource

SEED = "70cb4bdd05cccc1f99cf690582e66b7637b81da7"
FIELDS = (
    "citingPaper.paperId,citingPaper.title,citingPaper.year,citingPaper.venue,"
    "citingPaper.externalIds,citingPaper.citationCount,citingPaper.authors"
)


async def get_page(src, offset):
    for attempt in range(1, 11):
        try:
            return await src._get_json(
                f"/paper/{SEED}/citations",
                params={"limit": 1000, "offset": offset, "fields": FIELDS},
            )
        except RateLimitError as e:
            wait = (e.retry_after or 0) + 15
            print(f"429; sleep {wait}s (attempt {attempt})", flush=True)
            await asyncio.sleep(wait)
    raise RuntimeError("10 consecutive rate-limited attempts — stop politely")


async def main():
    src = SemanticScholarSource()
    rows = []
    offset = 0
    try:
        while True:
            data = await get_page(src, offset)
            if not data:
                break
            items = data.get("data") or []
            for item in items:
                c = item.get("citingPaper") or {}
                pid = c.get("paperId")
                if not pid or pid == SEED:
                    continue
                authors = [
                    {"id": a.get("authorId"), "display_name": a.get("name"), "institutions": []}
                    for a in (c.get("authors") or [])
                ]
                ext = c.get("externalIds") or {}
                rows.append(
                    {
                        "citing_paper_id": pid,
                        "doi": ext.get("DOI") or "",
                        "title": c.get("title") or "",
                        "year": c.get("year") or "",
                        "venue": c.get("venue") or "",
                        "cites": c.get("citationCount") or 0,
                        "authors_raw": ", ".join(a["display_name"] or "" for a in authors),
                        "authors_detail": json.dumps(authors, ensure_ascii=False, separators=(",", ":")),
                    }
                )
            print(f"offset {offset}: +{len(items)} (total {len(rows)})", flush=True)
            nxt = data.get("next")
            if not nxt or not items:
                break
            offset = nxt
            await asyncio.sleep(2)
    finally:
        await src.close()

    out = "analysis/lufeng-mostcited-citers-titles/citing.csv"
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {len(rows)} citing papers -> {out}")


asyncio.run(main())
