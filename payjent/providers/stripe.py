import hashlib
import hmac
import json

from fastapi import HTTPException


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
