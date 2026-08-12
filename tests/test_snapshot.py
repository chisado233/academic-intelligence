"""Tests for the OpenAlex snapshot feature (``academic_intelligence.snapshot``).

Fully offline: tiny gzip JSONL fixtures stand in for real partitions, and a
fake HTTP layer replaces manifest/download network calls.  Coverage:

- manifest parsing (current ``files`` shape + legacy ``entries`` shape,
  ``s3://`` URL rewrite) and discovery fallback chain;
- download resume via ``Range``, restart-on-ignored-range, gz verification,
  skip-already-downloaded, failure reporting;
- build → SQLite index: works rows, inverted citations, resume of a partial
  build, no-op rebuild, date-change rebuild;
- CLI: help surface, status, enable/disable routing switch, download with
  size notice + ``--date`` filter, build;
- routing: ``trace-citing --use-snapshot`` hit/miss/not-built/DOI, the
  enable/disable default switch, and the ``trace-profiles`` API-only notice.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

import academic_intelligence.cli_snapshot as cli_snapshot
import academic_intelligence.cli_trace as cli_trace
from academic_intelligence.cli import app
from academic_intelligence.snapshot import SnapshotError
from academic_intelligence.snapshot.build import build_snapshot
from academic_intelligence.snapshot.download import (
    DownloadSummary,
    download_all,
    download_partition,
)
from academic_intelligence.snapshot.manifest import (
    CURRENT_MANIFEST_URL,
    LEGACY_MANIFEST_URL,
    SnapshotManifest,
    fetch_manifest,
    parse_manifest,
    rewrite_s3_url,
)
from academic_intelligence.snapshot.store import (
    SnapshotStore,
    normalize_doi,
    normalize_work_id,
    read_routing_config,
    write_routing_config,
)
from academic_intelligence.trace.citing import CitingPaper, CitingResult

runner = CliRunner()

# ---------------------------------------------------------------------------
# Fixtures: two mini partitions forming a small citation graph
#
#   W100 refs [W200]            W101 refs [W200, W300]   W102 refs []
#   W103 refs [W300]            W104 refs [W100, W200]
#
# Reverse edges: W100 ← W104; W200 ← W100, W101, W104; W300 ← W101, W103.
# ---------------------------------------------------------------------------

PART1_DATE = "2024-01-01"
PART2_DATE = "2024-02-01"
PART1_KEY = f"{PART1_DATE}_part_0000.gz"
PART2_KEY = f"{PART2_DATE}_part_0000.gz"

WORK_LINES: dict[str, list[dict[str, Any]]] = {
    PART1_KEY: [
        {
            "id": "https://openalex.org/W100",
            "title": "Alpha",
            "display_name": "Alpha",
            "publication_year": 2023,
            "doi": "https://doi.org/10.1000/1",
            "cited_by_count": 5,
            "referenced_works": ["https://openalex.org/W200"],
            "authorships": [
                {
                    "author": {"id": "https://openalex.org/A1", "display_name": "Alice"},
                    "institutions": [{"display_name": "MIT"}],
                }
            ],
        },
        {
            "id": "https://openalex.org/W101",
            "title": "Beta",
            "publication_year": 2024,
            "doi": "https://doi.org/10.1000/2",
            "cited_by_count": 2,
            "referenced_works": [
                "https://openalex.org/W200",
                "https://openalex.org/W300",
            ],
            "authorships": [],
        },
        {
            "id": "https://openalex.org/W102",
            "title": "Gamma",
            "publication_year": 2022,
            "doi": None,
            "cited_by_count": 0,
            "referenced_works": [],
            "authorships": [],
        },
    ],
    PART2_KEY: [
        {
            "id": "https://openalex.org/W103",
            "title": "Delta",
            "publication_year": 2025,
            "doi": "https://doi.org/10.1000/3",
            "cited_by_count": 1,
            "referenced_works": ["https://openalex.org/W300"],
            "authorships": [],
        },
        {
            "id": "https://openalex.org/W104",
            "title": "Epsilon",
            "publication_year": 2024,
            "doi": "https://doi.org/10.1000/4",
            "cited_by_count": 9,
            "referenced_works": [
                "https://openalex.org/W100",
                "https://openalex.org/W200",
            ],
            "authorships": [],
        },
    ],
}


def _gz_bytes(works: list[dict[str, Any]]) -> bytes:
    """Gzip a list of work dicts into JSONL bytes."""
    lines = [json.dumps(work, ensure_ascii=False) for work in works]
    return gzip.compress(("\n".join(lines) + "\n").encode("utf-8"))


def _write_downloads(dir: Path, keys: list[str]) -> None:
    """Write gzip partition fixtures into ``<dir>/downloads/``."""
    dest = dir / "downloads"
    dest.mkdir(parents=True, exist_ok=True)
    for key in keys:
        (dest / key).write_bytes(_gz_bytes(WORK_LINES[key]))


def _fixture_manifest(
    date: str = "2024-03-01", include: list[str] | None = None
) -> SnapshotManifest:
    """A parsed manifest describing the two fixture partitions."""
    keys = include if include is not None else [PART1_KEY, PART2_KEY]
    return parse_manifest(
        {
            "date": date,
            "format": "jsonl",
            "entity": "works",
            "record_count": sum(len(WORK_LINES[k]) for k in keys),
            "files": [
                {
                    "url": (
                        "s3://openalex/data/jsonl/works/"
                        f"updated_date={k.split('_')[0]}/part_0000.gz"
                    ),
                    "meta": {
                        "content_length": len(_gz_bytes(WORK_LINES[k])),
                        "record_count": len(WORK_LINES[k]),
                    },
                }
                for k in keys
            ],
        },
        source_url=CURRENT_MANIFEST_URL,
    )


def _build_fixture_index(snapshot_dir: Path, keys: list[str] | None = None) -> None:
    """Run the real build over fixture downloads (routing tests' setup)."""
    _write_downloads(snapshot_dir, keys or [PART1_KEY, PART2_KEY])
    result = build_snapshot(_fixture_manifest(), snapshot_dir)
    assert result.works_count == 5
    assert result.citation_count == 6


# ---------------------------------------------------------------------------
# Fake HTTP layers
# ---------------------------------------------------------------------------


class _FakeStatusError(httpx.HTTPStatusError):
    """A 404-style HTTPStatusError standing in for a missing manifest URL."""

    def __init__(self, url: str) -> None:
        request = httpx.Request("GET", url)
        super().__init__(
            f"404 for {url}", request=request, response=httpx.Response(404, request=request)
        )


class FakeManifestHTTP:
    """HTTPClient stand-in: maps URL → payload dict or exception."""

    def __init__(self, payloads: dict[str, Any]) -> None:
        self.payloads = payloads
        self.calls: list[str] = []

    async def get_json(self, url: str, params: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        self.calls.append(url)
        payload = self.payloads.get(url)
        if payload is None:
            raise _FakeStatusError(url)
        if isinstance(payload, BaseException):
            raise payload
        return payload


class _FakeStreamResponse:
    def __init__(self, status_code: int, data: bytes) -> None:
        self.status_code = status_code
        self._data = data
        self.headers: dict[str, str] = {}

    async def aread(self) -> bytes:
        return self._data

    async def aiter_bytes(self) -> Any:
        for start in range(0, len(self._data), 7):
            yield self._data[start : start + 7]


class _FakeStreamContext:
    def __init__(self, response: _FakeStreamResponse) -> None:
        self._response = response

    async def __aenter__(self) -> _FakeStreamResponse:
        return self._response

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class FakeDownloadClient:
    """httpx.AsyncClient stand-in: URL → bytes, honoring Range with 206."""

    def __init__(
        self,
        url_data: dict[str, bytes],
        statuses: dict[str, int] | None = None,
    ) -> None:
        self.url_data = url_data
        self.statuses = statuses or {}
        self.calls: list[tuple[str, dict[str, str] | None]] = []

    def stream(
        self,
        method: str,
        url: str,
        headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> _FakeStreamContext:
        self.calls.append((url, headers))
        if url in self.statuses:
            return _FakeStreamContext(_FakeStreamResponse(self.statuses[url], b""))
        data = self.url_data.get(url, b"")
        if headers and headers.get("Range"):
            start = int(headers["Range"].split("=")[1].split("-")[0])
            return _FakeStreamContext(_FakeStreamResponse(206, data[start:]))
        return _FakeStreamContext(_FakeStreamResponse(200, data))


def _fixture_urls() -> dict[str, bytes]:
    """Map each manifest file URL to its gzip payload (aligned with the manifest)."""
    return {file.url: _gz_bytes(WORK_LINES[file.key]) for file in _fixture_files()}


def _fixture_files() -> list[Any]:
    return _fixture_manifest().files


# ---------------------------------------------------------------------------
# manifest: parsing + discovery
# ---------------------------------------------------------------------------


def test_rewrite_s3_url() -> None:
    assert (
        rewrite_s3_url("s3://openalex/data/jsonl/works/updated_date=2026-06-24/part_0000.gz")
        == "https://openalex.s3.amazonaws.com/data/jsonl/works/updated_date=2026-06-24/part_0000.gz"
    )
    assert (
        rewrite_s3_url("https://openalex.s3.amazonaws.com/x")
        == "https://openalex.s3.amazonaws.com/x"
    )


def test_parse_manifest_current_shape() -> None:
    manifest = _fixture_manifest()
    assert manifest.snapshot_date == "2024-03-01"
    assert manifest.format == "jsonl"
    assert manifest.record_count == 5
    assert manifest.content_length is not None
    assert [f.key for f in manifest.files] == [PART1_KEY, PART2_KEY]
    assert [f.date for f in manifest.files] == [PART1_DATE, PART2_DATE]
    assert manifest.files[0].url.startswith("https://openalex.s3.amazonaws.com/")
    assert manifest.files[0].size == len(_gz_bytes(WORK_LINES[PART1_KEY]))
    assert manifest.files[0].record_count == 3


def test_parse_manifest_legacy_entries() -> None:
    data: dict[str, Any] = {
        "entries": [
            {
                "url": "s3://openalex/data/works/updated_date=2024-01-01/part_000.gz",
                "meta": {"content_length": 10, "record_count": 3},
            },
            {
                "url": "s3://openalex/data/works/updated_date=2024-02-01/part_000.gz",
                "meta": {"content_length": 20, "record_count": 2},
            },
        ]
    }
    manifest = parse_manifest(data, source_url=LEGACY_MANIFEST_URL)
    assert manifest.snapshot_date == "2024-02-01"  # max updated_date
    assert manifest.content_length == 30
    assert manifest.record_count == 5
    assert manifest.files[0].url.startswith("https://openalex.s3.amazonaws.com/")
    assert manifest.files[0].key == "2024-01-01_part_000.gz"


def test_parse_manifest_bad_shape_raises() -> None:
    with pytest.raises(SnapshotError, match="manifest"):
        parse_manifest({"other": []}, source_url="http://x")
    with pytest.raises(SnapshotError, match="url"):
        parse_manifest({"files": [{"meta": {}}]}, source_url="http://x")


def test_fetch_manifest_current_hit() -> None:
    http = FakeManifestHTTP({CURRENT_MANIFEST_URL: _fixture_manifest_payload()})
    manifest = _fetch_manifest_sync(http)
    assert manifest.url == CURRENT_MANIFEST_URL
    assert manifest.snapshot_date == "2024-03-01"
    assert http.calls == [CURRENT_MANIFEST_URL]


def test_fetch_manifest_falls_back_to_legacy() -> None:
    payload: dict[str, Any] = {
        "entries": [
            {
                "url": "s3://openalex/data/works/updated_date=2024-01-01/part_000.gz",
                "meta": {"content_length": 1, "record_count": 1},
            }
        ]
    }
    http = FakeManifestHTTP({LEGACY_MANIFEST_URL: payload})
    manifest = _fetch_manifest_sync(http)
    assert manifest.url == LEGACY_MANIFEST_URL
    assert manifest.snapshot_date == "2024-01-01"
    assert http.calls == [CURRENT_MANIFEST_URL, LEGACY_MANIFEST_URL]


def test_fetch_manifest_all_probes_fail() -> None:
    http = FakeManifestHTTP({})
    with pytest.raises(SnapshotError, match="无法获取 OpenAlex works 快照清单"):
        _fetch_manifest_sync(http)


def test_fetch_manifest_non_object_payload_falls_back() -> None:
    http = FakeManifestHTTP(
        {CURRENT_MANIFEST_URL: [1, 2], LEGACY_MANIFEST_URL: _fixture_manifest_payload()}
    )
    manifest = _fetch_manifest_sync(http)
    assert manifest.url == LEGACY_MANIFEST_URL


def _fixture_manifest_payload() -> dict[str, Any]:
    """The raw manifest dict the fake HTTP serves (mirrors the live shape)."""
    return {
        "date": "2024-03-01",
        "format": "jsonl",
        "entity": "works",
        "record_count": 5,
        "files": [
            {
                "url": (
                    f"s3://openalex/data/jsonl/works/updated_date={k.split('_')[0]}/part_0000.gz"
                ),
                "meta": {
                    "content_length": len(_gz_bytes(WORK_LINES[k])),
                    "record_count": len(WORK_LINES[k]),
                },
            }
            for k in (PART1_KEY, PART2_KEY)
        ],
    }


def _fetch_manifest_sync(http: FakeManifestHTTP) -> SnapshotManifest:
    import asyncio

    return asyncio.run(fetch_manifest(http=http))


# ---------------------------------------------------------------------------
# store: schema, queries, routing config
# ---------------------------------------------------------------------------


def test_store_schema_and_queries(tmp_path: Path) -> None:
    store = SnapshotStore(tmp_path / "index.db")
    store.connect()
    try:
        store.insert_works(
            [
                {
                    "id": "W100",
                    "title": "Alpha",
                    "year": 2023,
                    "doi": "10.1000/1",
                    "cited_by_count": 5,
                },
                {"id": "W101", "title": "Beta", "year": 2024, "doi": None, "cited_by_count": 2},
            ]
        )
        store.insert_citation_pairs([("W200", "W100"), ("W200", "W101"), ("W300", "W101")])
        stats = store.stats()
        assert stats == {"works_count": 2, "citation_count": 3}
        assert store.query_work("W100") == {
            "id": "W100",
            "title": "Alpha",
            "year": 2023,
            "doi": "10.1000/1",
            "cited_by_count": 5,
        }
        assert store.query_work("W999") is None
        assert store.query_work_by_doi("10.1000/1")["id"] == "W100"
        citing = store.query_citing("W200")
        assert [r["citing_id"] for r in citing] == ["W100", "W101"]  # DESC by cited
        assert citing[0]["title"] == "Alpha"
        assert store.query_citing("W999") == []
        assert not store.is_built()
        store.set_meta("2024-03-01", "built")
        assert store.is_built()
        meta = store.get_meta()
        assert meta is not None and meta["snapshot_date"] == "2024-03-01"
        assert meta["status"] == "built"
    finally:
        store.close()


def test_store_build_state_roundtrip(tmp_path: Path) -> None:
    store = SnapshotStore(tmp_path / "index.db")
    store.connect()
    try:
        assert store.built_part_keys() == set()
        store.mark_part_built("2024-01-01_part_0000.gz")
        assert store.is_part_built("2024-01-01_part_0000.gz")
        assert not store.is_part_built("2024-02-01_part_0000.gz")
        assert store.built_part_keys() == {"2024-01-01_part_0000.gz"}
    finally:
        store.close()


def test_normalize_helpers() -> None:
    assert normalize_work_id("W123") == "W123"
    assert normalize_work_id("https://openalex.org/W123/") == "W123"
    assert normalize_work_id("10.1000/1") is None
    assert normalize_work_id(None) is None
    assert normalize_doi("https://doi.org/10.1000/1") == "10.1000/1"
    assert normalize_doi("10.1000/1") == "10.1000/1"
    assert normalize_doi("not-a-doi") is None
    assert normalize_doi(None) is None


def test_routing_config_roundtrip(tmp_path: Path) -> None:
    assert read_routing_config(tmp_path) is None
    write_routing_config(tmp_path, True)
    assert read_routing_config(tmp_path) is True
    write_routing_config(tmp_path, False)
    assert read_routing_config(tmp_path) is False
    (tmp_path / "config.json").write_text("{broken", encoding="utf-8")
    assert read_routing_config(tmp_path) is None


# ---------------------------------------------------------------------------
# download: resume, verification, failures
# ---------------------------------------------------------------------------


async def test_download_partition_writes_verified_file(tmp_path: Path) -> None:
    urls = _fixture_urls()
    client = FakeDownloadClient(urls)
    file = _fixture_files()[0]
    dest = tmp_path / "downloads"
    result = await download_partition(file, dest, client=client)  # type: ignore[arg-type]
    assert result.is_file()
    assert file.key in result.name
    assert not result.with_name(result.name + ".part").exists()
    assert (dest / file.key).read_bytes() == urls[file.url]
    assert client.calls == [(file.url, {})]


async def test_download_partition_resumes_with_range(tmp_path: Path) -> None:
    urls = _fixture_urls()
    file = _fixture_files()[0]
    data = urls[file.url]
    dest = tmp_path / "downloads"
    dest.mkdir(parents=True)
    (dest / (file.key + ".part")).write_bytes(data[:11])

    client = FakeDownloadClient(urls)
    result = await download_partition(file, dest, client=client)  # type: ignore[arg-type]
    assert (dest / file.key).read_bytes() == data
    assert not result.with_name(result.name + ".part").exists()
    _, headers = client.calls[0]
    assert headers is not None and headers["Range"] == "bytes=11-"


async def test_download_partition_restarts_when_range_ignored(tmp_path: Path) -> None:
    urls = _fixture_urls()
    file = _fixture_files()[0]
    data = urls[file.url]
    dest = tmp_path / "downloads"
    dest.mkdir(parents=True)
    (dest / (file.key + ".part")).write_bytes(data[:11])

    class _NoRangeClient(FakeDownloadClient):
        def stream(  # type: ignore[override]
            self, method: str, url: str, headers: dict[str, str] | None = None, **kwargs: Any
        ) -> _FakeStreamContext:
            return _FakeStreamContext(_FakeStreamResponse(200, self.url_data.get(url, b"")))

    client = _NoRangeClient(urls)
    result = await download_partition(file, dest, client=client)  # type: ignore[arg-type]
    assert (dest / file.key).read_bytes() == data  # not doubled
    assert result.is_file()


async def test_download_partition_skips_existing_verified(tmp_path: Path) -> None:
    urls = _fixture_urls()
    file = _fixture_files()[0]
    dest = tmp_path / "downloads"
    dest.mkdir(parents=True)
    (dest / file.key).write_bytes(urls[file.url])

    client = FakeDownloadClient(urls)
    result = await download_partition(file, dest, client=client)  # type: ignore[arg-type]
    assert result.is_file()
    assert client.calls == []  # no network at all


async def test_download_partition_404_raises(tmp_path: Path) -> None:
    file = _fixture_files()[0]
    client = FakeDownloadClient({}, statuses={file.url: 404})
    with pytest.raises(SnapshotError, match="404"):
        await download_partition(file, tmp_path / "downloads", client=client)  # type: ignore[arg-type]


async def test_download_partition_corrupt_gz_raises(tmp_path: Path) -> None:
    file = _fixture_files()[0]
    client = FakeDownloadClient({file.url: b"this is not gzip at all"})
    with pytest.raises(SnapshotError, match="gz 校验失败"):
        await download_partition(file, tmp_path / "downloads", client=client)  # type: ignore[arg-type]
    assert not (tmp_path / "downloads" / file.key).exists()


async def test_download_all_summary_skip_and_fail(tmp_path: Path) -> None:
    urls = _fixture_urls()
    files = _fixture_files()
    dest = tmp_path / "downloads"
    dest.mkdir(parents=True)
    (dest / files[0].key).write_bytes(urls[files[0].url])  # pre-existing valid

    client = FakeDownloadClient(urls, statuses={files[1].url: 403})
    with pytest.raises(SnapshotError, match="1/2"):
        await download_all(files, dest, client=client)  # type: ignore[arg-type]
    # The pre-existing file was untouched; the failed one leaves no partial.
    assert (dest / files[0].key).is_file()
    assert not (dest / files[1].key).exists()


async def test_download_all_classifies_downloaded_vs_skipped(tmp_path: Path) -> None:
    urls = _fixture_urls()
    files = _fixture_files()
    dest = tmp_path / "downloads"
    dest.mkdir(parents=True)
    (dest / files[1].key).write_bytes(urls[files[1].url])

    client = FakeDownloadClient(urls)
    summary = await download_all(files, dest, client=client)  # type: ignore[arg-type]
    assert isinstance(summary, DownloadSummary)
    assert summary.downloaded == [files[0].key]
    assert summary.skipped == [files[1].key]
    assert summary.failed == []
    assert summary.bytes_written == len(urls[files[0].url])
    assert (dest / files[0].key).is_file()


# ---------------------------------------------------------------------------
# build: full flow, resume, rebuild
# ---------------------------------------------------------------------------


def test_build_full_flow(tmp_path: Path) -> None:
    _write_downloads(tmp_path, [PART1_KEY, PART2_KEY])
    result = build_snapshot(_fixture_manifest(), tmp_path)
    assert result.snapshot_date == "2024-03-01"
    assert result.parts_processed == [PART1_KEY, PART2_KEY]
    assert result.parts_resumed == []
    assert result.missing_parts == []
    assert result.works_count == 5
    assert result.citation_count == 6
    assert not result.already_built

    store = SnapshotStore(tmp_path / "index.db")
    store.connect()
    try:
        assert store.is_built()
        # Reverse citation lookups (most-cited first).
        assert [r["citing_id"] for r in store.query_citing("W200")] == [
            "W104",
            "W100",
            "W101",
        ]
        assert [r["citing_id"] for r in store.query_citing("W300")] == [
            "W101",
            "W103",
        ]
        assert [r["citing_id"] for r in store.query_citing("W100")] == ["W104"]
        assert store.query_citing("W102") == []  # cited by nobody
        assert store.query_citing("W999") == []
        assert store.query_work_by_doi("10.1000/1")["id"] == "W100"
        assert store.query_work_by_doi("10.1000/4")["id"] == "W104"
        assert store.query_work("W102")["year"] == 2022
        assert store.query_work("W102")["title"] == "Gamma"
    finally:
        store.close()


def test_build_resumes_partial_download(tmp_path: Path) -> None:
    _write_downloads(tmp_path, [PART1_KEY])
    first = build_snapshot(_fixture_manifest(), tmp_path)
    assert first.parts_processed == [PART1_KEY]
    assert first.missing_parts == [PART2_KEY]
    assert first.works_count == 3
    assert first.citation_count == 3

    # Later, the second partition arrives; rebuild resumes instead of redoing.
    _write_downloads(tmp_path, [PART2_KEY])
    second = build_snapshot(_fixture_manifest(), tmp_path)
    assert second.parts_resumed == [PART1_KEY]
    assert second.parts_processed == [PART2_KEY]
    assert second.works_count == 5
    assert second.citation_count == 6
    store = SnapshotStore(tmp_path / "index.db")
    store.connect()
    try:
        # No duplicate rows from the resume.
        assert [r["citing_id"] for r in store.query_citing("W200")] == [
            "W104",
            "W100",
            "W101",
        ]
        assert store.stats()["works_count"] == 5
    finally:
        store.close()


def test_build_already_built_is_noop(tmp_path: Path) -> None:
    _write_downloads(tmp_path, [PART1_KEY, PART2_KEY])
    build_snapshot(_fixture_manifest(), tmp_path)
    again = build_snapshot(_fixture_manifest(), tmp_path)
    assert again.already_built
    assert again.parts_processed == []
    assert again.parts_resumed == [PART1_KEY, PART2_KEY]
    assert again.works_count == 5


def test_build_rebuilds_on_date_change(tmp_path: Path) -> None:
    _write_downloads(tmp_path, [PART1_KEY, PART2_KEY])
    build_snapshot(_fixture_manifest(date="2024-03-01"), tmp_path)
    rebuilt = build_snapshot(_fixture_manifest(date="2024-06-01"), tmp_path)
    assert rebuilt.rebuilt
    assert rebuilt.works_count == 5  # same data, fresh index, no stale rows
    store = SnapshotStore(tmp_path / "index.db")
    store.connect()
    try:
        assert store.get_meta() is not None
        assert store.get_meta()["snapshot_date"] == "2024-06-01"
        assert store.stats()["works_count"] == 5
    finally:
        store.close()


def test_build_no_downloads_raises(tmp_path: Path) -> None:
    with pytest.raises(SnapshotError, match="paper snapshot download"):
        build_snapshot(_fixture_manifest(), tmp_path)


def test_build_corrupt_partition_raises_and_stays_unbuilt(tmp_path: Path) -> None:
    dest = tmp_path / "downloads"
    dest.mkdir(parents=True)
    (dest / PART1_KEY).write_bytes(b"corrupt")
    with pytest.raises(SnapshotError, match="gz 解压失败"):
        build_snapshot(_fixture_manifest(), tmp_path)
    store = SnapshotStore(tmp_path / "index.db")
    store.connect()
    try:
        assert not store.is_built()
        assert not store.is_part_built(PART1_KEY)
    finally:
        store.close()


# ---------------------------------------------------------------------------
# CLI: help / status / enable-disable / download / build
# ---------------------------------------------------------------------------


def test_root_help_lists_snapshot() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "snapshot" in result.stdout


def test_snapshot_commands_help() -> None:
    result = runner.invoke(app, ["snapshot", "--help"])
    assert result.exit_code == 0
    for command in ("status", "download", "build", "enable", "disable"):
        assert command in result.stdout
        sub = runner.invoke(app, ["snapshot", command, "--help"])
        assert sub.exit_code == 0, sub.output


def test_snapshot_status_empty_dir(tmp_path: Path) -> None:
    result = runner.invoke(app, ["snapshot", "status", "--dir", str(tmp_path)])
    assert result.exit_code == 0
    assert "路由开关" in result.stdout
    assert "未构建" in result.stdout


def test_snapshot_enable_disable_roundtrip(tmp_path: Path) -> None:
    result = runner.invoke(app, ["snapshot", "enable", "--dir", str(tmp_path)])
    assert result.exit_code == 0
    assert read_routing_config(tmp_path) is True
    status = runner.invoke(app, ["snapshot", "status", "--dir", str(tmp_path)])
    assert "已启用" in status.stdout

    result = runner.invoke(app, ["snapshot", "disable", "--dir", str(tmp_path)])
    assert result.exit_code == 0
    assert read_routing_config(tmp_path) is False
    status = runner.invoke(app, ["snapshot", "status", "--dir", str(tmp_path)])
    assert "已禁用" in status.stdout


def test_snapshot_download_cli_with_size_notice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _fixture_manifest()
    monkeypatch.setattr(cli_snapshot, "fetch_manifest", _async_manifest(manifest))

    async def fake_download_all(
        files: Any,
        dest_dir: Path,
        *,
        client: Any = None,
        on_progress: Any = None,
        workers: int = 4,
    ) -> DownloadSummary:
        dest_dir.mkdir(parents=True, exist_ok=True)
        for file in files:
            (dest_dir / file.key).write_bytes(_gz_bytes(WORK_LINES[file.key]))
            if on_progress is not None:
                await on_progress(file.url, len(WORK_LINES[file.key]), file.size)
        return DownloadSummary(
            downloaded=[f.key for f in files],
            bytes_written=sum(len(WORK_LINES[f.key]) for f in files),
        )

    monkeypatch.setattr(cli_snapshot, "download_all", fake_download_all)
    result = runner.invoke(app, ["snapshot", "download", "--dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "磁盘大小提示" in result.stdout
    assert "下载完成" in result.stdout
    assert "2 个分区" in result.stdout
    assert "--date YYYY-MM-DD" in result.stdout  # unfiltered notice suggests --date
    assert (tmp_path / "downloads" / PART1_KEY).is_file()
    assert (tmp_path / "downloads" / PART2_KEY).is_file()


def test_snapshot_download_date_filter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = _fixture_manifest()
    monkeypatch.setattr(cli_snapshot, "fetch_manifest", _async_manifest(manifest))

    async def fake_download_all(
        files: Any,
        dest_dir: Path,
        *,
        client: Any = None,
        on_progress: Any = None,
        workers: int = 4,
    ) -> DownloadSummary:
        dest_dir.mkdir(parents=True, exist_ok=True)
        for file in files:
            (dest_dir / file.key).write_bytes(_gz_bytes(WORK_LINES[file.key]))
        return DownloadSummary(downloaded=[f.key for f in files])

    monkeypatch.setattr(cli_snapshot, "download_all", fake_download_all)
    result = runner.invoke(
        app, ["snapshot", "download", "--date", PART1_DATE, "--dir", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    assert (tmp_path / "downloads" / PART1_KEY).is_file()
    assert not (tmp_path / "downloads" / PART2_KEY).exists()

    bad = runner.invoke(
        app, ["snapshot", "download", "--date", "1999-01-01", "--dir", str(tmp_path)]
    )
    assert bad.exit_code == 2
    assert "没有" in bad.output


def test_snapshot_build_cli_then_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_downloads(tmp_path, [PART1_KEY, PART2_KEY])
    manifest = _fixture_manifest()
    monkeypatch.setattr(cli_snapshot, "fetch_manifest", _async_manifest(manifest))

    result = runner.invoke(app, ["snapshot", "build", "--dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "构建完成" in result.stdout
    assert "5 works" in result.stdout
    assert "6 引用边" in result.stdout

    status = runner.invoke(app, ["snapshot", "status", "--dir", str(tmp_path)])
    assert status.exit_code == 0
    assert "2024-03-01" in status.stdout
    assert "5 works" in status.stdout
    assert "6 引用边" in status.stdout


def _async_manifest(manifest: SnapshotManifest) -> Any:
    async def _fetch(http: Any = None) -> SnapshotManifest:
        return manifest

    return _fetch


# ---------------------------------------------------------------------------
# Routing: trace-citing --use-snapshot
# ---------------------------------------------------------------------------


def _install_api_fake(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Route cli_trace.fetch_citing_papers to a recording fake; return calls."""
    calls: list[str] = []

    async def fake_fetch(
        paper_id: str,
        *,
        sources: list[str] | None = None,
        limit: int | None = None,
        resume_from: str | None = None,
        http: Any = None,
    ) -> CitingResult:
        calls.append(paper_id)
        return CitingResult(
            papers=[CitingPaper(citing_paper_id="W50", doi="10.9999/1", title="API Paper")],
            source_stats={"openalex": 1},
            written_stats={"openalex": 1},
        )

    monkeypatch.setattr(cli_trace, "fetch_citing_papers", fake_fetch)
    return calls


def test_trace_citing_use_snapshot_hit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _build_fixture_index(tmp_path)
    api_calls = _install_api_fake(monkeypatch)

    result = runner.invoke(
        app,
        ["trace-citing", "W200", "--use-snapshot", "--snapshot-dir", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output
    assert api_calls == []  # local index answered; API never touched
    assert "命中本地快照" in result.stderr
    rows = list(__import__("csv").DictReader(result.stdout.splitlines()))
    assert [r["citing_paper_id"] for r in rows] == ["W104", "W100", "W101"]
    assert rows[0]["title"] == "Epsilon"
    assert rows[0]["doi"] == "10.1000/4"


def test_trace_citing_use_snapshot_by_doi(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _build_fixture_index(tmp_path)
    api_calls = _install_api_fake(monkeypatch)

    result = runner.invoke(
        app,
        ["trace-citing", "10.1000/1", "--use-snapshot", "--snapshot-dir", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output
    assert api_calls == []
    rows = list(__import__("csv").DictReader(result.stdout.splitlines()))
    assert [r["citing_paper_id"] for r in rows] == ["W104"]


def test_trace_citing_use_snapshot_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _build_fixture_index(tmp_path)
    api_calls = _install_api_fake(monkeypatch)
    result = runner.invoke(
        app,
        ["trace-citing", "W200", "--use-snapshot", "--limit", "1", "--snapshot-dir", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output
    assert api_calls == []
    rows = list(__import__("csv").DictReader(result.stdout.splitlines()))
    assert [r["citing_paper_id"] for r in rows] == ["W104"]


def test_trace_citing_use_snapshot_miss_falls_back_to_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _build_fixture_index(tmp_path)
    api_calls = _install_api_fake(monkeypatch)

    result = runner.invoke(
        app,
        ["trace-citing", "W999", "--use-snapshot", "--snapshot-dir", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output
    assert api_calls == ["W999"]
    assert "快照无此论文引用数据，回退 API" in result.stderr
    rows = list(__import__("csv").DictReader(result.stdout.splitlines()))
    assert [r["citing_paper_id"] for r in rows] == ["W50"]


def test_trace_citing_use_snapshot_not_built_falls_back_to_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api_calls = _install_api_fake(monkeypatch)
    result = runner.invoke(
        app,
        ["trace-citing", "W200", "--use-snapshot", "--snapshot-dir", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output
    assert api_calls == ["W200"]
    assert "本地快照未构建" in result.stderr
    assert "回退 API" in result.stderr


def test_trace_citing_routing_switch_enabled_defaults_to_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _build_fixture_index(tmp_path)
    write_routing_config(tmp_path, True)
    api_calls = _install_api_fake(monkeypatch)

    result = runner.invoke(app, ["trace-citing", "W200", "--snapshot-dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert api_calls == []  # no explicit flag; the enable switch routed locally
    assert "命中本地快照" in result.stderr


def test_trace_citing_routing_switch_disabled_uses_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _build_fixture_index(tmp_path)
    write_routing_config(tmp_path, False)
    api_calls = _install_api_fake(monkeypatch)

    result = runner.invoke(app, ["trace-citing", "W200", "--snapshot-dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert api_calls == ["W200"]
    assert "命中本地快照" not in result.stderr


def test_trace_citing_no_snapshot_flag_uses_api_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: without --use-snapshot / enable, behavior is API-only."""
    api_calls = _install_api_fake(monkeypatch)
    result = runner.invoke(app, ["trace-citing", "W200"])
    assert result.exit_code == 0, result.output
    assert api_calls == ["W200"]


def test_trace_profiles_use_snapshot_notice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    authors_csv = tmp_path / "authors.csv"
    authors_csv.write_text(
        "\ufeffauthor_name,appears_in,affiliation,author_id\nAlice,W1,,A1\n",
        encoding="utf-8",
    )
    profiled: list[str] = []

    async def fake_profiles(
        author_rows: Any, *, batch_size: int = 20, http: Any = None
    ) -> list[Any]:
        from academic_intelligence.trace.profiles import AuthorProfile

        profiled.append("called")
        return [
            AuthorProfile(
                author_name=row.author_name,
                author_id=row.author_id,
                institution="MIT",
            )
            for row in author_rows
        ]

    monkeypatch.setattr(cli_trace, "fetch_profiles", fake_profiles)
    result = runner.invoke(
        app,
        [
            "trace-profiles",
            str(authors_csv),
            "--use-snapshot",
            "--snapshot-dir",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0, result.output
    assert profiled == ["called"]  # API path still ran
    assert "快照不支持作者画像" in result.stderr
    assert "回退 API" in result.stderr
