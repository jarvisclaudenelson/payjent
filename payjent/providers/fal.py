from __future__ import annotations

import base64
from typing import Any, Callable
from urllib.parse import urlparse

import httpx

FAL_RUN_URL = "https://fal.run/fal-ai/fast-sdxl"
MAX_PROMPT_LENGTH = 2000
MAX_IMAGE_SIZE_BYTES = 5 * 1024 * 1024


class FalProviderNotConfigured(RuntimeError):
    pass


class FalProviderError(RuntimeError):
    pass


Transport = Callable[[dict[str, Any], str], dict[str, Any]]


def _contains_secret_like(value: Any) -> bool:
    markers = {"api_key", "apikey", "authorization", "token", "secret", "password", "cookie", "credential", "grant"}
    if isinstance(value, dict):
        return any(str(k).lower().replace("-", "_") in markers or _contains_secret_like(v) for k, v in value.items())
    if isinstance(value, list):
        return any(_contains_secret_like(v) for v in value)
    if isinstance(value, str):
        low = value.lower()
        return any(x in low for x in ("bearer ", "api_key=", "token=", "authorization:"))
    return False


def validate_image_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if _contains_secret_like(arguments):
        raise ValueError("provider credentials are not accepted in toolbox arguments")
    prompt = arguments.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt is required")
    prompt = prompt.strip()
    if len(prompt) > MAX_PROMPT_LENGTH:
        raise ValueError(f"prompt must be at most {MAX_PROMPT_LENGTH} characters")
    count = int(arguments.get("count", arguments.get("num_images", 1)) or 1)
    if count < 1 or count > 4:
        raise ValueError("count must be between 1 and 4")
    size = str(arguments.get("size", "square_hd"))
    if size not in {"square_hd", "square", "portrait_4_3", "portrait_16_9", "landscape_4_3", "landscape_16_9"}:
        raise ValueError("size is not supported")
    model = str(arguments.get("model", "fal-ai/fast-sdxl"))
    if model not in {"fal-ai/fast-sdxl", "fast-sdxl"}:
        raise ValueError("model is not supported")
    return {"prompt": prompt, "count": count, "size": size, "model": "fal-ai/fast-sdxl"}


def _default_transport(payload: dict[str, Any], api_key: str) -> dict[str, Any]:
    response = httpx.post(FAL_RUN_URL, headers={"Authorization": f"Key {api_key}", "Content-Type": "application/json"}, json={"prompt": payload["prompt"], "num_images": payload["count"], "image_size": payload["size"]}, timeout=60)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise FalProviderError("provider_execution_failed")
    return data


def _safe_https_url(url: Any) -> str | None:
    if not isinstance(url, str):
        return None
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.query:
        return None
    return parsed.geturl()[:1000]


def sanitize_fal_response(data: dict[str, Any], *, count: int) -> dict[str, Any]:
    images_in = data.get("images") if isinstance(data.get("images"), list) else []
    images = []
    for item in images_in[:count]:
        if not isinstance(item, dict):
            continue
        b64 = item.get("content_base64") or item.get("base64")
        raw = None
        if isinstance(b64, str):
            try:
                raw = base64.b64decode(b64, validate=True)
            except Exception:
                raw = None
            if raw is not None and len(raw) > MAX_IMAGE_SIZE_BYTES:
                raise FalProviderError("provider_execution_failed")
        safe = {"mime_type": str(item.get("content_type") or item.get("mime_type") or "image/png")[:100], "url": _safe_https_url(item.get("url"))}
        if raw:
            safe["content_bytes"] = raw
            safe["size_bytes"] = len(raw)
        images.append(safe)
    return {"provider": "fal", "tool_id": "fal.image.generate", "image_count": len(images), "images": images}


def run_image_generate(arguments: dict[str, Any], *, api_key: str | None, transport: Transport | None = None) -> dict[str, Any]:
    if not api_key:
        raise FalProviderNotConfigured("provider_not_configured")
    payload = validate_image_arguments(arguments)
    try:
        data = (transport or _default_transport)(payload, api_key)
    except FalProviderNotConfigured:
        raise
    except FalProviderError:
        raise
    except Exception as exc:
        raise FalProviderError("provider_execution_failed") from exc
    return sanitize_fal_response(data, count=payload["count"])
