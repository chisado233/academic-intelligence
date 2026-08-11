# arXiv Routing and Retry Observability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make exact arXiv identifiers return only the requested paper, preserve actual HTTP retry metadata through source exception wrapping, and give Agents a finite, truthful retry/stop contract.

**Architecture:** Query classification remains in `MultiSourceCollector`, source-specific exact retrieval remains in `ArxivSource`, transport retry policy remains in `RetryHandler`, and user-facing failure records remain in `SourceFailure`. The change connects these existing boundaries without adding a new identifier framework, exception type, dependency, or storage schema.

**Tech Stack:** Python 3.11+, asyncio, httpx, Pydantic v2, pytest/pytest-asyncio, Ruff, mypy, MkDocs, Hatch build backend.

## Global Constraints

- Work in the current `master` checkout because the working implementation is uncommitted and cannot be recreated from old HEAD in a new worktree.
- Preserve every unrelated dirty file and make only targeted edits.
- Do not run Git commit; checkpoint with focused diffs and tests instead.
- Use TDD for every production behavior: add the test, observe the expected failure, then write the smallest implementation.
- Do not change public source method signatures, default retry counts, storage schema, or dependency lists.
- Do not use live network for deterministic tests; use `httpx.MockTransport` or an in-memory source probe.

---

### Task 1: Exact arXiv identifier routing and response validation

**Files:**
- Create: `tests/test_fix_ag.py`
- Modify: `academic_intelligence/sources/arxiv.py`
- Modify: `academic_intelligence/collectors/base.py`

**Interfaces:**
- Consumes: `MultiSourceCollector.collect_paper(query: str, *, sources: Sequence[BaseSource] | None, limit: int) -> CollectionResult`.
- Consumes: `ArxivSource.get_paper_by_arxiv_id(arxiv_id: str) -> Paper | None`.
- Produces: internal `_parse_arxiv_id(value: str) -> str | None` and `_canonical_arxiv_id(value: str) -> str | None` helpers used by Collector routing and adapter response validation.
- Preserves: DOI, OpenAlex Work ID and free-text search behavior.

- [ ] **Step 1: Write failing Collector and adapter tests**

Create `tests/test_fix_ag.py` with real `Paper`/`Evidence` values and these cases:

```python
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("1810.04805", "1810.04805"),
        ("1810.04805v2", "1810.04805v2"),
        ("arXiv:1810.04805", "1810.04805"),
        ("https://arxiv.org/abs/1810.04805v2", "1810.04805v2"),
        ("hep-th/9901001v2", "hep-th/9901001v2"),
    ],
)
async def test_collect_paper_routes_complete_arxiv_ids_to_exact_lookup(
    query: str, expected: str
) -> None:
    source = _RoutingArxiv()
    result = await MultiSourceCollector(sources=[source]).collect_paper(
        query, sources=[source]
    )
    assert source.calls == [("get_paper_by_arxiv_id", expected)]
    assert [paper.title for paper in result.papers] == ["Exact target"]


@pytest.mark.asyncio
async def test_collect_paper_keeps_natural_language_containing_id_on_search_path() -> None:
    source = _RoutingArxiv()
    result = await MultiSourceCollector(sources=[source]).collect_paper(
        "paper 1810.04805", sources=[source]
    )
    assert source.calls == [("search_papers", "paper 1810.04805", 10)]
    assert len(result.papers) == 2


@pytest.mark.asyncio
async def test_arxiv_exact_lookup_ignores_unrelated_first_entry() -> None:
    source = ArxivSource(http_client=_StaticHTTP(_feed("9999.00001v1", "1810.04805v2")))
    paper = await source.get_paper_by_arxiv_id("1810.04805")
    assert paper is not None
    assert paper.arxiv_id == "1810.04805v2"


@pytest.mark.asyncio
async def test_arxiv_exact_lookup_rejects_embedded_id_without_http_call() -> None:
    http = _StaticHTTP(_feed("1810.04805v2"))
    source = ArxivSource(http_client=http)
    assert await source.get_paper_by_arxiv_id("paper 1810.04805") is None
    assert http.calls == []


def test_arxiv_parser_preserves_old_style_archive_prefix() -> None:
    source = ArxivSource(http_client=_StaticHTTP(""))
    papers = source._parse_feed(_feed("hep-th/9901001v2"))
    assert papers[0].arxiv_id == "hep-th/9901001v2"
```

The helpers `_RoutingArxiv`, `_StaticHTTP`, `_feed`, and `_paper` must be local to the test file. `_RoutingArxiv.search_papers()` returns two valid evidence-bearing papers; `_RoutingArxiv.get_paper_by_arxiv_id()` returns one valid target and records the received ID.

- [ ] **Step 2: Run Task 1 tests and verify RED**

Run:

```powershell
python -m pytest -q tests/test_fix_ag.py -k "arxiv or collect_paper" --no-cov
```

Expected failures:

- Collector records `search_papers` instead of `get_paper_by_arxiv_id`.
- Adapter returns the unrelated first feed entry.
- Embedded natural-language ID is accepted by the current `.search()` regex.
- Old-style feed parsing loses `hep-th/`.

- [ ] **Step 3: Implement strict parsing in `sources/arxiv.py`**

Replace the ID regex with a version-grouped expression and add strict helpers:

```python
_ARXIV_ID_RE = re.compile(
    r"((?:\d{4}\.\d{4,5}|[a-z\-]+(?:\.[a-z]{2})?/\d{7})(?:v\d+)?)",
    re.IGNORECASE,
)
_ARXIV_ID_PREFIXES = (
    "arxiv:",
    "https://arxiv.org/abs/",
    "http://arxiv.org/abs/",
    "https://arxiv.org/pdf/",
    "http://arxiv.org/pdf/",
)


def _parse_arxiv_id(value: str) -> str | None:
    cleaned = value.strip().rstrip("/")
    lowered = cleaned.lower()
    for prefix in _ARXIV_ID_PREFIXES:
        if lowered.startswith(prefix):
            cleaned = cleaned[len(prefix) :].strip().rstrip("/")
            break
    if cleaned.lower().endswith(".pdf"):
        cleaned = cleaned[:-4]
    match = _ARXIV_ID_RE.fullmatch(cleaned)
    return match.group(1) if match else None


def _canonical_arxiv_id(value: str) -> str | None:
    parsed = _parse_arxiv_id(value)
    return re.sub(r"v\d+$", "", parsed, flags=re.IGNORECASE) if parsed else None
```

In `_parse_entry`, preserve everything after `/abs/` instead of taking only the final slash segment. In `get_paper_by_arxiv_id`, reject non-full matches before HTTP, call `id_list` with the parsed ID, then return the first parsed paper whose `paper.arxiv_id` or `paper.id` has the same canonical ID. Return `None` when no exact record is present.

- [ ] **Step 4: Route exact IDs in `collectors/base.py`**

Import `_parse_arxiv_id`. Teach `_run_on_sources()` to append a single record for `get_paper_by_arxiv_id` alongside DOI/work-ID methods. In `collect_paper()` classify arXiv after DOI and before free text:

```python
arxiv_id = _parse_arxiv_id(cleaned)
if arxiv_id is not None:
    active = list(sources if sources is not None else self._sources)
    capable = [s for s in active if _source_supports(s, "get_paper_by_arxiv_id")]
    result = await self._run_on_sources(
        "get_paper_by_arxiv_id",
        arxiv_id,
        sources=capable or active,
    )
    if limit == 0:
        result.papers.clear()
        result.stats["paper_count"] = 0
        result.stats["empty"] = True
    return result
```

The `capable or active` fallback intentionally lets the existing unsupported-operation path produce a structured failure when no selected source supports exact arXiv lookup.

- [ ] **Step 5: Run Task 1 tests and relevant existing regressions GREEN**

Run:

```powershell
python -m pytest -q tests/test_fix_ag.py tests/test_fix_y.py tests/test_fix_s.py tests/test_sources_arxiv_pubmed_ieee.py tests/test_coverage_v2.py --no-cov
```

Expected: all selected tests pass, including arXiv DOI routing and existing exact helper tests.

- [ ] **Step 6: Check focused diff without committing**

Run:

```powershell
git diff --check -- academic_intelligence/sources/arxiv.py academic_intelligence/collectors/base.py tests/test_fix_ag.py
```

Expected: no whitespace errors. Do not commit because these files already contain approved uncommitted project work.

### Task 2: Preserve retry metadata through wrapped exceptions

**Files:**
- Modify: `tests/test_fix_ag.py`
- Modify: `academic_intelligence/utils/retry.py`
- Modify: `academic_intelligence/core/exceptions.py`

**Interfaces:**
- Consumes: `RetryHandler.execute(func, *args, **kwargs) -> T` and existing exception types.
- Produces: terminal exceptions with an internal `retry_count: int` attribute.
- Produces: `SourceFailure.from_exception(...)` that fills missing retry/status metadata from a bounded cause/context chain while preserving outer explicit context precedence.
- Preserves: original exception classes, messages, `RateLimitError.retry_after`, and retry policy.

- [ ] **Step 1: Add failing end-to-end retry metadata tests**

Append:

```python
@pytest.mark.asyncio
async def test_source_failure_recovers_real_retry_count_and_status_from_cause() -> None:
    calls = 0

    async def reject(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, headers={"Retry-After": "5"}, request=request)

    client = HTTPClient(
        strategy=AntiCrawlStrategy(
            max_retries=2,
            base_delay=0.0,
            adaptive_delay=False,
            jitter=False,
        ),
        enable_cache=False,
    )
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(reject))
    source = ArxivSource(http_client=client, min_interval_seconds=0.1)
    try:
        with pytest.raises(RateLimitError) as exc_info:
            await source.get_paper_by_arxiv_id("1810.04805")
        failure = SourceFailure.from_exception(
            source="arxiv",
            operation="get_paper_by_arxiv_id",
            exc=exc_info.value,
        )
    finally:
        await client.close()

    assert calls == 3
    assert failure.retry_count == 2
    assert failure.http_status == 429
    assert exc_info.value.retry_after == 5


def test_source_failure_outer_context_overrides_cause_metadata() -> None:
    request = httpx.Request("GET", "https://example.test")
    response = httpx.Response(429, request=request)
    inner = httpx.HTTPStatusError("limited", request=request, response=response)
    inner.retry_count = 2
    outer = RateLimitError(
        "outer",
        source_name="test",
        context={"retry_count": 7, "http_status": 503},
    )
    outer.__cause__ = inner
    failure = SourceFailure.from_exception(
        source="test", operation="search_papers", exc=outer
    )
    assert failure.retry_count == 7
    assert failure.http_status == 503
```

- [ ] **Step 2: Run retry test and verify RED**

Run:

```powershell
python -m pytest -q tests/test_fix_ag.py -k "retry or cause" --no-cov
```

Expected: end-to-end test fails with `retry_count == 0` and `http_status is None`; explicit-context precedence already passes and acts as a compatibility guard.

- [ ] **Step 3: Annotate terminal retry exceptions**

In `RetryHandler.execute()`, immediately before each terminal re-raise, attach the number of retries already performed:

```python
if attempt >= self.config.max_retries or not self.config.should_retry(exc):
    try:
        setattr(exc, "retry_count", attempt)
    except (AttributeError, TypeError):
        pass
    raise
```

`attempt` is zero-based and therefore equals retries already completed. Do not wrap the exception or change cancellation/retry decisions.

- [ ] **Step 4: Recover metadata from a bounded exception chain**

In `SourceFailure.from_exception()`, walk at most 16 unique exceptions. For each exception, inspect its dict `context`, direct `retry_count`, direct `status_code`, and `response.status_code`. Set each output only while it is missing, so the outermost explicit context wins. Follow `__cause__` first; otherwise follow unsuppressed `__context__`. Stop on `None`, a repeated object ID, or the depth bound. Convert only final `int | str` values, retaining the existing zero/None fallbacks.

- [ ] **Step 5: Run Task 2 and existing retry/security regressions GREEN**

Run:

```powershell
python -m pytest -q tests/test_fix_ag.py tests/test_fix_j.py tests/test_upgrade_top8_10.py tests/test_hardening_security_async.py tests/test_utils.py --no-cov
```

Expected: all selected tests pass; credential redaction and original retry exception types remain unchanged.

- [ ] **Step 6: Check focused diff without committing**

Run:

```powershell
git diff --check -- academic_intelligence/utils/retry.py academic_intelligence/core/exceptions.py tests/test_fix_ag.py
```

Expected: no whitespace errors.

### Task 3: Upgrade the Skill execution contract and public API guidance

**Files:**
- Modify: `tests/test_fix_ag.py`
- Modify: `SKILL.md`
- Modify: `README.md`
- Modify: `docs/api.md`
- Modify: `docs/architecture.md`
- Modify: `docs/decisions.md`

**Interfaces:**
- Documents existing `AuthorDisambiguator`, `KnowledgeGraph`, model imports, snapshot schema, exact arXiv behavior and finite Agent retry policy.
- Adds no new runtime interface.

- [ ] **Step 1: Add a documentation contract test**

Append a test that reads `SKILL.md` and asserts the presence of these exact contract tokens:

```python
def test_skill_documents_exact_agent_execution_contract() -> None:
    text = Path("SKILL.md").read_text(encoding="utf-8")
    required = [
        "get_paper_by_arxiv_id",
        "AuthorDisambiguator.score_pair",
        "AuthorDisambiguator.cluster",
        "AuthorDisambiguator.disambiguate",
        "KnowledgeGraph.add_node",
        "KnowledgeGraph.add_edge",
        "KnowledgeGraph.load_snapshot",
        '"node_count"',
        '"edge_count"',
        "不得创建无界",
        "PARTIAL",
        "BLOCKED",
    ]
    assert all(token in text for token in required)
```

- [ ] **Step 2: Run the documentation test and verify RED**

Run:

```powershell
python -m pytest -q tests/test_fix_ag.py -k skill --no-cov
```

Expected: failure listing missing exact signatures/stop-contract tokens.

- [ ] **Step 3: Update `SKILL.md`**

Add:

- top-level public import examples for `Paper`, `Author`, `AuthorRef`, `Citation`, `Evidence`, `CollectionResult`, `AuthorDisambiguator`, and `KnowledgeGraph`;
- exact `AuthorDisambiguator.score_pair(a, b)`, `.cluster(authors)`, and `.disambiguate(authors)` signatures and return meanings;
- exact `KnowledgeGraph.add_node`, `.add_edge`, `.save_snapshot`, and classmethod `.load_snapshot` signatures, explicitly stating that load returns a new graph;
- snapshot JSON required fields `version`, `directed`, `nodes`, `edges`, `node_count`, `edge_count`, including count equality, unique node ID, unique directed edge pair and existing endpoint validation;
- exact arXiv ID routing forms and the guarantee that exact lookup does not return mention-only records;
- an Agent retry budget section containing the approved internal-vs-outer retry, PARTIAL and BLOCKED rules.

- [ ] **Step 4: Synchronize user and architecture docs**

Update `README.md` identifier notes, `docs/api.md` public behavior, and `docs/architecture.md` module boundaries. Prepend two decisions to `docs/decisions.md`:

- exact identifiers are classified at Collector boundaries and executed only by capable sources;
- retry policy remains transport-owned, while retry/status observability is preserved through exception chains.

- [ ] **Step 5: Run documentation contract and strict build GREEN**

Run:

```powershell
python -m pytest -q tests/test_fix_ag.py -k skill --no-cov
python -m mkdocs build --strict
```

Expected: contract test passes and MkDocs completes without errors.

### Task 4: Full regression and static verification

**Files:**
- Modify after results: `docs/progress.md`

**Interfaces:** none; verification only.

- [ ] **Step 1: Run the complete test suite**

Run:

```powershell
python -m pytest -q
```

Expected: all tests pass with coverage at or above the existing 92% baseline.

- [ ] **Step 2: Run static gates**

Run:

```powershell
python -m ruff check academic_intelligence tests/test_fix_ag.py
python -m mypy academic_intelligence
git diff --check -- academic_intelligence/collectors/base.py academic_intelligence/sources/arxiv.py academic_intelligence/utils/retry.py academic_intelligence/core/exceptions.py tests/test_fix_ag.py SKILL.md README.md docs/api.md docs/architecture.md docs/decisions.md docs/progress.md
```

Expected: Ruff 0, mypy success, diff check clean except harmless line-ending warnings.

- [ ] **Step 3: Record actual results in `docs/progress.md`**

Replace the in-progress section with completed status, exact test counts/times, coverage, static results, clean-wheel results and any external network limitation. Do not write generic “tested” text.

### Task 5: Clean-wheel dogfood and bounded live verification

**Files:**
- Build output: a new task-owned directory under `D:\agent_workspace\tmp\projects\paper-research-crawler\`.
- Modify after results: `docs/progress.md`.

**Interfaces:** installed `ai` console entry and public Python imports.

- [ ] **Step 1: Build a wheel into a fresh task-owned temp directory**

Use `python -m build --wheel --outdir <temp>`, creating the destination under the workspace temp namespace. If the `build` module is unavailable, use `python -m pip wheel . --no-deps --wheel-dir <temp>`.

Expected: one `academic_intelligence-0.1.0-py3-none-any.whl`.

- [ ] **Step 2: Install into a clean virtual environment**

Create a new venv under the same temp directory, install only the wheel, and run:

```powershell
<venv>\Scripts\python.exe -m pip check
<venv>\Scripts\ai.exe --version
<venv>\Scripts\ai.exe --help
```

Expected: dependency check clean, version/help exit 0.

- [ ] **Step 3: Run installed-wheel offline behavior dogfood**

Run a standalone script from outside the repository that subclasses installed `ArxivSource` with an in-memory exact/search probe, calls installed `MultiSourceCollector.collect_paper("1810.04805")`, and asserts one target plus `get_paper_by_arxiv_id` routing. Separately run installed `HTTPClient` with `MockTransport(429)` and assert installed `SourceFailure.retry_count == 2` and `http_status == 429`.

Expected: both assertions pass without importing project source from the checkout.

- [ ] **Step 4: Perform one bounded live pass if arXiv is reachable**

From the clean wheel, query `1810.04805`, `1512.03385`, and `2303.08774` sequentially with `sources=["arxiv"]`. Do not add an outer retry loop. For each successful query assert exactly one record with the matching canonical arXiv ID. If the source is unavailable/rate-limited, record the exact structured error and stop the live pass; the deterministic offline wheel test remains the acceptance authority.

- [ ] **Step 5: Finalize progress and evidence paths**

Record wheel path, SHA256, venv path, dogfood commands and outcomes in `docs/progress.md`. Re-run `python -m mkdocs build --strict` after the final documentation edit.

## Plan Self-Review

- Spec coverage: exact routing, strict adapter validation, retry metadata, Skill stop contract, full gates and wheel dogfood are each mapped to one task.
- Placeholder scan: no TBD/TODO/“implement later” steps are present.
- Type consistency: `_parse_arxiv_id`, `_canonical_arxiv_id`, `get_paper_by_arxiv_id`, `retry_count`, and `http_status` names are consistent across tasks.
- Scope: no generic identifier framework, new exception type, dependency, schema change, or unrelated refactor is included.
