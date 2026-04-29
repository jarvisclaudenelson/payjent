import hmac
from hashlib import sha256
from secrets import token_urlsafe

from fastapi import Depends, Header, HTTPException
from sqlmodel import Session, select

from .config import Settings, get_settings
from .db import get_session
from .models import BotCredential


def hash_api_key(api_key: str, secret: str) -> str:
    """Return a keyed SHA-256 digest for an API key; never store plaintext keys."""
    return hmac.new(secret.encode("utf-8"), api_key.encode("utf-8"), sha256).hexdigest()


def verify_api_key(api_key: str, key_hash: str, secret: str) -> bool:
    return hmac.compare_digest(hash_api_key(api_key, secret), key_hash)


def generate_api_key() -> str:
    return f"payjent_{token_urlsafe(32)}"


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
