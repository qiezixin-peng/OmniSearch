import hashlib
import re
from datetime import UTC, datetime
from uuid import UUID, uuid4

from pydantic import Field
from sqlalchemy import func, or_, select

from omnisearch.domain.models import StrictModel
from omnisearch.services.acquisition import AcquiredDocument
from omnisearch.storage.database import Database
from omnisearch.storage.objects import ObjectStore
from omnisearch.storage.tables import (
    DocumentChunkRow,
    DocumentRow,
    DocumentVersionRow,
    ResultDocumentRow,
)


class StoredDocument(StrictModel):
    document_id: UUID
    version_id: UUID
    content_hash: str
    chunks: int = Field(ge=0)
    reused_version: bool = False
    object_uri: str | None = None


class DocumentRepository:
    def __init__(
        self,
        database: Database,
        *,
        chunk_size: int = 4000,
        overlap: int = 400,
        object_store: ObjectStore | None = None,
    ):
        self.database = database
        self.chunk_size = chunk_size
        self.overlap = overlap
        self.object_store = object_store

    def store(
        self,
        document: AcquiredDocument,
        *,
        research_id: UUID | None = None,
        result_id: UUID | None = None,
    ) -> StoredDocument:
        if (research_id is None) != (result_id is None):
            raise ValueError("research_id and result_id must be provided together")
        content_hash = hashlib.sha256(document.text.encode("utf-8")).hexdigest()
        canonical_url = str(document.final_url)
        with self.database.session_factory.begin() as session:
            row = session.scalar(
                select(DocumentRow).where(DocumentRow.canonical_url == canonical_url)
            )
            if row is None:
                row = DocumentRow(
                    id=str(uuid4()), canonical_url=canonical_url, created_at=datetime.now(UTC)
                )
                session.add(row)
                session.flush()
            existing = session.scalar(
                select(DocumentVersionRow).where(
                    DocumentVersionRow.document_id == row.id,
                    DocumentVersionRow.content_hash == content_hash,
                )
            )
            if existing is not None:
                count = session.scalar(
                    select(func.count(DocumentChunkRow.id)).where(
                        DocumentChunkRow.version_id == existing.id
                    )
                )
                self._link_result(
                    session,
                    research_id,
                    result_id,
                    row.id,
                    existing.id,
                )
                return StoredDocument(
                    document_id=UUID(row.id),
                    version_id=UUID(existing.id),
                    content_hash=content_hash,
                    chunks=int(count or 0),
                    reused_version=True,
                    object_uri=existing.object_uri,
                )
            version_id = uuid4()
            object_uri = None
            if self.object_store is not None:
                object_uri = self.object_store.put_text(
                    f"documents/{row.id}/{content_hash}.md",
                    document.text,
                    "text/markdown; charset=utf-8",
                )
            session.add(
                DocumentVersionRow(
                    id=str(version_id),
                    document_id=row.id,
                    content_hash=content_hash,
                    final_url=canonical_url,
                    content_type=document.content_type,
                    acquisition_mode=document.acquisition_mode,
                    object_uri=object_uri,
                    text=document.text,
                    pages=document.pages,
                    metadata_json=document.metadata,
                    fetched_at=datetime.now(UTC),
                )
            )
            session.flush()
            chunks = self._chunks(document.text)
            session.add_all(
                DocumentChunkRow(
                    id=str(uuid4()),
                    version_id=str(version_id),
                    ordinal=ordinal,
                    text=text,
                    start_offset=start,
                    end_offset=end,
                )
                for ordinal, (text, start, end) in enumerate(chunks)
            )
            self._link_result(
                session,
                research_id,
                result_id,
                row.id,
                str(version_id),
            )
            return StoredDocument(
                document_id=UUID(row.id),
                version_id=version_id,
                content_hash=content_hash,
                chunks=len(chunks),
                object_uri=object_uri,
            )

    @staticmethod
    def _link_result(
        session,
        research_id: UUID | None,
        result_id: UUID | None,
        document_id: str,
        version_id: str,
    ) -> None:
        if research_id is None or result_id is None:
            return
        key = (str(research_id), str(result_id))
        link = session.get(ResultDocumentRow, key)
        if link is None:
            session.add(
                ResultDocumentRow(
                    research_id=key[0],
                    result_id=key[1],
                    document_id=document_id,
                    version_id=version_id,
                    acquired_at=datetime.now(UTC),
                )
            )
        else:
            link.document_id = document_id
            link.version_id = version_id
            link.acquired_at = datetime.now(UTC)

    def _chunks(self, text: str) -> list[tuple[str, int, int]]:
        if not text:
            return []
        step = max(1, self.chunk_size - self.overlap)
        return [
            (text[start : start + self.chunk_size], start, min(len(text), start + self.chunk_size))
            for start in range(0, len(text), step)
        ]

    def get(self, document_id: UUID) -> dict:
        with self.database.session_factory() as session:
            document = session.get(DocumentRow, str(document_id))
            if document is None:
                raise LookupError(f"document not found: {document_id}")
            version = session.scalar(
                select(DocumentVersionRow)
                .where(DocumentVersionRow.document_id == document.id)
                .order_by(DocumentVersionRow.fetched_at.desc())
                .limit(1)
            )
            if version is None:
                raise LookupError(f"document has no versions: {document_id}")
            chunks = session.scalars(
                select(DocumentChunkRow)
                .where(DocumentChunkRow.version_id == version.id)
                .order_by(DocumentChunkRow.ordinal)
            ).all()
            return {
                "document_id": document.id,
                "version_id": version.id,
                "url": document.canonical_url,
                "content_hash": version.content_hash,
                "content_type": version.content_type,
                "acquisition_mode": version.acquisition_mode,
                "object_uri": version.object_uri,
                "text": version.text,
                "pages": version.pages,
                "metadata": version.metadata_json,
                "fetched_at": version.fetched_at.isoformat(),
                "chunks": [
                    {
                        "ordinal": chunk.ordinal,
                        "text": chunk.text,
                        "start_offset": chunk.start_offset,
                        "end_offset": chunk.end_offset,
                    }
                    for chunk in chunks
                ],
            }

    def evidence_for_research(self, research_id: UUID) -> dict[str, dict]:
        with self.database.session_factory() as session:
            rows = session.execute(
                select(ResultDocumentRow, DocumentRow, DocumentVersionRow)
                .join(DocumentRow, DocumentRow.id == ResultDocumentRow.document_id)
                .join(
                    DocumentVersionRow,
                    DocumentVersionRow.id == ResultDocumentRow.version_id,
                )
                .where(ResultDocumentRow.research_id == str(research_id))
            ).all()
            return {
                link.result_id: {
                    "document_id": document.id,
                    "version_id": version.id,
                    "url": document.canonical_url,
                    "content_length": len(version.text),
                    "excerpt": version.text[:1200],
                    "content_type": version.content_type,
                    "acquisition_mode": version.acquisition_mode,
                    "object_uri": version.object_uri,
                    "fetched_at": version.fetched_at.isoformat(),
                    "metadata": version.metadata_json,
                }
                for link, document, version in rows
            }

    def search_chunks(
        self,
        query: str,
        *,
        research_id: UUID | None = None,
        limit: int = 20,
    ) -> list[dict]:
        tokens = list(
            dict.fromkeys(
                token.lower()
                for token in re.findall(r"[\w\-]{2,}", query, flags=re.UNICODE)
            )
        )[:12]
        if not tokens:
            raise ValueError("query must contain at least one searchable term")
        with self.database.session_factory() as session:
            statement = (
                select(DocumentChunkRow, DocumentVersionRow, DocumentRow)
                .join(
                    DocumentVersionRow,
                    DocumentVersionRow.id == DocumentChunkRow.version_id,
                )
                .join(DocumentRow, DocumentRow.id == DocumentVersionRow.document_id)
                .where(
                    or_(
                        *(func.lower(DocumentChunkRow.text).contains(token) for token in tokens)
                    )
                )
            )
            if research_id is not None:
                version_ids = select(ResultDocumentRow.version_id).where(
                    ResultDocumentRow.research_id == str(research_id)
                )
                statement = statement.where(DocumentVersionRow.id.in_(version_ids))
            rows = session.execute(statement.limit(500)).all()
            matches = []
            for chunk, version, document in rows:
                lowered = chunk.text.lower()
                matched = [token for token in tokens if token in lowered]
                occurrences = sum(lowered.count(token) for token in matched)
                matches.append(
                    {
                        "document_id": document.id,
                        "version_id": version.id,
                        "url": document.canonical_url,
                        "ordinal": chunk.ordinal,
                        "start_offset": chunk.start_offset,
                        "end_offset": chunk.end_offset,
                        "text": chunk.text,
                        "matched_terms": matched,
                        "score": len(matched) * 10 + occurrences,
                    }
                )
            matches.sort(key=lambda item: (-item["score"], item["url"], item["ordinal"]))
            return matches[: max(1, min(limit, 100))]
