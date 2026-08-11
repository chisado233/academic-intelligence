# Academic Intelligence

A modular Python library + CLI for multi-source academic data collection, fusion, and recursive knowledge-graph browsing. Pure Python 3.11+, no web server required.

## Features

- **Multi-source**: arXiv, OpenAlex, Semantic Scholar, PubMed, IEEE Xplore, Crossref, Unpaywall, Europe PMC, OpenCitations (COCI), CORE — ten adapters wired in, plus Google Scholar (via SerpAPI, kept but **not registered by default**, see [Known limitations](#known-limitations))
- **Evidence tracking**: every record carries an `evidence_list` (source, source id, URL, timestamp, confidence, raw payload)
- **Deduplication**: automatic cross-source merging via DOI/arXiv/PMID/URL exact match, arXiv↔DOI cross-ID, and SequenceMatcher title similarity, with union-find transitive closure
- **Author disambiguation**: ID direct-link (ORCID / S2 / OpenAlex author ID) + heuristic feature clustering, with `auto` / `ambiguous` / `confirmed` status
- **Confidence scoring**: per-source baselines + multi-source bonus + DOI/PDF/staleness adjustments
- **Recursive knowledge graph**: `expand` / `subgraph` / `path` over papers, authors, citations and co-authorships, with lazy loading and cache
- **Incremental updates**: stale-gated refresh, field-level change detection, only changed data is written
- **Pure library**: import and use programmatically, no web server required

## Installation

```bash
# From PyPI
pip install academic-intelligence

# Development install (editable)
pip install -e ".[dev]"
```

## Quick Start

```python
import asyncio
from academic_intelligence import AcademicIntelligence, Config

async def main():
    config = Config(
        sources=["openalex", "semantic_scholar"],
        storage_type="sqlite",
        storage_path="./academic_intelligence.db",
    )
    async with AcademicIntelligence(config) as ai:
        # Fetch a paper by DOI (multi-source, deduplicated, confidence-scored)
        result = await ai.collect_paper("10.1038/nature14539", persist=True)
        paper = result.papers[0]
        print(paper.title, paper.year, paper.citations)
        print([a.name for a in paper.authors])
        for ev in paper.evidence_list:
            print(f"  {ev.source.value}: conf={ev.confidence:.2f}")

        # Collect an author's papers and profile
        author_result = await ai.collect_author_papers("Geoffrey Hinton")
        for author in author_result.authors:
            print(author.name, author.affiliation, author.h_index)

        # Browse the knowledge graph recursively
        exp = await ai.expand(paper.id, relations=["authors"])
        sub = await ai.subgraph(paper.id, radius=2)
        route = await ai.path(paper.id, author_result.authors[0].id)
        print(f"expanded {len(exp.nodes)} nodes, subgraph {sub['node_count']} nodes")

        # Incremental update (stale-gated)
        changes = await ai.update_author_papers("Geoffrey Hinton")

asyncio.run(main())
```

## CLI Usage

The CLI is installed as the `paper` command. The legacy `ai` name is a shim
that prints `Command 'ai' was renamed to 'paper'` and exits with code 2 — update
scripts and docs to call `paper` directly.

```bash
# Collect papers by author (--persist writes to the local database)
paper collect author "Geoffrey Hinton" --sources openalex,ss --output papers.json --persist

# Collect paper by DOI or title
paper collect paper "10.1038/nature14539" --sources all --output paper.json --persist

# Direct aliases (paper paper == paper collect paper), plus incremental author refresh
paper paper "10.1038/nature14539" --sources all --output paper.json --persist
paper author "Geoffrey Hinton" --sources openalex,ss
paper author-papers "Geoffrey Hinton" --persist
paper update --author "Geoffrey Hinton" --sources openalex

# Collect citation relationships for a paper id (e.g. an OpenAlex W-id)
paper collect citations "W2626778328" --sources openalex --persist

# Query stored data
paper query papers --author "Hinton" --year 2015-2024 --limit 10

# Storage statistics
paper stats

# Query the persisted record and use its internal `id` for graph operations
paper query papers --keyword "Deep learning" --output persisted-papers.json
paper expand "<paper.id>" --relations references,citations,authors --depth 2 --output graph.json

# Export from that snapshot in a later process
paper export --snapshot graph.json --center "<paper.id>" --radius 2 --output subgraph.json

# Stream the paper table without loading the entire result set
paper export-papers --format jsonl --output papers.jsonl
paper export-papers --format csv --output papers.csv
# Excel-safe mode adds a UTF-8 BOM and neutralizes formula-leading cells
paper export-papers --format csv --output papers-excel.csv --excel-safe
# Parquet is optional: pip install "academic-intelligence[export]"
paper export-papers --format parquet --output papers.parquet

# New sources: run a source adapter operation directly
#   paper source <source> <operation> [value] [OPTIONS]
paper source crossref search "deep learning" --limit 5
paper source unpaywall get "10.1038/nature14539"          # requires UNPAYWALL_EMAIL
paper source europe_pmc search "cancer" --limit 5         # europe-pmc / epmc alias to the same adapter
paper source opencitations citations "W2626778328"        # coci is an alias for opencitations
paper source core search "transformer" --limit 5          # optional CORE_API_KEY

# Legal OA full text: locate -> download -> parse -> segment (M15)
paper fulltext "10.1038/nature14539" --persist
paper fulltext "1706.03762" --sources unpaywall,arxiv

# Web crawling tools (robots pre-check; blocked/failed exit 2)
paper web crawl "https://example.com/paper" --output page.json
paper web crawl "https://example.com/paper" --extract schema.json

# Local PDF tools (parse into pages -> paragraphs, JSONL output)
paper pdf parse ./paper.pdf --output segments.jsonl

# Author identity resolution (WP6)
paper author resolve "<paper.id>" "Geoffrey Hinton"       # resolve a byline name inside a stored paper
paper author profile "A2365917581" --source openalex      # full profile for one authority id
paper author search "Geoffrey Hinton" --limit 10          # same-name candidates, disambiguated by default
paper author confirm "openalex:A2365917581" --for "<paper.id>" --name "Geoffrey Hinton"

# Source registry and health: capability matrix + per-source quota status
paper sources status
paper budget                                            # per-source budget quotas overview

# Show the CLI version
paper --version
```

> **`--persist` is required to store data.** Collection commands (`collect paper` /
> `collect author` / `collect citations`) only print and/or write the `--output`
> JSON file by default. Without `--persist`, `paper stats` and `paper query` will
> report 0 records because nothing was written to the database.

> **Cross-process graph workflow**: `paper expand --output graph.json` atomically
> writes a versioned snapshot. A later `paper export --snapshot graph.json` loads
> it before extracting the requested subgraph. Without `--snapshot`, export
> falls back to the current in-process graph and reports a clear error when it
> is empty.

> **Automation exit codes**: invalid input, corrupt snapshots, and a total
> `expand` failure exit with code 2. An expansion that returns useful partial
> results remains successful (code 0) and prints each relation failure.

Source aliases: `gs` (Google Scholar), `ss` / `s2` (Semantic Scholar), `oa` (OpenAlex), `epmc` (Europe PMC), `coci` (OpenCitations), `all` / `*` (all configured sources). CLI `source` subcommand names: `europe_pmc` (aliases `europe-pmc`, `epmc`) and `opencitations` (alias `coci`).

### Identifier forms per source

What a `collect paper` query means depends on the source(s) you select:

| Query | Works on | Notes |
|-------|----------|-------|
| DOI, e.g. `10.1038/nature14539` | Crossref, OpenAlex, Semantic Scholar, PubMed, Unpaywall, Europe PMC | OpenAlex also accepts the full `https://doi.org/...` URL; Unpaywall `get` additionally needs `UNPAYWALL_EMAIL` |
| arXiv DOI, e.g. `10.48550/arXiv.1706.03762` | arXiv (`--sources arxiv`) | OpenAlex / Semantic Scholar treat it as free text and usually find nothing |
| Complete arXiv ID, e.g. `1706.03762`, `1706.03762v2`, `hep-th/9901001`, `arXiv:1706.03762`, or an `/abs/` URL | arXiv (`--sources arxiv`) | Strict exact lookup: the collector routes only to the arXiv-capable adapter and accepts only a canonically matching response; prose containing an ID remains free-text search |
| OpenAlex work id, e.g. `W2626778328` | OpenAlex | bare or full `https://openalex.org/W...` URL |
| Free-text title / keywords | all sources | returns a ranked list, not a single record |

Citation collection (`collect citations`, `expand -r citations`) fetches at most
**50 citing works per request** (the sources' `per_page` cap); "top N" by
citation count is therefore a window-based result, not a global top-N.

### Advanced public APIs

`ArxivSource.get_paper_by_arxiv_id(arxiv_id)` is async and returns
`Paper | None`. `AuthorDisambiguator` exposes `score_pair(a, b)`,
`cluster(authors)`, and `disambiguate(authors)`. `KnowledgeGraph` exposes
`add_node(...)`, `add_edge(...)`, `save_snapshot(path)`, and the class method
`load_snapshot(path, *, cache_size=None)`. A version-1 snapshot contains
`version`, `directed`, `nodes`, `edges`, `node_count`, and `edge_count`; loading
validates counts, identities, endpoints, and relation fields before returning
a new graph.

## Architecture

```
academic_intelligence/
├── __init__.py          # AcademicIntelligence facade, public exports
├── cli.py               # `paper` CLI (Typer)
├── core/                # Models (v2), types/Config, exceptions, constants
├── sources/             # Source adapters (arXiv, OpenAlex, S2, PubMed, IEEE, GS)
│   └── base.py          # BaseSource abstract base class
├── collectors/          # MultiSourceCollector orchestration (fetch→enrich→dedup→validate)
├── processors/          # Processing pipeline
│   ├── deduplicator.py  #   Multi-source dedup + fusion
│   ├── disambiguator.py #   Author disambiguation (ID + heuristic clustering)
│   ├── scorer.py        #   Confidence scoring (baselines + adjustments)
│   ├── enricher.py      #   Missing-field enrichment
│   ├── validator.py     #   Schema / business-rule validation
│   └── incremental.py   #   Incremental change detection & merge
├── graph/               # Session knowledge graph (pure Python, no networkx)
│   ├── knowledge_graph.py
│   ├── traversal.py     # expand_from_graph (lazy BFS + stubs + truncation)
│   └── cache.py         # LRU graph cache
├── storage/             # Storage backends
│   ├── base.py          # BaseStorage interface
│   ├── sqlite_store.py  #   SQLAlchemy 2.0 async + aiosqlite
│   └── json_store.py    #   JSON directory backend (optional)
└── utils/               # HTTP client, proxy, rate limiter, retry, cache
```

## Data Models (v2)

### Paper

| Field | Type | Description |
|-------|------|-------------|
| `id` | `str \| None` | Internal / source id |
| `title` | `str` | Paper title |
| `authors` | `List[AuthorRef]` | Byline authors with order & correspondence |
| `year` | `int \| None` | Publication year |
| `venue` / `venue_type` | `str \| None` | Journal/conference name; `journal`/`conference`/`preprint` |
| `abstract` | `str \| None` | Abstract text |
| `doi` / `arxiv_id` / `pmid` | `str \| None` | Cross-source identifiers |
| `url` / `pdf_url` | `str \| None` | Landing page / PDF link |
| `citations` / `reference_count` | `int \| None` | Citation count / reference count |
| `keywords` / `fields_of_study` | `List[str]` | Tags and research fields |
| `references` / `citations_list` | `List[str] \| None` | Graph relation ids |
| `evidence_list` | `List[Evidence]` | One entry per confirming source |

`AuthorRef`: `author_id`, `name`, `position` (1-based), `is_corresponding`, `affiliation`.

### Author

`id`, `name`, `orcid`, `semantic_scholar_id`, `openalex_id`, `aliases`, `disambiguation_status` (`auto`/`confirmed`/`ambiguous`), `coauthors`, `venues`, `active_years`, `affiliation`, `email`, `homepage`, `h_index`, `citations`, `interests`, `profile_url`, `evidence_list`.

### Evidence

`source` (`SourceType`), `source_id`, `source_url`, `collected_at`, `confidence` (0.0–1.0), `raw_data`. Access the highest-confidence composite via `record.primary_evidence`.

## Deduplication & Disambiguation

- **Dedup rules** (papers): exact match on DOI / arXiv ID / PMID / URL / internal id → arXiv↔DOI cross-ID (title ≥ 0.92) → SequenceMatcher title similarity (≥ 0.92 with Jaccard guard) → weighted fuzzy (title 0.6 + authors 0.25 + year 0.15). Clustering uses union-find transitive closure, so merge order never affects the result.
- **Fusion**: field values are picked from the highest-confidence source; `evidence_list` keeps every source; composite confidence is recomputed by `ConfidenceScorer`.
- **Author disambiguation** (`AuthorDisambiguator`): the public multi-source author pipeline uses this stage instead of name-only deduplication. Any two records sharing an ORCID / S2 / OpenAlex author id are the same person; otherwise a weighted feature score (name, affiliation, topic, coauthors, year range, venue) decides — ≥ 0.85 auto-merge, 0.60–0.85 marked `ambiguous`, below that different people.

## Confidence Scoring

Per-source baselines: arXiv 0.95, PubMed 0.92, OpenAlex 0.90, Semantic Scholar 0.88, IEEE 0.85, Google Scholar 0.75. Each additional confirming source adds `+0.05` (capped at 1.0); an exact DOI adds `+0.05`; a PDF link `+0.03`; data older than 2 years loses `-0.10`.

## Incremental Updates

`update_author_papers(name)` / `update_paper(paper_id)` are stale-gated (`Config.paper_refresh_days`, default 7): nothing is re-collected within the window, otherwise fresh data is diffed against storage field-by-field and only new/updated records are written.

## Performance Notes

- **Write in batches**: `storage.save_paper(...)` persists one record per call and is ~40× slower than `save_batch(...)` (measured ~26 rec/s vs ~1,100 rec/s on a 2000-paper workload — every call pays a full transaction + index round trip; batch `save_batch` measures 3.8k–10.6k rec/s on other hardware, see `capability-boundaries.md` FIX-G / P39 — the numbers differ by workload/machine). Collect records and persist with a single `save_batch(papers=[...])` call; it is idempotent (upsert), so re-running is safe. The `collect_*` paths with `persist=True` already batch internally.
- **Keyword queries are indexed**: `query_papers(keyword=...)` is served through an FTS5 trigram index over paper titles/abstracts (`paper_text_fts`, FIX-AB-4), auto-maintained on every write path and backfilled on connect; selective ASCII keywords stay in the low-tens-of-ms range on 10k-row databases.
- **Stable keyset pages**: `query_papers` accepts `order_by="id"|"title"|"year"` and `query_authors` accepts `order_by="id"|"name"`. Pass the last returned entity ID as `after=` (or `cursor=`); ordering uses the ID as a deterministic tie-breaker, so adjacent pages have no duplicates or omissions.
- **Parsers validate cheaply**: the pubmed/arxiv adapters check DOIs / PMIDs with lightweight field-level helpers (`normalize_doi` / `normalize_pmid`, shared with the `Paper` model) instead of constructing a whole `Paper` per article, keeping parse throughput in the 10k+ rec/s range.
- A performance regression guard lives in `tests/performance/` (marked `performance` / `slow`).

## Configuration

```python
from academic_intelligence import AcademicIntelligence, Config

config = Config(
    sources=["openalex", "semantic_scholar"],   # default: ss, openalex, gs (gs skipped unless enable_google_scholar)
    storage_type="sqlite",                       # or "json"
    storage_path="./academic_intelligence.db",
    min_confidence=0.5,                          # below this: filtered out
    deduplication_threshold=0.85,
    max_expand_depth=3,                          # graph BFS depth limit
    max_expand_nodes=50,                         # per-pass node cap
    auto_merge_threshold=0.85,                   # disambiguation auto-merge
    ambiguous_threshold=0.60,
    cache_ttl=3600, cache_enabled=True,
    cache_persistent=True,                       # persist HTTP cache to disk (FIX-Y)
    cache_path="./.cache/http.json",             # cache JSON file when persistent
    rate_limit=1.0,                              # global requests/second ceiling
    max_concurrent_requests=4,                   # global in-flight HTTP ceiling
    enable_google_scholar=False,                 # must be true to register GS
    serpapi_key=None,                            # Google Scholar (or SERPAPI_KEY env)
    ieee_api_key=None,                           # IEEE Xplore (or IEEE_API_KEY env)
    crossref_mailto=None,                        # Crossref polite pool (or CROSSREF_MAILTO env)
    unpaywall_email=None,                        # Unpaywall API (or UNPAYWALL_EMAIL env)
    core_api_key=None,                           # CORE API v3 (or CORE_API_KEY env)
)
```

Secrets fall back to environment variables: `SERPAPI_KEY`, `SEMANTIC_SCHOLAR_API_KEY`, `OPENALEX_EMAIL`, `IEEE_API_KEY`, `CROSSREF_MAILTO`, `UNPAYWALL_EMAIL`, `CORE_API_KEY`.

## Known limitations

- **Google Scholar**: the adapter is kept but **not registered by default** — set `enable_google_scholar=True` (and `serpapi_key` / `SERPAPI_KEY`) to activate it.
- **Unpaywall**: requires `UNPAYWALL_EMAIL` (no anonymous access); `paper source unpaywall get <doi>` and the `fulltext` pipeline fail without it.
- **CORE**: `CORE_API_KEY` is optional; without it CORE lookup degrades (fewer / no results) instead of failing hard.

## Error Handling

`academic_intelligence.errors` (alias of `core.exceptions`) exposes `SourceUnavailableError`, `RateLimitError`, `AuthenticationError`, `ParseError`, `AllSourcesFailedError`, `DataValidationError`, `StorageError`, and more. Source failures are `SourceFailure` records (`source`, `operation`, `error_type`, `message`, `retry_count`, `http_status`, `transient`, `permanent`) that remain string-compatible for older consumers. Single-source failures remain soft in `CollectionResult.errors`; `AllSourcesFailedError` is raised only when every capable source fails. Inspect adapter support without connecting via `ai.source_capabilities()` or `source.supports(operation)`; arXiv and IEEE explicitly report `get_citations=False` instead of returning an indistinguishable real empty result through the collector.

The HTTP client owns the configured internal retry budget and preserves its
terminal retry count/status through source exception wrapping. Automation must
not create unbounded outer loops: default to no outer retry, or at most one
explicit extra attempt honoring `retry_after`. Report usable results with
source failures as **PARTIAL**; report **BLOCKED** and stop when all capable
sources remain unavailable/rate-limited or credentials are missing.

## Development

```bash
pip install -e ".[dev]"

# Run the full offline suite (VCR-style cassettes replay all HTTP)
pytest

# Lint / type-check
ruff check .
mypy academic_intelligence

# Build docs
mkdocs build
```

## License

MIT License
