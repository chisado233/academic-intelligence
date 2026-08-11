"""Tests for the full-text pipeline (locator / downloader / parser / segmenter)."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from academic_intelligence.core.models import Evidence, Paper
from academic_intelligence.core.types import SourceType
from academic_intelligence.fulltext.downloader import FulltextDownloader
from academic_intelligence.fulltext.exceptions import (
    FulltextDownloadError,
    NoLegalOAFulltextError,
)
from academic_intelligence.fulltext.locator import DEFAULT_SOURCES, FulltextLocator
from academic_intelligence.fulltext.parser import ParsedPage, PDFParser, TextLine
from academic_intelligence.fulltext.pipeline import FulltextPipeline
from academic_intelligence.fulltext.segmenter import Segmenter

FIXTURE_PDF = Path(__file__).resolve().parent / "fixtures" / "sample_paper.pdf"


class FakeHTTP:
    """Stub of the ``HTTPClient`` surface the pipeline uses (offline tests).

    ``json_by`` maps a URL substring to the JSON payload to return from
    ``get_json``; ``get`` returns queued ``httpx.Response`` objects or a
    default PDF response. All calls are recorded for priority assertions.
    """

    def __init__(
        self,
        *,
        json_by: dict[str, dict] | None = None,
        get_queue: list[httpx.Response] | None = None,
        pdf_bytes: bytes | None = None,
    ) -> None:
        self.json_by = json_by or {}
        self.get_queue = list(get_queue or [])
        self.pdf_bytes = pdf_bytes or b""
        self.json_calls: list[tuple[str, dict]] = []
        self.get_calls: list[tuple[str, dict]] = []

    async def get_json(self, url: str, **kwargs: object) -> dict:
        self.json_calls.append((url, kwargs))
        for key, payload in self.json_by.items():
            if key in url:
                return payload
        return {}

    async def get(self, url: str, **kwargs: object) -> httpx.Response:
        self.get_calls.append((url, kwargs))
        if self.get_queue:
            return self.get_queue.pop(0)
        return httpx.Response(status_code=200, content=self.pdf_bytes)


def _unpaywall_oa_payload() -> dict:
    return {
        "doi": "10.1000/xyz",
        "is_oa": True,
        "best_oa_location": {
            "url": "https://oa.example.com/paper",
            "url_for_pdf": "https://oa.example.com/paper.pdf",
            "license": "cc-by",
            "host_type": "repository",
        },
        "oa_locations": [
            {
                "url_for_pdf": "https://oa.example.com/paper.pdf",
                "license": "cc-by",
            }
        ],
    }


def _make_paper(
    *,
    paper_id: str = "p1",
    doi: str | None = "10.1000/xyz",
    arxiv_id: str | None = "2301.00001",
) -> Paper:
    return Paper(id=paper_id, title="Sample Paper", doi=doi, arxiv_id=arxiv_id)


# ---------------------------------------------------------------------------
# Locator: priority order and copyright red lines
# ---------------------------------------------------------------------------


async def test_locator_prefers_unpaywall_first() -> None:
    http = FakeHTTP(json_by={"api.unpaywall.org": _unpaywall_oa_payload()})
    locator = FulltextLocator(http, unpaywall_email="a@b.com", core_api_key="k")
    location = await locator.locate(_make_paper())
    assert location is not None
    assert location.source == "unpaywall"
    assert location.url == "https://oa.example.com/paper.pdf"
    assert location.license == "cc-by"
    # CORE must not be queried when Unpaywall already hit.
    assert not any("api.core.ac.uk" in url for url, _ in http.json_calls)


async def test_locator_falls_back_to_core_when_unpaywall_has_no_oa() -> None:
    http = FakeHTTP(
        json_by={
            "api.unpaywall.org": {"is_oa": False},
            "api.core.ac.uk": {
                "results": [{"id": "C1", "downloadUrl": "https://core.example.com/x.pdf"}]
            },
        }
    )
    locator = FulltextLocator(http, unpaywall_email="a@b.com", core_api_key="k")
    location = await locator.locate(_make_paper())
    assert location is not None
    assert location.source == "core"
    assert location.url == "https://core.example.com/x.pdf"
    # Sources are tried in priority order (unpaywall then core).
    urls = [url for url, _ in http.json_calls]
    assert urls[0].startswith("https://api.unpaywall.org")
    assert urls[1].startswith("https://api.core.ac.uk")


async def test_locator_falls_back_to_arxiv() -> None:
    http = FakeHTTP(
        json_by={
            "api.unpaywall.org": {"is_oa": False},
            "api.core.ac.uk": {"results": []},
        }
    )
    locator = FulltextLocator(http, unpaywall_email="a@b.com", core_api_key="k")
    location = await locator.locate(_make_paper(doi=None))
    assert location is not None
    assert location.source == "arxiv"
    assert location.url == "https://arxiv.org/pdf/2301.00001"
    # No DOI -> no API sources are queried at all (M15: arXiv-only path).
    assert http.json_calls == []


async def test_locator_no_identifiers_yields_no_location() -> None:
    http = FakeHTTP()
    locator = FulltextLocator(http, unpaywall_email="a@b.com", core_api_key="k")
    location = await locator.locate(_make_paper(doi=None, arxiv_id=None))
    assert location is None
    assert http.json_calls == []


async def test_locator_excludes_social_hosts_researchgate() -> None:
    """ResearchGate copies are never auto-download sources (red line §6.4)."""
    payload = {
        "is_oa": True,
        "best_oa_location": {
            "url_for_pdf": "https://www.researchgate.net/publication/1/file.pdf",
            "license": "cc-by",
        },
        "oa_locations": [{"url_for_pdf": "https://oa.example.com/ok.pdf"}],
    }
    http = FakeHTTP(json_by={"api.unpaywall.org": payload})
    locator = FulltextLocator(http, unpaywall_email="a@b.com", core_api_key="k")
    location = await locator.locate(_make_paper())
    assert location is not None
    assert location.url == "https://oa.example.com/ok.pdf"


async def test_locator_unpaywall_requires_email() -> None:
    """Without a polite-pool email Unpaywall is skipped, CORE still queried."""
    http = FakeHTTP(
        json_by={
            "api.core.ac.uk": {
                "results": [{"downloadUrl": "https://core.example.com/y.pdf"}]
            }
        }
    )
    locator = FulltextLocator(http, unpaywall_email=None, core_api_key="k")
    location = await locator.locate(_make_paper())
    assert location is not None
    assert location.source == "core"
    assert not any("api.unpaywall.org" in url for url, _ in http.json_calls)


# ---------------------------------------------------------------------------
# Locator: Europe PMC reuses the adapter's own OA evidence (E4)
# ---------------------------------------------------------------------------


def _europe_pmc_evidence(raw: dict) -> Evidence:
    return Evidence(
        source=SourceType.EUROPE_PMC,
        source_url="https://europepmc.org/article/MED/32479259",
        confidence=0.90,
        raw_data=raw,
    )


def _europe_pmc_oa_payload() -> dict:
    """Europe PMC search payload: one open-access record with a PMCID."""
    return {
        "hitCount": 1,
        "resultList": {
            "result": [
                {
                    "id": "32479259",
                    "source": "MED",
                    "pmid": "32479259",
                    "pmcid": "PMC7292645",
                    "doi": "10.1000/xyz",
                    "title": "An open-access biomedicine study",
                    "pubYear": "2020",
                    "isOpenAccess": "Y",
                    "inEPMC": "Y",
                }
            ]
        },
    }


async def test_locator_europe_pmc_reuses_in_memory_evidence() -> None:
    """A paper that already carries Europe PMC OA evidence hits with no request."""
    paper = Paper(
        id="p-epmc",
        title="OA paper",
        doi="10.1000/xyz",
        evidence_list=[
            _europe_pmc_evidence(
                {
                    "is_open_access": True,
                    "pmcid": "PMC7292645",
                    "fulltext_url": (
                        "https://www.ebi.ac.uk/europepmc/webservices/"
                        "rest/PMC7292645/fullTextXML"
                    ),
                }
            )
        ],
    )
    http = FakeHTTP()
    locator = FulltextLocator(http, unpaywall_email=None, core_api_key=None)
    location = await locator.locate(paper, sources=("europe_pmc",))
    assert location is not None
    assert location.source == "europe_pmc"
    assert location.url.endswith("/PMC7292645/fullTextXML")
    # No network call: the evidence was already on the paper (E4).
    assert http.json_calls == []


async def test_locator_europe_pmc_rejects_non_oa_evidence() -> None:
    """A Europe PMC record marked non-OA yields no location (no request)."""
    paper = Paper(
        id="p-paywalled",
        title="Paywalled paper",
        doi="10.1000/paywalled",
        evidence_list=[
            _europe_pmc_evidence(
                {
                    "is_open_access": False,
                    "pmcid": None,
                    "fulltext_url": None,
                }
            )
        ],
    )
    http = FakeHTTP()
    locator = FulltextLocator(http, unpaywall_email=None, core_api_key=None)
    location = await locator.locate(paper, sources=("europe_pmc",))
    assert location is None
    assert http.json_calls == []


async def test_locator_europe_pmc_queries_api_when_no_evidence() -> None:
    """A paper resolved via another source still finds Europe PMC OA text."""
    http = FakeHTTP(
        get_queue=[httpx.Response(status_code=200, json=_europe_pmc_oa_payload())]
    )
    locator = FulltextLocator(http, unpaywall_email=None, core_api_key=None)
    location = await locator.locate(_make_paper(), sources=("europe_pmc",))
    assert location is not None
    assert location.source == "europe_pmc"
    assert location.url.endswith("/PMC7292645/fullTextXML")
    assert any("europepmc" in url for url, _ in http.get_calls)


async def test_locator_europe_pmc_is_last_in_default_priority() -> None:
    """Europe PMC is the final fallback, after Unpaywall / CORE / arXiv."""
    assert DEFAULT_SOURCES == ("unpaywall", "core", "arxiv", "europe_pmc")


async def test_locator_europe_pmc_requires_doi() -> None:
    """Without a DOI the Europe PMC path is skipped entirely."""
    http = FakeHTTP()
    locator = FulltextLocator(http, unpaywall_email=None, core_api_key=None)
    location = await locator.locate(_make_paper(doi=None), sources=("europe_pmc",))
    assert location is None
    assert http.json_calls == []


# ---------------------------------------------------------------------------
# Pipeline: end-to-end, download failure, no-OA rejection
# ---------------------------------------------------------------------------


async def test_pipeline_fetch_end_to_end(tmp_path) -> None:
    pdf = FIXTURE_PDF.read_bytes()
    http = FakeHTTP(json_by={"api.unpaywall.org": _unpaywall_oa_payload()}, pdf_bytes=pdf)
    pipeline = FulltextPipeline(
        http_client=http,
        downloader=FulltextDownloader(http, cache_dir=str(tmp_path / "cache")),
        unpaywall_email="a@b.com",
        core_api_key="k",
    )
    try:
        fulltext = await pipeline.fetch(_make_paper())
        assert fulltext.source == "unpaywall"
        assert fulltext.oa_license == "cc-by"
        assert fulltext.paragraph_count == len(fulltext.segments)
        assert fulltext.paragraph_count >= 6
        # Paragraphs carry page numbers (some on page 2).
        assert any(segment.page == 2 for segment in fulltext.segments)
        # The PDF is cached locally and the path recorded.
        assert fulltext.file_path is not None
        assert Path(fulltext.file_path).exists()
    finally:
        await pipeline.close()


async def test_pipeline_rejects_no_legal_oa_explicitly(tmp_path) -> None:
    """No legal OA anywhere -> explicit '无合法 OA 全文' rejection, no bypass."""
    http = FakeHTTP(
        json_by={
            "api.unpaywall.org": {"is_oa": False},
            "api.core.ac.uk": {"results": []},
        }
    )
    pipeline = FulltextPipeline(
        http_client=http,
        downloader=FulltextDownloader(http, cache_dir=str(tmp_path / "cache")),
    )
    try:
        with pytest.raises(NoLegalOAFulltextError) as exc:
            await pipeline.fetch(_make_paper(arxiv_id=None))
        assert "无合法 OA 全文" in str(exc.value)
        assert exc.value.sources_attempted == [
            "unpaywall",
            "core",
            "arxiv",
            "europe_pmc",
        ]
    finally:
        await pipeline.close()


async def test_pipeline_download_http_error(tmp_path) -> None:
    http = FakeHTTP(
        json_by={"api.unpaywall.org": _unpaywall_oa_payload()},
        get_queue=[httpx.Response(status_code=403, content=b"forbidden")],
    )
    pipeline = FulltextPipeline(
        http_client=http,
        downloader=FulltextDownloader(http, cache_dir=str(tmp_path / "cache")),
    )
    try:
        with pytest.raises(FulltextDownloadError) as exc:
            await pipeline.fetch(_make_paper())
        assert exc.value.http_status == 403
    finally:
        await pipeline.close()


async def test_pipeline_rejects_non_pdf_body(tmp_path) -> None:
    """An HTML error/landing page must not be stored as a 'PDF'."""
    http = FakeHTTP(
        json_by={"api.unpaywall.org": _unpaywall_oa_payload()},
        get_queue=[httpx.Response(status_code=200, content=b"<html>landing</html>")],
    )
    pipeline = FulltextPipeline(
        http_client=http,
        downloader=FulltextDownloader(http, cache_dir=str(tmp_path / "cache")),
    )
    try:
        with pytest.raises(FulltextDownloadError) as exc:
            await pipeline.fetch(_make_paper())
        assert "not a PDF" in str(exc.value)
    finally:
        await pipeline.close()


async def test_downloader_caches_and_reuses(tmp_path) -> None:
    http = FakeHTTP(pdf_bytes=b"%PDF-1.4 fake pdf content")
    downloader = FulltextDownloader(http, cache_dir=str(tmp_path / "cache"))
    first = await downloader.download("https://example.com/a.pdf", "paper-1")
    assert first.exists()
    calls_after_first = len(http.get_calls)
    second = await downloader.download("https://example.com/a.pdf", "paper-1")
    assert second == first
    assert len(http.get_calls) == calls_after_first  # no second network call


# ---------------------------------------------------------------------------
# Parser (pdfplumber default; PyMuPDF optional)
# ---------------------------------------------------------------------------


def test_parser_pdfplumber_extracts_pages() -> None:
    pages = PDFParser().parse(FIXTURE_PDF)
    assert len(pages) == 2
    assert pages[0].page == 1
    assert pages[1].page == 2
    texts = [line.text for page in pages for line in page.lines]
    assert any("Attention Is All You Need" in text for text in texts)
    sizes = [line.size for page in pages for line in page.lines if line.size]
    assert sizes and max(sizes) == pytest.approx(20.0)


def test_parser_pymupdf_optional_backend() -> None:
    pytest.importorskip("fitz")
    pages = PDFParser(backend="pymupdf").parse(FIXTURE_PDF)
    assert len(pages) == 2
    texts = [line.text for page in pages for line in page.lines]
    assert any("Attention Is All You Need" in text for text in texts)


# ---------------------------------------------------------------------------
# Segmenter: paragraph splitting (blank-line/font heuristics) + page numbers
# ---------------------------------------------------------------------------


def test_segmenter_splits_paragraphs_headings_and_pages() -> None:
    pages = PDFParser().parse(FIXTURE_PDF)
    segments = Segmenter().segment(pages)
    assert len(segments) == 7
    headings = {segment.heading for segment in segments}
    assert {"Abstract", "1 Introduction", "2 Method"} <= headings
    # "We propose..." and "Experiments..." are separate paragraphs of Abstract.
    abstract = [s for s in segments if s.heading == "Abstract"]
    assert any("We propose" in s.text for s in abstract)
    assert any("Experiments" in s.text for s in abstract)
    # Page numbers are recorded per paragraph (2 paragraphs on page 2).
    assert sum(1 for s in segments if s.page == 2) == 2
    assert all(1 <= s.page <= 2 for s in segments)


def test_segmenter_paragraph_breaks_on_gaps_and_joins_lines() -> None:
    pages = [
        ParsedPage(
            page=1,
            lines=[
                TextLine(text="1 Introduction", top=50.0, size=14.0),
                TextLine(text="First sentence of the intro.", top=80.0, size=11.0),
                TextLine(text="Second sentence of the intro.", top=98.0, size=11.0),
                TextLine(text="A new paragraph.", top=130.0, size=11.0),
            ],
        ),
        ParsedPage(
            page=2,
            lines=[
                TextLine(text="2 Method", top=50.0, size=14.0),
                TextLine(text="Method paragraph.", top=80.0, size=11.0),
            ],
        ),
    ]
    segments = Segmenter().segment(pages)
    assert len(segments) == 3
    first = segments[0]
    assert first.heading == "1 Introduction"
    assert first.text == (
        "First sentence of the intro. Second sentence of the intro."
    )
    assert first.page == 1
    assert segments[1].text == "A new paragraph."
    assert segments[1].page == 1
    assert segments[2].heading == "2 Method"
    assert segments[2].text == "Method paragraph."
    assert segments[2].page == 2


def test_segmenter_handles_empty_pages() -> None:
    assert Segmenter().segment([]) == []
