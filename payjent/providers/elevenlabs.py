from __future__ import annotations

import re
from typing import Any, Callable

import httpx

ELEVENLABS_TTS_URL_TEMPLATE = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
DEFAULT_VOICE_ID = "21m00Tcm4TlvDq8ikWAM"  # ElevenLabs public Rachel voice id
MAX_TEXT_LENGTH = 5000
MAX_AUDIO_SIZE_BYTES = 25 * 1024 * 1024
METADATA_ONLY_AUDIO_CAVEAT = "metadata_only_audio_not_stored"
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


class ProviderNotConfigured(RuntimeError):
    pass


class ProviderError(RuntimeError):
    pass


ElevenLabsProviderNotConfigured = ProviderNotConfigured
ElevenLabsProviderError = ProviderError


Transport = Callable[[dict[str, Any], str], dict[str, Any]]


def _is_secret_like_key(key: Any) -> bool:
    normalized = str(key).lower().replace("-", "_")
    return any(marker in normalized for marker in ("api_key", "apikey", "authorization", "cookie", "token", "secret", "password", "credential", "private_key", "xi_api_key"))


def _is_secret_like_value(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    normalized = value.strip().lower()
    if not normalized:
        return False
    return any(marker in normalized for marker in ("bearer ", "api_key=", "apikey=", "xi-api-key", "authorization:", "sk-", "secret", "password", "token="))


def _contains_secret_like(value: Any) -> bool:
    if isinstance(value, dict):
        return any(_is_secret_like_key(key) or _contains_secret_like(nested) for key, nested in value.items())
    if isinstance(value, list):
        return any(_contains_secret_like(item) for item in value)
    return _is_secret_like_value(value)


def _safe_optional_id(value: Any, *, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    text = value.strip()
    if not _SAFE_ID_RE.fullmatch(text):
        raise ValueError(f"{field} contains unsupported characters")
    return text


def validate_text_to_speech_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if _contains_secret_like(arguments):
        raise ValueError("provider credentials are not accepted in toolbox arguments")
    text = arguments.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("text is required")
    text = text.strip()
    if len(text) > MAX_TEXT_LENGTH:
        raise ValueError(f"text must be at most {MAX_TEXT_LENGTH} characters")
    voice_id = _safe_optional_id(arguments.get("voice_id") or arguments.get("voice"), field="voice_id") or DEFAULT_VOICE_ID
    model_id = _safe_optional_id(arguments.get("model_id"), field="model_id") or "eleven_multilingual_v2"
    return {"text": text, "voice_id": voice_id, "model_id": model_id}


def _default_transport(payload: dict[str, Any], api_key: str) -> dict[str, Any]:
    audio_size_bytes = 0
    with httpx.stream(
        "POST",
        ELEVENLABS_TTS_URL_TEMPLATE.format(voice_id=payload["voice_id"]),
        headers={"xi-api-key": api_key, "Content-Type": "application/json", "Accept": "audio/mpeg"},
        json={"text": payload["text"], "model_id": payload["model_id"]},
        timeout=60,
    ) as response:
        response.raise_for_status()
        content_type = response.headers.get("content-type") or "application/octet-stream"
        _validate_audio_content_type(content_type)
        for chunk in response.iter_bytes():
            audio_size_bytes += len(chunk)
            if audio_size_bytes > MAX_AUDIO_SIZE_BYTES:
                raise ElevenLabsProviderError("provider_execution_failed")
        if audio_size_bytes <= 0:
            raise ElevenLabsProviderError("provider_execution_failed")
        return {
            "content_type": content_type,
            "audio_size_bytes": audio_size_bytes,
            "request_id": response.headers.get("request-id") or response.headers.get("x-request-id"),
        }


def _safe_str(value: Any, max_len: int) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text[:max_len]


def _normalized_content_type(value: Any) -> str | None:
    text = _safe_str(value, 100)
    if not text:
        return None
    return text.split(";", 1)[0].strip().lower() or None


def _is_allowed_audio_content_type(value: Any) -> bool:
    content_type = _normalized_content_type(value)
    return bool(content_type and (content_type.startswith("audio/") or content_type == "application/octet-stream"))


def _validate_audio_content_type(value: Any) -> None:
    if not _is_allowed_audio_content_type(value):
        raise ElevenLabsProviderError("provider_execution_failed")


def sanitize_elevenlabs_response(data: dict[str, Any], *, payload: dict[str, Any]) -> dict[str, Any]:
    size = data.get("audio_size_bytes")
    if not isinstance(size, int) or size < 0:
        raw_audio = data.get("audio") or data.get("content")
        if isinstance(raw_audio, (bytes, bytearray)):
            size = len(raw_audio)
        else:
            size = 0
    if size <= 0 or size > MAX_AUDIO_SIZE_BYTES:
        raise ElevenLabsProviderError("provider_execution_failed")
    content_type = _safe_str(data.get("content_type"), 100) or "audio/mpeg"
    _validate_audio_content_type(content_type)
    result: dict[str, Any] = {
        "provider": "elevenlabs",
        "tool_id": "elevenlabs.text_to_speech",
        "voice_id": payload["voice_id"],
        "model_id": payload["model_id"],
        "content_type": content_type,
        "audio_size_bytes": size,
        "delivery_mode": "metadata_only",
        "result_caveat": METADATA_ONLY_AUDIO_CAVEAT,
    }
    request_id = _safe_str(data.get("request_id"), 64)
    if request_id:
        result["provider_request_id"] = request_id
    return result


def run_text_to_speech(arguments: dict[str, Any], *, api_key: str | None, transport: Transport | None = None) -> dict[str, Any]:
    if not api_key:
        raise ElevenLabsProviderNotConfigured("provider_not_configured")
    payload = validate_text_to_speech_arguments(arguments)
    try:
        data = (transport or _default_transport)(payload, api_key)
        if not isinstance(data, dict):
            raise ElevenLabsProviderError("provider returned an invalid response")
        return sanitize_elevenlabs_response(data, payload=payload)
    except ElevenLabsProviderNotConfigured:
        raise
    except ElevenLabsProviderError:
        raise
    except Exception as exc:
        raise ElevenLabsProviderError("provider_execution_failed") from exc
