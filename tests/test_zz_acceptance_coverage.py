"""Coverage acceptance guard (§17.6) — self-contained, no stale artifacts.

The original ``test_acceptance_06_coverage_requirement`` read the
``coverage.xml`` file left in the project root by the *previous* pytest-cov
run.  Any partial pytest run (with coverage) overwrote that artifact with a
low-coverage subset, which made the next full run fail spuriously (FIX-E-7).

This replacement never touches ``coverage.xml``: it computes the CURRENT
run's line coverage from the in-process pytest-cov session
(``coverage.Coverage.current()``).  The ``test_zz_*`` module name sorts last
in a full-suite run (sub-directories sort before root-level ``test_*.py``
files), so by the time this test executes, the in-memory data covers the
whole run.

The guard only asserts on full-suite runs and skips otherwise:

- pytest-cov not active (no in-process ``Coverage`` instance) — same as the
  old "no coverage report" skip;
- the session collected only a subset of the suite (partial/filtered runs
  exercise a subset of the code, so a low figure would be a false alarm);
- no analysable data for the ``academic_intelligence`` package.
"""

from __future__ import annotations

import pytest

# Floor on collected test items below which a run is treated as partial.  The
# full suite collects 448+ items; runs far below that (single files, ``-k``
# filters, directory subsets) only exercise a fraction of the code, so the
# 80% guard is only meaningful above this bound.
_PARTIAL_RUN_ITEM_FLOOR = 300


def test_acceptance_06_coverage_requirement(request: pytest.FixtureRequest) -> None:
    """§17.6 — coverage >= 80% (guard against regressions).

    Computes line coverage from the current pytest-cov session in memory; a
    full-suite run must stay above the 80% bar from §17.6.  Skips for partial
    runs and for runs without pytest-cov, so it never gates offline
    development loops and never depends on stale ``coverage.xml`` artifacts.
    """
    if len(request.session.items) < _PARTIAL_RUN_ITEM_FLOOR:
        pytest.skip(
            f"partial run ({len(request.session.items)} items collected; "
            f"full suite is >{_PARTIAL_RUN_ITEM_FLOOR}) — coverage guard "
            "only enforced on full-suite runs"
        )

    import coverage

    cov = coverage.Coverage.current()
    if cov is None:
        pytest.skip("pytest-cov coverage session not active (run with --cov)")

    data = cov.get_data()
    analyzed = 0
    total_statements = 0
    total_executed = 0
    for filename in data.measured_files():
        normalized = filename.replace("\\", "/")
        if not normalized.endswith(".py") or "academic_intelligence" not in normalized:
            continue
        try:
            _name, statements, _excluded, missing, _formatted = cov.analysis2(filename)
        except Exception:
            # Skip files that cannot be analysed (deleted, unparseable...);
            # if none can, the guard skips below.
            continue
        if not statements:
            continue
        analyzed += 1
        total_statements += len(statements)
        total_executed += len(statements) - len(missing)

    if analyzed == 0:
        pytest.skip(
            "no analysable coverage data for academic_intelligence "
            "in the current run"
        )

    line_rate = total_executed / total_statements
    assert line_rate >= 0.80, (
        f"coverage {line_rate:.1%} below the §17.6 threshold of 80% "
        f"({total_executed}/{total_statements} lines executed)"
    )
