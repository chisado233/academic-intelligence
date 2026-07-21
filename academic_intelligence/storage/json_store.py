"""JSON file storage backend for Academic Intelligence.

Provides simple file-based storage for Author, Paper, and Citation records
using JSON files. Suitable for small datasets and prototyping.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from academic_intelligence.core.exceptions import StorageError
from academic_intelligence.core.models import Author, Citation, Paper
from academic_intelligence.storage.base import BaseStorage


def _new_id() -> str:
    return uuid.uuid4().hex


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
        self._papers: Dict[str, dict] = {}
        self._authors: Dict[str, dict] = {}
        self._citations: Dict[str, dict] = {}
        self._lock = asyncio.Lock()
        self._connected = False

    def _load_file(self, path: Path) -> Dict[str, dict]:
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
            return {}
        except Exception as exc:
            raise StorageError(
                f"Failed to load {path}: {exc}",
                backend=self.backend_name,
            ) from exc

    def _save_file(self, path: Path, data: Dict[str, dict]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )

    async def connect(self) -> None:
        """Create directory and load existing data."""
        try:
            self.base_path.mkdir(parents=True, exist_ok=True)
            self._papers = self._load_file(self._papers_file)
            self._authors = self._load_file(self._authors_file)
            self._citations = self._load_file(self._citations_file)
            self._connected = True
        except StorageError:
            raise
        except Exception as exc:
            raise StorageError(
                f"Failed to connect JSON storage: {exc}",
                backend=self.backend_name,
            ) from exc

    async def close(self) -> None:
        """Persist data to disk."""
        async with self._lock:
            try:
                self._save_file(self._papers_file, self._papers)
                self._save_file(self._authors_file, self._authors)
                self._save_file(self._citations_file, self._citations)
                self._connected = False
            except Exception as exc:
                raise StorageError(
                    f"Failed to close/persist JSON storage: {exc}",
                    backend=self.backend_name,
                ) from exc

    async def _persist(self) -> None:
        self._save_file(self._papers_file, self._papers)
        self._save_file(self._authors_file, self._authors)
        self._save_file(self._citations_file, self._citations)

    async def save_paper(self, paper: Paper) -> str:
        async with self._lock:
            paper_id = paper.id or _new_id()
            payload = paper.model_dump(mode="json")
            payload["id"] = paper_id
            self._papers[paper_id] = payload
            await self._persist()
            return paper_id

    async def get_paper(self, paper_id: str) -> Optional[Paper]:
        data = self._papers.get(paper_id)
        return Paper.model_validate(data) if data else None

    async def update_paper(self, paper_id: str, paper: Paper) -> bool:
        async with self._lock:
            if paper_id not in self._papers:
                return False
            payload = paper.model_dump(mode="json")
            payload["id"] = paper_id
            self._papers[paper_id] = payload
            await self._persist()
            return True

    async def delete_paper(self, paper_id: str) -> bool:
        async with self._lock:
            if paper_id not in self._papers:
                return False
            del self._papers[paper_id]
            await self._persist()
            return True

    async def save_author(self, author: Author) -> str:
        async with self._lock:
            author_id = author.id or _new_id()
            payload = author.model_dump(mode="json")
            payload["id"] = author_id
            self._authors[author_id] = payload
            await self._persist()
            return author_id

    async def get_author(self, author_id: str) -> Optional[Author]:
        data = self._authors.get(author_id)
        return Author.model_validate(data) if data else None

    async def update_author(self, author_id: str, author: Author) -> bool:
        async with self._lock:
            if author_id not in self._authors:
                return False
            payload = author.model_dump(mode="json")
            payload["id"] = author_id
            self._authors[author_id] = payload
            await self._persist()
            return True

    async def delete_author(self, author_id: str) -> bool:
        async with self._lock:
            if author_id not in self._authors:
                return False
            del self._authors[author_id]
            await self._persist()
            return True

    async def save_citation(self, citation: Citation) -> str:
        async with self._lock:
            citation_id = _new_id()
            payload = citation.model_dump(mode="json")
            payload["id"] = citation_id
            self._citations[citation_id] = payload
            await self._persist()
            return citation_id

    async def get_citations_by_paper(
        self,
        paper_id: str,
        *,
        direction: str = "outgoing",
    ) -> List[Citation]:
        results: List[Citation] = []
        for data in self._citations.values():
            if direction == "incoming":
                if data.get("cited_paper_id") == paper_id:
                    results.append(Citation.model_validate(data))
            else:
                if data.get("citing_paper_id") == paper_id:
                    results.append(Citation.model_validate(data))
        return results

    async def save_batch(
        self,
        *,
        authors: Optional[List[Author]] = None,
        papers: Optional[List[Paper]] = None,
        citations: Optional[List[Citation]] = None,
    ) -> Dict[str, List[str]]:
        ids: Dict[str, List[str]] = {"authors": [], "papers": [], "citations": []}
        async with self._lock:
            for author in authors or []:
                author_id = author.id or _new_id()
                payload = author.model_dump(mode="json")
                payload["id"] = author_id
                self._authors[author_id] = payload
                ids["authors"].append(author_id)
            for paper in papers or []:
                paper_id = paper.id or _new_id()
                payload = paper.model_dump(mode="json")
                payload["id"] = paper_id
                self._papers[paper_id] = payload
                ids["papers"].append(paper_id)
            for citation in citations or []:
                citation_id = _new_id()
                payload = citation.model_dump(mode="json")
                payload["id"] = citation_id
                self._citations[citation_id] = payload
                ids["citations"].append(citation_id)
            await self._persist()
        return ids

    async def query_papers(
        self,
        *,
        author: Optional[str] = None,
        year: Optional[int] = None,
        year_from: Optional[int] = None,
        year_to: Optional[int] = None,
        venue: Optional[str] = None,
        keyword: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Paper]:
        papers = [Paper.model_validate(d) for d in self._papers.values()]
        if author:
            al = author.lower()
            papers = [p for p in papers if any(al in a.lower() for a in p.authors)]
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
        return papers[offset : offset + limit]

    async def query_authors(
        self,
        *,
        name: Optional[str] = None,
        affiliation: Optional[str] = None,
        interest: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Author]:
        authors = [Author.model_validate(d) for d in self._authors.values()]
        if name:
            nl = name.lower()
            authors = [a for a in authors if nl in a.name.lower()]
        if affiliation:
            al = affiliation.lower()
            authors = [
                a for a in authors if a.affiliation and al in a.affiliation.lower()
            ]
        if interest:
            il = interest.lower()
            authors = [
                a for a in authors if any(il in i.lower() for i in a.interests)
            ]
        return authors[offset : offset + limit]

    async def get_stats(self) -> Dict[str, Any]:
        return {
            "total_papers": len(self._papers),
            "total_authors": len(self._authors),
            "total_citations": len(self._citations),
            "backend": self.backend_name,
            "base_path": str(self.base_path),
        }
