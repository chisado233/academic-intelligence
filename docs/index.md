# Academic Intelligence

A modular Python library for academic data collection, fusion, and analysis.

**Academic Intelligence** is a pure Python library and CLI for collecting scholarly data from multiple sources, tracking provenance with evidence chains, deduplicating and merging records, and storing results in SQLite or JSON.

---

## Features

<div class="grid cards" markdown>

-   :material-database-search: **Multi-source collection**

    ---

    Google Scholar, arXiv, Semantic Scholar, OpenAlex, PubMed, IEEE Xplore — query in parallel and fuse results.

-   :material-link-variant: **Evidence tracking**

    ---

    Every data point records source, URL, timestamp, confidence score, and optional raw payload.

-   :material-content-duplicate: **Deduplication**

    ---

    Automatic merging of duplicate papers and authors across sources using DOI, title, and author+year similarity.

-   :material-update: **Incremental updates**

    ---

    Detect changes (new papers, updated citations, affiliations) and update only what changed.

-   :material-shield-lock: **Anti-crawl tooling**

    ---

    Proxy rotation, rate limiting, retry with exponential backoff, and adaptive delay.

-   :material-package-variant: **Pure library**

    ---

    Import and use programmatically — no web server required. Optional CLI via the `ai` entry point.

</div>

---

## Quick navigation

| Section | What you'll find |
|---------|------------------|
| [Installation](getting-started/installation.md) | pip install, editable dev setup, optional extras |
| [Quick Start](getting-started/quick-start.md) | First collection script and CLI examples |
| [Configuration](getting-started/configuration.md) | Config model, env vars, anti-crawl options |
| [Core Concepts](user-guide/core-concepts.md) | Evidence chain, fusion, incremental updates |
| [API Reference](api/core-models.md) | Auto-generated Python API docs |
| [Architecture](development/architecture.md) | Module layout and data flow |

---

## Installation

```bash
pip install academic-intelligence
```

For documentation tools locally:

```bash
pip install -e ".[docs]"
```

---

## Quick example

```python
import asyncio
from academic_intelligence import AcademicIntelligence

async def main():
    async with AcademicIntelligence() as ai:
        result = await ai.collect_author_papers(
            name="Geoffrey Hinton",
            sources=["semantic_scholar", "openalex"],
        )
        print(f"Found {len(result.papers)} papers")
        print(f"Sources: {result.stats.get('sources_used')}")
        if result.stats.get("avg_confidence") is not None:
            print(f"Confidence: {result.stats['avg_confidence']:.2f}")

asyncio.run(main())
```

### CLI

```bash
# Collect papers by author
ai collect author "Geoffrey Hinton" --sources ss,openalex --output papers.json

# Collect paper by DOI
ai collect paper "10.1038/nature14539" --sources all --output paper.json

# Query stored data
ai query papers --author "Hinton" --year 2020-2024 --limit 10
```

---

## Architecture overview

```
academic_intelligence/
├── core/           # Models, types, exceptions
├── sources/        # Data source plugins
├── collectors/     # Collection orchestration
├── processors/     # Deduplication, enrichment, validation
├── storage/        # SQLite / JSON backends
├── utils/          # HTTP, proxy, rate limiter, retry, cache
└── cli.py          # Command-line interface
```

See [Architecture](development/architecture.md) for module boundaries and extension points.

---

## License

MIT License
