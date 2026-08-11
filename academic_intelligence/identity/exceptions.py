"""WP6 author identity exceptions.

Small hierarchy rooted at :class:`IdentityError` (an
:class:`~academic_intelligence.core.exceptions.AcademicIntelligenceError`)
so the CLI boundary (:func:`~academic_intelligence.cli_source._map_cli_error`)
maps them to friendly exit-2 messages.
"""

from __future__ import annotations

from academic_intelligence.core.exceptions import AcademicIntelligenceError


class IdentityError(AcademicIntelligenceError):
    """Base class for author identity resolution errors (WP6)."""


class PaperNotFoundError(IdentityError):
    """Raised when ``resolve``/``confirm`` cannot find the target paper."""


class AuthorNotFoundError(IdentityError):
    """Raised when the byline name is not in the paper, or the source has
    no such author id."""


class IdentitySourceError(IdentityError):
    """Raised when a source profile/search request fails.

    Distinct from :class:`AuthorNotFoundError` so callers can tell "the
    source answered but there is nothing" apart from "the source request
    failed" (transient rate-limit / network errors ride on the underlying
    :class:`~academic_intelligence.core.exceptions.SourceError` as the
    ``__cause__``).
    """
