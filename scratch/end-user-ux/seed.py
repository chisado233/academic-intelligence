"""Seed scratch DB via the documented public API (no network)."""
import asyncio
from academic_intelligence import AcademicIntelligence, Config
from academic_intelligence.core.models import Author, AuthorRef, Evidence, Paper, SourceType


def ev(source, sid, conf):
    return Evidence(
        source=source,
        source_id=sid,
        source_url=f"https://example.org/{sid}",
        confidence=conf,
    )


async def main():
    config = Config(sources=["openalex"], storage_type="sqlite",
                    storage_path="./academic_intelligence.db")
    async with AcademicIntelligence(config) as ai:
        p1 = Paper(
            id="W1",
            title="Attention Is All You Need",
            authors=[AuthorRef(name="Ashish Vaswani", position=1)],
            year=2017, venue="NeurIPS", doi="10.48550/arXiv.1706.03762",
            arxiv_id="1706.03762", citations=50000,
            evidence_list=[ev(SourceType.ARXIV, "1706.03762", 0.95)],
        )
        p2 = Paper(
            id="W2",
            title="Deep Learning",
            authors=[AuthorRef(name="Geoffrey Hinton", position=1)],
            year=2015, venue="Nature", doi="10.1038/nature14539",
            citations=30000,
            evidence_list=[ev(SourceType.OPENALEX, "W2", 0.9)],
        )
        a1 = Author(
            id="A1", name="Geoffrey Hinton", affiliation="University of Toronto",
            h_index=200, evidence_list=[ev(SourceType.OPENALEX, "A1", 0.9)],
        )
        await ai.storage.save_batch(papers=[p1, p2], authors=[a1])
        print("seeded")


asyncio.run(main())
