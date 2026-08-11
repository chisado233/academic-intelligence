# Data Sources

Academic Intelligence talks to scholarly APIs and search services through **source plugins** under `academic_intelligence.sources`. Each plugin implements `BaseSource`.

## Supported sources

| Source | Enum / name | Status | API key | Notes |
|--------|-------------|--------|---------|-------|
| Semantic Scholar | `semantic_scholar` (`ss`, `s2`) | Implemented | Optional (`SEMANTIC_SCHOLAR_API_KEY`) | Higher rate limits with key |
| OpenAlex | `openalex` (`oa`) | Implemented | Email recommended (`OPENALEX_EMAIL`) | Polite pool via email |
| Google Scholar | `google_scholar` (`gs`) | Implemented, **not registered by default** | **Required** SerpAPI (`SERPAPI_KEY`) | Via SerpAPI; needs `enable_google_scholar=True` |
| arXiv | `arxiv` | Implemented | None | Atom XML API |
| PubMed | `pubmed` | Implemented | None (NCBI email best practice) | E-utilities API |
| IEEE Xplore | `ieee` | Implemented | API key (`IEEE_API_KEY`) | Degrades gracefully without a key |
| Crossref | `crossref` | Implemented | Email recommended (`CROSSREF_MAILTO`) | Polite pool via mailto |
| Unpaywall | `unpaywall` | Implemented | **Required** (`UNPAYWALL_EMAIL`) | Legal OA full-text locator |
| Europe PMC | `europe_pmc` (`epmc`, `europe-pmc`) | Implemented | None | Search / get / fulltext |
| OpenCitations | `opencitations` (`coci`) | Implemented | None | Citation graph (COCI) |
| CORE | `core` | Implemented | Optional (`CORE_API_KEY`) | Legal OA full-text aggregator |

All adapters are registered in `AcademicIntelligence._build_sources` (Google Scholar only when `enable_google_scholar=True`) and can be queried directly or through the CLI (`--sources`). Unknown names are skipped for forward compatibility.

## Comparison

| Capability | Semantic Scholar | OpenAlex | Google Scholar (SerpAPI) |
|------------|------------------|----------|---------------------------|
| Paper search | Yes | Yes | Yes |
| DOI lookup | Yes | Yes | Limited |
| Author papers | Yes | Yes | Yes |
| Citation graphs | Strong | Strong | Partial |
| Free tier | Yes (rate limited) | Yes | SerpAPI paid/quota |
| Best for | CS/AI metadata, citations | Open bibliographic graph | Classic GS rankings / profiles |

## Configuration requirements

### Semantic Scholar

```python
Config(
    sources=["semantic_scholar"],
    semantic_scholar_api_key="...",  # or SEMANTIC_SCHOLAR_API_KEY
)
```

- Docs: [Semantic Scholar API](https://api.semanticscholar.org/)
- Without a key, public rate limits apply

### OpenAlex

```python
Config(
    sources=["openalex"],
    openalex_email="you@example.com",  # or OPENALEX_EMAIL
)
```

- Docs: [OpenAlex API](https://docs.openalex.org/)
- Providing an email enables the polite pool

### Google Scholar (SerpAPI)

```python
Config(
    sources=["google_scholar"],
    serpapi_key="...",  # or SERPAPI_KEY
)
```

- Requires a valid SerpAPI key
- Respect SerpAPI and Google terms of service

## Source interface (for extenders)

```python
from academic_intelligence.sources.base import BaseSource
from academic_intelligence.core.types import SourceType

class MySource(BaseSource):
    name = "my_source"
    source_type = SourceType.OPENALEX  # or extend the enum

    async def search_papers(self, query: str, limit: int = 10):
        ...

    async def get_paper_by_doi(self, doi: str):
        ...

    async def get_author_papers(self, author_name: str):
        ...
```

See [API: Sources](../api/sources.md) for generated signatures.

## Selecting sources at runtime

```python
# Default: config.sources
await ai.collect_author_papers("Name")

# Explicit list
await ai.collect_author_papers("Name", sources=["ss", "openalex"])

# All connected sources
await ai.collect_paper("doi:10.1038/...", sources=["all"])
```

CLI:

```bash
paper collect author "Name" --sources gs,ss,openalex
paper collect paper "10.1038/nature14539" --sources all
```

See [CLI: `paper source`](cli.md#paper-source-source-operation) for running a
single adapter directly (e.g. `paper source crossref search "deep learning"`),
and `paper sources status` for the capability matrix.

## Rate limits and etiquette

- Prefer official APIs (S2, OpenAlex) over HTML scraping
- Set `OPENALEX_EMAIL` and API keys for higher quotas
- Tune `anti_crawl.base_delay`, `rate_limit`, and proxies for GS/SerpAPI
- Do not bypass paywalls or violate source terms of service

## Related

- [Configuration](../getting-started/configuration.md)
- [Collection](collection.md)
