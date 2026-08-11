"""Content/schema extraction tests (offline, local HTML fixtures)."""

from __future__ import annotations

import pytest

from academic_intelligence.webcrawler import extractors as extractor_module
from academic_intelligence.webcrawler.extractors import (
    RuleSchemaExtractor,
    SchemaExtractionError,
    crawl4ai_available,
    cssselect_available,
    extract_content,
    extract_llm_schema,
)
from academic_intelligence.webcrawler.models import SchemaField, SchemaSpec

from .fixtures import ARTICLE_HTML, CHALLENGE_HTML, PLAIN_HTML


def test_extract_content_article() -> None:
    result = extract_content(ARTICLE_HTML, "https://example.com/article")

    assert result.extractor == "trafilatura"
    assert result.content_extracted is True
    assert "Attention Is All You Need" in result.title
    assert "Transformer" in result.content
    assert "attention mechanisms" in result.content
    assert any("https://external.example.com/related" in link for link in result.links)
    assert all(link.startswith(("http://", "https://")) for link in result.links)


def test_extract_content_plain_page_yields_no_content() -> None:
    result = extract_content(PLAIN_HTML, "https://example.com/plain")

    assert result.extractor == "trafilatura"
    assert result.content_extracted is False
    assert result.content == ""
    assert result.links == []


def test_extract_links_are_absolute_and_deduplicated() -> None:
    html = (
        '<html><body><a href="/a">A</a><a href="/a">A again</a>'
        '<a href="https://x.test/b">B</a><a href="mailto:x@y.z">mail</a></body></html>'
    )
    links = extractor_module.extract_links(html, "https://example.com/")
    assert links == [
        "https://example.com/a",
        "https://x.test/b",
    ]


def test_rule_schema_css_xpath_attribute() -> None:
    schema = SchemaSpec(
        fields=[
            SchemaField(field="heading", selector="h1", mode="css"),
            SchemaField(
                field="paras",
                selector="//article/p",
                mode="xpath",
                multiple=True,
            ),
            SchemaField(
                field="external_href",
                selector="a[href^='https://external']",
                mode="css",
                attribute="href",
            ),
            SchemaField(field="absent", selector=".nope", mode="css", default=None),
        ]
    )
    extracted = RuleSchemaExtractor().extract(ARTICLE_HTML, schema)

    assert extracted["heading"] == "Attention Is All You Need"
    assert len(extracted["paras"]) >= 2
    assert extracted["external_href"] == "https://external.example.com/related"
    assert extracted["absent"] is None


def test_rule_schema_css_falls_back_to_bs4(monkeypatch: pytest.MonkeyPatch) -> None:
    # Simulate cssselect being absent; the bs4 backend must still evaluate
    # CSS selectors.
    monkeypatch.setattr(extractor_module, "_CSSSELECT_AVAILABLE", False)
    monkeypatch.setattr(extractor_module, "_BS4_AVAILABLE", True)
    schema = SchemaSpec(
        fields=[
            SchemaField(field="heading", selector="h1", mode="css"),
        ]
    )
    extracted = RuleSchemaExtractor().extract(ARTICLE_HTML, schema)
    assert extracted["heading"] == "Attention Is All You Need"


def test_rule_schema_css_unavailable_diagnostic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(extractor_module, "_CSSSELECT_AVAILABLE", False)
    monkeypatch.setattr(extractor_module, "_BS4_AVAILABLE", False)
    schema = SchemaSpec(fields=[SchemaField(field="h", selector="h1", mode="css")])
    with pytest.raises(SchemaExtractionError, match="cssselect"):
        RuleSchemaExtractor().extract(ARTICLE_HTML, schema)


def test_rule_schema_invalid_selector_raises() -> None:
    schema = SchemaSpec(fields=[SchemaField(field="h", selector="not a selector [", mode="css")])
    with pytest.raises(SchemaExtractionError):
        RuleSchemaExtractor().extract(ARTICLE_HTML, schema)


def test_cssselect_backend_flag() -> None:
    # The primary CSS backend should be available in this environment.
    assert cssselect_available() is True


@pytest.mark.asyncio
async def test_extract_llm_schema_unavailable_returns_none() -> None:
    # crawl4ai is not installed here → None (fall back to rule mode).
    assert crawl4ai_available() is False
    schema = SchemaSpec(fields=[SchemaField(field="h", selector="h1", mode="css")])
    assert await extract_llm_schema("https://example.com/x", schema) is None


def test_challenge_detection_markers() -> None:
    from academic_intelligence.webcrawler.crawler import (
        _CHALLENGE_MARKERS,
        _detect_challenge,
    )

    marker = _detect_challenge(CHALLENGE_HTML)
    assert marker is not None
    assert marker in _CHALLENGE_MARKERS
    assert _detect_challenge(ARTICLE_HTML) is None
