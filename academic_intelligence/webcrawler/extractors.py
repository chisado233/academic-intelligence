"""Content and schema extraction for the webcrawler (WP3).

- :func:`extract_content` — main-text extraction with **Trafilatura** (the
  formal lightweight dependency) plus title/metadata and absolute link list.
- :class:`RuleSchemaExtractor` — schema extraction with CSS/XPath rules.
- :func:`extract_llm_schema` — optional LLM-mode schema extraction via
  **Crawl4AI** (import-detected; rule mode remains the fallback).

Optional dependencies are all import-detected: when absent the layer
degrades gracefully and records a diagnostic instead of crashing.
"""

from __future__ import annotations

import importlib.util
import logging
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin

from .models import SchemaField, SchemaSpec

logger = logging.getLogger(__name__)

_MAX_LINKS = 500
"""Cap on the number of links kept in :attr:`WebDocument.links`."""


# ---------------------------------------------------------------------------
# Optional-dependency detection
# ---------------------------------------------------------------------------


def _importable(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except Exception:  # pragma: no cover - find_spec is robust, defensive anyway
        return False


_CSSSELECT_AVAILABLE: bool = _importable("cssselect")
_BS4_AVAILABLE: bool = _importable("bs4")
_CRAWL4AI_AVAILABLE: bool = _importable("crawl4ai")


def cssselect_available() -> bool:
    """Whether the optional ``cssselect`` package is importable."""
    return _CSSSELECT_AVAILABLE


def crawl4ai_available() -> bool:
    """Whether the optional ``crawl4ai`` package is importable."""
    return _CRAWL4AI_AVAILABLE


# ---------------------------------------------------------------------------
# Content extraction (Trafilatura)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContentExtractResult:
    """Outcome of main-content extraction.

    Attributes:
        title: Best-effort page title (Trafilatura metadata, else ``<title>``).
        content: Extracted main text (Trafilatura), ``""`` when empty.
        links: Absolute HTTP(S) links, deduplicated, order-preserving.
        extractor: ``"trafilatura"`` (or ``"fallback"`` when it errored).
        content_extracted: Whether a non-empty main text was produced.
    """

    title: str = ""
    content: str = ""
    links: list[str] = field(default_factory=list)
    extractor: str = "trafilatura"
    content_extracted: bool = False


def extract_links(html: str, base_url: str, *, max_links: int = _MAX_LINKS) -> list[str]:
    """Extract absolute HTTP(S) links from *html* via lxml (XPath).

    Relative hrefs are resolved against *base_url*.  lxml is guaranteed by
    Trafilatura (formal dependency), so this never needs an optional import.
    """
    links: list[str] = []
    seen: set[str] = set()
    try:
        import lxml.html  # type: ignore[import-untyped]

        tree = lxml.html.fromstring(html)
    except Exception:
        logger.debug("link extraction parse failed for %s", base_url)
        return links
    for href in tree.xpath("//a/@href") if tree is not None else []:
        try:
            absolute = urljoin(base_url, str(href))
        except ValueError:
            continue
        if not absolute.startswith(("http://", "https://")):
            continue
        if absolute not in seen:
            seen.add(absolute)
            links.append(absolute)
            if len(links) >= max_links:
                break
    return links


def _fallback_title(html: str) -> str:
    try:
        import lxml.html

        tree = lxml.html.fromstring(html)
        if tree is not None:
            titles = tree.xpath("//title/text()")
            if titles:
                return str(titles[0]).strip()
    except Exception:
        pass
    return ""


def extract_content(html: str, url: str) -> ContentExtractResult:
    """Extract title/content/links from *html* with Trafilatura.

    The extraction is defensive: any Trafilatura failure degrades to an
    empty content with ``extractor="fallback"`` rather than raising, so the
    crawler can still return a usable (partial) ``WebDocument``.
    """
    title = ""
    content = ""
    extractor = "trafilatura"
    try:
        import trafilatura
        from trafilatura.metadata import extract_metadata

        try:
            raw_content = trafilatura.extract(
                html,
                url=url,
                include_links=False,
                favor_recall=True,
                include_comments=False,
            )
        except TypeError:  # pragma: no cover - older trafilatura signatures
            raw_content = trafilatura.extract(html, url=url)
        content = raw_content.strip() if raw_content else ""
        try:
            # trafilatura 2.x names the URL parameter ``default_url``.
            metadata = extract_metadata(html, default_url=url)
        except TypeError:  # pragma: no cover - older trafilatura signatures
            metadata = extract_metadata(html)
        if metadata is not None:
            md_title = getattr(metadata, "title", None)
            if md_title:
                title = str(md_title).strip()
    except Exception as exc:
        extractor = "fallback"
        logger.warning("trafilatura extraction failed for %s: %s", url, exc)

    if not title:
        title = _fallback_title(html)
    links = extract_links(html, url)
    return ContentExtractResult(
        title=title,
        content=content,
        links=links,
        extractor=extractor,
        content_extracted=bool(content),
    )


# ---------------------------------------------------------------------------
# Rule-mode schema extraction (CSS / XPath)
# ---------------------------------------------------------------------------


class SchemaExtractionError(Exception):
    """Raised when a rule-mode schema cannot be evaluated."""


def _css_nodes(html: str, selector: str, root: Any) -> list[Any]:
    """Evaluate a CSS selector against parsed HTML.

    Primary backend: lxml + cssselect (when installed).  Fallback:
    BeautifulSoup's ``select`` (when installed).  Returns a list of
    node-like objects supporting ``get(name)`` and text access.
    """
    if _CSSSELECT_AVAILABLE:
        try:
            from cssselect import GenericTranslator

            xpath = GenericTranslator().css_to_xpath(selector)
            nodes = root.xpath(xpath)
            if nodes:
                return list(nodes)
        except Exception as exc:
            logger.debug("cssselect failed for %r, trying bs4: %s", selector, exc)
    if _BS4_AVAILABLE:
        try:
            from bs4 import BeautifulSoup

            soup = BeautifulSoup(html, "html.parser")
            return list(soup.select(selector))
        except Exception as exc:
            raise SchemaExtractionError(
                f"CSS selector {selector!r} could not be evaluated: {exc}"
            ) from exc
    raise SchemaExtractionError(
        "CSS rule extraction needs 'cssselect' or 'beautifulsoup4'; install the "
        "[crawler] extra or use XPath rules"
    )


def _node_text(node: Any) -> str:
    text_content = getattr(node, "text_content", None)
    if callable(text_content):
        return str(text_content()).strip()
    get_text = getattr(node, "get_text", None)
    if callable(get_text):
        try:
            return str(get_text(" ", strip=True)).strip()
        except TypeError:
            return str(get_text()).strip()
    return ""


def _evaluate_field(html: str, root: Any, field: SchemaField) -> Any:
    """Evaluate one :class:`SchemaField` and return its extracted value."""
    if field.mode == "xpath":
        nodes = list(root.xpath(field.selector))
    else:
        nodes = _css_nodes(html, field.selector, root)

    if not nodes:
        return [] if field.multiple else field.default

    def _value(node: Any) -> Any:
        if field.attribute is not None:
            attr = node.get(field.attribute)
            return attr.strip() if isinstance(attr, str) else attr
        return _node_text(node)

    if field.multiple:
        return [_value(n) for n in nodes]
    return _value(nodes[0])


class RuleSchemaExtractor:
    """Evaluate a :class:`SchemaSpec` against HTML using CSS/XPath rules.

    XPath is always available (lxml).  CSS prefers ``cssselect`` and falls
    back to BeautifulSoup; when neither is installed a
    :class:`SchemaExtractionError` carries the installation diagnostic.
    """

    def extract(self, html: str, schema: SchemaSpec) -> dict[str, Any]:
        """Return ``{field: value}`` for every rule in *schema*."""
        if not schema.fields:
            return {}
        try:
            import lxml.html

            root = lxml.html.fromstring(html)
        except Exception as exc:
            raise SchemaExtractionError(f"HTML parse failed for schema: {exc}") from exc
        result: dict[str, Any] = {}
        for item in schema.fields:
            try:
                result[item.field] = _evaluate_field(html, root, item)
            except SchemaExtractionError:
                raise
            except Exception as exc:
                raise SchemaExtractionError(
                    f"field {item.field!r} ({item.mode} {item.selector!r}) failed: {exc}"
                ) from exc
        return result


# ---------------------------------------------------------------------------
# Optional LLM-mode schema extraction (Crawl4AI)
# ---------------------------------------------------------------------------


async def extract_llm_schema(url: str, schema: SchemaSpec) -> dict[str, Any] | None:
    """Run LLM-mode schema extraction with Crawl4AI when available.

    Returns ``None`` (never raises) when Crawl4AI is not installed or the
    extraction fails; the caller falls back to rule mode with a diagnostic.
    Crawl4AI re-fetches the URL with its own transport — it is only invoked
    *after* the robots pre-check and the anti-crawl checks have passed, and
    its result still flows through the same ``blocked`` classification.
    """
    if not _CRAWL4AI_AVAILABLE:
        return None
    try:
        from crawl4ai import (  # type: ignore[import-not-found]
            AsyncWebCrawler,
            LLMExtractionStrategy,
        )

        strategy = LLMExtractionStrategy(
            llm_config={"provider": "openai/gpt-4o-mini"},
            schema=SchemaSpec(fields=schema.fields).model_dump(mode="json"),
            extraction_type="schema",
            verbose=False,
        )
        async with AsyncWebCrawler(verbose=False) as crawler:
            result = await crawler.arun(
                url=url,
                extraction_strategy=strategy,
                bypass_cache=True,
            )
        raw = getattr(result, "extracted_content", None)
        if raw is None:
            return None
        if isinstance(raw, dict):
            return raw
        import json

        if isinstance(raw, str) and raw.strip():
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else None
        return None
    except Exception as exc:  # pragma: no cover - optional heavy dependency
        logger.warning("crawl4ai LLM extraction failed for %s: %s", url, exc)
        return None
