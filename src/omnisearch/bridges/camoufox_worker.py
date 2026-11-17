"""Small stdlib-only bridge executed by the isolated Camoufox environment."""

import asyncio
import json
import sys
from contextlib import suppress
from typing import Any
from urllib.parse import urlparse

RESULT_MARKER = "OMNISEARCH_CAMOUFOX_RESULT:"


async def resolve_google_redirects(context: Any, page: Any) -> None:
    if urlparse(page.url).hostname not in {"google.com", "www.google.com"}:
        return
    links = await page.locator('a[href^="/goto?"]').evaluate_all(
        "anchors => [...new Set(anchors.map(anchor => anchor.getAttribute('href'))) ].slice(0, 40)"
    )

    async def resolve(href: str) -> tuple[str, str] | None:
        try:
            response = await context.request.get(
                "https://www.google.com" + href,
                timeout=6_000,
                max_redirects=0,
            )
            location = response.headers.get("location", "")
            await response.dispose()
            if urlparse(location).scheme in {"http", "https"}:
                return href, location
        except Exception:
            return None
        return None

    resolved = await asyncio.gather(*(resolve(href) for href in links))
    mapping = dict(item for item in resolved if item is not None)
    if mapping:
        await page.locator('a[href^="/goto?"]').evaluate_all(
            "(anchors, mapping) => anchors.forEach(anchor => {"
            "const next = mapping[anchor.getAttribute('href')];"
            "if (next) anchor.setAttribute('href', next);"
            "})",
            mapping,
        )


async def run(payload: dict[str, Any]) -> dict[str, Any]:
    from camoufox.async_api import AsyncCamoufox

    options: dict[str, Any] = {
        "headless": payload.get("headless", True),
        "humanize": True,
        "os": "windows",
        "persistent_context": True,
        "user_data_dir": payload["profile"],
        "block_images": payload.get("block_images", False),
        "block_webrtc": payload.get("block_webrtc", False),
    }
    if payload.get("locale"):
        options["locale"] = payload["locale"]
    if payload.get("proxy"):
        options["proxy"] = payload["proxy"]
    async with AsyncCamoufox(**options) as context:
        page = context.pages[0] if context.pages else await context.new_page()
        response = await page.goto(
            payload["url"],
            wait_until="domcontentloaded",
            timeout=payload["timeout_ms"],
        )
        if payload.get("wait_selector"):
            await page.wait_for_selector(
                payload["wait_selector"], timeout=payload["timeout_ms"]
            )
        if payload.get("network_idle"):
            with suppress(Exception):
                await page.wait_for_load_state(
                    "networkidle", timeout=min(payload["timeout_ms"], 10_000)
                )
        if payload.get("wait_after_load_ms"):
            await page.wait_for_timeout(payload["wait_after_load_ms"])
        await resolve_google_redirects(context, page)
        try:
            text = await page.locator("body").inner_text(timeout=5_000)
        except Exception:
            text = ""
        headers = await response.all_headers() if response else {}
        return {
            "final_url": page.url,
            "status_code": response.status if response else 200,
            "content_type": headers.get("content-type"),
            "text": text,
            "html": await page.content(),
        }


def main() -> None:
    payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    result = asyncio.run(run(payload))
    encoded = (RESULT_MARKER + json.dumps(result, ensure_ascii=False) + "\n").encode("utf-8")
    sys.stdout.buffer.write(encoded)


if __name__ == "__main__":
    main()






