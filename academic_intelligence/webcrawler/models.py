"""WebCrawler data models (WP3 webcrawler layer).

Defines the :class:`WebDocument` result contract of
:meth:`~academic_intelligence.webcrawler.crawler.WebCrawler.crawl` together
with the schema-extraction spec models.

Contract (technical-design.md §1.2): ``WebCrawler.crawl(url, schema=None) →
WebDocument`` with ``title``/``url``/``content``/``links``/``metadata`` and a
``status`` of ``ok | blocked | failed``.  ``blocked`` is reserved for
robots.txt denials and anti-crawl interceptions (403 / challenge / captcha
pages) — the red line never escalates against those.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field


class CrawlStatus(StrEnum):
    """Terminal status of one crawl attempt.

    - ``OK``: page fetched and (when possible) content extracted.
    - ``BLOCKED``: robots.txt denial or anti-crawl interception; crawling
      stops here by policy (no challenge solving, no captcha bypass).
    - ``FAILED``: transport/parse error or unexpected non-anti-crawl status.
    """

    OK = "ok"
    BLOCKED = "blocked"
    FAILED = "failed"


class WebDocument(BaseModel):
    """Result of a single :meth:`WebCrawler.crawl` call.

    Attributes:
        url: The requested URL.
        status: One of ``ok`` / ``blocked`` / ``failed``.
        title: Page title (best effort; may be empty).
        content: Extracted main text content (Trafilatura).
        links: Absolute HTTP(S) links found in the page, deduplicated.
        metadata: Diagnostic/provenance key-value pairs (see keys below).
        extracted: Schema-extraction results (rules mode, or LLM mode when
            Crawl4AI is available and requested); ``None`` when no schema
            was given or extraction produced nothing.

    Metadata keys (documented contract):
    ``status_code``, ``reason``, ``fetcher`` (``httpx``/``curl_cffi``/
    ``scrapling``), ``fetched_at`` (ISO-8601 UTC), ``content_extractor``,
    ``content_extracted``, ``links_count``, ``robots_allowed``,
    ``robots_url``, ``schema_mode``, ``schema_diagnostic``,
    ``browser_required``, ``diagnostic`` (blocked/failed explanation).
    """

    url: str
    status: CrawlStatus = CrawlStatus.OK
    title: str = ""
    content: str = ""
    links: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    extracted: dict[str, Any] | None = None

    @property
    def ok(self) -> bool:
        """Whether the crawl finished with ``status == ok``."""
        return self.status == CrawlStatus.OK

    @property
    def diagnostic(self) -> str | None:
        """Human-readable explanation for ``blocked``/``failed`` documents."""
        value = self.metadata.get("diagnostic")
        return value if isinstance(value, str) and value else None

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dictionary (JSON-compatible)."""
        return self.model_dump(mode="json")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WebDocument:
        """Deserialize from a plain dictionary."""
        return cls.model_validate(data)

    def to_json(self, *, indent: int = 2) -> str:
        """Serialize to a JSON string."""
        return self.model_dump_json(indent=indent)

    @classmethod
    def from_json(cls, raw: str) -> WebDocument:
        """Deserialize from a JSON string."""
        return cls.model_validate_json(raw)


class SchemaField(BaseModel):
    """One rule-mode extraction field.

    Attributes:
        field: Output key in the ``extracted`` mapping.
        selector: CSS or XPath selector expression.
        mode: ``"css"`` or ``"xpath"``.
        attribute: When set, extract this HTML attribute instead of the text.
        multiple: Collect every match into a list; otherwise only the first.
        default: Value used when the selector matches nothing.
    """

    field: str = Field(min_length=1)
    selector: str = Field(min_length=1)
    mode: Literal["css", "xpath"] = "css"
    attribute: str | None = None
    multiple: bool = False
    default: Any = None


class SchemaSpec(BaseModel):
    """Schema for structured extraction from a crawled page.

    Attributes:
        fields: Rule-mode (CSS/XPath) extraction fields.
        llm: When ``True`` and Crawl4AI is installed, an LLM extraction
            pass is attempted first; rule mode remains the fallback.
    """

    fields: list[SchemaField] = Field(default_factory=list)
    llm: bool = False


def utc_now_iso() -> str:
    """Current UTC time as an ISO-8601 string (metadata ``fetched_at``)."""
    return datetime.now(UTC).isoformat()


class CrawlCacheRecord(BaseModel):
    """One persisted ``crawl_cache`` row (technical-design.md §2).

    The storage layer (``SQLiteStorage.save_crawl_cache`` /
    ``get_crawl_cache``) reads and writes these rows; the webcrawler keeps
    crawl outcomes (``ok`` / ``blocked`` / ``failed``) queryable across
    sessions when a persistent store is configured, replacing the pure
    in-memory :class:`Cache` as the cross-session record.

    Attributes:
        url: The crawled URL (primary key).
        status: Terminal crawl status — ``"ok"`` / ``"blocked"`` /
            ``"failed"``.
        fetched_at: ISO-8601 UTC fetch time.
        etag: Optional transport-level ETag of the fetched resource.
        body_hash: Optional content hash of the fetched body.
        web_doc: The serialized :class:`WebDocument` (JSON dict), when the
            crawl produced one.
    """

    url: str
    status: str = CrawlStatus.OK.value
    fetched_at: str | None = None
    etag: str | None = None
    body_hash: str | None = None
    web_doc: dict[str, Any] | None = None


@runtime_checkable
class CrawlCacheStore(Protocol):
    """Persistence contract for the ``crawl_cache`` table (technical-design §2).

    ``SQLiteStorage`` satisfies this structurally (``get_crawl_cache`` /
    ``save_crawl_cache``); the webcrawler treats a configured store as the
    cross-session crawl record, falling back to the in-memory cache when no
    store is supplied.
    """

    async def get_crawl_cache(self, url: str) -> CrawlCacheRecord | None:
        """Return the persisted crawl-cache row for *url*, or ``None``."""
        ...

    async def save_crawl_cache(self, record: CrawlCacheRecord) -> None:
        """Upsert the crawl-cache row for ``record.url``."""
        ...
