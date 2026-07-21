# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/), and this project adheres to [Semantic Versioning](https://semver.org/).

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
