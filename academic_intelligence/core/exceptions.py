"""
academic_intelligence.core.exceptions

Custom exception hierarchy for the Academic Intelligence system.

This module defines all domain-specific exceptions raised across the
library — from source-level scraping failures to data-validation and
storage errors.  Every exception carries enough context for upstream
callers to decide whether to retry, fallback to another source, or
abort the pipeline.

Architecture reference
--------------------
- Base class: AcademicIntelligenceError (root of the hierarchy)
- Source errors: raised by *sources/* plugins (Google Scholar, arXiv, …)
- Collector errors: raised by *collectors/* orchestration layer
- Processor errors: raised by *processors/* (dedup, enrich, validate)
- Storage errors: raised by *storage/* backends (SQLite, JSON)
- CLI errors: raised by *cli.py* when argument validation fails

Typical usage
-------------
    from academic_intelligence.core.exceptions import (
        SourceUnavailableError,
        RateLimitError,
        DataValidationError,
    )

    try:
        result = await collector.collect_author_papers("Geoffrey Hinton")
    except RateLimitError as exc:
        # Back-off and retry later
        logger.warning("Rate limited on %s", exc.source_name)
    except SourceUnavailableError:
        # Fallback to next source in priority list
        pass
    except DataValidationError as exc:
        # Log schema violations and continue with partial data
        logger.error("Validation failed: %s", exc.details)
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Base exception
# ---------------------------------------------------------------------------

class AcademicIntelligenceError(Exception):
    """Root exception for all Academic Intelligence errors.

    Attributes:
        message: Human-readable description of the failure.
        context: Optional structured data that helps debugging.
    """

    def __init__(
        self,
        message: str,
        *,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.context = context or {}

    def __str__(self) -> str:
        if self.context:
            return f"{self.message} (context={self.context})"
        return self.message


# ---------------------------------------------------------------------------
# Source-level errors  (raised by sources/ plugins)
# ---------------------------------------------------------------------------

class SourceError(AcademicIntelligenceError):
    """Base class for errors originating from a data source."""

    def __init__(
        self,
        message: str,
        *,
        source_name: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message, context=context)
        self.source_name = source_name


class SourceUnavailableError(SourceError):
    """Raised when a configured source is unreachable or permanently down.

    TODO: Add retry-count and last-successful-contact metadata.
    """

    def __init__(
        self,
        message: str,
        *,
        source_name: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message, source_name=source_name, context=context)
        # TODO: track retry_count and last_contact timestamp.


class RateLimitError(SourceError):
    """Raised when a source responds with HTTP 429 or equivalent rate-limit signal.

    TODO: Expose `retry_after` (seconds) so callers can schedule back-off.
    """

    def __init__(
        self,
        message: str,
        *,
        source_name: str,
        retry_after: Optional[int] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message, source_name=source_name, context=context)
        self.retry_after = retry_after
        # TODO: implement automatic back-off helper.


class AuthenticationError(SourceError):
    """Raised when a source rejects credentials or API keys.

    TODO: Include guidance text pointing to configuration docs.
    """

    def __init__(
        self,
        message: str,
        *,
        source_name: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message, source_name=source_name, context=context)
        # TODO: add `help_url` or `config_key` attribute.


class ParseError(SourceError):
    """Raised when the raw HTML/JSON from a source cannot be parsed.

    TODO: Keep a snippet of the raw payload for post-mortem analysis.
    """

    def __init__(
        self,
        message: str,
        *,
        source_name: str,
        raw_snippet: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message, source_name=source_name, context=context)
        self.raw_snippet = raw_snippet
        # TODO: cap raw_snippet length to avoid memory bloat.


# ---------------------------------------------------------------------------
# Collector-level errors  (raised by collectors/)
# ---------------------------------------------------------------------------

class CollectorError(AcademicIntelligenceError):
    """Base class for errors in the high-level collection orchestration."""

    def __init__(
        self,
        message: str,
        *,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message, context=context)


class AllSourcesFailedError(CollectorError):
    """Raised when every configured source fails for a given query.

    TODO: Aggregate per-source exceptions into `failures` list.
    """

    def __init__(
        self,
        message: str,
        *,
        query: str,
        sources_attempted: List[str],
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message, context=context)
        self.query = query
        self.sources_attempted = sources_attempted
        # TODO: capture the nested exceptions that caused each source to fail.


class PartialResultError(CollectorError):
    """Raised when some sources succeed but others fail, yielding incomplete data.

    TODO: Provide helper to decide whether partial data is acceptable.
    """

    def __init__(
        self,
        message: str,
        *,
        partial_result: Any,
        failed_sources: List[str],
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message, context=context)
        self.partial_result = partial_result
        self.failed_sources = failed_sources
        # TODO: add `is_acceptable(threshold: float) -> bool` helper.


# ---------------------------------------------------------------------------
# Processor-level errors  (raised by processors/)
# ---------------------------------------------------------------------------

class ProcessorError(AcademicIntelligenceError):
    """Base class for errors in deduplication, enrichment, or validation."""

    def __init__(
        self,
        message: str,
        *,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message, context=context)


class DataValidationError(ProcessorError):
    """Raised when a merged or raw record violates the expected schema.

    TODO: Include `schema_violations` mapping field -> expected_type.
    """

    def __init__(
        self,
        message: str,
        *,
        record_id: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message, context=context)
        self.record_id = record_id
        self.details = details or {}
        # TODO: implement `to_dict()` for structured logging.


class DeduplicationError(ProcessorError):
    """Raised when the deduplication engine encounters an unresolvable conflict.

    TODO: Expose the conflicting records so the caller can manually arbitrate.
    """

    def __init__(
        self,
        message: str,
        *,
        conflicting_ids: Optional[List[str]] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message, context=context)
        self.conflicting_ids = conflicting_ids or []
        # TODO: add `suggest_resolution()` heuristic helper.


class EnrichmentError(ProcessorError):
    """Raised when an enrichment step (e.g. citation count augmentation) fails.

    TODO: Distinguish between transient and permanent enrichment failures.
    """

    def __init__(
        self,
        message: str,
        *,
        enrichment_step: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message, context=context)
        self.enrichment_step = enrichment_step
        # TODO: add `is_transient` flag and recommended retry policy.


# ---------------------------------------------------------------------------
# Storage-level errors  (raised by storage/)
# ---------------------------------------------------------------------------

class StorageError(AcademicIntelligenceError):
    """Base class for errors in persistence backends (SQLite, JSON, etc.)."""

    def __init__(
        self,
        message: str,
        *,
        backend: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message, context=context)
        self.backend = backend


class RecordNotFoundError(StorageError):
    """Raised when a requested record does not exist in storage.

    TODO: Include the query/filter that produced zero results.
    """

    def __init__(
        self,
        message: str,
        *,
        backend: str,
        record_id: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message, backend=backend, context=context)
        self.record_id = record_id
        # TODO: add `query_params` attribute for debugging.


class StorageIntegrityError(StorageError):
    """Raised when stored data violates internal consistency constraints.

    TODO: Provide a repair-mode flag for automated cleanup.
    """

    def __init__(
        self,
        message: str,
        *,
        backend: str,
        constraint: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message, backend=backend, context=context)
        self.constraint = constraint
        # TODO: add `repair()` coroutine for automated recovery.


# ---------------------------------------------------------------------------
# CLI-level errors  (raised by cli.py)
# ---------------------------------------------------------------------------

class CLIError(AcademicIntelligenceError):
    """Base class for command-line interface errors."""

    def __init__(
        self,
        message: str,
        *,
        exit_code: int = 1,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message, context=context)
        self.exit_code = exit_code


class ConfigurationError(CLIError):
    """Raised when CLI arguments or config files are invalid.

    TODO: Pretty-print a human-readable remediation hint.
    """

    def __init__(
        self,
        message: str,
        *,
        config_key: Optional[str] = None,
        exit_code: int = 2,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message, exit_code=exit_code, context=context)
        self.config_key = config_key
        # TODO: implement `suggest_fix()` based on config schema.
