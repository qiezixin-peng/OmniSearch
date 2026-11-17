from typing import Any

import pymupdf


def extract_pdf(content: bytes) -> tuple[str, list[str], dict[str, Any]]:
    with pymupdf.open(stream=content, filetype="pdf") as document:
        pages = [page.get_text("text").strip() for page in document]
        metadata = {
            "page_count": document.page_count,
            "pdf_metadata": {key: value for key, value in document.metadata.items() if value},
        }
    text = "\n\n".join(
        f"<!-- page {number} -->\n{page}"
        for number, page in enumerate(pages, start=1)
        if page
    )
    return text, pages, metadata
