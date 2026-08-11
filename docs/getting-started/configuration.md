# Configuration

All runtime settings go through the `Config` model (`academic_intelligence.core.types.Config`). You can pass a `Config` instance or a plain dict into `AcademicIntelligence`.

## Basic usage

```python
from academic_intelligence import AcademicIntelligence, Config
from academic_intelligence.core.types import AntiCrawlStrategy

config = Config(
    sources=["semantic_scholar", "openalex", "google_scholar"],
    rate_limit=1.0,
    storage_type="sqlite",
    storage_path="./academic_intelligence.db",
    min_confidence=0.5,
    deduplication_threshold=0.85,
    cache_enabled=True,
    cache_ttl=3600,
    timeout=30.0,
    max_concurrent_sources=3,
    max_concurrent_requests=4,
    enable_google_scholar=True,
    anti_crawl=AntiCrawlStrategy(
        base_delay=1.0,
        max_retries=3,
        retry_backoff=2.0,
    ),
)

async with AcademicIntelligence(config) as ai:
    ...
```

## Configuration fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `sources` | `list[str]` | `semantic_scholar`, `openalex`, `google_scholar` | Enabled sources (order matters for priority) |
| `rate_limit` | `float` | `1.0` | Enforced global requests-per-second ceiling |
| `proxy` | `str \| None` | `None` | Single proxy URL |
| `proxies` | `list[str]` | `[]` | Additional proxy URLs |
| `storage_type` | `str` | `"sqlite"` | `"sqlite"` or `"json"` |
| `storage_path` | `str` | `./academic_intelligence.db` | DB file or JSON data directory |
| `min_confidence` | `float` | `0.5` | Minimum confidence to accept records |
| `deduplication_threshold` | `float` | `0.85` | Title similarity threshold for merge |
| `cache_ttl` | `int` | `3600` | HTTP cache TTL (seconds) |
| `cache_enabled` | `bool` | `True` | Enable response caching |
| `timeout` | `float` | `30.0` | HTTP timeout (seconds) |
| `serpapi_key` | `str \| None` | `None` | SerpAPI key (Google Scholar) |
| `semantic_scholar_api_key` | `str \| None` | `None` | Semantic Scholar API key |
| `openalex_email` | `str \| None` | `None` | Email for OpenAlex polite pool |
| `ieee_api_key` | `str \| None` | `None` | IEEE Xplore Metadata API key (env: `IEEE_API_KEY`) |
| `anti_crawl` | `AntiCrawlStrategy` | defaults | Nested anti-crawl settings |
| `max_concurrent_sources` | `int` | `3` | Parallel source queries |
| `enable_google_scholar` | `bool` | `False` | Gate for Google Scholar registration; `sources` alone does not enable it |
| `download_delay` | `float` | `1.0` | Delay between downloads (s) |
| `max_concurrent_requests` | `int` | `4` | Enforced global in-flight HTTP request ceiling |
| `max_expand_depth` | `int` | `3` | Graph expansion depth limit |
| `max_expand_nodes` | `int` | `50` | Max nodes per graph expansion pass |
| `graph_cache_size` | `int` | `5000` | Knowledge graph cache capacity (nodes) |
| `auto_merge_threshold` | `float` | `0.85` | Disambiguation auto-merge threshold |
| `ambiguous_threshold` | `float` | `0.60` | Disambiguation ambiguous threshold |
| `paper_refresh_days` | `int` | `7` | Days before a paper is refreshed incrementally |
| `author_refresh_days` | `int` | `30` | Days before an author profile is refreshed |

### AntiCrawlStrategy

| Field | Default | Description |
|-------|---------|-------------|
| `proxy_pool` | `[]` | Proxies for rotation |
| `proxy_rotation_interval` | `10` | Rotate every N requests |
| `base_delay` | `1.0` | Base delay between requests (s) |
| `adaptive_delay` | `True` | Adjust delay from responses |
| `jitter` | `True` | Randomize delays |
| `fallback_sources` | `True` | Fall back to other sources on failure |
| `fallback_strategies` | `True` | Fall back to alternate strategies |
| `max_retries` | `3` | Retry attempts |
| `retry_backoff` | `2.0` | Exponential backoff multiplier |
| `retry_on_status` | `[429, 503, 504]` | HTTP statuses that trigger retry |

Proxies from `proxy`, `proxies`, and `anti_crawl.proxy_pool` are merged (order-preserving, de-duplicated) via `Config.proxy_list()`.

## Environment variables

Secrets are filled from the environment when not set on `Config`:

| Variable | Maps to |
|----------|---------|
| `SERPAPI_KEY` | `serpapi_key` (Google Scholar via SerpAPI) |
| `SEMANTIC_SCHOLAR_API_KEY` | `semantic_scholar_api_key` |
| `OPENALEX_EMAIL` | `openalex_email` |
| `IEEE_API_KEY` | `ieee_api_key` (IEEE Xplore) |

```bash
# Linux / macOS
export SERPAPI_KEY=...
export SEMANTIC_SCHOLAR_API_KEY=...
export OPENALEX_EMAIL=you@example.com
export IEEE_API_KEY=...

# Windows PowerShell
$env:SERPAPI_KEY = "..."
$env:SEMANTIC_SCHOLAR_API_KEY = "..."
$env:OPENALEX_EMAIL = "you@example.com"
$env:IEEE_API_KEY = "..."
```

## Config file example

There is no required on-disk config format; load JSON/YAML yourself and validate:

```python
import json
from pathlib import Path
from academic_intelligence import AcademicIntelligence, Config

data = json.loads(Path("ai-config.json").read_text(encoding="utf-8"))
config = Config.from_dict(data)
# or: config = Config.model_validate(data)
```

Example `ai-config.json`:

```json
{
  "sources": ["semantic_scholar", "openalex"],
  "storage_type": "sqlite",
  "storage_path": "./data/academic.db",
  "min_confidence": 0.5,
  "deduplication_threshold": 0.85,
  "cache_enabled": true,
  "timeout": 30.0,
  "openalex_email": "you@example.com",
  "anti_crawl": {
    "base_delay": 1.0,
    "max_retries": 3,
    "retry_backoff": 2.0,
    "proxy_pool": []
  }
}
```

## CLI configuration

CLI commands accept storage and sources flags instead of a full config file:

```bash
paper collect author "Name" \
  --sources gs,ss,openalex \
  --storage-type sqlite \
  --storage-path ./academic_intelligence.db \
  --persist
```

API keys still come from environment variables.

## Source name aliases

When listing sources (API or CLI):

| Name | Aliases |
|------|---------|
| `google_scholar` | `gs` |
| `semantic_scholar` | `ss`, `s2` |
| `openalex` | `oa` |

All six adapters (`arxiv`, `openalex`, `semantic_scholar`, `pubmed`, `ieee`, `google_scholar`) are implemented and wired into `_build_sources`; aliases only exist for the three above. Google Scholar additionally requires `enable_google_scholar=True`; otherwise it is skipped even when listed in `sources`. IEEE queries without an API key warn at startup and degrade gracefully. Unsupported names are skipped at runtime for forward compatibility.

## Next steps

- [Data Sources](../user-guide/data-sources.md) — keys and capabilities per source
- [Collection](../user-guide/collection.md) — workflow and error handling
