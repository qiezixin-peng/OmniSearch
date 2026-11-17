from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import trafilatura
from selectolax.parser import HTMLParser


def extract_html(raw_html: str, fallback_markdown: str = "") -> tuple[str, dict[str, Any]]:
    tree = HTMLParser(raw_html)
    title_node = tree.css_first("title")
    extracted = trafilatura.extract(
        raw_html,
        output_format="markdown",
        include_links=True,
        include_tables=True,
        include_images=False,
        favor_recall=True,
    )
    body_text = tree.body.text(separator="\n") if tree.body else tree.text(separator="\n")
    text = (extracted or fallback_markdown or body_text).strip()
    return text, {
        "title": title_node.text(strip=True) if title_node else None,
        "links_count": len(tree.css("a[href]")),
    }


def extract_links(raw_html: str, base_url: str, *, limit: int = 500) -> list[str]:
    """Return canonical public-web candidates without fragments or tracking parameters."""
    tree = HTMLParser(raw_html)
    links: list[str] = []
    seen: set[str] = set()
    for node in tree.css("a[href]"):
        href = (node.attributes.get("href") or "").strip()
        if not href:
            continue
        absolute = urljoin(base_url, href)
        parts = urlsplit(absolute)
        if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
            continue
        host = parts.hostname.lower()
        port = parts.port
        if port and not (parts.scheme.lower() == "http" and port == 80) and not (
            parts.scheme.lower() == "https" and port == 443
        ):
            host = f"{host}:{port}"
        query = urlencode(
            [
                (key, value)
                for key, value in parse_qsl(parts.query, keep_blank_values=True)
                if not key.lower().startswith("utm_")
                and key.lower() not in {"fbclid", "gclid", "ref", "source"}
            ],
            doseq=True,
        )
        normalized = urlunsplit(
            (parts.scheme.lower(), host, parts.path or "/", query, "")
        )
        if normalized in seen:
            continue
        seen.add(normalized)
        links.append(normalized)
        if len(links) >= limit:
            break
    return links
