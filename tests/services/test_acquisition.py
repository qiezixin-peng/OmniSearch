from pathlib import Path

import pytest

from omnisearch.fetchers import ContentFetcher, FetchedDocument, FetchMode, FetchRequest
from omnisearch.security import UnsafeUrlError
from omnisearch.services.acquisition import AcquisitionRequest, DocumentAcquisitionService


class FakeFetcher(ContentFetcher):
    def __init__(self, result=None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.requests: list[FetchRequest] = []

    async def fetch(self, request: FetchRequest) -> FetchedDocument:
        self.requests.append(request)
        if self.error:
            raise self.error
        return self.result


@pytest.mark.asyncio
async def test_falls_back_to_full_chrome_when_http_fetch_fails(
    tmp_path: Path, monkeypatch
) -> None:
    async def allow_url(_url: str) -> None:
        return None

    monkeypatch.setattr("omnisearch.services.acquisition.validate_public_url", allow_url)
    http = FakeFetcher(error=RuntimeError("blocked"))
    chrome = FakeFetcher(
        result=FetchedDocument(
            url="https://example.com/page",
            final_url="https://example.com/page",
            mode=FetchMode.CHROME_MANAGED,
            status_code=200,
            content_type="text/html",
            markdown="A sufficiently long extracted technical document about audio watermarking. "
            * 3,
            raw_html="<html><title>Paper</title><body><article>"
            + ("Detailed evidence. " * 20)
            + "</article></body></html>",
        )
    )
    ego = FakeFetcher(error=AssertionError("ego should not be reached"))
    service = DocumentAcquisitionService(http, chrome, ego, tmp_path)

    result = await service.fetch(AcquisitionRequest(url="https://example.com/page"))

    assert result.acquisition_mode == "chrome_managed"
    assert chrome.requests[0].solve_cloudflare is True
    assert chrome.requests[0].pierce_shadow is True
    assert result.metadata["attempts"][0].startswith("http: RuntimeError")


@pytest.mark.asyncio
async def test_parses_pdf_detected_by_content_type_without_suffix(
    tmp_path: Path, monkeypatch
) -> None:
    async def allow_url(_url: str) -> None:
        return None

    monkeypatch.setattr("omnisearch.services.acquisition.validate_public_url", allow_url)
    monkeypatch.setattr(
        "omnisearch.services.acquisition.extract_pdf",
        lambda content: ("PDF evidence", ["PDF evidence"], {"bytes": len(content)}),
    )
    http = FakeFetcher(
        result=FetchedDocument(
            url="https://example.com/download?id=1",
            final_url="https://example.com/download?id=1",
            mode=FetchMode.HTTP,
            status_code=200,
            content_type="application/pdf",
            markdown="",
            raw_bytes=b"%PDF-1.7 fake",
        )
    )
    service = DocumentAcquisitionService(
        http,
        FakeFetcher(error=AssertionError("browser should not be reached")),
        FakeFetcher(error=AssertionError("ego should not be reached")),
        tmp_path,
    )

    result = await service.fetch(
        AcquisitionRequest(url="https://example.com/download?id=1")
    )

    assert result.acquisition_mode == "pdf_http"
    assert result.pages == ["PDF evidence"]


@pytest.mark.asyncio
async def test_security_policy_failure_never_falls_back_to_browser(
    tmp_path: Path, monkeypatch
) -> None:
    async def allow_initial(_url: str) -> None:
        return None

    monkeypatch.setattr(
        "omnisearch.services.acquisition.validate_public_url", allow_initial
    )
    service = DocumentAcquisitionService(
        FakeFetcher(error=UnsafeUrlError("redirect resolves to private address")),
        FakeFetcher(error=AssertionError("unsafe redirect must not reach Chrome")),
        FakeFetcher(error=AssertionError("unsafe redirect must not reach ego-browser")),
        tmp_path,
    )

    with pytest.raises(UnsafeUrlError, match="private address"):
        await service.fetch(AcquisitionRequest(url="https://example.com/redirect"))
