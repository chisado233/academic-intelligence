---
name: academic-intelligence
description: Use when collecting, querying, or analyzing academic information such as papers, authors, citations, venues, or research trends. Use when integrating scholarly data from multiple sources (Google Scholar, arXiv, Semantic Scholar, OpenAlex) or building academic data pipelines. Use when needing evidence-tracked, confidence-scored academic data with deduplication and incremental updates.
---

# Academic Intelligence

A modular Python library and CLI for academic data collection, fusion, and analysis. Provides multi-source paper/author/citation acquisition with evidence tracking, confidence scoring, deduplication, and incremental updates.

## Overview

Academic Intelligence is a **reusable Python skill** for collecting and processing academic data. Unlike monolithic web platforms, it is:

- **Pure library**: Import and use programmatically, no web server required
- **Multi-source**: Google Scholar, arXiv, Semantic Scholar, OpenAlex, PubMed, IEEE Xplore
- **Evidence-tracked**: Every data point records source, timestamp, and confidence score
- **Deduplicated**: Automatic merging of duplicate records across sources
- **Incremental**: Detects changes and updates only what changed

## When to Use

- Collecting paper metadata, author profiles, or citation networks
- Building academic datasets for research analysis
- Integrating scholarly data into existing pipelines
- Needing confidence-scored, evidence-tracked academic data
- Performing cross-source verification of paper/author information

## When NOT to Use

- Real-time web scraping at scale (respect rate limits)
- Bypassing paywalls or terms of service
- Building a full web application (use as library, not platform)

## Quick Reference

### Installation

```bash
pip install academic-intelligence
```

### Basic Usage

```python
import asyncio
from academic_intelligence import AcademicIntelligence

async def main():
    ai = AcademicIntelligence()
    
    # Collect author papers from multiple sources
    result = await ai.collect_author_papers(
        name="Geoffrey Hinton",
        sources=["google_scholar", "semantic_scholar", "openalex"]
    )
    
    print(f"Found {len(result.papers)} papers")
    print(f"Sources: {result.stats['sources_used']}")
    print(f"Confidence: {result.stats['avg_confidence']:.2f}")

asyncio.run(main())
```

### CLI Usage

```bash
# Collect papers by author
ai collect author "Geoffrey Hinton" --sources gs,ss,openalex --output papers.json

# Collect paper by DOI
ai collect paper "10.1038/nature14539" --sources all --output paper.json

# Query stored data
ai query papers --author "Hinton" --year 2020-2024 --limit 10
```

## Core Concepts

### Evidence Chain

Every data point carries an `Evidence` object:

```python
class Evidence:
    source: SourceType           # Which source provided this data
    source_url: str              # Original URL
    collected_at: datetime       # Timestamp
    confidence: float            # 0.0-1.0 confidence score
    raw_data: Optional[dict]     # Original raw response
```

This enables:
- **Source tracing**: Know exactly where each data point came from
- **Confidence scoring**: Weight data by source reliability
- **Auditability**: Full provenance for every field

### Multi-Source Fusion

When collecting from multiple sources, the system:

1. **Collects** from each configured source in parallel
2. **Deduplicates** using title/DOI/author+year similarity
3. **Merges** fields using confidence-weighted strategy
4. **Validates** merged result against schema
5. **Scores** overall confidence based on source agreement

### Incremental Updates

For recurring collections, the system:

1. Compares new data against stored records
2. Detects changes (new papers, updated citations, changed affiliations)
3. Updates only changed fields with new evidence
4. Maintains history of changes

## Architecture

```
academic_intelligence/
├── core/           # Models, types, exceptions
├── sources/        # Data source plugins
│   ├── base.py     # Abstract base class
│   ├── google_scholar.py
│   ├── arxiv.py
│   ├── semantic_scholar.py
│   ├── openalex.py
│   ├── pubmed.py
│   └── ieee.py
├── collectors/     # High-level collection orchestration
├── processors/     # Deduplication, enrichment, validation
├── storage/        # SQLite/JSON storage backends
└── cli.py          # Command-line interface
```

### Adding a New Source

Implement the `BaseSource` abstract class:

```python
from academic_intelligence.sources.base import BaseSource

class MySource(BaseSource):
    name = "my_source"
    
    async def search_papers(self, query: str, limit: int = 10) -> List[Paper]:
        # Implementation
        pass
    
    async def get_author_papers(self, author_name: str) -> List[Paper]:
        # Implementation
        pass
    
    async def get_paper_by_doi(self, doi: str) -> Optional[Paper]:
        # Implementation
        pass
```

## Configuration

```python
from academic_intelligence import Config

config = Config(
    # Sources to use (ordered by priority)
    sources=["google_scholar", "semantic_scholar", "openalex"],
    
    # Rate limiting
    rate_limit=1.0,  # requests per second
    
    # Proxy settings
    proxy="http://proxy:8080",
    
    # Storage
    storage_type="sqlite",  # or "json"
    storage_path="./data.db",
    
    # Confidence thresholds
    min_confidence=0.5,
)

ai = AcademicIntelligence(config)
```

## Data Models

### Author

| Field | Type | Description |
|-------|------|-------------|
| id | str | Unique identifier |
| name | str | Full name |
| affiliation | str | Current institution |
| h_index | int | H-index |
| citations | int | Total citations |
| interests | List[str] | Research interests |
| profile_url | str | Profile page URL |
| evidence | Evidence | Source evidence |

### Paper

| Field | Type | Description |
|-------|------|-------------|
| id | str | Unique identifier |
| title | str | Paper title |
| authors | List[str] | Author names |
| year | int | Publication year |
| venue | str | Journal/conference |
| abstract | str | Abstract text |
| doi | str | DOI |
| url | str | Paper URL |
| pdf_url | str | PDF link |
| citations | int | Citation count |
| keywords | List[str] | Keywords |
| evidence | Evidence | Source evidence |

## Common Patterns

### Collect and Store

```python
# Collect papers and persist to database
result = await ai.collect_author_papers("Geoffrey Hinton")
ai.storage.save(result)

# Later, query stored data
papers = ai.storage.query_papers(author="Hinton", year=2020)
```

### Cross-Source Verification

```python
# Collect from multiple sources and compare
result = await ai.collect_paper("10.1038/nature14539", sources="all")

for paper in result.papers:
    print(f"Title: {paper.title}")
    print(f"Confidence: {paper.evidence.confidence}")
    print(f"Sources: {paper.evidence.source}")
```

### Incremental Update

```python
# First collection
await ai.collect_author_papers("Geoffrey Hinton")

# Later, incremental update (only fetches changes)
changes = await ai.update_author_papers("Geoffrey Hinton")
print(f"New papers: {len(changes.new)}")
print(f"Updated: {len(changes.updated)}")
```

## Error Handling

```python
from academic_intelligence import AcademicIntelligence, errors

try:
    result = await ai.collect_author_papers("Unknown Author")
except errors.SourceUnavailableError:
    # All sources failed
    pass
except errors.RateLimitError:
    # Hit rate limit, retry with backoff
    pass
except errors.DataValidationError as e:
    # Data didn't pass validation
    print(e.details)
```

## Performance Tips

1. **Use async**: All collection methods are async for concurrent source queries
2. **Configure rate limits**: Respect source rate limits to avoid blocks
3. **Enable caching**: Repeated queries are cached automatically
4. **Batch operations**: Use bulk methods for multiple authors/papers
5. **Selective sources**: Only enable sources you need

## Limitations

- **Rate limits**: Academic sources have strict rate limits
- **Data coverage**: Not all papers are indexed by all sources
- **Accuracy**: Confidence scores indicate uncertainty
- **Legal**: Respect terms of service of each source

## Contributing

See `CONTRIBUTING.md` for development setup and guidelines.

## License

MIT License
