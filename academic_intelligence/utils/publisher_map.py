"""Static DOI-prefix → publisher mapping (fallback for source metadata).

Backup path of the two-level "paper → publisher" resolution
(upgrade technical-design §3.1, WP2a):

1. Preferred: the source's own metadata ``publisher`` field (Crossref works
   carry it, e.g. ``10.1038/...`` → "Springer Nature"); zero maintenance.
2. Fallback: this module's static registrant → publisher table, used only
   when the source provides no publisher field.

Matching is on the DOI *registrant* (the ``10.XXXX`` part before the first
``/``), case-insensitive per the DOI handle spec.  Unknown registrants
return ``None`` so callers can leave the field empty rather than guessing.
"""

from __future__ import annotations

# Common DOI registrant prefixes → publisher display name.  The first twelve
# entries are the canonical names fixed by the upgrade design (§3.1); the
# rest cover other frequently encountered registrants.
PUBLISHER_BY_DOI_PREFIX: dict[str, str] = {
    "10.1000": "DOI Foundation",  # reserved for DOI handbook / test DOIs
    "10.1002": "Wiley",
    "10.1007": "Springer",
    "10.1016": "Elsevier",
    "10.1021": "ACS",
    "10.1024": "Hogrefe",
    "10.1029": "Wiley",
    "10.1038": "Springer Nature",
    "10.1039": "RSC",
    "10.1055": "Thieme",
    "10.1063": "AIP Publishing",
    "10.1073": "PNAS",
    "10.1080": "Taylor & Francis",
    "10.1088": "IOP",
    "10.1093": "Oxford University Press",
    "10.1097": "Wolters Kluwer",
    "10.1101": "Cold Spring Harbor Laboratory",
    "10.1103": "American Physical Society",
    "10.1109": "IEEE",
    "10.1111": "Wiley",
    "10.1117": "SPIE",
    "10.1126": "American Association for the Advancement of Science",
    "10.1128": "American Society for Microbiology",
    "10.1136": "BMJ",
    "10.1142": "World Scientific",
    "10.1145": "ACM",
    "10.1148": "Radiological Society of North America",
    "10.1152": "American Physiological Society",
    "10.1155": "Hindawi",
    "10.1162": "MIT Press",
    "10.1172": "American Society for Clinical Investigation",
    "10.1177": "SAGE",
    "10.1186": "BioMed Central",
    "10.1190": "Society of Exploration Geophysicists",
    "10.1200": "American Society of Clinical Oncology",
    "10.1287": "INFORMS",
    "10.1364": "Optica Publishing Group",
    "10.1371": "PLOS",
    "10.1525": "University of California Press",
    "10.1542": "American Academy of Pediatrics",
    "10.21105": "The Open Journal",
    "10.3233": "IOS Press",
    "10.3389": "Frontiers",
    "10.3390": "MDPI",
    "10.48550": "arXiv",
    "10.5194": "Copernicus Publications",
    "10.5281": "Zenodo",
    "10.5334": "Ubiquity Press",
    "10.7554": "eLife",
    "10.7717": "PeerJ",
}

# Common URL/identifier prefixes stripped before registrant extraction.
_DOI_WRAPPERS = ("https://doi.org/", "http://doi.org/", "doi:")


def publisher_from_doi(doi: str | None) -> str | None:
    """Return the publisher mapped from *doi*'s registrant prefix.

    Accepts a bare DOI (``10.1038/s41586-025-09422-z``) or a wrapped form
    (``https://doi.org/10.1038/...``).  Matching is exact on the registrant
    (``10.1038``), case-insensitive.  Returns ``None`` for empty input or an
    unknown registrant — the fallback is silent by design.
    """
    if doi is None:
        return None
    cleaned = doi.strip()
    lowered = cleaned.lower()
    for wrapper in _DOI_WRAPPERS:
        if lowered.startswith(wrapper):
            cleaned = cleaned[len(wrapper) :].strip()
            break
    registrant = cleaned.split("/", 1)[0].lower()
    if not registrant or not registrant.startswith("10."):
        return None
    return PUBLISHER_BY_DOI_PREFIX.get(registrant)


__all__ = ["PUBLISHER_BY_DOI_PREFIX", "publisher_from_doi"]
