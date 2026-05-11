from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from fastapi import HTTPException

from payjent.config import Settings
from payjent.models import PaymentSession, Quote


class DecalCheckoutClient(Protocol):
    def create_checkout_session(self, payload: dict[str, Any], idempotency_key: str) -> dict[str, Any]: ...
    def retrieve_checkout_session(self, session_id: str) -> dict[str, Any]: ...


@dataclass
class DecalHTTPClient:
    api_key: str
    base_url: str = "https://api.usedecal.com"

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    def create_checkout_session(self, payload: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
        headers = self._headers() | {"Idempotency-Key": idempotency_key}
        with httpx.Client(timeout=10.0) as client:
            response = client.post(f"{self.base_url.rstrip('/')}/v0/checkout/sessions", json=payload, headers=headers)
            response.raise_for_status()
            return response.json()

    def retrieve_checkout_session(self, session_id: str) -> dict[str, Any]:
        with httpx.Client(timeout=10.0) as client:
            response = client.get(f"{self.base_url.rstrip('/')}/v0/checkout/sessions/{session_id}", headers=self._headers())
            response.raise_for_status()
            return response.json()


def _safe_decal_error(exc: Exception, action: str) -> str:
    response = getattr(exc, "response", None) if isinstance(exc, httpx.HTTPError) else None
    status = getattr(response, "status_code", None)
    body = ""
    if response is not None:
        try:
            body = response.text or ""
        except Exception:
            body = ""
    if isinstance(exc, httpx.HTTPError):
        message = body or f"Decal {action} network request failed"
    else:
        message = str(exc)
    sanitized = re.sub(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [redacted]", message.replace("\n", " ")).strip()
    sanitized = re.sub(r"(?i)(decal|api)[-_ ]?key[:= ]+[A-Za-z0-9._~+/=-]+", r"\1_key=[redacted]", sanitized)
    sanitized = re.sub(r"(?i)(authorization|token|secret|password)\"?\s*[:=]\s*\"?[^\",}\s]+", r"\1=[redacted]", sanitized)
    sanitized = re.sub(r'(?i)(paymentDestination)"?\s*[:=]\s*"?[^",}\s]+', r"\1=[redacted]", sanitized)
    sanitized = re.sub(r"\b[1-9A-HJ-NP-Za-km-z]{32,64}\b", "[redacted-wallet]", sanitized)
    if len(sanitized) > 500:
        sanitized = sanitized[:497] + "..."
    parts = [f"status={status}"] if status else []
    if sanitized:
        parts.append(sanitized)
    return f"Decal {action} failed" + (f": {'; '.join(parts)}" if parts else "")


def build_decal_checkout_urls(settings: Settings, payment_session_id: str) -> tuple[str, str]:
    if not settings.public_base_url:
        raise HTTPException(status_code=503, detail="PAYJENT_PUBLIC_BASE_URL is required for Decal checkout")
    base = settings.canonical_public_base_url or settings.public_base_url.rstrip("/")
    values = {"payment_session_id": payment_session_id}
    success_template = settings.decal_success_url_template or f"{base}/status/{{payment_session_id}}?checkout=success"
    callback_template = settings.decal_callback_url_template or f"{base}/api/v1/webhooks/decal?payment_session_id={{payment_session_id}}"
    return success_template.format(**values), callback_template.format(**values)


def build_decal_checkout_payload(quote: Quote, payment_session: PaymentSession, settings: Settings) -> dict[str, Any]:
    success_url, callback_url = build_decal_checkout_urls(settings, payment_session.id)
    if settings.is_production and not settings.decal_payment_destination:
        raise HTTPException(status_code=503, detail="PAYJENT_DECAL_PAYMENT_DESTINATION is required for production Decal checkout")
    payload: dict[str, Any] = {
        "items": [{"name": quote.request_summary[:120] or "Payjent request", "quantity": 1, "unitPrice": quote.amount_minor}],
        "successUrl": success_url,
        "callbackUrl": callback_url,
    }
    if settings.decal_payment_destination:
        payload["paymentDestination"] = settings.decal_payment_destination
    return payload


def _response_value(response: dict[str, Any], *keys: str) -> Any:
    value: Any = response
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def create_decal_checkout_session(
    quote: Quote,
    payment_session: PaymentSession,
    settings: Settings,
    client: DecalCheckoutClient | None = None,
) -> tuple[str, str]:
    if not settings.decal_api_key:
        raise HTTPException(status_code=503, detail="PAYJENT_DECAL_API_KEY is required for Decal checkout")
    payload = build_decal_checkout_payload(quote, payment_session, settings)
    decal_client = client or DecalHTTPClient(settings.decal_api_key, settings.decal_api_base_url)
    try:
        created = decal_client.create_checkout_session(payload, payment_session.id)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=_safe_decal_error(exc, "checkout session creation")) from exc
    provider_session_id = _response_value(created, "id") or _response_value(created, "session", "id")
    hosted_url = _response_value(created, "url") or _response_value(created, "session", "url")
    if not provider_session_id or not hosted_url:
        raise HTTPException(status_code=502, detail="Decal checkout session response missing id or url")
    return str(provider_session_id), str(hosted_url)


def retrieve_decal_checkout_session(session_id: str, settings: Settings, client: DecalCheckoutClient | None = None) -> dict[str, Any]:
    if not settings.decal_api_key:
        raise HTTPException(status_code=503, detail="PAYJENT_DECAL_API_KEY is required for Decal session verification")
    decal_client = client or DecalHTTPClient(settings.decal_api_key, settings.decal_api_base_url)
    try:
        return decal_client.retrieve_checkout_session(session_id)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=_safe_decal_error(exc, "session retrieval")) from exc
