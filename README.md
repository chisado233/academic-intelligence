# Academic Intelligence

A modular Python library for academic data collection, fusion, and analysis.

## Features

- **Multi-source**: Google Scholar, arXiv, Semantic Scholar, OpenAlex, PubMed, IEEE Xplore
- **Evidence tracking**: Every data point records source, timestamp, and confidence
- **Deduplication**: Automatic merging of duplicate records across sources
- **Incremental updates**: Detects changes and updates only what changed
- **Anti-crawl**: Proxy rotation, rate limiting, retry with backoff
- **Pure library**: Import and use programmatically, no web server required

## Installation

```bash
pip install academic-intelligence
```

## Quick Start

```python
import asyncio
from academic_intelligence import AcademicIntelligence

async def main():
    ai = AcademicIntelligence()
    
    # Collect papers by author
    result = await ai.collect_author_papers(
        name="Geoffrey Hinton",
        sources=["google_scholar", "semantic_scholar", "openalex"]
    )
    
    print(f"Found {len(result.papers)} papers")
    print(f"Sources: {result.stats['sources_used']}")
    print(f"Confidence: {result.stats['avg_confidence']:.2f}")

asyncio.run(main())
```

## CLI Usage

```bash
# Collect papers by author
ai collect author "Geoffrey Hinton" --sources gs,ss,openalex --output papers.json

# Collect paper by DOI
ai collect paper "10.1038/nature14539" --sources all --output paper.json

# Query stored data
ai query papers --author "Hinton" --year 2020-2024 --limit 10
```

## Architecture

```
academic_intelligence/
├── core/           # Models, types, exceptions
├── sources/        # Data source plugins
├── collectors/   # Collection orchestration
├── processors/     # Deduplication, enrichment, validation
├── storage/        # SQLite/JSON backends
├── utils/          # HTTP, proxy, rate limiter, retry, cache
└── cli.py          # Command-line interface
```

## Development

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run tests
pytest

# Run linting
ruff check .
mypy academic_intelligence

# Build docs
mkdocs serve
```

## License

MIT License
