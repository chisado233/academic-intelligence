"""Unit tests for the static DOI-prefix → publisher mapping table."""

from __future__ import annotations

import pytest

from academic_intelligence.utils.publisher_map import (
    PUBLISHER_BY_DOI_PREFIX,
    publisher_from_doi,
)


def test_design_smoke_prefix() -> None:
    """The canonical design case (§3.1 / user-test-plan Q1)."""
    assert publisher_from_doi("10.1038/s41586-025-09422-z") == "Springer Nature"


@pytest.mark.parametrize(
    ("doi", "expected"),
    [
        ("10.1038/s41586-025-09422-z", "Springer Nature"),
        ("10.1109/TPAMI.2017.2699184", "IEEE"),
        ("10.1145/3292500.3330701", "ACM"),
        ("10.1016/j.cell.2019.03.044", "Elsevier"),
        ("10.1007/978-3-319-10602-1_13", "Springer"),
        ("10.1177/0146167216643934", "SAGE"),
        ("10.1111/j.1469-8986.2010.01128.x", "Wiley"),
        ("10.1021/acs.jmedchem.0c01193", "ACS"),
        ("10.1088/1751-8121/aa6b1c", "IOP"),
        ("10.1039/C4CP04566B", "RSC"),
        ("10.1073/pnas.2023309118", "PNAS"),
        ("10.7554/eLife.12345", "eLife"),
    ],
)
def test_common_prefixes(doi: str, expected: str) -> None:
    assert publisher_from_doi(doi) == expected


def test_unknown_prefix_returns_none() -> None:
    assert publisher_from_doi("10.99999/unknown-doi") is None


@pytest.mark.parametrize("value", [None, "", "   ", "not-a-doi", "10.xyz/no-digits"])
def test_invalid_or_empty_input_returns_none(value: str | None) -> None:
    assert publisher_from_doi(value) is None


def test_prefix_match_is_exact_registrant() -> None:
    # "10.103" is a different registrant than "10.1038" — must not match.
    assert publisher_from_doi("10.103/not-a-real-doi") is None


def test_url_wrapped_doi_still_matches() -> None:
    assert publisher_from_doi("https://doi.org/10.1038/s41586-025-09422-z") == "Springer Nature"


def test_registrant_match_is_case_insensitive() -> None:
    assert publisher_from_doi("10.1038/S41586-025-09422-z") == "Springer Nature"


def test_table_has_expected_core_entries() -> None:
    for prefix in (
        "10.1038",
        "10.1109",
        "10.1145",
        "10.1016",
        "10.1007",
        "10.1177",
        "10.1111",
        "10.1021",
        "10.1088",
        "10.1039",
        "10.1073",
        "10.7554",
    ):
        assert prefix in PUBLISHER_BY_DOI_PREFIX
