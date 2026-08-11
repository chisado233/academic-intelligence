# Storage

Collected data can be persisted through the `BaseStorage` interface. Two backends ship with the library: **SQLite** (default) and **JSON files**.

## Choosing a backend

| Backend | `storage_type` | `storage_path` | Best for |
|---------|----------------|----------------|----------|
| SQLite | `"sqlite"` | Path to `.db` file | Queryable local datasets, default |
| JSON | `"json"` | Directory path | Simple dumps, debugging, portable folders |

```python
from academic_intelligence import AcademicIntelligence, Config

# SQLite (default)
config = Config(storage_type="sqlite", storage_path="./academic_intelligence.db")

# JSON directory
config = Config(storage_type="json", storage_path="./data/json_store")

async with AcademicIntelligence(config) as ai:
    ...
```

## SQLite storage

- Implementation: `academic_intelligence.storage.sqlite_store.SQLiteStorage`
- Uses SQLAlchemy async + `aiosqlite`
- Tables: `papers`, `authors`, `citations`, `authorships`, `coauthorships`, `evidence` (one row per confirming source), plus `paper_hashes` / `source_updates` / `entity_sync` for incremental tracking (the `entity_sync` table records per-`(entity, source)` refresh gating, FIX-B2); query indexes: `paper_author_tokens` (B-tree) + `paper_author_names_fts` (FTS5 trigram) serving `query_papers(author=...)` and `paper_text_fts` (FTS5 trigram) serving `query_papers(keyword=...)`, all auto-maintained on write paths and auto-backfilled on connect; v2 columns are auto-migrated for pre-existing databases

```bash
paper collect author "Name" --storage-type sqlite --storage-path ./my.db --persist
paper query papers --storage-path ./my.db --author "Name"
paper stats --storage-path ./my.db
```

### Typical lifecycle

```python
async with AcademicIntelligence(Config(storage_path="./my.db")) as ai:
    await ai.collect_author_papers("Geoffrey Hinton", persist=True)
    papers = await ai.query_papers(author="Hinton", year_from=2015, limit=50)
    stats = await ai.get_stats()
```

## JSON storage

- Implementation: `academic_intelligence.storage.json_store.JSONStorage`
- Intended for single-process/single-writer, small-dataset use; a second live `JSONStorage` instance for the same resolved directory fails closed with `StorageError`
- There is no cross-process writer lock; use SQLite for concurrent processes or larger libraries
- `store.json` is the atomically replaced source-of-truth snapshot; blocking disk I/O is delegated off the event loop
- Historical `authors.json` / `papers.json` / relation files remain readable as migration inputs and are refreshed as inspection mirrors on clean close
- Citation identity is the directed `(citing_paper_id, cited_paper_id)` pair, and coauthorship counts are derived from current authorships, making batch replay and byline replacement idempotent
- Writes after `close()` raise `StorageError`; use the async context manager or reconnect explicitly

```python
config = Config(storage_type="json", storage_path="./data/export")
```

## Public storage operations

Accessed via `ai.storage` after connect, or convenience methods on `AcademicIntelligence`:

| Operation | Description |
|-----------|-------------|
| `save_author` / `save_paper` / save citation helpers | Single-record writes |
| `save_batch(authors=..., papers=..., citations=...)` | Batch persist (used by `persist=True`) |
| `query_papers(...)` | Filter by author, year, venue, keyword |
| `query_authors(...)` | Filter by name, affiliation, interest |
| `get_stats()` | Counts and backend metrics |
| `connect()` / `close()` | Lifecycle (handled by context manager) |

See [API: Storage](../api/storage.md) for full signatures.

## Custom storage backend

Implement `BaseStorage` and plug it in by subclassing or replacing the factory (advanced):

```python
from academic_intelligence.storage.base import BaseStorage

class MyStorage(BaseStorage):
    backend_name = "my_store"

    async def connect(self) -> None: ...
    async def close(self) -> None: ...
    async def save_author(self, author): ...
    # ... implement remaining abstract methods
```

Minimal requirements:

1. Durable save/update/delete for authors, papers, citations
2. Query methods used by CLI and library helpers
3. Raise `StorageError` / `RecordNotFoundError` on failures

Wire-in options:

- Fork `_build_storage()` in a subclass of `AcademicIntelligence`
- Or use your backend only from application code after collection (call `save_batch` yourself)

## Data migration

There is no automatic migrator between backends in v0.1. A practical approach:

1. Collect or load from the source backend into models
2. Open a target backend
3. `save_batch` authors, papers, citations

Sketch:

```python
from academic_intelligence import AcademicIntelligence, Config

async def migrate_sqlite_to_json(src: str, dst: str):
    async with AcademicIntelligence(Config(storage_type="sqlite", storage_path=src)) as src_ai:
        papers = await src_ai.query_papers(limit=10_000)
        authors = await src_ai.query_authors(limit=10_000)
        # citations: use backend-specific APIs if exposed, or re-collect

    async with AcademicIntelligence(Config(storage_type="json", storage_path=dst)) as dst_ai:
        await dst_ai.storage.save_batch(authors=authors, papers=papers, citations=[])
```

For schema evolution of SQLite, prefer export → re-import after model changes, or manage Alembic yourself if you embed the DB in a larger app.

## Tips

- Use absolute paths in production to avoid CWD surprises
- Keep secrets out of the DB; evidence `raw_data` may contain API payloads — scrub if needed
- `paper stats` is a quick health check for empty vs populated stores

## Related

- [Collection](collection.md)
- [CLI](cli.md)
- [API: Storage](../api/storage.md)
