import hmac
import base64
import json
import time
from hashlib import pbkdf2_hmac, sha256
from secrets import token_urlsafe

from fastapi import Depends, Header, HTTPException
from sqlmodel import Session, select

from .config import Settings, get_settings
from .db import get_session
from .models import Account, BotCredential


DASHBOARD_SESSION_COOKIE = "payjent_dashboard_session"
PASSWORD_ITERATIONS = 210_000


def hash_api_key(api_key: str, secret: str) -> str:
    """Return a keyed SHA-256 digest for an API key; never store plaintext keys."""
    return hmac.new(secret.encode("utf-8"), api_key.encode("utf-8"), sha256).hexdigest()


def verify_api_key(api_key: str, key_hash: str, secret: str) -> bool:
    return hmac.compare_digest(hash_api_key(api_key, secret), key_hash)


def generate_api_key() -> str:
    return f"payjent_{token_urlsafe(32)}"


def normalize_email(email: str) -> str:
    return email.strip().lower()


def hash_password(password: str) -> str:
    salt = token_urlsafe(24).encode("utf-8")
    digest = pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS)
    return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${salt.decode()}${base64.urlsafe_b64encode(digest).decode()}"


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        algorithm, iterations, salt, digest = stored_hash.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        expected = base64.urlsafe_b64decode(digest.encode("utf-8"))
        actual = pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), int(iterations))
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("utf-8").rstrip("=")


def _unb64(data: str) -> bytes:
    return base64.urlsafe_b64decode((data + "=" * (-len(data) % 4)).encode("utf-8"))


def create_dashboard_session_cookie(account_id: str, secret: str, ttl_seconds: int = 60 * 60 * 24 * 7) -> str:
    payload = {"account_id": account_id, "exp": int(time.time()) + ttl_seconds}
    payload_b64 = _b64(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signature = hmac.new(secret.encode("utf-8"), payload_b64.encode("utf-8"), sha256).digest()
    return f"{payload_b64}.{_b64(signature)}"


def verify_dashboard_session_cookie(cookie_value: str | None, secret: str) -> str | None:
    if not cookie_value or "." not in cookie_value:
        return None
    payload_b64, signature_b64 = cookie_value.split(".", 1)
    expected = hmac.new(secret.encode("utf-8"), payload_b64.encode("utf-8"), sha256).digest()
    try:
        supplied = _unb64(signature_b64)
        payload = json.loads(_unb64(payload_b64))
    except Exception:
        return None
    if not hmac.compare_digest(supplied, expected):
        return None
    if int(payload.get("exp", 0)) < int(time.time()):
        return None
    account_id = payload.get("account_id")
    return account_id if isinstance(account_id, str) and account_id else None


def get_account_from_cookie(cookie_value: str | None, session: Session, settings: Settings) -> Account | None:
    account_id = verify_dashboard_session_cookie(cookie_value, settings.signing_secret)
    if not account_id:
        return None
    return session.get(Account, account_id)


def create_bot_credential(session: Session, bot_id: str, api_key: str, secret: str, role: str = "bot") -> BotCredential:
    credential = BotCredential(bot_id=bot_id, key_hash=hash_api_key(api_key, secret), role=role)
    session.add(credential)
    session.commit()
    session.refresh(credential)
    return credential


def _extract_bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token


def require_bot_credential(
    authorization: str | None = Header(default=None, alias="Authorization"),
    x_payjent_bot_key: str | None = Header(default=None, alias="X-Payjent-Bot-Key"),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> BotCredential:
    api_key = _extract_bearer(authorization) or x_payjent_bot_key
    if not api_key:
        raise HTTPException(status_code=401, detail="missing API key")

    key_hash = hash_api_key(api_key, settings.signing_secret)
    credential = session.exec(select(BotCredential).where(BotCredential.key_hash == key_hash)).first()
    if not credential or not hmac.compare_digest(credential.key_hash, key_hash):
        raise HTTPException(status_code=401, detail="invalid API key")
    return credential


def require_operator_credential(credential: BotCredential = Depends(require_bot_credential)) -> BotCredential:
    if credential.role not in {"operator", "admin"}:
        raise HTTPException(status_code=403, detail="operator credential required")
    return credential
