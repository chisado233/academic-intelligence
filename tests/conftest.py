"""Shared pytest fixtures for Academic Intelligence tests.

Network integration tests use offline cassette fixtures (JSON response
replay) so CI never depends on live third-party APIs.
"""

from __future__ import annotations

from typing import Callable

import pytest

from academic_intelligence.core.models import Evidence
from academic_intelligence.core.types import SourceType
from tests.cassette_replay import install_cassette, install_merged_cassettes, load_cassette


def pytest_configure(config: pytest.Config) -> None:
    """Register custom markers (also declared in pyproject.toml)."""
    config.addinivalue_line("markers", "slow: marks tests as slow")
    config.addinivalue_line("markers", "integration: marks tests as integration tests")
    config.addinivalue_line("markers", "network: marks tests that require network access")
    config.addinivalue_line("markers", "boundary: marks tests as boundary tests")
    config.addinivalue_line("markers", "performance: marks tests as performance tests")


@pytest.fixture
def evidence() -> Evidence:
    """Minimal valid Evidence for model construction."""
    return Evidence(
        source=SourceType.OPENALEX,
        source_url="https://openalex.org/W1",
        confidence=0.85,
    )


@pytest.fixture
def make_evidence() -> Callable[..., Evidence]:
    def _make(
        source: SourceType = SourceType.OPENALEX,
        url: str = "https://example.com",
        confidence: float = 0.85,
    ) -> Evidence:
        return Evidence(source=source, source_url=url, confidence=confidence)

    return _make


# Re-export for convenience in tests that prefer fixtures
@pytest.fixture
def cassette_loader() -> Callable[[str], dict]:
    return load_cassette


@pytest.fixture
def cassette_install() -> Callable:
    return install_cassette


@pytest.fixture
def merged_cassette_install() -> Callable:
    return install_merged_cassettes
