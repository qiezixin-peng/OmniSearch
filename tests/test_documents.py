import pytest

from omnisearch.services.acquisition import AcquiredDocument
from omnisearch.storage.database import Database
from omnisearch.storage.documents import DocumentRepository
from omnisearch.storage.objects import LocalObjectStore


def test_versions_and_chunks_acquired_documents(tmp_path) -> None:
    database = Database(f"sqlite:///{(tmp_path / 'documents.db').as_posix()}")
    database.create_schema()
    repository = DocumentRepository(
        database,
        chunk_size=20,
        overlap=5,
        object_store=LocalObjectStore(tmp_path / "objects"),
    )
    document = AcquiredDocument(
        url="https://example.com/article",
        final_url="https://example.com/article",
        content_type="text/html",
        acquisition_mode="http",
        text="A technical document with enough content to produce several overlapping chunks.",
    )

    first = repository.store(document)
    second = repository.store(document)

    assert first.chunks > 1
    assert first.object_uri is not None
    assert second.document_id == first.document_id
    assert second.version_id == first.version_id
    assert second.reused_version is True
    restored = repository.get(first.document_id)
    assert restored["text"] == document.text
    assert restored["object_uri"].startswith("file:")
    assert len(restored["chunks"]) == first.chunks
    matches = repository.search_chunks("technical overlapping", limit=5)
    assert matches
    assert matches[0]["document_id"] == str(first.document_id)
    assert {"technical", "overlapping"} <= {
        term for match in matches for term in match["matched_terms"]
    }

    with pytest.raises(ValueError, match="searchable term"):
        repository.search_chunks("!")
