# CLI

The `ai` command is defined in `academic_intelligence.cli` and installed via the `ai` console script.

```bash
ai --help
```

## Command overview

| Command | Description |
|---------|-------------|
| `ai collect author <name>` | Collect papers for an author |
| `ai collect paper <query>` | Collect by DOI or title |
| `ai query papers` | Query stored papers |
| `ai stats` | Show storage statistics |

## `ai collect author`

```bash
ai collect author "Geoffrey Hinton" [OPTIONS]
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
ai collect author "Geoffrey Hinton" --sources ss,openalex --output papers.json
ai collect author "Yoshua Bengio" --sources all --persist
ai collect author "Yann LeCun" -s gs,ss --storage-path ./lecun.db --persist
```

## `ai collect paper`

```bash
ai collect paper "10.1038/nature14539" [OPTIONS]
```

| Option | Short | Default | Description |
|--------|-------|---------|-------------|
| `--sources` | `-s` | config default | Sources or `all` |
| `--output` | `-o` | stdout JSON | Output path |
| `--storage-type` | | `sqlite` | Backend type |
| `--storage-path` | | `./academic_intelligence.db` | Backend path |
| `--persist` | | off | Save to storage |
| `--limit` | `-n` | `10` | Max results for search-style queries |

Examples:

```bash
ai collect paper "10.1038/nature14539" --sources all -o paper.json
ai collect paper "Attention is All You Need" -s ss,openalex --limit 5
```

## `ai query`

```bash
ai query papers [OPTIONS]
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
ai query papers --author "Hinton" --year 2020-2024 --limit 10
ai query papers --venue NeurIPS --year 2023 -o out.json
ai query papers --keyword transformer --storage-path ./my.db
```

## `ai stats`

```bash
ai stats [--storage-type sqlite] [--storage-path ./academic_intelligence.db]
```

Prints key/value storage statistics in a table.

## Source aliases

| Alias | Source |
|-------|--------|
| `gs` | Google Scholar |
| `ss`, `s2` | Semantic Scholar |
| `oa`, `openalex` | OpenAlex |
| `all`, `*` | All sources built for this run |

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
```

## Exit behavior

- Invalid year format or unsupported entity → Typer/`BadParameter` error
- Soft source errors print as yellow warnings but still emit partial JSON when possible

## Related

- [Quick Start](../getting-started/quick-start.md)
- [Collection](collection.md)
