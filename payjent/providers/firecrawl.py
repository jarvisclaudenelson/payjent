from __future__ import annotations

import ipaddress
from typing import Any, Callable
from urllib.parse import urlparse, urlunparse

import httpx

FIRECRAWL_SCRAPE_URL = "https://api.firecrawl.dev/v2/scrape"
MAX_CONTENT_LENGTH = 4000
MAX_LINKS = 25
ALLOWED_FORMATS = {"markdown", "html", "links"}


class FirecrawlProviderNotConfigured(RuntimeError):
    pass


class FirecrawlProviderError(RuntimeError):
    pass


Transport = Callable[[dict[str, Any], str], dict[str, Any]]


def _is_secret_like_key(key: Any) -> bool:
    normalized = str(key).lower().replace("-", "_")
    return any(marker in normalized for marker in ("api_key", "apikey", "authorization", "cookie", "token", "secret", "password", "credential", "private_key"))


def _contains_secret_like_key(value: Any) -> bool:
    if isinstance(value, dict):
        return any(_is_secret_like_key(key) or _contains_secret_like_key(nested) for key, nested in value.items())
    if isinstance(value, list):
        return any(_contains_secret_like_key(item) for item in value)
    return False


def _validate_public_https_url(raw_url: Any) -> tuple[str, dict[str, str]]:
    if isinstance(raw_url, dict):
        raw_url = raw_url.get("canonical_url") or raw_url.get("url")
    if not isinstance(raw_url, str) or not raw_url.strip():
        raise ValueError("url is required")
    parsed = urlparse(raw_url.strip())
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise ValueError("url must be a public HTTPS URL")
    host = parsed.hostname.lower().rstrip(".")
    if host in {"localhost", "metadata.google.internal"} or host.endswith(".local"):
        raise ValueError("url must be a public HTTPS URL")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            raise ValueError("url must be a public HTTPS URL")
    netloc = host
    if parsed.port:
        netloc = f"{host}:{parsed.port}"
    canonical = urlunparse(("https", netloc, parsed.path or "/", "", parsed.query, ""))
    return canonical, {"scheme": "https", "host": host}


def validate_scrape_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if _contains_secret_like_key(arguments):
        raise ValueError("provider credentials are not accepted in toolbox arguments")
    url, url_summary = _validate_public_https_url(arguments.get("url"))
    raw_formats = arguments.get("formats", ["markdown"])
    if isinstance(raw_formats, str):
        raw_formats = [raw_formats]
    if not isinstance(raw_formats, list) or not raw_formats:
        raise ValueError("formats must be a non-empty list")
    formats: list[str] = []
    for item in raw_formats:
        if not isinstance(item, str):
            raise ValueError("formats must contain strings")
        fmt = item.strip().lower()
        if fmt not in ALLOWED_FORMATS:
            raise ValueError("formats contains an unsupported value")
        if fmt not in formats:
            formats.append(fmt)
    only_main_content = arguments.get("only_main_content", True)
    if not isinstance(only_main_content, bool):
        raise ValueError("only_main_content must be a boolean")
    return {"url": url, "url_summary": url_summary, "formats": formats, "only_main_content": only_main_content}


def _default_transport(payload: dict[str, Any], api_key: str) -> dict[str, Any]:
    response = httpx.post(
        FIRECRAWL_SCRAPE_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={"url": payload["url"], "formats": payload["formats"], "onlyMainContent": payload["only_main_content"]},
        timeout=45,
    )
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise FirecrawlProviderError("provider returned an invalid response")
    return data


def _safe_str(value: Any, max_len: int) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text[:max_len]


def _safe_links(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    links: list[str] = []
    for item in value[:MAX_LINKS]:
        text = _safe_str(item, 1000)
        if text and text.startswith("https://"):
            links.append(text)
    return links


def sanitize_firecrawl_response(data: dict[str, Any], *, url_summary: dict[str, str]) -> dict[str, Any]:
    body = data.get("data") if isinstance(data.get("data"), dict) else data
    result: dict[str, Any] = {"provider": "firecrawl", "tool_id": "firecrawl.scrape", "url_summary": url_summary}
    for source, target in (("markdown", "markdown"), ("html", "html"), ("text", "text")):
        value = _safe_str(body.get(source), MAX_CONTENT_LENGTH)
        if value is not None:
            result[target] = value
    links = _safe_links(body.get("links"))
    if links:
        result["links"] = links
    metadata = body.get("metadata") if isinstance(body.get("metadata"), dict) else {}
    safe_metadata: dict[str, Any] = {}
    for key in ("title", "description", "language", "statusCode", "sourceURL"):
        value = metadata.get(key)
        if key == "sourceURL":
            continue
        safe = _safe_str(value, 500)
        if safe is not None:
            safe_metadata["status_code" if key == "statusCode" else key] = safe
    if safe_metadata:
        result["metadata"] = safe_metadata
    return result


def run_scrape(arguments: dict[str, Any], *, api_key: str | None, transport: Transport | None = None) -> dict[str, Any]:
    if not api_key:
        raise FirecrawlProviderNotConfigured("provider_not_configured")
    payload = validate_scrape_arguments(arguments)
    try:
        data = (transport or _default_transport)(payload, api_key)
    except FirecrawlProviderNotConfigured:
        raise
    except Exception as exc:
        raise FirecrawlProviderError("provider_execution_failed") from exc
    return sanitize_firecrawl_response(data, url_summary=payload["url_summary"])
