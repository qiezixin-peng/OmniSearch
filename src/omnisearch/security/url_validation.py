import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

import httpx


class UnsafeUrlError(ValueError):
    pass


async def validate_public_url(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise UnsafeUrlError("only public HTTP(S) URLs are allowed")
    try:
        literal = ipaddress.ip_address(parts.hostname)
    except ValueError:
        literal = None
    if literal is not None:
        if not literal.is_global:
            raise UnsafeUrlError(f"URL uses a non-public address: {literal}")
        return
    try:
        addresses = await asyncio.to_thread(
            socket.getaddrinfo,
            parts.hostname,
            parts.port or (443 if parts.scheme == "https" else 80),
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror as error:
        raise UnsafeUrlError(f"URL host could not be resolved: {parts.hostname}") from error
    resolved = {item[4][0] for item in addresses}
    if resolved and all(_is_fake_proxy_ip(address) for address in resolved):
        resolved = await _resolve_doh(parts.hostname)
    for address in resolved:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            raise UnsafeUrlError(f"URL resolves to a non-public address: {address}")


def _is_fake_proxy_ip(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    return ip in ipaddress.ip_network("198.18.0.0/15")


async def _resolve_doh(hostname: str) -> set[str]:
    try:
        async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
            response = await client.get(
                "https://cloudflare-dns.com/dns-query",
                params={"name": hostname, "type": "A"},
                headers={"Accept": "application/dns-json"},
            )
            response.raise_for_status()
            answers = response.json().get("Answer", [])
    except (httpx.HTTPError, ValueError) as error:
        raise UnsafeUrlError("public DNS verification failed for proxy Fake-IP") from error
    resolved = {
        answer["data"]
        for answer in answers
        if answer.get("type") == 1 and answer.get("data")
    }
    if not resolved:
        raise UnsafeUrlError("public DNS returned no A record for proxy Fake-IP")
    return resolved
