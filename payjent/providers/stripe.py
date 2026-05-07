from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from fastapi import HTTPException

from payjent.config import Settings
from payjent.models import PaymentSession, Quote


class StripeCheckoutClient(Protocol):
    def create_checkout_session(self, payload: dict[str, Any], idempotency_key: str) -> dict[str, Any]: ...


class StripeRefundClient(Protocol):
    def retrieve_checkout_session(self, provider_session_id: str) -> Any: ...
    def create_refund(self, payload: dict[str, Any], idempotency_key: str) -> Any: ...


@dataclass
class StripeSDKCheckoutClient:
    secret_key: str

    def create_checkout_session(self, payload: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
        try:
            import stripe  # type: ignore
        except ImportError as exc:
            raise HTTPException(status_code=503, detail="Stripe SDK is not installed") from exc
        stripe.api_key = self.secret_key
        return stripe.checkout.Session.create(**payload, idempotency_key=idempotency_key)

    def retrieve_checkout_session(self, provider_session_id: str) -> Any:
        try:
            import stripe  # type: ignore
        except ImportError as exc:
            raise HTTPException(status_code=503, detail="Stripe SDK is not installed") from exc
        stripe.api_key = self.secret_key
        return stripe.checkout.Session.retrieve(provider_session_id)

    def create_refund(self, payload: dict[str, Any], idempotency_key: str) -> Any:
        try:
            import stripe  # type: ignore
        except ImportError as exc:
            raise HTTPException(status_code=503, detail="Stripe SDK is not installed") from exc
        stripe.api_key = self.secret_key
        return stripe.Refund.create(**payload, idempotency_key=idempotency_key)


def build_checkout_urls(settings: Settings, payment_session_id: str) -> tuple[str, str]:
    if not settings.public_base_url:
        raise HTTPException(status_code=503, detail="PAYJENT_PUBLIC_BASE_URL is required for Stripe checkout")
    base = settings.public_base_url.rstrip("/")
    values = {"payment_session_id": payment_session_id}
    success_template = settings.stripe_success_url_template or f"{base}/status/{{payment_session_id}}?checkout=success"
    cancel_template = settings.stripe_cancel_url_template or f"{base}/pay/{{payment_session_id}}?checkout=cancelled"
    return success_template.format(**values), cancel_template.format(**values)


def build_stripe_checkout_payload(quote: Quote, payment_session: PaymentSession, settings: Settings) -> dict[str, Any]:
    success_url, cancel_url = build_checkout_urls(settings, payment_session.id)
    metadata = {
        "quote_id": quote.id,
        "payment_session_id": payment_session.id,
        "bot_id": quote.bot_id,
        "request_hash": quote.request_hash,
    }
    return {
        "mode": "payment",
        "success_url": success_url,
        "cancel_url": cancel_url,
        "client_reference_id": payment_session.id,
        "metadata": metadata,
        "payment_intent_data": {"metadata": metadata},
        "line_items": [
            {
                "quantity": 1,
                "price_data": {
                    "currency": quote.currency.lower(),
                    "unit_amount": quote.amount_minor,
                    "product_data": {"name": quote.request_summary[:120] or "Payjent request"},
                },
            }
        ],
    }


def _safe_stripe_checkout_error(exc: Exception) -> str:
    """Return a non-secret, actionable Stripe checkout failure for API callers."""
    message = getattr(exc, "user_message", None) or getattr(exc, "message", None) or str(exc)
    code = getattr(exc, "code", None)
    status = getattr(exc, "http_status", None)
    if isinstance(exc, httpx.HTTPError):
        message = "Stripe checkout network request failed"
    detail = "Stripe checkout session creation failed"
    parts = []
    if status:
        parts.append(f"status={status}")
    if code:
        parts.append(f"code={code}")
    if message:
        sanitized = str(message).replace("\n", " ").strip()
        sanitized = re.sub(r"\b(?:sk|pk|rk)_(?:test|live)_[A-Za-z0-9_=-]+", "[redacted_stripe_key]", sanitized)
        if len(sanitized) > 300:
            sanitized = sanitized[:297] + "..."
        parts.append(sanitized)
    if parts:
        detail = f"{detail}: {'; '.join(parts)}"
    return detail


def _safe_stripe_refund_error(exc: Exception) -> str:
    message = getattr(exc, "user_message", None) or getattr(exc, "message", None) or str(exc)
    code = getattr(exc, "code", None)
    status = getattr(exc, "http_status", None)
    if isinstance(exc, httpx.HTTPError):
        message = "Stripe refund network request failed"
    detail = "Stripe refund creation failed"
    parts = []
    if status:
        parts.append(f"status={status}")
    if code:
        parts.append(f"code={code}")
    if message:
        sanitized = str(message).replace("\n", " ").strip()
        sanitized = re.sub(r"\b(?:sk|pk|rk)_(?:test|live)_[A-Za-z0-9_=-]+", "[redacted_stripe_key]", sanitized)
        if len(sanitized) > 300:
            sanitized = sanitized[:297] + "..."
        parts.append(sanitized)
    if parts:
        detail = f"{detail}: {'; '.join(parts)}"
    return detail


def _stripe_response_value(response: Any, key: str) -> Any:
    """Read values from Stripe SDK objects and plain dict test doubles."""
    if isinstance(response, dict):
        return response.get(key)
    try:
        return response[key]
    except (KeyError, TypeError, AttributeError):
        return getattr(response, key, None)


def create_stripe_checkout_session(
    quote: Quote,
    payment_session: PaymentSession,
    settings: Settings,
    client: StripeCheckoutClient | None = None,
) -> tuple[str, str]:
    if not settings.stripe_secret_key:
        raise HTTPException(status_code=503, detail="PAYJENT_STRIPE_SECRET_KEY is required for Stripe checkout")
    payload = build_stripe_checkout_payload(quote, payment_session, settings)
    stripe_client = client or StripeSDKCheckoutClient(settings.stripe_secret_key)
    # Stripe idempotency must identify this exact Checkout Session creation attempt,
    # not the caller's semantic paid-action/request idempotency key. Agents often
    # reuse request-level keys while changing amounts or endpoints during retries;
    # forwarding those to Stripe can collide with stale Checkout Session params and
    # surface as production 500s. Payjent still stores caller idempotency for its
    # own duplicate-detection, but Stripe gets the unique payment session id.
    try:
        created = stripe_client.create_checkout_session(payload, payment_session.id)
    except HTTPException:
        raise
    except Exception as exc:
        detail = _safe_stripe_checkout_error(exc)
        raise HTTPException(status_code=502, detail=detail) from exc
    provider_session_id = _stripe_response_value(created, "id")
    hosted_url = _stripe_response_value(created, "url")
    if not provider_session_id or not hosted_url:
        raise HTTPException(status_code=502, detail="Stripe checkout session response missing id or url")
    return str(provider_session_id), str(hosted_url)


def create_stripe_refund(
    payment_session: PaymentSession,
    quote: Quote,
    settings: Settings,
    *,
    reason: str | None = None,
    client: StripeRefundClient | None = None,
) -> tuple[str, str]:
    if not settings.stripe_secret_key:
        raise HTTPException(status_code=503, detail="PAYJENT_STRIPE_SECRET_KEY is required for Stripe refunds")
    if payment_session.provider != "stripe":
        raise HTTPException(status_code=409, detail="payment session provider is not stripe")
    if not payment_session.provider_session_id:
        raise HTTPException(status_code=409, detail="Stripe provider_session_id is required for refund")
    stripe_client = client or StripeSDKCheckoutClient(settings.stripe_secret_key)
    try:
        checkout_session = stripe_client.retrieve_checkout_session(payment_session.provider_session_id)
        payment_intent = _stripe_response_value(checkout_session, "payment_intent")
        if not payment_intent:
            raise HTTPException(status_code=409, detail="Stripe checkout session missing payment_intent")
        refund_payload: dict[str, Any] = {
            "payment_intent": str(payment_intent),
            "amount": quote.amount_minor,
            "metadata": {
                "quote_id": quote.id,
                "payment_session_id": payment_session.id,
                "bot_id": quote.bot_id,
            },
        }
        if reason:
            refund_payload["metadata"]["payjent_refund_reason"] = reason[:300]
        refund = stripe_client.create_refund(refund_payload, f"refund:{payment_session.id}")
    except HTTPException:
        raise
    except Exception as exc:
        detail = _safe_stripe_refund_error(exc)
        raise HTTPException(status_code=502, detail=detail) from exc
    refund_id = _stripe_response_value(refund, "id")
    refund_status = _stripe_response_value(refund, "status") or "unknown"
    if not refund_id:
        raise HTTPException(status_code=502, detail="Stripe refund response missing id")
    return str(refund_id), str(refund_status)


def verify_stripe_signature(raw_body: bytes, signature_header: str | None, secret: str | None) -> None:
    """Verify Stripe-style webhook signature when a webhook secret is configured.

    Supports deterministic test signing using the standard Stripe signed payload
    format: HMAC_SHA256(secret, f"{timestamp}.{raw_body}") supplied as `v1` in the
    `Stripe-Signature` header. Timestamp freshness is intentionally not enforced
    for deterministic local tests.
    """
    if not secret:
        raise HTTPException(status_code=503, detail="Stripe webhook secret not configured")
    if not signature_header:
        raise HTTPException(status_code=400, detail="missing Stripe signature")

    parts: dict[str, list[str]] = {}
    for item in signature_header.split(","):
        key, sep, value = item.partition("=")
        if sep:
            parts.setdefault(key, []).append(value)
    timestamp = parts.get("t", [None])[0]
    signatures = parts.get("v1", [])
    if not timestamp or not signatures:
        raise HTTPException(status_code=400, detail="invalid Stripe signature")

    signed_payload = timestamp.encode("utf-8") + b"." + raw_body
    expected = hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, sig) for sig in signatures):
        raise HTTPException(status_code=400, detail="invalid Stripe signature")


def parse_stripe_event(raw_body: bytes) -> dict:
    try:
        event = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail="invalid webhook payload") from exc
    if not isinstance(event, dict):
        raise HTTPException(status_code=400, detail="invalid webhook payload")
    return event
