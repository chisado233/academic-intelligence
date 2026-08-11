"""WP6 author identity resolution package.

Given a paper + byline name, resolve the author's identity (institution /
h-index / homepage / interests / representative papers), disambiguate
same-name candidates, and confirm identities back into the cross-paper
``author_identity_global`` table (upgrade technical-design.md §1.5 / §2).

Public surface:

- :class:`~academic_intelligence.identity.resolver.Resolver` —
  ``resolve`` / ``profile`` / ``search`` / ``confirm``;
- :class:`~academic_intelligence.identity.fetcher.SourceFetcher` —
  polite OpenAlex / Semantic Scholar author fetcher;
- :mod:`~academic_intelligence.identity.models` — typed payloads
  (``ResolveResult`` / ``AuthorProfile`` / ``AuthorCandidate`` ...);
- :mod:`~academic_intelligence.identity.exceptions` — domain errors.
"""

from __future__ import annotations

from academic_intelligence.identity.exceptions import (
    AuthorNotFoundError,
    IdentityError,
    IdentitySourceError,
    PaperNotFoundError,
)
from academic_intelligence.identity.fetcher import SourceFetcher
from academic_intelligence.identity.models import (
    AuthorCandidate,
    AuthorProfile,
    ConfirmResult,
    RepresentativePaper,
    ResolveResult,
)
from academic_intelligence.identity.resolver import Resolver, parse_candidate_id

__all__ = [
    "Resolver",
    "SourceFetcher",
    "parse_candidate_id",
    "ResolveResult",
    "ConfirmResult",
    "AuthorProfile",
    "AuthorCandidate",
    "RepresentativePaper",
    "IdentityError",
    "IdentitySourceError",
    "PaperNotFoundError",
    "AuthorNotFoundError",
]
