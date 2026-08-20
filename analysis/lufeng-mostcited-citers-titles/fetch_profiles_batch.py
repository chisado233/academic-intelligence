"""Batch-enrich citing authors via S2 /author/batch (<=1000 ids/request).

Polite: 4 requests total, bounded 429 back-off (same semantics as the
library's adapters — no unbounded retry). Output profiles.csv keeps the
trace-profiles column contract plus citations/homepage/appears_in.
"""
import asyncio
import csv
import json
import sys

sys.path.insert(0, ".")

from academic_intelligence.core.exceptions import RateLimitError
from academic_intelligence.sources.semantic_scholar import SemanticScholarSource

FIELDS = "name,affiliations,paperCount,citationCount,hIndex,homepage"


async def batch_get(src, ids):
    for attempt in range(1, 13):
        try:
            client = await src._client()
            resp = await client.post(
                "https://api.semanticscholar.org/graph/v1/author/batch",
                headers=src._headers(),
                params={"fields": FIELDS},
                json={"ids": ids},
            )
            if resp.status_code == 429:
                raise RateLimitError("S2 429", source_name="semantic_scholar",
                                     retry_after=int(resp.headers.get("Retry-After", "20") or 20))
            resp.raise_for_status()
            return resp.json()
        except RateLimitError as e:
            wait = (e.retry_after or 0) + 15
            print(f"429; sleep {wait}s (attempt {attempt})", flush=True)
            await asyncio.sleep(wait)
        except Exception as e:
            print(f"err {type(e).__name__}: {e}; retry in 15s", flush=True)
            await asyncio.sleep(15)
    raise RuntimeError("12 consecutive failed batch attempts — stop politely")


async def main():
    rows = list(csv.DictReader(open(
        "analysis/lufeng-mostcited-citers-titles/authors.csv", encoding="utf-8-sig")))
    # aggregate by author_id (name variants merged only when S2 id identical)
    agg = {}
    for r in rows:
        aid = r["author_id"] or ""
        key = aid or "NAME::" + r["author_name"]
        a = agg.setdefault(key, {"author_id": aid or None, "names": set(), "n_papers": 0})
        a["names"].add(r["author_name"])
        a["n_papers"] += len([p for p in r["appears_in"].split(";") if p])
    print(f"{len(rows)} rows -> {len(agg)} unique authors")

    src = SemanticScholarSource()
    profiles = {}
    ids = [k for k in agg if not k.startswith("NAME::")]
    try:
        for i in range(0, len(ids), 1000):
            chunk = ids[i:i + 1000]
            data = await batch_get(src, chunk)
            payload = data if isinstance(data, list) else (data.get("data") or [])
            for k, v in zip(chunk, payload):
                if isinstance(v, dict):
                    profiles[k] = v
            print(f"batch {i // 1000 + 1}: +{sum(1 for k in chunk if k in profiles)}/{len(chunk)}", flush=True)
            await asyncio.sleep(3)
    finally:
        await src.close()

    out = []
    for key, a in agg.items():
        p = profiles.get(key) or {}
        out.append({
            "author_name": sorted(a["names"], key=len, reverse=True)[0],
            "author_id": a["author_id"],
            "institution": "; ".join(p.get("affiliations") or [])[:180],
            "h_index": p.get("hIndex"),
            "citations": p.get("citationCount"),
            "works_count": p.get("paperCount"),
            "homepage": p.get("homepage") or "",
            "appears_in": a["n_papers"],
            "source": "s2" if p else "",
        })
    out.sort(key=lambda r: -(r["citations"] or 0))
    path = "analysis/lufeng-mostcited-citers-titles/profiles.csv"
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(out[0].keys()))
        w.writeheader()
        w.writerows(out)
    print(f"wrote {len(out)} -> {path}; with profile: {sum(1 for r in out if r['source'])}")


asyncio.run(main())
