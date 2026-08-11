"""Unicode normalization helpers (FIX-W W3).

The storage contract stores text in Unicode NFC (canonical composed) form so
precomposed and decomposed spellings of the same text — ``"Résumé"`` vs
``"Re\\u0301sume\\u0301"`` — collide instead of being treated as different
strings.  NFC is applied on the model write path (validators) and on query
inputs (storage backends), so new data is stored composed, queries hit
regardless of the caller's spelling, and already-stored decomposed data
read back composed through the model read path.
"""

from __future__ import annotations

import unicodedata


def normalize_nfc(value: str) -> str:
    """Return *value* in Unicode NFC (canonical composed) form.

    NFC never lengthens a string and is a no-op for already-composed text, so
    normalizing read/write is safe for existing data.  Already-NFC strings are
    returned unchanged (``unicodedata.normalize`` returns early on composed
    input).
    """
    if not value or value.isascii():
        return value
    return unicodedata.normalize("NFC", value)
