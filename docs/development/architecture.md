# Architecture

Academic Intelligence is a **pure Python library** with an optional CLI. There is no web server in the core package: applications import `AcademicIntelligence` or call the `ai` entry point.

## System architecture

```text
┌──────────────────────────────────────────────────────────────┐
│                     CLI (typer)  /  Library API              │
│                  academic_intelligence.cli / __init__          │
└────────────────────────────┬─────────────────────────────────┘
                             │
                             ▼
┌──────────────────────────────────────────────────────────────┐
│                 AcademicIntelligence (facade)                  │
│   connect → sources + HTTP + storage + MultiSourceCollector  │
└───────┬──────────────────┬──────────────────┬────────────────┘
        │                  │                  │
        ▼                  ▼                  ▼
   sources/*          collectors/*        graph/*
   (6 adapters)    (MultiSourceCollector) (KnowledgeGraph,
        │                  │                traversal, cache)
        │                  ▼
        │            processors/*
        │       (dedup / disambiguate / score /
        │        enrich / validate / incremental)
        ▼
     utils/*   (http, proxy, rate_limiter, retry, cache)
        │
        ▼
   core/*   (models, types, exceptions, constants)
```

## Package layout

```text
academic_intelligence/
├── __init__.py          # AcademicIntelligence facade, public exports
├── cli.py               # `ai` console script
├── core/
│   ├── models.py        # Evidence, AuthorRef, Author, Paper, Citation, CollectionResult, ExpandResult
│   ├── types.py         # SourceType, Config, AntiCrawlStrategy
│   ├── exceptions.py    # Error hierarchy
│   └── constants.py
├── sources/
│   ├── base.py          # BaseSource ABC
│   ├── arxiv.py
│   ├── openalex.py
│   ├── semantic_scholar.py
│   ├── pubmed.py
│   ├── ieee.py
│   └── google_scholar.py
├── collectors/
│   └── base.py          # BaseCollector, MultiSourceCollector
├── processors/
│   ├── deduplicator.py
│   ├── disambiguator.py
│   ├── scorer.py
│   ├── enricher.py
│   ├── validator.py
│   └── incremental.py
├── graph/
│   ├── knowledge_graph.py
│   ├── traversal.py
│   └── cache.py
├── storage/
│   ├── base.py
│   ├── sqlite_store.py
│   └── json_store.py
└── utils/
    ├── http.py
    ├── proxy.py
    ├── rate_limiter.py
    ├── retry.py
    └── cache.py
```

## Module boundaries

| Module | Responsibility | Not responsible for |
|--------|----------------|---------------------|
| `core` | Domain models, config types, exceptions | I/O, network |
| `sources` | Talk to one external scholarly API | Cross-source merge |
| `collectors` | Orchestrate multi-source queries | Low-level HTTP details |
| `processors` | Dedup, enrich, validate | Network or storage |
| `storage` | Persist and query records | Collection logic |
| `utils` | Cross-cutting HTTP/rate/retry/cache | Domain rules |
| `cli` | Argument parsing and human output | Business rules beyond calling the library |

## Data flow

```text
User query
   → AcademicIntelligence.collect_*
   → MultiSourceCollector
   → parallel BaseSource methods
   → list[Paper|Author|Citation] + Evidence
   → Deduplicator.merge
   → Enricher.enrich
   → Validator.validate
   → CollectionResult
   → (optional) BaseStorage.save_batch
```

Evidence is attached as early as possible at the source boundary so downstream steps never lose provenance.

## Dependencies (runtime)

| Library | Use |
|---------|-----|
| pydantic | Models & Config |
| httpx | Async HTTP |
| SQLAlchemy + aiosqlite | SQLite backend |
| typer + rich | CLI |

## Extension guide

### Add a new data source

1. Implement `BaseSource` in `sources/my_source.py`
2. Add a `SourceType` value if needed
3. Register construction in `AcademicIntelligence._build_sources`
4. Add tests under `tests/`
5. Document keys and limits in [Data Sources](../user-guide/data-sources.md)

### Add a storage backend

1. Subclass `BaseStorage`
2. Implement connect/close/CRUD/query/stats
3. Hook into `_build_storage` or inject from application code

### Add a processor step

1. Keep pure functions/classes under `processors/`
2. Invoke from `MultiSourceCollector` after raw collection
3. Prefer soft errors collected into `CollectionResult.errors` when partial results remain useful

## Design principles

From the project skill/design brief:

1. **Multi-source first** — never hard-depend on a single provider
2. **Evidence everywhere** — provenance + confidence on records
3. **Library, not platform** — no FastAPI coupling in the core
4. **Incremental-friendly storage** — queryable persistence for change detection
5. **Pluggable adapters** — new sources/backends without rewriting the facade

## Related

- [Contributing](contributing.md)
- [Testing](testing.md)
- [API Reference](../api/core-models.md)
