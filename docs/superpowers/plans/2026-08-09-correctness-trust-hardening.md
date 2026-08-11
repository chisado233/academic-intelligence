# Academic Intelligence Correctness and Trust Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `test-driven-development` to implement this plan task-by-task. `subagent-driven-development` is prohibited by the workspace dispatch policy; inline execution uses `executing-plans` checkpoints.

**Goal:** Make the public API, CLI, JSON/SQLite storage backends, graph snapshots, exports, documentation, and installed wheel satisfy every frozen hardening acceptance case.

**Architecture:** Keep the existing modules and public APIs. Add small contract helpers at the owning layer, repair derived-state maintenance transactionally, route convenience CLI commands through existing APIs, and lock every behavior with focused regression tests before implementation.

**Tech Stack:** Python 3.11+, asyncio, httpx, Pydantic v2, SQLAlchemy async + SQLite, Typer/Click, PyArrow optional export, pytest/pytest-asyncio, Ruff, mypy, MkDocs/Hatchling.

## Global Constraints

- Preserve every pre-existing user change in the approximately 240-entry dirty worktree.
- Do not delete or untrack existing generated artifacts.
- Do not commit, push, publish, deploy, or access live scholarly APIs.
- No production-code edit may precede its failing regression test.
- Required gates are SEC-01, DATA-01/02/03, QUERY-01/02, ASYNC-01,
  STORAGE-01, GRAPH-01, CLI-01, EXPORT-01, ENG-01, DOC-01, HYGIENE-01.
- Full-suite coverage must remain at least 90%; no frozen gate may be weakened.
- Main Agent alone edits shared project/Goal documentation and resolves conflicts.

---

## File map

**New focused regression files**

- `tests/test_hardening_security_async.py` — SEC-01 and ASYNC-01.
- `tests/test_hardening_storage_contracts.py` — DATA-01/02, QUERY-01/02,
  STORAGE-01.
- `tests/test_hardening_author_pipeline.py` — DATA-03.
- `tests/test_hardening_graph_cli_export.py` — GRAPH-01, CLI-01, EXPORT-01.

**Production files**

- `academic_intelligence/utils/http.py` — sanitized HTTP exception object graph.
- `academic_intelligence/core/exceptions.py` — native transient failure mapping.
- `academic_intelligence/utils/cache.py` — cancellation-isolated single-flight.
- `academic_intelligence/__init__.py` — serialized facade initialization.
- `academic_intelligence/core/models.py` — interest NFC normalization.
- `academic_intelligence/storage/json_store.py` — lifecycle and writer ownership.
- `academic_intelligence/storage/sqlite_store.py` — coauthorship convergence,
  citation IDs, keyword parity, interest parity.
- `academic_intelligence/collectors/base.py` — disambiguation in the public pipeline.
- `academic_intelligence/graph/knowledge_graph.py` — snapshot integrity checks.
- `academic_intelligence/exporters.py` — dependency handling and stable export schema.
- `academic_intelligence/cli.py` — exit semantics and promised commands.
- `pyproject.toml` — truthful test/coverage/tooling settings if required by fresh
  evidence.
- `.gitignore` — non-destructive generated-file exclusions.

**Shared docs updated by the Main Agent only**

- `README.md`, `SKILL.md`, `mkdocs.yml`.
- `docs/requirements.md`, `docs/features.md`, `docs/architecture.md`,
  `docs/api.md`, `docs/tech-stack.md`, `docs/deploy.md`, `docs/progress.md`,
  `docs/decisions.md`, `docs/goals/active.md`.

---

### Task 1: HTTP secret safety and native failure classification

**Acceptance:** SEC-01, ASYNC-01 (classification slice)

**Files:**

- Modify: `academic_intelligence/utils/http.py:28-87, 272-328, 386-396`
- Modify: `academic_intelligence/core/exceptions.py:42-47, 249-275`
- Create: `tests/test_hardening_security_async.py`

**Interfaces:**

- Produces: `_redact_exception(exc: BaseException) -> BaseException` returning a
  sanitized exception whose message and attached request/response are safe.
- Produces: `SourceFailure.from_exception(...)` native httpx/builtin timeout mapping.

- [ ] **Step 1: Write the failing secret-object-graph test**

```python
@pytest.mark.asyncio
async def test_get_json_redacts_secret_from_status_exception_object() -> None:
    secret = "SEC-01-DO-NOT-LEAK"
    transport = httpx.MockTransport(
        lambda request: httpx.Response(401, request=request, json={"error": "denied"})
    )
    client = HTTPClient(enable_cache=False)
    client._client = httpx.AsyncClient(transport=transport)  # real httpx response path

    with pytest.raises(httpx.HTTPStatusError) as caught:
        await client.get_json(
            "https://example.test/api",
            params={"api_key": secret},
            headers={"Authorization": f"Bearer {secret}"},
        )

    exc = caught.value
    observable = "\n".join(
        [str(exc), str(exc.request.url), repr(dict(exc.request.headers))]
    )
    assert secret not in observable
    assert "***" in observable
```

- [ ] **Step 2: Run RED**

Run: `python -m pytest -q --no-cov tests/test_hardening_security_async.py::test_get_json_redacts_secret_from_status_exception_object`

Expected: FAIL because the secret remains in `exc.request.url` and request headers.

- [ ] **Step 3: Implement sanitized request/response reconstruction**

Add helpers that build a minimal safe `httpx.Request`, scrub sensitive headers,
and reconstruct `HTTPStatusError`/`RequestError`. Change catch sites to raise the
returned sanitized object rather than mutating only `exc.args[0]`.

```python
_SENSITIVE_HEADERS = frozenset(
    {"authorization", "proxy-authorization", "cookie", "set-cookie", "x-api-key"}
)

def _safe_request(request: httpx.Request) -> httpx.Request:
    headers = {
        key: ("***" if key.lower() in _SENSITIVE_HEADERS else value)
        for key, value in request.headers.items()
    }
    return httpx.Request(
        request.method,
        redact_url_secrets(str(request.url)),
        headers=headers,
    )
```

For a status error, attach a new response with the safe request and status code.
For a request error, reconstruct the same concrete httpx exception class with the
redacted message and safe request. `get_json()` catches `HTTPStatusError` from
`raise_for_status()` and raises the reconstructed result.

- [ ] **Step 4: Add and RED-run native transient classification cases**

```python
@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectError("down"),
        httpx.ReadTimeout("slow"),
        TimeoutError("slow"),
        asyncio.TimeoutError("slow"),
    ],
)
def test_native_network_failures_are_transient(exc: BaseException) -> None:
    failure = SourceFailure.from_exception(source="stub", operation="fetch", exc=exc)
    assert failure.transient is True
    assert failure.permanent is False
```

Run the single parametrized test; expected RED is `transient=False`.

- [ ] **Step 5: Implement classification and run GREEN**

Import `builtins`, `asyncio`, and `httpx`; classify `httpx.TransportError`,
`builtins.TimeoutError`, the project `TimeoutError`, `RateLimitError`, and
`SourceUnavailableError` as transient. Preserve extracted status/retry metadata.

Run: `python -m pytest -q --no-cov tests/test_hardening_security_async.py`

Expected: PASS.

- [ ] **Step 6: Adjacent regression and checkpoint**

Run:

```powershell
python -m pytest -q --no-cov tests/test_fix_af.py tests/boundary/test_utils_boundary.py tests/integration/test_sources.py
python -m ruff check academic_intelligence/utils/http.py academic_intelligence/core/exceptions.py tests/test_hardening_security_async.py
python -m mypy academic_intelligence
git diff -- academic_intelligence/utils/http.py academic_intelligence/core/exceptions.py tests/test_hardening_security_async.py
```

Expected: tests, Ruff, and mypy pass; diff contains no credential literal. Do not commit.

---

### Task 2: Facade lifecycle and cancellation-safe cache single-flight

**Acceptance:** ASYNC-01

**Files:**

- Modify: `academic_intelligence/__init__.py:9-18, 163-243`
- Modify: `academic_intelligence/utils/cache.py:120-170`
- Extend: `tests/test_hardening_security_async.py`

**Interfaces:**

- Produces: `AcademicIntelligence._connect_lock: asyncio.Lock`.
- Preserves: `connect()`, `close()`, `_ensure()` signatures.
- Produces: cancellation isolation via `await asyncio.shield(task)`.

- [ ] **Step 1: Write and RED-run the concurrent connect test**

Use an `asyncio.Event`-controlled fake HTTP client and storage rather than sleeps.
Run three `ai.connect()` calls and assert one HTTP/storage instance exists and is
closed exactly once. Expected RED: three HTTP clients are constructed and only the
last published one is closed.

- [ ] **Step 2: Implement serialized, rollback-safe connect**

```python
self._connect_lock = asyncio.Lock()

async def connect(self) -> None:
    if self._connected:
        return
    async with self._connect_lock:
        if self._connected:
            return
        try:
            ...  # existing construction in the same order
        except BaseException:
            await self._rollback_connect()
            raise
```

Catch `BaseException` so cancellation during initialization also releases partially
constructed resources. Do not hold the lock in `close()`; `connect()` is the only
initialization publisher.

- [ ] **Step 3: Run connect GREEN and existing rollback tests**

Run:

```powershell
python -m pytest -q --no-cov tests/test_hardening_security_async.py -k connect
python -m pytest -q --no-cov tests/test_fix_aa.py -k connect
```

Expected: PASS.

- [ ] **Step 4: Write and RED-run waiter cancellation test**

Create two `Cache.get_or_set("same", factory)` tasks synchronized by Events, cancel
one waiter, release the factory, and assert the second returns the value and the
factory executed once. Expected RED: second waiter receives `CancelledError`.

- [ ] **Step 5: Implement shielded wait and run GREEN**

Replace `return await task` with `return await asyncio.shield(task)`. Keep inflight
cleanup in `_compute_and_store()` so one cancelled waiter cannot remove the shared
entry.

Run:

```powershell
python -m pytest -q --no-cov tests/test_hardening_security_async.py
python -m pytest -q --no-cov tests/test_utils.py tests/boundary/test_utils_boundary.py
python -m ruff check academic_intelligence/__init__.py academic_intelligence/utils/cache.py tests/test_hardening_security_async.py
git diff -- academic_intelligence/__init__.py academic_intelligence/utils/cache.py tests/test_hardening_security_async.py
```

Expected: PASS. Do not commit.

---

### Task 3: JSON lifecycle, Unicode interests, keyword parity, and citation identity

**Acceptance:** DATA-02, QUERY-01, QUERY-02, STORAGE-01

**Files:**

- Modify: `academic_intelligence/core/models.py:207-243`
- Modify: `academic_intelligence/storage/json_store.py:9-30, 88-126, 225-281, all write methods, 779-833`
- Modify: `academic_intelligence/storage/sqlite_store.py:995-1024, 2433-2460, 2831-2842, 2848-3251, 3253-3365`
- Create: `tests/test_hardening_storage_contracts.py`

**Interfaces:**

- Produces: `Author.interests` NFC-normalized on model validation.
- Produces: private JSON writer claim/release and `_ensure_connected()`.
- Preserves all public storage signatures.
- Produces stable stored citation IDs from SQLite `RETURNING`.

- [ ] **Step 1: Write parameterized RED tests**

Create JSON and SQLite fixtures and test:

```python
async def assert_keyword_contract(store: BaseStorage) -> None:
    await store.save_paper(Paper(id="kw", title="Control", keywords=["quantum entanglement"]))
    assert [p.id for p in await store.query_papers(keyword="quantum")] == ["kw"]

async def assert_interest_contract(store: BaseStorage) -> None:
    await store.save_author(Author(id="a", name="Ada", interests=["Résumé"]))
    hits = await store.query_authors(interest="Re\u0301sume\u0301")
    assert [a.id for a in hits] == ["a"]

async def assert_citation_contract(store: BaseStorage) -> None:
    citation = Citation(citing_paper_id="p1", cited_paper_id="p2", evidence=_evidence())
    assert await store.save_citation(citation) == await store.save_citation(citation)
```

Also cover batch citation replay and keyword literals `%`, `_`, backslash, ASCII,
and non-ASCII. Expected RED: SQLite keyword and citation ID; both interest paths.

- [ ] **Step 2: Implement interest normalization**

Add an `Author` validator returning `[normalize_nfc(item) for item in value]`.
Normalize and case-fold both query and stored values at JSON/SQLite read time so
legacy NFD rows also match.

- [ ] **Step 3: Implement keyword parity without replacing FTS**

Keep `paper_text_fts`. Add a correlated `json_each(papers.keywords)` match to the
plain SQLAlchemy predicate and equivalent `EXISTS` clause to the direct FTS SQL.
Use the same escaped pattern and literal-wildcard contract as title/abstract.

The SQL predicate must match an individual JSON array value, not serialized array
delimiters, to avoid cross-element false positives.

- [ ] **Step 4: Implement stable citation IDs**

Use SQLite `INSERT ... ON CONFLICT ... DO UPDATE ... RETURNING id` for the single
save and batch paths. The conflicting row keeps its original ID; the returned scalar
list becomes `ids["citations"]` in input order.

- [ ] **Step 5: Write and RED-run JSON lifecycle tests**

Test that a second connected JSON writer for the same resolved path raises
`StorageError`, closing the first releases ownership, and every write method rejects
after close with `StorageError`.

- [ ] **Step 6: Implement in-process writer ownership and guards**

Use a module-level `threading.Lock` plus `set[Path]` of resolved paths. Each instance
stores its claimed key. `connect()` is idempotent for the same instance, claims
before loading, and releases on load failure. `close()` is idempotent and releases in
`finally`. `_ensure_connected()` is called by every public mutation.

- [ ] **Step 7: Run GREEN and adjacent storage regression**

```powershell
python -m pytest -q --no-cov tests/test_hardening_storage_contracts.py
python -m pytest -q --no-cov tests/test_storage.py tests/boundary/test_storage_boundary.py tests/test_fix_ab.py tests/test_fix_ad.py tests/test_fix_ae.py
python -m ruff check academic_intelligence/core/models.py academic_intelligence/storage/json_store.py academic_intelligence/storage/sqlite_store.py tests/test_hardening_storage_contracts.py
python -m mypy academic_intelligence
git diff -- academic_intelligence/core/models.py academic_intelligence/storage/json_store.py academic_intelligence/storage/sqlite_store.py tests/test_hardening_storage_contracts.py
```

Expected: PASS. Do not commit.

---

### Task 4: Transactional SQLite coauthorship convergence

**Acceptance:** DATA-01

**Files:**

- Modify: `academic_intelligence/storage/sqlite_store.py:817-859, 1266-1390, 2143-2188, 2231-2337, 2644-2845`
- Extend: `tests/test_hardening_storage_contracts.py`

**Interfaces:**

- Produces: `_rebuild_coauthorships_for_authors(session, author_ids) -> None`.
- Preserves: `CoauthorshipRow` schema and all public storage methods.

- [ ] **Step 1: Write the full mutation-sequence RED test**

For both backends: insert paper `(a,b)`, replay, update to `(a,c)`, update year,
add another `(a,c)` paper, then delete each paper. Assert `get_coauthors()` after
every operation. For SQLite, query `CoauthorshipRow` and assert `paper_count`,
`first_year`, and `last_year`. Expected RED: stale `(a,b)` remains after update/delete.

- [ ] **Step 2: Implement bounded affected-author rebuild**

Before replacing/deleting authorships, read old author IDs. After the mutation,
delete `CoauthorshipRow` records whose A or B endpoint is in `old_ids | new_ids`.
Query current authorship rows for papers containing any affected author, load all
authors on those papers plus years, aggregate unordered resolved-author pairs, and
bulk upsert only pairs touching the affected set.

The helper runs in the caller's existing transaction. Pseudo IDs beginning `~` are
excluded. Paper replay does not increment; update/deletion retract old pairs.

- [ ] **Step 3: Wire every paper mutation path**

- Single `save_paper`: new paper keeps current delta fast path; existing paper uses
  affected rebuild.
- `update_paper`: capture old IDs, replace authorships, rebuild.
- `delete_paper`: capture old IDs, delete paper/authorships, rebuild.
- `save_batch`: capture previous authors for updated paper IDs in one query, perform
  existing bulk replacement, then rebuild union of old/new authors for updated rows;
  retain delta aggregation for purely new rows.

- [ ] **Step 4: GREEN, scale regression, and checkpoint**

```powershell
python -m pytest -q --no-cov tests/test_hardening_storage_contracts.py -k coauthor
python -m pytest -q --no-cov tests/test_b2_evidence_storage_dedup.py tests/test_fix_c.py tests/test_fix_g.py tests/test_fix_p.py tests/test_fix_af.py
python -m pytest -q --no-cov tests/performance/test_save_batch_performance.py
python -m ruff check academic_intelligence/storage/sqlite_store.py tests/test_hardening_storage_contracts.py
git diff -- academic_intelligence/storage/sqlite_store.py tests/test_hardening_storage_contracts.py
```

Expected: correctness and performance gates pass. Do not commit.

---

### Task 5: Public author disambiguation pipeline

**Acceptance:** DATA-03

**Files:**

- Modify: `academic_intelligence/collectors/base.py:13-23, 95-120, 257-292`
- Create: `tests/test_hardening_author_pipeline.py`

**Interfaces:**

- Produces: `BaseCollector.disambiguator: AuthorDisambiguator` configured from
  `Config.auto_merge_threshold` and `Config.ambiguous_threshold`.
- Preserves: collector method signatures and `CollectionResult` shape.

- [ ] **Step 1: Write public-pipeline RED tests**

Create deterministic sources returning:

1. `Wei Wang` at institution A/topic quantum and institution B/topic ecology — two
   output authors, not auto-merged.
2. Two profiles sharing the same valid ORCID — one merged output with two evidence
   entries.
3. Same-name ambiguous-band profiles — separate outputs marked `ambiguous`.

Call `MultiSourceCollector.collect_author_papers()`, not the disambiguator directly.
Expected RED: ordinary dedup merges the first pair.

- [ ] **Step 2: Wire existing disambiguator as the author identity stage**

Instantiate `DisambiguationConfig` from the two Config thresholds. Replace the
public pipeline's unconditional `deduplicate_authors()` call with
`disambiguator.disambiguate()`, then retain author enrichment.

Do not change the standalone `Deduplicator.deduplicate_authors()` API; callers that
explicitly ask for name-based fuzzy dedup keep that behavior.

- [ ] **Step 3: GREEN and disambiguator regression**

```powershell
python -m pytest -q --no-cov tests/test_hardening_author_pipeline.py
python -m pytest -q --no-cov tests/test_disambiguator.py tests/test_b2_evidence_storage_dedup.py tests/test_b4_wiring.py
python -m ruff check academic_intelligence/collectors/base.py tests/test_hardening_author_pipeline.py
python -m mypy academic_intelligence
git diff -- academic_intelligence/collectors/base.py tests/test_hardening_author_pipeline.py
```

Expected: PASS. Do not commit.

---

### Task 6: Snapshot integrity

**Acceptance:** GRAPH-01

**Files:**

- Modify: `academic_intelligence/graph/knowledge_graph.py:260-317`
- Create: `tests/test_hardening_graph_cli_export.py`

**Interfaces:**

- Preserves: `KnowledgeGraph.load_snapshot(path, *, cache_size=None)`.

- [ ] **Step 1: Write table-driven RED tests**

Start from a valid two-node/one-edge snapshot and mutate one property at a time:
wrong `node_count`, wrong `edge_count`, duplicate node ID, duplicate directed edge,
dangling endpoint, non-list arrays, unsupported version. Each load must raise a
specific `ValueError`; valid round-trip stays unchanged.

- [ ] **Step 2: Validate payload before graph construction**

Validate declared counts with exact integer types, reject duplicate node IDs while
building the ID set, pre-validate all edges and unique `(source,target)` keys, then
construct the graph. Preserve safe basename-only file errors.

- [ ] **Step 3: GREEN and graph regression**

```powershell
python -m pytest -q --no-cov tests/test_hardening_graph_cli_export.py -k snapshot
python -m pytest -q --no-cov tests/test_graph_knowledge_graph.py tests/test_graph_api.py tests/test_upgrade_top8_10.py -k "graph or snapshot"
python -m ruff check academic_intelligence/graph/knowledge_graph.py tests/test_hardening_graph_cli_export.py
git diff -- academic_intelligence/graph/knowledge_graph.py tests/test_hardening_graph_cli_export.py
```

Expected: PASS. Do not commit.

---

### Task 7: CLI automation contracts and robust exports

**Acceptance:** CLI-01, EXPORT-01

**Files:**

- Modify: `academic_intelligence/cli.py:172-346, 421-480, 523-550`
- Modify: `academic_intelligence/exporters.py`
- Extend: `tests/test_hardening_graph_cli_export.py`

**Interfaces:**

- Adds CLI: `paper`, `author`, `author-papers`, `update --author`.
- Extends: `export_papers(..., excel_safe: bool = False) -> int`.
- Adds CLI flag: `export-papers --excel-safe/--raw-csv`.

- [ ] **Step 1: Write and RED-run CLI help/forwarding tests**

Use Typer `CliRunner` and monkeypatch the lowest external boundary. Assert all four
promised commands appear in root help, direct commands forward arguments to the
existing API, and `update --author` prints/checks `IncrementalUpdateResult` counts.
Expected RED: commands do not exist.

- [ ] **Step 2: Add thin convenience commands**

Extract private async helpers for the existing collect-author/paper behavior so both
the nested and direct command call one code path. `author-papers` calls the author
helper. `update --author` calls `update_author_papers()` and emits `to_dict()` to
`--output` when requested.

- [ ] **Step 3: Write and RED-run expand exit semantics**

Patch `AcademicIntelligence.expand()` to return a total failure and a partial result.
Assert total failure exits 2, partial output exits 0 and includes the warning.

- [ ] **Step 4: Implement expand total-failure exit**

After printing detailed failures, raise `typer.Exit(code=2)` only when `failed > 0`
and no useful node/edge/cache/fetch result exists. Partial results remain exit 0.

- [ ] **Step 5: Write and RED-run optional dependency tests**

Patch `importlib.import_module` to raise plain `ImportError` for `pyarrow`; assert a
friendly `ExportDependencyError`. Use a small fake Arrow implementation to assert a
fixed schema is passed for empty, null-first, and multi-batch exports.

- [ ] **Step 6: Implement stable Parquet records**

Catch `ImportError` only during module loading. Define explicit Arrow types for
scalar paper fields. Encode nested/arbitrary structures (`authors`, list fields,
`evidence_list`) as deterministic JSON strings so every batch and empty output uses
one schema independent of observed null values. Pass the schema to every
`Table.from_pylist()` call.

- [ ] **Step 7: Write and RED-run Excel-safe CSV tests**

Export a title beginning `=HYPERLINK(...)` plus Chinese text. Raw mode remains UTF-8
without modification; Excel-safe mode starts with UTF-8 BOM and prefixes dangerous
text cells with an apostrophe. JSONL remains unchanged.

- [ ] **Step 8: Implement explicit Excel-safe mode**

Add a helper that converts nested values to deterministic JSON, then neutralizes
strings beginning with `=`, `+`, `-`, `@`, tab, or carriage return when
`excel_safe=True`. Open CSV as `utf-8-sig` in safe mode and `utf-8` otherwise. Reject
`--excel-safe` for non-CSV formats at the CLI boundary.

- [ ] **Step 9: GREEN, CLI/export regression, and checkpoint**

```powershell
python -m pytest -q --no-cov tests/test_hardening_graph_cli_export.py
python -m pytest -q --no-cov tests/test_graph_cli.py tests/test_import_and_cli.py tests/test_imports_and_cli.py tests/test_upgrade_top8_10.py
python -m ruff check academic_intelligence/cli.py academic_intelligence/exporters.py tests/test_hardening_graph_cli_export.py
python -m mypy academic_intelligence
git diff -- academic_intelligence/cli.py academic_intelligence/exporters.py tests/test_hardening_graph_cli_export.py
```

Expected: PASS. Do not commit.

---

### Task 8: Engineering gates, documentation truth, and installed-wheel dogfood

**Acceptance:** ENG-01, DOC-01, HYGIENE-01 plus final execution of all required IDs

**Files:**

- Modify mechanically: test files reported by Ruff.
- Create: `.gitignore`.
- Modify Main Agent docs listed in the file map.
- Modify: `README.md`, `SKILL.md`, `mkdocs.yml`, optionally `pyproject.toml` only when
  fresh coverage evidence supports the change.

**Interfaces:** None; this task aligns project truth and delivery evidence.

- [ ] **Step 1: Mechanically fix Ruff-safe test findings**

Run `python -m ruff check tests --fix`, inspect every change, then manually resolve
remaining F841/import-naming/SIM findings without deleting an assertion-relevant
fixture. Run the affected test file after each manual edit.

- [ ] **Step 2: Add non-destructive `.gitignore`**

Include `.venv/`, `__pycache__/`, `*.py[cod]`, `.pytest_cache/`, `.mypy_cache/`,
`.ruff_cache/`, `.coverage`, `coverage*.xml`, `htmlcov/`, `site/`, `dist/`, `build/`,
`*.egg-info/`, local `*.db`, and project-local worktree directories. Do not delete or
untrack files already tracked.

- [ ] **Step 3: Align user documentation**

Document direct and nested CLI commands, automation exit codes, how persisted IDs
feed `expand`, raw versus Excel-safe CSV, JSON single-process/single-writer limits,
Parquet extra installation, and the now-wired author disambiguation behavior.

- [ ] **Step 4: Fill the standard project docs**

Replace templates with project-specific requirements, features, stack, and deploy
instructions. Update architecture/API for the repaired contracts. Record decisions
for JSON writer fail-closed behavior, affected-author coauthorship rebuild, Parquet
nested JSON representation, and convenience CLI aliases.

Move the active Goal from its false completed state to remediation while work is in
progress; only mark completed after Step 8 evidence. Preserve its historical scope
and explicitly map old AC-4/6/8/9/10 to fresh commands/tests.

- [ ] **Step 5: Run focused frozen matrix**

```powershell
python -m pytest -q --no-cov tests/test_hardening_security_async.py tests/test_hardening_storage_contracts.py tests/test_hardening_author_pipeline.py tests/test_hardening_graph_cli_export.py
python -m ruff check academic_intelligence tests
python -m mypy academic_intelligence
python -m mkdocs build --strict
```

Expected: all focused tests pass; Ruff/mypy/MkDocs clean.

- [ ] **Step 6: Run fresh full suite and coverage**

Run: `python -m pytest -q`

Expected: zero failures/errors and total coverage at least 90%. Do not use checked-in
coverage artifacts as evidence; capture the fresh terminal summary.

- [ ] **Step 7: Build and install the wheel outside the source tree**

Build with `python -m pip wheel . --no-deps -w <fresh-temp-wheel-dir>`. Create a fresh
venv, install the wheel, switch cwd outside the project, and verify import, version,
root help, all direct command help, and storage stats.

- [ ] **Step 8: Installed-wheel dogfood**

In a fresh temp directory whose path contains spaces and Chinese characters:

- seed a SQLite DB through the installed package;
- query Unicode title/keyword/interest data;
- update/delete a paper and inspect coauthors;
- exercise graph snapshot save/load and reject a tampered snapshot;
- export CSV, JSONL, Excel-safe CSV, and Parquet in a clean export-enabled venv;
- verify total expand failure is non-zero;
- verify no secret appears in a captured HTTP failure.

- [ ] **Step 9: Independent review and repair loop**

Dispatch a cross-family reviewer and independent tester with read-only project access.
For every Critical/Important finding, add a failing test, fix it, rerun its focused
and affected regressions, then repeat the full final gates.

- [ ] **Step 10: Final documentation and Goal evidence**

Record exact commands, pass counts, coverage, wheel path, dogfood evidence, known
limits, and unchanged Git authorization in `docs/progress.md` and
`docs/goals/active.md`. Confirm no required ID is pending/failed and no Critical or
Important issue remains.

- [ ] **Step 11: Final workspace checkpoint**

```powershell
git status --short
git diff --check
git diff --stat
```

Expected: only intended source/test/docs/config changes plus pre-existing user changes;
no commit, push, deletion, or untracking action.

