"""Author-name normalization helpers shared across query layers.

The storage backends filter papers by author name in Python (author lists are
stored as JSON), so the substring rule ``query in stored_name`` is the single
point where name-format differences (case, middle initials) break lookups —
``"Geoffrey Hinton"`` never matched a stored ``"Geoffrey E. Hinton"``.  These
helpers widen the match to token-level equality without changing the original
substring semantics.
"""

from __future__ import annotations

import re

from academic_intelligence.utils.normalize import normalize_nfc


def normalize_author_tokens(name: str) -> set[str]:
    """Lowercase significant tokens of a name, dropping single-char tokens.

    ``"Geoffrey E. Hinton"`` -> ``{"geoffrey", "hinton"}``; single-character
    tokens are dropped so middle-initial variants normalize identically.

    (FIX-W W3) The input is NFC-normalized first so a decomposed query
    spelling (``"Jo\\u0301se Silva"``) yields the same tokens as the composed
    stored spelling (``"José Silva"``).
    """
    cleaned = re.sub(r"[^\w\s]", " ", normalize_nfc(name).lower())
    return {token for token in cleaned.split() if len(token) > 1}


def author_name_matches(query: str, stored: str) -> bool:
    """Whether a *stored* author name matches a *query* name.

    A name matches when:

    - the query is a substring of the stored name (case-insensitive —
      the original semantics), or
    - every significant token of the query appears in the stored name
      (e.g. ``"Geoffrey Hinton"`` matches ``"Geoffrey E. Hinton"``).

    Args:
        query: The author name being searched for.
        stored: One stored author byline name.

    Returns:
        ``True`` when the stored name should be considered a match.

    (FIX-W W3) Both sides are NFC-normalized first, so a decomposed query or
    stored spelling still matches its composed counterpart.
    """
    q = normalize_nfc(query).lower().strip()
    s = normalize_nfc(stored).lower().strip()
    if not q:
        return False
    if q in s:
        return True
    q_tokens = normalize_author_tokens(query)
    if not q_tokens:
        return False
    return q_tokens <= normalize_author_tokens(stored)
