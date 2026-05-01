from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException

from .config import Settings


@dataclass(frozen=True)
class WorkOSProfile:
    email: str
    user_id: str | None = None


def workos_configured(settings: Settings) -> bool:
    return bool(settings.workos_api_key and settings.workos_client_id)


def workos_redirect_uri(settings: Settings) -> str | None:
    if settings.workos_redirect_uri:
        return settings.workos_redirect_uri
    if settings.public_base_url:
        return settings.public_base_url.rstrip("/") + "/auth/workos/callback"
    return None


def require_workos_config(settings: Settings) -> str:
    redirect_uri = workos_redirect_uri(settings)
    if not settings.workos_api_key or not settings.workos_client_id or not redirect_uri:
        raise HTTPException(status_code=503, detail="WorkOS AuthKit is not configured for this Payjent instance.")
    return redirect_uri


def create_workos_client(settings: Settings) -> Any:
    if not settings.workos_api_key or not settings.workos_client_id:
        raise HTTPException(status_code=503, detail="WorkOS AuthKit is not configured for this Payjent instance.")
    try:
        from workos import WorkOSClient  # type: ignore
    except Exception as exc:  # pragma: no cover - exercised only when package missing at runtime
        raise HTTPException(status_code=503, detail="WorkOS AuthKit SDK is not installed.") from exc
    return WorkOSClient(api_key=settings.workos_api_key, client_id=settings.workos_client_id)


def get_authorization_url(client: Any, redirect_uri: str) -> str:
    return client.user_management.get_authorization_url(provider="authkit", redirect_uri=redirect_uri)


def authenticate_with_code(client: Any, code: str) -> WorkOSProfile:
    try:
        result = client.user_management.authenticate_with_code(code=code)
    except TypeError:
        # Some WorkOS SDK versions accept an optional session dict for cookie/session options.
        result = client.user_management.authenticate_with_code(code=code, session={})
    user = _get(result, "user") or result
    email = _get(user, "email")
    user_id = _get(user, "id") or _get(user, "user_id")
    if not isinstance(email, str) or "@" not in email:
        raise ValueError("WorkOS authentication response did not include a valid email")
    return WorkOSProfile(email=email, user_id=str(user_id) if user_id else None)


def _get(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        return value.get(key)
    return getattr(value, key, None)
