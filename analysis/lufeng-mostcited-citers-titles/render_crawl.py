"""Render-crawl a JS page via the library WebCrawler with browser fetcher enabled.

Usage: python render_crawl.py <url> <out.json>
Respects robots + polite rate limits (skill §3.5); scrapling optional dep.
"""
import asyncio
import json
import sys

sys.path.insert(0, ".")

from academic_intelligence.webcrawler.crawler import WebCrawler


async def main(url: str, out: str) -> None:
    async with WebCrawler(enable_browser=True) as crawler:
        result = await crawler.crawl(url, use_browser=True)
        payload = {
            "url": url,
            "status": getattr(result, "status", None),
            "title": getattr(result, "title", None),
            "content": getattr(result, "content", None) or "",
            "links": getattr(result, "links", None) or [],
            "notes": getattr(result, "notes", None),
        }
        with open(out, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
        print("status:", payload["status"], "| title:", payload["title"], "| chars:", len(payload["content"]))
        print("notes:", payload["notes"])


asyncio.run(main(sys.argv[1], sys.argv[2]))
