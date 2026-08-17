# Changelog

## 2026-08-17 — Bare `.[dev]` gate green again (fresh-install verification fixes)

### Fixed

- **Fresh `pip install -e ".[dev]"` now passes the offline gate as documented**: `brotli`, `beautifulsoup4`, `cssselect` added to the `dev` extra. Before, `tests/test_fix_f.py` failed at collection (`ModuleNotFoundError: brotli`) and the 5 webcrawler CSS-extraction tests failed on a bare install — both were previously masked by environments that happened to have the packages.
- **`crawler` extra now contains what its own error message tells users to install**: `beautifulsoup4` + `cssselect` added (extractors.py advises "install the [crawler] extra" for CSS rules).
- **`mypy academic_intelligence` is 0 errors on every environment again** (bare `.[dev]` and with extras): stale `# type: ignore` comments removed (`fulltext/parser.py`, `webcrawler/extractors.py`), redundant cast removed in `utils/retry.py`, `utils/curl_fetcher.py` loads `curl_cffi` via `importlib.import_module` (env-independent `ModuleType | None`), and a mypy override tolerates absent/untyped `bs4` / `cssselect` / `fitz`.
- **Docs**: SKILL.md (§3.3 example, §11.1b, §11.4) and HANDOFF.md told users to run `paper source ss ...`, which fails (`No such command`); corrected to `paper source semantic_scholar ...`, plus a note under the capability matrix that short aliases (`ss`/`s2`/`oa`/`gs`) only apply to `--sources` selection — the `paper source` subcommand only mounts `europe-pmc`/`epmc`/`coci`.

### Verified

- bare `.[dev]` venv: quick suite 1331 passed / 4 skipped (optional-dep degradation skips), `ruff` 0, `mypy` 0
- venv with extras: quick suite 1334 passed / 1 skipped, `ruff` 0, `mypy` 0
- live smoke: arxiv get, crossref get, multi-source collect (crossref+oa fusion), `epmc` alias, author search all working

## 2026-08-11 — Crawler upgrade: `ai` → `paper`, new sources & pipelines

### Breaking Change

- **CLI renamed `ai` → `paper`** (I3). The `ai` entry point is now a shim that prints `Command 'ai' was renamed to 'paper'` and exits with code 2. Scripts, CI jobs, and documentation must call `paper` instead.

### Added

- **Five new free sources**: Crossref, Unpaywall, Europe PMC, OpenCitations (COCI), CORE — wired into collection and the direct `paper source <source> <operation>` interface (declared operations: crossref `search`/`get`; unpaywall `get`/`fulltext`; europe_pmc `search`/`get`/`fulltext`; opencitations `citations`; core `search`/`get`/`fulltext`)
- **Full-text pipeline** (M15): `paper fulltext <identifier>` locates → downloads → parses → segments legal OA full text (locators: unpaywall / core / arxiv / europe_pmc)
- **Web crawling tools**: `paper web crawl <url>` with robots pre-check and optional `--extract <schema.json>` structured extraction (blocked/failed exit 2)
- **Local PDF tools**: `paper pdf parse <file>` (pages → paragraphs, JSONL output)
- **Author identity resolution** (WP6/Q2/Q3/Q6/Q7/I8): `paper author resolve <paper_id> <name>` / `profile <author_id>` / `search <name>` / `confirm <candidate_id> --for <paper_id> --name <name>`
- **Source registry & budget**: `paper sources status` (capability matrix + per-source quota status) and `paper budget` (all-source quota overview)
- **New database tables**: `full_text`, `budget_usage`, `crawl_cache`, `author_identity_global`, `author_identity`

### Changed

- `paper source` subcommand aliases: `europe_pmc` (also `europe-pmc` / `epmc`), `opencitations` (also `coci`); Google Scholar adapter retained but **not registered by default** (`enable_google_scholar=True` to activate)
- New environment variables: `UNPAYWALL_EMAIL` (required for Unpaywall), `CORE_API_KEY` (optional, CORE), `CROSSREF_MAILTO` (Crossref polite pool)

### Migration notes

1. Replace `ai ...` with `paper ...` in scripts, cron jobs, and docs (one-for-one; the command surface is unchanged for existing commands).
2. `ai --version` / `ai --help` now exit 2 with a rename message — update any guard that expected success.
3. Databases open with the new tables automatically; no manual migration needed for existing SQLite/JSON stores.

## 2026-08-09 — Correctness and trust hardening

- Sanitized HTTP exception request/response attributes, classified native transport timeouts as transient, and made cache/facade async lifecycle cancellation-safe.
- Made SQLite/JSON query and identity behavior converge: Unicode interests, structured keywords, stable citation IDs, and retractable coauthorship aggregates.
- Wired author disambiguation into multi-source collection and hardened graph snapshot counts/duplicate validation.
- Added direct CLI commands (`paper`, `author`, `author-papers`, `update --author`) and non-zero total-expansion failure semantics.
- Added stable-schema Parquet, explicit Excel-safe CSV, JSON same-directory writer exclusion, CLI coverage, and generated-artifact ignore rules.
- Declared Click as a direct runtime dependency after clean-wheel dogfood exposed that the CLI imported it without packaging it; optional Parquet import failures now stay concise even with an ABI-broken local pyarrow.

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/), and this project adheres to [Semantic Versioning](https://semver.org/).

## Unreleased

### Added

- Data model v2: `AuthorRef` (ordered authors with correspondence), `evidence_list` per record, `arxiv_id` / `pmid` / `fields_of_study` / `references` / `citations_list` on `Paper`, identity + disambiguation fields on `Author` (`orcid`, `semantic_scholar_id`, `openalex_id`, `aliases`, `disambiguation_status`, `coauthors`, `venues`, `active_years`)
- Full six-source wiring: arXiv, OpenAlex, Semantic Scholar, PubMed, IEEE Xplore, Google Scholar (SerpAPI) adapters
- Deduplication v2: union-find transitive closure, arXiv↔DOI cross-ID matching, SequenceMatcher title similarity with Jaccard guard
- Author disambiguation: `AuthorDisambiguator` (ID direct link + heuristic feature clustering; `auto` / `ambiguous` / `confirmed` status)
- Confidence scoring: `ConfidenceScorer` with per-source baselines, multi-source bonus, DOI/PDF/staleness adjustments
- Knowledge graph layer (`graph/`): `expand` / `subgraph` / `path`, lazy loading with placeholder nodes, LRU graph cache — no networkx dependency
- CLI: `ai expand` and `ai export` commands
- Incremental updates: stale-gated refresh (`paper_refresh_days`), field-level change detection, per-source last-update tracking
- Error handling: `AllSourcesFailedError` carries a per-source `failures` map
- Acceptance integration tests for design §17 (six criteria), offline via cassette replay
- Deduplication scaling (B7-P43): `deduplicate_papers` dispatches inputs ≥ 1024 records to a bucketed implementation (global exact-ID union + conflict-class skip + title-token blocking) that is partition-identical to the O(n²) loop — 10k same-DOI records drop from 71-93s to <1s; the new `deduplicate_papers_bucketed` entry point exposes it directly
- Author-name query index (B7-P43): `paper_author_tokens` (per-name token/window B-tree) + `paper_author_names_fts` (FTS5 trigram) serve `query_papers(author=...)`'s prefilter; the 100k-library author filter drops from 718-829ms to ~16-90ms for typical queries, with identical `author_name_matches` semantics (CJK / Cyrillic / middle-initial / substring) and auto-built on old databases

### Changed

- SQLite storage: added `authorships`, `coauthorships`, `evidence`, `paper_hashes`, `source_updates` tables; automatic v2 column migration for pre-existing databases
- SQLite storage: `query_papers(author=...)` now filters through the materialized author-name index (replacing the raw-JSON `LIKE` scan); the index is maintained by every paper write path and auto-backfilled on connect for pre-existing databases
- Google Scholar requires a SerpAPI key (`serpapi_key` / `SERPAPI_KEY`)
- Citations table: `(citing_paper_id, cited_paper_id)` unique index — duplicate inserts from old binaries surface as `IntegrityError` instead of silent duplicate rows; the current API upserts via `ON CONFLICT` (FIX-V / Z-3)
- Storage migration: base columns are now migrated alongside v2 columns (`ALTER TABLE ADD COLUMN` idempotent); databases missing required columns (`papers.title` / `authors.name` / `citations.id`) fail connect with a clear "schema too old" `StorageError`; NOT NULL list/status columns carry server defaults (`DEFAULT '[]'` / `DEFAULT 'auto'`) (FIX-Z / FIX-Z2)
- Incremental updates: refresh gating is now per `(entity, source)` via the `entity_sync` table; multi-source field conflicts from merged updates surface on `IncrementalUpdateResult.warnings`; new values win over stored ones with a priority margin (FIX-B2 / FIX-V)
- HTTP cache: `cache_persistent` / `cache_path` config options persist the cache to disk across processes (FIX-Y)
- Security: `storage_path` / `cache_path` traversal outside the working directory is rejected at `Config` validation (C-2); error messages no longer embed raw SQL; absolute paths removed from stats / error context; default retry scope narrowed to HTTP errors (FIX-AA)
- CLI: new `ai collect citations <paper_id>` subcommand for collecting citing works; `ai expand` reports per-failure reasons via `stats.failures` (FIX-T / FIX-R)
- Performance: `query_papers(keyword=...)` served through an FTS5 trigram index (`paper_text_fts`); pubmed/arxiv parse throughput improved (`_safe_doi` lightweight validation); performance regression guards added under `tests/performance/` (FIX-AB)

## 0.1.0 (2026-07-21)

### Features

- Multi-source academic data collection (Google Scholar via SerpAPI, Semantic Scholar, OpenAlex; arXiv / PubMed / IEEE planned)
- Evidence chain tracking on authors, papers, and citations
- Data deduplication and multi-source fusion
- Incremental update models (`ChangeDetection`, `IncrementalUpdateResult`)
- SQLite and JSON storage backends
- CLI tool (`ai`) for collect, query, and stats
- Anti-crawl utilities: proxy pool, rate limiting, retry with backoff, HTTP cache
- Processors for enrichment and validation
- Library facade `AcademicIntelligence` with async context manager

### Documentation

- MkDocs Material documentation site
- Getting started, user guide, API reference (mkdocstrings), development docs


## Unreleased (2026-08-09 第三轮)

### FIX-AD 故障恢复
- 写路径错误消息卫生(12 个写方法去 `[SQL: ...]`)
- WAL checkpoint(TRUNCATE)+ `-wal` 残留告警,收窄崩溃丢失窗口
- JSON 错误路径绝对路径脱敏

### FIX-AE 并发
- 写路径 `database is locked` 有界重试(12 个写方法 @_retry_busy)
- `Config.sqlite_busy_timeout` 可配置
- NullPool 并发契约文档化

### FIX-AF 隐私合规
- 错误路径密钥脱敏(api_key/apikey 等 query 参数 → ***)
- delete_paper/delete_author 级联清理(evidence/citations/authorships/hashes/entity_sync)
- 代理凭据 user:pass 脱敏(序列化 + 日志)

## Codex 轮(2026-08-09 第三方全面审查+升级)

### 审查轮(Codex fast 审查 + 主 Agent 独立复核)
- 确认 3 个 Important 数据一致性 bug: 增量身份漂移(I-1)/JSON citation 重复边(I-2)/JSON coauthorship 重复累计(I-3)
- 确认 4 个无效 Config 字段(I-4)、JSON 非原子持久化风险(I-5)、ruff 810/mypy 38 门禁失败(I-6)
- Minor: close 首错阻断清理(M-1)、Cache stampede+非原子(M-2)、全量测试可操作性(M-3)

### 升级轮(Codex 高推理,TDD,784→795)
- I-1 增量更新固定 old storage ID 为 apply 主键(JSON/SQLite 参数化回归)
- I-2 JSON citation 以 (citing,cited) pair 为领域身份,幂等 upsert
- I-3 coauthorship 从不可逆 +1 改为基于 _authorships 重算(可撤销)
- I-4 rate_limit/max_concurrent_requests/author_refresh_days/enable_google_scholar 全部接线
- I-5 JSON 单文件原子快照(temp+fsync+os.replace) + asyncio.to_thread 落盘 + legacy 迁移
- I-6 ruff 0 / mypy 0(41 files);配置迁移 [tool.ruff.lint]
- M-1 close 逐项 best-effort + ExceptionGroup 聚合;M-2 Cache single-flight + 原子线程写;M-3 测试分层(marker + fast 命令)

### 复审轮(codex review --uncommitted,795→800)
- 修复 P2: all-source 别名未展开导致 stale gate 失效(_source_names 展开 all/*)
- 结果: 800 passed / 92% / ruff 0 / mypy 0

## Top 8~10 升级轮(2026-08-09,Codex 实施,800→817)

### 结构化错误 + 来源能力表
- `SourceFailure` dataclass(source/operation/error_type/message/retry_count/http_status/transient/permanent),旧字符串消费兼容
- `BaseSource.capabilities`/`supports()`;arXiv/IEEE 显式声明 get_citations=False,citation stub 空结果不再当真实空
- `ai.source_capabilities()` 无需 connect 查询;旧 duck-typed 适配器按方法推导

### 可持久化 graph 工作流
- `KnowledgeGraph.save_snapshot/load_snapshot`(原子写 + 版本校验,未知版本拒绝)
- CLI: `ai expand --output graph.json` 写快照 / `ai export --snapshot graph.json --center <id>` 跨进程读图

### 大规模查询与流式导出
- `query_papers/query_authors` 新增 order_by + after/cursor keyset 分页(非唯一排序值 ID tie-breaker)
- 新增 `exporters.export_papers` + `ai export-papers --format {csv,jsonl,parquet}`;CSV 标准库、JSONL 逐行、Parquet 懒导入可选([export] extra)
- 流式分批查询,不整库驻留内存

### 结果
- 817 passed / 92% / ruff 0 / mypy 0(42 files)/ MkDocs strict 通过
- codex 复审无离散可操作缺陷;主 Agent 抽查 T8/T9/T10 通过
