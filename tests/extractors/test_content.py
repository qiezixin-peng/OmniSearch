import pymupdf

from omnisearch.extractors import extract_html, extract_pdf


def test_extracts_main_html_content_and_metadata() -> None:
    text, metadata = extract_html(
        """
        <html><head><title>Watermark Research</title></head>
        <body><nav>Menu</nav><article><h1>Method</h1>
        <p>This is a detailed primary technical explanation of the method.</p>
        <a href="https://example.com/paper">Paper</a></article></body></html>
        """
    )

    assert "Method" in text
    assert "technical explanation" in text
    assert metadata == {"title": "Watermark Research", "links_count": 1}


def test_extracts_pdf_with_page_boundaries() -> None:
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), "Audio watermark evidence")
    content = document.tobytes()
    document.close()

    text, pages, metadata = extract_pdf(content)

    assert "<!-- page 1 -->" in text
    assert pages == ["Audio watermark evidence"]
    assert metadata["page_count"] == 1
