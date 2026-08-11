# CLI

The `paper` command is defined in `academic_intelligence.cli` and installed via the `paper` console script. The legacy `ai` entry point is a shim that prints `Command 'ai' was renamed to 'paper'` and exits with code 2 — call `paper` directly.

```bash
paper --help
```

## Command overview

| Command | Description |
|---------|-------------|
| `paper collect author <name>` | Collect papers for an author |
| `paper collect paper <query>` | Collect by DOI or title |
| `paper collect citations <paper_id>` | Collect citing works for a paper id |
| `paper paper <query>` | Direct alias for `paper collect paper` |
| `paper author <name>` | Collect an author profile and associated papers |
| `paper author-papers <name>` | Direct alias for `paper collect author` |
| `paper update --author <name>` | Incrementally refresh an author's papers |
| `paper query papers` | Query stored papers |
| `paper stats` | Show storage statistics |
| `paper expand <entity_id>` | Expand an entity's relations in the session knowledge graph |
| `paper export` | Export the subgraph around a center entity as JSON |
| `paper export-papers` | Stream stored papers to CSV, JSONL, or optional Parquet |
| `paper source <source> <operation> [value]` | Run a source adapter operation directly |
| `paper sources status` | Capability matrix + per-source quota status |
| `paper budget` | Per-source budget quotas overview |
| `paper fulltext <identifier>` | Legal OA full text: locate → download → parse → segment |
| `paper web crawl <url>` | Crawl a public page (robots pre-check) |
| `paper pdf parse <file>` | Parse a local PDF into text segments |
| `paper author resolve/profile/search/confirm` | Author identity resolution |

## `paper collect author`

```bash
paper collect author "Geoffrey Hinton" [OPTIONS]
```

| Option | Short | Default | Description |
|--------|-------|---------|-------------|
| `--sources` | `-s` | config default | Comma-separated sources or `all` |
| `--output` | `-o` | stdout JSON | Write result JSON to path |
| `--storage-type` | | `sqlite` | `sqlite` or `json` |
| `--storage-path` | | `./academic_intelligence.db` | Backend path |
| `--persist` | | off | Save results to storage |

Examples:

```bash
paper collect author "Geoffrey Hinton" --sources ss,openalex --output papers.json
paper collect author "Yoshua Bengio" --sources all --persist
paper collect author "Yann LeCun" -s gs,ss --storage-path ./lecun.db --persist
```

## `paper collect paper`

```bash
paper collect paper "10.1038/nature14539" [OPTIONS]
```

| Option | Short | Default | Description |
|--------|-------|---------|-------------|
| `--sources` | `-s` | config default | Sources or `all` |
| `--output` | `-o` | none | Versioned graph snapshot path |
| `--storage-type` | | `sqlite` | Backend type |
| `--storage-path` | | `./academic_intelligence.db` | Backend path |
| `--persist` | | off | Save to storage |
| `--limit` | `-n` | `10` | Max results for search-style queries |

Examples:

```bash
paper collect paper "10.1038/nature14539" --sources all -o paper.json
paper collect paper "Attention is All You Need" -s ss,openalex --limit 5
```

## `paper collect citations`

```bash
paper collect citations <paper_id> [OPTIONS]
```

Collects citing works for a paper id (e.g. an OpenAlex work id like `W2626778328`). Sources that expose citing works also return the full citing-paper records so they can be persisted.

| Option | Short | Default | Description |
|--------|-------|---------|-------------|
| `--sources` | `-s` | config default | Sources or `all` |
| `--output` | `-o` | stdout JSON | Output path |
| `--storage-type` | | `sqlite` | Backend type |
| `--storage-path` | | `./academic_intelligence.db` | Backend path |
| `--persist` | | off | Save to storage |

Examples:

```bash
paper collect citations "W2626778328" --sources openalex --persist
paper collect citations "W2626778328" -o citing.json
```

Prints a summary line (`Found N citations, M citing papers`), surfaces soft source errors / multi-source warnings as yellow messages, and writes the `CollectionResult.to_dict()` payload to the output path (or prints it). Citation collection is capped at **50 citing works per request** (`per_page`), so "top N" is window-based. On an empty result it prints a `No results` hint and returns without writing output.

## `paper query`

```bash
paper query papers [OPTIONS]
```

Currently only the `papers` entity is supported.

| Option | Default | Description |
|--------|---------|-------------|
| `--author` | | Filter by author substring |
| `--year` | | `YYYY` or `YYYY-YYYY` |
| `--venue` | | Venue filter |
| `--keyword` | | Keyword filter |
| `--limit` / `-n` | `10` | Max rows |
| `--storage-type` | `sqlite` | Backend |
| `--storage-path` | `./academic_intelligence.db` | Backend path |
| `--output` / `-o` | | If set, write JSON; else Rich table |

Examples:

```bash
paper query papers --author "Hinton" --year 2020-2024 --limit 10
paper query papers --venue NeurIPS --year 2023 -o out.json
paper query papers --keyword transformer --storage-path ./my.db
```

## `paper stats`

```bash
paper stats [--storage-type sqlite] [--storage-path ./academic_intelligence.db]
```

Prints key/value storage statistics in a table.

## `paper expand`

```bash
paper expand <entity_id> [OPTIONS]
```

Expands an entity's relationships (papers: `references`, `citations`, `authors`; authors: `papers`, `coauthors`) in the session knowledge graph. Storage-first; misses are fetched from the data sources when `--fetch-missing` is on.

| Option | Short | Default | Description |
|--------|-------|---------|-------------|
| `--relations` | `-r` | all applicable | Comma-separated: `references,citations,authors,papers,coauthors` |
| `--depth` | `-d` | `1` | Expansion depth (max `Config.max_expand_depth`, 3) |
| `--sources` | `-s` | config default | Sources or `all` |
| `--fetch-missing` | | on | `--no-fetch-missing` to only use stored data |
| `--output` | `-o` | stdout JSON | Output path |
| `--storage-type` | | `sqlite` | Backend type |
| `--storage-path` | | `./academic_intelligence.db` | Backend path |

Examples:

```bash
paper query papers --keyword "Deep learning" --output persisted-papers.json
paper expand "<paper.id>" --relations references,citations,authors --depth 2
paper expand "1695689" --relations papers,coauthors --no-fetch-missing -o graph.json
```

The console reports `ExpandResult` statistics. With `--output`, the file is the complete session graph snapshot (`version`, `nodes`, `edges`, counts), written atomically for a later `paper export --snapshot` call.

Graph operations require the persisted internal `Paper.id`/`Author.id`; a DOI suffix
such as `nature14539` is not an internal ID. Persist collection results, then read the
ID from the result or `paper query` output. A total relation failure with no useful node,
edge, cache hit, or fetched record exits 2; useful partial results exit 0 and retain
their warnings.

## `paper export`

```bash
paper export --center <entity_id> [OPTIONS]
```

Exports the subgraph around a center entity (undirected ego-graph traversal, radius default 2) as JSON.

| Option | Short | Default | Description |
|--------|-------|---------|-------------|
| `--center` | `-c` | (required) | Center entity id |
| `--radius` | `-r` | `2` | Subgraph radius |
| `--output` | `-o` | stdout JSON | Output path |
| `--snapshot` | | current process graph | Versioned snapshot from `paper expand --output` |
| `--storage-type` | | `sqlite` | Backend type |
| `--storage-path` | | `./academic_intelligence.db` | Backend path |

Examples:

```bash
paper export --snapshot graph.json --center "<paper.id>" --radius 2 --output subgraph.json
```

The payload is a serializable dict with `nodes`, `edges`, `center`, `radius`, `node_count`, `edge_count`.

## `paper export-papers`

```bash
paper export-papers --format csv --output papers.csv [OPTIONS]
```

`--format` accepts `csv`, `jsonl`, or `parquet`; `--after <paper-id>` resumes after a previous ID cursor and `--batch-size` defaults to 500. Raw CSV uses UTF-8 and preserves values exactly. CSV-only `--excel-safe` adds a UTF-8 BOM and prefixes cells beginning with `=`, `+`, `-`, `@`, tab, or carriage return with an apostrophe. Nested model fields are deterministic JSON strings. JSONL writes one compact raw object per line. Parquet uses a declared stable schema and lazily imports pyarrow; install `academic-intelligence[export]` when unavailable or ABI-incompatible.

## `paper source <source> <operation>`

```bash
paper source <source> <operation> [value] [OPTIONS]
```

Runs a source adapter operation directly, bypassing the multi-source collector. The `<source>` is the adapter's canonical name; `europe_pmc` also accepts the `europe-pmc` / `epmc` spellings and `opencitations` also accepts `coci`. Operations are per-adapter:

| Source | Operations |
|--------|------------|
| `arxiv` | `search`, `get` |
| `semantic_scholar` | `search`, `get`, `citations` |
| `openalex` | `search`, `get`, `citations` |
| `pubmed` | `search`, `get`, `citations` |
| `ieee` | `search`, `get` |
| `crossref` | `search`, `get` |
| `unpaywall` | `get`, `fulltext` |
| `europe_pmc` (`europe-pmc`, `epmc`) | `search`, `get`, `fulltext` |
| `opencitations` (`coci`) | `citations` |
| `core` | `search`, `get`, `fulltext` |

| Option | Short | Default | Description |
|--------|-------|---------|-------------|
| `--limit` | `-n` | `10` | Max results (search only) |
| `--persist` | | off | Save results to storage (upsert) |
| `--fulltext` | | off | Run the legal-OA full-text pipeline after `get` |
| `--output` | `-o` | stdout JSON | Write JSON output to a file |
| `--storage-type` | | `sqlite` | Backend type |
| `--storage-path` | | `./academic_intelligence.db` | Backend path |

Examples:

```bash
paper source arxiv search "transformer" --limit 5
paper source crossref search "deep learning" -n 3
paper source unpaywall get "10.1038/nature14539"       # requires UNPAYWALL_EMAIL
paper source europe_pmc search "cancer" -n 5           # europe-pmc / epmc work too
paper source opencitations citations "W2626778328"     # coci is an alias
paper source core get "10.1038/nature14539" --fulltext # optional CORE_API_KEY
```

## `paper fulltext`

```bash
paper fulltext <identifier> [OPTIONS]
```

Fetches legal OA full text: locate → download → parse → segment (M15). The identifier is an internal id, arXiv ID, or DOI of the paper.

| Option | Short | Default | Description |
|--------|-------|---------|-------------|
| `--sources` | `-s` | all | Comma-separated full-text sources: `unpaywall`, `core`, `arxiv`, `europe_pmc` |
| `--persist` | | off | Save full text to storage |
| `--output` | `-o` | stdout JSON | Write the FullText JSON to a file |
| `--storage-type` | | `sqlite` | Backend type |
| `--storage-path` | | `./academic_intelligence.db` | Backend path |

Examples:

```bash
paper fulltext "10.1038/nature14539" --persist
paper fulltext "1706.03762" --sources unpaywall,arxiv
```

## `paper web crawl`

```bash
paper web crawl <url> [OPTIONS]
```

Crawls a public page with a robots pre-check; blocked or failed crawls exit with code 2.

| Option | Short | Default | Description |
|--------|-------|---------|-------------|
| `--extract` | `-e` | | JSON schema file for structured extraction (rules mode) |
| `--output` | `-o` | stdout JSON | Write the result JSON to a file |

Examples:

```bash
paper web crawl "https://example.com/paper" --output page.json
paper web crawl "https://example.com/paper" --extract schema.json
```

## `paper pdf parse`

```bash
paper pdf parse <file> [OPTIONS]
```

Parses a local PDF into text segments (pages → paragraphs).

| Option | Short | Default | Description |
|--------|-------|---------|-------------|
| `--output` | `-o` | stdout JSON | Write segments as JSONL (one paragraph per line) |

Examples:

```bash
paper pdf parse ./paper.pdf --output segments.jsonl
```

## `paper author`

Author identity resolution (WP6): `resolve`, `profile`, `search`, `confirm`.

```bash
paper author resolve <paper_id> <name> [OPTIONS]
```

Resolves an author's identity inside a stored paper (Q2 / Q6). `--show-all` prints every candidate instead of the default top 10 + total count.

```bash
paper author profile <author_id> [OPTIONS]
```

Fetches the complete profile for one author id (Q3), with representative works sorted by citations. `--source` selects the authority system (`openalex` | `s2` | `orcid`, default `openalex`); the id must match that system (e.g. `A2365917581`, an S2 id, or an ORCID).

```bash
paper author search <name> [OPTIONS]
```

Searches same-name candidates, optionally disambiguated (Q7). `--disambiguate` / `--no-disambiguate` controls candidate scoring (default: disambiguate); `--limit` / `-n` caps candidates (default 10).

```bash
paper author confirm <candidate_id> [OPTIONS]
```

Confirms a candidate as the identity of a paper's byline name (I8). Writes `author_identity_global` (status=confirmed) plus the paper-level evidence link; the next `paper author resolve` of the same name hits it directly (cross-paper reuse). `--for <paper_id>` and `--name <byline-name>` are required; `candidate_id` is `openalex:<id>` / `s2:<id>` / `orcid:<id>` or an OpenAlex URL.

All four subcommands share `--storage-type`, `--storage-path`, and `--output` / `-o`.

## `paper sources status`

```bash
paper sources status [OPTIONS]
```

Shows the source capability matrix (search / get / citations / fulltext per adapter) plus per-source quota status. See also `paper budget`.

## `paper budget`

```bash
paper budget [OPTIONS]
```

Shows per-source budget quotas (all-source overview).

## Source aliases

| Alias | Source |
|-------|--------|
| `gs` | Google Scholar |
| `ss`, `s2` | Semantic Scholar |
| `oa`, `openalex` | OpenAlex |
| `epmc`, `europe-pmc`, `europe_pmc` | Europe PMC |
| `coci`, `opencitations` | OpenCitations |
| `all`, `*` | All sources built for this run |

`paper source` subcommand names are `europe_pmc` (aliases `europe-pmc`, `epmc`) and `opencitations` (alias `coci`); the other new adapters (`crossref`, `unpaywall`, `core`) use their canonical names directly.

## Output format

### Collection commands

- Default: pretty JSON to the terminal (`rich` JSON)
- `--output path`: write UTF-8 JSON file (`indent=2`)

Payload is `CollectionResult.to_dict()` shape: authors, papers, citations, errors, stats.

### Query command

- Without `--output`: Rich table (year, title, authors, DOI)
- With `--output`: JSON array of paper dicts

## Environment

API keys are read from the environment (see [Configuration](../getting-started/configuration.md)):

```bash
$env:SERPAPI_KEY = "..."
$env:SEMANTIC_SCHOLAR_API_KEY = "..."
$env:OPENALEX_EMAIL = "you@example.com"
$env:CROSSREF_MAILTO = "you@example.com"
$env:UNPAYWALL_EMAIL = "you@example.com"   # required for Unpaywall
$env:CORE_API_KEY = "..."                  # optional, CORE
```

## Exit behavior

- Invalid year format or unsupported entity → Typer/`BadParameter` error
- Soft source errors print as yellow warnings but still emit partial JSON when possible
- Invalid input, corrupt snapshots, and total expansion failure exit with code 2
- Partial expansion with useful results exits 0 and prints relation failures
- `paper web crawl`: robots-blocked or failed crawls exit with code 2
- `ai` shim: prints `Command 'ai' was renamed to 'paper'` and exits with code 2

## Related

- [Quick Start](../getting-started/quick-start.md)
- [Collection](collection.md)
