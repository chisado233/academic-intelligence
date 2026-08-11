# Academic Intelligence correctness and trust hardening design

- Date: 2026-08-09
- Status: approved for planning
- Approved approach: A — contract-first, two-stage convergence
- Scope owner: primary Codex agent
- Source audit: `D:\agent_workspace\tmp\agent-dispatch\20260809-codex-optimize\HANDOFF.md`

## 1. Outcome

Bring `academic_intelligence` from “broad tests pass” to a state where its public
contracts, two storage backends, CLI, documentation, and installed-wheel behavior
agree under adversarial and real-user workflows.

The implementation must fix confirmed security, data-consistency, concurrency,
Unicode, snapshot, optional-dependency, and CLI-contract defects without a broad
rewrite. Existing user changes in the dirty worktree must be preserved.

## 2. Confirmed baseline

The design is based on fresh local evidence, not the handoff claims alone:

- `python -m pytest -q`: 817 passed; 92% total coverage.
- Focused round-2/top-10 regression: 27 passed.
- `python -m ruff check academic_intelligence`: clean.
- `python -m ruff check academic_intelligence tests`: 99 findings in tests.
- `python -m mypy academic_intelligence`: clean across 42 source files.
- `python -m mkdocs build --strict`: succeeds.
- Wheel build, isolated install, package import, console entry point, and help work.
- Installed-wheel dogfood works for SQLite paths containing spaces and Unicode,
  stats, query, CSV/JSONL export, expansion, and snapshot export.
- The worktree has about 240 status entries and tracked generated artifacts. There
  is no project `.gitignore`.

Passing this baseline does not cover the defects below; each defect was separately
reproduced or directly observed.

## 3. Goals

### 3.1 Required

1. Eliminate credential disclosure from HTTP exceptions and logs.
2. Make SQLite and JSON storage honor the same public query and mutation contracts.
3. Keep SQLite materialized coauthorship state correct after paper insert, update,
   replacement, and deletion.
4. Make public async lifecycle and single-flight behavior cancellation-safe.
5. Correctly classify native network failures as transient where appropriate.
6. Reject corrupt or internally inconsistent graph snapshots.
7. Return automation-safe CLI exit codes and restore CLI entries promised by the
   active Goal.
8. Degrade cleanly when optional Parquet dependencies are absent or broken.
9. Prevent the public collection path from silently merging clearly distinct
   same-name authors.
10. Make project documentation and completion claims match executable behavior.
11. End with clean source-and-test Ruff, mypy, strict docs, full pytest, wheel
    install, and installed-wheel dogfood gates.

### 3.2 Non-goals

- No rewrite of the storage layer, graph engine, collectors, or public API.
- No live-network regression suite; adapters use deterministic fakes/cassettes.
- No PyPI publication, GitHub push, deployment, or credential configuration.
- No Git-history rewrite or repository garbage collection.
- No removal or untracking of existing generated artifacts without separate user
  approval.
- No Git commit in this pass unless the user later authorizes it.

## 4. Public contracts

### 4.1 Storage parity

Both storage backends must implement the same behavior for the same model inputs:

- `query_papers(keyword=...)` searches normalized title, abstract, and keyword
  entries.
- Author interest filtering compares NFC-normalized, case-folded strings.
- Re-saving the same citation pair returns its existing relationship ID.
- Mutations attempted after `close()` raise a typed `StorageError`.
- A second writable `JSONStorage` instance for the same resolved directory in the
  same process is rejected explicitly rather than silently overwriting state.

The JSON backend remains documented as a single-process backend. The implementation
will add an in-process path registry so the supported process cannot accidentally
open two competing writers. Cross-process multi-writer support remains out of scope;
SQLite is the documented choice for that requirement.

### 4.2 Derived coauthorship state

Coauthorship counts are derived from current paper authorship, not append-only event
counts. Every SQLite paper mutation must update paper, authorship rows, author index,
FTS state, evidence, and affected coauthorship pairs within one transaction.

For an existing paper update:

1. Read the previous author-ID set.
2. Compute the new author-ID set.
3. Reconcile coauthorship pairs affected by either set against current authorship
   rows, or rebuild the bounded affected slice.
4. Commit the base paper and all derived state atomically.

For deletion, remove the paper and rebuild/remove all pairs affected by its former
authors before commit. Replaying an unchanged paper must not increase counts.

### 4.3 Author identity

The collector must not run unconditional same-name fusion before identity
disambiguation has enough information to protect distinct people.

The collection path will use `AuthorDisambiguator` semantics:

- shared ORCID/Semantic Scholar/OpenAlex identity IDs may merge directly;
- heuristic score at or above the configured auto-merge threshold may merge;
- the ambiguous band remains separate and is marked `ambiguous`;
- clearly different affiliation/topic profiles remain separate.

This preserves the existing standalone disambiguator and thresholds rather than
inventing another identity algorithm.

## 5. Component design

### 5.1 HTTP security and failure classification

`HTTPClient.get_json()` must not call `raise_for_status()` in a path that exposes a
raw request URL containing sensitive query parameters. Status handling will reuse a
single sanitizing boundary which:

- applies the existing sensitive-key policy to URL query parameters and headers;
- raises a project-owned typed exception, or reconstructs an `HTTPStatusError`,
  whose message, request URL, and response request URL are sanitized;
- preserves safe status/method/host context for debugging;
- never mutates or logs the secret value.

`SourceFailure.from_exception()` will recognize project exceptions plus native
`httpx` connection/timeout exceptions and built-in/asyncio timeout types. HTTP 4xx
other than retryable statuses remain permanent; rate limiting and transport/server
failures remain transient.

### 5.2 Facade lifecycle

`AcademicIntelligence.connect()` will be guarded by an async lock with a second
connected check inside the lock. Only one coroutine may construct resources.

Partial initialization must be exception-safe: resources constructed before a
later failure are closed in reverse order and are not published as the active
facade state. Concurrent callers receive the same success state or the same failure,
not separate resource sets.

`close()` remains idempotent and clears published state even when a child close
fails, while preserving the existing aggregated-error behavior.

### 5.3 Cache single-flight

Waiters will await the shared factory task through `asyncio.shield()`. Cancelling a
waiter cancels only that waiter; it does not cancel the shared fetch needed by other
waiters. The inflight entry remains owned by the shared task lifecycle and is removed
exactly once on task completion.

### 5.4 Search and normalization

A shared normalization helper will define NFC plus case-fold comparison for
human-entered text. It will be used for author interests in both backends and must
not alter persisted raw display values.

SQLite ASCII and non-ASCII keyword paths must implement the same title/abstract/
keywords contract. The existing FTS table is active and must be retained. Keywords
will either be included in the maintained FTS/index representation or handled by a
bounded companion predicate that preserves the fast path and exact semantics.

### 5.5 Citation identity

SQLite citation upsert must return the stored row ID for `(citing_paper_id,
cited_paper_id)`. It may select the existing ID before/after upsert or use a stable
pair-derived ID, but migration must not rewrite existing relationship IDs. Batch and
single-save paths share this rule.

### 5.6 Graph snapshots

`load_snapshot()` will validate before constructing the graph:

- declared `node_count` equals the node array length;
- declared `edge_count` equals the edge array length;
- node IDs are unique and non-empty;
- edges have valid unique IDs if the format declares them;
- every edge endpoint exists;
- the snapshot version is supported.

Invalid snapshots raise `ValueError` with a safe, actionable reason. No partial graph
is returned.

### 5.7 CLI contract

The CLI will distinguish success, partial success, user/input failure, and total
operation failure. In particular, `ai expand` with zero useful results and one or
more relation failures exits non-zero; partial results remain visible and are
explicitly labeled.

The active Goal's required convenience commands will be made executable with thin
wrappers over existing public APIs:

- `ai paper <identifier>`: collect one paper and print the existing paper result.
- `ai author <name>`: collect one author profile and print the existing author result.
- `ai author-papers <name>`: collect an author's papers using the existing API.
- `ai update --author <name>`: call `update_author_papers()` and report structured
  update statistics.

The existing `ai collect ...` commands remain supported. New wrappers do not create
a second business-logic path.

Documentation will stop presenting a DOI suffix such as `nature14539` as an
internal graph entity ID. Examples must show how to capture or query the persisted
`paper.id` before expansion.

### 5.8 Export behavior

Parquet dependency loading will convert `ImportError`-class failures caused by an
absent or ABI-broken `pyarrow` into `ExportDependencyError` with the export-extra
installation instruction. It will not suppress runtime errors after a successful
module import.

Parquet batches must have a stable schema derived independently of the first batch's
observed null values. Empty and multi-batch exports use the same declared schema.

CSV/Excel safety will be explicit rather than silently corrupting raw data: an
Excel-safe mode will emit UTF-8 BOM and neutralize formula-leading text fields. Raw
CSV remains available for lossless machine interchange, and the CLI/help must make
the distinction clear.

## 6. Error and exit semantics

- Secret-bearing inputs never appear in exception strings, request/response URLs,
  Rich output, or logger records.
- Public storage lifecycle misuse raises `StorageError` consistently.
- Corrupt snapshot and invalid CLI input errors exit with code 2.
- Runtime/source total failure exits non-zero while retaining per-source reasons.
- Partial success exits zero only when the requested operation produced useful
  output and the partial state is explicitly reported.
- Optional export dependency problems use `ExportDependencyError`, not raw import
  tracebacks.

## 7. Documentation and repository hygiene

The following project documents will be filled with real content and kept mutually
consistent:

- `docs/requirements.md`
- `docs/features.md`
- `docs/tech-stack.md`
- `docs/deploy.md`
- `docs/architecture.md`
- `docs/api.md`
- `docs/progress.md`
- `docs/decisions.md`
- `docs/goals/active.md`

The active Goal cannot remain “completed” during remediation. Its acceptance rows
will record fresh evidence only after implementation and all required gates pass.

A `.gitignore` will be added for local databases, Python caches, coverage output,
MkDocs output, and common build artifacts. Existing tracked files are not removed
from the index or filesystem in this scope; that cleanup requires a separate,
explicitly reviewed action because the worktree is already heavily modified.

## 8. Test strategy

All behavior changes use red-green-refactor:

1. Add a minimal failing regression that reproduces one confirmed defect.
2. Confirm it fails for the expected reason.
3. Implement the smallest contract-level fix.
4. Run the focused test and adjacent module tests.
5. Run the complete required gate set after each coherent batch.

Tests will be deterministic, use temporary paths, and avoid live network calls.
Storage contract cases will be parameterized over JSON and SQLite wherever both
backends support the operation.

## 9. Frozen acceptance cases

### SEC-01 — HTTP secret redaction

Given a 401/403 response whose request contains secrets in query parameters and
headers, neither `str(exception)`, `exception.request.url`, response request URL,
console output, nor captured logs may contain the original secret.

### DATA-01 — Coauthorship convergence

Insert a two-author paper, update it to one author, replace authors, replay it, then
delete it. SQLite and JSON coauthor results and counts must match after every step.

### DATA-02 — Citation identity

Repeated single and batch saves of the same citation pair return one stable ID and
store one relationship on both backends.

### DATA-03 — Same-name author separation

Two records with the same normalized name but clearly different affiliations and
topics remain distinct through the public collection path; shared authoritative ID
records merge.

### QUERY-01 — Keyword parity

Keywords appearing only in `Paper.keywords`, only in title, and only in abstract,
including ASCII/non-ASCII and literal wildcard characters, return identical ID sets
on SQLite and JSON.

### QUERY-02 — Unicode interest parity

NFC- and NFD-equivalent interest queries match the same authors on both backends.

### ASYNC-01 — Lifecycle and cancellation

Concurrent first connect creates one resource set and closes it once. Cancelling one
cache waiter does not cancel another waiter or the shared factory. Native connection
and timeout failures are classified transient.

### STORAGE-01 — JSON writer and close guards

A second same-directory JSON writer fails explicitly. Both backends reject writes
after close with `StorageError`.

### GRAPH-01 — Snapshot integrity

Tampered counts, duplicate node IDs, dangling edges, malformed arrays, and unsupported
versions are rejected. A valid snapshot round-trips unchanged.

### CLI-01 — Automation and promised commands

Total expansion failure exits non-zero; partial success is explicit. `paper`,
`author`, `author-papers`, and `update --author` help and deterministic stub-backed
execution work from an installed wheel.

### EXPORT-01 — Optional dependency and stable formats

Missing and ABI-broken `pyarrow` produce one friendly typed error. Empty, null-first,
and multi-batch Parquet exports have stable schemas. Excel-safe CSV prevents formula
execution and preserves readable Unicode.

### ENG-01 — Engineering gates

- `python -m ruff check academic_intelligence tests`: zero findings.
- `python -m mypy academic_intelligence`: success.
- `python -m pytest -q`: all tests pass, total coverage at least 90%, and new behavior
  has focused coverage.
- `python -m mkdocs build --strict`: success.
- Wheel builds and installs in an isolated environment; imports and CLI smoke pass.
- Installed-wheel Unicode-path dogfood passes without using the source tree.

### DOC-01 — Truthful project state

All standard project documents contain project-specific content, the Goal contains
no nonexistent command or false completion claim, and validation evidence is dated
and reproducible.

### HYGIENE-01 — Non-destructive hygiene

`.gitignore` prevents new cache/database/coverage/site noise. No pre-existing tracked
artifact is deleted or untracked in this implementation pass.

## 10. Implementation sequence

1. Security and async lifecycle.
2. Storage consistency and query parity.
3. Author identity and graph integrity.
4. CLI and export behavior.
5. Test-suite Ruff cleanup and engineering gates.
6. Documentation, Goal truth, `.gitignore`, installed-wheel dogfood, and independent
   review.

Each sequence point is a review checkpoint. A failure in a required acceptance case
blocks later completion claims; acceptance thresholds may not be silently weakened.

