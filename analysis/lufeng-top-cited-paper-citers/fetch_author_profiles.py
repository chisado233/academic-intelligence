"""Fetch S2 author profiles (affiliations = identity fingerprints) for the
title-verification shortlist, via the skill's S2 adapter request layer."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from academic_intelligence.core.exceptions import RateLimitError, SourceUnavailableError
from academic_intelligence.sources.semantic_scholar import SemanticScholarSource

FIELDS = "name,affiliations,paperCount,hIndex,citationCount,homepage"

SHORTLIST = {
    # name -> s2 author ids to probe (first that resolves wins)
    "Qionghai Dai": ["1491800101", "144954808"],
    "Huadong Ma": ["144258295", "2248034542"],
    "Qingming Huang": ["1689702", "2237597856"],
    "Jiaying Liu": ["2239051902", "41127426"],
    "Risheng Liu": ["2237951125", "34469457"],
    "Wenqi Ren": ["144850642", "2053308882"],
    "Ying Fu": ["143728560", "2107214086"],
    "Zhengjun Zha": ["143962510"],
    "Jianbing Shen": ["145953515"],
    "Ling-yu Duan": ["7667912", "2284844824"],
    "Guangtao Zhai": ["144826390", "2266393212"],
    "Man Zhou": ["2121684731", "2272766397"],
    "Xiongkuo Min": ["2246414"],
    "Yuming Fang": ["2355404388", "1748601102"],
    "Jinyuan Liu": ["2108510456", "2293559619"],
    "Zhiwei Xiong": ["2352456"],
    "Dong Liu": ["2152508413"],
    "Feng Wu": ["144864333"],
    "Jinde Cao": ["2161849202"],
    "Ming-Ming Cheng": ["1557350184"],
    "Wangmeng Zuo": ["2279210262"],
    "Boxin Shi": ["2273929993"],
    "Ryan Wen Liu": ["2268299485", "2124017717"],
    "Wenhan Yang": ["1898172"],
    "Lei Zhang": ["2256832888"],
    "Jinwei Gu": ["143785523"],
    "Ling Shao": ["144082425"],
    "Bo Zhang (DarkVision)": ["2155611061"],
    "Bo Zhang (GDP2023)": ["2141897317"],
}

OUT = Path(__file__).with_name("shortlist_profiles.json")


async def fetch_one(src: SemanticScholarSource, author_id: str):
    data = await src._get_json(
        f"/author/{author_id}", params={"fields": FIELDS}
    )
    return data


async def main() -> None:
    src = SemanticScholarSource()
    result: dict[str, dict] = {}
    queue = [(name, ids) for name, ids in SHORTLIST.items()]
    idx = 0
    attempts = 0
    try:
        while idx < len(queue):
            name, ids = queue[idx]
            got = None
            for aid in ids:
                while True:
                    attempts += 1
                    if attempts > 400:
                        raise SystemExit("too many throttled attempts")
                    try:
                        got = await fetch_one(src, aid)
                        break
                    except RateLimitError:
                        print(f"429 author {aid}; sleep 75s", flush=True)
                        await asyncio.sleep(75)
                    except SourceUnavailableError as exc:
                        print(f"unavailable {aid}: {exc}; sleep 75s", flush=True)
                        await asyncio.sleep(75)
                    except Exception as exc:  # 404 etc. -> permanent
                        print(f"perm fail {aid}: {exc}", flush=True)
                        got = None
                        break
                if got:
                    break
            if got:
                result[name] = {
                    "resolved_id": got.get("authorId"),
                    "name": got.get("name"),
                    "affiliations": got.get("affiliations"),
                    "paperCount": got.get("paperCount"),
                    "hIndex": got.get("hIndex"),
                    "citationCount": got.get("citationCount"),
                    "homepage": got.get("homepage"),
                }
                print(f"[ok] {name} -> {got.get('name')} "
                      f"{(got.get('affiliations') or [])[:1]}", flush=True)
            else:
                result[name] = {"error": "no profile resolved"}
                print(f"[miss] {name}", flush=True)
            idx += 1
            await asyncio.sleep(3)
    finally:
        await src.close()
        OUT.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"wrote {OUT}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
