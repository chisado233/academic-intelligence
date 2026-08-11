"""JSON file storage backend for Academic Intelligence.

Provides simple file-based storage for Author, Paper, and Citation records
using JSON files. Suitable for small datasets and prototyping.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import threading
import uuid
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from academic_intelligence.core.exceptions import StorageError
from academic_intelligence.core.models import (
    Author,
    AuthorRef,
    Citation,
    Evidence,
    Paper,
)
from academic_intelligence.processors.scorer import ConfidenceScorer
from academic_intelligence.storage.base import BaseStorage
from academic_intelligence.utils.names import author_name_matches
from academic_intelligence.utils.normalize import normalize_nfc

_WRITER_REGISTRY_LOCK = threading.Lock()
_ACTIVE_WRITER_PATHS: set[str] = set()


def _new_id() -> str:
    return uuid.uuid4().hex


def _sanitize_file_error(path: Path, exc: Exception) -> str:
    """Exception text with every spelling of *path* reduced to its basename.

    (FIX-AD F3 / AD-3) Filesystem errors embed the absolute path in their own
    message (``PermissionError: [Errno 13] ... 'C:\\\\Users\\\\...\\\\papers.json'``),
    which would leak the storage location through the ``StorageError`` —
    unlike the sqlite backend (FIX-AA-2), the JSON backend previously exposed
    it verbatim (P49 round-31 V3.3: ``Failed to load D:\\...\\papers.json``).
    Windows spells the path three ways (forward slashes, single backslashes,
    escaped backslashes); every spelling of the file and its directory is
    replaced with a basename label so the detail stays diagnosable without
    leaking the location.
    """
    text = str(exc)
    for raw in (str(path), str(path.parent)):
        back = raw.replace("/", "\\")
        for spelling in {raw, back, back.replace("\\", "/"), back.replace("\\", "\\\\")}:
            label = os.path.basename(spelling) or spelling
            text = text.replace(spelling, label)
    return text


def _rebuild_synthetic_paper(paper: Paper) -> Paper:
    """Recompute the composite confidence of a loaded paper (I1).

    The composite confidence (multi-source bonus + field-level adjustments)
    is a derived quantity: the deprecated ``evidence`` alias is excluded from
    serialization and only per-source ``evidence_list`` entries are persisted.
    :meth:`ConfidenceScorer.score_paper` is an idempotent pure function, so
    rebuilding it on the read path keeps ``primary_evidence`` identical to the
    value computed at write time.
    """
    if not paper.evidence_list:
        return paper
    return ConfidenceScorer().score_paper(paper)


def _rebuild_synthetic_author(author: Author) -> Author:
    """Recompute the composite confidence of a loaded author (I1)."""
    if not author.evidence_list:
        return author
    return ConfidenceScorer().score_author(author)


def _authorship_key(ref: AuthorRef) -> str:
    """Stable author key for an authorship edge."""
    if ref.author_id:
        return ref.author_id
    return f"~{ref.name}"


class JSONStorage(BaseStorage):
    """JSON file-based storage implementation."""

    backend_name: str = "json"

    def __init__(self, base_path: str = "./data") -> None:
        """Initialize JSON storage.

        Args:
            base_path: Directory to store JSON files.
        """
        self.base_path = Path(base_path)
        self.connection_string = str(self.base_path)
        self._papers_file = self.base_path / "papers.json"
        self._authors_file = self.base_path / "authors.json"
        self._citations_file = self.base_path / "citations.json"
        self._hashes_file = self.base_path / "paper_hashes.json"
        self._source_updates_file = self.base_path / "source_updates.json"
        self._entity_sync_file = self.base_path / "entity_sync.json"
        self._evidence_file = self.base_path / "evidence.json"
        self._authorships_file = self.base_path / "authorships.json"
        self._coauthorships_file = self.base_path / "coauthorships.json"
        self._snapshot_file = self.base_path / "store.json"
        self._papers: dict[str, dict[str, Any]] = {}
        self._authors: dict[str, dict[str, Any]] = {}
        self._citations: dict[str, dict[str, Any]] = {}
        self._hashes: dict[str, str] = {}
        self._source_updates: dict[str, str] = {}
        # "entity_type|entity_id|source" -> isoformat of last sync time
        self._entity_sync: dict[str, str] = {}
        # "paper:<id>" / "author:<id>" -> list of evidence dicts
        self._evidence: dict[str, list[dict[str, Any]]] = {}
        # paper_id -> list of authorship dicts
        self._authorships: dict[str, list[dict[str, Any]]] = {}
        # "author_a|author_b" -> {"paper_count": int, "first_year": .., "last_year": ..}
        self._coauthorships: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()
        self._connected = False
        self._writer_key = os.path.normcase(str(self.base_path.resolve()))
        self._owns_writer_claim = False

    def _claim_writer(self) -> None:
        with _WRITER_REGISTRY_LOCK:
            if self._writer_key in _ACTIVE_WRITER_PATHS:
                raise StorageError(
                    "JSON storage directory is already open by another writer",
                    backend=self.backend_name,
                )
            _ACTIVE_WRITER_PATHS.add(self._writer_key)
            self._owns_writer_claim = True

    def _release_writer(self) -> None:
        if not self._owns_writer_claim:
            return
        with _WRITER_REGISTRY_LOCK:
            _ACTIVE_WRITER_PATHS.discard(self._writer_key)
            self._owns_writer_claim = False

    def _ensure_connected(self) -> None:
        if not self._connected:
            raise StorageError(
                "JSON storage is not connected",
                backend=self.backend_name,
            )

    def _load_file(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
            return {}
        except Exception as exc:
            raise StorageError(
                # (FIX-AD F3 / AD-3) Basename label only — never the absolute
                # path (sqlite FIX-AA-2 alignment; P49 V3.3 leaked it).
                f"Failed to load {path.name}: {_sanitize_file_error(path, exc)}",
                backend=self.backend_name,
            ) from exc

    def _save_file(self, path: Path, data: Mapping[str, Any]) -> None:
        temporary = path.with_name(f"{path.name}.tmp-{uuid.uuid4().hex}")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2, default=str)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except Exception as exc:
            # (FIX-AD F3 / AD-3) Basename label only — never the absolute
            # path (sqlite FIX-AA-2 alignment; a read-only or missing
            # directory surfaces a typed StorageError instead of a raw
            # OSError).
            raise StorageError(
                f"Failed to save {path.name}: {_sanitize_file_error(path, exc)}",
                backend=self.backend_name,
            ) from exc
        finally:
            temporary.unlink(missing_ok=True)

    def _snapshot_payload(self) -> dict[str, Any]:
        return {
            "version": 1,
            "papers": self._papers,
            "authors": self._authors,
            "citations": self._citations,
            "paper_hashes": self._hashes,
            "source_updates": self._source_updates,
            "entity_sync": self._entity_sync,
            "evidence": self._evidence,
            "authorships": self._authorships,
            "coauthorships": self._coauthorships,
        }

    def _load_legacy_state(self) -> dict[str, Any]:
        """Load the pre-snapshot nine-file layout for transparent migration."""
        return {
            "papers": self._load_file(self._papers_file),
            "authors": self._load_file(self._authors_file),
            "citations": self._load_file(self._citations_file),
            "paper_hashes": self._load_file(self._hashes_file),
            "source_updates": self._load_file(self._source_updates_file),
            "entity_sync": self._load_file(self._entity_sync_file),
            "evidence": self._load_file(self._evidence_file),
            "authorships": self._load_file(self._authorships_file),
            "coauthorships": self._load_file(self._coauthorships_file),
        }

    def _restore_state(self, state: dict[str, Any]) -> None:
        self._papers = dict(state.get("papers") or {})
        self._authors = dict(state.get("authors") or {})
        self._citations = dict(state.get("citations") or {})
        self._hashes = {
            k: str(v) for k, v in dict(state.get("paper_hashes") or {}).items()
        }
        self._source_updates = {
            k: str(v) for k, v in dict(state.get("source_updates") or {}).items()
        }
        self._entity_sync = {
            k: str(v) for k, v in dict(state.get("entity_sync") or {}).items()
        }
        self._evidence = dict(state.get("evidence") or {})
        self._authorships = dict(state.get("authorships") or {})
        self._coauthorships = dict(state.get("coauthorships") or {})

    def _save_legacy_mirrors(self) -> None:
        """Write the historical files on clean close for external readers."""
        mirrors: tuple[tuple[Path, Mapping[str, Any]], ...] = (
            (self._papers_file, self._papers),
            (self._authors_file, self._authors),
            (self._citations_file, self._citations),
            (self._hashes_file, self._hashes),
            (self._source_updates_file, self._source_updates),
            (self._entity_sync_file, self._entity_sync),
            (self._evidence_file, self._evidence),
            (self._authorships_file, self._authorships),
            (self._coauthorships_file, self._coauthorships),
        )
        for path, data in mirrors:
            self._save_file(path, data)

    async def connect(self) -> None:
        """Create directory and load existing data."""
        async with self._lock:
            if self._connected:
                return
            self._claim_writer()
            try:
                await asyncio.to_thread(self.base_path.mkdir, parents=True, exist_ok=True)
                snapshot_exists = await asyncio.to_thread(self._snapshot_file.exists)
                state: dict[str, Any]
                if snapshot_exists:
                    state = await asyncio.to_thread(self._load_file, self._snapshot_file)
                else:
                    state = await asyncio.to_thread(self._load_legacy_state)
                self._restore_state(state)
                self._rebuild_coauthorships()
                self._connected = True
            except BaseException as exc:
                self._release_writer()
                if isinstance(exc, StorageError):
                    raise
                if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                    raise
                raise StorageError(
                    f"Failed to connect JSON storage: {exc}",
                    backend=self.backend_name,
                ) from exc

    async def close(self) -> None:
        """Persist data to disk."""
        async with self._lock:
            if not self._connected:
                return
            try:
                await self._persist()
                await asyncio.to_thread(self._save_legacy_mirrors)
                self._connected = False
                self._release_writer()
            except StorageError:
                # (FIX-AD F3 / AD-3) A file-level failure already carries a
                # basename-only StorageError; do not double-wrap it.
                raise
            except Exception as exc:
                raise StorageError(
                    f"Failed to close/persist JSON storage: {exc}",
                    backend=self.backend_name,
                ) from exc

    async def _persist(self) -> None:
        self._ensure_connected()
        await asyncio.to_thread(
            self._save_file, self._snapshot_file, self._snapshot_payload()
        )

    async def save_paper(self, paper: Paper) -> str:
        self._ensure_connected()
        async with self._lock:
            paper_id = paper.id or _new_id()
            existed = paper_id in self._papers
            payload = paper.model_dump(mode="json")
            payload["id"] = paper_id
            self._papers[paper_id] = payload
            authors = self._resolve_name_author_refs(paper.authors)
            self._write_authorships(paper_id, authors, paper.year, count=not existed)
            self._evidence[f"paper:{paper_id}"] = [
                e.model_dump(mode="json") for e in paper.evidence_list
            ]
            await self._persist()
            return paper_id

    async def get_paper(self, paper_id: str) -> Paper | None:
        data = self._papers.get(paper_id)
        if not data:
            return None
        stored = self._evidence.get(f"paper:{paper_id}")
        if stored is not None:
            data = {**data, "evidence_list": stored}
        return _rebuild_synthetic_paper(Paper.model_validate(data))

    async def update_paper(self, paper_id: str, paper: Paper) -> bool:
        self._ensure_connected()
        async with self._lock:
            if paper_id not in self._papers:
                return False
            payload = paper.model_dump(mode="json")
            payload["id"] = paper_id
            self._papers[paper_id] = payload
            authors = self._resolve_name_author_refs(paper.authors)
            self._write_authorships(paper_id, authors, paper.year, count=False)
            self._evidence[f"paper:{paper_id}"] = [
                e.model_dump(mode="json") for e in paper.evidence_list
            ]
            await self._persist()
            return True

    async def delete_paper(self, paper_id: str) -> bool:
        self._ensure_connected()
        async with self._lock:
            if paper_id not in self._papers:
                return False
            del self._papers[paper_id]
            self._evidence.pop(f"paper:{paper_id}", None)
            self._authorships.pop(paper_id, None)
            self._rebuild_coauthorships()
            # (FIX-AF F2 / AF-2) Cascade-clean every reference to the paper:
            # citation edges (both directions), paper hash and per-entity sync
            # timestamps, mirroring the sqlite backend.
            self._citations = {
                cid: data
                for cid, data in self._citations.items()
                if data.get("citing_paper_id") != paper_id
                and data.get("cited_paper_id") != paper_id
            }
            self._hashes.pop(paper_id, None)
            sync_prefix = f"paper|{paper_id}|"
            self._entity_sync = {
                key: value
                for key, value in self._entity_sync.items()
                if not key.startswith(sync_prefix)
            }
            await self._persist()
            return True

    async def save_author(self, author: Author) -> str:
        self._ensure_connected()
        async with self._lock:
            author_id = author.id or _new_id()
            payload = author.model_dump(mode="json")
            payload["id"] = author_id
            self._authors[author_id] = payload
            self._evidence[f"author:{author_id}"] = [
                e.model_dump(mode="json") for e in author.evidence_list
            ]
            await self._persist()
            return author_id

    async def get_author(self, author_id: str) -> Author | None:
        data = self._authors.get(author_id)
        if not data:
            return None
        stored = self._evidence.get(f"author:{author_id}")
        if stored is not None:
            data = {**data, "evidence_list": stored}
        return _rebuild_synthetic_author(Author.model_validate(data))

    async def update_author(self, author_id: str, author: Author) -> bool:
        self._ensure_connected()
        async with self._lock:
            if author_id not in self._authors:
                return False
            payload = author.model_dump(mode="json")
            payload["id"] = author_id
            self._authors[author_id] = payload
            self._evidence[f"author:{author_id}"] = [
                e.model_dump(mode="json") for e in author.evidence_list
            ]
            await self._persist()
            return True

    async def delete_author(self, author_id: str) -> bool:
        self._ensure_connected()
        async with self._lock:
            if author_id not in self._authors:
                return False
            del self._authors[author_id]
            self._evidence.pop(f"author:{author_id}", None)
            # (FIX-AF F2 / AF-2) Cascade-clean every reference to the author:
            # authorship edges (in any paper) and coauthorship pairs on either
            # side, plus per-entity sync timestamps.  Papers are untouched.
            self._authorships = {
                paper_id: [
                    ref
                    for ref in refs
                    if ref.get("author_id") != author_id
                ]
                for paper_id, refs in self._authorships.items()
            }
            self._authorships = {
                paper_id: refs
                for paper_id, refs in self._authorships.items()
                if refs
            }
            self._rebuild_coauthorships()
            sync_prefix = f"author|{author_id}|"
            self._entity_sync = {
                key: value
                for key, value in self._entity_sync.items()
                if not key.startswith(sync_prefix)
            }
            await self._persist()
            return True

    def _name_to_id_index(self) -> dict[str, str]:
        """Case-insensitive name -> stored Author id map (first id wins)."""
        name_to_id: dict[str, str] = {}
        for stored_id, payload in self._authors.items():
            name = payload.get("name")
            if name:
                name_to_id.setdefault(str(name).strip().lower(), str(stored_id))
        return name_to_id

    def _resolve_name_author_refs(
        self,
        refs: list[AuthorRef],
        name_to_id: dict[str, str] | None = None,
    ) -> list[AuthorRef]:
        """Re-key unresolved (``author_id=None``) bylines to stored Author ids.

        (FIX-O F2) Mirrors the sqlite backend's FIX-M/N1 semantics: an Author
        persisted earlier (``save_author`` / ``save_batch``) is matched by
        exact name, case-insensitive, and a match re-keys the byline so the
        authorship edge points at the Author record instead of the ``~name``
        pseudo-key — ``get_author_papers(<author id>)`` then serves papers
        that name wrote.  Unmatched names keep the ``~name`` fallback.  JSON
        storage holds every record in memory, so no DB query or negative
        cache is needed.  ``save_batch`` builds the index once and shares it
        across the whole batch (N1), the first stored author of a name wins
        (the sqlite ``IN`` query's first-match-wins semantics).
        """
        name_to_id = self._name_to_id_index() if name_to_id is None else name_to_id
        resolved: list[AuthorRef] = []
        for ref in refs:
            if ref.author_id:
                resolved.append(ref)
                continue
            resolved_id = name_to_id.get(ref.name.strip().lower())
            if resolved_id:
                resolved.append(ref.model_copy(update={"author_id": resolved_id}))
            else:
                resolved.append(ref)
        return resolved

    def _write_authorships(
        self,
        paper_id: str,
        authors: list[AuthorRef],
        year: int | None,
        *,
        count: bool,
    ) -> None:
        """Record authorship + coauthorship edges for a paper (in-memory).

        Args:
            paper_id: Paper record ID.
            authors: Author references (in byline order).
            year: Paper publication year (coauthorship window).
            count: Retained for backward-compatible internal call sites. The
                aggregate is rebuilt from authorships, so replay/update
                semantics no longer depend on this flag.
        """
        self._authorships[paper_id] = [
            {
                "author_id": _authorship_key(ref),
                "position": ref.position,
                "is_corresponding": ref.is_corresponding,
                "raw_name": ref.name,
                "affiliation": ref.affiliation,
            }
            for ref in authors
        ]
        self._rebuild_coauthorships()

    def _rebuild_coauthorships(self) -> None:
        """Derive coauthorship aggregates from the current paper bylines.

        Rebuilding makes paper upserts, byline replacement, and deletion
        naturally reversible. JSON storage is explicitly the small-dataset
        backend, so correctness is preferred over maintaining an incremental
        counter that cannot safely retract old pairs.
        """
        rebuilt: dict[str, dict[str, Any]] = {}
        for paper_id, edges in self._authorships.items():
            resolved = sorted(
                {
                    str(edge["author_id"])
                    for edge in edges
                    if edge.get("author_id")
                    and not str(edge["author_id"]).startswith("~")
                }
            )
            if len(resolved) < 2:
                continue
            raw_year = self._papers.get(paper_id, {}).get("year")
            year = int(raw_year) if raw_year is not None else None
            for i, author_a in enumerate(resolved):
                for author_b in resolved[i + 1 :]:
                    key = f"{author_a}|{author_b}"
                    aggregate = rebuilt.setdefault(
                        key,
                        {"paper_count": 0, "first_year": None, "last_year": None},
                    )
                    aggregate["paper_count"] += 1
                    if year is not None:
                        first = aggregate["first_year"]
                        last = aggregate["last_year"]
                        aggregate["first_year"] = (
                            min(first, year) if first is not None else year
                        )
                        aggregate["last_year"] = (
                            max(last, year) if last is not None else year
                        )
        self._coauthorships = rebuilt

    def _upsert_citation(self, citation: Citation) -> str:
        """Upsert a citation by its domain identity: the directed paper pair."""
        citing = citation.citing_paper_id
        cited = citation.cited_paper_id
        citation_id = next(
            (
                stored_id
                for stored_id, data in self._citations.items()
                if data.get("citing_paper_id") == citing
                and data.get("cited_paper_id") == cited
            ),
            "",
        )
        if not citation_id:
            pair = f"{citing}\0{cited}".encode()
            citation_id = hashlib.sha256(pair).hexdigest()
        payload = citation.model_dump(mode="json")
        payload["id"] = citation_id
        self._citations[citation_id] = payload
        return citation_id

    async def save_citation(self, citation: Citation) -> str:
        self._ensure_connected()
        async with self._lock:
            citation_id = self._upsert_citation(citation)
            await self._persist()
            return citation_id

    async def get_citations_by_paper(
        self,
        paper_id: str,
        *,
        direction: str = "outgoing",
    ) -> list[Citation]:
        results: list[Citation] = []
        for data in self._citations.values():
            if direction == "incoming":
                if data.get("cited_paper_id") == paper_id:
                    results.append(Citation.model_validate(data))
            else:
                if data.get("citing_paper_id") == paper_id:
                    results.append(Citation.model_validate(data))
        return results

    # ------------------------------------------------------------------
    # Graph / relationship edges (3A v2 §8)
    # ------------------------------------------------------------------

    async def get_references(self, paper_id: str) -> list[str]:
        """Return the IDs of papers cited by *paper_id* (outgoing edges).

        Returns the deduplicated union of the ``citations`` edge table and
        the persisted ``papers.references`` JSON column (FIX-E-2): edges are
        the collected citation relations, the column is the adapter-parsed
        full reference list, and the two complement each other.  When the
        edge table is empty the result equals the column, preserving the
        storage-first fallback (FIX-B1 F2 / D-5).
        """
        refs = [
            str(data["cited_paper_id"])
            for data in self._citations.values()
            if data.get("citing_paper_id") == paper_id
        ]
        column = self._papers.get(paper_id, {}).get("references") or []
        refs.extend(str(ref) for ref in column)
        return list(dict.fromkeys(refs))

    async def get_citations(self, paper_id: str) -> list[str]:
        """Return the IDs of papers that cite *paper_id* (incoming edges).

        Deduplicated union of the ``citations`` edge table and the persisted
        ``papers.citations_list`` JSON column (FIX-E-2), mirroring
        :meth:`get_references`.
        """
        citing = [
            str(data["citing_paper_id"])
            for data in self._citations.values()
            if data.get("cited_paper_id") == paper_id
        ]
        column = self._papers.get(paper_id, {}).get("citations_list") or []
        citing.extend(str(c) for c in column)
        return list(dict.fromkeys(citing))

    async def get_author_papers(self, author_id: str) -> list[str]:
        """Return the IDs of papers authored by *author_id*."""
        result: list[str] = []
        for paper_id, authorships in self._authorships.items():
            if any(a.get("author_id") == author_id for a in authorships):
                result.append(paper_id)
        # Paper IDs never carry the ``~`` pseudo-author prefix; the filter
        # keeps the API free of unresolved-author keys (M3).
        return [pid for pid in result if not pid.startswith("~")]

    async def get_coauthors(self, author_id: str) -> list[str]:
        """Return the IDs of authors that co-authored papers with *author_id*.

        Reads the ``coauthorships`` store; falls back to deriving the pairs
        from ``authorships`` when the store is empty.
        """
        coauthors: list[str] = []
        for key in self._coauthorships:
            parts = key.split("|")
            if len(parts) != 2:
                continue
            a, b = parts
            if a == author_id:
                coauthors.append(b)
            elif b == author_id:
                coauthors.append(a)
        if coauthors:
            # Drop ``~name`` pseudo-author keys (unresolved byline names are
            # not real authors table primary keys) so graph callers never
            # expand a dead key (M3).
            return [c for c in dict.fromkeys(coauthors) if not c.startswith("~")]
        # Fallback: papers of this author → other authors on those papers.
        paper_ids = await self.get_author_papers(author_id)
        from_edges: list[str] = []
        for pid in paper_ids:
            for edge in self._authorships.get(pid, []):
                other = edge.get("author_id")
                if other and other != author_id:
                    from_edges.append(str(other))
        # Same ``~`` pseudo-key filter as the table path (M3).
        return [c for c in dict.fromkeys(from_edges) if not c.startswith("~")]

    async def save_evidence(
        self,
        entity_type: str,
        entity_id: str,
        evidence_list: list[Evidence],
    ) -> None:
        """Persist an evidence list for a record (``"paper"`` / ``"author"``)."""
        self._ensure_connected()
        async with self._lock:
            self._evidence[f"{entity_type}:{entity_id}"] = [
                e.model_dump(mode="json") for e in evidence_list
            ]
            await self._persist()

    async def get_evidence(
        self,
        entity_type: str,
        entity_id: str,
    ) -> list[Evidence]:
        """Return the persisted evidence list for a record."""
        stored = self._evidence.get(f"{entity_type}:{entity_id}", [])
        return [Evidence.model_validate(d) for d in stored if isinstance(d, dict)]

    async def save_batch(
        self,
        *,
        authors: list[Author] | None = None,
        papers: list[Paper] | None = None,
        citations: list[Citation] | None = None,
    ) -> dict[str, list[str]]:
        self._ensure_connected()
        ids: dict[str, list[str]] = {"authors": [], "papers": [], "citations": []}
        async with self._lock:
            for author in authors or []:
                author_id = author.id or _new_id()
                payload = author.model_dump(mode="json")
                payload["id"] = author_id
                self._authors[author_id] = payload
                self._evidence[f"author:{author_id}"] = [
                    e.model_dump(mode="json") for e in author.evidence_list
                ]
                ids["authors"].append(author_id)
            name_to_id = self._name_to_id_index()
            for paper in papers or []:
                paper_id = paper.id or _new_id()
                payload = paper.model_dump(mode="json")
                payload["id"] = paper_id
                self._papers[paper_id] = payload
                resolved_authors = self._resolve_name_author_refs(paper.authors, name_to_id)
                self._write_authorships(paper_id, resolved_authors, paper.year, count=True)
                self._evidence[f"paper:{paper_id}"] = [
                    e.model_dump(mode="json") for e in paper.evidence_list
                ]
                ids["papers"].append(paper_id)
            for citation in citations or []:
                citation_id = self._upsert_citation(citation)
                ids["citations"].append(citation_id)
            await self._persist()
        return ids

    async def query_papers(
        self,
        *,
        author: str | None = None,
        year: int | None = None,
        year_from: int | None = None,
        year_to: int | None = None,
        venue: str | None = None,
        keyword: str | None = None,
        limit: int = 100,
        offset: int = 0,
        order_by: str = "id",
        after: str | None = None,
        cursor: str | None = None,
    ) -> list[Paper]:
        # (FIX-I F4) Reject negative pagination up front so both backends
        # agree (sqlite previously treated limit=-1 as "all", JSON sliced the
        # last row away — I-4).
        if limit < 0 or offset < 0:
            raise ValueError("limit and offset must be >= 0")
        if order_by not in {"id", "title", "year"}:
            raise ValueError("paper order_by must be one of: id, title, year")
        if after is not None and cursor is not None:
            raise ValueError("specify only one of after or cursor")
        after = after if after is not None else cursor
        # (FIX-W W3) NFC-normalize free-text inputs so decomposed caller
        # spellings hit the composed stored text (models normalize on write).
        if author is not None:
            author = normalize_nfc(author)
        if venue is not None:
            venue = normalize_nfc(venue)
        if keyword is not None:
            keyword = normalize_nfc(keyword)
        # (FIX-W W6) Dict iteration preserves insertion order, so the JSON
        # backend already returns results in insertion order (matching the
        # sqlite backend's explicit ORDER BY rowid).
        papers = [
            _rebuild_synthetic_paper(Paper.model_validate(d))
            for d in self._papers.values()
        ]
        if author:
            # Normalized matching (F2): token-level equality tolerates middle
            # initials / casing differences (e.g. "Geoffrey Hinton" -> stored
            # "Geoffrey E. Hinton"); the substring rule is kept as before.
            papers = [
                p
                for p in papers
                if any(author_name_matches(author, a.name) for a in p.authors)
            ]
        if year is not None:
            papers = [p for p in papers if p.year == year]
        if year_from is not None:
            papers = [p for p in papers if p.year is not None and p.year >= year_from]
        if year_to is not None:
            papers = [p for p in papers if p.year is not None and p.year <= year_to]
        if venue:
            vl = venue.lower()
            papers = [p for p in papers if p.venue and vl in p.venue.lower()]
        if keyword:
            kl = keyword.lower()
            papers = [
                p
                for p in papers
                if kl in p.title.lower()
                or (p.abstract and kl in p.abstract.lower())
                or any(kl in k.lower() for k in p.keywords)
            ]
        def sort_key(paper: Paper) -> tuple[bool, str | int, str]:
            value: str | int | None
            if order_by == "title":
                value = paper.title
            elif order_by == "year":
                value = paper.year
            else:
                value = paper.id or ""
            return (value is not None, value if value is not None else "", paper.id or "")

        papers.sort(key=sort_key)
        if after is not None:
            raw_cursor = self._papers.get(after)
            if raw_cursor is None:
                raise ValueError(f"paper cursor {after!r} was not found")
            cursor_key = sort_key(Paper.model_validate(raw_cursor))
            papers = [paper for paper in papers if sort_key(paper) > cursor_key]
        return papers[offset : offset + limit]

    async def query_authors(
        self,
        *,
        name: str | None = None,
        affiliation: str | None = None,
        interest: str | None = None,
        limit: int = 100,
        offset: int = 0,
        order_by: str = "id",
        after: str | None = None,
        cursor: str | None = None,
    ) -> list[Author]:
        # (FIX-I F4) Negative pagination rejected (see query_papers).
        if limit < 0 or offset < 0:
            raise ValueError("limit and offset must be >= 0")
        if order_by not in {"id", "name"}:
            raise ValueError("author order_by must be one of: id, name")
        if after is not None and cursor is not None:
            raise ValueError("specify only one of after or cursor")
        after = after if after is not None else cursor
        # (FIX-W W3) NFC-normalize free-text inputs (see query_papers).
        if name is not None:
            name = normalize_nfc(name)
        if affiliation is not None:
            affiliation = normalize_nfc(affiliation)
        if interest is not None:
            interest = normalize_nfc(interest)
        # (FIX-W W6) Dict iteration preserves insertion order (see query_papers).
        authors = [
            _rebuild_synthetic_author(Author.model_validate(d))
            for d in self._authors.values()
        ]
        if name:
            nl = name.lower()
            authors = [a for a in authors if nl in a.name.lower()]
        if affiliation:
            al = affiliation.lower()
            authors = [
                a for a in authors if a.affiliation and al in a.affiliation.lower()
            ]
        if interest:
            il = interest.casefold()
            authors = [
                a
                for a in authors
                if any(il in normalize_nfc(i).casefold() for i in a.interests)
            ]
        def sort_key(author: Author) -> tuple[str, str]:
            value = author.name if order_by == "name" else (author.id or "")
            return (value, author.id or "")

        authors.sort(key=sort_key)
        if after is not None:
            raw_cursor = self._authors.get(after)
            if raw_cursor is None:
                raise ValueError(f"author cursor {after!r} was not found")
            cursor_key = sort_key(Author.model_validate(raw_cursor))
            authors = [author for author in authors if sort_key(author) > cursor_key]
        return authors[offset : offset + limit]

    async def get_stats(self) -> dict[str, Any]:
        return {
            "total_papers": len(self._papers),
            "total_authors": len(self._authors),
            "total_citations": len(self._citations),
            "backend": self.backend_name,
            # (FIX-AA-2) Basename only — never leak the absolute path.
            "base_path": self.base_path.name,
        }

    # ------------------------------------------------------------------
    # Incremental update metadata
    # ------------------------------------------------------------------

    async def get_paper_hash(self, paper_id: str) -> str | None:
        return self._hashes.get(paper_id)

    async def save_paper_hash(self, paper_id: str, hash: str) -> None:
        self._ensure_connected()
        async with self._lock:
            self._hashes[paper_id] = hash
            await self._persist()

    async def get_last_update_time(self, source: str) -> datetime | None:
        raw = self._source_updates.get(source)
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return None

    async def save_last_update_time(self, source: str, time: datetime) -> None:
        self._ensure_connected()
        async with self._lock:
            self._source_updates[source] = time.isoformat()
            await self._persist()

    async def get_entity_sync(
        self,
        entity_type: str,
        entity_id: str,
        source: str,
    ) -> datetime | None:
        raw = self._entity_sync.get(f"{entity_type}|{entity_id}|{source}")
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return None

    async def save_entity_sync(
        self,
        entity_type: str,
        entity_id: str,
        source: str,
        time: datetime,
    ) -> None:
        self._ensure_connected()
        async with self._lock:
            self._entity_sync[f"{entity_type}|{entity_id}|{source}"] = time.isoformat()
            await self._persist()
