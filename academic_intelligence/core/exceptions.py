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

import builtins
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx

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
        context: dict[str, Any] | None = None,
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
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, context=context)
        self.source_name = source_name


class NotSupportedError(SourceError):
    """Raised when a source adapter does not support a requested operation.

    Sources that lack a capability (e.g. Unpaywall has no citation graph and
    no metadata search) raise this instead of returning an empty result, so
    callers can tell "the source does not implement this operation" apart
    from "no data found" (upgrade technical-design §1.1.1 C1 revision).
    """


class TimeoutError(SourceError):
    """Raised when a source request times out (read/connect/pool/write).

    Distinct from :class:`SourceUnavailableError` so callers can tell "the
    source is slow / timed out" apart from "the source is unreachable", and
    decide on their own retry/fallback policy (FIX-J F3).
    """

    def __init__(
        self,
        message: str,
        *,
        source_name: str,
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, source_name=source_name, context=context)


class SourceUnavailableError(SourceError):
    """Raised when a configured source is unreachable or permanently down.

    TODO: Add retry-count and last-successful-contact metadata.
    """

    def __init__(
        self,
        message: str,
        *,
        source_name: str,
        context: dict[str, Any] | None = None,
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
        retry_after: int | None = None,
        context: dict[str, Any] | None = None,
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
        context: dict[str, Any] | None = None,
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
        raw_snippet: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, source_name=source_name, context=context)
        self.raw_snippet = raw_snippet
        # TODO: cap raw_snippet length to avoid memory bloat.


@dataclass(frozen=True, eq=False)
class SourceFailure:
    """Structured, string-compatible description of a failed source call."""

    source: str
    operation: str
    error_type: str
    message: str
    retry_count: int = 0
    http_status: int | None = None
    transient: bool = False
    permanent: bool = True

    def __str__(self) -> str:
        return f"{self.source}: {self.message}"

    def __contains__(self, value: object) -> bool:
        return isinstance(value, str) and value in str(self)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, str):
            return other in {self.message, str(self)}
        if not isinstance(other, SourceFailure):
            return NotImplemented
        return self.to_dict() == other.to_dict()

    def to_dict(self) -> dict[str, str | int | bool | None]:
        """Return a JSON-compatible representation."""
        return {
            "source": self.source,
            "operation": self.operation,
            "error_type": self.error_type,
            "message": self.message,
            "retry_count": self.retry_count,
            "http_status": self.http_status,
            "transient": self.transient,
            "permanent": self.permanent,
        }

    @classmethod
    def from_message(
        cls,
        *,
        source: str,
        operation: str,
        message: str,
        error_type: str = "SourceError",
        retry_count: int = 0,
        http_status: int | None = None,
        transient: bool = False,
        permanent: bool | None = None,
    ) -> SourceFailure:
        """Build a structured record from a legacy string failure."""
        return cls(
            source=source,
            operation=operation,
            error_type=error_type,
            message=message,
            retry_count=retry_count,
            http_status=http_status,
            transient=transient,
            permanent=not transient if permanent is None else permanent,
        )

    @classmethod
    def from_exception(
        cls,
        *,
        source: str,
        operation: str,
        exc: BaseException,
    ) -> SourceFailure:
        """Extract retry/status/classification metadata from an exception."""
        retry_count: int | None = None
        status: int | None = None
        current: BaseException | None = exc
        seen: set[int] = set()

        # Adapters deliberately wrap transport errors in domain exceptions.
        # Walk a small, cycle-safe chain so retry/status metadata survives the
        # boundary.  Outer explicit context wins, including retry_count=0.
        while current is not None and len(seen) < 16:
            identity = id(current)
            if identity in seen:
                break
            seen.add(identity)

            raw_context = getattr(current, "context", None)
            context = raw_context if isinstance(raw_context, Mapping) else {}
            if retry_count is None:
                retry_count = cls._metadata_int(context.get("retry_count"))
                if retry_count is None:
                    retry_count = cls._metadata_int(getattr(current, "retry_count", None))
            if status is None:
                status = cls._metadata_int(context.get("http_status"))
                if status is None:
                    status = cls._metadata_int(getattr(current, "status_code", None))
                if status is None:
                    response = getattr(current, "response", None)
                    status = cls._metadata_int(getattr(response, "status_code", None))

            cause = current.__cause__
            if cause is not None:
                current = cause
            elif not current.__suppress_context__:
                current = current.__context__
            else:
                current = None
        is_transient = isinstance(
            exc,
            (
                RateLimitError,
                TimeoutError,
                SourceUnavailableError,
                builtins.TimeoutError,
                httpx.TransportError,
            ),
        )
        return cls.from_message(
            source=source,
            operation=operation,
            error_type=exc.__class__.__name__,
            message=getattr(exc, "message", str(exc)),
            retry_count=retry_count or 0,
            http_status=status,
            transient=is_transient,
        )

    @staticmethod
    def _metadata_int(value: object) -> int | None:
        """Return integer exception metadata without letting bad values escape."""
        if isinstance(value, bool) or not isinstance(value, int | str):
            return None
        try:
            return int(value)
        except ValueError:
            return None


# ---------------------------------------------------------------------------
# Collector-level errors  (raised by collectors/)
# ---------------------------------------------------------------------------

class CollectorError(AcademicIntelligenceError):
    """Base class for errors in the high-level collection orchestration."""

    def __init__(
        self,
        message: str,
        *,
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, context=context)


class AllSourcesFailedError(CollectorError):
    """Raised when every configured source fails for a given query.

    Attributes:
        query: The query that triggered the collection.
        sources_attempted: Names of every source that was tried.
        failures: Mapping ``source_name -> failure reason`` for each failed
            source (3A v2 §11.2: the exception message includes every reason).
    """

    def __init__(
        self,
        message: str,
        *,
        query: str,
        sources_attempted: list[str],
        failures: Mapping[str, str | SourceFailure] | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, context=context)
        self.query = query
        self.sources_attempted = list(sources_attempted)
        self.failures = {
            source: (
                failure
                if isinstance(failure, SourceFailure)
                else SourceFailure.from_message(
                    source=source,
                    operation="unknown",
                    message=failure,
                )
            )
            for source, failure in (failures or {}).items()
        }

    def __str__(self) -> str:
        bits: list[str] = []
        if self.failures:
            reasons = ", ".join(
                f"{source}: {reason.message}"
                for source, reason in self.failures.items()
            )
            bits.append(f"source failures: {reasons}")
        if self.context:
            bits.append(f"context={self.context}")
        suffix = f" ({'; '.join(bits)})" if bits else ""
        return f"{self.message}{suffix}"


class PartialResultError(CollectorError):
    """Raised when some sources succeed but others fail, yielding incomplete data.

    TODO: Provide helper to decide whether partial data is acceptable.
    """

    def __init__(
        self,
        message: str,
        *,
        partial_result: Any,
        failed_sources: list[str],
        context: dict[str, Any] | None = None,
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
        context: dict[str, Any] | None = None,
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
        record_id: str | None = None,
        details: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
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
        conflicting_ids: list[str] | None = None,
        context: dict[str, Any] | None = None,
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
        context: dict[str, Any] | None = None,
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
        context: dict[str, Any] | None = None,
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
        record_id: str | None = None,
        context: dict[str, Any] | None = None,
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
        constraint: str | None = None,
        context: dict[str, Any] | None = None,
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
        context: dict[str, Any] | None = None,
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
        config_key: str | None = None,
        exit_code: int = 2,
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, exit_code=exit_code, context=context)
        self.config_key = config_key
        # TODO: implement `suggest_fix()` based on config schema.
