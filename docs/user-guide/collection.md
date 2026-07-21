# Collection

Collection is the path from a **query** (author name, title, DOI, paper id) to a **`CollectionResult`**, optionally persisted to storage.

## Workflow

```text
Input (name / DOI / title / paper_id)
        │
        ▼
  AcademicIntelligence  ── connect HTTP + sources + storage
        │
        ▼
  MultiSourceCollector
        │
        ├─► Source A (async)
        ├─► Source B (async)
        └─► Source C (async)
        │
        ▼
  Deduplicator → Enricher → Validator
        │
        ▼
  CollectionResult  ── optional save_batch → Storage
```

Entry points on `AcademicIntelligence`:

| Method | Purpose |
|--------|---------|
| `collect_author_papers(name, ...)` | Papers (and author records) for a scholar |
| `collect_paper(query, ...)` | Paper by DOI or title search |
| `collect_citations(paper_id, ...)` | Citation edges for a paper |

Always use `async with AcademicIntelligence(...)` or call `await ai.connect()` / `await ai.close()`.

## Collection strategies

### Parallel multi-source

Default behavior: query configured sources concurrently (bounded by `max_concurrent_sources`).

```python
config = Config(
    sources=["semantic_scholar", "openalex", "google_scholar"],
    max_concurrent_sources=3,
)
```

### Source subset

Pass `sources=` on each call without changing global config:

```python
await ai.collect_author_papers("Name", sources=["semantic_scholar"])
```

### Persist vs in-memory

```python
# Memory only
result = await ai.collect_paper("10.1038/nature14539")

# Write through storage
result = await ai.collect_paper("10.1038/nature14539", persist=True)
```

CLI: `--persist` flag.

### Confidence filtering

```python
Config(min_confidence=0.6)
```

Records below the threshold may be dropped or flagged depending on processor settings.

### Deduplication threshold

```python
Config(deduplication_threshold=0.85)
```

Higher values require closer title similarity before merging.

## Anti-crawl configuration

Anti-crawl lives on `Config.anti_crawl` (`AntiCrawlStrategy`) and is applied through the shared `HTTPClient`:

- **Proxy rotation** — `proxy` / `proxies` / `proxy_pool`
- **Base delay + jitter** — reduce burst patterns
- **Adaptive delay** — slow down under pressure
- **Retries** — exponential backoff on 429/503/504 (configurable)
- **Caching** — `cache_enabled` / `cache_ttl` to avoid repeat hits

```python
from academic_intelligence.core.types import AntiCrawlStrategy, Config

config = Config(
    anti_crawl=AntiCrawlStrategy(
        proxy_pool=["http://proxy1:8080", "http://proxy2:8080"],
        proxy_rotation_interval=10,
        base_delay=1.5,
        adaptive_delay=True,
        jitter=True,
        max_retries=3,
        retry_backoff=2.0,
        retry_on_status=[429, 503, 504],
    ),
    cache_enabled=True,
    cache_ttl=3600,
)
```

!!! warning "Legal & ethical use"
    Respect each source’s terms of service and robots policies. Prefer official APIs. Do not use this library to bypass paywalls or access controls.

## Error handling

Domain errors live in `academic_intelligence.core.exceptions` (imported as `academic_intelligence.errors`).

| Exception | When |
|-----------|------|
| `SourceUnavailableError` | Source down / unreachable |
| `RateLimitError` | Rate limited by source |
| `AuthenticationError` | Bad or missing credentials |
| `ParseError` | Response parse failure |
| `AllSourcesFailedError` | Every source failed |
| `PartialResultError` | Some sources failed (may still have data) |
| `DataValidationError` | Schema / validation failure |
| `StorageError` | Persistence failure |

Soft failures often appear in `CollectionResult.errors` while successful partial data is still returned:

```python
result = await ai.collect_author_papers("Name")
for err in result.errors:
    print("soft error:", err)
```

Hard failures raise and should be caught at the call site:

```python
from academic_intelligence import AcademicIntelligence, errors

try:
    async with AcademicIntelligence() as ai:
        result = await ai.collect_author_papers("Name")
except errors.RateLimitError:
    # back off and retry
    ...
except errors.AllSourcesFailedError:
    ...
```

## Interpreting CollectionResult

```python
result.papers       # list[Paper]
result.authors      # list[Author]
result.citations    # list[Citation]
result.errors       # list[str] soft errors
result.stats        # dict: counts, sources_used, etc.
```

## Related

- [Core Concepts](core-concepts.md)
- [Storage](storage.md)
- [CLI](cli.md)
