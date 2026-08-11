"""Generate the sample-paper PDF test fixture.

The fixture is a deterministic two-page PDF used by the full-text pipeline
tests (paragraph segmentation, heading detection, page numbers). It is
checked in as ``sample_paper.pdf`` so tests run without PyMuPDF — this script
is only for regenerating it after fixture changes.

Requires PyMuPDF (``pip install pymupdf``), the optional AGPL extra; the
generated fixture itself is backend-independent.
"""

from __future__ import annotations

from pathlib import Path

_OUT = Path(__file__).resolve().parent / "sample_paper.pdf"


def _line(page: object, y: float, text: str, size: int = 11) -> float:
    page.insert_text((72, y), text, fontsize=size, fontname="helv")  # type: ignore[attr-defined]
    return y


def main() -> None:
    import fitz

    doc = fitz.open()

    # Page 1: title (20pt), authors (11), Abstract heading (14) + two
    # paragraphs (11), 1 Introduction heading (14) + two paragraphs (11).
    p1 = doc.new_page()
    y = 60.0
    y = _line(p1, y, "Attention Is All You Need", 20) + 40
    y = _line(p1, y, "Vaswani et al. 2017") + 34
    y = _line(p1, y, "Abstract", 14) + 26
    y = _line(
        p1,
        y,
        "We propose a new network architecture, the Transformer, based solely on "
        "attention mechanisms, dispensing with recurrence and convolutions entirely.",
    ) + 30
    y = _line(
        p1,
        y,
        "Experiments on two machine translation tasks show these models to be "
        "superior in quality while being more parallelizable.",
    ) + 34
    y = _line(p1, y, "1 Introduction", 14) + 26
    y = _line(
        p1,
        y,
        "Recurrent neural networks have long been the de facto choice for "
        "sequence modeling.",
    ) + 30
    y = _line(
        p1,
        y,
        "The Transformer allows for significantly more parallelization and can "
        "reach a new state of the art in translation quality.",
    )

    # Page 2: 2 Method heading (14) + two paragraphs (11).
    p2 = doc.new_page()
    y = 60.0
    y = _line(p2, y, "2 Method", 14) + 26
    y = _line(p2, y, "The model applies a multi-head self-attention mechanism.") + 30
    y = _line(p2, y, "Positional encodings are added to the input embeddings.")

    doc.save(str(_OUT))
    doc.close()
    print(f"wrote {_OUT}")


if __name__ == "__main__":
    main()
