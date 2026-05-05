import hashlib
import hmac
from typing import Any

from .money import canonical_json


def sign_payload(payload: dict[str, Any], secret: str) -> str:
    return hmac.new(secret.encode(), canonical_json(payload).encode(), hashlib.sha256).hexdigest()


def verify_signature(payload: dict[str, Any], signature: str, secret: str) -> bool:
    return hmac.compare_digest(sign_payload(payload, secret), signature)
