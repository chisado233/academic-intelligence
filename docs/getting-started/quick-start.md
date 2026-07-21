# Quick Start

This guide walks through your first collection with the Python API and the CLI.

## Prerequisites

```bash
pip install academic-intelligence
```

For Google Scholar via SerpAPI, set an API key (optional for Semantic Scholar / OpenAlex demos):

```bash
# Windows PowerShell
$env:SERPAPI_KEY = "your-key"
$env:SEMANTIC_SCHOLAR_API_KEY = "your-key"   # optional, higher limits
$env:OPENALEX_EMAIL = "you@example.com"      # polite pool
```

## First collection (Python)

Use the async context manager so HTTP clients and storage are cleaned up:

```python
import asyncio
from academic_intelligence import AcademicIntelligence, Config

async def main():
    config = Config(
        sources=["semantic_scholar", "openalex"],
        storage_type="sqlite",
        storage_path="./academic_intelligence.db",
    )
    async with AcademicIntelligence(config) as ai:
        result = await ai.collect_author_papers(
            name="Geoffrey Hinton",
            sources=["semantic_scholar", "openalex"],
            persist=True,  # save to storage
        )
        print(f"Papers: {len(result.papers)}")
        print(f"Authors: {len(result.authors)}")
        if result.errors:
            for err in result.errors:
                print(f"Warning: {err}")

        for paper in result.papers[:5]:
            conf = paper.evidence.confidence
            print(f"- [{paper.year}] {paper.title[:70]} (conf={conf:.2f})")

asyncio.run(main())
```

### Collect a single paper by DOI or title

```python
async with AcademicIntelligence() as ai:
    result = await ai.collect_paper(
        "10.1038/nature14539",
        sources=["semantic_scholar", "openalex"],
        limit=5,
    )
    for p in result.papers:
        print(p.title, p.doi, p.evidence.source)
```

### Query stored records

```python
async with AcademicIntelligence() as ai:
    papers = await ai.query_papers(author="Hinton", year_from=2020, year_to=2024, limit=10)
    stats = await ai.get_stats()
    print(stats)
```

## CLI quick usage

```bash
# Author papers → JSON file
ai collect author "Geoffrey Hinton" \
  --sources ss,openalex \
  --output papers.json \
  --persist

# Paper by DOI
ai collect paper "10.1038/nature14539" --sources all --output paper.json

# Query local DB
ai query papers --author "Hinton" --year 2020-2024 --limit 10

# Storage statistics
ai stats
```

Source aliases for `--sources`:

| Alias | Source |
|-------|--------|
| `gs` | Google Scholar |
| `ss` / `s2` | Semantic Scholar |
| `oa` / `openalex` | OpenAlex |
| `all` | All configured sources |

## Common use cases

### 1. Build a local author dataset

```python
async with AcademicIntelligence(Config(storage_path="./hinton.db")) as ai:
    await ai.collect_author_papers("Geoffrey Hinton", persist=True)
    await ai.collect_author_papers("Yoshua Bengio", persist=True)
    print(await ai.get_stats())
```

### 2. Cross-source verification of one paper

```python
async with AcademicIntelligence() as ai:
    result = await ai.collect_paper("Attention is All You Need", sources=["all"], limit=10)
    for p in result.papers:
        print(p.evidence.source, p.citations, p.venue)
```

### 3. Export without persistence

Omit `--persist` / `persist=True` and write only to `--output` / process `CollectionResult` in memory.

## Error handling sketch

```python
from academic_intelligence import AcademicIntelligence, errors

try:
    async with AcademicIntelligence() as ai:
        result = await ai.collect_author_papers("Unknown Author")
except errors.SourceUnavailableError:
    print("All sources unreachable")
except errors.RateLimitError:
    print("Rate limited — retry later")
except errors.DataValidationError as e:
    print("Validation failed:", e)
```

## Next steps

- [Configuration](configuration.md) — rate limits, proxies, API keys
- [Core Concepts](../user-guide/core-concepts.md) — evidence chain and fusion
- [CLI](../user-guide/cli.md) — full command reference
