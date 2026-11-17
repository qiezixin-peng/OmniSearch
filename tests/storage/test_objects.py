import pytest

from omnisearch.storage.objects import LocalObjectStore


def test_local_object_store_rejects_traversal_and_writes_utf8(tmp_path) -> None:
    store = LocalObjectStore(tmp_path / "objects")

    uri = store.put_text("documents/one.md", "中文 evidence", "text/markdown")

    assert (tmp_path / "objects" / "documents" / "one.md").read_text(
        encoding="utf-8"
    ) == "中文 evidence"
    assert uri.startswith("file:")
    with pytest.raises(ValueError):
        store.put_text("../escape.md", "blocked", "text/plain")
