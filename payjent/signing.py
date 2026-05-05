import hashlib
import hmac
import time
from typing import Any

from .money import canonical_json


def sign_payload(payload: dict[str, Any], secret: str) -> str:
    return hmac.new(secret.encode(), canonical_json(payload).encode(), hashlib.sha256).hexdigest()


def verify_signature(payload: dict[str, Any], signature: str, secret: str) -> bool:
    return hmac.compare_digest(sign_payload(payload, secret), signature)


PAYJENT_TIMESTAMP_HEADER = "X-Payjent-Timestamp"
PAYJENT_SIGNATURE_HEADER = "X-Payjent-Signature"


def sign_webhook_payload(payload: dict[str, Any], secret: str, timestamp: int | None = None) -> tuple[str, str]:
    ts = str(int(timestamp if timestamp is not None else time.time()))
    signed = f"{ts}.{canonical_json(payload)}"
    signature = hmac.new(secret.encode(), signed.encode(), hashlib.sha256).hexdigest()
    return ts, f"v1={signature}"


def verify_webhook_signature(
    payload: dict[str, Any],
    timestamp: str,
    signature: str,
    secret: str,
    *,
    tolerance_seconds: int = 300,
    now: int | None = None,
) -> bool:
    try:
        ts_int = int(timestamp)
    except (TypeError, ValueError):
        return False
    current = int(now if now is not None else time.time())
    if tolerance_seconds >= 0 and abs(current - ts_int) > tolerance_seconds:
        return False
    _ts, expected = sign_webhook_payload(payload, secret, ts_int)
    candidates = [signature]
    if signature.startswith("v1="):
        candidates.append(signature[3:])
    return any(hmac.compare_digest(expected, candidate) or hmac.compare_digest(expected[3:], candidate) for candidate in candidates)
